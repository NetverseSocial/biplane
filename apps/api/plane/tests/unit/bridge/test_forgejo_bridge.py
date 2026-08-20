# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Git-bridge tests, to the PR-#9 bar: the violating cases are the point.

RC 3070 coverage on top of 3064/3066/3068/3069: production wiring witnesses
(celery task registry + settings actually loaded — killed by removing the
import/setting line, NOT masked by direct imports); atomic claim/lease with
stale-processor and restart pins; delivery-id binding (409 on content
mismatch, required header); target-state GROUP checks; truncated pushes
deferred off the request path with a fail-closed range resolver (duplicate/
non-progressing/malformed/incomplete pages all stay pending); multi-ref
retries accumulate the complete result."""

import hashlib
import hmac
import json
import logging as logging_mod
import uuid as uuid_lib
from unittest import mock

import pytest
from django.test import Client, override_settings
from django.utils import timezone as dj_timezone

from plane.bgtasks.forgejo_bridge_task import reconcile_forgejo_deliveries
from plane.bridge import forges
from plane.bridge import write_boundary
from plane.bridge.forgejo_bridge import _collect_refs
from plane.bridge import grammar
from plane.bridge import grammar as grammar_mod
from plane.bridge.forgejo_bridge import claim_delivery, process_delivery
from plane.db.models import (
    ForgejoDelivery,
    Issue,
    IssueActivity,
    Project,
    State,
    User,
    Workspace,
)

URL = "/api/public/git-bridge/forgejo/"
SECRET = "test-bridge-secret"
REPO = "acme/x"


@pytest.fixture(autouse=True)
def _bridge_secret(settings):
    settings.FORGEJO_WEBHOOK_SECRET = SECRET
    settings.FORGEJO_INSTANCE_ID = "forgejo"
    settings.GITHUB_INSTANCE_ID = "github"
    settings.GITLAB_INSTANCE_ID = "gitlab"


def _fixture(identifier=None):
    u = User.objects.create(email=f"gb-{uuid_lib.uuid4().hex[:8]}@example.com", username=uuid_lib.uuid4().hex[:12])
    ws = Workspace.objects.create(slug=f"g{uuid_lib.uuid4().hex[:10]}", name="G", owner=u)
    ident = identifier or ("GB" + uuid_lib.uuid4().hex[:3].upper())
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


def _recognised(response) -> set:
    """The tickets this delivery RECOGNISED, from the durable refusal record.

    THE WRITE BOUNDARY MADE BOARD STATE USELESS AS A PROBE (BIP-67). Every
    bridge write is refused, so "the ticket did not move" and moved == [] are
    now true for every reason at once — INCLUDING "recognition is broken". A
    test whose only observable is that absence keeps passing with the thing it
    tests deleted.

    Recognition still discriminates: a directive that IS recognised leaves an
    ``ignored.unverified`` entry naming its ticket, because the boundary
    refused a candidate it understood. One that is masked, quoted or fenced
    leaves nothing at all. So this is the probe those tests always wanted, and
    did-it-move only ever stood in for it.

    Use it in PAIRS inside a single test — assert a visible control IS here and
    the masked subject is NOT. Asserting only the absence MOVES the vacuum
    rather than closing it (Aria).
    """
    ignored = (response.json() or {}).get("ignored") or {}
    return {entry["ticket"] for entry in ignored.get("unverified") or []}


def _proposed(response) -> dict:
    """{ticket: "complete" | "advance"} — the CLASS each directive proposed.

    `_recognised` answers WHETHER a directive was seen. It cannot answer WHICH
    KIND, and a whole class of tests here is about exactly that: Closes versus
    Refs, the weaker-class-wins rule, a complete keyword on a PUSH downgrading
    to advance. Converting those to a bare "is it recognised" would throw away
    the distinction they exist to test — the observable has to carry the class.

    It does, and not by accident: `_advance` branches on the class to choose
    WHICH refusal to produce, so a completion refusal and an advance refusal are
    different reason codes. That branch is the class decision, made visible.

    Reading a reason code rather than a target state is also strictly more
    precise than the old probe: `Review` was the target for every advance from
    any prior state, so two different classes could land on it from different
    directions and the assertion could not tell them apart.
    """
    out = {}
    for entry in ((response.json() or {}).get("ignored") or {}).get("unverified") or []:
        reason = entry.get("reason")
        if reason == write_boundary.BINDING_UNAVAILABLE:
            out[entry["ticket"]] = "complete"
        elif reason == write_boundary.ADVANCE_NOT_AUTHORISED:
            out[entry["ticket"]] = "advance"
        else:
            out[entry["ticket"]] = reason
    return out


def _recognised_in(result) -> list:
    """Tickets recognised, COUNTED — sorted, duplicates preserved.

    A set cannot tell one refusal from two for the same ticket, and those are
    different claims. Takes a result dict, or `response.json()`.
    """
    ignored = (result or {}).get("ignored") or {}
    return sorted(entry["ticket"] for entry in ignored.get("unverified") or [])


def _proposed_in(result) -> list:
    """[(ticket, "complete" | "advance" | reason)] — sorted, counted."""
    out = []
    for entry in ((result or {}).get("ignored") or {}).get("unverified") or []:
        reason = entry.get("reason")
        if reason == write_boundary.BINDING_UNAVAILABLE:
            out.append((entry["ticket"], "complete"))
        elif reason == write_boundary.ADVANCE_NOT_AUTHORISED:
            out.append((entry["ticket"], "advance"))
        else:
            out.append((entry["ticket"], reason))
    return sorted(out)


def _control_issue(ws, proj, seqs, name="control"):
    """A second ticket, so a masking test carries its own liveness control."""
    return Issue.objects.create(workspace=ws, project=proj, name=name, state=seqs["Todo"])


def _project_ids(ws):
    # BIP-38 scope guard: map values are lists of stable project UUIDs. These
    # tests grant the workspace's projects EXPLICITLY — the workspace-slug
    # value is the retired schema (a config defect).
    from plane.db.models import Project

    return [str(pid) for pid in Project.objects.filter(workspace=ws).values_list("id", flat=True)]


def _scoped(ws):
    return override_settings(FORGEJO_BRIDGE_REPO_MAP=json.dumps({REPO: _project_ids(ws)}))


def _post(client, event, payload, delivery_id=None, omit_delivery_header=False):
    body = json.dumps(payload).encode()
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "HTTP_X_FORGEJO_EVENT": event,
        "HTTP_X_FORGEJO_SIGNATURE": sig,
    }
    if not omit_delivery_header:
        headers["HTTP_X_FORGEJO_DELIVERY"] = delivery_id or str(uuid_lib.uuid4())
    return client.post(URL, data=body, content_type="application/json", **headers)


def _push_payload(message):
    return {"repository": {"full_name": REPO}, "commits": [{"id": "a" * 40, "message": message}]}


def _merge_payload(body, title=""):
    """A merged PR whose body is the only directive-bearing source."""
    return {
        "action": "closed",
        "repository": {"full_name": REPO},
        "pull_request": {"merged": True, "number": 5, "title": title, "body": body},
    }


def _merge_payload_body_only(body):
    return {
        "action": "closed",
        "repository": {"full_name": REPO},
        "pull_request": {"merged": True, "number": 5, "title": "a title", "body": body},
    }


def _due_now():
    ForgejoDelivery.objects.filter(status="pending").update(
        next_attempt_at=dj_timezone.now() - dj_timezone.timedelta(seconds=1)
    )


def _resp(batch):
    m = mock.Mock(status_code=200)
    m.json.return_value = batch
    return m


def _page_get(pages):
    def g(url, params=None, **kw):
        return _resp(pages[min((params or {}).get("page", 1) - 1, len(pages) - 1)])
    return g


def _sha(n, fill="b"):
    return f"{n:02d}" + fill * 38


@pytest.mark.django_db
class TestProductionWiring:
    """RC 3070 items 1-2: the suite must witness PRODUCTION wiring, not mask
    it with direct imports and override_settings."""

    def test_reconciler_task_is_registered_with_celery(self):
        from django.conf import settings as dj_settings

        assert "plane.bgtasks.forgejo_bridge_task" in dj_settings.CELERY_IMPORTS, (
            "reconciler module missing from CELERY_IMPORTS: workers would drop the beat task"
        )
        from plane.celery import app

        app.loader.import_default_modules()
        assert "plane.bgtasks.forgejo_bridge_task.reconcile_forgejo_deliveries" in app.tasks
        beat_tasks = {entry["task"] for entry in app.conf.beat_schedule.values()}
        assert "plane.bgtasks.forgejo_bridge_task.reconcile_forgejo_deliveries" in beat_tasks

    def test_bridge_settings_are_loaded_from_env(self):
        # hasattr on real Django settings — reds if the common.py line is
        # removed, regardless of what override_settings does elsewhere.
        from django.conf import settings as dj_settings

        for name in (
            "FORGEJO_WEBHOOK_SECRET",
            "FORGEJO_BRIDGE_REPO_MAP",
            "FORGEJO_BASE_URL",
            "FORGEJO_BRIDGE_API_TOKEN",
        ):
            assert hasattr(dj_settings, name), f"{name} is not loaded into Django settings"


@pytest.mark.django_db
class TestFailClosed:
    def test_no_secret_configured_rejects_everything(self):
        _fixture()
        with override_settings(FORGEJO_WEBHOOK_SECRET=None):
            r = _post(Client(), "push", _push_payload("x"))
        assert r.status_code == 403
        assert ForgejoDelivery.objects.count() == 0

    def test_short_secret_rejects_everything(self):
        _fixture()
        with override_settings(FORGEJO_WEBHOOK_SECRET="short"):
            body = json.dumps(_push_payload("x")).encode()
            sig = hmac.new(b"short", body, hashlib.sha256).hexdigest()
            r = Client().post(
                URL, data=body, content_type="application/json",
                HTTP_X_FORGEJO_EVENT="push", HTTP_X_FORGEJO_SIGNATURE=sig,
            )
        assert r.status_code == 403

    def test_bad_signature_rejected(self):
        _fixture()
        r = Client().post(
            URL, data=b"{}", content_type="application/json",
            HTTP_X_FORGEJO_EVENT="push", HTTP_X_FORGEJO_SIGNATURE="0" * 64,
        )
        assert r.status_code == 403
        assert ForgejoDelivery.objects.count() == 0

    def test_unmapped_repo_is_inert_200(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        with override_settings(FORGEJO_BRIDGE_REPO_MAP=json.dumps({"acme/other": _project_ids(ws)})):
            r = _post(Client(), "push", _push_payload(f"refs {ident}-{issue.sequence_id}"))
        assert r.status_code == 200 and r.json()["moved"] == []
        # THE DISCRIMINATOR IS THE RECORDED REASON, NOT THE STATE. With every
        # write refused, moved == [] is equally true for an unmapped repo and
        # for a mapped one whose directive the boundary declined; only this
        # separates them, and without it the test passes with the scope lookup
        # deleted.
        assert (r.json().get("ignored") or {}).get("unscoped_repo") == REPO
        issue.refresh_from_db()
        assert issue.state.name == "Todo"
        assert ForgejoDelivery.objects.get().status == "processed"

    def test_explicit_empty_map_object_is_valid_and_inert(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        with override_settings(FORGEJO_BRIDGE_REPO_MAP="{}"):
            r = _post(Client(), "push", _push_payload(f"refs {ident}-{issue.sequence_id}"))
        assert r.status_code == 200 and r.json()["moved"] == []
        # An explicit empty map scopes NOTHING, so the repo is unmapped and the
        # delivery says so. Asserting only moved == [] would pass even if "{}"
        # were treated as "everything".
        assert (r.json().get("ignored") or {}).get("unscoped_repo") == REPO


@pytest.mark.django_db
class TestMalformedDeliveries:
    def test_all_malformed_shapes_are_exact_400_nothing_stored(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        t15 = [{"id": _sha(n), "message": "noise"} for n in range(15)]
        cases = [
            ("push", {}),
            ("push", {"commits": []}),
            ("push", {"repository": {"full_name": REPO}}),
            ("push", {"repository": {"full_name": ""}, "commits": []}),
            ("push", {"repository": "not-a-dict", "commits": []}),
            ("push", {"repository": {"full_name": 42}, "commits": []}),
            ("push", {"repository": {"full_name": REPO}, "commits": "not-a-list"}),
            ("push", {"repository": {"full_name": REPO}, "commits": ["not-a-dict"]}),
            ("push", {"repository": {"full_name": REPO}, "commits": [{"id": "a"}]}),
            ("push", {"repository": {"full_name": REPO}, "commits": [{"id": 7, "message": 9}]}),
            ("push", {"repository": {"full_name": REPO}, "commits": [], "total_commits": "x"}),
            # RC 3070: bool is not an int; truncated pushes need canonical anchors
            ("push", {"repository": {"full_name": REPO}, "commits": [], "total_commits": True}),
            ("push", {"repository": {"full_name": REPO}, "commits": t15, "total_commits": 16}),
            ("push", {"repository": {"full_name": REPO}, "commits": t15, "total_commits": 16,
                      "before": "zz", "after": "b" * 40}),
            ("push", {"repository": {"full_name": REPO}, "commits": t15, "total_commits": 16,
                      "before": "f" * 40}),
            ("pull_request", {"repository": {"full_name": REPO}}),
            ("pull_request", {"action": "closed", "repository": {"full_name": REPO}}),
            ("pull_request", {"action": "closed", "repository": {"full_name": REPO}, "pull_request": "nope"}),
            ("pull_request", {"action": "closed", "repository": {"full_name": REPO},
                              "pull_request": {"number": 5, "title": "t", "body": ""}}),
            ("pull_request", {"action": "closed", "repository": {"full_name": REPO},
                              "pull_request": {"merged": "yes", "number": 5, "title": "t", "body": ""}}),
            ("pull_request", {"action": "closed", "repository": {"full_name": REPO},
                              "pull_request": {"merged": True, "number": "x", "title": 5, "body": None}}),
            ("push", [1, 2, 3]),
        ]
        for event, payload in cases:
            r = _post(Client(), event, payload)
            assert r.status_code == 400, (event, payload, r.status_code)
        issue.refresh_from_db()
        assert issue.state.name == "Todo"
        assert IssueActivity.objects.filter(issue=issue).count() == 0
        assert ForgejoDelivery.objects.count() == 0

    def test_invalid_utf8_body_is_400(self):
        _fixture()
        body = b'{"repository": {"full_name": "\xff\xfe"}}'
        sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        r = Client().post(
            URL, data=body, content_type="application/json",
            HTTP_X_FORGEJO_EVENT="push", HTTP_X_FORGEJO_SIGNATURE=sig,
            HTTP_X_FORGEJO_DELIVERY=str(uuid_lib.uuid4()),
        )
        assert r.status_code == 400
        assert ForgejoDelivery.objects.count() == 0

    def test_non_uuid_delivery_ids_are_400_nothing_stored(self):
        # RC 3071: Forgejo emits canonical UUIDs; short invented identities
        # and oversized values (would be a DataError at the column) owe 400.
        u, ws, proj, issue, seqs, ident = _fixture()
        payload = _push_payload(f"refs {ident}-{issue.sequence_id}")
        for bad in ("short", uuid_lib.uuid4().hex, "z" * 36, "a" * 129):
            with _scoped(ws):
                r = _post(Client(), "push", payload, delivery_id=bad)
            assert r.status_code == 400, (bad, r.status_code)
        assert ForgejoDelivery.objects.count() == 0
        issue.refresh_from_db()
        assert issue.state.name == "Todo"

    def test_missing_delivery_header_is_400_nothing_stored(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        with _scoped(ws):
            r = _post(Client(), "push", _push_payload(f"refs {ident}-{issue.sequence_id}"),
                      omit_delivery_header=True)
        assert r.status_code == 400
        assert ForgejoDelivery.objects.count() == 0


@pytest.mark.django_db
class TestDeliveryBinding:
    """RC 3070 item 5: the idempotency key is bound to event/repo/body."""

    def test_same_id_different_body_is_409_zero_processing(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        did = str(uuid_lib.uuid4())
        with _scoped(ws):
            r1 = _post(Client(), "push", _push_payload(f"refs {ident}-{issue.sequence_id}"), delivery_id=did)
            r2 = _post(Client(), "push", _push_payload("totally different body"), delivery_id=did)
        assert r1.status_code == 200
        assert r2.status_code == 409
        assert ForgejoDelivery.objects.count() == 1
        # "Zero processing" was an activity row, and no delivery writes one now,
        # so it held for every reason at once. The pair: the first body WAS
        # processed and recognised its ticket; the rejected duplicate was not.
        ref = f"{ident}-{issue.sequence_id}"
        assert _recognised_in(r1.json()) == [ref], "the first body was not processed"
        assert _recognised_in(r2.json()) == [], "the rejected duplicate processed something"

    def test_duplicate_while_processing_is_202(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        did = str(uuid_lib.uuid4())
        payload = _push_payload(f"refs {ident}-{issue.sequence_id}")
        body = json.dumps(payload).encode()
        digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        ForgejoDelivery.objects.create(
            delivery_id=did, event="push", payload=payload, repository=REPO,
            body_digest=digest, status="processing", lease_token="held",
            lease_expires_at=dj_timezone.now() + dj_timezone.timedelta(seconds=60),
        )
        with _scoped(ws):
            r = _post(Client(), "push", payload, delivery_id=did)
        assert r.status_code == 202 and r.json()["pending"] is True
        issue.refresh_from_db()
        assert issue.state.name == "Todo"  # the in-flight owner does the work

    def test_duplicate_after_processed_is_200_with_stored_result(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        ref = f"{ident}-{issue.sequence_id}"
        did = str(uuid_lib.uuid4())
        with _scoped(ws):
            r1 = _post(Client(), "push", _push_payload(f"refs {ref}"), delivery_id=did)
            r2 = _post(Client(), "push", _push_payload(f"refs {ref}"), delivery_id=did)
        # The subject is that the duplicate REPLAYS the stored outcome rather
        # than recomputing one. `moved` is [] for everything now.
        assert r1.status_code == 200 and _recognised_in(r1.json()) == [ref]
        assert r2.status_code == 200 and r2.json().get("duplicate") is True
        assert _recognised_in(r2.json()) == _recognised_in(r1.json()), "the duplicate did not replay"
        assert ForgejoDelivery.objects.count() == 1


@pytest.mark.django_db
class TestClaimLease:
    """RC 3070 item 4: atomic claim, expiring lease, owner-conditioned writes."""

    def _row(self, payload):
        body = json.dumps(payload).encode()
        return ForgejoDelivery.objects.create(
            delivery_id=str(uuid_lib.uuid4()), event="push", payload=payload, repository=REPO,
            body_digest=hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(),
        )

    def test_only_one_worker_wins_the_claim(self):
        row = self._row(_push_payload("x"))
        t1 = claim_delivery(row)
        t2 = claim_delivery(row)
        assert t1 is not None and t2 is None

    def test_unexpired_lease_is_not_reclaimable(self):
        row = self._row(_push_payload("x"))
        assert claim_delivery(row) is not None
        row.refresh_from_db()
        assert row.status == "processing" and row.lease_expires_at > dj_timezone.now()
        assert claim_delivery(row) is None

    def test_crash_after_claim_recovers_via_expired_lease(self):
        # restart witness: worker claimed then died; lease expires; a fresh
        # reconciler pass (pure DB read) recovers the delivery.
        u, ws, proj, issue, seqs, ident = _fixture()
        ref = f"{ident}-{issue.sequence_id}"
        with _scoped(ws):
            row = self._row(_push_payload(f"refs {ref}"))
            assert claim_delivery(row) is not None  # worker "crashes" here
            assert reconcile_forgejo_deliveries() == 0  # lease still live: untouchable
            ForgejoDelivery.objects.filter(pk=row.pk).update(
                lease_expires_at=dj_timezone.now() - dj_timezone.timedelta(seconds=1)
            )
            assert reconcile_forgejo_deliveries() == 1
        row.refresh_from_db()
        assert row.status == "processed"
        # The recovered pass's own record proves it ran; reconcile() == 0 above
        # is the negative arm and is untouched.
        assert _recognised_in(row.result) == [ref], "the recovered pass did not process"

    def test_reclaimed_lease_processor_makes_zero_side_effects(self):
        # THE RC 3071 VIOLATING DIRECTION: token A expires, token B reclaims,
        # A resumes against a STILL-TODO issue — A must produce zero
        # state/activity/result changes.
        u, ws, proj, issue, seqs, ident = _fixture()
        ref = f"{ident}-{issue.sequence_id}"
        with _scoped(ws):
            row = self._row(_push_payload(f"refs {ref}"))
            token_a = claim_delivery(row)
            assert token_a
            ForgejoDelivery.objects.filter(pk=row.pk).update(
                lease_expires_at=dj_timezone.now() - dj_timezone.timedelta(seconds=1)
            )
            token_b = claim_delivery(row)
            assert token_b and token_b != token_a
            from plane.bridge.forgejo_bridge import _LeaseLostError

            row_a = ForgejoDelivery.objects.get(pk=row.pk)
            with pytest.raises(_LeaseLostError):
                process_delivery(row_a, token_a)
            issue.refresh_from_db()
            assert issue.state.name == "Todo", "stale processor moved the issue"
            row.refresh_from_db()
            assert row.status == "processing" and row.lease_token == token_b
            assert _recognised_in(row.result) == [], "the stale processor recorded an outcome"
            # B (the rightful owner) completes normally
            result = process_delivery(ForgejoDelivery.objects.get(pk=row.pk), token_b)
        # POSITIVE ARM: without it the absence above is free.
        assert _recognised_in(result) == [ref], "the rightful owner must still process"

class TestTheBoundarySentenceMustBeTrueOfItsEvent:
    """`decide_advance` renders a different sentence per event, so `event` is
    mandatory and closed (Morrow).

    It used to DEFAULT to "push" and treat every unrecognised value as a push.
    A caller that forgot the argument — or passed a wire event name like
    "pull_request" instead of the boundary's "merged_pr" — got a PUBLIC COMMENT
    telling the author "a push determines nothing" about an event that was not a
    push. A confidently false sentence on the exact axis this boundary exists to
    be truthful about.

    No database: these are pure rendering decisions.
    """

    def test_omitting_the_event_is_a_TypeError(self):
        from plane.bridge import write_boundary as wb

        with pytest.raises(TypeError):
            wb.decide_advance(ticket="BIP-1", context="commit abc", repo=REPO)

    def test_an_unknown_event_is_loud_not_treated_as_a_push(self):
        from plane.bridge import write_boundary as wb

        # "pull_request" is the ENDPOINT's name and is NOT this boundary's.
        # Silently rendering it as a push is the original defect.
        with pytest.raises(ValueError):
            wb.decide_advance(
                ticket="BIP-1", context="merged PR #1", repo=REPO, event="pull_request"
            )

    @pytest.mark.parametrize(
        "event,must_say,must_not_say",
        [
            ("push", "A push determines nothing", "did merge"),
            ("merged_pr", "did merge", "A push determines nothing"),
        ],
    )
    def test_each_valid_event_renders_its_truthful_branch(self, event, must_say, must_not_say):
        from plane.bridge import write_boundary as wb

        r = wb.decide_advance(ticket="BIP-1", context="ctx", repo=REPO, event=event)
        assert must_say in r.detail, f"{event} did not render its own sentence: {r.detail}"
        assert must_not_say not in r.detail, f"{event} rendered the other event's sentence"
        assert r.reason == wb.ADVANCE_NOT_AUTHORISED

    def test_the_boundary_set_is_NOT_the_endpoint_set(self):
        """They differ on purpose; substituting one for the other reintroduces
        the false sentence."""
        from plane.bridge import forgejo_bridge as fb
        from plane.bridge import write_boundary as wb

        assert wb.BOUNDARY_EVENTS == ("push", "merged_pr")
        assert wb.BOUNDARY_EVENTS != fb.HANDLED_EVENTS


@pytest.mark.django_db(transaction=True)
class TestPushReductionIsEventWide:
    """A push is ONE event, so its reduction is over every commit in it.

    Reduction used to run per COMMIT (Morrow). Two consequences, and the second
    is the one that bites later: a split-class pair across two commits never
    met, so no conflict was recorded; and a ticket named in two commits was
    yielded TWICE. delivery_result's dedupe hid the second yield — harmless
    while nothing writes, TWO WRITE ATTEMPTS the day completion returns.

    These assert at the COLLECTOR, not through the result, because the result
    is exactly where the duplicate was being hidden.
    """

    def _collect(self, messages):
        from plane.bridge import forgejo_bridge as fb

        payload = {
            "repository": {"full_name": REPO},
            "commits": [{"id": f"{i:040d}", "message": m} for i, m in enumerate(messages)],
        }
        near, conf = [], []
        refs = list(fb._collect_refs("push", payload, REPO, near_misses=near, conflicts=conf))
        return refs, conf

    def test_same_class_in_two_commits_yields_exactly_one_candidate(self):
        refs, conf = self._collect(["refs GB-1", "refs GB-1"])
        assert len(refs) == 1, (
            f"one ticket named in two commits produced {len(refs)} candidates; "
            "delivery_result would hide this today and it becomes two write "
            "attempts when completion returns"
        )
        assert conf == [], "same class twice is a repeat, not a conflict"

    def test_split_class_across_two_commits_records_the_conflict(self):
        refs, conf = self._collect(["Closes GB-1", "refs GB-1"])
        assert len(refs) == 1, "split class across commits must still yield one candidate"
        assert conf == ["GB-1"], (
            f"expected exactly one conflict fact, got {conf}. A complete-class "
            "trailer in one commit and an advance-class trailer in another never "
            "met while reduction was per-commit; and `in conf` would not have "
            "noticed the same key recorded twice"
        )

    def test_a_conflict_within_AND_across_commits_is_ONE_fact(self):
        """Conflicts are event-level, so the same ticket is one fact (Morrow).

        Commit A conflicts with itself; commit B then conflicts with A across
        the boundary. That is one ticket demoted once, not two facts — and
        extending the caller list per commit recorded it twice, with
        delivery_result's dedupe hiding it. The same masking boundary removed
        for candidates, in the field beside them.
        """
        refs, conf = self._collect(["Closes GB-1\nrefs GB-1", "Closes GB-1"])
        assert conf == ["GB-1"], f"expected one conflict fact, got {conf}"
        assert len(refs) == 1

    def test_two_internally_conflicting_commits_are_ONE_fact(self):
        refs, conf = self._collect(["Closes GB-1\nrefs GB-1", "Closes GB-1\nrefs GB-1"])
        assert conf == ["GB-1"], f"expected one conflict fact, got {conf}"
        assert len(refs) == 1

    def test_an_event_accepted_at_ingress_but_unhandled_here_RAISES(self):
        """Mandatory closed event.

        This collector handles exactly push and pull_request; review_rejected
        has its own path. It used to fall through on anything else,
        producing zero refs, zero near misses and zero diagnostics — the same
        observable as an event that genuinely named nothing. A handler added at
        the endpoint and forgotten here would have looked like ordinary quiet.
        """
        from plane.bridge import forgejo_bridge as fb

        with pytest.raises(fb._MalformedDeliveryError):
            list(fb._collect_refs("issue_comment", {"repository": {"full_name": REPO}}, REPO))

    def test_the_closed_set_is_named_once_not_copied(self):
        """Two copies of a closed set are one edit from disagreeing, silently."""
        from plane.bridge import forgejo_bridge as fb

        assert fb.HANDLED_EVENTS == ("push", "pull_request", "review_rejected")

    def test_review_rejected_ALSO_raises_in_the_forward_collector(self):
        """The case my first guard missed.

        review_rejected is in HANDLED_EVENTS — it is handled, just not HERE.
        Guarding on that set meant this event fell through silently while the
        unhandled-event test passed on `issue_comment`. Two events, two
        different reasons, one of them uncovered.
        """
        from plane.bridge import forgejo_bridge as fb

        with pytest.raises(fb._MalformedDeliveryError):
            list(fb._collect_refs("review_rejected", {"repository": {"full_name": REPO}}, REPO))

    def test_reduction_happens_BEFORE_the_push_downgrade(self):
        """The ordering is the whole fix.

        A push flattens every class to ADVANCE at the yield. Reduce after that
        and both commits look identical, so no conflict is found — and the test
        above would pass against a broken implementation.
        """
        refs, conf = self._collect(["Closes GB-1", "refs GB-1"])
        assert conf, "reduction ran after the downgrade: nothing left to compare"
        assert all(r[2] == "advance" or r[2] == grammar_mod.ADVANCE for r in refs), (
            "the downgrade must still apply to what is yielded"
        )


@pytest.mark.django_db(transaction=True)
class TestConfigDefects:
    def test_unset_map_is_503(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        with override_settings(FORGEJO_BRIDGE_REPO_MAP=None):
            r = _post(Client(), "push", _push_payload(f"refs {ident}-{issue.sequence_id}"))
        assert r.status_code == 503
        assert ForgejoDelivery.objects.get().status == "pending"

    def test_empty_string_map_is_503(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        with override_settings(FORGEJO_BRIDGE_REPO_MAP=""):
            r = _post(Client(), "push", _push_payload(f"refs {ident}-{issue.sequence_id}"))
        assert r.status_code == 503

    def test_invalid_map_json_is_503(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        with override_settings(FORGEJO_BRIDGE_REPO_MAP="not json at all"):
            r = _post(Client(), "push", _push_payload(f"refs {ident}-{issue.sequence_id}"))
        assert r.status_code == 503

    def test_non_object_map_is_503(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        with override_settings(FORGEJO_BRIDGE_REPO_MAP="[]"):
            r = _post(Client(), "push", _push_payload(f"refs {ident}-{issue.sequence_id}"))
        assert r.status_code == 503

    def test_non_list_scope_value_is_503(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        with override_settings(FORGEJO_BRIDGE_REPO_MAP=json.dumps({REPO: 42})):
            r = _post(Client(), "push", _push_payload(f"refs {ident}-{issue.sequence_id}"))
        assert r.status_code == 503

    def test_missing_project_503_then_config_fix_recovers_via_reconciler(self, caplog):
        u, ws, proj, issue, seqs, ident = _fixture()
        ghost = str(uuid_lib.uuid4())  # well-formed uuid, no such project
        with override_settings(FORGEJO_BRIDGE_REPO_MAP=json.dumps({REPO: [ghost]})), caplog.at_level(
            logging_mod.WARNING, logger="plane.worker"
        ):
            r = _post(Client(), "push", _push_payload(f"refs {ident}-{issue.sequence_id}"))
        assert r.status_code == 503
        assert any("do not exist" in rec.getMessage() for rec in caplog.records)
        _due_now()
        with _scoped(ws):
            assert reconcile_forgejo_deliveries() == 1
        issue.refresh_from_db()
        # The 503 response carries no result; the RECONCILED delivery does.
        row = ForgejoDelivery.objects.get()
        entries = ((row.result or {}).get("ignored") or {}).get("unverified") or []
        assert f"{ident}-{issue.sequence_id}" in {e["ticket"] for e in entries}, (
            "after the config fix the reconciler reached the boundary but "
            "recorded no refusal"
        )
        assert ForgejoDelivery.objects.get().status == "processed"

    def test_missing_target_state_is_loud_503_pending(self, caplog):
        u, ws, proj, issue, seqs, ident = _fixture()
        with mock.patch("plane.db.mixins.soft_delete_related_objects.delay"):
            seqs["Review"].delete()  # incidental soft-delete task publish, not under test (BIP-63)
        with _scoped(ws), caplog.at_level(logging_mod.WARNING, logger="plane.worker"):
            r = _post(Client(), "push", _push_payload(f"refs {ident}-{issue.sequence_id}"))
        # SUBJECT MOVED BY THE BOUNDARY (BIP-67, Morrow cold read). This used to
        # 503 and retry forever on a project with no target state. TARGET
        # CONFIGURATION IS NOW IRRELEVANT — target resolution is DELETED, not
        # deferred, so nothing reads a project's state groups at all — and the
        # delivery must complete and say why rather than wedge. If a write ever
        # returns, its own guard returns with it; there is no dormant one here.
        assert r.status_code == 200
        assert f"{ident}-{issue.sequence_id}" in _recognised(r), (
            "the ref never reached the boundary, so this proves nothing about "
            "target configuration being irrelevant"
        )
        assert ForgejoDelivery.objects.get().status == "processed", (
            "a config defect that writes nothing still wedged the delivery"
        )

@pytest.mark.django_db(transaction=True)
class TestTemplateMatrix:
    """RC 3071 item 2 (John's smart-matching ruling) as it stands after BIP-67.

    That ruling required the bridge to work on every shipped workflow template,
    push resolving to the review-ish started state and merge to the done-ish
    completed one. TARGET RESOLUTION IS DELETED, so no template resolves to
    anything. What these still pin is that the outcome is the SAME on every
    shipped template — recognised, then refused — so no workflow shape can
    make the bridge behave differently."""

    TEMPLATES = {
        "plane-default": (
            [("Backlog", "backlog"), ("Todo", "unstarted"), ("In Progress", "started"),
             ("Done", "completed"), ("Cancelled", "cancelled")],
            "In Progress", "Done"),
        "biplane": (
            [("Backlog", "backlog"), ("Todo", "unstarted"), ("Design", "started"),
             ("Code & TDD", "started"), ("Review", "started"), ("Integration Test", "started"),
             ("Deploy", "completed"), ("Done", "completed"), ("Cancelled", "cancelled")],
            "Review", "Done"),
        "scrum": (
            [("Product Backlog", "backlog"), ("Sprint Backlog", "unstarted"),
             ("In Progress", "started"), ("In Review", "started"), ("Done", "completed"),
             ("Cancelled", "cancelled")],
            "In Review", "Done"),
        "kanban": (
            [("Backlog", "backlog"), ("To Do", "unstarted"), ("In Progress", "started"),
             ("Blocked", "started"), ("Review", "started"), ("Done", "completed"),
             ("Cancelled", "cancelled")],
            "Review", "Done"),
        "bug-triage": (
            [("New", "backlog"), ("Confirmed", "unstarted"), ("In Progress", "started"),
             ("In Review", "started"), ("Resolved", "completed"), ("Closed", "completed"),
             ("Wont Fix", "cancelled")],
            "In Review", "Resolved"),
    }

    def _template_fixture(self, states):
        u = User.objects.create(email=f"tm-{uuid_lib.uuid4().hex[:8]}@example.com", username=uuid_lib.uuid4().hex[:12])
        ws = Workspace.objects.create(slug=f"t{uuid_lib.uuid4().hex[:10]}", name="T", owner=u)
        ident = "TM" + uuid_lib.uuid4().hex[:3].upper()
        proj = Project.objects.create(workspace=ws, name="P", identifier=ident)
        first_unstarted = None
        for i, (name, group) in enumerate(states):
            st = State.objects.create(
                name=name, project=proj, workspace=ws, group=group, sequence=(i + 1) * 100,
                color="#000", default=(i == 0), created_by=u,
            )
            if group == "unstarted" and first_unstarted is None:
                first_unstarted = st
        issue = Issue.objects.create(workspace=ws, project=proj, name="t", state=first_unstarted)
        return ws, issue, ident

    @pytest.mark.parametrize("template", sorted(TEMPLATES))
    def test_push_and_merge_are_refused_alike_on_every_shipped_template(self, template):
        """Split into PROPERTY (end-to-end) and MECHANISM (unit), because the
        boundary made the per-template TARGET unobservable through HTTP: the
        state never changes, and the refusal records the ticket and class but
        not which state would have been chosen. Same shape as the push-downgrade
        conversion, for the same reason.

        End-to-end, what is still true and asserted here: on EVERY shipped
        template the bridge recognises, classifies, and refuses with the right
        class — push as advance, merge-closes as complete. A template whose
        states confused the pipeline would surface as a 503 or a missing
        refusal, not as a silent pass.

        The mechanism — WHICH state each template resolves to — is asserted
        directly against _resolve_target below, where it is visible."""
        states, push_target, merge_target = self.TEMPLATES[template]
        ws, issue, ident = self._template_fixture(states)
        ref = f"{ident}-{issue.sequence_id}"
        with _scoped(ws):
            r = _post(Client(), "push", _push_payload(f"refs {ref}"))
            assert r.status_code == 200, template
            assert _proposed(r) == {ref: "advance"}, (template, "push must refuse as advance")
            r = _post(Client(), "pull_request", _merge_payload(f"closes {ref}"))
            assert r.status_code == 200, template
            assert _proposed(r) == {ref: "complete"}, (template, "merge-closes must refuse as complete")
        issue.refresh_from_db()
        assert issue.completed_at is None, template

    # RETIRED WITH ITS SUBJECT: `_resolve_target` no longer exists. This asserted
    # WHICH state each template resolves to, and there is no resolver to ask.
    # Target resolution returns with the write path and its mechanism test
    # returns with it — pinning a deleted function is not coverage.

    def test_project_with_no_completed_state_is_refused_durably_not_503(self):
        """Sia's conversion (777959d), resurrected as ruled.

        Target resolution is DELETED (not moved below the boundary, as an
        earlier version of this docstring said), so a project missing its
        completed state no longer 503s-and-retries-forever: the delivery is
        refused DURABLY, and the refusal names the ticket like any other.
        Target configuration is irrelevant because nothing reads it.

        Her function-level defect pin is deliberately NOT carried over:
        `_resolve_target` is deleted, so the loud config signal it guarded
        cannot exist at delivery time and RETURNS WITH THE WRITE PATH (Aria's
        ruling). That is a real loss of a loud signal, recorded here rather
        than papered over.
        """
        ws, issue, ident = self._template_fixture(
            [("Backlog", "backlog"), ("Todo", "unstarted"), ("In Progress", "started")]
        )
        ref = f"{ident}-{issue.sequence_id}"
        with _scoped(ws):
            r = _post(Client(), "pull_request", _merge_payload(f"closes {ref}"))
        assert r.status_code == 200
        assert _recognised_in(r.json()) == [ref], "the refusal must NAME the ticket it understood"
        assert _proposed_in(r.json()) == [(ref, "complete")], "merge-closes must refuse as complete"
        issue.refresh_from_db()
        assert issue.state.name == "Todo"
        assert ForgejoDelivery.objects.get().status == "processed", "durable refusal, not a retrying 503"

    def test_project_with_no_started_state_is_refused_durably_not_503(self):
        """Sibling of the conversion above, same reasoning and same ruling."""
        ws, issue, ident = self._template_fixture(
            [("Backlog", "backlog"), ("Todo", "unstarted"), ("Done", "completed")]
        )
        ref = f"{ident}-{issue.sequence_id}"
        with _scoped(ws):
            r = _post(Client(), "push", _push_payload(f"refs {ref}"))
        assert r.status_code == 200
        assert _recognised_in(r.json()) == [ref], "the refusal must NAME the ticket it understood"
        assert _proposed_in(r.json()) == [(ref, "advance")], "push must refuse as advance"
        issue.refresh_from_db()
        assert issue.state.name == "Todo"
        assert ForgejoDelivery.objects.get().status == "processed", "durable refusal, not a retrying 503"


@pytest.mark.django_db
class TestDurableInbox:
    def test_the_activity_write_is_never_attempted_on_the_push_path(self):
        """Converted from test_activity_failure_recovers_internally_without_
        second_post. Its subject — recovery from a failed ACTIVITY write — has
        no failure left to arm: the boundary refuses before any write, so the
        sabotaged create is simply never called. Same shape as the rework
        rollback conversion, on the other write site. The sabotage stays wired
        so a future change that lets the push path write again trips the
        RuntimeError; recovery-from-transient-failure itself is still exercised
        by the truncated-push 503-then-reconcile cases and the two below."""
        u, ws, proj, issue, seqs, ident = _fixture()
        ref = f"{ident}-{issue.sequence_id}"
        sabotaged = mock.Mock(side_effect=RuntimeError("boom"))
        with _scoped(ws):
            # PATCH THE MODEL, NOT THE MODULE. `forgejo_bridge` no longer
            # references IssueActivity — the write path went and took the
            # attribute with it — so the tripwire could not even be ARMED: it
            # raised AttributeError, which reads as a broken test rather than as
            # the thing it is. On the manager it stays armed and gets STRICTLY
            # WIDER: it now trips on a board write added anywhere.
            with mock.patch(
                "plane.db.models.IssueActivity.objects.create", sabotaged
            ):
                r = _post(Client(), "push", _push_payload(f"refs {ref}"))
        assert r.status_code == 200, "a refusal is a decision, not a failure"
        assert sabotaged.call_count == 0, "the push path attempted a board write"
        row = ForgejoDelivery.objects.get()
        assert row.status == "processed" and row.last_error is None
        assert {e["ticket"] for e in (row.result.get("ignored") or {}).get("unverified") or []} == {ref}

    def test_multi_ref_retry_accumulates_complete_result(self):
        # RC 3070: if ref A lands and ref B fails, the retry must report the
        # COMPLETE delivery effect, not just the final attempt's.
        u, ws, proj, issue, seqs, ident = _fixture()
        issue_b = Issue.objects.create(workspace=ws, project=proj, name="second", state=seqs["Todo"])
        ref_a = f"{ident}-{issue.sequence_id}"
        ref_b = f"{ident}-{issue_b.sequence_id}"
        # The SABOTAGE MOVES TO THE BOUNDARY, because there is no board write
        # left to fail. The subject — RC 3070, a retry must report the COMPLETE
        # delivery effect, not the final attempt's — is unchanged; the effect
        # being accumulated is now the refusal per ref, and the rebuild hazard
        # it guards (a retry overwriting the partial result) is the same one.
        real_decide = write_boundary.decide_advance
        first = {"attempt": True}

        def flaky(**kwargs):
            if first["attempt"] and kwargs["ticket"] == ref_b:
                first["attempt"] = False
                raise RuntimeError("boom on B")
            return real_decide(**kwargs)

        with _scoped(ws):
            with mock.patch(
                "plane.bridge.forgejo_bridge.write_boundary.decide_advance", side_effect=flaky
            ):
                # One ticket per line (doc M2): two directives, two lines.
                r = _post(Client(), "push", _push_payload(f"refs {ref_a}\nrefs {ref_b}"))
            assert r.status_code == 503
            row = ForgejoDelivery.objects.get()
            assert row.status == "pending"
            assert {e["ticket"] for e in (row.result.get("ignored") or {}).get("unverified") or []} == {ref_a}, \
                "the first attempt's refusal for A must survive on the pending row"
            _due_now()
            assert reconcile_forgejo_deliveries() == 1
        row.refresh_from_db()
        assert row.status == "processed"
        assert row.result["moved"] == []
        # EXACT MULTIPLICITY, not a set (Morrow). The set-compare here is what
        # masked a real defect: the retry re-processes A before reaching B, and
        # plain append stored [A, A, B] — duplicating A in the durable result
        # and in the reply. A set cannot see a repeat, so it agreed with both
        # the correct and the broken behaviour.
        tickets = [e["ticket"] for e in (row.result.get("ignored") or {}).get("unverified") or []]
        assert sorted(tickets) == sorted([ref_a, ref_b]), (
            f"the retry must accumulate the COMPLETE effect exactly once each, got {tickets}"
        )

    def test_reconciler_respects_backoff(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        with _scoped(ws):
            # Sabotage the BOUNDARY, not the activity write — the activity
            # write is never reached, so sabotaging it produces a clean 200 and
            # nothing pending, and this test would assert backoff on a row that
            # is not retrying.
            with mock.patch(
                "plane.bridge.forgejo_bridge.write_boundary.decide_advance",
                side_effect=RuntimeError("boom"),
            ):
                _post(Client(), "push", _push_payload(f"refs {ident}-{issue.sequence_id}"))
            assert reconcile_forgejo_deliveries() == 0
        row = ForgejoDelivery.objects.get()
        assert row.status == "pending" and row.next_attempt_at > dj_timezone.now()


@pytest.mark.django_db
class TestTruncatedPush:
    """RC 3070 items 3+7: fail-closed range resolution, OFF the request path."""

    CREDS = dict(FORGEJO_BASE_URL="http://forgejo.test", FORGEJO_BRIDGE_API_TOKEN="t" * 20)

    def _payload(self, before=None):
        return {
            "repository": {"full_name": REPO},
            "commits": [{"id": _sha(n), "message": f"noise {n}"} for n in range(15)],
            "total_commits": 16,
            "before": before or "f" * 40,
            "after": _sha(0),
        }

    def _entries(self, shas_msgs):
        return [{"sha": s, "commit": {"message": m}} for s, m in shas_msgs]

    def test_truncated_with_creds_defers_202_api_never_called_inline(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        with _scoped(ws), override_settings(**self.CREDS), mock.patch(
            "plane.bridge.forgejo_bridge.http_requests.get"
        ) as g:
            r = _post(Client(), "push", self._payload())
        assert r.status_code == 202 and r.json()["deferred"] is True
        assert not g.called, "range resolution must never run on the request path"
        assert ForgejoDelivery.objects.get().status == "pending"

    def test_deferred_resolution_completes_at_before_boundary(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        ref = f"{ident}-{issue.sequence_id}"
        before = "f" * 40
        page1 = self._entries([(_sha(n), f"noise {n}") for n in range(10)])
        page2 = self._entries(
            [(_sha(15, "c"), f"the omitted one\n\nrefs {ref}")] + [(before, "boundary commit")]
        )
        with _scoped(ws), override_settings(**self.CREDS):
            r = _post(Client(), "push", self._payload(before=before))
            assert r.status_code == 202
            with mock.patch(
                "plane.bridge.forgejo_bridge.http_requests.get", side_effect=_page_get([page1, page2])
            ):
                assert reconcile_forgejo_deliveries() == 1
        # The SUBJECT is range resolution: the directive lived in an OMITTED
        # commit, and finding it proves the pages were walked to the boundary.
        # The refusal naming the ref is that proof — a directive that was never
        # fetched cannot be refused.
        row = ForgejoDelivery.objects.get()
        assert row.result["moved"] == []
        assert _recognised_in(row.result) == [ref]

    def test_deferred_resolution_completes_by_count_on_branch_create(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        ref = f"{ident}-{issue.sequence_id}"
        page1 = self._entries([(_sha(n), f"noise {n}") for n in range(10)])
        page2 = self._entries(
            [(_sha(n), f"noise {n}") for n in range(10, 15)] + [(_sha(15, "c"), f"refs {ref}")]
        )
        with _scoped(ws), override_settings(**self.CREDS):
            r = _post(Client(), "push", self._payload(before="0" * 40))
            assert r.status_code == 202
            with mock.patch(
                "plane.bridge.forgejo_bridge.http_requests.get", side_effect=_page_get([page1, page2])
            ):
                assert reconcile_forgejo_deliveries() == 1
        row = ForgejoDelivery.objects.get()
        assert {e["ticket"] for e in (row.result.get("ignored") or {}).get("unverified") or []} == {ref}, \
            "the ref in the count-terminated final page was not resolved"

    def _assert_stays_pending(self, ws, pages=None, get_mock=None, max_pages=None):
        r = _post(Client(), "push", self._payload())
        assert r.status_code == 202
        patches = [mock.patch(
            "plane.bridge.forgejo_bridge.http_requests.get",
            side_effect=(get_mock or _page_get(pages)),
        )]
        if max_pages is not None:
            patches.append(mock.patch("plane.bridge.forgejo_bridge.FETCH_MAX_PAGES", max_pages))
        with patches[0]:
            if max_pages is not None:
                with patches[1]:
                    assert reconcile_forgejo_deliveries() == 0
            else:
                assert reconcile_forgejo_deliveries() == 0
        row = ForgejoDelivery.objects.get()
        assert row.status == "pending" and row.last_error

    def test_repeated_page_stays_pending(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        page = self._entries([(_sha(n), f"noise {n}") for n in range(5)])
        with _scoped(ws), override_settings(**self.CREDS):
            self._assert_stays_pending(ws, pages=[page])  # every page identical
        issue.refresh_from_db()
        assert issue.state.name == "Todo"

    def test_exhausted_history_stays_pending(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        page1 = self._entries([(_sha(n), f"noise {n}") for n in range(5)])
        with _scoped(ws), override_settings(**self.CREDS):
            self._assert_stays_pending(ws, pages=[page1, []])  # ends at 5/16, no boundary
        issue.refresh_from_db()
        assert issue.state.name == "Todo"

    def test_malformed_json_stays_pending(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        bad = mock.Mock(status_code=200)
        bad.json.side_effect = ValueError("nope")
        with _scoped(ws), override_settings(**self.CREDS):
            self._assert_stays_pending(ws, get_mock=lambda *a, **k: bad)

    def test_malformed_entry_stays_pending(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        with _scoped(ws), override_settings(**self.CREDS):
            self._assert_stays_pending(ws, pages=[[{"sha": 42}]])

    def test_page_cap_stays_pending(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        # every page yields ONE new unique commit, never completing 16
        pages = [self._entries([(_sha(50 + n, "d"), f"noise {n}")]) for n in range(4)]
        with _scoped(ws), override_settings(**self.CREDS):
            self._assert_stays_pending(ws, pages=pages, max_pages=2)

    def test_truncated_without_creds_is_503_then_recovers_after_config(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        ref = f"{ident}-{issue.sequence_id}"
        with _scoped(ws):
            r = _post(Client(), "push", self._payload(before="0" * 40))
            assert r.status_code == 503  # config defect surfaces inline (cheap check)
            issue.refresh_from_db()
            assert issue.state.name == "Todo"
            page = self._entries(
                [(_sha(n), f"noise {n}") for n in range(15)] + [(_sha(15, "c"), f"refs {ref}")]
            )
            _due_now()
            with override_settings(**self.CREDS), mock.patch(
                "plane.bridge.forgejo_bridge.http_requests.get", side_effect=_page_get([page])
            ):
                assert reconcile_forgejo_deliveries() == 1
        # RECOVERY means the once-blocked delivery was processed to a decision:
        # the ref in the omitted commit was resolved, recognised, and refused.
        row = ForgejoDelivery.objects.get()
        assert row.status == "processed"
        assert {e["ticket"] for e in (row.result.get("ignored") or {}).get("unverified") or []} == {ref}

    def test_untruncated_push_never_calls_the_api(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        with _scoped(ws), mock.patch("plane.bridge.forgejo_bridge.http_requests.get") as g:
            r = _post(Client(), "push", _push_payload(f"refs {ident}-{issue.sequence_id}"))
        assert r.status_code == 200 and not g.called


@pytest.mark.django_db
class TestTenancy:
    def test_same_identifier_in_other_workspace_never_moves(self):
        u_a, ws_a, proj_a, issue_a, seqs_a, ident = _fixture()
        u_b, ws_b, proj_b, issue_b, seqs_b, _ = _fixture(identifier=ident)
        assert issue_a.sequence_id == issue_b.sequence_id
        with _scoped(ws_a):
            r = _post(Client(), "push", _push_payload(f"refs {ident}-{issue_a.sequence_id}"))
        assert r.status_code == 200
        # The boundary refuses every write, so "b did not move" is true for
        # every reason at once. Recognition still discriminates: the ref in
        # workspace A reached the boundary and the one in B did not, which
        # is the tenancy claim itself.
        recognised = _recognised(r)
        assert f"{ident}-{issue_a.sequence_id}" in recognised
        # COUNTED: A's ticket reached the boundary exactly once and B's never
        # did. Board state reads "Todo" for both now, for every reason at once.
        assert _recognised_in(r.json()) == [f"{ident}-{issue_a.sequence_id}"], (
            "tenancy breach: the delivery crossed workspaces, or A never processed"
        )


@pytest.mark.django_db
class TestReferenceGrammar:
    def test_bare_ids_and_tech_tokens_never_mutate(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        msg = f"switch digests to SHA-256, encode UTF-8, rework AES-256; touches {ident}-{issue.sequence_id}"
        with _scoped(ws):
            r = _post(Client(), "push", _push_payload(msg))
        assert r.status_code == 200 and r.json()["moved"] == []
        issue.refresh_from_db()
        assert issue.state.name == "Todo"

    def test_keyworded_ref_with_tech_noise_selects_only_the_ref(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        with _scoped(ws):
            r = _post(Client(), "push", _push_payload(f"use SHA-256 everywhere, refs {ident}-{issue.sequence_id}"))
        # PREMISE INVERTED BY THE SINGLE ANCHORED POLICY (Aria's ruling). This
        # line is MID-LINE PROSE, so it now selects NOTHING — and it is not a
        # near miss either, because a keyword buried in a sentence is prose
        # rather than a failed attempt at a directive. The subject survives and
        # WIDENS: a tech token never fires, and now neither does a directive
        # someone wrote inside a sentence.
        assert _recognised_in(r.json()) == [], "mid-line prose selected a ticket"
        assert not ((r.json() or {}).get("ignored") or {}).get("near_miss"), (
            "mid-line prose was reported as a failed attempt; it is prose"
        )
        # POSITIVE CONTROL: the same ref as an own-line trailer still selects,
        # so the absence above is the anchoring rule and not a dead recogniser.
        with _scoped(ws):
            r2 = _post(Client(), "push", _push_payload(f"refs {ident}-{issue.sequence_id}"))
        assert _recognised_in(r2.json()) == [f"{ident}-{issue.sequence_id}"], (
            "the anchored trailer stopped selecting: this test is measuring nothing"
        )


@pytest.mark.django_db
class TestTransitions:
    def test_push_on_a_todo_ticket_is_recognised_and_refused(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        with _scoped(ws):
            # FIXTURE MOVED, SUBJECT UNCHANGED: one anchored policy means a
            # keyword inside a sentence is prose. The subject is the CLASS a
            # push proposes, not where the words sit.
            r = _post(Client(), "push", _push_payload(f"fix widget\n\nrefs {ident}-{issue.sequence_id}"))
        assert r.status_code == 200
        assert _proposed_in(r.json()) == [(f"{ident}-{issue.sequence_id}", "advance")]
        issue.refresh_from_db()
        assert issue.state.name == "Todo", "the boundary refuses every write"

    def test_merged_pr_proposes_completion_and_is_refused_without_stamping(self):
        """A complete-class directive in the PR body PROPOSES completion — and
        is refused. Nothing completes: the state is unchanged and `completed_at`
        is never stamped. The name of this test is the behaviour; an earlier
        docstring here still said it completes."""
        u, ws, proj, issue, seqs, ident = _fixture()
        assert issue.completed_at is None
        with _scoped(ws):
            r = _post(Client(), "pull_request", _merge_payload(f"closes {ident}-{issue.sequence_id}"))
        assert r.status_code == 200
        assert _proposed(r) == {f"{ident}-{issue.sequence_id}": "complete"}
        issue.refresh_from_db()
        # THE POINT OF THE TEST SURVIVES INVERTED: completed_at is the field a
        # false Done corrupts irreversibly, and the boundary means it is never
        # stamped at all. Asserting it stays None is the stronger claim.
        assert issue.completed_at is None

    def test_merged_pr_with_refs_proposes_advance_not_completion(self):
        """THE BIP-54 REGRESSION, with assertions that can actually fail.

        My first version asserted only `state != Done` and `completed_at is
        None`, which a NO-OP satisfies — Rowan's mutant skipped every advance
        directive and all three of my regressions stayed green (RC 3537).
        Killing that mutant once needed the exact target state and the moved
        result; both are gone with the write path, so what kills it now is the
        RECOGNITION and the PROPOSED CLASS — the refusal records which ticket
        was named and that it was named with an advance keyword, and a mutant
        that skips advance directives still cannot produce either."""
        u, ws, proj, issue, seqs, ident = _fixture()
        with _scoped(ws):
            r = _post(Client(), "pull_request", _merge_payload(f"refs {ident}-{issue.sequence_id}"))
        assert r.status_code == 200
        # Rowan's RC 3537 mutant skipped every advance directive and three
        # regressions stayed green, because they asserted only what did NOT
        # happen. The class per ticket is what kills that mutant now — a skipped
        # directive produces no entry at all.
        assert _proposed(r) == {f"{ident}-{issue.sequence_id}": "advance"}, \
            "Refs must propose ADVANCE"
        issue.refresh_from_db()
        assert issue.completed_at is None, "Refs must not stamp completed_at"

    def test_merged_pr_with_closes_proposes_completion(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        with _scoped(ws):
            r = _post(Client(), "pull_request", _merge_payload(f"closes {ident}-{issue.sequence_id}"))
        assert r.status_code == 200
        assert _proposed(r) == {f"{ident}-{issue.sequence_id}": "complete"}
        issue.refresh_from_db()
        assert issue.completed_at is None

    def test_a_body_only_directive_still_works(self):
        """Making titles inert must not narrow the admitted body source."""
        u, ws, proj, issue, seqs, ident = _fixture()
        with _scoped(ws):
            r = _post(Client(), "pull_request", _merge_payload(f"closes {ident}-{issue.sequence_id}"))
        assert _proposed(r) == {f"{ident}-{issue.sequence_id}": "complete"}

    def test_a_conventional_commit_prefix_no_longer_carries_a_directive(self):
        """Was test_an_inline_body_directive_still_works: "`feat: closes GB-1`
        inside a body is live behaviour too." INVERTED by the single anchored
        policy — a keyword behind a conventional-commit prefix is mid-line, so
        it is prose. Kept rather than deleted because this exact shape is what
        people type, and its absence should be findable."""
        u, ws, proj, issue, seqs, ident = _fixture()
        ref = f"{ident}-{issue.sequence_id}"
        with _scoped(ws):
            r = _post(Client(), "pull_request",
                      _merge_payload_body_only(f"feat: closes {ref}"))
        assert _proposed_in(r.json()) == [], "a prefixed directive still selected"
        # POSITIVE CONTROL: on its own line the same directive still completes.
        with _scoped(ws):
            r2 = _post(Client(), "pull_request", _merge_payload_body_only(f"closes {ref}"))
        assert _proposed_in(r2.json()) == [(ref, "complete")]

    def test_one_merged_pr_proposes_advance_for_one_and_completion_for_another(self):
        """Class is per DIRECTIVE — an event-typed decision cannot express this."""
        u, ws, proj, issue, seqs, ident = _fixture()
        other = Issue.objects.create(workspace=ws, project=proj, name="t2", state=seqs["Todo"])
        # One event, two directives — now one per line, since a single
        # anchored pass takes one ticket per line (doc M2).
        body = f"refs {ident}-{issue.sequence_id}\ncloses {ident}-{other.sequence_id}"
        with _scoped(ws):
            r = _post(Client(), "pull_request", _merge_payload_body_only(body))
        assert r.status_code == 200
        # ONE event, TWO classes — the whole point, and now visible directly
        # rather than inferred from two different target states.
        assert _proposed_in(r.json()) == sorted([
            (f"{ident}-{issue.sequence_id}", "advance"),
            (f"{ident}-{other.sequence_id}", "complete"),
        ])
        issue.refresh_from_db()
        other.refresh_from_db()
        assert issue.completed_at is None and other.completed_at is None

    @pytest.mark.parametrize("kw,expect_done", [
        ("ref", False), ("refs", False),
        ("close", True), ("closes", True),
        ("fix", True), ("fixes", True),
        ("resolve", True), ("resolves", True),
        # Capitalised and upper — the CONVENTIONAL spellings. A classifier that
        # only knew lowercase would silently demote every one of these to
        # advance, and the lowercase-only suite would stay green (Sia RC 3545).
        ("Closes", True), ("CLOSES", True), ("Fixes", True), ("Refs", False),
    ])
    def test_the_full_keyword_matrix_on_a_merged_pr(self, kw, expect_done):
        """Every keyword the datum owns, singular and plural, both cases.

        Morrow RC 3546: the matcher and the classifier were two owners, so
        deleting fix/fixes from the classifier left `fixes BIP-54` matching and
        silently reclassified — every transition test green. The datum now owns
        both, and this matrix is what makes a deletion visible."""
        u, ws, proj, issue, seqs, ident = _fixture()
        with _scoped(ws):
            r = _post(Client(), "pull_request", _merge_payload(f"{kw} {ident}-{issue.sequence_id}"))
        assert r.status_code == 200
        t = f"{ident}-{issue.sequence_id}"
        assert t in _proposed(r), f"{kw!r} matched nothing"
        assert _proposed(r)[t] == ("complete" if expect_done else "advance"), \
            f"{kw!r} was classified wrongly"
        issue.refresh_from_db()
        assert issue.completed_at is None

    @pytest.mark.parametrize("kw", ["close", "closes", "fix", "fixes", "resolve", "resolves", "Closes"])
    def test_a_complete_keyword_in_a_PUSHED_commit_downgrades_to_advance(self, kw):
        """A push cannot assert completion — the branch may never merge.

        This rule was stated in a comment and pinned by nothing: Sia's mutant
        made the push branch classify by keyword instead of always-advance and
        the whole suite stayed green (RC 3545). `closes` on an unmerged branch
        completing a ticket is exactly the false-Done this ticket exists to
        stop, arriving through the other door."""
        u, ws, proj, issue, seqs, ident = _fixture()
        with _scoped(ws):
            r = _post(Client(), "push", _push_payload(f"{kw} {ident}-{issue.sequence_id}"))
        assert r.status_code == 200
        # REFRAMED, BECAUSE THIS TEST CANNOT SEE ITS OWN SUBJECT ANY MORE.
        # The push loop discards the proposed class — the variable is literally
        # named `_proposed` — and yields ADVANCE unconditionally, BEFORE the
        # write boundary is consulted. So both `closes` and `refs` in a commit
        # message arrive as advance and produce the same refusal reason: the
        # downgrade is invisible from here because by this point there is
        # nothing left to tell apart.
        #
        # What is still true end-to-end, and worth keeping, is the PROPERTY: a
        # push never completes, whatever the keyword. The MECHANISM — that the
        # grammar proposed `complete` and the push path threw it away — is
        # asserted directly against the grammar in
        # test_the_push_downgrade_discards_a_COMPLETE_proposal below, where it
        # is observable (Sable, checked at source).
        assert _proposed(r) == {f"{ident}-{issue.sequence_id}": "advance"}, \
            f"a pushed {kw!r} must never complete"
        issue.refresh_from_db()
        assert issue.completed_at is None, f"a pushed {kw!r} must not complete"

    @pytest.mark.parametrize("kw", ["close", "closes", "fix", "fixes", "resolve", "resolves", "Closes"])
    def test_the_push_downgrade_discards_a_COMPLETE_proposal(self, kw):
        """The MECHANISM the HTTP test above can no longer observe.

        Sia's RC 3545 mutant made the push branch classify by keyword instead of
        always-advance and the whole suite stayed green. That mutant is now even
        harder to catch end-to-end, because the boundary refuses both classes
        identically — so the assertion moves to where the discard is visible.

        Two halves, and the first is what makes the second mean anything: the
        grammar really does PROPOSE complete for these keywords, and the push
        path really does yield ADVANCE anyway. Asserting only the second would
        pass if the grammar had quietly stopped recognising the keyword at all.
        """
        # `nominated_tickets` is deleted with the compatibility consumer;
        # `forward_selection` is the one selection entry, and `source` is
        # mandatory so no caller can silently parse a commit message as a body.
        proposals, _near, _conflicts = grammar.forward_selection(
            f"{kw} GB-1", source="commit_message"
        )
        assert proposals, f"{kw!r} matched nothing in the grammar"
        assert proposals[0][2] == grammar.COMPLETE, \
            f"the grammar must PROPOSE complete for {kw!r}, or the discard tests nothing"

        payload = _push_payload(f"{kw} GB-1")
        yielded = [klass for _t, _ctx, klass in
                   _collect_refs("push", payload, REPO, forges.ForgejoForge)]
        assert yielded == [grammar.ADVANCE], \
            f"a pushed {kw!r} must be downgraded to ADVANCE at the push site"

    def test_same_ticket_with_conflicting_keywords_demotes_to_the_advance_class(self):
        """The weaker class wins. A false completion is worse than an
        under-move: a stale ticket is visibly behind, a completed one is
        invisibly wrong."""
        u, ws, proj, issue, seqs, ident = _fixture()
        ref = f"{ident}-{issue.sequence_id}"
        with _scoped(ws):
            # Both directives on their own lines: the conflict rule is about
            # one TICKET named twice, not about two keywords sharing a line.
            r = _post(Client(), "pull_request", _merge_payload_body_only(f"closes {ref}\nrefs {ref}"))
        assert r.status_code == 200
        assert _proposed_in(r.json()) == [(ref, "advance")], "conflict must demote to advance"
        issue.refresh_from_db()
        assert issue.completed_at is None

    def test_updated_at_is_persisted(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        before = issue.updated_at
        with _scoped(ws):
            r = _post(Client(), "push", _push_payload(f"refs {ident}-{issue.sequence_id}"))
        issue.refresh_from_db()
        # SUBJECT INVERTED, NOT LOST. This pinned that a bridge write persists
        # updated_at. There is no bridge write now, so the current truth is the
        # stronger one: the row is NOT touched at all. Paired with the refusal
        # so it cannot pass by the delivery never arriving.
        assert _proposed(r) == {f"{ident}-{issue.sequence_id}": "advance"}
        assert issue.updated_at == before, "a refused delivery must not touch the row"

    def test_unmerged_close_moves_nothing(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        payload = _merge_payload(f"closes {ident}-{issue.sequence_id}")
        payload["pull_request"]["merged"] = False
        with _scoped(ws):
            r = _post(Client(), "pull_request", payload)
        issue.refresh_from_db()
        assert issue.state.name == "Todo"
        # An UNMERGED close yields no refs at all, so nothing reaches the
        # boundary. Without this the test passes with the merged check removed:
        # a merged close is refused too and also leaves the ticket in Todo, so
        # the two outcomes are indistinguishable by state alone.
        assert not ((r.json().get("ignored") or {}).get("unverified") or [])

    def test_a_push_naming_a_completed_ticket_is_refused_like_any_other(self):
        """The hazard survives its guard (BIP-67).

        This was test_forward_only_push_never_drags_done_back, and the ordinal
        it named is gone. The HAZARD it guarded did not go: a completed ticket
        moved by a stray push is section 1 of the boundary. So it now asserts
        the positive — the push was REFUSED and said so — and that a completed
        ticket gets no special treatment, the same refusal as any other state.
        Asserting only that Done stayed Done would pass with recognition dead.
        """
        u, ws, proj, issue, seqs, ident = _fixture()
        issue.state = seqs["Done"]
        issue.save(update_fields=["state"])
        with _scoped(ws):
            r = _post(Client(), "push", _push_payload(f"late fixup\n\nrefs {ident}-{issue.sequence_id}"))
        assert r.status_code == 200 and r.json()["moved"] == []
        entries = ((r.json() or {}).get("ignored") or {}).get("unverified") or []
        by_ticket = {e["ticket"]: e for e in entries}
        key = f"{ident}-{issue.sequence_id}"
        assert key in by_ticket, "a push naming a completed ticket produced no refusal"
        assert by_ticket[key]["reason"] == "advance-not-authorised", (
            "a completed ticket was special-cased instead of refused like any other"
        )
        issue.refresh_from_db()
        assert issue.state.name == "Done"

    def test_unknown_ref_is_noop_and_200(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        with _scoped(ws):
            r = _post(Client(), "push", _push_payload("refs ZZZZZ-99999 which is nobody"))
        assert r.status_code == 200 and r.json()["moved"] == []
        # NOTHING REACHED THE BOUNDARY, which is the strong claim and the one
        # separating an unknown ticket from a known-and-refused one. A ref the
        # scope cannot resolve is rejected before the write site, so it leaves
        # no unverified entry.
        assert not ((r.json().get("ignored") or {}).get("unverified") or [])

@pytest.mark.django_db(transaction=True)
class TestNoIncidentalActorWhenNothingIsSaid:
    """RC 3588, NARROWED to what is still true (Morrow correction).

    HISTORY, because the reasoning is worth keeping even though its subject is
    gone. The original wording was "a delivery that moves nothing writes
    nothing, not even the bridge User". That was then narrowed, because a
    refusal naming a recipient wrote a Notification and writing one legitimately
    materialised the messenger identity authoring it — so the guarantee had to
    exclude the message case.

    **BOTH OF THOSE ARE NOW DELETED.** There is no Notification write (the
    notification half is cut from this release) and no `_bridge_actor` (it went
    with the board writes). So the narrowing is moot and the ORIGINAL, BROADEST
    form is simply true again:

        **A DELIVERY WRITES NO BOARD ROW AND NO USER, EVER.**

    It is now true by construction rather than by fixture: there is no code path
    that could create either. These tests are kept as the pin — they fail if
    anyone reintroduces an actor materialisation above the refusal, which is the
    regression Aria caught on this branch by asserting user-absence and watching
    it fail. Morrow ruled then that the code moves, not the invariant; the code
    has since moved all the way to absent.
    """

    def test_a_known_project_with_a_missing_sequence_still_refuses(self):
        """Scope A: "a missing or unmatched ticket" is a named ask-case.

        The existing unknown-ref test uses an unknown PROJECT and proves only
        scope rejection. This is the other half — project in scope, sequence
        does not exist — which used to return before the boundary was consulted
        at all, so the directive vanished with no record, no reply and no nudge
        (Morrow cold read).

        There is no `notify` on a refusal at all any more — recipient selection
        was cut with the notification half. The refusal still matters, and how
        it reaches anyone depends entirely on the event: on a MERGED PR the
        bridge can comment where the person who typed the number is present,
        but only with a write token. **On a PUSH there is no pull request to
        comment on**, so the refusal is durable and human-silent. This test
        covers the record, not the telling.
        """
        u, ws, proj, issue, seqs, ident = _fixture()
        missing = issue.sequence_id + 9999
        with _scoped(ws):
            r = _post(Client(), "push", _push_payload(f"refs {ident}-{missing}"))
        assert r.status_code == 200
        entries = ((r.json() or {}).get("ignored") or {}).get("unverified") or []
        by_ticket = {e["ticket"]: e for e in entries}
        assert f"{ident}-{missing}" in by_ticket, (
            "a directive naming a nonexistent ticket in an IN-SCOPE project was "
            "silently dropped: no refusal, so nothing to reply with"
        )
        assert by_ticket[f"{ident}-{missing}"]["reason"] == "ticket-not-found"
        # `notify` is gone from the entries by ruling — write_boundary now says
        # outright that RECIPIENTS ARE NOT HERE. The invariant this test exists
        # for is that a nonexistent ticket still produces a NAMED refusal rather
        # than a silent drop, and that is untouched.

    # RETIRED WITH ITS SUBJECT, and NOT hollowed out. This asserted that a
    # soft-deleted assignee is not notified: `issue.assignees.values_list(...)`
    # applied no `deleted_at` predicate, so someone taken off a ticket kept
    # receiving boundary nudges. `write_boundary` now states outright that
    # RECIPIENTS ARE NOT HERE — there is no `notify` on an entry and no
    # `who_to_ask` in the package — so the leak it guarded has no surface.
    #
    # Ruling 4 was "assert tickets/reasons, drop notify". That is right for the
    # sibling above, whose subject is the refusal. It is wrong here: strip
    # notify from THIS test and what is left is a test named for asking people,
    # asserting nothing about asking anyone. WHEN RECIPIENT SELECTION RETURNS
    # WITH THE WRITE PATH, THE SOFT-DELETE PREDICATE RETURNS WITH IT, and this
    # guard has to come back at the same time.

    def test_removing_the_advance_refusal_STILL_cannot_write(self):
        """Morrow cold read: the refusal must not be the only thing stopping it.

        A gate whose mutation code sits immediately behind `if refusal: return`
        is a mode flag in return-value form — one edit making the decision
        return None reactivates a board write the ruling removed. The advance
        path is now shaped so there IS no write below the branch, so this mutant
        loses the RECORD and still cannot move a ticket.

        This is the constitution's rule as a test: a function that must not
        write is a function without the write.
        """
        u, ws, proj, issue, seqs, ident = _fixture()
        before = issue.state.name
        with _scoped(ws), mock.patch(
            "plane.bridge.write_boundary.decide_advance", return_value=None
        ):
            r = _post(Client(), "push", _push_payload(f"refs {ident}-{issue.sequence_id}"))
        assert r.status_code == 200
        assert r.json()["moved"] == [], "removing the refusal produced a board write"
        issue.refresh_from_db()
        assert issue.state.name == before, "removing the refusal moved the ticket"
        assert IssueActivity.objects.filter(issue=issue).count() == 0

    def test_a_refused_delivery_writes_no_activity_row(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        with _scoped(ws):
            r = _post(Client(), "pull_request", _merge_payload_body_only(f"Closes {ident}-{issue.sequence_id}"))
        assert r.status_code == 200
        assert f"{ident}-{issue.sequence_id}" in _recognised(r), "the ref never reached the boundary"
        assert IssueActivity.objects.filter(issue=issue).count() == 0
        issue.refresh_from_db()
        assert issue.completed_at is None, "a refused completion stamped completed_at"


@pytest.mark.django_db(transaction=True)
class TestQuotedDirectivesAreProse:
    """BIP-54 slice 2: the bridge acts on the directives a reader can SEE.

    Two directions, and the second is the one that bites. Text a reader cannot
    see must not fire — quoted, fenced, in link metadata, in an HTML attribute.
    And text a reader CAN see must still fire, including a directive sharing
    its line with an unrelated code span: the first attempt at this dropped
    those, which is a live directive silently ceasing to work (Morrow RC 3571).

    Every case here is at the HTTP entry point rather than against the helper,
    because the defects RC 3571 found lived in the seam between the helper's
    contract and its caller's — a level the helper's own tests cannot see.

    Inline body and commit-message matching remains deliberate compatibility
    behavior. Pull-request titles are covered separately as inert input.
    """

    def test_the_incident_itself_a_body_quoting_a_directive_moves_nothing(self):
        """PR #69's actual shape: the fix for the false-Done, tripping it."""
        u, ws, proj, issue, seqs, ident = _fixture()
        body = (
            "The target state was chosen by event type, so merging anything "
            f"completed every ticket it referenced. A merged PR carrying only "
            f"`Refs {ident}-{issue.sequence_id}` marked it Done when the work was not done."
        )
        control = _control_issue(ws, proj, seqs)
        body += f"\n\nRefs {ident}-{control.sequence_id}"
        with _scoped(ws):
            r = _post(Client(), "pull_request", _merge_payload_body_only(body))
        assert r.status_code == 200
        recognised = _recognised(r)
        # The control proves recognition is ALIVE, so the subject's absence
        # below means the code span held — not that the recogniser is dead.
        assert f"{ident}-{control.sequence_id}" in recognised, "the visible control was not recognised"
        assert f"{ident}-{issue.sequence_id}" not in recognised, "a directive inside a code span fired"

    @pytest.mark.parametrize("shape,label", [
        ("`refs {t}`", "code span"),
        ("```\nrefs {t}\n```", "fenced block"),
        ("> refs {t}", "blockquote"),
        ("    refs {t}", "indented block"),
        ("- refs {t}", "list item"),
    ])
    def test_a_directive_inside_a_masked_region_does_not_fire(self, shape, label):
        u, ws, proj, issue, seqs, ident = _fixture()
        control = _control_issue(ws, proj, seqs)
        body = (
            "context line\n\n"
            + shape.format(t=f"{ident}-{issue.sequence_id}")
            + f"\n\nRefs {ident}-{control.sequence_id}"
        )
        with _scoped(ws):
            r = _post(Client(), "pull_request", _merge_payload_body_only(body))
        assert r.status_code == 200
        recognised = _recognised(r)
        assert f"{ident}-{control.sequence_id}" in recognised, (
            f"the visible control was not recognised, so this case cannot tell "
            f"whether the {label} held or the recogniser is dead"
        )
        assert f"{ident}-{issue.sequence_id}" not in recognised, f"a directive in a {label} fired"

    def test_POSITIVE_CONTROL_the_same_directive_unquoted_still_fires(self):
        """Without this the class above is satisfiable by a broken fixture.

        Same ticket, same body, same endpoint — only the backticks removed. If
        this does not move, the tests above prove nothing about masking."""
        u, ws, proj, issue, seqs, ident = _fixture()
        with _scoped(ws):
            r = _post(Client(), "pull_request",
                      _merge_payload_body_only(f"refs {ident}-{issue.sequence_id}"))
        assert r.status_code == 200
        # RECOGNISED, not moved: the boundary refuses every write, so a live
        # directive shows up as a refusal naming its ticket. Asserting a state
        # change here would fail for the boundary's reason and say nothing
        # about masking.
        assert _recognised(r) == {f"{ident}-{issue.sequence_id}"}

    def test_mid_line_matching_IS_narrowed_now(self):
        """RENAMED AND INVERTED, because the ruling it pinned was reversed.

        It read: "`fix widget, refs GB-1` is live operator behaviour and slice 2
        keeps it. Slice 2 removes what the markdown says is not prose — not what
        is merely mid-line." ONE ANCHORED ADMISSION POLICY REVERSES THAT
        DECISION (BIP-67): mid-line IS prose now, on every event.

        Recorded as an inversion rather than deleted, because the old ruling was
        explicit and a reader deserves to find out here that it was overturned
        rather than wonder why the compatibility they were promised is gone. The
        migration cost was measured: 25 selections lost across 523 documents,
        none of them a clean trailer anyone intended as a board move."""
        u, ws, proj, issue, seqs, ident = _fixture()
        with _scoped(ws):
            r = _post(Client(), "pull_request", _merge_payload_body_only(
                f"fix the widget, refs {ident}-{issue.sequence_id}"))
        assert r.status_code == 200
        assert _recognised_in(r.json()) == [], "mid-line matching survived the consolidation"

    def test_a_quoted_directive_in_a_PUSHED_commit_message_does_not_fire(self):
        """The second call site. The push path and the PR path once used
        DIFFERENT recognisers — the push side an unanchored selection pass — so
        masking had to be fixed at both, and one site fixed with the other left
        was how this defect class survived a fix. They are now ONE anchored
        policy (`forward_selection` delegates to `parse_directives`), so this
        test pins that the consolidation actually holds on the push path rather
        than guarding a second implementation."""
        u, ws, proj, issue, seqs, ident = _fixture()
        control = _control_issue(ws, proj, seqs)
        with _scoped(ws):
            r = _post(Client(), "push", _push_payload(
                f"Refs {ident}-{control.sequence_id}\n\n"
                f"docs: explain that `refs {ident}-{issue.sequence_id}` advances"))
        assert r.status_code == 200
        recognised = _recognised(r)
        assert f"{ident}-{control.sequence_id}" in recognised, (
            "the visible control was not recognised on the PUSH path, so this case "
            "cannot tell whether the code span held or the recogniser is dead"
        )
        assert f"{ident}-{issue.sequence_id}" not in recognised, \
            "a quoted directive in a commit message fired"

    def test_a_body_quoting_a_directive_does_not_fire_either(self):
        """The admitted body source still applies the shared visibility rule."""
        u, ws, proj, issue, seqs, ident = _fixture()
        control = _control_issue(ws, proj, seqs)
        with _scoped(ws):
            r = _post(Client(), "pull_request", _merge_payload(
                f"Refs {ident}-{control.sequence_id}\n\n"
                f"docs: what `closes {ident}-{issue.sequence_id}` means"))
        assert r.status_code == 200
        recognised = _recognised(r)
        assert f"{ident}-{control.sequence_id}" in recognised, "the visible control was not recognised"
        assert f"{ident}-{issue.sequence_id}" not in recognised

    # -- RC 3571 (a): a VISIBLE directive must survive a hidden span on the
    # same line. The first attempt dropped the whole line, silently stopping a
    # live directive — the opposite of what this slice exists to do, and the
    # more dangerous direction of the two.

    @pytest.mark.parametrize("body,label", [
        # FIXTURES MOVED, SUBJECT PRESERVED (Aria's ruling). Every one of these
        # put the directive MID-LINE, so under one anchored policy they would now
        # fail for a reason that has nothing to do with the hidden span — the
        # subject would be destroyed rather than converted. The span moves to a
        # DIFFERENT LINE, which is the question that still matters: an unrelated
        # hidden span ELSEWHERE IN THE BODY must not disarm a real trailer.
        ("fix widget and `example`\n\nrefs {t}", "code span on an earlier line"),
        ("refs {t}\n\nthen `example` after",     "code span on a later line"),
        ("<!-- note -->\n\nrefs {t}",            "comment on an earlier line"),
        # A LINE-STARTING `<!--` opens an HTML BLOCK, so that case moved to
        # test_block_level_raw_html_is_never_a_recognition_site in round five.
        # This one keeps the comment mid-line, where it stays inline.
        ("text <!-- note --> here\n\nrefs {t}", "inline comment on an earlier line"),
    ])
    def test_a_visible_directive_survives_an_unrelated_hidden_span(self, body, label):
        u, ws, proj, issue, seqs, ident = _fixture()
        t = f"{ident}-{issue.sequence_id}"
        with _scoped(ws):
            r = _post(Client(), "pull_request", _merge_payload_body_only(body.format(t=t)))
        assert r.status_code == 200
        assert _recognised_in(r.json()) == [t], f"a live directive was dropped: {label}"

    # -- RC 3571 (b): directive-shaped text in hidden Markdown syntax. The
    # anchored parser never had to exclude these, because a directive must own
    # its whole line and these are inline. The unanchored compatibility matcher
    # that DID scan through them is deleted; these stay as regression pins that
    # nothing re-introduces mid-line matching.

    @pytest.mark.parametrize("body,label", [
        ('<span title="refs {t}">visible</span>',         "HTML attribute"),
        ('[visible](https://example.invalid "refs {t}")', "link title"),
        ("![refs {t}](image.png)",                        "image alt text"),
        ("[visible](https://example.invalid/refs-{t})",   "link destination"),
        ("<!-- refs {t}",                                 "unterminated comment"),
    ])
    def test_a_directive_in_hidden_markdown_syntax_does_not_fire(self, body, label):
        u, ws, proj, issue, seqs, ident = _fixture()
        t = f"{ident}-{issue.sequence_id}"
        control = _control_issue(ws, proj, seqs)
        c = f"{ident}-{control.sequence_id}"
        with _scoped(ws):
            # Control FIRST: an unterminated construct swallows everything after
            # it, so a trailing control would be masked too and the case would
            # fail for its own reason.
            r = _post(Client(), "pull_request",
                      _merge_payload_body_only(f"Refs {c}\n\n" + body.format(t=t)))
        assert r.status_code == 200
        recognised = _recognised(r)
        assert c in recognised, (
            f"the visible control was not recognised, so this case cannot tell "
            f"whether {label} held or the recogniser is dead"
        )
        assert t not in recognised, f"a directive fired from {label}"

    def test_POSITIVE_CONTROL_a_link_LABEL_is_visible_and_still_fires(self):
        """The other side of the hidden-syntax cases: a directive used as the
        link TEXT is read by a human and must still work. Without this, the
        class above is satisfiable by blanking all link syntax wholesale."""
        u, ws, proj, issue, seqs, ident = _fixture()
        t = f"{ident}-{issue.sequence_id}"
        with _scoped(ws):
            r = _post(Client(), "pull_request",
                      _merge_payload_body_only(f"[refs {t}](https://example.invalid)"))
        assert r.status_code == 200
        # NARROWED BY RECONSTRUCTION, not by visibility. The label IS visible —
        # that part of the old subject still holds — but the line also carries a
        # destination a reader never sees, so the difference between the source
        # line and its residue is not entirely accounted for and the line is no
        # longer a clean trailer. Visible is necessary and not sufficient.
        assert _recognised_in(r.json()) == [], "a link line was admitted as a trailer"
        # POSITIVE CONTROL: the same label with nothing hidden on the line.
        with _scoped(ws):
            r2 = _post(Client(), "pull_request", _merge_payload_body_only(f"refs {t}"))
        assert _recognised_in(r2.json()) == [t], "the recogniser is dead, not narrow"

    @pytest.mark.parametrize("title", [
        "refs {t}",
        "# refs {t}",
        "- refs {t}",
        "> refs {t}",
        "`refs {t}`",
        "<textarea>refs {t}</textarea>",
    ])
    def test_pull_request_titles_are_INERT(self, title):
        """Scope A's source list excludes titles regardless of rendering."""
        u, ws, proj, issue, seqs, ident = _fixture()
        t = f"{ident}-{issue.sequence_id}"
        control = _control_issue(ws, proj, seqs)
        c = f"{ident}-{control.sequence_id}"
        with _scoped(ws):
            # The control goes in the BODY, the one admitted source, so a live
            # recogniser must find it while the title finds nothing. Without it
            # this asserts an absence true for THREE reasons at once: titles are
            # inert, the boundary refuses, and the title arm no longer exists.
            r = _post(Client(), "pull_request",
                      _merge_payload(f"Refs {c}", title=title.format(t=t)))
        assert r.status_code == 200
        recognised = _recognised(r)
        assert c in recognised, "the body control was not recognised"
        assert t not in recognised, "a title nominated a ticket"

    # -- Round five (Morrow RC 3636). A generated 235-case element differential
    # disagreed with the deployed renderer 42 times, in BOTH directions. The
    # round rule fired: rather than a sixth patch, block-level raw HTML is no
    # longer a recognition site AT ALL. Not because its text is invisible —
    # often it is plainly visible — but because deciding which of it is visible
    # means reimplementing Forgejo's sanitizer, and five rounds proved that
    # cannot be done by approximation.
    #
    # Measured cost on the whole repository, 337 commits and 68 PR bodies:
    # ZERO directives lost, because not one text contains a top-level
    # html_block. The apparatus this replaces served a case that has never
    # occurred here.

    @pytest.mark.parametrize("body,label", [
        ("<div>refs {t}</div>",                    "visible text in a block — the stated cost"),
        ("<textarea>refs {t}</textarea>",          "Morrow's second witness: renders visibly, not recognized"),
        ("<script>refs {t}</script>",              "block script"),
        ("<!-- x --> refs {t}",                    "same-line remainder after a comment block"),
        ("<div title=\"<!--\">visible refs {t}</div>", "no attribute model any more, and none needed"),
    ])
    def test_block_level_raw_html_is_never_a_recognition_site(self, body, label):
        """One rule, no sanitizer model — so it cannot drift.

        The two Morrow witnesses are here together deliberately: the false
        ACTION (`<script/>`) is now impossible, and the false NEGATIVE
        (`<textarea>`) is now deliberate and stated rather than an accident of
        whose HTML parser was consulted. Both were disagreements with the
        renderer; only one of them was dangerous.
        """
        u, ws, proj, issue, seqs, ident = _fixture()
        t = f"{ident}-{issue.sequence_id}"
        control = _control_issue(ws, proj, seqs)
        c = f"{ident}-{control.sequence_id}"
        with _scoped(ws):
            r = _post(Client(), "pull_request",
                      _merge_payload_body_only(f"Refs {c}\n\n" + body.format(t=t)))
        assert r.status_code == 200
        recognised = _recognised(r)
        assert c in recognised, (
            f"the visible control was not recognised, so this case cannot tell "
            f"whether block HTML was excluded or the recogniser is dead ({label})"
        )
        assert t not in recognised, f"block HTML was treated as a recognition site: {label}"

    def test_a_directive_in_the_PARAGRAPH_AFTER_block_html_still_fires(self):
        """The bound is on the BLOCK, not on the rest of the field. This is the
        ordinary shape — a badge or comment, a blank line, then the trailer —
        and it must keep working, or the rule would be a silent-inertness
        machine of the kind BIP-33 exists to prevent."""
        u, ws, proj, issue, seqs, ident = _fixture()
        t = f"{ident}-{issue.sequence_id}"
        with _scoped(ws):
            r = _post(Client(), "pull_request",
                      _merge_payload_body_only(f"<img src=\"x.png\">\n\nrefs {t}"))
        assert r.status_code == 200
        assert _recognised(r) == {t}, "the exclusion leaked past its own block"

    # -- INLINE raw HTML is different and IS still read: markdown hands its
    # text over as ordinary `text` tokens, so recognizing them costs no
    # sanitizer modelling. Only script/style content is suppressed.

    @pytest.mark.parametrize("body,expected_recognised,label", [
        ("text <script>refs {t}</script> more", False, "inline script content is not shown"),
        ("text <style>refs {t}</style> more",   False, "inline style content is not shown"),
        # SAME MOVE, SAME REASON: these put the directive mid-line, so the
        # suppression question is asked ACROSS LINES now — which is the sharper
        # form of it: does raw HTML on one line suppress a trailer on a later one?
        ("text <textarea>x</textarea>\n\nrefs {t}", True,  "textarea on an earlier line"),
        ("<span>text</span>\n\nrefs {t}",        True,  "an inline tag does not disarm a later line"),
        ("text <script>refs {t}",               False, "an unclosed inline script suppresses onward"),
        # MEASURED, not reasoned. HTML5 forbids `<script/>`, so the obvious
        # inference is that it opens the element and swallows what follows —
        # and RC 3636's first witness asserted exactly that. The deployed
        # renderer disagrees: `<script/>x refs GB-1` renders `x refs GB-1`,
        # because the sanitizer removes the empty element and keeps the text.
        # Pinned in the direction the renderer actually goes.
        ("<script/>x\n\nrefs {t}",               True,  "a self-closing script does NOT open the element"),
        ("<style/>x\n\nrefs {t}",                True,  "same for style"),
    ])
    def test_inline_raw_html_is_still_read(self, body, expected_recognised, label):
        u, ws, proj, issue, seqs, ident = _fixture()
        t = f"{ident}-{issue.sequence_id}"
        control = _control_issue(ws, proj, seqs)
        c = f"{ident}-{control.sequence_id}"
        with _scoped(ws):
            r = _post(Client(), "pull_request",
                      _merge_payload_body_only(f"Refs {c}\n\n" + body.format(t=t)))
        assert r.status_code == 200
        recognised = _recognised(r)
        # A PAIR INSIDE ONE FUNCTION, and the inert arm needs the control MORE
        # than the live one does. Asserting `recognised == set()` for the inert
        # arm is an absence, so it passes with the recogniser dead — I proved
        # exactly that: with recognition stubbed out, every live arm went red
        # and these three inert arms stayed GREEN. Moving the assertion to
        # recognition did not close the vacuum; it relocated it. The control is
        # what closes it.
        assert c in recognised, (
            f"the visible control was not recognised, so this case cannot tell "
            f"whether the rule held or the recogniser is dead: {label}"
        )
        assert (t in recognised) is bool(expected_recognised), label

    def test_a_title_does_not_enter_the_body_conflict_rule(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        t = f"{ident}-{issue.sequence_id}"
        with _scoped(ws):
            r = _post(Client(), "pull_request", _merge_payload(f"refs {t}", title=f"closes {t}"))
        assert r.status_code == 200
        # The BODY's `refs` is recognised; the TITLE's `closes` must not be, or
        # it would enter the same-ticket conflict rule and upgrade the class.
        assert _recognised(r) == {t}, "an inert title entered selection"
        issue.refresh_from_db()
        assert issue.completed_at is None



# RETIRED WITH ITS SUBJECT (Morrow's ruling, BIP-67). TestPushMergeRace was the
# deterministic lock-window witness: it paused inside the state UPDATE and
# released the merge delivery once the push was inside that pause. THE ISSUE ROW
# LOCK IS GONE FROM BOTH PATHS along with the mutations, so there is no window
# to interleave and nothing to serialise. Re-anchoring it onto the refusal
# decision was proposed and REJECTED: it would have kept a green test whose
# stated subject no longer exists. The witness returns with the write path.

@pytest.mark.django_db
class TestReplyCannotChangeADeliveryOutcome:
    """The load-bearing restraint on the bridge's first outbound WRITE.

    Telling someone why a delivery declined is best-effort: the decision is
    made and its refusal durable by the time it runs. There is no board write to
    be pending — every one is refused — so the alternative this used to state,
    "already happened or already been refused", had a branch that cannot occur.
    If a failed reply escaped, the delivery would go back for retry — and the
    retry would re-run the whole decision in order to produce a message.

    The unit tests for `reply` prove the module returns rather than raises.
    THIS proves the delivery path survives one that raises anyway, which is the
    property that actually protects the board. Without it, deleting the guard
    reds nothing.
    """

    def _row(self, payload, event="push"):
        body = json.dumps(payload).encode()
        return ForgejoDelivery.objects.create(
            delivery_id=str(uuid_lib.uuid4()), event=event, payload=payload, repository=REPO,
            body_digest=hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(),
        )

    def test_a_reply_that_RAISES_leaves_the_delivery_processed(self):
        from plane.bridge import reply as reply_mod

        u, ws, proj, issue, seqs, ident = _fixture()
        row = self._row(_push_payload("nothing here nominates a ticket"))
        token = claim_delivery(row)
        with _scoped(ws), mock.patch.object(
            reply_mod, "refusal_comment", side_effect=RuntimeError("forge exploded")
        ):
            process_delivery(row, token)

        row.refresh_from_db()
        assert row.status == "processed", "a failed reply sent the delivery back for retry"
        assert row.last_error is None
        assert row.lease_token is None

    def test_the_reply_is_attempted_with_the_delivery_id_and_the_result(self):
        """It must carry the delivery id — that is what makes a redelivery
        recognisable — and the result it is describing."""
        from plane.bridge import reply as reply_mod

        u, ws, proj, issue, seqs, ident = _fixture()
        row = self._row(_push_payload("nothing here nominates a ticket"))
        token = claim_delivery(row)
        with _scoped(ws), mock.patch.object(reply_mod, "refusal_comment", return_value=False) as spoke:
            process_delivery(row, token)

        assert spoke.call_count == 1
        kwargs = spoke.call_args.kwargs
        assert kwargs["delivery_id"] == row.delivery_id
        assert "moved" in kwargs["result"]
