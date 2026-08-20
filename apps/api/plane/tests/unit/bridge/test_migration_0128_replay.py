# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""BIP-46 PR-B1: literal 0127 -> 0128 migration replay (Morrow RC 3335/3343/3348).

Only a real replay exercises OPERATION ORDERING (backfill runs before
AddConstraint), CONSTRAINT INSTALLATION (a backfill that left two rows sharing a
hash would fail AddConstraint), and the COALESCED-ALIAS backfill (a migrated
non-holder must DEFER execution, never re-run the event).

RUN CONTRACT (Morrow RC 3348 b3) -- a skip is not evidence:
  The repo's pytest addopts carry --nomigrations. django-test-migrations 1.4.0
  SKIPS its `migrator` executor when migrations are disabled; it does NOT force
  them. So these tests cannot run under the default invocation, and are
  DESELECTED from it by the `migration_replay` marker (pytest.ini). Run them
  explicitly, with migrations ON:

      pytest -m migration_replay --migrations \
          plane/tests/unit/bridge/test_migration_0128_replay.py

  The `_require_migrations_enabled` guard below FAILS CLOSED (never skips) if the
  suite is selected without migrations, so a disabled run can never be mistaken
  for a passing one.

django-test-migrations pinned at 1.4.0 (vetted this round; a bump needs a
re-vet). Its `mute_migrate_signals` lacks try/finally, so a raising migration
would leave pre/post_migrate receivers cleared process-wide; the
`_restore_migrate_signals` fixture repairs that unconditionally (Vex vet:
adopt WITH this mitigation)."""

import uuid as uuid_lib

import pytest

pytestmark = pytest.mark.migration_replay

REPO = "acme/x"
REPO_ID = 4242
_PAYLOAD = {
    "repository": {"full_name": REPO, "id": REPO_ID},
    "ref": "refs/heads/main", "before": "b" * 40, "after": "c" * 40, "commits": [],
}


@pytest.fixture(autouse=True)
def _instance_id(settings):
    """The migration namespaces historical rows by the CONFIGURED instance id
    (ADR 010 §1); set it so the backfill keys them instead of leaving them
    unkeyed."""
    settings.FORGEJO_INSTANCE_ID = "forgejo"


@pytest.fixture(autouse=True)
def _require_migrations_enabled(django_db_use_migrations):
    """Fail CLOSED if this suite is selected without migrations -- a skipped
    migration replay is not evidence that it passed (Morrow RC 3348 b3)."""
    if not django_db_use_migrations:
        pytest.fail(
            "migration replay requires migrations ENABLED (got --nomigrations). "
            "Run: pytest -m migration_replay --migrations "
            "plane/tests/unit/bridge/test_migration_0128_replay.py",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _restore_migrate_signals():
    """django-test-migrations 1.4.0's mute_migrate_signals lacks try/finally; a
    RAISING migration (this suite's negative path) would leave pre/post_migrate
    receivers cleared for the whole process, silently poisoning later tests.
    Snapshot and restore unconditionally (Vex source-vet mitigation)."""
    from django.db.models.signals import post_migrate, pre_migrate
    saved_pre = list(pre_migrate.receivers)
    saved_post = list(post_migrate.receivers)
    try:
        yield
    finally:
        pre_migrate.receivers = saved_pre
        post_migrate.receivers = saved_post
        pre_migrate.sender_receivers_cache.clear()
        post_migrate.sender_receivers_cache.clear()


@pytest.mark.django_db
def test_replay_processed_holder_alias_is_FINAL_not_repaired(migrator):
    """Two delivery ids for ONE event, the LATER processed. The migration must
    produce the FINAL alias shape DIRECTLY (Morrow: immediate parity, ADR 010
    §4) — the non-holder is finalized to PROCESSED with the holder's result, NOT
    left pending for a later reconciler to repair. A repair step would prove the
    reconciler works, not that the migration wrote the identical shape."""
    old = migrator.apply_initial_migration(("db", "0127_audit_outbox"))
    Delivery = old.apps.get_model("db", "ForgejoDelivery")
    early_pending = Delivery.objects.create(
        delivery_id=str(uuid_lib.uuid4()), forge="forgejo", event="push",
        payload=_PAYLOAD, repository=REPO, body_digest="same", status="pending",
    )
    later_processed = Delivery.objects.create(
        delivery_id=str(uuid_lib.uuid4()), forge="forgejo", event="push",
        payload=_PAYLOAD, repository=REPO, body_digest="same", status="processed",
        result={"moved": ["GB-1"]},
    )
    new = migrator.apply_tested_migration(("db", "0128_forgejodelivery_semantic_key"))
    After = new.apps.get_model("db", "ForgejoDelivery")
    rows = {r.delivery_id: r for r in After.objects.all()}
    assert len(rows) == 2
    e = rows[str(early_pending.delivery_id)]    # the non-holder alias
    p = rows[str(later_processed.delivery_id)]  # the holder
    assert p.semantic_key_hash is not None and e.semantic_key_hash is None
    assert e.semantic_key == p.semantic_key
    # FINAL shape, produced by the migration itself -- no process_delivery repair:
    assert e.status == "processed", "processed-holder alias is FINALIZED by the migration, not left pending"
    assert (e.result or {}).get("coalesced_to") == str(later_processed.delivery_id)
    assert (e.result or {}).get("moved") == ["GB-1"], "finalized to the holder's result"
    assert e.processed_at is not None

    # driving the FINALIZED alias through the real processor is a NO-OP: the
    # reconciler does not re-claim a terminal row -- there is nothing to repair.
    from plane.bridge import forgejo_bridge as fb
    from plane.db.models import ForgejoDelivery as RealFD
    alias = RealFD.objects.get(delivery_id=str(early_pending.delivery_id))
    assert fb._is_alias(alias)
    assert fb.claim_delivery(alias) is None, "a finalized alias is not re-claimed by the reconciler"


@pytest.mark.django_db
def test_replay_pending_holder_alias_stays_pending_for_the_reconciler(migrator):
    """When NO row is processed at migration time, the migration must NOT
    fabricate a result: the alias is left PENDING with the coalesced_to pointer
    (exactly what post() writes), for the reconciler to finalize once the holder
    completes."""
    old = migrator.apply_initial_migration(("db", "0127_audit_outbox"))
    Delivery = old.apps.get_model("db", "ForgejoDelivery")
    holder_pending = Delivery.objects.create(
        delivery_id=str(uuid_lib.uuid4()), forge="forgejo", event="push",
        payload=_PAYLOAD, repository=REPO, body_digest="same", status="pending",
    )
    later_dupe = Delivery.objects.create(
        delivery_id=str(uuid_lib.uuid4()), forge="forgejo", event="push",
        payload=_PAYLOAD, repository=REPO, body_digest="same", status="pending",
    )
    new = migrator.apply_tested_migration(("db", "0128_forgejodelivery_semantic_key"))
    After = new.apps.get_model("db", "ForgejoDelivery")
    rows = {r.delivery_id: r for r in After.objects.all()}
    h = rows[str(holder_pending.delivery_id)]
    a = rows[str(later_dupe.delivery_id)]
    assert h.semantic_key_hash is not None, "earliest holds the hash when none is processed"
    assert a.semantic_key_hash is None
    assert a.status == "pending", "alias with an unfinished holder stays pending"
    assert (a.result or {}).get("coalesced_to") == str(holder_pending.delivery_id)
    assert "moved" not in (a.result or {}), "no fabricated result while the holder is unfinished"


@pytest.mark.django_db
def test_replay_installs_unique_constraint_that_then_rejects_a_duplicate(migrator):
    """After the replay, the partial unique constraint is live: a second row with
    the same non-null hash is rejected. Proves the constraint installed, not
    merely that the backfill ran."""
    from django.db import IntegrityError, transaction

    new = migrator.apply_tested_migration(("db", "0128_forgejodelivery_semantic_key"))
    Delivery = new.apps.get_model("db", "ForgejoDelivery")
    Delivery.objects.create(
        delivery_id=str(uuid_lib.uuid4()), forge="forgejo", event="push", payload=_PAYLOAD,
        repository=REPO, body_digest="d", status="processed",
        semantic_key="k", semantic_key_hash="deadbeef" * 8,
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Delivery.objects.create(
                delivery_id=str(uuid_lib.uuid4()), forge="forgejo", event="push", payload=_PAYLOAD,
                repository=REPO, body_digest="d", status="processed",
                semantic_key="k", semantic_key_hash="deadbeef" * 8,
            )
