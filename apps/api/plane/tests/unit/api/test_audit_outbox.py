# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The audit intent is a ROW, written in the mutation's transaction.

BIP-18. `issue_activity.delay(...)` sends a message to a broker, which is not a
database write. `on_commit` fixes two of the three failures — running before
the commit, and firing for a rolled-back mutation — but it cannot fix the
third: a broker outage AFTER commit loses the audit silently, because the thing
being deferred is still an external side effect.

So the authoritative artifact is a row committed with the mutation, and the
worker wake-up becomes an optimisation. These pin that, including the case that
makes it worth having at all: the mutation rolls back, and its audit intent
goes with it.
"""

import pytest
from django.db import transaction

from plane.api.audit import AuditOutsideTransactionError, enqueue_audit
from plane.db.models import AuditOutbox, User

MARKER = "bip18-outbox-probe@biplane.invalid"


@pytest.fixture(autouse=True)
def _clean():
    AuditOutbox.objects.filter(task="probe").delete()
    User.objects.filter(email=MARKER).delete()
    yield
    AuditOutbox.objects.filter(task="probe").delete()
    User.objects.filter(email=MARKER).delete()


@pytest.mark.django_db(transaction=True)
def test_it_refuses_to_run_outside_a_transaction():
    """The guarantee is meaningless without one, so this fails loudly.

    Degrading quietly here would silently restore the behaviour this ticket
    removed — an audit row that commits on its own, describing a change that
    may never happen.
    """
    with pytest.raises(AuditOutsideTransactionError):
        enqueue_audit("probe", issue_id="x")

    assert AuditOutbox.objects.filter(task="probe").count() == 0


@pytest.mark.django_db(transaction=True)
def test_it_writes_a_pending_row_inside_a_transaction():
    with transaction.atomic():
        row = enqueue_audit("probe", issue_id="x", actor_id="y")

    stored = AuditOutbox.objects.get(pk=row.pk)
    assert stored.status == "pending"
    assert stored.payload == {"issue_id": "x", "actor_id": "y"}
    assert stored.attempts == 0
    assert stored.event_key, "a durable event handle must be assigned at enqueue"


@pytest.mark.django_db(transaction=True)
def test_the_audit_intent_ROLLS_BACK_WITH_THE_MUTATION():
    """The case the whole design exists for.

    A broker message cannot be un-sent. A row can.
    """
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            User.objects.create(email=MARKER, username=MARKER, display_name="probe")
            enqueue_audit("probe", issue_id="x")
            raise RuntimeError("the mutation failed after the audit was recorded")

    assert User.objects.filter(email=MARKER).count() == 0
    assert AuditOutbox.objects.filter(task="probe").count() == 0, "audit intent outlived its mutation"


@pytest.mark.django_db(transaction=True)
def test_a_committed_mutation_keeps_exactly_one_intent():
    """The other half — without it, refusing to write anything would pass above."""
    with transaction.atomic():
        User.objects.create(email=MARKER, username=MARKER, display_name="probe")
        enqueue_audit("probe", issue_id="x")

    assert User.objects.filter(email=MARKER).count() == 1
    assert AuditOutbox.objects.filter(task="probe").count() == 1


@pytest.mark.django_db(transaction=True)
def test_event_keys_are_unique_per_row():
    """A durable per-intent handle. Two intents must not collide."""
    with transaction.atomic():
        a = enqueue_audit("probe", n=1)
        b = enqueue_audit("probe", n=2)

    assert a.event_key != b.event_key


@pytest.mark.django_db
def test_event_key_type_and_value_survive_save_and_refresh():
    # Morrow (PR 23 preflight): as a CharField with a uuid4 default, a fresh
    # instance held a UUID object while the reloaded row held a string — the
    # event handle changed Python type across save/refresh, and any
    # future JSON task use would serialize it inconsistently. As a UUIDField
    # the type is UUID on both sides of the round trip, same value.
    import uuid as uuid_lib

    from django.db import transaction as dj_transaction

    with dj_transaction.atomic():
        row = enqueue_audit("issue_activity", probe="parity")
    before = row.event_key
    assert isinstance(before, uuid_lib.UUID)
    row.refresh_from_db()
    assert isinstance(row.event_key, uuid_lib.UUID)
    assert row.event_key == before
