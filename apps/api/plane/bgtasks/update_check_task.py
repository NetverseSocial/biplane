# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Scheduled update check (BIP-41). Thin: the service owns everything.

THIS module owns the cadence (UPDATE_CHECK_INTERVAL_SECONDS below);
plane/celery.py and the service's cache TTL are consumers of it. Failure
inside the check is already converted to an honest UNKNOWN payload by the
service; this wrapper exists only so celery has a task to schedule."""

from celery import shared_task

#: THE update-check cadence — the single authority (Morrow on #54: a `3600`
#: in the service and a crontab in celery.py were two authorities for one
#: fact). plane/celery.py builds the beat schedule from THIS value and the
#: service derives its cache TTL from it; changing the cadence is one edit.
UPDATE_CHECK_INTERVAL_SECONDS = 3600


@shared_task
def run_update_check():
    from plane.license.services.update_check import run_update_check as _run

    payload = _run()
    try:
        _maybe_auto_apply(payload)
    except Exception:  # noqa: BLE001 — the auto path must never fail the check
        import logging

        logging.getLogger("plane.license").exception("auto-apply hook failed")
    return payload["state"]


def _maybe_auto_apply(payload):
    """The automatic mode (ticket 69): apply-on-flag, OFF by default.

    Config-gated (BIPLANE_APPLY_AUTO), and attempted AT MOST ONCE per flagged
    tag — the attempt is recorded durably before the request is sent, so an
    apply that fails does not retry itself every hour into a crash-restore
    loop; a failed auto-apply is an operator's decision to retry, via the
    button or the host log. Refusals (full level, unconfigured, nothing
    flagged) are the shared client's; this function adds only the gate and
    the once-per-tag guard, and logs every outcome because the beat task has
    no other voice."""
    # The switch lives in Settings → Updates (InstanceConfiguration), with the
    # env var as a deployment-level force-on (John's design, 2026-08-16). One
    # authority: auto_apply_enabled().
    from plane.license.api.views.auto_apply_setting import auto_apply_enabled

    if not auto_apply_enabled():
        return
    if payload.get("state") != "update_available":
        return
    tag = (payload.get("latest_release") or {}).get("tag")
    if not tag:
        return

    import logging

    from django.db import IntegrityError, transaction

    from plane.license.models import BiplaneAutoApplyAttempt
    from plane.license.services import apply_client

    logger = logging.getLogger("plane.license")
    # Configuration is checked BEFORE the guard is written (Sable RC 3826 #3):
    # an operator who enables auto before configuring the applier must not
    # silently burn the once-per-tag guard on a request that can never leave.
    if not apply_client.is_configured():
        logger.warning("auto-apply is on but no applier is configured; not attempting %s", tag)
        return
    # Claim BEFORE sending, as an INSERT into a unique column — not a
    # compare-and-set on a single field. The CAS regressed under
    # newer -> stale-worker -> newer ordering (Rowan 3834): the stale
    # worker's exclude matched against the newer value, rolled the guard
    # backward, and the newer tag became attemptable again. An append-only
    # row cannot regress; the database enforces exactly-once whatever order
    # workers arrive in, and IntegrityError means someone else already
    # attempted it — never ours to send.
    try:
        with transaction.atomic():
            BiplaneAutoApplyAttempt.objects.create(tag=tag)
    except IntegrityError:
        return
    verdict = apply_client.request_apply_of_flagged(status_payload=payload)
    logger.info("auto-apply for %s: %s", tag, verdict.get("kind"))
    if verdict.get("kind") != apply_client.REQUESTED:
        logger.warning("auto-apply refused for %s: %s", tag, verdict.get("detail"))
