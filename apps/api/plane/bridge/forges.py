# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""biplane (BIP-15): forge personalities for the git bridge.

The bridge logic — durable inbox, lease, selection, the write boundary and its
durable refusal — is forge-agnostic. (It once listed advance and target
resolution here; both are deleted, BIP-67.) Only the *request envelope* differs
between hosts, in exactly three places:

  1. how a delivery is authenticated
  2. which header carries the delivery id
  3. which header carries the event name, and what it is called

This module isolates those three, and nothing else. Anything that starts
reaching into payload shape or API calls belongs in a later slice, not here —
that surface is larger and deserves its own review.

AUTHENTICATION IS NOT UNIFORM, AND THAT MATTERS

  Forgejo/Gitea and GitHub both sign the raw body with HMAC-SHA256 under a
  shared secret, so a delivery is bound to its bytes: tampering invalidates it.
  GitLab does not. GitLab sends the shared secret back verbatim in
  `X-Gitlab-Token` and nothing covers the body.

  That is a genuine, unavoidable difference in what a verified delivery means,
  not a detail to paper over with a common interface. `body_bound` states it
  per personality so a caller can refuse the weaker guarantee if it needs the
  stronger one, rather than assuming every forge gives what Forgejo gives.
"""

import hashlib
import hmac


def _constant_time_equals(provided: str, expected: str) -> bool:
    """Constant-time compare of two credential strings, safe on ANY input.

    biplane: hmac.compare_digest RAISES on str operands containing non-ASCII
    ("comparing strings with non-ASCII characters is not supported"), and
    `provided` is an attacker-controlled header. Passing it in directly turned
    a single non-ASCII byte into an unhandled 500 from an unauthenticated
    endpoint — the deployed defect fixed on main by the non-ASCII-signature PR.

    Every personality routes through here so no future forge can reintroduce it
    by writing the obvious thing.

    ENCODE AS UTF-8, NOT ASCII (Morrow RC 3124)

    The first version encoded both sides as ASCII and returned False on
    UnicodeEncodeError, reasoning that a credential which is not ASCII cannot
    be a hex digest. That is true of a SIGNATURE, and wrong for a TOKEN.

    GitLab does not sign the body; it echoes the configured secret back in
    `X-Gitlab-Token`, and that secret is an arbitrary operator-chosen string.
    So a perfectly valid non-ASCII secret failed to encode on the EXPECTED
    side and could never match: GitLab deliveries refused forever, with no
    configuration error to explain it, while the very same secret kept working
    for Forgejo because there it is only ever an HMAC key. A silent, permanent,
    unexplained failure is worse than the crash it replaced.

    UTF-8 fixes both halves: `compare_digest` on BYTES never raises whatever
    the header contains, so the unauthenticated 500 stays fixed, and a
    legitimate non-ASCII secret compares equal to itself.

    Non-ASCII input to a hex-digest comparison still fails, as it must — but
    now because the bytes genuinely differ, rather than because we refused to
    look at them.
    """
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _dig(payload, *path):
    """payload["a"]["b"] without exploding on a non-dict at any level."""
    node = payload
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


class Forge:
    """One forge's request envelope. Subclasses set the class attributes."""

    name = ""
    # Headers are tried in order; the first present one wins.
    signature_headers = ()
    delivery_headers = ()
    event_headers = ()
    # True when the signature covers the request body, so tampering is
    # detectable. False when the forge only echoes a shared secret.
    body_bound = True
    # The Django setting holding THIS forge's credential (Morrow 10146).
    # Sharing one secret across personalities would let GitLab's echoed
    # bearer token double as the HMAC key for the body-bound forges — the
    # opt-in's weaker guarantee would silently infect the stronger doors.
    secret_setting = None
    instance_id_setting = None
    # Raw event name -> the bridge's canonical name. Anything absent from this
    # map is not an event the bridge acts on.
    event_map = {}

    @classmethod
    def _first_header(cls, request, names):
        for name in names:
            value = request.headers.get(name)
            if value:
                return value
        return None

    @classmethod
    def delivery_id(cls, request):
        return cls._first_header(request, cls.delivery_headers)

    @classmethod
    def event(cls, request):
        """Canonical event name, or "" when this delivery is not one we act on."""
        raw = cls._first_header(request, cls.event_headers) or ""
        return cls.event_map.get(raw, "")

    @classmethod
    def verify(cls, request, secret: str) -> bool:
        """HMAC-SHA256 over the raw body, hex, constant-time compared."""
        provided = cls._first_header(request, cls.signature_headers) or ""
        expected = hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
        return _constant_time_equals(provided, expected)

    # ---- payload shape (BIP-15 slice 3, wired per Morrow RC 3170) ----------
    # The headers above say WHO sent a delivery; the attributes and methods
    # below say WHAT it contains, and they diverge more than the headers do.
    # The defaults are the Forgejo/Gitea/GitHub lineage, which share a payload
    # shape. GitLab does not, and overrides what differs.
    #
    # The bridge's `_validate_shape` REMAINS the single typed boundary — but it
    # now reads the field locations from the forge (`repo_path`, `total_field`)
    # and delegates the one genuinely divergent shape (the merge-request
    # object) to `validate_pull_request`. Strictness is per-forge on purpose:
    # laundering a GitLab payload into the Forgejo shape before validation
    # would turn "malformed" into "empty" and accept it.
    #
    # Accessors are total. A malformed payload returns an empty value here and
    # is rejected by the validator that exists for it, rather than raising an
    # AttributeError somewhere downstream.

    # Where the repository's full name lives, and under which key a push
    # declares its commit count (None: this forge never declares one).
    # Validation error messages use these names, so an operator reads the
    # field their forge actually sent.
    repo_path = ("repository", "full_name")
    # The repository's STABLE numeric id — the tenancy key (Morrow, PR 18
    # gate): a display path is mutable, so a rename followed by path reuse
    # would transfer workspace authority to a different repository. The id
    # survives renames and cannot be reused.
    stable_id_path = ("repository", "id")
    total_field = "total_commits"
    # Some forges declare no total but CAP the delivered commits array; a
    # delivery at the cap may have lost the rest (Morrow 10147). None: no cap.
    commit_cap = None
    # Only the Forgejo/Gitea API has a range resolver in this bridge. A forge
    # without one must not pretend a truncated push is resolvable.
    resolves_push_ranges = False

    @classmethod
    def repo_full_name(cls, payload):
        return _dig(payload, *cls.repo_path)

    @classmethod
    def repo_stable_id(cls, payload):
        """The forge's own immutable repository id, or None. Total, like every
        accessor: an absent/malformed id means no id-keyed scope can match —
        fail closed at the tenancy lookup, never crash."""
        value = _dig(payload, *cls.stable_id_path)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @classmethod
    def commits(cls, payload):
        commits = payload.get("commits") if isinstance(payload, dict) else None
        return commits if isinstance(commits, list) else []

    @classmethod
    def declared_commit_total(cls, payload):
        """How many commits the push CLAIMS, which may exceed those delivered.

        That difference is what tells the bridge a push was truncated, so a
        forge keeping the count under another key must report it here or
        truncation goes undetected and references are silently lost. A forge
        with NO total field returns None even if the payload carries a
        Forgejo-shaped count — reading another lineage's field would invent a
        contract the sender never made.
        """
        if cls.total_field is None or not isinstance(payload, dict):
            return None
        return payload.get(cls.total_field)

    @classmethod
    def validate_pull_request(cls, payload):
        """Raise ValueError naming the offending field, in THIS forge's terms.

        Required/typed checks only — the semantic decision (is this a merge?)
        stays in `merged_pull_request`."""
        action = payload.get("action")
        if not isinstance(action, str):
            raise ValueError("action (string) is required for pull_request")
        pr = payload.get("pull_request")
        if not isinstance(pr, dict):
            raise ValueError("pull_request (object) is required")
        if action == "closed" and not isinstance(pr.get("merged"), bool):
            raise ValueError("pull_request.merged (boolean) is required when closed")
        for key in ("title", "body"):
            value = pr.get(key)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"pull_request.{key} must be a string")
        number = pr.get("number")
        if number is not None and not (isinstance(number, int) and not isinstance(number, bool)):
            raise ValueError("pull_request.number must be an integer")

    @classmethod
    def merged_pull_request(cls, payload):
        """(merged, fields, number). merged is False when this is not a merge.

        `fields` preserves the provider's exact (title, body) boundary. Scope A
        admits only the body for ticket nomination; the title remains available
        as inert event data rather than being concatenated or classified.
        """
        if not isinstance(payload, dict):
            return False, ("", ""), "?"
        pr = payload.get("pull_request")
        if not isinstance(pr, dict):
            return False, ("", ""), "?"
        merged = payload.get("action") == "closed" and pr.get("merged") is True
        fields = (pr.get("title") or "", pr.get("body") or "")
        number = pr.get("number") if pr.get("number") is not None else "?"
        return merged, fields, number

    @classmethod
    def merge_identity(cls, payload):
        """(pr_number, merge_commit_sha) for the SEMANTIC KEY — the immutable
        identity of a merged PR. Per-forge: this base reads the github/forgejo
        `pull_request` family. Either component absent => (None, None) => the
        caller assigns no key (a partial tuple must never form one)."""
        if not isinstance(payload, dict):
            return None, None
        pr = payload.get("pull_request")
        if not isinstance(pr, dict):
            return None, None
        number = pr.get("number")
        merge_sha = pr.get("merge_commit_sha")
        number = number if (isinstance(number, int) and not isinstance(number, bool)) else None
        merge_sha = merge_sha if (isinstance(merge_sha, str) and merge_sha) else None
        return number, merge_sha


class ForgejoForge(Forge):
    """Forgejo, and Gitea — same wire format, different header prefix.

    This must stay byte-compatible with the pre-BIP-15 behaviour: same headers,
    same HMAC, same order. It is the only personality with a deployed bridge
    behind it.
    """

    name = "forgejo"
    signature_headers = ("X-Forgejo-Signature", "X-Gitea-Signature")
    delivery_headers = ("X-Forgejo-Delivery", "X-Gitea-Delivery")
    event_headers = ("X-Forgejo-Event", "X-Gitea-Event")
    body_bound = True
    event_map = {
        "push": "push",
        "pull_request": "pull_request",
        "pull_request_review_rejected": "review_rejected",
    }
    secret_setting = "FORGEJO_WEBHOOK_SECRET"  # legacy name kept: deployed bridges keep working
    instance_id_setting = "FORGEJO_INSTANCE_ID"
    resolves_push_ranges = True


class GitHubForge(Forge):
    name = "github"
    signature_headers = ("X-Hub-Signature-256",)
    delivery_headers = ("X-GitHub-Delivery",)
    event_headers = ("X-GitHub-Event",)
    body_bound = True
    event_map = {"push": "push", "pull_request": "pull_request"}
    secret_setting = "GITHUB_WEBHOOK_SECRET"
    instance_id_setting = "GITHUB_INSTANCE_ID"
    # GitHub's push payload declares NO commit total; its commits array is
    # capped at 2048 and the docs say to use the Commits API for the rest
    # (docs.github.com/en/webhooks/webhook-events-and-payloads#push, Morrow
    # 10147). A delivery AT the cap may therefore have lost commits with no
    # field saying so — the cap itself is the only truncation signal.
    total_field = None
    commit_cap = 2048

    @classmethod
    def verify(cls, request, secret: str) -> bool:
        # GitHub prefixes the digest with "sha256=". Build the expected value
        # WITH the prefix and compare the whole string, rather than stripping
        # the prefix off what was provided — stripping would let a delivery
        # carrying a bare hex digest, or a "sha1=" digest, be accepted.
        provided = cls._first_header(request, cls.signature_headers) or ""
        digest = hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
        return _constant_time_equals(provided, f"sha256={digest}")


class GitLabForge(Forge):
    """GitLab.

    NOT body-bound. GitLab echoes the configured secret in `X-Gitlab-Token`;
    no signature covers the payload. A verified GitLab delivery proves the
    sender knew the secret, and nothing about the bytes.
    """

    name = "gitlab"
    signature_headers = ("X-Gitlab-Token",)
    delivery_headers = ("X-Gitlab-Event-UUID",)
    event_headers = ("X-Gitlab-Event",)
    body_bound = False
    secret_setting = "GITLAB_WEBHOOK_TOKEN"
    instance_id_setting = "GITLAB_INSTANCE_ID"
    # GitLab's event names are human-cased and differ from everyone else's.
    event_map = {
        "Push Hook": "push",
        "Merge Request Hook": "pull_request",
    }

    @classmethod
    def verify(cls, request, secret: str) -> bool:
        provided = cls._first_header(request, cls.signature_headers) or ""
        return _constant_time_equals(provided, secret)

    # ---- payload overrides -------------------------------------------------
    # GitLab did not inherit the Forgejo/GitHub payload; it is a different
    # shape, not a renamed one:
    #
    #   repository        -> project.path_with_namespace
    #   total_commits     -> total_commits_count
    #   pull_request.*    -> object_attributes.*, and merged is an ACTION
    #                        value ("merge"), not a boolean field
    #   body              -> description
    #   number            -> iid, NOT id. iid is the per-project number a
    #                        human sees and would write in a commit message;
    #                        id is a global row id and referencing it would
    #                        point at the wrong merge request.

    repo_path = ("project", "path_with_namespace")
    stable_id_path = ("project", "id")
    total_field = "total_commits_count"

    @classmethod
    def validate_pull_request(cls, payload):
        attrs = payload.get("object_attributes")
        if not isinstance(attrs, dict):
            raise ValueError("object_attributes (object) is required")
        if not isinstance(attrs.get("action"), str):
            raise ValueError("object_attributes.action (string) is required")
        for key in ("title", "description"):
            value = attrs.get(key)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"object_attributes.{key} must be a string")
        iid = attrs.get("iid")
        if iid is not None and not (isinstance(iid, int) and not isinstance(iid, bool)):
            raise ValueError("object_attributes.iid must be an integer")

    @classmethod
    def merged_pull_request(cls, payload):
        attrs = _dig(payload, "object_attributes")
        if not isinstance(attrs, dict):
            return False, ("", ""), "?"
        merged = attrs.get("action") == "merge"
        # Preserve the provider boundary. Downstream admission treats the
        # description as the body and keeps the title inert.
        fields = (attrs.get("title") or "", attrs.get("description") or "")
        number = attrs.get("iid") if attrs.get("iid") is not None else "?"
        return merged, fields, number

    @classmethod
    def merge_identity(cls, payload):
        # GitLab: the human-referenced number is object_attributes.iid, and the
        # merge sha is object_attributes.merge_commit_sha — NOT the pull_request
        # family, which GitLab does not have (Morrow 3329).
        attrs = _dig(payload, "object_attributes")
        if not isinstance(attrs, dict):
            return None, None
        iid = attrs.get("iid")
        merge_sha = attrs.get("merge_commit_sha")
        iid = iid if (isinstance(iid, int) and not isinstance(iid, bool)) else None
        merge_sha = merge_sha if (isinstance(merge_sha, str) and merge_sha) else None
        return iid, merge_sha


# Order matters only for determinism; the header sets do not overlap.
FORGES = (ForgejoForge, GitHubForge, GitLabForge)


def detect(request):
    """Return the personality whose signature header this request carries.

    Selection is on the SIGNATURE header, not the event header, so a request
    cannot present one forge's event and another's credential and be judged by
    the more permissive of the two. Returns None when nothing matches, which
    callers must treat as unauthenticated — never as a default forge.
    """
    for forge in FORGES:
        if forge._first_header(request, forge.signature_headers):
            return forge
    return None


def by_name(name):
    for forge in FORGES:
        if forge.name == name:
            return forge
    return None
