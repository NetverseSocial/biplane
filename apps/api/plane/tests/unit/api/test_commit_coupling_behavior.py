# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""BIP-18: the mutation commits FIRST; dispatch is post-commit and
best-effort — witnessed, not grepped (Morrow 10161, blocking 2 and 3).

What that buys and what it does not: the worker never sees an uncommitted
row, and a rolled-back mutation dispatches nothing. A broker failure after
commit still loses the dispatch — that stays lossy until PR 23's outbox
worker, and nothing here claims otherwise.

The earlier test scanned the source for the strings `transaction.atomic` and
`transaction.on_commit`; a callback replaced with `lambda: None`, a wrong
task, or an immediate dispatch plus a registered no-op all still passed. These
tests capture the ACTUAL commit callbacks through the real route:

  - success: nothing dispatched before commit; after the callbacks run, the
    exact activity + webhook work is dispatched with the created row's id;
  - a refused service-token assertion (canonical-but-nonexistent created_by,
    junk created_at): controlled 400, zero Issue/child rows, zero dispatches,
    zero registered callbacks — Morrow 10161 blocking 3's contract.

Kill demonstration (run, not automated): replacing on_commit registration
with a direct .delay call turns the first assertion red ("dispatched before
commit"); dropping the registration turns the second half red.
"""

import uuid as uuid_lib
from unittest import mock

import pytest

from plane.db.models import Issue, IssueAssignee

from .test_service_token_response_storage_parity import _client_for, _project_fixture


def _post_issue(client, ws, proj, body):
    return client.post(
        f"/api/v1/workspaces/{ws.slug}/projects/{proj.id}/issues/",
        body,
        format="json",
    )


# Plain django_db (test wrapped in an outer atomic): the request's commit is
# deferred, which is exactly what lets django_capture_on_commit_callbacks HOLD
# the registered callbacks instead of watching them fire at a real commit.
@pytest.mark.django_db
class TestDispatchIsCoupledToCommit:
    def test_success_dispatches_exactly_once_and_only_after_commit(
        self, django_capture_on_commit_callbacks
    ):
        caller, other, ws, proj = _project_fixture()
        client = _client_for(caller, is_service=False)
        with mock.patch("plane.api.views.issue.model_activity") as web:
            with django_capture_on_commit_callbacks(execute=False) as callbacks:
                r = _post_issue(client, ws, proj, {"name": "coupling probe"})
                assert r.status_code == 201, r.content
                # The whole point: NOTHING is dispatched inside the transaction.
                assert web.delay.call_count == 0
            assert callbacks, "no commit callbacks were registered at all"
            for cb in callbacks:
                cb()
            # After commit: the EXACT work, with the created row's id.
            row = Issue.objects.get(name="coupling probe")
            # AUDIT is no longer dispatched here — it is a durable outbox row
            # written inside the mutation's transaction (BIP-18).
            from plane.db.models import AuditOutbox

            audit = AuditOutbox.objects.get(payload__issue_id=str(row.pk))
            assert audit.task == "issue_activity"
            assert audit.payload["type"] == "issue.activity.created"
            assert audit.status == "pending"
            assert web.delay.call_count == 1
            assert web.delay.call_args.kwargs["model_id"] == str(row.pk)
            assert web.delay.call_args.kwargs["model_name"] == "issue"

    def test_refused_assertion_emits_nothing_and_writes_nothing(
        self, django_capture_on_commit_callbacks
    ):
        # Morrow 10161 blocking 3: a canonical-but-nonexistent created_by used
        # to surface as a deferred FK failure at commit — outside DRF's
        # handled path, with the write half-made. Now: controlled 400 at the
        # boundary, before any write or registration.
        caller, other, ws, proj = _project_fixture()
        client = _client_for(caller, is_service=True)
        ghost = str(uuid_lib.uuid4())
        with mock.patch("plane.api.views.issue.model_activity") as web:
            with django_capture_on_commit_callbacks(execute=True) as callbacks:
                r = _post_issue(
                    client, ws, proj, {"name": "ghost probe", "created_by": ghost}
                )
        assert r.status_code == 400, r.content
        assert b"created_by" in r.content
        assert Issue.objects.filter(name="ghost probe").count() == 0
        assert IssueAssignee.objects.count() == 0
        assert web.delay.call_count == 0
        # No audit intent either: the 400 happens before any write.
        from plane.db.models import AuditOutbox

        assert AuditOutbox.objects.count() == 0
        assert callbacks == []

    @pytest.mark.parametrize(
        "body,named",
        [
            ({"name": "junk-by", "created_by": "not-a-uuid"}, b"created_by"),
            ({"name": "junk-at", "created_at": "yesterday-ish"}, b"created_at"),
        ],
    )
    def test_malformed_asserted_values_are_named_400s_with_zero_rows(self, body, named):
        caller, other, ws, proj = _project_fixture()
        client = _client_for(caller, is_service=True)
        r = _post_issue(client, ws, proj, body)
        assert r.status_code == 400, r.content
        assert named in r.content
        assert Issue.objects.filter(name=body["name"]).count() == 0

    def test_an_ordinary_token_is_not_subject_to_the_assertion_contract(self):
        # Non-service callers' created_by/created_at is STRIPPED, not
        # validated: garbage from them was never input, so it cannot 400.
        caller, other, ws, proj = _project_fixture()
        client = _client_for(caller, is_service=False)
        r = _post_issue(client, ws, proj, {"name": "stripped", "created_by": "not-a-uuid"})
        assert r.status_code == 201, r.content
        row = Issue.objects.get(name="stripped")
        assert str(row.created_by_id) == str(caller.id)


@pytest.mark.django_db(transaction=True)
class TestRobustCallbacksSurviveBrokerFailure:
    """Morrow, PR 22 pre-read: without robust=True, a throwing .delay() at
    commit time propagates AFTER the DB committed — the HTTP request becomes
    a 500 with the row written, exactly the response-vs-storage split the
    boundary forbids, and it invites a duplicate retry. transaction=True on
    purpose: the request's commit is REAL here, so Django's own robust
    handling runs — not a test-harness re-implementation of it.

    Plainly: this is dispatch coupling, not atomicity. Audit delivery stays
    LOSSY on broker failure until the outbox worker and call-site conversion
    land (PR 23 onward)."""

    def test_a_throwing_first_callback_keeps_the_201_and_runs_the_second(self):
        caller, other, ws, proj = _project_fixture()
        client = _client_for(caller, is_service=False)
        with mock.patch("plane.api.views.issue.model_activity") as web:
            web.delay.side_effect = RuntimeError("broker down")
            r = _post_issue(client, ws, proj, {"name": "robust probe"})
        # The caller sees success and the row is committed even though the
        # commit callback threw — robust=True keeps a broker failure from
        # turning a completed write into a 500.
        assert r.status_code == 201, r.content
        assert Issue.objects.filter(name="robust probe").count() == 1
        assert web.delay.call_count == 1


@pytest.mark.django_db(transaction=True)
class TestVisibilityOrderThroughTheHelper:
    """Morrow's option-(a) evidence items, through the REAL commit path."""

    def test_the_row_is_visible_when_the_webhook_dispatch_runs(self):
        # The regression the sweep closed: an immediate .delay handed the
        # worker a row that was not committed yet. Witness the restored order
        # on a task that still uses this path — AUDIT now goes through the
        # outbox (BIP-18), so model_activity is the live example.
        caller, other, ws, proj = _project_fixture()
        client = _client_for(caller, is_service=False)
        seen = {}
        with mock.patch("plane.api.views.issue.model_activity") as web:
            web.delay.side_effect = lambda **kw: seen.update(
                row_visible=Issue.objects.filter(pk=kw["model_id"]).exists()
            )
            r = _post_issue(client, ws, proj, {"name": "visibility probe"})
        assert r.status_code == 201, r.content
        assert seen.get("row_visible") is True

    def test_helper_args_are_evaluated_eagerly_not_at_commit(self):
        # A bare lambda over caller locals would read whatever the variable
        # holds AT COMMIT. The helper must snapshot at the call site.
        from django.db import transaction as dj_transaction

        from plane.api.views.base import dispatch_after_commit

        task = mock.Mock()
        value = "at-call-time"
        with dj_transaction.atomic():
            dispatch_after_commit(task, payload=value)
            value = "mutated-before-commit"
        assert task.delay.call_args.kwargs["payload"] == "at-call-time"


@pytest.mark.django_db(transaction=True)
class TestHelperRefusesFalseDeferral:
    def test_outside_a_transaction_is_a_loud_refusal(self):
        # Django's on_commit RUNS IMMEDIATELY with no atomic block open —
        # the exact false deferral the helper exists to eliminate. Fail loud
        # instead (Morrow; same boundary rule as enqueue_audit).
        from plane.api.views.base import dispatch_after_commit

        task = mock.Mock()
        with pytest.raises(RuntimeError, match="outside a transaction"):
            dispatch_after_commit(task, x=1)
        assert task.delay.call_count == 0
