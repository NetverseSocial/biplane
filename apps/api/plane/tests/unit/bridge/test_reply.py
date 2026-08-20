# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The bridge's first outbound write: telling the actor where they acted.

Every case here is a way this could hurt rather than help — changing a
delivery's outcome, commenting twice on a redelivery, or speaking when it has
nothing to say. The happy path is one test; the rest are restraints.
"""

from unittest import mock

import pytest
from django.test import override_settings

from plane.bridge import reply

REPO = "acme/app"
CREDS = override_settings(FORGEJO_BASE_URL="https://forge.example", FORGEJO_BRIDGE_WRITE_TOKEN="w0ken")


class _Resp:
    def __init__(self, status=200, body=None, raises=None):
        self.status_code = status
        self._body = body if body is not None else []
        self._raises = raises

    def json(self):
        if self._raises:
            raise self._raises
        return self._body


def _calls(get=None, post=None):
    """Patch the module's HTTP surface, returning the recorded posts."""
    posted = []

    def _get(url, **kw):
        return get if get is not None else _Resp(200, [])

    def _post(url, **kw):
        posted.append({"url": url, "json": kw.get("json")})
        return post if post is not None else _Resp(201, {})

    return posted, mock.patch.multiple(reply.http_requests, get=_get, post=_post)


NEAR_MISS = {"moved": [], "ignored": {"near_misses": ["Closes BIP-7 once CI is green"]}}


@CREDS
def test_it_says_what_was_not_done_on_the_pull_request():
    posted, patch = _calls()
    with patch:
        assert reply.refusal_comment(delivery_id="d1", result=NEAR_MISS, repo=REPO, number=8, forge="forgejo") is True
    assert posted[0]["url"].endswith(f"/repos/{REPO}/issues/8/comments")
    body = posted[0]["json"]["body"]
    assert "did not move a ticket" in body
    assert "Closes BIP-7 once CI is green" in body
    assert "the bridge assists, it does not decide" in body


@CREDS
def test_a_redelivery_does_not_comment_twice():
    """Forge redeliveries and lease retries re-run processing. A bridge that
    comments every time is a bridge people mute."""
    marker = reply._MARKER.format(delivery_id="d1")
    posted, patch = _calls(get=_Resp(200, [{"body": f"{marker}\nalready said this"}]))
    with patch:
        assert reply.refusal_comment(delivery_id="d1", result=NEAR_MISS, repo=REPO, number=8, forge="forgejo") is False
    assert posted == []


@CREDS
def test_a_DIFFERENT_delivery_about_the_same_pull_request_still_speaks():
    """The marker is keyed on the delivery, not the pull request — a genuinely
    new event must not be silenced by an older comment."""
    other = reply._MARKER.format(delivery_id="OLD")
    posted, patch = _calls(get=_Resp(200, [{"body": f"{other}\nan earlier refusal"}]))
    with patch:
        assert reply.refusal_comment(delivery_id="d2", result=NEAR_MISS, repo=REPO, number=8, forge="forgejo") is True
    assert len(posted) == 1


@CREDS
@pytest.mark.parametrize("failure", [
    {"post": _Resp(500, {})},
    {"post": _Resp(403, {})},
    {"post": None},   # replaced below with a raising transport
])
def test_a_FAILED_reply_never_raises(failure):
    """THE LOAD-BEARING RESTRAINT. The decision is already made and its refusal
    already durable by the time this runs — there is no board write, and this
    function only ever follows a refusal — so telling someone is best-effort. If
    this raised, the delivery would retry, and the retry would re-run the
    decision in order to produce a message."""
    if failure["post"] is None:
        def _boom(*a, **kw):
            raise reply.http_requests.RequestException("network gone")
        patch = mock.patch.multiple(reply.http_requests, get=lambda *a, **kw: _Resp(200, []), post=_boom)
    else:
        _, patch = _calls(post=failure["post"])
    with patch:
        assert reply.refusal_comment(delivery_id="d1", result=NEAR_MISS, repo=REPO, number=8, forge="forgejo") is False


@CREDS
def test_an_unreadable_comment_list_declines_to_speak():
    """If we cannot tell whether we already commented, silence is the safe
    direction — commenting again is what trains people to ignore the bridge."""
    posted, patch = _calls(get=_Resp(500, {}))
    with patch:
        assert reply.refusal_comment(delivery_id="d1", result=NEAR_MISS, repo=REPO, number=8, forge="forgejo") is False
    assert posted == []


@CREDS
@pytest.mark.parametrize("result", [
    {"moved": ["BIP-7"]},                       # a clean move says nothing
    {"moved": [], "ignored": {}},
    {"moved": []},
    {},
    None,
])
def test_it_stays_silent_when_there_is_nothing_to_report(result):
    posted, patch = _calls()
    with patch:
        assert reply.refusal_comment(delivery_id="d1", result=result, repo=REPO, number=8, forge="forgejo") is False
    assert posted == []


@CREDS
@pytest.mark.parametrize("repo,number", [(None, 8), (REPO, None), ("", 8)])
def test_it_stays_silent_without_somewhere_to_speak(repo, number):
    """Push events have no pull request to comment on."""
    posted, patch = _calls()
    with patch:
        assert reply.refusal_comment(
            delivery_id="d1", result=NEAR_MISS, repo=repo, number=number, forge="forgejo"
        ) is False
    assert posted == []


@override_settings(FORGEJO_BASE_URL=None, FORGEJO_BRIDGE_API_TOKEN=None)
def test_an_unconfigured_deployment_is_silent_rather_than_broken():
    posted, patch = _calls()
    with patch:
        assert reply.refusal_comment(delivery_id="d1", result=NEAR_MISS, repo=REPO, number=8, forge="forgejo") is False
    assert posted == []


@CREDS
def test_it_carries_the_structured_refusal_when_one_is_present():
    """The shape Sable's write boundary produces: ignored.unverified entries
    with a ticket, a stable reason code and a human sentence. This consumes the
    sentence and never parses the prose.

    NOTE the `notify` key below is deliberately EXTRA. The boundary no longer
    emits it — recipient selection was cut — and it is kept here as a
    forward-compatibility pin: the renderer must tolerate unknown keys on a
    durable record rather than assume its own producer's exact shape, because
    results persisted by an older build are replayed by a newer one."""
    result = {"moved": [], "ignored": {"unverified": [
        {"ticket": "BIP-7", "reason": "no_reviewers",
         "detail": "BIP-7 names no reviewers, so approvals cannot be checked",
         "notify": ["u-1"]},
    ]}}
    posted, patch = _calls()
    with patch:
        assert reply.refusal_comment(delivery_id="d1", result=result, repo=REPO, number=8, forge="forgejo") is True
    body = posted[0]["json"]["body"]
    assert "**BIP-7**" in body
    assert "names no reviewers" in body


@CREDS
def test_scope_and_unmapped_repository_refusals_are_reported():
    result = {"moved": [], "ignored": {
        "cross_project": [{"ticket": "SB-3", "repo": "acme/x", "reason": "outside the mapped project"}],
        "unscoped_repo": "acme/unmapped",
    }}
    posted, patch = _calls()
    with patch:
        assert reply.refusal_comment(delivery_id="d1", result=result, repo=REPO, number=8, forge="forgejo") is True
    body = posted[0]["json"]["body"]
    assert "SB-3" in body and "outside the mapped project" in body
    assert "acme/unmapped" in body


@CREDS
@pytest.mark.parametrize("forge", ["github", "gitlab", "gitea", "", None, "GitHub", "unknown"])
def test_it_NEVER_speaks_for_a_delivery_from_another_forge(forge):
    """Rowan RC 3765, and this is the dangerous one.

    `FORGEJO_BASE_URL` is a single hardcoded destination. Without this guard a
    GitHub-sourced delivery would post its refusal to whatever repository and
    pull-request NUMBER happen to coincide on the Forgejo instance — a comment
    on a stranger's work, about an event that never happened there. The
    delivery's OWN forge decides whether we may speak; unknown is silence.
    """
    posted, patch = _calls()
    with patch:
        assert reply.refusal_comment(
            delivery_id="d1", result=NEAR_MISS, repo=REPO, number=8, forge=forge) is False
    assert posted == [], f"posted to Forgejo for a {forge!r} delivery"


@CREDS
@pytest.mark.parametrize("forge", ["forgejo", "Forgejo", "FORGEJO"])
def test_it_speaks_for_the_one_forge_it_has_a_destination_for(forge):
    """The positive control: refusing everything would satisfy the test above."""
    posted, patch = _calls()
    with patch:
        assert reply.refusal_comment(
            delivery_id="d1", result=NEAR_MISS, repo=REPO, number=8, forge=forge) is True
    assert len(posted) == 1


@override_settings(FORGEJO_BASE_URL="https://forge.example",
                   FORGEJO_BRIDGE_API_TOKEN="read-only", FORGEJO_BRIDGE_WRITE_TOKEN="")
def test_the_READ_token_is_not_accepted_as_a_write_credential():
    """Rowan RC 3765: the shipped bridge token is read-only by design — a POST
    with it returns 403, and on the live board it is present but EMPTY. Falling
    back to it would 403 on every refusal, which reads as a broken bridge rather
    than an unconfigured feature."""
    posted, patch = _calls()
    with patch:
        assert reply.refusal_comment(
            delivery_id="d1", result=NEAR_MISS, repo=REPO, number=8, forge="forgejo") is False
    assert posted == []


@CREDS
def test_the_forge_check_happens_BEFORE_any_network_call():
    """Morrow's fix direction: fail closed before touching the network for every
    non-Forgejo provider. A guard that refuses only after reading the comment
    list has already told a stranger's forge that we are interested in their
    pull request."""
    touched = []

    def _seen(url, **kw):
        touched.append(url)
        return _Resp(200, [])

    with mock.patch.multiple(reply.http_requests, get=_seen, post=_seen):
        assert reply.refusal_comment(
            delivery_id="d1", result=NEAR_MISS, repo=REPO, number=8, forge="github") is False
    assert touched == [], f"made a network call for a github delivery: {touched}"


@CREDS
def test_a_github_delivery_with_a_COLLIDING_repo_and_number_is_still_silent():
    """The concrete harm, spelled out: `acme/app#8` exists on GitHub AND on this
    Forgejo. Without the provider check the refusal for the GitHub merge lands on
    the unrelated Forgejo pull request of the same number."""
    posted, patch = _calls()
    with patch:
        assert reply.refusal_comment(
            delivery_id="gh-1", result=NEAR_MISS, repo=REPO, number=8, forge="github") is False
    assert posted == []


@CREDS
def test_a_gitlab_delivery_is_silent_too():
    """GitLab merge requests are numbered per project exactly as Forgejo pull
    requests are, so the same collision applies."""
    posted, patch = _calls()
    with patch:
        assert reply.refusal_comment(
            delivery_id="gl-1", result=NEAR_MISS, repo=REPO, number=8, forge="gitlab") is False
    assert posted == []


@override_settings(FORGEJO_BASE_URL="https://forge.example",
                   FORGEJO_BRIDGE_API_TOKEN="read-only-token", FORGEJO_BRIDGE_WRITE_TOKEN="reply-token")
def test_the_reply_credential_is_the_one_actually_sent():
    """The read token must not be what authorises a write, even when both are
    configured — otherwise widening the read token silently becomes the fix
    someone reaches for."""
    sent = {}

    def _post(url, **kw):
        sent.update(kw.get("headers") or {})
        return _Resp(201, {})

    with mock.patch.multiple(reply.http_requests, get=lambda *a, **kw: _Resp(200, []), post=_post):
        assert reply.refusal_comment(
            delivery_id="d1", result=NEAR_MISS, repo=REPO, number=8, forge="forgejo") is True
    assert sent["Authorization"] == "token reply-token"
    assert "read-only-token" not in sent["Authorization"]


@CREDS
def test_GITEA_is_silent_because_there_is_nowhere_of_its_own_to_speak():
    """Morrow RC 3767. Gitea speaks the same comment API, which is exactly why
    listing it was tempting and wrong: there is one configured destination, so a
    Gitea delivery would have posted to the FORGEJO instance. Speaking the same
    API is not the same as knowing where to speak. A forge joins the list only
    with a destination of its own."""
    posted, patch = _calls()
    with patch:
        assert reply.refusal_comment(
            delivery_id="gt-1", result=NEAR_MISS, repo=REPO, number=8, forge="gitea") is False
    assert posted == []
