# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""BIP-46 PR-B1: the semantic event key and its dedup behavior.

Aria's three control pairs are the point: (positive) two observations of ONE
real event collapse to ONE outcome on the deployed webhook path; (negative) two
DISTINCT events never collide; (cross-transport) the key computes identically
from a webhook payload and from the primitives a poll observation would carry —
the property the whole column exists for."""

import hashlib
import hmac
import json
import uuid as uuid_lib

import pytest
from django.test import Client, override_settings

from plane.bridge import forges, semantic_key as skey
from plane.bridge.forgejo_bridge import _semantic_key
from plane.db.models import ForgejoDelivery, Issue, IssueActivity, Project, State, User, Workspace

URL = "/api/public/git-bridge/forgejo/"
SECRET = "test-bridge-secret"
REPO = "acme/x"
REPO_ID = 4242


@pytest.fixture(autouse=True)
def _bridge_secret(settings):
    settings.FORGEJO_WEBHOOK_SECRET = SECRET
    # provider-instance ids (ADR 010 §1). Using the family name as the id
    # reproduces the pre-instance keys, so value assertions hold; 4d proves
    # distinct instances differ.
    settings.FORGEJO_INSTANCE_ID = "forgejo"
    settings.GITHUB_INSTANCE_ID = "github"
    settings.GITLAB_INSTANCE_ID = "gitlab"


def _fixture():
    u = User.objects.create(email=f"gb-{uuid_lib.uuid4().hex[:8]}@example.com", username=uuid_lib.uuid4().hex[:12])
    ws = Workspace.objects.create(slug=f"g{uuid_lib.uuid4().hex[:10]}", name="G", owner=u)
    ident = "GB" + uuid_lib.uuid4().hex[:3].upper()
    proj = Project.objects.create(workspace=ws, name="P", identifier=ident)
    seqs = {}
    for i, (name, group) in enumerate(
        [("Backlog", "backlog"), ("Todo", "unstarted"), ("Review", "started"), ("Done", "completed")]
    ):
        seqs[name] = State.objects.create(
            name=name, project=proj, workspace=ws, group=group, sequence=(i + 1) * 100,
            color="#000", default=(name == "Backlog"), created_by=u,
        )
    issue = Issue.objects.create(workspace=ws, project=proj, name="target", state=seqs["Todo"])
    return u, ws, proj, issue, seqs, ident


def _scoped(ws):
    # BIP-38: values are explicit project-UUID lists, never workspace slugs.
    from plane.db.models import Project

    ids = [str(pid) for pid in Project.objects.filter(workspace=ws).values_list("id", flat=True)]
    return override_settings(FORGEJO_BRIDGE_REPO_MAP=json.dumps({REPO: ids}))


def _push(ident, seq, before="b" * 40, after="c" * 40, ref="refs/heads/main", repo_id=REPO_ID):
    # a real Forgejo push envelope shape: ref/before/after + repository.id
    return {
        "repository": {"full_name": REPO, "id": repo_id},
        "ref": ref, "before": before, "after": after,
        "commits": [{"id": after, "message": f"fix widget\n\nrefs {ident}-{seq}"}],
    }


def _post(event, payload, delivery_id=None):
    body = json.dumps(payload).encode()
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return Client().post(
        URL, data=body, content_type="application/json",
        HTTP_X_FORGEJO_EVENT=event, HTTP_X_FORGEJO_SIGNATURE=sig,
        HTTP_X_FORGEJO_DELIVERY=delivery_id or str(uuid_lib.uuid4()),
    )


# ---------------------------------------------------------------- constructor
def test_push_key_is_deterministic_and_sha_stable():
    a = skey.push_key("forgejo", 42, "refs/heads/main", "b" * 40, "c" * 40)
    b = skey.push_key("forgejo", 42, "refs/heads/main", "b" * 40, "c" * 40)
    assert a == b and skey.key_hash(a) == skey.key_hash(b)


def test_distinct_after_sha_gives_distinct_key():
    a = skey.push_key("forgejo", 42, "refs/heads/main", "b" * 40, "c" * 40)
    b = skey.push_key("forgejo", 42, "refs/heads/main", "b" * 40, "d" * 40)
    assert a != b and skey.key_hash(a) != skey.key_hash(b)


def test_separator_injection_cannot_alias_two_events():
    # a component carrying the reserved separator would let two events collide;
    # refused rather than silently aliased.
    with pytest.raises(ValueError):
        skey.push_key("forgejo", 42, "refs/heads/main\x1fevil", "b" * 40, "c" * 40)


def test_merged_pr_key_distinct_on_merge_sha():
    a = skey.merged_pr_key("github", 7, 5, "a" * 40)
    b = skey.merged_pr_key("github", 7, 5, "z" * 40)
    assert a != b


# ------------------------------------------------------ cross-transport (req c)
def test_key_computes_identically_from_webhook_and_poll_primitives():
    """The property the column exists for: a webhook payload and a poll
    observation of the SAME event feed the SAME constructor and yield the SAME
    key. The webhook path extracts via _semantic_key; a poll observation (a
    dict the GitHub API would yield) carries the same ref/before/after/repo_id
    and calls the constructor directly."""
    payload = _push("BIP", 7)
    webhook_key = _semantic_key("push", payload, forges.ForgejoForge)
    # what a poll observation carries, extracted from GitHub API responses:
    poll_key = skey.push_key("forgejo", REPO_ID, "refs/heads/main", "b" * 40, "c" * 40)
    assert webhook_key == poll_key
    assert webhook_key is not None


def test_unmerged_pr_and_missing_repo_id_have_no_key():
    unmerged = {"action": "closed", "repository": {"full_name": REPO, "id": REPO_ID},
                "pull_request": {"merged": False, "number": 5, "title": "x", "body": ""}}
    assert _semantic_key("pull_request", unmerged, forges.ForgejoForge) is None
    no_id = {"repository": {"full_name": REPO}, "ref": "r", "before": "b", "after": "c", "commits": []}
    assert _semantic_key("push", no_id, forges.ForgejoForge) is None


# ------------------------------------------------------- handler dedup (req c)
@pytest.mark.django_db
def test_positive_two_observations_one_outcome():
    """Same real event, two DIFFERENT delivery ids -> ONE outcome, the second
    reported duplicate, one processed row for the semantic key. (Deployed
    webhook path, real envelope shape.)

    BIP-67 conversion: the outcome this test counts used to be a state move and
    an activity row. Under the write boundary a push writes nothing, so the
    countable outcome is now the recorded REFUSAL — one entry, once.

    That is a strictly better probe than the activity row was, and worth saying
    why rather than treating it as a downgrade: this test has only ever been
    about EXECUTING ONCE, and an activity row is one particular side effect of
    executing. Counting the refusal counts the execution itself, which is the
    property the semantic key exists to guarantee. It also keeps the test
    meaningful when the boundary's facts land and the move comes back."""
    u, ws, proj, issue, seqs, ident = _fixture()
    payload = _push(ident, issue.sequence_id)
    d1 = str(uuid_lib.uuid4()); d2 = str(uuid_lib.uuid4())
    with _scoped(ws):
        r1 = _post("push", payload, delivery_id=d1)
        r2 = _post("push", payload, delivery_id=d2)  # distinct id, same event
    assert r1.status_code == 200
    assert r2.status_code == 200 and r2.json().get("duplicate") is True
    issue.refresh_from_db()
    assert issue.state.name == "Todo", "the write boundary leaves the ticket where it was"
    canonical = _semantic_key("push", payload, forges.ForgejoForge)
    h = skey.key_hash(canonical)
    # execution coalesced to ONE outcome: one hash-holder, one refusal, and
    # still no board write anywhere for either observation.
    holders = ForgejoDelivery.objects.filter(semantic_key_hash=h, status="processed")
    assert holders.count() == 1
    refusals = ((holders.get().result or {}).get("ignored") or {}).get("unverified") or []
    assert [e["ticket"] for e in refusals] == [f"{ident}-{issue.sequence_id}"]
    assert IssueActivity.objects.filter(issue=issue, comment__contains="git bridge").count() == 0
    # BOTH observations DURABLY survive (M3 audit — Morrow RC 3343): both
    # delivery ids remain queryable; the second is a coalesced non-holder row.
    assert set(ForgejoDelivery.objects.values_list("delivery_id", flat=True)) == {d1, d2}
    second = ForgejoDelivery.objects.get(delivery_id=d2)
    assert second.semantic_key_hash is None and second.semantic_key == canonical
    assert second.result.get("coalesced_to") == d1


@pytest.mark.django_db
def test_negative_two_distinct_events_do_not_collide():
    u, ws, proj, issue, seqs, ident = _fixture()
    p1 = _push(ident, issue.sequence_id, after="c" * 40)
    p2 = _push(ident, issue.sequence_id, before="c" * 40, after="e" * 40)  # a later, distinct push
    with _scoped(ws):
        r1 = _post("push", p1)
        r2 = _post("push", p2)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json().get("duplicate") is not True
    k1 = skey.key_hash(_semantic_key("push", p1, forges.ForgejoForge))
    k2 = skey.key_hash(_semantic_key("push", p2, forges.ForgejoForge))
    assert k1 != k2
    assert ForgejoDelivery.objects.filter(semantic_key_hash__in=[k1, k2]).count() == 2


@pytest.mark.django_db
def test_backfilled_rows_carry_a_semantic_key():
    """A processed delivery gets its semantic_key persisted (so a later poll of
    the same event dedups against it)."""
    u, ws, proj, issue, seqs, ident = _fixture()
    payload = _push(ident, issue.sequence_id)
    with _scoped(ws):
        _post("push", payload)
    row = ForgejoDelivery.objects.get(semantic_key_hash=skey.key_hash(_semantic_key("push", payload, forges.ForgejoForge)))
    assert row.semantic_key and row.semantic_key_hash
    assert row.semantic_key == _semantic_key("push", payload, forges.ForgejoForge)
