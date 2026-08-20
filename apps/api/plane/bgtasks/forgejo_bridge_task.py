# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Reconciler for the git-bridge delivery inbox.

Forgejo never auto-retries a failed webhook delivery, so the bridge owns
retry: this task claims due pending rows — and `processing` rows whose lease
has EXPIRED (a worker crashed mid-flight) — and re-processes them. Claims are
atomic and completion is conditioned on lease ownership, so overlapping beat
ticks or workers cannot double-process. Restart-safe by construction: the row
is the only state."""

# Python imports
import logging

# Third party imports
from celery import shared_task

# Django imports
from django.db.models import Q
from django.utils import timezone

# Module imports
from plane.db.models import ForgejoDelivery

logger = logging.getLogger("plane.worker")

RECONCILE_BATCH_SIZE = 100


@shared_task
def reconcile_forgejo_deliveries():
    from plane.bridge.forgejo_bridge import claim_delivery, process_delivery

    now = timezone.now()
    due = ForgejoDelivery.objects.filter(
        Q(status="pending") & (Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        | Q(status="processing", lease_expires_at__lt=now)
    ).order_by("created_at")[:RECONCILE_BATCH_SIZE]
    recovered = 0
    for delivery in due:
        lease = claim_delivery(delivery)
        if lease is None:
            continue  # another worker owns it
        delivery.refresh_from_db()
        try:
            process_delivery(delivery, lease)
            recovered += 1
        except Exception as e:
            # process_delivery already recorded attempts/error/backoff
            logger.info(f"git-bridge reconcile: {delivery.delivery_id} still pending: {e}")
    if recovered:
        logger.info(f"git-bridge reconcile: recovered {recovered} pending deliveries")
    return recovered
