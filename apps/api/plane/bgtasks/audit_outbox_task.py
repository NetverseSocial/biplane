# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Drain the durable audit outbox (BIP-18).

`enqueue_audit()` writes an AuditOutbox row INSIDE the mutation's transaction,
so audit intent commits with the mutation or neither does. This task turns
that intent into the real activity rows.

LEASE OWNERSHIP IS NOT IDEMPOTENCY (Morrow's ruling, and the thing that makes
this worker correct rather than plausible). A worker that dispatches the
activity and THEN marks the row processed has a crash window between the two:
the activity rows exist, the outbox row still says pending, the next tick runs
it again, and the work item now carries two identical activity sets. A lease
does not help — the lease was validly held both times.

So the audit write and the state transition happen SYNCHRONOUSLY IN ONE
TRANSACTION, under the lease:

    with transaction.atomic():
        issue_activity(**payload)          # writes the activity rows
        <mark processed, conditioned on still holding the lease>

A crash anywhere inside that block rolls back BOTH halves, so the retry starts
from a clean slate and exactly one activity set can ever exist. If the
conditional mark matches zero rows the lease was reclaimed mid-flight, and the
whole transaction is rolled back rather than racing the new owner.

Latency: `enqueue_audit` wakes this task on commit (best effort). The
every-minute beat is RECOVERY — for a lost wake-up, a crashed worker, or a
broker outage — not the normal path.
"""

import logging
from uuid import uuid4

from celery import shared_task
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from plane.db.models import AuditOutbox

logger = logging.getLogger("plane.worker")

DRAIN_BATCH_SIZE = 100
LEASE_SECONDS = 120
RETRY_BACKOFF_CAP_SECONDS = 3600


class _LeaseLostError(Exception):
    """The lease was reclaimed mid-flight; abort and let the owner finish."""


class _AuditNotWrittenError(Exception):
    """The audit task returned without producing activities.

    issue_activity has early returns (an invalid project_id, for one) that
    exit normally having written nothing. Those look identical to success from
    the outside, so without this the row would be marked processed with zero
    audit — the same failure as a swallowed exception, by a different door.
    """


def _resolve_task(name):
    """Audit tasks this worker may execute, by name. A row's `task` is DATA,
    so it is matched against this allowlist — never imported dynamically."""
    if name == "issue_activity":
        from plane.bgtasks.issue_activities_task import issue_activity

        return issue_activity
    return None


def _result_of(created) -> dict:
    """What the audit write actually produced, for the row's `result`.

    Ids only. The payload is already stored on the row, and copying activity
    bodies here would duplicate mutable content into an append-only record.
    """
    # Defensive on purpose: this runs AFTER the audit write has succeeded, so
    # an unexpected return shape must not turn a good delivery into a retry
    # loop. Record that the shape was unrecognised instead of raising.
    try:
        activities = list(created or [])
    except TypeError:
        logger.warning(f"audit-outbox: unrecognised audit return shape {type(created).__name__}")
        return {"activity_ids": [], "activity_count": None, "unrecognised_return": True}

    ids = []
    for activity in activities:
        activity_id = getattr(activity, "id", None)
        if activity_id is not None:
            ids.append(str(activity_id))
    return {"activity_ids": ids, "activity_count": len(activities)}


def _claim(row) -> str | None:
    """Atomically claim a due row into a leased `processing` state."""
    token = uuid4().hex
    now = timezone.now()
    updated = (
        AuditOutbox.objects.filter(pk=row.pk)
        .filter(Q(status="pending") | Q(status="processing", lease_expires_at__lt=now))
        .update(
            status="processing",
            lease_token=token,
            lease_expires_at=now + timezone.timedelta(seconds=LEASE_SECONDS),
        )
    )
    return token if updated else None


def _fail(row, lease_token: str, error: str) -> None:
    attempts = row.attempts + 1
    backoff = min(2 ** min(attempts, 12), RETRY_BACKOFF_CAP_SECONDS)
    AuditOutbox.objects.filter(pk=row.pk, lease_token=lease_token, status="processing").update(
        status="pending",
        attempts=attempts,
        last_error=error[:1000],
        next_attempt_at=timezone.now() + timezone.timedelta(seconds=backoff),
        lease_token=None,
        lease_expires_at=None,
    )


def _process(row, lease_token: str) -> bool:
    task = _resolve_task(row.task)
    if task is None:
        _fail(row, lease_token, f"unknown audit task {row.task!r}")
        return False

    payload = dict(row.payload or {})

    # A nested dispatch would escape the transaction below and survive a
    # rollback, so the audit write itself runs with the fan-out OFF. But the
    # request's INTENT is not discarded (Morrow, RC 3226): four converted call
    # sites ask for notifications, and forcing this to False silently removed
    # them. If the row asked for notifications they are dispatched AFTER this
    # transaction commits, built from the activities the write actually
    # produced — so they can neither escape a rollback nor go missing.
    wants_notification = bool(payload.pop("notification", False))
    payload["notification"] = False

    # issue_activity swallows every exception and returns normally, so a quiet
    # return is NOT evidence that audit was written. Without this the block
    # below would mark the row processed having written nothing.
    payload["raise_on_error"] = True

    created = None
    try:
        with transaction.atomic():
            # The audit write and the state transition are ONE unit: a crash
            # between them would otherwise duplicate the activity on retry.
            created = task(**payload)
            if created is None:
                # A normal return that wrote nothing. Defer rather than record
                # success; the row is preserved and retried with backoff.
                raise _AuditNotWrittenError(
                    f"{row.task} returned without writing audit for {row.event_key}"
                )
            owned = AuditOutbox.objects.filter(
                pk=row.pk, lease_token=lease_token, status="processing"
            ).update(
                status="processed",
                attempts=row.attempts + 1,
                processed_at=timezone.now(),
                last_error=None,
                lease_token=None,
                lease_expires_at=None,
                # `result` existed for exactly this and nothing wrote it, so
                # every processed row claimed success with no record of WHAT
                # it produced. Written in the same statement as the processed
                # mark, so the two can never disagree.
                result=_result_of(created),
            )
            if not owned:
                # Lease reclaimed mid-flight. Roll the activity back too — the
                # new owner is authoritative and will write it exactly once.
                raise _LeaseLostError(f"lease on {row.event_key} was reclaimed")
    except _LeaseLostError as e:
        logger.info(f"audit-outbox: {e}")
        return False
    except Exception as e:  # noqa: BLE001 — any failure defers; the row is never lost
        _fail(row, lease_token, str(e))
        return False

    # Past this point the audit rows and the processed mark are COMMITTED. A
    # notification failure must not undo that or re-run the audit, so it is
    # logged and dropped rather than raised — but logged LOUDLY, because a
    # fan-out that silently stops is how a working system rots quietly.
    if wants_notification:
        _dispatch_notifications(payload, created)

    return True


def _dispatch_notifications(payload, created) -> None:
    """Fan out notifications for an audit row that asked for them."""
    try:
        import json

        from django.core.serializers.json import DjangoJSONEncoder

        from plane.app.serializers import IssueActivitySerializer
        from plane.bgtasks.notification_task import notifications

        notifications.delay(
            type=payload.get("type"),
            issue_id=payload.get("issue_id"),
            actor_id=payload.get("actor_id"),
            project_id=payload.get("project_id"),
            subscriber=payload.get("subscriber", True),
            issue_activities_created=json.dumps(
                IssueActivitySerializer(created or [], many=True).data,
                cls=DjangoJSONEncoder,
            ),
            requested_data=payload.get("requested_data"),
            current_instance=payload.get("current_instance"),
        )
    except Exception as e:  # noqa: BLE001 — audit is already durable; never undo it
        logger.warning(f"audit-outbox: notification fan-out failed after a committed audit write: {e}")


@shared_task
def drain_audit_outbox():
    now = timezone.now()
    due = AuditOutbox.objects.filter(
        Q(status="pending") & (Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        | Q(status="processing", lease_expires_at__lt=now)
    ).order_by("created_at")[:DRAIN_BATCH_SIZE]
    delivered = 0
    for row in due:
        lease = _claim(row)
        if lease is None:
            continue  # another worker owns it
        row.refresh_from_db()
        if _process(row, lease):
            delivered += 1
    if delivered:
        logger.info(f"audit-outbox: delivered {delivered} rows")
    return delivered
