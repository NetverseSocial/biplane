# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The outbox worker delivers rows exactly once, recovers crashes, backs off
(BIP-18). The row is the only state; every behaviour is witnessed against it."""

import json
import uuid as uuid_lib
from unittest import mock

import pytest
from django.db import transaction
from django.utils import timezone

from plane.api.audit import enqueue_audit
from plane.bgtasks.audit_outbox_task import drain_audit_outbox
from plane.db.models import AuditOutbox


def _enqueue(task="issue_activity", **payload):
    with transaction.atomic():
        return enqueue_audit(task, **(payload or {"issue_id": "x"}))


@pytest.mark.django_db(transaction=True)
def test_a_pending_row_is_delivered_and_marked_processed():
    row = _enqueue(issue_id="i1")
    with mock.patch("plane.bgtasks.audit_outbox_task._resolve_task") as resolve:
        activity = mock.Mock()
        resolve.return_value = activity
        activity.return_value = []  # production returns the created activities
        assert drain_audit_outbox() == 1
        # The worker runs the activity SYNCHRONOUSLY inside the transaction —
        # not .delay — so that the write and the processed mark commit together.
        activity.assert_called_once_with(issue_id="i1", notification=False, raise_on_error=True)
    row.refresh_from_db()
    assert row.status == "processed" and row.processed_at is not None and row.lease_token is None


@pytest.mark.django_db(transaction=True)
def test_a_dispatch_failure_leaves_the_row_pending_with_backoff_not_lost():
    row = _enqueue(issue_id="i2")
    with mock.patch("plane.bgtasks.audit_outbox_task._resolve_task") as resolve:
        resolve.return_value = mock.Mock(side_effect=RuntimeError("broker down"))
        assert drain_audit_outbox() == 0
    row.refresh_from_db()
    # The whole point of the outbox: a broker failure does NOT lose the audit.
    assert row.status == "pending" and row.attempts == 1
    assert row.next_attempt_at is not None and "broker down" in (row.last_error or "")


@pytest.mark.django_db(transaction=True)
def test_an_expired_lease_is_recovered_a_crashed_worker_does_not_strand_a_row():
    row = _enqueue(issue_id="i3")
    # Simulate a worker that claimed the row then died: processing, lease expired.
    AuditOutbox.objects.filter(pk=row.pk).update(
        status="processing",
        lease_token="dead",
        lease_expires_at=timezone.now() - timezone.timedelta(seconds=1),
    )
    with mock.patch("plane.bgtasks.audit_outbox_task._resolve_task", return_value=mock.Mock(return_value=[])):
        assert drain_audit_outbox() == 1
    row.refresh_from_db()
    assert row.status == "processed"


@pytest.mark.django_db(transaction=True)
def test_a_live_lease_is_left_alone():
    row = _enqueue(issue_id="i4")
    AuditOutbox.objects.filter(pk=row.pk).update(
        status="processing",
        lease_token="held",
        lease_expires_at=timezone.now() + timezone.timedelta(seconds=60),
    )
    with mock.patch("plane.bgtasks.audit_outbox_task._resolve_task") as resolve:
        activity = mock.Mock()
        resolve.return_value = activity
        activity.return_value = []  # production returns the created activities
        assert drain_audit_outbox() == 0
        activity.assert_not_called()
    row.refresh_from_db()
    assert row.status == "processing" and row.lease_token == "held"


@pytest.mark.django_db(transaction=True)
def test_an_unknown_task_name_stays_pending_loudly_never_an_arbitrary_import():
    row = _enqueue(task="rm_rf_slash", issue_id="i5")
    assert drain_audit_outbox() == 0
    row.refresh_from_db()
    assert row.status == "pending" and "unknown audit task" in (row.last_error or "")


@pytest.mark.django_db(transaction=True)
def test_a_future_next_attempt_is_not_yet_due():
    row = _enqueue(issue_id="i6")
    AuditOutbox.objects.filter(pk=row.pk).update(
        next_attempt_at=timezone.now() + timezone.timedelta(minutes=10)
    )
    with mock.patch("plane.bgtasks.audit_outbox_task._resolve_task") as resolve:
        activity = mock.Mock()
        resolve.return_value = activity
        activity.return_value = []  # production returns the created activities
        assert drain_audit_outbox() == 0
        activity.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_delivery_is_idempotent_a_processed_row_is_not_redelivered():
    row = _enqueue(issue_id="i7")
    with mock.patch("plane.bgtasks.audit_outbox_task._resolve_task") as resolve:
        activity = mock.Mock()
        resolve.return_value = activity
        activity.return_value = []  # production returns the created activities
        drain_audit_outbox()
        drain_audit_outbox()  # second tick
        assert activity.call_count == 1  # the processed status guards the double


@pytest.mark.django_db(transaction=True)
class TestCrashWindowCannotDuplicate:
    """Morrow's ruling: LEASE OWNERSHIP IS NOT IDEMPOTENCY.

    A worker that writes the activity and THEN marks the row processed has a
    window between the two. Crash there and the activity rows exist while the
    outbox row still says pending, so the next tick writes them AGAIN — and the
    lease does not help, because it was validly held both times. The fix under
    test is that both halves are one transaction."""

    def _issue_fixture(self):
        from plane.db.models import Issue, IssueActivity, Project, State, User, Workspace

        u = User.objects.create(email=f"cw-{uuid_lib.uuid4().hex[:8]}@example.com", username=uuid_lib.uuid4().hex[:12])
        ws = Workspace.objects.create(slug=f"cw{uuid_lib.uuid4().hex[:10]}", name="CW", owner=u)
        proj = Project.objects.create(workspace=ws, name="P", identifier="CW" + uuid_lib.uuid4().hex[:2].upper())
        State.objects.create(name="Todo", project=proj, workspace=ws, group="unstarted", sequence=100, color="#000", created_by=u)
        issue = Issue.objects.create(workspace=ws, project=proj, name="crash probe")
        return u, proj, issue, IssueActivity

    def test_a_crash_between_the_activity_and_the_mark_leaves_exactly_one_set(self):
        from plane.bgtasks import audit_outbox_task

        u, proj, issue, IssueActivity = self._issue_fixture()
        with transaction.atomic():
            row = enqueue_audit(
                "issue_activity",
                type="issue.activity.updated",
                requested_data=json.dumps({"name": "crash probe 2"}),
                current_instance=json.dumps({"name": "crash probe"}),
                issue_id=str(issue.id),
                actor_id=str(u.id),
                project_id=str(proj.id),
                epoch=int(timezone.now().timestamp()),
            )

        before = IssueActivity.objects.filter(issue_id=issue.id).count()
        with mock.patch.object(
            audit_outbox_task, "_resolve_task", side_effect=_dying_resolve(audit_outbox_task)
        ):
            with pytest.raises(SystemExit):
                drain_audit_outbox()

        # Nothing committed: the activity rows rolled back WITH the failed mark.
        assert IssueActivity.objects.filter(issue_id=issue.id).count() == before
        row.refresh_from_db()
        # A worker that DIED does not tidy up after itself: the row is still
        # `processing` and still holds the lease it took. That is the state a
        # real crash leaves, and asserting "pending" here would be modelling a
        # graceful failure instead of the crash under test.
        assert row.status == "processing"

        # Recovery happens when the dead worker's lease expires.
        AuditOutbox.objects.filter(pk=row.pk).update(
            lease_expires_at=timezone.now() - timezone.timedelta(seconds=1)
        )

        # The retry now writes exactly one set.
        assert drain_audit_outbox() == 1
        after = IssueActivity.objects.filter(issue_id=issue.id).count()
        assert after > before, "the retry must actually write the activity"
        # And a further tick must not write a second set.
        drain_audit_outbox()
        assert IssueActivity.objects.filter(issue_id=issue.id).count() == after

    def test_a_lease_reclaimed_mid_write_rolls_back_the_REAL_activity_rows(self):
        """Morrow RC 3237: the kill for the whole exactly-once design.

        The earlier reclaim test used a stub that returned [] and wrote no
        database row, so deleting `transaction.atomic()` from `_process` left
        it observing identical state — the guarantee had no mutation kill.

        Here the task writes REAL activity rows inside the real `_process`,
        THEN loses its lease on an independent connection. The conditional
        mark matches zero rows, `_LeaseLostError` is raised, and the atomic
        block must take the activity rows down with it. If it does not, both
        this worker and the new owner write the same audit and the work item
        carries two identical sets.
        """
        from plane.bgtasks import audit_outbox_task

        u, proj, issue, IssueActivity = self._issue_fixture()
        with transaction.atomic():
            row = enqueue_audit(
                "issue_activity",
                type="issue.activity.updated",
                requested_data=json.dumps({"name": "reclaim probe 2"}),
                current_instance=json.dumps({"name": "reclaim probe"}),
                issue_id=str(issue.id),
                actor_id=str(u.id),
                project_id=str(proj.id),
                epoch=int(timezone.now().timestamp()),
            )

        before = IssueActivity.objects.filter(issue_id=issue.id).count()
        real_resolve = audit_outbox_task._resolve_task
        wrote = {"count": 0}

        def resolve(name):
            task = real_resolve(name)

            def write_then_lose_the_lease(**payload):
                created = task(**payload)  # REAL rows, inside the real atomic block
                wrote["count"] = len(created or [])
                _steal_lease_on_another_connection(row.pk)
                return created

            return write_then_lose_the_lease

        with mock.patch.object(audit_outbox_task, "_resolve_task", side_effect=resolve):
            assert drain_audit_outbox() == 0, "the loser of the race counted a delivery"

        assert wrote["count"] > 0, "the write never happened, so the rollback proves nothing"

        assert IssueActivity.objects.filter(issue_id=issue.id).count() == before, (
            "a real activity write survived a lost lease — the audit rows and the "
            "processed mark are NOT one transaction, so a retry will duplicate them"
        )

        row.refresh_from_db()
        assert row.status != "processed"
        assert row.lease_token == "other-worker"


def _dying_resolve(module):
    """Resolve to a task that writes the REAL activity rows and then dies.

    Morrow RC 3237: the previous version of this helper replaced `_process`
    wholesale, including its own `t.atomic()`. That meant the real
    `transaction.atomic()` in `_process` was never exercised — deleting it
    left this test observing exactly the same state, so the central
    exactly-once guarantee had no kill.

    The crash is raised INSIDE the real `_process` now. `SystemExit` rather
    than `Exception` on purpose: `_process` catches `Exception` and would turn
    a crash into a graceful deferral, which is a different scenario. A
    BaseException propagates, so the rollback under observation is the real
    atomic block and nothing else.
    """
    real_resolve = module._resolve_task

    def resolve(name):
        task = real_resolve(name)

        def crash(**payload):
            task(**payload)  # real activity rows, written for real
            raise SystemExit("worker died before marking processed")

        return crash

    return resolve


# --------------------------------------------------------------------------
# Morrow RC 3226, bars 1 and 2. The tests above stub _resolve_task, so they
# prove the WORKER's contract but say nothing about the real task. Production
# issue_activity catches every exception and returns normally, so a stub that
# raises exercises a path production does not have. These use the REAL task.
# --------------------------------------------------------------------------


def _real_payload(**overrides):
    payload = {
        "type": "issue.activity.updated",
        "requested_data": json.dumps({"name": "x"}),
        "current_instance": json.dumps({"name": "y"}),
        "issue_id": str(uuid_lib.uuid4()),
        "actor_id": str(uuid_lib.uuid4()),
        "project_id": str(uuid_lib.uuid4()),
        "epoch": 1754870000.0,
    }
    payload.update(overrides)
    return payload


@pytest.mark.django_db(transaction=True)
def test_a_real_production_failure_is_not_marked_processed():
    """The production task SWALLOWS exceptions and returns normally.

    project_id here is a well-formed uuid for a project that does not exist,
    so Project.objects.get() raises inside issue_activity. Before this fix
    that exception was logged and swallowed, the task returned, and the worker
    marked the row processed having written zero audit rows — the exact
    outcome BIP-18 exists to prevent. No mock: this is the real task.
    """
    row = _enqueue(**_real_payload())

    assert drain_audit_outbox() == 0

    row.refresh_from_db()
    assert row.status == "pending", "a real production failure was recorded as success"
    assert row.attempts == 1
    assert row.next_attempt_at is not None
    assert row.last_error


@pytest.mark.django_db(transaction=True)
def test_a_task_that_returns_without_writing_is_not_marked_processed():
    """The other door to the same failure: issue_activity early-returns on an
    invalid project_id, writing nothing and raising nothing. A normal return
    is not evidence of a write."""
    row = _enqueue(**_real_payload(project_id="not-a-uuid"))

    assert drain_audit_outbox() == 0

    row.refresh_from_db()
    assert row.status == "pending", "a silent no-op was recorded as a successful audit write"
    assert row.attempts == 1
    assert "without writing audit" in (row.last_error or "")


@pytest.mark.django_db(transaction=True)
def test_notification_intent_survives_the_worker_and_fires_after_commit():
    """Four converted call sites pass notification=True. The worker must not
    run the fan-out INSIDE its transaction (it would escape a rollback), but
    forcing it to False silently dropped those notifications entirely."""
    row = _enqueue(**_real_payload(notification=True))

    with mock.patch("plane.bgtasks.audit_outbox_task._resolve_task") as resolve:
        resolve.return_value = mock.Mock(return_value=[])
        with mock.patch("plane.bgtasks.notification_task.notifications") as notifications:
            assert drain_audit_outbox() == 1

            # Dispatched once, and only after the audit committed.
            notifications.delay.assert_called_once()
            # The audit write itself ran with the fan-out off.
            called = resolve.return_value.call_args.kwargs
            assert called["notification"] is False
            assert called["raise_on_error"] is True

    row.refresh_from_db()
    assert row.status == "processed"


@pytest.mark.django_db(transaction=True)
def test_no_notification_is_dispatched_when_the_row_did_not_ask_for_one():
    row = _enqueue(**_real_payload())

    with mock.patch("plane.bgtasks.audit_outbox_task._resolve_task") as resolve:
        resolve.return_value = mock.Mock(return_value=[])
        with mock.patch("plane.bgtasks.notification_task.notifications") as notifications:
            assert drain_audit_outbox() == 1
            notifications.delay.assert_not_called()

    row.refresh_from_db()
    assert row.status == "processed"


@pytest.mark.django_db(transaction=True)
def test_a_failed_notification_does_not_undo_a_committed_audit():
    """The audit rows and the processed mark are already committed by then.
    A fan-out failure must be logged and dropped, never re-run the audit."""
    row = _enqueue(**_real_payload(notification=True))

    with mock.patch("plane.bgtasks.audit_outbox_task._resolve_task") as resolve:
        resolve.return_value = mock.Mock(return_value=[])
        with mock.patch("plane.bgtasks.notification_task.notifications") as notifications:
            notifications.delay.side_effect = RuntimeError("broker down")
            assert drain_audit_outbox() == 1

    row.refresh_from_db()
    assert row.status == "processed", "a notification failure rolled back a committed audit"
    assert row.attempts == 1


# --------------------------------------------------------------------------
# Morrow RC 3226, bar 3: a deterministic CONCURRENT witness.
#
# The lease tests above are single-writer — they set up a state and then run
# one worker, so they can never reach a genuine race. The steal below happens
# on a SEPARATE database connection, mid-write, and commits independently.
# Doing it on the worker's own connection would enrol the steal in the
# worker's transaction, so the rollback would undo the steal too and the test
# would pass for the wrong reason.
# --------------------------------------------------------------------------


def _steal_lease_on_another_connection(row_pk, token="other-worker"):
    """Reclaim the lease the way a second worker process would: its own
    connection, its own commit, invisible to any rollback here."""
    from django.db import connections

    conn = connections.create_connection("default")
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE audit_outbox SET lease_token = %s WHERE id = %s",
                [token, str(row_pk)],
            )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.django_db(transaction=True)
def test_a_lease_reclaimed_mid_write_rolls_the_whole_thing_back():
    """LEASE OWNERSHIP IS NOT IDEMPOTENCY. If the lease is reclaimed while this
    worker is mid-write, its conditional mark matches zero rows and the audit
    write must roll back with it — otherwise both workers write the activity
    and the work item carries two identical audit sets."""
    row = _enqueue(**_real_payload())
    stolen = {"count": 0}

    def steal_then_succeed(**kwargs):
        stolen["count"] += 1
        _steal_lease_on_another_connection(row.pk)
        return []

    with mock.patch("plane.bgtasks.audit_outbox_task._resolve_task") as resolve:
        resolve.return_value = mock.Mock(side_effect=steal_then_succeed)
        # The loser must not count the row as delivered.
        assert drain_audit_outbox() == 0

    assert stolen["count"] == 1, "the write never ran, so the race was not reached"

    row.refresh_from_db()
    assert row.status != "processed", "the loser of the race marked the row processed"
    assert row.lease_token == "other-worker", "the reclaiming worker's lease was overwritten"
    assert row.processed_at is None


@pytest.mark.django_db(transaction=True)
def test_the_loser_does_not_stamp_its_own_failure_over_the_new_owner():
    """The losing worker must also not run its FAILURE path over the row —
    that would hand a live lease back to the pool while the new owner is
    still working on it."""
    row = _enqueue(**_real_payload())

    with mock.patch("plane.bgtasks.audit_outbox_task._resolve_task") as resolve:
        resolve.return_value = mock.Mock(
            side_effect=lambda **kw: (_steal_lease_on_another_connection(row.pk), [])[1]
        )
        drain_audit_outbox()

    row.refresh_from_db()
    assert row.status == "processing", "the loser reset a row owned by another worker"
    assert row.lease_token == "other-worker"
    assert row.attempts == 0, "the loser charged an attempt against the new owner's row"


# --------------------------------------------------------------------------
# Morrow RC 3226, bar 5: the record must match the worker.
#
# `result` existed on the model specifically so a processed row could say what
# it produced, and nothing wrote it — every processed row claimed success with
# no record of what it made. These pin the honest behaviour.
# --------------------------------------------------------------------------


class _FakeActivity:
    def __init__(self, activity_id):
        self.id = activity_id


@pytest.mark.django_db(transaction=True)
def test_a_processed_row_records_what_it_actually_created():
    row = _enqueue(**_real_payload())
    ids = [uuid_lib.uuid4(), uuid_lib.uuid4()]

    with mock.patch("plane.bgtasks.audit_outbox_task._resolve_task") as resolve:
        resolve.return_value = mock.Mock(return_value=[_FakeActivity(i) for i in ids])
        assert drain_audit_outbox() == 1

    row.refresh_from_db()
    assert row.status == "processed"
    assert row.result is not None, "a processed row claimed success with no record of what it made"
    assert row.result["activity_count"] == 2
    assert row.result["activity_ids"] == [str(i) for i in ids]


@pytest.mark.django_db(transaction=True)
def test_a_deferred_row_records_no_result():
    """A row that did not complete must not carry a result — that would read
    as a delivery that happened."""
    row = _enqueue(**_real_payload())

    with mock.patch("plane.bgtasks.audit_outbox_task._resolve_task") as resolve:
        resolve.return_value = mock.Mock(side_effect=RuntimeError("broker down"))
        assert drain_audit_outbox() == 0

    row.refresh_from_db()
    assert row.status == "pending"
    assert row.result is None


@pytest.mark.django_db(transaction=True)
def test_the_result_is_written_in_the_same_statement_as_the_processed_mark():
    """If the two could diverge, a row could be processed with a stale or
    missing result. They are set by one UPDATE, so a processed row without a
    result is impossible rather than merely unlikely."""
    row = _enqueue(**_real_payload())

    with mock.patch("plane.bgtasks.audit_outbox_task._resolve_task") as resolve:
        resolve.return_value = mock.Mock(return_value=[])
        assert drain_audit_outbox() == 1

    row.refresh_from_db()
    # An empty write is still a completed one, and says so honestly.
    assert row.status == "processed"
    assert row.result == {"activity_ids": [], "activity_count": 0}


@pytest.mark.django_db(transaction=True)
def test_an_unrecognised_return_shape_does_not_undo_a_good_delivery():
    """_result_of runs AFTER the audit write succeeded. A surprise return
    shape must be recorded, not raised — raising would defer a row whose
    audit is already written and retry it forever."""
    row = _enqueue(**_real_payload())

    with mock.patch("plane.bgtasks.audit_outbox_task._resolve_task") as resolve:
        resolve.return_value = mock.Mock(return_value=object())  # not iterable
        assert drain_audit_outbox() == 1

    row.refresh_from_db()
    assert row.status == "processed"
    assert row.result["unrecognised_return"] is True
    assert row.result["activity_count"] is None


@pytest.mark.django_db(transaction=True)
def test_an_unknown_activity_type_is_not_silently_recorded_as_processed():
    """Morrow RC 3237. ACTIVITY_MAPPER.get(type) returns None for an unknown
    type, and the old path fell through, bulk-created [], and returned [] —
    indistinguishable from a legitimate zero-diff update. The worker consumed
    the durable row and stored activity_count 0, so a typo in any converted
    call site would discard its audit permanently and silently."""
    from plane.db.models import Issue, Project, State, User, Workspace

    # A REAL project/issue: with a fabricated project_id the task dies on the
    # Project lookup before it ever reaches the mapper, so the row would go
    # pending for the wrong reason and this test would pass without touching
    # the behaviour under test.
    u = User.objects.create(email=f"ut-{uuid_lib.uuid4().hex[:8]}@example.com", username=uuid_lib.uuid4().hex[:12])
    ws = Workspace.objects.create(slug=f"ut{uuid_lib.uuid4().hex[:10]}", name="UT", owner=u)
    proj = Project.objects.create(workspace=ws, name="P", identifier="UT" + uuid_lib.uuid4().hex[:2].upper())
    State.objects.create(
        name="Todo", project=proj, workspace=ws, group="unstarted", sequence=100, color="#000", created_by=u
    )
    issue = Issue.objects.create(workspace=ws, project=proj, name="typo probe")

    with transaction.atomic():
        row = enqueue_audit(
            "issue_activity",
            type="issue.activity.typo",  # not in ACTIVITY_MAPPER
            requested_data=json.dumps({"name": "b"}),
            current_instance=json.dumps({"name": "a"}),
            issue_id=str(issue.id),
            actor_id=str(u.id),
            project_id=str(proj.id),
            epoch=int(timezone.now().timestamp()),
        )

    assert drain_audit_outbox() == 0

    row.refresh_from_db()
    assert row.status == "pending", "an unknown activity type consumed its durable row"
    assert row.attempts == 1
    assert "unknown audit activity type" in (row.last_error or ""), (
        f"deferred for the wrong reason: {row.last_error!r}"
    )
    assert row.result is None


@pytest.mark.django_db(transaction=True)
def test_a_known_type_with_no_changes_is_still_a_valid_delivery():
    """The counterpart: a KNOWN type that legitimately produces no diff must
    stay valid. Failing those would turn every no-op update into a retry
    loop."""
    from plane.db.models import Issue, IssueActivity, Project, State, User, Workspace

    u = User.objects.create(email=f"nz-{uuid_lib.uuid4().hex[:8]}@example.com", username=uuid_lib.uuid4().hex[:12])
    ws = Workspace.objects.create(slug=f"nz{uuid_lib.uuid4().hex[:10]}", name="NZ", owner=u)
    proj = Project.objects.create(workspace=ws, name="P", identifier="NZ" + uuid_lib.uuid4().hex[:2].upper())
    State.objects.create(
        name="Todo", project=proj, workspace=ws, group="unstarted", sequence=100, color="#000", created_by=u
    )
    issue = Issue.objects.create(workspace=ws, project=proj, name="same")

    with transaction.atomic():
        row = enqueue_audit(
            "issue_activity",
            type="issue.activity.updated",
            # identical before and after: a real type, genuinely zero diff
            requested_data=json.dumps({"name": "same"}),
            current_instance=json.dumps({"name": "same"}),
            issue_id=str(issue.id),
            actor_id=str(u.id),
            project_id=str(proj.id),
            epoch=int(timezone.now().timestamp()),
        )

    assert drain_audit_outbox() == 1

    row.refresh_from_db()
    assert row.status == "processed"
    assert row.result["activity_count"] == 0
    assert IssueActivity.objects.filter(issue_id=issue.id).count() == 0


@pytest.mark.django_db(transaction=True)
def test_result_counts_activities_a_handler_wrote_directly():
    """BIP-34, found on a LIVE farm deployment and invisible to this suite
    until now.

    create_issue_activity is the one handler that does not append to
    `issue_activities` — it writes its row directly so it can backdate
    created_at to the issue's own timestamp, which bulk_create cannot do.
    Its activity therefore never reached `issue_activities_created`, so a
    processed outbox row reported activity_count 0 for a delivery that had in
    fact written an activity. `result` exists precisely so a processed row
    says what it made; reporting 0 there is the same lie in a smaller font.
    """
    from plane.db.models import Issue, IssueActivity, Project, State, User, Workspace

    u = User.objects.create(email=f"b34-{uuid_lib.uuid4().hex[:8]}@example.com", username=uuid_lib.uuid4().hex[:12])
    ws = Workspace.objects.create(slug=f"b34{uuid_lib.uuid4().hex[:10]}", name="B34", owner=u)
    proj = Project.objects.create(workspace=ws, name="P", identifier="B3" + uuid_lib.uuid4().hex[:3].upper())
    State.objects.create(
        name="Todo", project=proj, workspace=ws, group="unstarted", sequence=100, color="#000", created_by=u
    )
    issue = Issue.objects.create(workspace=ws, project=proj, name="b34 probe", created_by=u)

    with transaction.atomic():
        row = enqueue_audit(
            "issue_activity",
            type="issue.activity.created",
            requested_data=json.dumps({"name": "b34 probe"}),
            current_instance=None,
            issue_id=str(issue.id),
            actor_id=str(u.id),
            project_id=str(proj.id),
            epoch=int(timezone.now().timestamp()),
        )

    assert drain_audit_outbox() == 1

    written = IssueActivity.objects.filter(issue_id=issue.id, verb="created")
    assert written.count() == 1, "the creation activity was not written at all"

    row.refresh_from_db()
    assert row.status == "processed"
    assert row.result["activity_count"] == 1, (
        f"result under-reports a directly-written activity: {row.result}"
    )
    assert row.result["activity_ids"] == [str(written.first().id)]
