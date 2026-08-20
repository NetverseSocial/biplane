# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Django imports
from django.db import models

# Module imports
from .base import BaseModel


class BoardOperation(BaseModel):
    """The M8 outcome ledger (BIP-37; docs/board-service-design.md).

    One row per board mutation, committed IN THE SAME TRANSACTION as the
    mutation it records (§M8.2): an outcome-less mutation and a post-commit
    outcome write are both contract violations. Because of that coupling,
    404-on-key is a SAFE answer — "never committed" — which is what makes
    query-before-retry (§M8.1) sound.

    Identity: `(principal, op_key)` unique together. The key is minted and
    persisted by the CALLER before the call; the principal scope means one
    agent's replay can neither collide with nor read another's outcome. The
    canonical request digest binds that key to its source, scope, verb and
    payload, so reusing a key for different work is a conflict, not a replay.
    Nothing in this model decides policy — it is a ledger, not an authority.
    """

    # The immutable principal the operation is scoped by — the token identity
    # (M7 binds it server-side; asserted identities never reach here).
    principal = models.CharField(max_length=255)
    # Caller-durable idempotency key, persisted caller-side BEFORE the call.
    op_key = models.CharField(max_length=255)
    source = models.CharField(max_length=64)
    request_digest = models.CharField(max_length=64)
    verb = models.CharField(max_length=64)
    workspace_slug = models.CharField(max_length=255)
    project_identifier = models.CharField(max_length=255, blank=True)
    # The stored outcome returned verbatim on replay (§M8.1) — never re-executed.
    outcome = models.JSONField(default=dict)

    class Meta:
        db_table = "board_operations"
        verbose_name = "Board Operation"
        verbose_name_plural = "Board Operations"
        constraints = [
            models.UniqueConstraint(fields=["principal", "op_key"], name="board_operation_principal_op_key_uniq")
        ]
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.principal}:{self.op_key} ({self.verb})"
