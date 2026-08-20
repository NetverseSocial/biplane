# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Django imports
from django.db import models

# Module imports
from .base import BaseModel


class ForgejoDelivery(BaseModel):
    """Durable inbox for Forgejo webhook deliveries (git bridge).

    Forgejo 15.x marks a delivery delivered BEFORE the HTTP request and never
    auto-retries a 5xx (services/webhook/deliver.go), so the bridge persists
    every signed, well-formed delivery before processing and owns retry
    itself: a reconciliation task re-processes due rows with backoff.

    Ownership: work is CLAIMED atomically into `processing` with a lease
    token and expiry; completion/failure writes are conditioned on still
    holding the token, so overlapping workers, duplicate POSTs, and stale
    processors cannot double-process or overwrite a finished row. Expired
    leases are recovered by the reconciler (crash/restart safety).

    Idempotency: keyed by the Forgejo delivery uuid, BOUND to the event,
    repository, and an HMAC-covered body digest — a reused delivery id with
    different content fails closed instead of colliding.
    """

    STATUS_CHOICES = (
        ("pending", "pending"),
        ("processing", "processing"),
        ("processed", "processed"),
    )

    delivery_id = models.CharField(max_length=128, unique=True)
    # Which forge personality sent it (BIP-15): the processing path rehydrates
    # this via forges.by_name to read the payload in the sender's field names.
    # Rows from before the column exist are Forgejo deliveries, hence default.
    forge = models.CharField(max_length=32, default="forgejo")
    event = models.CharField(max_length=32)
    payload = models.JSONField()
    repository = models.CharField(max_length=512)
    body_digest = models.CharField(max_length=64)
    # Provider-qualified semantic event key (BIP-46): the SAME real git event
    # observed by webhook or poll computes the SAME key from immutable content
    # (see plane.bridge.semantic_key), so duplicate observations collapse to
    # one outcome. Plaintext is KEPT (audit answerability — "which event was
    # this?"); the sha256 hash is the unique dedup index.
    #
    # NULLABLE CLASSES ARE PERMANENT, not a backfill window (Morrow 3329 b4):
    #   - semantic_key is NULL for an event with no dedupable transition (an
    #     unmerged PR close) or whose repo carries no stable id — these are the
    #     runtime invariant too, not just historical rows.
    #   - semantic_key_hash is additionally NULL on the NON-holder rows of a
    #     pre-BIP-46 group that shared one real event under different delivery
    #     ids: the PROCESSED row (authoritative outcome; else earliest) holds
    #     the hash, the rest retain the plaintext key
    #     for audit but a NULL hash (the partial unique constraint permits it).
    #     A LIVE second observation (webhook/poll) of an already-held event is
    #     likewise stored as its own NULL-hash row (status processed, result
    #     coalesced_to the holder) — every delivery id is durable (M3).
    # So this is unique-where-not-null, never "every row filled".
    semantic_key = models.TextField(null=True, blank=True)
    semantic_key_hash = models.CharField(max_length=64, null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending")
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, null=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    lease_token = models.CharField(max_length=64, null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    result = models.JSONField(null=True, blank=True)

    class Meta:
        verbose_name = "Forgejo Delivery"
        verbose_name_plural = "Forgejo Deliveries"
        db_table = "forgejo_deliveries"
        ordering = ("created_at",)
        indexes = [
            models.Index(fields=["status", "next_attempt_at"]),
            models.Index(fields=["status", "lease_expires_at"]),
            models.Index(fields=["semantic_key_hash"], name="db_forgejod_semanti_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["semantic_key_hash"],
                condition=models.Q(semantic_key_hash__isnull=False),
                name="uniq_forgejo_delivery_semantic_key_hash",
            ),
        ]

    def __str__(self):
        return f"{self.delivery_id} ({self.event}, {self.status})"
