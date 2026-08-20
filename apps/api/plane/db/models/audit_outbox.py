# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import uuid

# Django imports
from django.db import models

# Module imports
from .base import BaseModel


class AuditOutbox(BaseModel):
    """Durable record of an audit event, written WITH the mutation it describes.

    BIP-18. Audit was dispatched with `issue_activity.delay(...)`, which is a
    message to a broker and not a database write. Three failures followed, and
    only the third survives `on_commit`:

      1. the task could run before the mutation committed, and read stale or
         absent data;
      2. a rolled-back mutation still emitted its audit;
      3. **a broker outage after commit loses the audit silently** — the
         mutation is committed, the caller got a 2xx, and there is nothing left
         to retry from.

    `on_commit` fixes 1 and 2. It cannot fix 3, because the thing being deferred
    is still an external side effect. So the authoritative artifact is a ROW,
    written inside the same transaction as the mutation. `on_commit` is now
    only a latency optimisation — it wakes the worker sooner — and a missed
    wake-up costs latency rather than the audit, because the row is still
    there for the next beat to pick up.

    WHAT IS LIVE (updated with the worker slice, Morrow RC 3226 bar 5)

    Live: the model, its migration, `enqueue_audit()`'s inside-a-transaction
    contract, and `drain_audit_outbox` — the worker that claims rows on a
    lease, performs the audit write, and marks them processed. Recovery is the
    worker itself: it picks up rows whose lease has expired, so a crashed
    processor does not strand work. There is no separate scanner process.

    Ownership is the same shape as `ForgejoDelivery`, the durable inbox this
    codebase already runs: work is claimed into `processing` with a lease
    token and expiry, and completion is conditioned on still holding that
    token, so overlapping workers cannot overwrite a finished row.

    IDEMPOTENCY IS TRANSACTIONAL, NOT KEYED. An earlier version of this
    docstring said `event_key` was the idempotency handle and that the worker
    "creates the activity keyed on it". That is not what the worker does, and
    saying so was worse than saying nothing — it described a mechanism a
    reader could rely on and no code implements.

    What actually makes delivery exactly-once: the audit write and the
    processed mark happen in ONE transaction, under the lease. A crash
    anywhere inside rolls back BOTH, so a retry starts from a clean slate and
    exactly one activity set can exist. A lease alone would not be enough —
    a worker that wrote the activity and then died before marking the row
    would leave valid work to be repeated, and the lease was validly held both
    times.

    `event_key` is therefore a STABLE EXTERNAL HANDLE, not a dedupe key: it is
    unique, survives retries, and gives callers and logs something durable to
    reference. Nothing keys deduplication on it today.

    `result` records what a processed row actually produced — the created
    activity ids and their count — written in the same statement as the
    processed mark, so a row can never claim success without saying what it
    made.
    """

    STATUS_CHOICES = (
        ("pending", "pending"),
        ("processing", "processing"),
        ("processed", "processed"),
    )

    # Stable across retries; a durable external handle, NOT a dedupe key —
    # nothing keys deduplication on it. Exactly-once comes from the audit
    # write and the processed mark sharing one transaction under the lease.
    event_key = models.UUIDField(unique=True, default=uuid.uuid4)
    # The activity task name and its kwargs, captured at enqueue time. Stored
    # rather than recomputed: by the time the worker runs, the request is gone
    # and the row may have changed again.
    task = models.CharField(max_length=64)
    payload = models.JSONField()

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending")
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, null=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    lease_token = models.CharField(max_length=64, null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    # Sia, PR 23 review: ForgejoDelivery carries this and AuditOutbox did not.
    # Added NOW rather than inherited-by-omission, because the worker slice wants
    # somewhere to record the created activity id for Traveler linkage, and
    # adding it later is migration churn for no reason.
    result = models.JSONField(null=True, blank=True)

    class Meta:
        verbose_name = "Audit Outbox"
        verbose_name_plural = "Audit Outbox"
        db_table = "audit_outbox"
        ordering = ("created_at",)
        indexes = [
            models.Index(fields=["status", "next_attempt_at"]),
            models.Index(fields=["status", "lease_expires_at"]),
        ]

    def __str__(self):
        return f"{self.task} ({self.status})"
