# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Biplane git bridge: Forgejo webhooks are ANSWERED; no event moves a ticket.

The 2026-07-31 FULLY AUTOMATIC ruling this header used to carry is SUPERSEDED
by the 2026-08-14 write-authority ruling (Scope A, "Who may move a ticket"):
roles move tickets, the bridge is a tool with no authority of its own, and in
this release EVERY board write is refused — no state transition, completed_at,
activity, or backlink, on any event. What the bridge does instead: recognises
directives (selection only, never authority), records a durable per-ticket
refusal or event-level no-ticket diagnostic on the delivery result, and answers
on the pull request where one exists. Target resolution is deleted with the
mutations; it returns with the first authorised write.

Configuration (environment; wired in deployments/selfhost/docker-compose.override.yml,
env.example, AND loaded in plane/settings/common.py — all three or it does
not exist. docker-compose.yml stays byte-for-byte upstream, so Biplane deltas
live in the override):
  FORGEJO_WEBHOOK_SECRET    HMAC-SHA256 secret shared with the Forgejo webhook.
                            Min 16 chars. Unset/short: every delivery rejected.
  FORGEJO_BRIDGE_REPO_MAP   JSON object mapping a repository to the PROJECTS
                            whose work items a directive in that repository may
                            NAME. (It said "may move items in" while no event
                            moves anything; the guard is on SELECTION, and it
                            keeps its full force there — an out-of-scope ref is
                            rejected before it is ever looked up, so the map
                            still decides which tickets this repository can
                            reach at all.) The scope guard (BIP-38,
                            docs/scope-a-architecture.md §M2). Keys are
                            "<provider INSTANCE id>:<stable repository id>" —
                            the prefix is the configured *_INSTANCE_ID value
                            (the same identity semantic keys use), NOT the
                            forge family name; values are non-empty lists of
                            stable project UUIDs, e.g. with
                            FORGEJO_INSTANCE_ID=pi5-forgejo:
                            {"pi5-forgejo:42": ["<project-uuid>"]} — ids on
                            both sides are immutable, so neither a repo
                            rename nor a project rename/identifier reuse can
                            transfer authority, and two same-family instances
                            sharing a repo number stay isolated. A key whose
                            prefix differs from the configured id grants
                            nothing and logs a loud wrong-prefix warning. A ref to any project outside the list
                            is REJECTED and durably recorded in the delivery
                            result. A legacy workspace-slug value is a CONFIG
                            DEFECT (503) naming the migration: list the
                            workspace's project UUIDs explicitly — the
                            workspace-wide form is the live cross-project
                            mover this guard closes.
                            A bare full_name key ("owner/repo") still resolves
                            but is a FORGEJO-ONLY legacy migration convenience,
                            not the tenancy boundary: it logs a loud warning
                            naming the id-keyed entry to migrate to, and a
                            rename plus path reuse would hand this workspace to
                            a different repository. Do not configure new
                            deployments with path keys.
                            Unset or empty string while the secret is set =
                            config defect (503). An explicit "{}" is a valid
                            map that scopes nothing (deliveries are inert
                            200s). An unmapped repo is a legitimate no-op.
  FORGEJO_BASE_URL          Forgejo root URL, e.g. http://forgejo:3000. Only
  FORGEJO_BRIDGE_API_TOKEN  needed for pushes exceeding Forgejo's webhook
                            commit limit (default 15): the bridge then resolves
                            the full commit range from the Forgejo API. That
                            range resolution is its ONLY remaining use: the
                            review fetch it used to authorise is deleted, since
                            the ask now reads the signed event body and needs
                            no forge permission at all.
  FORGEJO_BRIDGE_WRITE_TOKEN
                            The REPLY credential, and the only write capability
                            the bridge has in this release. Unset or empty: the
                            bridge decides and records exactly as before, and
                            says nothing on the pull request — refusals stay
                            durable on the delivery result, but no person is
                            told. Deliberately SEPARATE from the read token so
                            the two capabilities can be granted independently,
                            and so a deployment that wants the bridge silent
                            has a way to say so that is not "break the read
                            path". A comment is the entire write surface: there
                            is no board credential to configure, because there
                            is no board write to authorise.

Delivery reliability (RC 3069/3070): Forgejo 15.x marks a webhook task
delivered BEFORE the HTTP request and never auto-retries a 5xx; replay is a
manual UI action only. Every signed, well-formed delivery is persisted to the
ForgejoDelivery inbox BEFORE processing, keyed by the REQUIRED
X-Forgejo-Delivery uuid and bound to event/repo/HMAC-covered body digest.
Work is CLAIMED atomically into a leased `processing` state; completion and
failure writes are conditioned on lease ownership, so overlapping workers or
stale processors cannot double-process or clobber results. The
reconcile_forgejo_deliveries beat task (registered via CELERY_IMPORTS)
retries due rows with backoff and recovers expired leases after crashes.
Truncated pushes are never resolved on the request path — the row defers to
the reconciler so a slow Forgejo API cannot wedge the API worker.

HTTP contract:
  403  bad/absent signature, or no usable secret
  400  malformed delivery — bad UTF-8/JSON, non-object payload, missing or
       wrong-typed/required fields, missing X-Forgejo-Delivery, truncated
       push without canonical before/after/total; zero writes, nothing stored
  409  delivery id reused with different event/repo/body — fail closed,
       zero processing
  503  config defect or transient execution failure — the inbox row stays
       pending and internal retry recovers it
  202  accepted but not finished: truncated push deferred to the
       reconciler, or a duplicate arrived while the original is in flight
  200  processed (the result carries what was RECOGNISED and what was
       refused, and the moved list is always empty), inert unmapped repo,
       or idempotent duplicate
"""

import hashlib
import hmac
import json
import logging
import re
import time as time_mod
from uuid import UUID, uuid4

import requests as http_requests

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from plane.bridge import delivery_result, forges, reply
from plane.bridge import inbox, instance_config
from plane.bridge import semantic_key as skey
from plane.bridge import grammar
from plane.bridge import write_boundary
from plane.bridge.grammar import parse_directives
from plane.db.models import ForgejoDelivery, Issue, Project

logger = logging.getLogger("plane.worker")

MIN_SECRET_LENGTH = 16
FETCH_PAGE_SIZE = 50
FETCH_MAX_PAGES = 40  # 2000-commit guard for pathological pushes
FETCH_PAGE_TIMEOUT_SECONDS = 10
FETCH_TOTAL_DEADLINE_SECONDS = 60
RETRY_BACKOFF_CAP_SECONDS = 3600
LEASE_SECONDS = 120

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DELIVERY_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# Explicit reference grammar (RC 3066): the id shape alone is ambiguous with
# ordinary technical vocabulary (SHA-256, UTF-8, AES-256), so a ticket is only
# SELECTED behind a reference keyword. Nothing moves — selection is the whole
# of what a keyword buys (BIP-67).
#
# ONE DATUM OWNS BOTH WHICH KEYWORDS MATCH AND WHAT EACH MEANS (Morrow RC 3546).
# The pattern's alternation is DERIVED from this map, so the two cannot drift:
# a keyword removed here stops matching as well as stops classifying. They were
# briefly two owners — a mutation deleting fix/fixes from the classifier left
# `fixes BIP-54` still matching and silently reclassified as advance, with every
# transition test green. That is the same two-owners defect this ticket exists
# to remove, one layer inside the fix for it.
#
# THE MATCHER AND THE KEYWORD→CLASS MAP LIVED HERE AND NOW LIVE IN `grammar`
# (BIP-54). This module had its own copy of both, beside grammar's own
# `_COMPLETE_KEYWORDS` and `_keyword_class` — two spellings of one rule in two
# files, with the runtime using this one. Adding a keyword to the grammar would
# have changed nothing the bridge did, silently. Selection now has one owner and
# this module asks it: `grammar.forward_selection(text, source)`.

# TARGET RESOLUTION IS DELETED (BIP-67), not disabled. What stood here
# described the "smart" resolver: push to the review-ish started state, merge
# to the completed one, groups structural rather than by name. No state is
# selected on any event now, so nothing in a project's workflow — its state
# names, its groups, or their order — affects this module at all. It returns
# with the first authorised write, specified alongside the caller that
# performs it rather than inherited from here.



class _MalformedDeliveryError(Exception):
    """Wrong bytes or wrong shape from the sender: controlled 400, zero writes."""


class _TransientBridgeError(Exception):
    """Execution failure; the inbox row stays pending for internal retry."""


class _LeaseLostError(Exception):
    """This processor's lease was reclaimed: abort with ZERO further writes —
    the new owner's outcome is authoritative."""


class _ConfigDefectError(Exception):
    """Broken bridge/workflow configuration. Also leaves the row pending: the
    moment an operator fixes the config, the reconciler lands the delivery."""

def _authenticate(request):
    """Identify the sending forge and verify its credential.

    biplane (BIP-15): returns the forge personality on success, or None. The
    Forgejo/Gitea path is unchanged — same headers, same HMAC, same secret —
    so a deployed bridge behaves identically; the other personalities are new
    doors that were previously closed to everyone.

    Returning the forge rather than a bool matters: the caller needs it to read
    the event and delivery-id headers, and reading those with Forgejo's names
    while authenticating with someone else's would be the mismatch class this
    module exists to prevent.

    ONE CREDENTIAL PER FORGE (Morrow 10146). A single shared secret would be a
    key-reuse vulnerability, not a convenience: GitLab sends its credential
    back VERBATIM as a bearer token, while Forgejo/GitHub use theirs as an
    HMAC key. Shared, the echoed GitLab token IS the HMAC key — anyone who
    observes it can sign arbitrary bodies for the supposedly body-bound doors,
    so the opt-in's weaker guarantee would silently infect the stronger ones.
    Each personality therefore reads its own setting, fails closed when it is
    unset, and GitLab is refused outright if its token EQUALS an HMAC secret —
    separation that exists only in variable names contains nothing.
    """
    forge = forges.detect(request)
    if forge is None:
        return None  # no recognised credential header at all

    secret = getattr(settings, forge.secret_setting, None)
    if not secret:
        return None  # fail closed: this forge's door is not configured
    if len(secret) < MIN_SECRET_LENGTH:
        logger.error(
            f"git-bridge: {forge.secret_setting} is shorter than {MIN_SECRET_LENGTH} chars; "
            f"refusing all {forge.name} deliveries"
        )
        return None

    # A forge whose signature does not cover the body gives a weaker guarantee
    # than the one this bridge was built on: it proves the sender knew the
    # secret, and nothing about the bytes that arrived. The delivery inbox keys
    # on a body digest and the whole design assumes tampering is detectable, so
    # accepting such a forge is a deliberate operator decision, not a default.
    if not forge.body_bound:
        if not getattr(settings, "BRIDGE_ALLOW_UNSIGNED_BODY_FORGES", False):
            logger.error(
                f"git-bridge: refusing {forge.name} delivery — its signature does not cover the "
                "request body. Set BRIDGE_ALLOW_UNSIGNED_BODY_FORGES=1 to accept this weaker "
                "guarantee deliberately."
            )
            return None
        # The echoed token must never equal an HMAC secret, or observing it
        # hands out signing power for the body-bound personalities.
        for other in forges.FORGES:
            if other.body_bound and other.secret_setting:
                other_secret = getattr(settings, other.secret_setting, None)
                if other_secret and forges._constant_time_equals(secret, other_secret):
                    logger.error(
                        f"git-bridge: {forge.secret_setting} equals {other.secret_setting}; "
                        f"the echoed {forge.name} token would double as {other.name}'s HMAC "
                        f"key. Refusing {forge.name} deliveries until the credentials differ."
                    )
                    return None

    if not forge.verify(request, secret):
        return None
    return forge


def _body_digest(raw_body: bytes, forge=forges.ForgejoForge) -> str:
    secret = getattr(settings, forge.secret_setting, "") or ""
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def _parse_payload(raw_body: bytes) -> dict:
    """Bytes -> validated JSON object, or _MalformedDeliveryError."""
    try:
        text = (raw_body or b"{}").decode("utf-8")
    except UnicodeDecodeError:
        raise _MalformedDeliveryError("body is not valid UTF-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise _MalformedDeliveryError("body is not valid JSON")
    if not isinstance(payload, dict):
        raise _MalformedDeliveryError("payload must be an object")
    return payload


def _is_strict_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_shape(event: str, payload: dict, forge=forges.ForgejoForge) -> None:
    """Typed boundary (RC 3068/3069/3070): event-specific REQUIRED fields,
    exact types. A signed delivery that fails this owes a controlled 400 with
    zero writes — required-and-absent is as malformed as wrong-typed.

    Field LOCATIONS come from the forge (RC 3170 follow-through): the same
    strictness is applied to each forge's own shape, in its own field names.
    Normalising first and validating the result would launder malformed into
    empty, so validation reads the raw payload."""
    parent = payload.get(forge.repo_path[0])
    if not isinstance(parent, dict):
        raise _MalformedDeliveryError(f"{forge.repo_path[0]} (object) is required")
    repo_field = ".".join(forge.repo_path)
    full_name = forge.repo_full_name(payload)
    if not isinstance(full_name, str) or not full_name:
        raise _MalformedDeliveryError(f"{repo_field} (non-empty string) is required")
    if event == "push":
        commits = payload.get("commits")
        if not isinstance(commits, list):
            raise _MalformedDeliveryError("commits (list) is required for push")
        for commit in commits:
            if not isinstance(commit, dict):
                raise _MalformedDeliveryError("each commit must be an object")
            if not isinstance(commit.get("message"), str):
                raise _MalformedDeliveryError("commit.message (string) is required")
            commit_id = commit.get("id")
            if commit_id is not None and not isinstance(commit_id, str):
                raise _MalformedDeliveryError("commit.id must be a string")
        for key in ("ref", "before", "after"):
            value = payload.get(key)
            if value is not None and not isinstance(value, str):
                raise _MalformedDeliveryError(f"{key} must be a string")
        if forge.total_field is not None:
            total = payload.get(forge.total_field)
            if total is not None and not _is_strict_int(total):
                raise _MalformedDeliveryError(f"{forge.total_field} must be an integer")
            if _is_strict_int(total) and total > len(commits):
                # truncated: range resolution needs canonical anchors (RC 3070)
                if total <= 0:
                    raise _MalformedDeliveryError(f"{forge.total_field} must be positive")
                for key in ("before", "after"):
                    value = payload.get(key)
                    if not isinstance(value, str) or not SHA_RE.match(value):
                        raise _MalformedDeliveryError(f"truncated push requires canonical 40-hex {key}")
    elif event == "pull_request":
        try:
            forge.validate_pull_request(payload)
        except ValueError as e:
            raise _MalformedDeliveryError(str(e))
    elif event == "review_rejected":
        stable_repo_id = forge.repo_stable_id(payload)
        if stable_repo_id is None or stable_repo_id <= 0:
            raise _MalformedDeliveryError("repository.id must be a positive integer")
        pull_request = payload.get("pull_request")
        if not isinstance(pull_request, dict):
            raise _MalformedDeliveryError("pull_request (object) is required")
        number = pull_request.get("number")
        if not _is_strict_int(number) or number <= 0:
            raise _MalformedDeliveryError("pull_request.number must be a positive integer")
        # THE SELECTION FIELD IS VALIDATED AT INGRESS (Morrow). The body is what
        # nominates a ticket on this path, so a payload without it is malformed
        # — a 400 with NO stored row. Rejecting it later in the helper is not
        # equivalent: the row is already durable by then, the raise is wrapped
        # as transient, and the delivery RETRIES FOREVER instead of failing the
        # HTTP contract. An explicit null normalizes to empty, because a pull
        # request opened with no description is a genuine empty selection.
        if "body" not in pull_request:
            raise _MalformedDeliveryError("pull_request.body is required")
        if pull_request["body"] is not None and not isinstance(pull_request["body"], str):
            raise _MalformedDeliveryError("pull_request.body must be a string or null")
        review = payload.get("review")
        if not isinstance(review, dict):
            raise _MalformedDeliveryError("review (object) is required")
        review_id = review.get("id")
        if not _is_strict_int(review_id) or review_id <= 0:
            raise _MalformedDeliveryError("review.id must be a positive integer")


def _is_truncated(event: str, payload: dict, forge=forges.ForgejoForge) -> bool:
    """A push that may have lost commits: a declared total above the delivered
    count, OR — for a forge that declares no total (GitHub, Morrow 10147) — a
    commits array AT the forge's documented cap. A delivery of exactly
    cap-many commits cannot be distinguished from a capped larger push, so it
    is treated as possibly truncated: over-reporting is the safe direction,
    silent ref loss is not."""
    if event != "push":
        return False
    commits = forge.commits(payload)
    total = forge.declared_commit_total(payload)
    if _is_strict_int(total) and total > len(commits):
        return True
    return forge.commit_cap is not None and len(commits) >= forge.commit_cap


def _api_creds_present() -> bool:
    return bool(getattr(settings, "FORGEJO_BASE_URL", None)) and bool(
        getattr(settings, "FORGEJO_BRIDGE_API_TOKEN", None)
    )

def _review_pull_number(payload: dict) -> int:
    """The one identity field the review path still consumes. The stable repo
    id fed the deleted authority cross-check and the review id fed its API
    read; an accessor returning ids nobody uses is the residue of that owner
    (Morrow). Shape validation guarantees presence and type before storage.
    """
    return payload["pull_request"]["number"]


def _review_body_from_payload(payload) -> str:
    """The pull-request body from the SIGNED event, for SELECTION only.

    Author-controlled, and that is acceptable now: under the ruling a body
    selects a ticket and never authorises anything. The bridge cannot move a
    row from this, so a stale or edited body can at worst make the bridge ask
    about the wrong ticket — a mistake, which is the case the ruling says to
    ask about rather than guard against.

    ABSENT IS NOT EMPTY (Morrow cold read; the class BIP-46 closed). An earlier
    version returned "" for a missing or malformed body, MANUFACTURING "this
    event named no ticket" out of a field nobody had read. So it fails closed
    and is retried as malformed rather than silently answered.
    """
    pull = (payload or {}).get("pull_request") or {}
    # Presence and type are guaranteed by _validate_shape at INGRESS, which is
    # where a malformed payload must be refused: a 400 with no stored row,
    # rather than a durable row that retries forever.
    body = pull.get("body")
    if body is None:
        # A pull request opened with no description. A GENUINE empty selection,
        # distinct from the field's absence.
        return ""
    if not isinstance(body, str):
        raise _MalformedDeliveryError(
            f"review payload pull_request.body is {type(body).__name__}, not a string"
        )
    return body

def _project_scope(payload: dict, repo: str, forge=forges.ForgejoForge):
    """Map a repository to the PROJECTS whose work items a directive in it may
    NAME (BIP-38 scope guard, docs/scope-a-architecture.md §M2). Returns
    {identifier: Project} for the mapped scope, or None for an unmapped repo.

    Not "may move items in": nothing moves. The guard is on SELECTION and keeps
    its full force there — an out-of-scope ref is rejected before it is ever
    looked up.

    Resolution NEVER leaves the mapped scope: a ref whose identifier is not in
    the returned dict is rejected without ever querying other projects or
    workspaces — out-of-scope tickets are not looked up, existence included.

    Unset/empty configuration while the secret is set is a config DEFECT (the
    operator clearly meant the bridge to be on); an explicit "{}" is a valid
    map that scopes nothing. Unmapped repo -> None -> inert delivery.

    Values are non-empty lists of stable project UUIDs. A legacy
    workspace-slug value is a CONFIG DEFECT, not a fallback: the workspace-wide
    grammar is the live cross-project mover this guard exists to close, so
    honoring it "for compatibility" would keep the defect while claiming the
    fix. Two scoped projects sharing an identifier would make ticket keys
    ambiguous — config defect, refused at resolution time.

    THE AUTHORITATIVE KEY IS "<provider instance id>:<stable repository id>"
    — M2's authority tuple, verbatim (Morrow ruling on the #76
    instance-keying finding). The prefix is the CONFIGURED instance id
    (FORGEJO_INSTANCE_ID etc.), the same namespace semantic event keys use —
    one notion of provider identity in this module, not two. A display path
    is mutable — a rename followed by path reuse would hand the scope to a
    DIFFERENT repository — and a family-name prefix ("forgejo") is ambiguous
    the moment a second same-family instance is bridged: repo ids are
    per-instance sequences, so the same number on two servers is likely, and
    a family key would grant one instance the other's projects. A family
    prefix that differs from the configured id grants NOTHING, loudly (the
    wrong-prefix diagnostic below): silent-inert is the failure mode here —
    the wrong prefix does not error, it 200s forever and moves nothing.

    A bare "org/repo" path key is preserved ONLY as a legacy migration path,
    FORGEJO ONLY, and every use logs a loud warning naming the id-keyed entry
    to migrate to — it is not the safe boundary and never grants any other
    provider authority. A delivery whose payload carries no usable stable id
    can match no id-keyed entry: fail closed to inert (or the warned Forgejo
    legacy key), never to a path-based guess."""
    raw = getattr(settings, "FORGEJO_BRIDGE_REPO_MAP", None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        logger.error("git-bridge: FORGEJO_BRIDGE_REPO_MAP is not configured")
        raise _ConfigDefectError("FORGEJO_BRIDGE_REPO_MAP is not configured")
    try:
        mapping = raw if isinstance(raw, dict) else json.loads(raw)
    except (TypeError, ValueError):
        logger.error("git-bridge: FORGEJO_BRIDGE_REPO_MAP is not valid JSON")
        raise _ConfigDefectError("FORGEJO_BRIDGE_REPO_MAP is not valid JSON")
    if not isinstance(mapping, dict):
        logger.error("git-bridge: FORGEJO_BRIDGE_REPO_MAP is not a JSON object")
        raise _ConfigDefectError("FORGEJO_BRIDGE_REPO_MAP is not a JSON object")
    scope = None
    stable_id = forge.repo_stable_id(payload)
    # ONE notion of provider identity (Morrow ruling on Vex's #76 finding; M2's
    # authority tuple is (provider INSTANCE, stable repo id)): the key prefix
    # is the configured instance id — the same namespace semantic keys use —
    # consumed from the single owner, never restated here. A family-name
    # prefix ("forgejo") grants nothing when it differs from the configured
    # id: repo ids are per-instance sequences, so two same-family instances
    # can allocate the same number, and a family key would hand one instance
    # the other's project scope.
    instance = _provider_instance(forge)
    if stable_id is not None:
        scope = mapping.get(f"{instance}:{stable_id}")
        if scope is None:
            # A key for this repo id under ANOTHER prefix is far more likely a
            # config error than a genuinely unmapped repo — and the failure is
            # otherwise SILENT (unmapped = legitimate inert no-op, 200s
            # forever, moves nothing). Loud, both directions: stale
            # family-keyed entries after this migration, and instance-keyed
            # entries under a renamed instance (Vex, #76 finding).
            #
            # EXCEPT other providers' own keys (Morrow RC 3600): repo ids are
            # per-instance sequences, so a VALID gitlab-prod:42 coexisting
            # with an unmapped Forgejo repo 42 is normal — telling the
            # operator to migrate a correct key is the same class of defect
            # this diagnostic exists to catch. Prefixes belonging to another
            # configured forge personality are excluded, consumed from the
            # same single owner; a personality whose instance id is unset or
            # malformed contributes nothing here — the static check owns that
            # defect, and this is a logging diagnostic, not a gate.
            other_instances = set()
            for other in forges.FORGES:
                if other is forge:
                    continue
                try:
                    other_instances.add(instance_config.resolve(other))
                except instance_config.InstanceConfigError:
                    continue
            wrong_prefix = sorted(
                k for k in mapping
                if k.rsplit(":", 1)[-1] == str(stable_id)
                and k != f"{instance}:{stable_id}"
                and k.rsplit(":", 1)[0] not in other_instances
            )
            if wrong_prefix:
                logger.warning(
                    f"git-bridge: repo {repo} (stable id {stable_id}) is mapped under {wrong_prefix} "
                    f"but the configured provider instance is {instance!r}, so the lookup key is "
                    f"'{instance}:{stable_id}'. A family-name or other-instance prefix grants no "
                    f"scope; migrate the entry. Treating as unmapped (inert)."
                )
    if scope is None and forge is forges.ForgejoForge:
        scope = mapping.get(repo)
        if scope is not None:
            logger.warning(
                f"git-bridge: repo {repo} matched by LEGACY path key. Path keys are a "
                f"migration convenience, not the tenancy boundary — a rename plus path "
                f"reuse transfers this scope to a different repository. Migrate the "
                f"entry to '{instance}:{stable_id if stable_id is not None else '<repository id>'}'."
            )
    if scope is None:
        return None  # unmapped repo: a legitimate no-op, not a config defect
    if isinstance(scope, str):
        logger.error(
            f"git-bridge: FORGEJO_BRIDGE_REPO_MAP entry for {repo} is a workspace slug — the "
            f"pre-BIP-38 schema. Workspace-wide scope is the live cross-project mover the scope "
            f"guard closes; migrate the entry to an explicit list of project UUIDs."
        )
        raise _ConfigDefectError(f"repo map entry for {repo} uses the retired workspace-slug schema")
    if (
        not isinstance(scope, list)
        or not scope
        or not all(isinstance(p, str) and p.strip() for p in scope)
    ):
        logger.error(
            f"git-bridge: FORGEJO_BRIDGE_REPO_MAP entry for {repo} is not a non-empty list of project UUIDs"
        )
        raise _ConfigDefectError(f"repo map entry for {repo} is not a non-empty list of project UUIDs")
    try:
        uuids = {UUID(p) for p in scope}
    except ValueError:
        logger.error(f"git-bridge: FORGEJO_BRIDGE_REPO_MAP entry for {repo} contains a malformed project uuid")
        raise _ConfigDefectError(f"repo map entry for {repo} contains a malformed project uuid")
    projects = list(Project.objects.filter(id__in=uuids).select_related("workspace"))
    if len(projects) != len(uuids):
        missing = sorted(str(u) for u in uuids - {p.id for p in projects})
        logger.error(f"git-bridge: repo {repo} maps to project uuid(s) {missing} which do not exist")
        raise _ConfigDefectError(f"mapped project uuid(s) {missing} do not exist")
    by_identifier = {}
    for project in projects:
        clash = by_identifier.get(project.identifier)
        if clash is not None:
            logger.error(
                f"git-bridge: repo {repo} scopes projects {clash.id} and {project.id} which share "
                f"identifier {project.identifier!r}; ticket keys would be ambiguous"
            )
            raise _ConfigDefectError(
                f"repo map entry for {repo} scopes two projects sharing identifier {project.identifier!r}"
            )
        by_identifier[project.identifier] = project
    return by_identifier

def _directive_ticket(directive) -> tuple[str, int]:
    identifier, sequence_id = directive.ticket_key.rsplit("-", 1)
    return identifier, int(sequence_id)


def _apply_review_rejection(
    delivery,
    lease_token: str,
    scope: dict,
    repo: str,
    payload: dict,
) -> dict:
    """Record a durable refusal for a changes-requested review. It writes no state.

    THERE IS NO LONGER A REVIEW -> CODE & TDD EDGE (BIP-67), and no forge
    authority re-read either: selection comes from the SIGNED event's body,
    validated at ingress. ADR 009's automatic mutation is superseded outright,
    so this function no longer moves anything and the mutation code is absent
    rather than gated — a changes-requested
    review is neither a merge nor an approval, so no fact can authorise it.

    What survives is the durability: the delivery lease, the refusal record and
    the terminal inbox state share one transaction, so a crash cannot leave a
    processed holder whose retry overwrites the durable outcome. **The forge
    authority re-read is NOT among them** — it was deleted with the write it
    guarded (see the comment at the read site below), and listing it here as a
    participant in the transaction described a call this function no longer
    makes.
    """
    pull_number = _review_pull_number(payload)
    with transaction.atomic():
        owned = (
            ForgejoDelivery.objects.select_for_update()
            .filter(
                pk=delivery.pk,
                lease_token=lease_token,
                status="processing",
                lease_expires_at__gte=timezone.now(),
            )
            .first()
        )
        if owned is None:
            raise _LeaseLostError(f"lease on {delivery.delivery_id} was reclaimed")

        # THE ASK DOES NOT RE-READ AUTHORITY (Morrow cold read, BIP-67).
        #
        # This used to re-read pull-request authority from the forge first,
        # via a helper now deleted with the write it guarded. That read needs
        # FORGEJO_BRIDGE_API_TOKEN — empty on the live board — so every review
        # delivery stayed pending or 503 and NEITHER refused NOR asked. The one
        # case the ruling names as exactly when the bridge should speak was the
        # one case it could not.
        #
        # And the facts it fetched — official, current, open — were ADR 009
        # WRITE-permission checks. That write is gone. Under the ruling, text
        # SELECTS and never authorises, so the ask needs only what the signed
        # event already proves: a review happened, on this pull request, and it
        # named these tickets. Nothing here can move a board row, so nothing
        # here needs permission. The authoritative read belongs to whatever
        # eventually WRITES, and today that is nothing.
        parsed = parse_directives(_review_body_from_payload(payload), source="pr_body")

        tickets = sorted({_directive_ticket(directive) for directive in parsed.directives})
        # ZERO NOMINATION IS ALSO AN ASK-CASE (Morrow cold read). A
        # changes-requested review naming no ticket used to produce nothing at
        # all — no boundary call, no record, so neither half of the asking had
        # anything to say. Event-level because there is no ticket to key it by.
        no_ticket = (
            None
            if (tickets or parsed.near_misses)
            else write_boundary.describe_no_nomination(
                repo=repo, context=f"review on #{pull_number}"
            )
        )
        moved = []
        rejected = []
        unverified = []
        # A delivery whose every directive is rejected writes NOTHING (Morrow
        # RC 3588), same ownership boundary as the forward path. That guarantee
        # is now trivial rather than earned: NO delivery writes a board row, and
        # the lazily-materialised bridge User this used to be about was deleted
        # with the writes. Kept because the property still holds and is still
        # the thing to preserve if a write ever returns.
        for identifier, sequence_id in tickets:
            project = scope.get(identifier)
            if project is None:
                # Scope guard (BIP-38): the ref names a project outside this
                # repository's mapped scope — rejected, durably recorded, and
                # NEVER looked up (existence included; see _project_scope).
                logger.warning(
                    f"git-bridge: REJECTED cross-project ref {identifier}-{sequence_id} "
                    f"from review delivery in {repo}: not in the repository's mapped scope"
                )
                rejected.append(
                    delivery_result.cross_project_entry(
                        f"{identifier}-{sequence_id}", repo, "project not in this repository's mapped scope"
                    )
                )
                continue
            # PLAIN lookup, not select_for_update (Morrow's race ruling): with no
            # board-state read or mutation left, an Issue row lock has no
            # correctness job — it only serialized harmless refusals. The
            # ForgejoDelivery lease-row lock still owns the durable result
            # writes. If a board write is ever reintroduced, whoever adds it
            # owns re-deciding the locking too — that is a requirement on the
            # future slice, not a lock waiting to come back.
            issue = Issue.objects.filter(project=project, sequence_id=sequence_id).first()
            if issue is None:
                # Same named ask-case on the review path (Morrow cold read):
                # in-scope project, no such ticket. Recorded rather than
                # dropped, so the reply can tell whoever typed it.
                unverified.append(
                    delivery_result.unverified_entry(
                        write_boundary.decide_missing_ticket(
                            ticket=f"{identifier}-{sequence_id}",
                            repo=repo,
                            context=f"review on #{pull_number}",
                        )
                    )
                )
                continue
            # BOARD STATE MUST NOT GATE THE ASK (Morrow cold read, BIP-67).
            # This used to require state == "Review" before going further —
            # superseded ADR 009 machinery. Under it, a changes-requested
            # review naming a ticket in any other state recorded NO refusal
            # and nudged NOBODY: a silent drop, in the one case the ruling
            # names as exactly when the bridge should ask. The ticket exists
            # and is in scope, so it gets an answer whatever state it is in.
            # THE WRITE BOUNDARY (BIP-67). ADR 009's automatic Review -> Code &
            # TDD write is SUPERSEDED OUTRIGHT, not gated: a changes-requested
            # review is neither a merge nor an approval, so no waiting makes it
            # qualify. The ruling names this as exactly the case where the
            # bridge asks instead of acting.
            # THERE IS NO REWORK WRITE. Not gated — ABSENT (Morrow cold read,
            # BIP-67). ADR 009's automatic Review -> Code & TDD mutation is
            # superseded OUTRIGHT: a changes-requested review is neither a
            # merge nor an approval, so no fact can ever authorise it. Leaving
            # the mutation behind `if refusal` would be a mode flag in
            # return-value form — one edit making decide_rework return None
            # would silently reactivate a board write the ruling removed. A
            # function that must not write is a function WITHOUT the write.
            refusal = write_boundary.decide_rework(
                ticket=f"{identifier}-{sequence_id}", repo=repo, pull_number=pull_number
            )
            if refusal is not None:
                unverified.append(delivery_result.unverified_entry(refusal))
                logger.info(
                    f"git-bridge: {refusal.ticket} NOT moved ({refusal.reason}) — {refusal.detail}"
                )
            # UNCONDITIONAL: there is no rework write to fall through to.
            continue

        result = delivery_result.build(
            moved,
            near_misses=[near.text for near in parsed.near_misses],
            cross_project=rejected,
            unverified=unverified,
            no_ticket=no_ticket,
            conflicts=list(parsed.conflicts),
        )
        _complete_review_delivery(owned, result)
        return result


def _complete_review_delivery(owned, result):
    """Terminalize the locked review holder atomically with its refusal record."""
    owned.status = "processed"
    owned.attempts += 1
    owned.processed_at = timezone.now()
    owned.last_error = None
    owned.lease_token = None
    owned.lease_expires_at = None
    owned.result = result
    owned.save(
        update_fields=[
            "status",
            "attempts",
            "processed_at",
            "last_error",
            "lease_token",
            "lease_expires_at",
            "result",
            "updated_at",
        ]
    )


def _fetch_full_push_commits(repo: str, before: str, after: str, total: int):
    """Resolve a truncated push's FULL commit range from the Forgejo API,
    failing CLOSED on any incompleteness (RC 3070): duplicate/non-progressing
    pages, malformed responses, exhausted pages, or a range that never proves
    complete all classify for retry — a partial range is never processed.

    Completeness: the walk reaches `before` (the pre-push boundary), or it
    collects exactly `total` unique commits (branch-create pushes have
    before=0{40}, which never appears in history). Runs ONLY in the
    reconciler (deferred), never on the API request path."""
    if not _api_creds_present():
        raise _ConfigDefectError(
            f"push has {total} commits but the webhook payload was truncated; "
            "FORGEJO_BASE_URL and FORGEJO_BRIDGE_API_TOKEN are required to resolve the full range"
        )
    base = settings.FORGEJO_BASE_URL
    token = settings.FORGEJO_BRIDGE_API_TOKEN
    deadline = time_mod.monotonic() + FETCH_TOTAL_DEADLINE_SECONDS
    seen: dict = {}  # sha -> message, insertion-ordered
    prev_first_sha = None
    page = 1
    while page <= FETCH_MAX_PAGES:
        if time_mod.monotonic() > deadline:
            raise _TransientBridgeError("push-range resolution exceeded its time budget")
        try:
            response = http_requests.get(
                f"{base.rstrip('/')}/api/v1/repos/{repo}/commits",
                params={"sha": after, "limit": FETCH_PAGE_SIZE, "page": page, "stat": "false"},
                headers={"Authorization": f"token {token}"},
                timeout=FETCH_PAGE_TIMEOUT_SECONDS,
            )
        except http_requests.RequestException as e:
            raise _TransientBridgeError(f"Forgejo API unreachable while resolving push range: {e}") from e
        if response.status_code != 200:
            raise _TransientBridgeError(f"Forgejo API returned {response.status_code} while resolving push range")
        try:
            batch = response.json()
        except ValueError as e:
            raise _TransientBridgeError("Forgejo API returned malformed JSON for push range") from e
        if not isinstance(batch, list):
            raise _TransientBridgeError("Forgejo API returned a non-list for push range")
        if not batch:
            raise _TransientBridgeError(
                f"push range incomplete: API history ended with {len(seen)}/{total} unique commits"
            )
        first = batch[0]
        first_sha = first.get("sha") if isinstance(first, dict) else None
        if first_sha is not None and first_sha == prev_first_sha:
            raise _TransientBridgeError("push range resolution is not progressing (repeated page)")
        prev_first_sha = first_sha
        new_unique = 0
        for entry in batch:
            if not isinstance(entry, dict):
                raise _TransientBridgeError("malformed commit entry in push range response")
            sha = entry.get("sha")
            message = (entry.get("commit") or {}).get("message") if isinstance(entry.get("commit"), dict) else None
            if not isinstance(sha, str) or not SHA_RE.match(sha) or not isinstance(message, str):
                raise _TransientBridgeError("malformed commit entry in push range response")
            if sha == before:
                return [{"id": s, "message": m} for s, m in seen.items()]
            if sha not in seen:
                seen[sha] = message
                new_unique += 1
            if len(seen) >= total:
                return [{"id": s, "message": m} for s, m in seen.items()]
        if new_unique == 0:
            raise _TransientBridgeError("push range resolution is not progressing (no new commits)")
        page += 1
    raise _TransientBridgeError(
        f"push range incomplete after {FETCH_MAX_PAGES} pages ({len(seen)}/{total} unique commits)"
    )


#: The CLOSED set of events this bridge handles. Named rather than repeated as
#: a literal, because two copies of a closed set are one edit from disagreeing —
#: and the disagreement would be silent: an event accepted at the endpoint but
#: unhandled by the collector yields no refs, no near misses and no diagnostic,
#: which is indistinguishable from an event that legitimately named nothing.
HANDLED_EVENTS = ("push", "pull_request", "review_rejected")


def _collect_refs(event: str, payload: dict, repo: str, forge=forges.ForgejoForge, near_misses=None, conflicts=None):
    """Yield (ticket_ref, context) pairs the event legitimately asserts.

    ``near_misses``, when a caller passes a LIST, is filled with disqualified
    directive lines — text that tried the trailer form and failed it. THE PARSE
    PRODUCES MORE THAN TICKETS, AND THE COLLECTOR MUST CARRY ALL OF IT (Morrow,
    candidate blocker; Sable's diagnosis): dropping them here meant a merged PR
    whose whole body was `Closes BIP-7 after QA` was answered "no ticket was
    named" — confidently false — and a push near miss vanished from the durable
    record entirely, against Scope A 108-109. One grammar pass produces both
    outcomes; callers must not run a second, divergent pass of their own.

    Shapes are guaranteed by _validate_shape() before storage. Reads go
    through the forge's accessors (RC 3170 follow-through) so a GitLab
    payload is read in GitLab's field names, not silently found empty."""
    if event == "push":
        commits = forge.commits(payload)
        total = forge.declared_commit_total(payload)
        if _is_truncated(event, payload, forge):
            if not forge.resolves_push_ranges:
                # Fail LOUDLY, never partially: processing only the delivered
                # commits would silently drop references (RC 3070's rule). The
                # row stays pending with this error until the push is
                # redelivered whole (or split below the forge's limit).
                declared = (
                    f"{len(commits)}/{total} commits delivered"
                    if _is_strict_int(total)
                    else f"{len(commits)} commits delivered, at {forge.name}'s cap of "
                    f"{forge.commit_cap} — the payload declares no total, so the rest "
                    "may be silently missing"
                )
                raise _TransientBridgeError(
                    f"possibly truncated {forge.name} push ({declared}); range resolution "
                    "is only implemented for the Forgejo API — split the push below the "
                    "forge's webhook commit limit or redeliver untruncated"
                )
            commits = _fetch_full_push_commits(repo, payload.get("before") or "", payload.get("after") or "", total)
        # Event-wide accumulators: a push is ONE event, so its reduction is over
        # every commit in it, not over each commit separately.
        event_classes: dict = {}
        event_context: dict = {}
        # A dict as an ORDERED SET. Conflicts are event-level facts, so a ticket
        # that conflicts within one commit AND again across commits is ONE fact,
        # not two (Morrow). Extending the caller's list per commit duplicated it
        # and delivery_result's dedupe hid that — the same masking boundary we
        # just removed for candidates, in the field beside it. I fixed one and
        # left the other.
        event_conflicts: dict = {}
        for commit in commits:
            message = commit.get("message") or ""
            sha = (commit.get("id") or "?")[:7]
            # A push PROPOSES advance whatever the keyword says: the branch may
            # never merge, so a commit message cannot assert completion. The
            # class is a proposal only — the boundary refuses it, so nothing
            # advances (BIP-67). Historically this mirrored a real target, the
            # started group for any push; that target is deleted, and what
            # survives is the class, which still decides what the event would
            # have asked for and therefore what gets recorded.
            # Visible prose only (BIP-54 slice 2): a directive quoted inside a
            # code span or fence is prose ABOUT a directive, not one. A commit
            # message is ONE field, so it is one call.
            nominated, commit_near, commit_conf = grammar.forward_selection(message, source="commit_message")
            if near_misses is not None:
                near_misses.extend(commit_near)
            for key in commit_conf:
                event_conflicts[key] = True
            for identifier, sequence, proposed_class in nominated:
                # ACCUMULATE ACROSS THE WHOLE PUSH, DO NOT YIELD YET (Morrow).
                # Reduction used to happen per COMMIT, so `Closes BIP-N` in one
                # commit and `Refs BIP-N` in another never met: no conflict was
                # recorded, and the ticket was yielded TWICE. Deduping later in
                # delivery_result hid the second yield — harmless while nothing
                # writes, two write attempts the day completion returns.
                key = (identifier, sequence)
                if key not in event_classes:
                    event_classes[key] = proposed_class
                    event_context[key] = f"commit {sha}"
                elif event_classes[key] != proposed_class:
                    # Weaker-class-wins, at EVENT level, and the demotion is a
                    # DATUM rather than just an outcome (Scope A 110-112).
                    event_classes[key] = grammar.ADVANCE
                    event_conflicts[f"{identifier}-{sequence}"] = True
        if conflicts is not None:
            conflicts.extend(event_conflicts)
        for (identifier, sequence), _class in event_classes.items():
            # EXACTLY ONE candidate per ticket per event. The push downgrade
            # applies after reduction, not before it — reducing on classes that
            # have already been flattened to ADVANCE would find nothing to
            # compare, and would look like it worked.
            yield (identifier, sequence), event_context[(identifier, sequence)], grammar.ADVANCE
    elif event == "pull_request":
        merged, fields, number = forge.merged_pull_request(payload)
        if merged:
            # Scope A's source list is closed: a pull-request body may nominate
            # a ticket and its title is inert. Keep the adapter's exact fields
            # separate and admit only the body; do not preserve a title parser
            # beside the normative grammar.
            _title, body = fields
            nominated, body_near, body_conf = grammar.forward_selection(body, source="pr_body")
            if near_misses is not None:
                near_misses.extend(body_near)
            if conflicts is not None:
                conflicts.extend(body_conf)
            for identifier, sequence, klass in nominated:
                yield (identifier, sequence), f"merged PR #{number}", klass
    else:
        # MANDATORY CLOSED EVENT, exhaustive BY CONSTRUCTION. This collector
        # handles exactly push and pull_request; review_rejected has its own
        # path. My first attempt guarded on HANDLED_EVENTS membership — the
        # ENDPOINT's set — so review_rejected fell through here silently while
        # the unhandled-event test passed on a different event entirely.
        # Falling through yields zero refs, zero near misses and zero
        # diagnostics: the same observable as an event that named nothing.
        raise _MalformedDeliveryError(
            f"event {event!r} reached the forward collector, which handles only "
            "push and pull_request; an unbranched event here is silent by "
            "construction"
        )

def _advance(
    delivery,
    lease_token: str,
    project,
    event,
    sequence_id: int,
    klass: str,
    context: str,
    repo: str,
) -> bool:
    # The caller resolved `project` from the repo's mapped scope (BIP-38):
    # only in-scope projects reach this function, so no lookup — and no
    # tenancy walk — happens here.
    # TARGET RESOLUTION IS DELETED (BIP-67) — not moved below the boundary, as
    # this comment used to say. It once ran here, so a project missing the
    # started/completed target raised a config defect and RETRIED FOREVER
    # instead of durably refusing and asking. While every board write is off,
    # target configuration must be
    # IRRELEVANT: nothing is going to be written to it either way.
    with transaction.atomic():
        # Lease ownership is validated UNDER A DB LOCK (RC 3071): a processor
        # whose lease was reclaimed aborts HERE, before any side effect, and
        # the abort rolls this transaction back entirely. What shares the
        # transaction is the lease and the durable REFUSAL RECORD — there is no
        # issue mutation to share it with, and saying otherwise described the
        # write this function was built to stop making.
        owned = (
            ForgejoDelivery.objects.select_for_update()
            .filter(
                pk=delivery.pk,
                lease_token=lease_token,
                status="processing",
                lease_expires_at__gte=timezone.now(),
            )
            .first()
        )
        if owned is None:
            raise _LeaseLostError(f"lease on {delivery.delivery_id} was reclaimed")
        # PLAIN EXISTENCE LOOKUP, NO ROW LOCK (Morrow). select_for_update had a
        # correctness job when this function READ board state and then WROTE it
        # under the same lock. It reads nothing and writes nothing now, so the
        # lock defends an invariant that no longer exists and only serialises
        # refusals against each other. The DELIVERY lease-row lock stays — that
        # one still owns the durable result. If a board write is ever
        # reintroduced, whoever adds it owns re-deciding the locking with it —
        # a requirement on that future slice, not a lock waiting to return.
        issue = Issue.objects.filter(project=project, sequence_id=sequence_id).first()
        if issue is None:
            # A MISSING TICKET IS A REFUSAL, NOT A SILENT RETURN (Scope A: "a
            # missing or unmatched ticket" is a named ask-case; Morrow cold
            # read). This used to return before the boundary was consulted, so
            # a directive naming a typo'd number vanished with no record, no
            # reply and no nudge.
            refusal = write_boundary.decide_missing_ticket(
                ticket=f"{project.identifier}-{sequence_id}", repo=repo, context=context
            )
            owned.result = delivery_result.merge_ignored(
                owned.result, unverified=[delivery_result.unverified_entry(refusal)]
            )
            owned.save(update_fields=["result", "updated_at"])
            logger.info(f"git-bridge: {refusal.ticket} NOT moved ({refusal.reason}) — {refusal.detail}")
            return False
        # THE WRITE BOUNDARY (BIP-67). Scope A, "Who may move a ticket": the
        # bridge writes only where facts it verified determine the outcome, and
        # EVERY BOARD/STATE write ships off in this release. The refusal is
        # recorded on the delivery rather than merely
        # logged — it is what the asking is built from, and under the ruling the
        # asking is the more valuable half of the service.
        #
        # Deliberately NOT a flag: each refusal derives from a named missing
        # fact or a withdrawn rule rather than a setting, so there is no switch
        # anyone can flip to make the bridge write. See bridge/write_boundary.py.
        #
        # THAT IS NOT THE SAME AS A REFUSAL EXPIRING WHEN ITS FACT ARRIVES, and
        # an earlier version of this comment taught exactly that. Two of the
        # three are PERMANENT: ADVANCE_NOT_AUTHORISED and REWORK_SUPERSEDED are
        # withdrawn rules, not pending facts, and no fact will ever make them
        # unreachable. The third, BINDING_UNAVAILABLE, is returned
        # UNCONDITIONALLY — a field appearing in the schema changes nothing on
        # its own, because the boundary is never told which pull request this
        # is. Enabling completion is a future CODE change carrying the fields,
        # the forge reads, the forge-account-to-participant mapping, the
        # decision and the write TOGETHER.
        #
        # THE FORWARD-ONLY ORDINAL USED TO LIVE HERE AND IS GONE. John's ruling:
        # there is no forward-only rule on the board, and the bridge keeps no
        # board-state rule of its own. It was doing TWO jobs and only one was
        # ever sanctioned — Scope A's single use of "forward-only" is the
        # BACKLINK IDEMPOTENCY bullet, not a safety rule.
        #
        # WHAT ITS REMOVAL OWES, before the first write is allowed: the
        # same-event replay half is already discharged above this layer by
        # ADR 010's holder/alias lifecycle, which survives a replay where a
        # delivery UUID does not. The remaining half is two DISTINCT events
        # targeting one transition — a second pull request also naming the
        # ticket — which needs an EQUALITY check ("already in the requested
        # target state"), not an ordinal. That is a legitimate no-op rather
        # than a safety case, which is exactly why fusing it to the safety job
        # was the original error. Nothing needs it while every write is refused.
        ticket_key = f"{project.identifier}-{sequence_id}"
        # ADVANCE NEVER REACHES MUTATION CODE (Morrow cold read). A push is
        # permanently unauthorised — not pending a fact — so its refusal is
        # STRUCTURAL: it returns here, above everything that writes. A mutant
        # making decide_advance return None cannot produce a board write,
        # because there is no write below this branch to reach.
        if klass != grammar.COMPLETE:
            refusal = write_boundary.decide_advance(
                ticket=ticket_key, context=context, repo=repo,
                event="merged_pr" if event == "pull_request" else "push",
            )
            if refusal is not None:
                owned.result = delivery_result.merge_ignored(
                    owned.result, unverified=[delivery_result.unverified_entry(refusal)]
                )
                owned.save(update_fields=["result", "updated_at"])
                logger.info(
                    f"git-bridge: {ticket_key} NOT moved ({refusal.reason}) — {refusal.detail}"
                )
            # UNCONDITIONAL. A mutant making decide_advance return None loses
            # the RECORD but still cannot produce a WRITE — which is the
            # property Morrow asked to be pinned.
            return False
        # Only COMPLETE reaches anything below, and — read the next comment —
        # there is NO write behind it either. This branch is not a gate holding
        # a mutation back; the mutation is deleted. An earlier version of this
        # comment called it "a real gate rather than an absence", which taught
        # the opposite of what the code does and contradicted the comment
        # sixteen lines below it.
        refusal = write_boundary.decide_completion(
            ticket=ticket_key, repo=repo, context=context
        )
        if refusal is not None:
            owned.result = delivery_result.merge_ignored(
                owned.result, unverified=[delivery_result.unverified_entry(refusal)]
            )
            owned.save(update_fields=["result", "updated_at"])
            logger.info(f"git-bridge: {ticket_key} NOT moved ({refusal.reason}) — {refusal.detail}")
            return False
        # THERE IS NO COMPLETION WRITE EITHER (Morrow cold read, BIP-67).
        # Removed rather than gated, for the same reason the rework mutation
        # was: decide_completion could return None the moment a future caller
        # supplies merger/approvals/binding, and the code below would then have
        # recorded the SYNTHETIC BRIDGE ACTOR as the author of the transition —
        # the merger fact and the acting identity were never connected. A
        # function that must not write is a function without the write, so the
        # write is absent until the verified merger identity IS the actor.
        #
        # What was here: target resolution, actor materialisation, the state
        # and completed_at update, the IssueActivity row, and the moved
        # bookkeeping. It comes back with the API slice that carries a real
        # merger, not before.
        return False
    # UNREACHABLE BY CONSTRUCTION, and that is the point: every path inside the
    # transaction returns False, because there is no longer any code here that
    # writes. `_advance` records refusals; it does not advance anything. The
    # name survives its behaviour for one release so the diff stays readable,
    # and goes when the completion path returns with a verified merger.
    raise AssertionError("_advance reached its end: a write path was reintroduced")


def _tell_whoever_acted(delivery, result, repo, payload):
    """Post this delivery's refusal on the pull request that caused it.

    TWO CALL SITES, BECAUSE `process_delivery` HAS TWO REPORTING EXITS out of
    THREE. (The third — the holder-collapse path, where a duplicate observation
    attaches to a holder that already spoke — returns deliberately WITHOUT
    reporting, and the comment there explains why adding a third call site is
    not a fix.) The review branch
    RETURNS EARLY, so the single call this replaces sat only on the push/merge
    path and a review delivery reached it never. That silently excluded the
    case John's ruling describes most directly — an agent requests changes, the
    bridge declines to move the ticket, and the pull request they are looking at
    says nothing — even though a review event carries a pull-request number and
    is the most certain of all our events that a person is PRESENT.

    Nothing about the delivery depends on this. It runs after the delivery is
    durably processed and outside the retry try/except: a reply that failed must
    never turn a processed delivery into a retry, because the retry would re-run
    the decision in order to produce a message. `refusal_comment` returns rather
    than raises on every failure path; the bare except is the second belt.
    """
    try:
        reply.refusal_comment(
            delivery_id=delivery.delivery_id,
            result=result,
            repo=repo,
            number=(payload.get("pull_request") or {}).get("number"),
            forge=getattr(delivery, "forge", None),
        )
    except Exception:
        logger.exception("git-bridge: reply failed; the delivery outcome is unaffected")


def claim_delivery(delivery) -> str:
    """Atomically claim a due row into a leased `processing` state.

    Claimable: pending, or processing with an EXPIRED lease (crash recovery).
    Returns the lease token, or None if another worker owns it — the caller
    must then not touch the row."""
    token = uuid4().hex
    now = timezone.now()
    updated = (
        ForgejoDelivery.objects.filter(pk=delivery.pk)
        .filter(Q(status="pending") | Q(status="processing", lease_expires_at__lt=now))
        .update(
            status="processing",
            lease_token=token,
            lease_expires_at=now + timezone.timedelta(seconds=LEASE_SECONDS),
        )
    )
    return token if updated else None


def _is_alias(delivery) -> bool:
    """Thin forward to the lifecycle owner. Classifying a stored observation
    belongs with the code that stores it (BIP-56); a second definition here is
    the two-owners defect this extraction removed."""
    return inbox.is_alias(delivery)


def _resolve_alias(delivery):
    """Resolve a coalesced alias against the CURRENT holder — never a stale
    snapshot. Returns ("processed", result) | ("pending", None) |
    ("missing", None). Lazily finalizes the alias with the holder's
    authoritative result once the holder completes, so a retry returns the real
    outcome (moved/failed), not the pending placeholder it was created with."""
    holder_id = (delivery.result or {}).get("coalesced_to")
    holder = None
    if holder_id:
        holder = ForgejoDelivery.objects.filter(delivery_id=holder_id).first()
    if holder is None and delivery.semantic_key:
        # re-resolve by the semantic key (covers a transient missing holder at
        # alias-creation time — the loser of a first-holder race).
        holder = (
            ForgejoDelivery.objects.filter(semantic_key_hash=skey.key_hash(delivery.semantic_key))
            .exclude(pk=delivery.pk)
            .first()
        )
    if holder is None:
        return "missing", None
    if holder.status == "processed":
        # coalesced_to LAST: it is THE alias discriminator (inbox.is_alias),
        # and spreading the holder's result OVER it would let a holder that
        # ever carried a key by that name silently overwrite this alias's own
        # pointer — one accidental key away from a row that never executes
        # (Vex, BIP-38 result-contract census; see bridge/delivery_result.py).
        final = {**(holder.result or {"moved": []}), "coalesced_to": holder.delivery_id}
        ForgejoDelivery.objects.filter(pk=delivery.pk).exclude(status="processed").update(
            status="processed", processed_at=timezone.now(), last_error=None,
            lease_token=None, lease_expires_at=None, result=final,
        )
        return "processed", final
    return "pending", None


def process_delivery(delivery, lease_token: str) -> dict:
    """Process one CLAIMED inbox row. Completion/failure writes are
    conditioned on lease ownership: a stale processor whose lease was
    reclaimed cannot clobber the owner's outcome. The moved list is accumulated
    across attempts, but it is now ALWAYS EMPTY: no event moves a ticket, so
    what the accumulation actually preserves is the refusal record. The
    structure is kept because retries must not lose earlier refs' outcomes."""
    # Entry gate: never begin side effects on a lease we no longer hold
    # (RC 3071). There is no board mutation to revalidate under lock any more —
    # `_advance` writes only the durable refusal — so this gate and the delivery
    # lease-row lock are the whole of the ownership story.
    if not ForgejoDelivery.objects.filter(
        pk=delivery.pk,
        lease_token=lease_token,
        status="processing",
        lease_expires_at__gte=timezone.now(),
    ).exists():
        raise _LeaseLostError(f"lease on {delivery.delivery_id} is not held")
    # A coalesced alias NEVER executes — it defers to its holder. Resolve and
    # release the lease; if the holder is not done yet, stay retryable so a
    # later tick finalizes it with the real outcome (Morrow RC 3348).
    if _is_alias(delivery):
        state, result = _resolve_alias(delivery)
        if state == "processed":
            # THE THIRD EXIT, AND IT MUST NOT TELL ANYONE — a comment here would
            # be a SECOND comment about ONE real event. The alias is a different
            # delivery id for an event the holder already processed and already
            # reported; `reply` keys its idempotency marker on the delivery id,
            # so it would not recognise the holder's comment as its own.
            #
            # Written down because this is the exact place a later reader,
            # seeing two call sites below, would add a third and call it a fix.
            # The trade is deliberate: if the holder's reply failed, the message
            # is lost rather than retried. Telling someone is best-effort by
            # construction, and a bridge that comments on every redelivery is a
            # bridge people mute.
            return result
        ForgejoDelivery.objects.filter(pk=delivery.pk, lease_token=lease_token).update(
            lease_token=None, lease_expires_at=None, status="pending",
        )
        raise _TransientBridgeError(f"coalesced alias {delivery.delivery_id} awaiting holder")
    moved = []
    no_ticket = None
    try:
        payload, event, repo = delivery.payload, delivery.event, delivery.repository
        # Rehydrate the sending forge from the row; pre-BIP-15 rows carry the
        # column default and read exactly as they always did. NO fallback
        # (Morrow 10147): an unknown stored name interpreted as Forgejo would
        # read the payload with the wrong semantics AND inherit Forgejo's
        # legacy tenancy keys — a corrupt/removed/future personality stays
        # pending, loudly, with zero writes.
        forge = forges.by_name(delivery.forge)
        if forge is None:
            raise _ConfigDefectError(
                f"delivery {delivery.delivery_id} stores unknown forge "
                f"{delivery.forge!r}; refusing to guess its payload semantics"
            )
        scope = _project_scope(payload, repo, forge)
        unscoped = scope is None
        rejected = []
        if unscoped:
            pass  # legitimate no-op; recorded below
        else:
            if event == "review_rejected":
                try:
                    result = _apply_review_rejection(delivery, lease_token, scope, repo, payload)
                except (_ConfigDefectError, _TransientBridgeError, _LeaseLostError):
                    raise
                except Exception as e:
                    logger.exception("git-bridge: failed recording review-rejection refusal; nothing was written")
                    raise _TransientBridgeError("failed recording review-rejection refusal") from e
                _tell_whoever_acted(delivery, result, repo, payload)
                return result
            else:
                # Each directive carries its own class, so ONE merged PR can
                # PROPOSE advance for one ticket and completion for another.
                # Both board writes are off, so neither proposal is carried out;
                # the class still decides which refusal is recorded.
                forward_near = []
                forward_conflicts = []
                refs = list(_collect_refs(event, payload, repo, forge,
                                          near_misses=forward_near, conflicts=forward_conflicts))
                # ONLY A MERGED PULL REQUEST (Morrow cold read). describe_no_nomination
                # is about an event that COULD have completed something and named
                # nothing. An UNMERGED pull request yields no refs by design even
                # when its body carries a directive, and an ordinary push with no
                # directive is the overwhelmingly common case — telling either of
                # them "no ticket was named" is durable noise, and it would speak
                # on the pull request too.
                #
                # NO TICKET means no nomination AND NO NEAR MISS (Morrow): a
                # near miss is a named ticket that failed the trailer rule —
                # "Closes BIP-7 after QA" — and telling its author "no ticket
                # was named" is confidently false. The near miss itself is
                # recorded below and rendered by the reply.
                if (not refs and not forward_near
                        and event == "pull_request" and forge.merged_pull_request(payload)[0]):
                    no_ticket = write_boundary.describe_no_nomination(
                        repo=repo, context=f"{event} in {repo}"
                    )
                for (identifier, seq), context, klass in refs:
                    project = scope.get(identifier)
                    if project is None:
                        # Scope guard (BIP-38): ref to a project outside this
                        # repository's mapped scope. REJECTED with zero writes
                        # for THIS ref — in-scope refs on the same delivery
                        # still proceed (mixed events, §M2) — and durably
                        # recorded in the delivery result, not just logs.
                        logger.warning(
                            f"git-bridge: REJECTED cross-project ref {identifier}-{seq} "
                            f"from {repo}: not in the repository's mapped scope"
                        )
                        rejected.append(
                            delivery_result.cross_project_entry(
                                f"{identifier}-{seq}", repo,
                                "project not in this repository's mapped scope",
                            )
                        )
                        continue
                    try:
                        if _advance(delivery, lease_token, project, event, seq, klass, context, repo):
                            moved.append(f"{identifier}-{seq}")
                    except (_ConfigDefectError, _TransientBridgeError, _LeaseLostError):
                        raise
                    except Exception as e:
                        logger.exception(
                            f"git-bridge: failed recording refusal for {identifier}-{seq}; "
                            "nothing was written"
                        )
                        raise _TransientBridgeError(f"failed recording refusal for {identifier}-{seq}") from e
        fresh = ForgejoDelivery.objects.get(pk=delivery.pk)
        result = dict(fresh.result or {"moved": []})
        if not unscoped and event != "review_rejected" and (forward_near or forward_conflicts):
            # The parse produced more than tickets; the durable record carries
            # all of it (Scope A 108-112) — near misses loudly, and a
            # Closes+Refs demotion as a recorded conflict, not a silent
            # under-move.
            result = delivery_result.merge_ignored(
                result, near_misses=forward_near, conflicts=forward_conflicts
            )
        if unscoped:
            result = delivery_result.build(unscoped_repo=repo)
        elif no_ticket is not None:
            result = delivery_result.merge_ignored(
                result, no_ticket=no_ticket, cross_project=rejected
            )
        elif rejected:
            # Rejections are recorded at completion (they mutate no board
            # state, so they carry no crash-consistency coupling with the
            # per-ref incremental writes); merge_ignored preserves moved.
            result = delivery_result.merge_ignored(result, cross_project=rejected)
        owned = ForgejoDelivery.objects.filter(pk=delivery.pk, lease_token=lease_token, status="processing").update(
            status="processed",
            attempts=F("attempts") + 1,
            processed_at=timezone.now(),
            last_error=None,
            lease_token=None,
            lease_expires_at=None,
            result=result,
        )
        if not owned:
            raise _LeaseLostError(f"lease on {delivery.delivery_id} lost at completion")

        _tell_whoever_acted(delivery, result, repo, payload)
        return result
    except _LeaseLostError:
        raise
    except (_ConfigDefectError, _TransientBridgeError) as e:
        attempts = delivery.attempts + 1
        backoff = min(2 ** min(attempts, 12), RETRY_BACKOFF_CAP_SECONDS)
        ForgejoDelivery.objects.filter(pk=delivery.pk, lease_token=lease_token, status="processing").update(
            status="pending",
            attempts=F("attempts") + 1,
            last_error=str(e)[:1000],
            next_attempt_at=timezone.now() + timezone.timedelta(seconds=backoff),
            lease_token=None,
            lease_expires_at=None,
        )
        raise



def _provider_instance(forge) -> str:
    """The per-forge-INSTANCE namespace for semantic keys (ADR 010 §1). Endpoint
    defence-in-depth: it CONSUMES the single config owner (instance_config) and
    does not restate presence/validity predicates — a config defect (missing or
    malformed) is refused BEFORE parsing (4e), the 500 sitting behind the
    startup check's fail-closed refusal, not instead of it."""
    try:
        return instance_config.resolve(forge)
    except instance_config.InstanceConfigError as e:
        raise _ConfigDefectError(e.detail) from e


def _semantic_key(event, payload, forge):
    """Canonical semantic key for THIS observation, or None when the event
    asserts no dedupable transition (unmerged PR) OR its complete, immutable
    identity tuple cannot be proven (Morrow 3329 b2): a key is derived only
    from a fully present tuple, never a partial one that could alias. Review
    observations use the same holder/alias lifecycle as push and merge events.
    The verdict is not part of identity — and it is no longer re-read from the
    forge either: the SIGNED event supplies it, since the ask needs no forge
    permission (the authority re-read was deleted with the write it guarded)."""
    repo_id = forge.repo_stable_id(payload)
    if repo_id is None:
        return None
    instance = _provider_instance(forge)
    try:
        if event == "push":
            return skey.push_key(
                instance, repo_id, payload.get("ref"), payload.get("before"), payload.get("after")
            )
        if event == "pull_request":
            merged, _fields, _number = forge.merged_pull_request(payload)
            if not merged:
                return None
            number, merge_sha = forge.merge_identity(payload)
            return skey.merged_pr_key(instance, repo_id, number, merge_sha)
        if event == "review_rejected":
            pull = payload.get("pull_request") or {}
            review = payload.get("review") or {}
            return skey.review_key(instance, repo_id, pull.get("number"), review.get("id"))
    except skey.IncompleteEvent:
        return None
    return None


class ForgejoWebhookEndpoint(APIView):
    """POST /api/public/git-bridge/forgejo/ — HMAC-verified Forgejo webhook."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = []  # HMAC-verified machine traffic: Forgejo deliveries burst legitimately

    def post(self, request):
        forge = _authenticate(request)
        if forge is None:
            return Response({"error": "signature"}, status=status.HTTP_403_FORBIDDEN)
        event = forge.event(request)
        if event not in HANDLED_EVENTS:
            return Response({"ok": True, "ignored": event}, status=status.HTTP_200_OK)
        # 4e (ADR 010 / Morrow RC 3432): provider_instance is server CONFIG. An
        # empty configured id is a DEPLOYMENT DEFECT — refuse HERE, before the
        # body is parsed and before any row is written, never degrade it into a
        # silently-unkeyed row that keeps looking healthy.
        try:
            _provider_instance(forge)
        except _ConfigDefectError as e:
            logger.error(f"git-bridge: {e}; refusing delivery")
            return Response(
                {"error": "provider instance not configured"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        try:
            payload = _parse_payload(request.body)
            _validate_shape(event, payload, forge)
        except _MalformedDeliveryError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        delivery_id = forge.delivery_id(request)
        if not delivery_id or not DELIVERY_ID_RE.match(delivery_id):
            # Every supported forge emits a canonical uuid; anything else is
            # malformed — and an oversized value must 400 here, never reach
            # the column as a DataError (RC 3071). Name the header the
            # SENDING forge actually uses (Morrow 10147, low).
            return Response(
                {"error": f"{forge.delivery_headers[0]} must be a canonical UUID"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        repo = forge.repo_full_name(payload)
        digest = _body_digest(request.body, forge)
        # Semantic-key dedup (BIP-46): a prior observation of the SAME real
        # event — by any transport, under any delivery_id — wins. delivery_id
        # dedup below still catches a same-id webhook replay.
        try:
            canonical = _semantic_key(event, payload, forge)
        except ValueError as e:
            # A component carrying the reserved separator (a 0x1f control char
            # in a ref/sha) is malformed input on a signed endpoint. Fail
            # CLOSED with a clean 400 — never let it escape as a 500 (Morrow
            # RC 3335). IncompleteEvent is handled inside _semantic_key and
            # never reaches here.
            return Response(
                {"error": f"malformed event: {e}"}, status=status.HTTP_400_BAD_REQUEST
            )
        skhash = skey.key_hash(canonical) if canonical else None
        # Delivery-id binding is enforced FIRST (below): get_or_create finds an
        # existing delivery_id and the collision check 409s a reused id with new
        # content, BEFORE any semantic coalescing (Morrow 3329 b1). Semantic
        # coalescing happens ONLY when a genuinely NEW delivery_id carries an
        # event already stored under another id — the unique constraint raises
        # IntegrityError and we return the prior outcome.
        recorded = inbox.record_observation(
            delivery_id=delivery_id, event=event, payload=payload,
            repository=repo, digest=digest, forge_name=forge.name,
            canonical_key=canonical, key_hash=skhash,
        )
        if recorded.outcome == inbox.COLLISION:
            # reused delivery id with different content: fail closed, on the
            # alias path exactly as on the normal one (Morrow RC 3348 b2).
            return Response(
                {"error": "delivery id collision with different content"},
                status=status.HTTP_409_CONFLICT,
            )
        if recorded.is_alias:
            state, result = _resolve_alias(recorded.delivery)
            if state == "processed":
                return Response(
                    {"ok": True, "duplicate": True, **result}, status=status.HTTP_200_OK
                )
            # holder in flight (or a transient missing holder): retryable, and
            # the alias row persists for the reconciler to finalize.
            return Response(
                {"ok": True, "pending": True, "coalesced_to": recorded.coalesced_to},
                status=status.HTTP_202_ACCEPTED,
            )
        delivery = recorded.delivery
        created = recorded.outcome == inbox.CREATED
        if not created:
            # content binding already enforced by the seam (COLLISION above)
            if _is_alias(delivery):
                # a retry of a coalesced alias resolves the holder's CURRENT
                # outcome and NEVER executes (Morrow RC 3348 b3).
                state, result = _resolve_alias(delivery)
                if state == "processed":
                    return Response(
                        {"ok": True, "duplicate": True, **result}, status=status.HTTP_200_OK
                    )
                return Response(
                    {"ok": True, "pending": True,
                     "coalesced_to": (delivery.result or {}).get("coalesced_to")},
                    status=status.HTTP_202_ACCEPTED,
                )
            if delivery.status == "processed":
                return Response(
                    {"ok": True, "duplicate": True, **(delivery.result or {"moved": []})},
                    status=status.HTTP_200_OK,
                )
        if _is_truncated(event, payload, forge) and (_api_creds_present() or not forge.resolves_push_ranges):
            # never resolve ranges on the sole API worker (RC 3070): the row
            # is due immediately and the reconciler owns the fetch. A forge
            # with no resolver defers too — the reconciler records the loud
            # unresolvable error rather than this path guessing at one.
            return Response({"ok": True, "deferred": True, "pending": True}, status=status.HTTP_202_ACCEPTED)
        lease = claim_delivery(delivery)
        if lease is None:
            # the original request (or a reconciler) is processing it now
            return Response({"ok": True, "pending": True}, status=status.HTTP_202_ACCEPTED)
        delivery.refresh_from_db()
        try:
            result = process_delivery(delivery, lease)
        except _LeaseLostError as e:
            return Response({"ok": True, "pending": True, "note": str(e)}, status=status.HTTP_202_ACCEPTED)
        except _ConfigDefectError as e:
            return Response(
                {"ok": False, "pending": True, "error": f"bridge configuration defect: {e}"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except _TransientBridgeError as e:
            return Response(
                {"ok": False, "pending": True, "error": str(e)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({"ok": True, **result}, status=status.HTTP_200_OK)
