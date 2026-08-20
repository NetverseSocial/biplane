# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Run the update check and store its honest result (M5.2, BIP-41).

ONE checker: release metadata comes through
`plane.license.utils.release_source` (Forgejo-preferred, GitHub-mirror
fallback, per the owner ruling) over the bounded transport (origin allowlist,
streamed size cap, validated redirects, wall-clock deadline). The decision is
a semantic-version comparison in `plane.updates.classify` — pure, exhaustively
tested. This service supplies the impure edges: configuration, the clock, and
storage.

The RUNNING version comes from M4 — the `biplane_installed_version` Instance
field (RC 3392 #2), the comparable RELEASE TAG baked into release images and
recorded at registration. Deliberately distinct from `biplane_installed_build`
(exact commit identity, never semver) and never Plane's `current_version`
pair (a different product's namespace). NULL means UNKNOWN with a named
reason, never "up to date": dev builds and pre-pipeline images bake no
version, so UNKNOWN is their correct standing answer — a check that guesses
which version it is running has already failed. No config fallback exists on
purpose: a second version source is the duplicate-mechanism pattern the audit
retired.

STORAGE, and why two layers: the FULL status payload (state, reason, level,
changelog) goes to the Django cache — a derived observation of THIS check,
safe to lose, and where "we looked and could not tell" lives. The durable M4
columns on Instance (`biplane_latest_version` / `biplane_latest_source` /
`biplane_latest_checked_at`) record LAST-KNOWN-GOOD only (Morrow 3349 #2): an
UNKNOWN check writes nothing — destroying last-known values on a failed look
conflates no-update with couldn't-look — and the timestamp travels WITH the
values it dates, so a failed check can never launder stale values under a
fresh date (the BIP-32 defect) and NULL columns still mean genuinely-never-
known. After a restart the status endpoint degrades to these columns and says
so, rather than replaying a cached claim it can no longer date.
"""

from django.core.cache import cache
from django.utils import timezone

# The beat cadence, from its single authority (the task module) —
# plane/celery.py builds the schedule from the same value (Morrow on #54:
# a 3600 here and a crontab there were two authorities for one fact).
from plane.bgtasks.update_check_task import (
    UPDATE_CHECK_INTERVAL_SECONDS as CHECK_INTERVAL_SECONDS,
)
from plane.license.utils.release_source import fetch_latest_release_metadata
from plane.updates.classify import Classification, STATE_CURRENT, STATE_UNKNOWN, classify, status_payload

STATUS_CACHE_KEY = "biplane:update-check:status"
#: The cached observation dies after two missed checks (RC 3392 #5). The
#: deployed Django cache is Redis-backed and SURVIVES restarts — an immortal
#: entry could pin a stale CURRENT banner forever if the checker stops, which
#: is the exact false all-clear this design exists to prevent. Expiry makes
#: the endpoint degrade to the durable columns and say so.
STATUS_CACHE_TTL_SECONDS = 2 * CHECK_INTERVAL_SECONDS


def _installed_version():
    """The comparable installed RELEASE VERSION (`biplane_installed_version`,
    RC 3392 #2), or None. Deliberately NOT `biplane_installed_build`: the
    build id is exact commit identity and never semver — comparing it was the
    hold. NULL (dev build, or a pre-pipeline image) classifies UNKNOWN, the
    honest standing answer."""
    from plane.license.models import Instance

    instance = Instance.objects.first()
    return getattr(instance, "biplane_installed_version", None) if instance else None


def run_update_check(*, fetch=fetch_latest_release_metadata) -> dict:
    """Execute one check and store the result. Returns the stored payload.

    Never raises for operational problems — every refusal becomes an UNKNOWN
    payload with the reason as operator text, because a crashed beat task is
    an invisible degrade while an UNKNOWN banner is a visible one.
    """
    checked_at = timezone.now()
    running_version = _installed_version()

    try:
        latest, source = fetch()
    except Exception as exc:  # defensive: the fetch path is designed not to raise
        source = None
        classification = Classification(
            STATE_UNKNOWN, f"release check failed: {exc}", None, None
        )
    else:
        classification = classify(running_version, latest)

    payload = status_payload(classification, checked_at.isoformat())
    payload["source"] = source if classification.latest_release else None

    cache.set(STATUS_CACHE_KEY, payload, timeout=STATUS_CACHE_TTL_SECONDS)
    _store_m4_columns(payload, source, checked_at)
    return payload


def _store_m4_columns(payload: dict, source, checked_at) -> None:
    """Durable columns record LAST-KNOWN-GOOD, and only that (Morrow 3349 #2).

    An UNKNOWN check writes NOTHING here: destroying last-known values on a
    failed look is the can't-tell-no-update-from-couldn't-look bug one layer
    up. The timestamp TRAVELS WITH the values it dates — it is "when these
    values were last verified", not "when we last tried". "When we last tried
    and with what outcome" lives in the cached payload, which the status
    endpoint serves alongside these columns.
    """
    from plane.license.models import Instance

    latest = payload.get("latest_release")
    if payload["state"] == STATE_UNKNOWN or latest is None or source is None:
        return
    instance = Instance.objects.first()
    if instance is None:  # not yet registered — nothing durable to record onto
        return
    instance.biplane_latest_version = latest["tag"]
    instance.biplane_latest_source = source
    instance.biplane_latest_checked_at = checked_at
    instance.save(
        update_fields=[
            "biplane_latest_version",
            "biplane_latest_source",
            "biplane_latest_checked_at",
        ]
    )


def check_status_payload() -> dict:
    """What GET /api/instances/updates/status/ returns.

    Cache hit: the full last-check payload. Cache miss (restart, or no check
    yet): degrade to the durable M4 columns and SAY SO — state is unknown with
    an explicit reason, latest_release carries only what the columns hold
    (tag; level and changelog need a completed check), and nothing pretends to
    a freshness it cannot date.
    """
    cached = cache.get(STATUS_CACHE_KEY)
    if cached is not None:
        return cached

    from plane.license.models import Instance

    instance = Instance.objects.first()
    checked_at = getattr(instance, "biplane_latest_checked_at", None) if instance else None
    version = getattr(instance, "biplane_latest_version", None) if instance else None
    source = getattr(instance, "biplane_latest_source", None) if instance else None
    running = getattr(instance, "biplane_installed_version", None) if instance else None

    # When the durable columns hold BOTH sides of the comparison, classify from
    # them rather than declaring UNKNOWN while holding the answer. The window
    # this closes is real and was hit in production (2026-08-19, v1.2.9): every
    # apply recreates the api container, which empties this cache, and the
    # post-apply re-check racing a cold Django on slow hardware lost — so the
    # page said "Update status unavailable" about a deployment whose running
    # AND latest versions were both sitting in the database, equal.
    #
    # This is not a replayed freshness: checked_at is the columns' own
    # timestamp, dating the LATEST value to when it was actually verified. The
    # honest-degrade path below is unchanged for every case the columns cannot
    # answer (never checked, or no declared running version) — UNKNOWN with the
    # reason, exactly as before. Deliberately NOT cached: the next completed
    # check must replace this, not find the shelf already occupied.
    #
    # And the columns may answer CURRENT only (Sable 4043). They cannot carry
    # `level` — there is no column for it — and level gates two of the three
    # full-release guards: the UI's manual-path message and the client-side
    # FULL_LEVEL refusal both read it from this payload. A reconstructed
    # update_available with level None would render a working-looking Update
    # button for a `full` release and let the click through to die at the
    # applier's own refusal — fail-safe only by the guard that does not read
    # this payload. CURRENT offers nothing, so nothing can be mis-offered;
    # every other classification falls through to honest UNKNOWN below, and
    # the next completed check upgrades it with a level that is real.
    if version and running:
        classification = classify(
            running, {"tag": version, "level": None, "changelog_url": None}
        )
        if classification.state == STATE_CURRENT:
            payload = status_payload(
                classification, checked_at.isoformat() if checked_at else None
            )
            payload["source"] = source
            return payload

    return {
        "state": STATE_UNKNOWN,
        "reason": (
            "no recent completed update check — the last observation expired "
            "or none has run yet"
        ),
        "checked_at": checked_at.isoformat() if checked_at else None,
        "running_release": None,
        "latest_release": (
            {"tag": version, "level": None, "changelog_url": None} if version else None
        ),
        "source": source if version else None,
    }
