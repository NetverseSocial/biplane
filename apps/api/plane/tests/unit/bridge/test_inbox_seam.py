# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The inbox seam's own contract (BIP-56).

WHY THESE EXIST, AND WHAT THEY DO NOT PROVE.

The extraction came back byte-identical to main under the existing bridge
suites — 947 passed, same failure set in both directions. That is necessary and
not sufficient: identical results only mean something once the suite can detect
a broken extraction. Mutating the seam showed it can, for the parts that
matter — reporting an alias as a fresh holder reddens 6, skipping the reused-id
content check reddens 5.

Two mutants SURVIVED, and I chased them rather than filing a coverage gap:
removing either explicit ``transaction.atomic()`` changes nothing. They are
**equivalent mutants**. Django's ``QuerySet.get_or_create`` already wraps its
``create()`` in ``transaction.atomic(using=self.db)`` — read from the installed
source, not assumed — so the explicit savepoint is insurance against that
internal changing, which is exactly what the original comment in the webhook
path claimed it was for. It is not currently load-bearing and **no test here
pins it**; saying otherwise would be the false-rationale failure this file
exists to avoid.

What these rows DO pin is the contract the seam owes both transports: both
observations queryable, the alias keeping its plaintext key and pointer, the
holder owning the hash, and a reused delivery id with new content failing
closed on the alias path exactly as on the normal one.
"""

import uuid as uuid_lib

import pytest
from django.db import transaction

from plane.bridge import inbox
from plane.db.models import ForgejoDelivery

KEY = "instance\x1frepo\x1fref\x1fbefore\x1fafter"
HASH = "a" * 64


def _obs(delivery_id, digest="d1", event="push", repo="acme/app", key_hash=HASH):
    return dict(
        delivery_id=delivery_id, event=event, payload={"x": 1}, repository=repo,
        digest=digest, forge_name="github", canonical_key=KEY, key_hash=key_hash,
    )


@pytest.mark.django_db(transaction=True)
def test_alias_recovery_survives_an_ENCLOSING_atomic_block():
    """Alias recovery must WORK under an enclosing atomic — ADR 010 §6.

    Four things, kept apart because an earlier version of this docstring ran
    two of them together and contradicted the module header three lines later
    (Morrow RC 3550, Aria):

    1. **Recovery under an enclosing transaction IS pinned.** An observation of
       an already-held event, recorded inside a caller's own transaction,
       becomes a durable alias with a pointer, and that transaction is still
       usable afterwards. This is the property a second consumer depends on.
    2. **`get_or_create` supplies the load-bearing savepoint today.** It wraps
       its `create()` in `transaction.atomic(using=self.db)` — read from the
       installed Django source, not assumed.
    3. **The explicit wrappers in `record_observation` are deliberate but
       UNPINNED insurance** against that internal changing. Removing either one
       alone leaves this test, and everything else, green.
    4. **The bare-`create()` mutant is the one that reds** — the raising insert
       with no savepoint of any kind. Executed: `IntegrityError` on the unique
       constraint, then
       `django.db.transaction.TransactionManagementError: An error occurred in
       the current transaction.`

    So "without a savepoint" and "without the EXPLICIT savepoint" are different
    mutants with different results, and only the first reds.
    """
    first = inbox.record_observation(**_obs(str(uuid_lib.uuid4())))
    assert first.outcome == inbox.CREATED

    with transaction.atomic():          # the enclosing block is the point
        second = inbox.record_observation(**_obs(str(uuid_lib.uuid4())))
        assert second.is_alias
        # The transaction must still be USABLE. This assertion reds when the
        # raising insert has NO savepoint at all — a bare create() gives
        # TransactionManagementError here, executed. It does NOT red when only
        # the EXPLICIT atomic() is removed, because get_or_create supplies one
        # internally; the explicit wrapper is deliberate but unpinned insurance.
        assert ForgejoDelivery.objects.count() == 2
        assert second.coalesced_to


@pytest.mark.django_db(transaction=True)
def test_both_observations_remain_queryable_holder_and_alias():
    """ADR 010 §3/§4: coalescing applies to EXECUTION, not to storage."""
    a = str(uuid_lib.uuid4())
    b = str(uuid_lib.uuid4())
    inbox.record_observation(**_obs(a))
    second = inbox.record_observation(**_obs(b))

    holder = ForgejoDelivery.objects.get(semantic_key_hash=HASH)
    alias = ForgejoDelivery.objects.get(semantic_key_hash__isnull=True)
    assert holder.delivery_id == a
    assert alias.delivery_id == b
    assert alias.semantic_key == KEY, "the alias keeps its plaintext key"
    assert (alias.result or {}).get("coalesced_to") == a
    assert second.coalesced_to == a
    assert ForgejoDelivery.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_a_reused_delivery_id_with_new_content_is_a_collision_on_both_paths():
    """The race loser must not masquerade as coalesced (Morrow RC 3348 b2)."""
    a = str(uuid_lib.uuid4())
    inbox.record_observation(**_obs(a))
    # same id, different body: collision on the normal path
    assert inbox.record_observation(**_obs(a, digest="CHANGED")).outcome == inbox.COLLISION
    # and on the alias path: an existing alias id re-presented with new content
    b = str(uuid_lib.uuid4())
    assert inbox.record_observation(**_obs(b)).is_alias
    assert inbox.record_observation(**_obs(b, digest="CHANGED")).outcome == inbox.COLLISION


@pytest.mark.django_db(transaction=True)
def test_the_same_delivery_id_with_identical_content_is_EXISTING_not_a_duplicate_row():
    a = str(uuid_lib.uuid4())
    assert inbox.record_observation(**_obs(a)).outcome == inbox.CREATED
    again = inbox.record_observation(**_obs(a))
    assert again.outcome == inbox.EXISTING
    assert ForgejoDelivery.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_the_retry_of_a_durable_alias_is_classified_ALIAS_not_EXISTING():
    """Rowan RC 3541 — the extraction's own instance of the defect it removed.

    Holder A is CREATED. B is ALIAS. B retried with identical content finds its
    own durable row, raises no IntegrityError, and fell through to EXISTING —
    so the domain result said "an ordinary pre-existing row" about an alias.

    The webhook path stayed correct only because it separately re-inspected the
    row with _is_alias. A result that requires the caller to re-derive what the
    seam just stored is not safe for a second consumer, and the poller — which
    has no such inspection — would have executed it.
    """
    a, b = str(uuid_lib.uuid4()), str(uuid_lib.uuid4())
    assert inbox.record_observation(**_obs(a)).outcome == inbox.CREATED

    first = inbox.record_observation(**_obs(b))
    assert first.is_alias and first.coalesced_to == a

    retry = inbox.record_observation(**_obs(b))
    assert retry.outcome == inbox.ALIAS, "a durable alias retried was reported as a holder"
    assert retry.is_alias
    assert retry.coalesced_to == a, "the retry must still name its holder"
    assert ForgejoDelivery.objects.count() == 2, "the retry must not create a third row"


@pytest.mark.django_db(transaction=True)
def test_classification_is_total_no_caller_re_inspection_required():
    """Every outcome is decidable from the result alone.

    The point of the seam is that a consumer never re-derives what was stored.
    A holder is never is_alias; an alias always is, first time and on retry.
    """
    a, b = str(uuid_lib.uuid4()), str(uuid_lib.uuid4())
    holder = inbox.record_observation(**_obs(a))
    alias = inbox.record_observation(**_obs(b))
    holder_retry = inbox.record_observation(**_obs(a))
    alias_retry = inbox.record_observation(**_obs(b))

    assert (holder.is_alias, alias.is_alias) == (False, True)
    assert (holder_retry.is_alias, alias_retry.is_alias) == (False, True)
    assert holder_retry.outcome == inbox.EXISTING
    assert alias_retry.outcome == inbox.ALIAS


@pytest.mark.django_db(transaction=True)
def test_an_orphaned_alias_stays_an_alias_and_never_promotes_itself():
    """Aria's case, raised on the #70 seat — double execution through a
    different door, and currently fail-closed. Committed so it stays that way.

    If the holder row vanishes, the alias points at nothing. The dangerous
    behaviour would be noticing the hash is unclaimed and promoting itself to
    an executable holder: if the original holder had already executed, the
    event runs twice. Staying an alias means it does not execute again — the
    reconciler's problem, loudly, rather than the lifecycle's silently.

    Note the shape: NOT a nominated property, and not reachable by testing that
    storage is lossless. It asks what the interface does when its own invariant
    is already broken.
    """
    a, b = str(uuid_lib.uuid4()), str(uuid_lib.uuid4())
    inbox.record_observation(**_obs(a))
    alias = inbox.record_observation(**_obs(b))
    assert alias.is_alias and alias.coalesced_to == a

    ForgejoDelivery.objects.filter(delivery_id=a).delete()   # holder vanishes
    assert not ForgejoDelivery.objects.filter(semantic_key_hash=HASH).exists()

    orphaned = inbox.record_observation(**_obs(b))
    assert orphaned.outcome == inbox.ALIAS, "an orphaned alias promoted itself to a holder"
    assert orphaned.is_alias
    assert orphaned.coalesced_to == a, "it still names the holder it was coalesced to"
    assert ForgejoDelivery.objects.count() == 1, "no replacement holder was minted"
