# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The one door for recording an audit event (BIP-18).

LIVE (updated with the worker + call-site slice): the 23 audit call sites on
the token API call this door, and `drain_audit_outbox` consumes the rows.
Audit call sites do not talk to the broker at all — they call
`enqueue_audit(...)`, which writes a durable row inside the caller's
transaction. The worker is woken afterwards as an optimisation; the row is
the truth.

Non-audit work (webhook and functional tasks) deliberately still uses
`dispatch_after_commit` — post-commit and best-effort. That partition is
approved, not an oversight, and is pinned by the gate in
`test_no_bare_dispatch_gate.py`.

WHY THIS REFUSES TO RUN OUTSIDE A TRANSACTION

The whole guarantee is "the audit intent commits with the mutation, or neither
does". Outside an atomic block that sentence is false — the row commits on its
own the moment it is written, and a later failure leaves an audit for a change
that never happened. That is one of the exact defects this ticket exists to
close, so it fails loudly rather than degrading quietly.

It is a programming error, not a runtime condition: it means a call site was
added outside the base transaction boundary. An invisible degrade here would
mean an outage silently reverting us to the behaviour we just removed.
"""

import logging

from django.db import transaction

from plane.db.models import AuditOutbox

logger = logging.getLogger("plane.api")


class AuditOutsideTransactionError(RuntimeError):
    """enqueue_audit() was called with no transaction open."""


def enqueue_audit(task: str, **payload) -> AuditOutbox:
    """Record an audit event durably, in the caller's transaction.

    :param task: the activity task this row will drive, e.g. "issue_activity"
    :param payload: the kwargs that task will be called with
    :raises AuditOutsideTransactionError: if no transaction is open
    """
    # Reads the DEFAULT connection, so this pairing assumes the mutation and the
    # outbox row share one database alias. True today - ReadReplicaRouter sends
    # every write to the primary - and it would stop being true the moment a
    # write were routed elsewhere (Sia, PR 23 review).
    if not transaction.get_connection().in_atomic_block:
        raise AuditOutsideTransactionError(
            f"enqueue_audit({task!r}) was called outside a transaction. The audit row must "
            "commit with the mutation it describes, or neither should. Unsafe API requests get "
            "their transaction from MutationDispatchMixin; a call site outside that boundary "
            "needs its own atomic block, not a relaxation of this check."
        )

    row = AuditOutbox.objects.create(task=task, payload=payload)

    # Wake the drain worker once this transaction commits, so audit latency
    # stays what it was before the outbox (Morrow): the every-minute beat is
    # RECOVERY — for a lost wake-up, a crashed worker, or a broker outage — not
    # the normal path. robust=True and a swallowed failure on purpose: the row
    # is already durable, so a broker that cannot take the wake must not turn a
    # committed mutation into a 500. The beat picks it up.
    def _wake():
        try:
            from plane.bgtasks.audit_outbox_task import drain_audit_outbox

            drain_audit_outbox.delay()
        except Exception:  # noqa: BLE001 - the row is the truth; the beat recovers
            logger.warning("audit-outbox: wake-up dispatch failed; the beat will drain it")

    transaction.on_commit(_wake, robust=True)
    return row
