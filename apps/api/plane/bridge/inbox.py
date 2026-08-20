# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""THE observation inbox: one owner for the ADR 010 holder/alias lifecycle.

WHY THIS FILE EXISTS (BIP-56, Morrow RC 3536).

ADR 010 §3/§4 settle how an observation is stored: every observation — webhook
or poll — is durably inserted as its own row; the HOLDER owns the unique
semantic hash and executes; a NON-HOLDER alias keeps its plaintext key, carries
a NULL hash and a ``coalesced_to`` pointer, and never executes. Coalescing
applies to EXECUTION, not to storage.

That contract was implemented correctly and reviewed three times — **inside the
webhook view**, interleaved with ``Response(...)`` across ~120 lines. It was
therefore not callable, and the second transport (BIP-46's poller) re-implemented
it rather than consuming it. That is a property of the code's shape, not of the
poller: **any third consumer would have done the same.**

So the lifecycle moves here, returning a DOMAIN result. The view maps that
result to its HTTP statuses; the poller calls the same function. One owner,
invariant 8, and Morrow's savepoint requirement satisfied in the single place
it lives.

WHAT THIS DELIBERATELY DOES NOT DO: it does not decide policy. It does not
process, lease, resolve push ranges, or map anything to a status code. It
answers one question — *what happened when I tried to store this observation
under its semantic identity?* — and the caller decides what that means for its
transport.
"""

from dataclasses import dataclass
from typing import Any, Optional

from django.db import IntegrityError, transaction

from plane.db.models import ForgejoDelivery

#: The observation was stored as a new row holding the semantic key. Caller
#: proceeds to process it.
CREATED = "created"
#: The delivery_id already existed with identical content and is not an alias.
#: Caller decides based on its status (it may already be processed).
EXISTING = "existing"
#: The observation is a non-holder alias of an event another row holds. It is
#: durably stored; EXECUTION belongs to the holder. Never process an alias.
ALIAS = "alias"
#: The delivery_id already existed carrying DIFFERENT content. Fail closed —
#: the race loser must not masquerade as coalesced (Morrow RC 3348 b2).
COLLISION = "collision"


@dataclass(frozen=True)
class Recorded:
    """What happened when one observation was stored. No transport in it."""

    outcome: str
    #: The row this observation is (holder, alias, or the pre-existing row).
    #: None only for COLLISION, where nothing of ours was written.
    delivery: Optional[Any] = None
    #: For ALIAS: the holder's delivery_id, when a holder was found.
    coalesced_to: Optional[str] = None

    @property
    def is_alias(self) -> bool:
        return self.outcome == ALIAS


def is_alias(row) -> bool:
    """A coalesced NON-HOLDER observation: its own audit row that defers
    execution to the holder of the semantic event (Morrow RC 3348).

    Lives here rather than in the webhook module because CLASSIFYING a stored
    observation is part of owning the lifecycle. Leaving it outside was the
    defect Rowan found in the first extraction (RC 3541): `record_observation`
    returned EXISTING for the retry of an already-durable alias, and the
    webhook path stayed correct only because it separately re-inspected the
    row. A domain result that needs the caller to re-derive the thing it just
    stored is not safe for a second consumer — which is the entire reason this
    seam exists.
    """
    return bool((row.result or {}).get("coalesced_to")) or (
        row.semantic_key_hash is None
        and isinstance(row.result, dict)
        and "coalesced_to" in row.result
    )


def _content_differs(row, *, event, payload_repo, digest, forge_name) -> bool:
    """Delivery-id binding: a reused id must carry the SAME content."""
    return (
        row.body_digest != digest
        or row.event != event
        or row.repository != payload_repo
        or row.forge != forge_name
    )


def record_observation(
    *,
    delivery_id,
    event,
    payload,
    repository,
    digest,
    forge_name,
    canonical_key,
    key_hash,
) -> Recorded:
    """Store ONE observation under its semantic identity. The only writer.

    Delivery-id binding is enforced FIRST: an existing ``delivery_id`` is found
    by ``get_or_create``, and a reused id carrying new content is a COLLISION
    before any semantic coalescing (Morrow 3329 b1). Semantic coalescing happens
    ONLY when a genuinely new ``delivery_id`` carries an event already stored
    under another id — the unique constraint raises and we store the alias.

    Both inserts sit in their OWN explicit savepoint (ADR 010 §6). The
    ``IntegrityError`` below is caught and recovered from, so the raising insert
    must be savepointed — otherwise, under any enclosing ``atomic`` (an outer
    request transaction, or a caller's own block), the caught error poisons the
    transaction and the recovery path fails. Explicit, so it does not depend on
    ``get_or_create``'s internals; never a bare ``create()`` in this path.
    """
    defaults = {
        "event": event,
        "payload": payload,
        "repository": repository,
        "body_digest": digest,
        "forge": forge_name,
        "semantic_key": canonical_key,
    }
    try:
        with transaction.atomic():
            delivery, created = ForgejoDelivery.objects.get_or_create(
                delivery_id=delivery_id,
                defaults={**defaults, "semantic_key_hash": key_hash},
            )
    except IntegrityError:
        # A NEW delivery_id carrying an event already HELD by another row.
        # Provider delivery ids are audit provenance (M3): store THIS
        # observation durably as its own NON-HOLDER row (hash NULL) and coalesce
        # EXECUTION to the holder. The alias is PENDING with a coalesced_to
        # pointer — never a snapshot of the holder's result — so a retry always
        # resolves the holder's CURRENT outcome (Morrow RC 3348 b3).
        holder = ForgejoDelivery.objects.filter(semantic_key_hash=key_hash).first()
        with transaction.atomic():
            alias, created_alias = ForgejoDelivery.objects.get_or_create(
                delivery_id=delivery_id,
                defaults={
                    **defaults,
                    "semantic_key_hash": None,
                    "status": "pending",
                    # "coalesced_to" is RESERVED to this seam: it is THE alias
                    # discriminator (is_alias above). Every other result write
                    # goes through bridge/delivery_result.py, whose
                    # constructor cannot emit this key.
                    "result": {"coalesced_to": holder.delivery_id if holder else None},
                },
            )
        if not created_alias and _content_differs(
            alias, event=event, payload_repo=repository, digest=digest, forge_name=forge_name
        ):
            return Recorded(COLLISION)
        return Recorded(
            ALIAS, delivery=alias, coalesced_to=holder.delivery_id if holder else None
        )

    if not created and _content_differs(
        delivery, event=event, payload_repo=repository, digest=digest, forge_name=forge_name
    ):
        return Recorded(COLLISION)

    if created:
        return Recorded(CREATED, delivery=delivery)

    # TOTAL CLASSIFICATION (Rowan RC 3541). The row already existed with
    # identical content — but "already existed" does not mean "is a holder".
    # A retry of a durable alias lands here, and reporting it as EXISTING made
    # the caller responsible for noticing, which the poller would not have.
    if is_alias(delivery):
        return Recorded(
            ALIAS, delivery=delivery,
            coalesced_to=(delivery.result or {}).get("coalesced_to"),
        )
    return Recorded(EXISTING, delivery=delivery)
