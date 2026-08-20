# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""BIP-15: forge personality envelope.

Every case here is a negative or a boundary. A personality that accepts its own
correctly-signed delivery proves almost nothing — the bridge's whole security
posture is what it REFUSES, so that is what these pin:

  - a tampered body
  - the wrong secret
  - an absent signature
  - a bare digest where a prefixed one is required
  - a sha1 digest where sha256 is required
  - one forge's credential presented to another forge
"""

import hashlib
import hmac

import pytest

from plane.bridge.forges import (
    FORGES,
    ForgejoForge,
    GitHubForge,
    GitLabForge,
    by_name,
    detect,
)

SECRET = "a-secret-long-enough-to-be-plausible"
BODY = b'{"ref":"refs/heads/main"}'


class FakeRequest:
    def __init__(self, headers=None, body=BODY):
        self.headers = headers or {}
        self.body = body


def hmac_hex(secret=SECRET, body=BODY):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------
# each personality accepts its own well-formed delivery (the baseline, so the
# negatives below mean something)
# --------------------------------------------------------------------------


def test_forgejo_accepts_its_own_signature():
    r = FakeRequest({"X-Forgejo-Signature": hmac_hex()})
    assert ForgejoForge.verify(r, SECRET) is True


def test_gitea_header_is_accepted_by_the_forgejo_personality():
    # Same wire format; Gitea only differs by header prefix.
    r = FakeRequest({"X-Gitea-Signature": hmac_hex()})
    assert ForgejoForge.verify(r, SECRET) is True


def test_github_accepts_its_own_prefixed_signature():
    r = FakeRequest({"X-Hub-Signature-256": f"sha256={hmac_hex()}"})
    assert GitHubForge.verify(r, SECRET) is True


def test_gitlab_accepts_the_echoed_secret():
    r = FakeRequest({"X-Gitlab-Token": SECRET})
    assert GitLabForge.verify(r, SECRET) is True


# --------------------------------------------------------------------------
# refusals — the point of the module
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forge,headers",
    [
        (ForgejoForge, {"X-Forgejo-Signature": hmac_hex()}),
        (GitHubForge, {"X-Hub-Signature-256": f"sha256={hmac_hex()}"}),
    ],
)
def test_body_bound_forges_reject_a_tampered_body(forge, headers):
    # Signature computed over BODY, delivered with different bytes.
    r = FakeRequest(headers, body=BODY + b" ")
    assert forge.verify(r, SECRET) is False


@pytest.mark.parametrize("forge", [ForgejoForge, GitHubForge, GitLabForge])
def test_every_forge_rejects_the_wrong_secret(forge):
    good = {
        "forgejo": {"X-Forgejo-Signature": hmac_hex()},
        "github": {"X-Hub-Signature-256": f"sha256={hmac_hex()}"},
        "gitlab": {"X-Gitlab-Token": SECRET},
    }[forge.name]
    assert forge.verify(FakeRequest(good), "a-different-secret-entirely") is False


@pytest.mark.parametrize("forge", [ForgejoForge, GitHubForge, GitLabForge])
def test_every_forge_rejects_an_absent_signature(forge):
    assert forge.verify(FakeRequest({}), SECRET) is False


def test_github_rejects_a_bare_digest_without_the_prefix():
    # The reason verify() builds the expected value WITH "sha256=" instead of
    # stripping the prefix from what arrived.
    r = FakeRequest({"X-Hub-Signature-256": hmac_hex()})
    assert GitHubForge.verify(r, SECRET) is False


def test_github_rejects_a_sha1_prefixed_digest():
    sha1 = hmac.new(SECRET.encode(), BODY, hashlib.sha1).hexdigest()
    r = FakeRequest({"X-Hub-Signature-256": f"sha1={sha1}"})
    assert GitHubForge.verify(r, SECRET) is False


def test_gitlab_rejects_an_hmac_where_the_raw_secret_is_required():
    r = FakeRequest({"X-Gitlab-Token": hmac_hex()})
    assert GitLabForge.verify(r, SECRET) is False


# --------------------------------------------------------------------------
# cross-forge: a credential valid for one forge must not satisfy another
# --------------------------------------------------------------------------


def test_a_github_delivery_is_not_accepted_by_the_forgejo_personality():
    r = FakeRequest({"X-Hub-Signature-256": f"sha256={hmac_hex()}"})
    assert ForgejoForge.verify(r, SECRET) is False


def test_a_forgejo_delivery_is_not_accepted_by_the_github_personality():
    r = FakeRequest({"X-Forgejo-Signature": hmac_hex()})
    assert GitHubForge.verify(r, SECRET) is False


def test_a_gitlab_token_does_not_satisfy_a_body_bound_forge():
    r = FakeRequest({"X-Gitlab-Token": SECRET})
    for forge in (ForgejoForge, GitHubForge):
        assert forge.verify(r, SECRET) is False


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"X-Forgejo-Signature": "x"}, "forgejo"),
        ({"X-Gitea-Signature": "x"}, "forgejo"),
        ({"X-Hub-Signature-256": "x"}, "github"),
        ({"X-Gitlab-Token": "x"}, "gitlab"),
    ],
)
def test_detect_selects_on_the_signature_header(headers, expected):
    assert detect(FakeRequest(headers)).name == expected


def test_detect_returns_none_when_no_credential_is_present():
    # Callers must treat this as unauthenticated, never as a default forge.
    assert detect(FakeRequest({"X-GitHub-Event": "push"})) is None


def test_detect_ignores_the_event_header_when_choosing():
    # A delivery carrying GitLab's event name but GitHub's signature must be
    # judged as GitHub — the credential decides, not the event, or the weaker
    # scheme could be selected by an attacker-chosen header.
    r = FakeRequest({"X-Gitlab-Event": "Push Hook", "X-Hub-Signature-256": "x"})
    assert detect(r).name == "github"


# --------------------------------------------------------------------------
# envelope extraction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forge,headers,expected",
    [
        (ForgejoForge, {"X-Forgejo-Event": "push"}, "push"),
        (ForgejoForge, {"X-Gitea-Event": "pull_request"}, "pull_request"),
        (GitHubForge, {"X-GitHub-Event": "push"}, "push"),
        (GitLabForge, {"X-Gitlab-Event": "Push Hook"}, "push"),
        (GitLabForge, {"X-Gitlab-Event": "Merge Request Hook"}, "pull_request"),
    ],
)
def test_event_normalisation(forge, headers, expected):
    assert forge.event(FakeRequest(headers)) == expected


@pytest.mark.parametrize("forge", [ForgejoForge, GitHubForge, GitLabForge])
def test_unknown_events_normalise_to_empty_not_to_a_guess(forge):
    header = forge.event_headers[0]
    assert forge.event(FakeRequest({header: "issues"})) == ""
    assert forge.event(FakeRequest({})) == ""


@pytest.mark.parametrize(
    "forge,headers",
    [
        (ForgejoForge, {"X-Forgejo-Delivery": "abc"}),
        (ForgejoForge, {"X-Gitea-Delivery": "abc"}),
        (GitHubForge, {"X-GitHub-Delivery": "abc"}),
        (GitLabForge, {"X-Gitlab-Event-UUID": "abc"}),
    ],
)
def test_delivery_id_extraction(forge, headers):
    assert forge.delivery_id(FakeRequest(headers)) == "abc"


@pytest.mark.parametrize("forge", [ForgejoForge, GitHubForge, GitLabForge])
def test_delivery_id_is_none_when_absent(forge):
    assert forge.delivery_id(FakeRequest({})) is None


# --------------------------------------------------------------------------
# the body-binding difference is declared, not hidden
# --------------------------------------------------------------------------


def test_gitlab_declares_it_is_not_body_bound():
    assert GitLabForge.body_bound is False
    assert ForgejoForge.body_bound is True
    assert GitHubForge.body_bound is True


def test_by_name_round_trips_every_registered_forge():
    for forge in FORGES:
        assert by_name(forge.name) is forge
    assert by_name("bitbucket") is None


# --------------------------------------------------------------------------
# non-ASCII credentials must be REFUSED, never raise
#
# hmac.compare_digest raises TypeError on non-ASCII str operands, and every
# `provided` here is an attacker-controlled header. On main this turned one
# byte into an unauthenticated 500. Each personality is checked, because a fix
# applied to only the base class would leave the two overrides open — and the
# overrides are the ones with hand-written comparisons.
# --------------------------------------------------------------------------

NON_ASCII_CREDENTIALS = [
    ("é", "accented latin"),
    ("中文", "CJK"),
    ("\U0001f600", "astral-plane emoji"),
    ("ÿ" * 64, "full-length high bytes"),
    ("deadébeef", "hidden mid-token"),
]


@pytest.mark.parametrize("value,label", NON_ASCII_CREDENTIALS)
@pytest.mark.parametrize(
    "forge,header",
    [
        (ForgejoForge, "X-Forgejo-Signature"),
        (ForgejoForge, "X-Gitea-Signature"),
        (GitHubForge, "X-Hub-Signature-256"),
        (GitLabForge, "X-Gitlab-Token"),
    ],
)
def test_non_ascii_credential_is_refused_without_raising(forge, header, value, label):
    # "did not raise" is as much the assertion as "returned False": an escaping
    # TypeError is the 500.
    assert forge.verify(FakeRequest({header: value}), SECRET) is False, label


def test_a_valid_digest_with_one_non_ascii_char_is_refused():
    # The near-miss: everything right except the last character.
    poisoned = hmac_hex()[:-1] + "é"
    assert ForgejoForge.verify(FakeRequest({"X-Forgejo-Signature": poisoned}), SECRET) is False
    assert GitHubForge.verify(FakeRequest({"X-Hub-Signature-256": f"sha256={poisoned}"}), SECRET) is False


def test_a_non_ascii_SECRET_does_not_raise_either():
    # The other operand. An operator can configure any string as the secret,
    # and a non-ASCII one must fail closed rather than crash every delivery.
    assert ForgejoForge.verify(FakeRequest({"X-Forgejo-Signature": "abc"}), "sécret-长-enough-16") is False
    assert GitLabForge.verify(FakeRequest({"X-Gitlab-Token": "abc"}), "sécret-长-enough-16") is False


def test_the_valid_paths_still_verify():
    # Without this, a verify() that returned False unconditionally would pass
    # every refusal test above.
    assert ForgejoForge.verify(FakeRequest({"X-Forgejo-Signature": hmac_hex()}), SECRET) is True
    assert GitHubForge.verify(FakeRequest({"X-Hub-Signature-256": f"sha256={hmac_hex()}"}), SECRET) is True
    assert GitLabForge.verify(FakeRequest({"X-Gitlab-Token": SECRET}), SECRET) is True


# --------------------------------------------------------------------------
# Morrow RC 3124: the EQUALITY direction for a non-ASCII credential.
#
# Every non-ASCII case above asserts a REFUSAL, and a comparator that returned
# False unconditionally would pass all of them. That is precisely what the
# ASCII-only encode did to GitLab: its token is not a digest, it is an
# arbitrary operator-chosen secret, so a valid non-ASCII secret failed to
# encode on the EXPECTED side and could never match. Refused forever, with no
# error, while the same secret kept working for Forgejo where it is only ever
# an HMAC key.
#
# These pin the direction the refusal tests structurally cannot see.
# --------------------------------------------------------------------------

NON_ASCII_SECRETS = [
    ("passwörd-långt-nog-16", "latin with diacritics"),
    ("密码密码密码密码密码密码密码密码", "CJK"),
    ("sécret-🔐-long-enough", "emoji in the middle"),
    ("Ω" * 24, "a full-length string of non-ASCII"),
]


@pytest.mark.parametrize("secret,label", NON_ASCII_SECRETS)
def test_a_valid_non_ascii_gitlab_token_is_ACCEPTED(secret, label):
    # The regression. Before the UTF-8 fix this was False for every case.
    assert GitLabForge.verify(FakeRequest({"X-Gitlab-Token": secret}), secret) is True, label


@pytest.mark.parametrize("secret,label", NON_ASCII_SECRETS)
def test_a_wrong_non_ascii_gitlab_token_is_still_refused(secret, label):
    # ...and the acceptance above is not simply "everything matches now".
    assert GitLabForge.verify(FakeRequest({"X-Gitlab-Token": secret + "x"}), secret) is False, label


@pytest.mark.parametrize("secret,label", NON_ASCII_SECRETS)
def test_the_same_non_ascii_secret_still_works_as_an_hmac_key(secret, label):
    # The asymmetry that made this invisible in production: the identical
    # configured secret worked for Forgejo the entire time, because there it is
    # only ever a key and never compared as a string.
    digest = hmac.new(secret.encode("utf-8"), BODY, hashlib.sha256).hexdigest()
    assert ForgejoForge.verify(FakeRequest({"X-Forgejo-Signature": digest}), secret) is True, label


def test_a_non_ascii_signature_header_is_still_refused_without_raising():
    # UTF-8 must not weaken the original guard: an attacker-controlled header
    # carrying non-ASCII is still refused, now because the bytes differ rather
    # than because we declined to encode it.
    assert ForgejoForge.verify(FakeRequest({"X-Forgejo-Signature": "deadébeef"}), SECRET) is False
    assert GitHubForge.verify(FakeRequest({"X-Hub-Signature-256": "sha256=dé"}), SECRET) is False
# payload accessors (slice 3)
#
# GitLab is the reason these exist. Forgejo, Gitea and GitHub share a payload
# lineage; GitLab has a different shape, not a renamed one, so every accessor
# below is checked against BOTH lineages rather than assuming one generalises.
# --------------------------------------------------------------------------

FORGEJO_PUSH = {
    "repository": {"full_name": "example/biplane"},
    "commits": [{"id": "a" * 40, "message": "refs BIP-15 do the thing"}],
    "total_commits": 1,
}

GITLAB_PUSH = {
    "project": {"path_with_namespace": "example/biplane"},
    "commits": [{"id": "b" * 40, "message": "refs BIP-15 do the thing"}],
    "total_commits_count": 1,
}

FORGEJO_MERGE = {
    "action": "closed",
    "pull_request": {"merged": True, "title": "refs BIP-15", "body": "body", "number": 7},
}

GITLAB_MERGE = {
    "object_attributes": {"action": "merge", "title": "refs BIP-15", "description": "body", "iid": 7}
}


@pytest.mark.parametrize(
    "forge,payload",
    [(ForgejoForge, FORGEJO_PUSH), (GitHubForge, FORGEJO_PUSH), (GitLabForge, GITLAB_PUSH)],
)
def test_repo_full_name_is_read_from_each_lineage(forge, payload):
    assert forge.repo_full_name(payload) == "example/biplane"


def test_gitlab_does_not_find_a_repo_in_the_forgejo_shape():
    # The point of the override: reading GitLab's payload with Forgejo's keys
    # silently yields nothing, and a bridge that treats that as "no repo" would
    # drop every GitLab delivery without a word.
    assert GitLabForge.repo_full_name(FORGEJO_PUSH) is None
    assert ForgejoForge.repo_full_name(GITLAB_PUSH) is None


@pytest.mark.parametrize(
    "forge,payload",
    [(ForgejoForge, FORGEJO_PUSH), (GitLabForge, GITLAB_PUSH)],
)
def test_declared_commit_total_is_read_from_the_right_key(forge, payload):
    assert forge.declared_commit_total(payload) == 1


def test_github_declares_no_total_even_when_the_payload_carries_one():
    # Morrow 10147: GitHub's push webhook has NO total field — its commits
    # array is simply capped at 2048. The earlier version inherited Forgejo's
    # total_commits and the tests fed it a Forgejo-shaped payload, inventing a
    # contract GitHub never made; a real capped push then read as "not
    # truncated" and silently lost every ref beyond the cap.
    assert GitHubForge.declared_commit_total(FORGEJO_PUSH) is None
    assert GitHubForge.total_field is None
    assert GitHubForge.commit_cap == 2048


def test_a_missed_commit_total_would_hide_truncation():
    # total lives under a different key per lineage. Reading the wrong one
    # returns None, the bridge concludes "not truncated", and the commits
    # beyond the webhook limit are never fetched — references silently lost.
    assert GitLabForge.declared_commit_total(FORGEJO_PUSH) is None
    assert ForgejoForge.declared_commit_total(GITLAB_PUSH) is None


@pytest.mark.parametrize(
    "forge,payload",
    [(ForgejoForge, FORGEJO_MERGE), (GitHubForge, FORGEJO_MERGE), (GitLabForge, GITLAB_MERGE)],
)
def test_merged_pull_request_is_recognised_in_each_lineage(forge, payload):
    merged, fields, number = forge.merged_pull_request(payload)
    assert merged is True
    # Fields are returned SEPARATELY, never joined: concatenating them let one
    # field change the other's Markdown parse (Morrow RC 3571).
    assert isinstance(fields, tuple) and len(fields) == 2
    assert any("refs BIP-15" in f for f in fields)
    assert number == 7


def test_an_unmerged_close_is_not_a_merge():
    payload = {"action": "closed", "pull_request": {"merged": False, "title": "t", "number": 1}}
    merged, _, _ = ForgejoForge.merged_pull_request(payload)
    assert merged is False


def test_gitlab_merge_requires_the_merge_action_not_merely_a_close():
    payload = {"object_attributes": {"action": "close", "title": "t", "iid": 1}}
    merged, _, _ = GitLabForge.merged_pull_request(payload)
    assert merged is False


def test_gitlab_reads_iid_not_id():
    # id is a global row id; iid is the per-project number a human writes.
    # Reporting id would point reviewers at the wrong merge request.
    payload = {"object_attributes": {"action": "merge", "title": "t", "iid": 7, "id": 90210}}
    _, _, number = GitLabForge.merged_pull_request(payload)
    assert number == 7


@pytest.mark.parametrize("forge", [ForgejoForge, GitHubForge, GitLabForge])
@pytest.mark.parametrize("junk", [None, [], "string", 42, {}])
def test_accessors_are_total_and_never_raise(forge, junk):
    # A malformed payload must fail at the validator that exists to reject it,
    # producing a controlled 400 — not an AttributeError deeper in the bridge.
    assert forge.repo_full_name(junk) is None
    assert forge.commits(junk) == []
    assert forge.declared_commit_total(junk) is None
    merged, fields, number = forge.merged_pull_request(junk)
    assert merged is False and fields == ("", "") and number == "?"
