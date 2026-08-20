# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""M5.2 check service + status endpoint: storage semantics and honest degrade.

The classification rules are pinned in plane/tests/unit/updates/; the fetch
path in test_release_source/test_bounded_fetch. These tests cover what the
Django layer adds — cache + M4-column storage with last-known-good-only
writes, the running version read from M4's biplane_installed_version
(RC 3392 #2), the finite cache lifetime (RC 3392 #5), the degrade path that
says so, and the instance-admin gate."""

import uuid

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from plane.db.models import User
from plane.license.models import Instance, InstanceAdmin
from plane.license.services import update_check as svc


def _instance(installed_version=None):
    return Instance.objects.create(
        instance_name="Biplane",
        instance_id=uuid.uuid4().hex,
        current_version="1.3.1",
        domain="",
        last_checked_at=timezone.now(),
        # The comparison reads biplane_installed_version (RC 3392 #2) — the
        # build id stays exact identity and is deliberately not set here.
        biplane_installed_version=installed_version,
    )


def _admin(instance):
    admin = User.objects.create(
        email=f"check-{uuid.uuid4().hex}@example.com", username=uuid.uuid4().hex[:16]
    )
    InstanceAdmin.objects.create(instance=instance, user=admin, role=20)
    return admin


def _latest(tag="v1.2.0"):
    return {"tag": tag, "level": "code", "changelog_url": f"https://github.com/x/{tag}"}


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.delete(svc.STATUS_CACHE_KEY)
    yield
    cache.delete(svc.STATUS_CACHE_KEY)


@pytest.mark.django_db
def test_current_check_stores_cache_and_m4_columns():
    instance = _instance(installed_version="v1.2.0")
    payload = svc.run_update_check(fetch=lambda: (_latest("v1.2.0"), "forgejo"))

    assert payload["state"] == "current"
    assert payload["source"] == "forgejo"
    assert cache.get(svc.STATUS_CACHE_KEY) == payload
    instance.refresh_from_db()
    assert instance.biplane_latest_version == "v1.2.0"
    assert instance.biplane_latest_source == "forgejo"
    assert instance.biplane_latest_checked_at is not None


@pytest.mark.django_db
def test_update_available_when_latest_is_newer():
    _instance(installed_version="v1.2.0")
    payload = svc.run_update_check(fetch=lambda: (_latest("v1.10.0"), "github"))
    assert payload["state"] == "update_available"
    assert payload["latest_release"]["tag"] == "v1.10.0"


@pytest.mark.django_db
def test_failed_check_preserves_last_known_good_untouched():
    """Morrow 3349 #2 (survives the redesign): an UNKNOWN check writes NOTHING
    durable — destroying last-known values on a failed look conflates
    no-update with couldn't-look — and the timestamp travels WITH the values
    it dates, so a failed check can never launder stale values under a fresh
    date. "We looked and could not tell" lives in the cached payload."""
    instance = _instance(installed_version="v1.1.0")
    known_at = timezone.now()
    instance.biplane_latest_version = "v1.1.0"
    instance.biplane_latest_source = "forgejo"
    instance.biplane_latest_checked_at = known_at
    instance.save()

    payload = svc.run_update_check(fetch=lambda: (None, None))

    assert payload["state"] == "unknown"
    assert "no release source" in payload["reason"]
    assert payload["checked_at"] is not None  # when we LOOKED, in the cache only
    instance.refresh_from_db()
    assert instance.biplane_latest_version == "v1.1.0"
    assert instance.biplane_latest_source == "forgejo"
    assert instance.biplane_latest_checked_at == known_at


@pytest.mark.django_db
def test_null_installed_version_is_unknown_with_the_availability_reason():
    """biplane_installed_version is NULL on dev builds and every pre-pipeline
    image (only release builds bake a version). That is the correct standing
    answer — UNKNOWN, with the latest release still visible — not a gap."""
    _instance(installed_version=None)
    payload = svc.run_update_check(fetch=lambda: (_latest(), "github"))
    assert payload["state"] == "unknown"
    assert payload["reason"] == "running version not available"
    assert payload["latest_release"]["tag"] == "v1.2.0"


@pytest.mark.django_db
def test_a_non_semver_installed_version_is_unknown_not_compared():
    """Nothing should ever put a build id in the VERSION field (that is what
    biplane_installed_build is for), but if one arrives it must classify
    UNKNOWN, never be string-compared."""
    _instance(installed_version="pi5-06bcb6f")
    payload = svc.run_update_check(fetch=lambda: (_latest(), "github"))
    assert payload["state"] == "unknown"
    assert "not a semantic version" in payload["reason"]


@pytest.mark.django_db
def test_a_raising_fetch_stores_unknown_instead_of_crashing_the_task():
    _instance(installed_version="v1.0.0")

    def explode():
        raise RuntimeError("boom")

    payload = svc.run_update_check(fetch=explode)
    assert payload["state"] == "unknown"
    assert "release check failed" in payload["reason"]
    assert cache.get(svc.STATUS_CACHE_KEY)["state"] == "unknown"


@pytest.mark.django_db
def test_no_instance_registered_is_unknown_and_stores_nothing_durable():
    payload = svc.run_update_check(fetch=lambda: (_latest(), "github"))
    assert payload["state"] == "unknown"
    assert payload["reason"] == "running version not available"


@pytest.mark.django_db
def test_endpoint_returns_cached_payload_to_instance_admin():
    instance = _instance()
    admin = _admin(instance)
    cache.set(svc.STATUS_CACHE_KEY, {"state": "update_available", "reason": None}, None)

    client = APIClient()
    client.force_authenticate(user=admin)
    response = client.get("/api/instances/updates/status/")
    assert response.status_code == 200, response.content
    assert response.data["state"] == "update_available"


@pytest.mark.django_db
def test_cache_miss_classifies_from_columns_when_both_sides_survive():
    """The v1.2.9 production window (2026-08-19): apply recreates the api
    container (cache emptied), the post-apply re-check loses the race against a
    cold Django, and the endpoint said "unavailable" about a deployment whose
    running and latest versions were BOTH in the database, equal. When the
    columns hold both sides, classify from them."""
    checked = timezone.now()
    instance = _instance(installed_version="v1.2.9")
    instance.biplane_latest_version = "v1.2.9"
    instance.biplane_latest_source = "forgejo"
    instance.biplane_latest_checked_at = checked
    instance.save()
    admin = _admin(instance)

    client = APIClient()
    client.force_authenticate(user=admin)
    response = client.get("/api/instances/updates/status/")
    assert response.status_code == 200
    assert response.data["state"] == "current"
    assert response.data["running_release"] == "v1.2.9"
    assert response.data["latest_release"]["tag"] == "v1.2.9"
    assert response.data["source"] == "forgejo"
    # The timestamp is the columns' own — dating the value to when it was
    # verified, never a replayed freshness.
    assert response.data["checked_at"] == checked.isoformat()
    # Deliberately NOT cached: the next completed check must replace this,
    # not find the shelf already occupied.
    assert cache.get(svc.STATUS_CACHE_KEY) is None


@pytest.mark.django_db
def test_cache_miss_with_newer_column_latest_stays_unknown():
    """The columns may answer CURRENT only (Sable 4043). They cannot carry
    `level`, and level gates the UI's manual-path message and the client-side
    FULL_LEVEL refusal — a reconstructed update_available with level None would
    render a working Update button for a `full` release. So a column latest
    NEWER than running falls through to the honest degrade, and the next
    completed check reports it with a level that is real."""
    instance = _instance(installed_version="v1.2.8")
    instance.biplane_latest_version = "v1.2.9"
    instance.biplane_latest_source = "forgejo"
    instance.biplane_latest_checked_at = timezone.now()
    instance.save()
    admin = _admin(instance)

    client = APIClient()
    client.force_authenticate(user=admin)
    response = client.get("/api/instances/updates/status/")
    assert response.status_code == 200
    assert response.data["state"] == "unknown"
    assert "no recent completed update check" in response.data["reason"]
    # The columns still surface what they DO know, exactly as before this
    # change: the last verified latest tag, never a fabricated level.
    assert response.data["latest_release"]["tag"] == "v1.2.9"
    assert response.data["latest_release"]["level"] is None


@pytest.mark.django_db
def test_endpoint_degrades_after_restart_and_says_so():
    """Cache gone (restart): state is unknown WITH the reason; the durable
    columns supply the only claims that survive — never a replayed freshness."""
    instance = _instance()
    instance.biplane_latest_version = "v1.2.0"
    instance.biplane_latest_source = "github"
    instance.biplane_latest_checked_at = timezone.now()
    instance.save()
    admin = _admin(instance)

    client = APIClient()
    client.force_authenticate(user=admin)
    response = client.get("/api/instances/updates/status/")
    assert response.status_code == 200
    assert response.data["state"] == "unknown"
    assert "no recent completed update check" in response.data["reason"]
    assert response.data["latest_release"]["tag"] == "v1.2.0"
    assert response.data["latest_release"]["level"] is None
    assert response.data["source"] == "github"
    assert response.data["running_release"] is None


@pytest.mark.django_db
def test_endpoint_refuses_non_admin():
    instance = _instance()
    outsider = User.objects.create(
        email=f"out-{uuid.uuid4().hex}@example.com", username=uuid.uuid4().hex[:16]
    )
    client = APIClient()
    client.force_authenticate(user=outsider)
    assert client.get("/api/instances/updates/status/").status_code == 403


@pytest.mark.django_db
def test_cached_status_has_a_finite_cadence_tied_lifetime(monkeypatch):
    """RC 3392 #5: the deployed Django cache is Redis-backed and SURVIVES
    restarts — an immortal entry could pin a stale CURRENT banner forever if
    the checker stops. The observation must die after a bounded number of
    missed checks; expiry (cache miss) then degrades to the durable columns
    with the honest reason (pinned by the degrade test above)."""
    _instance(installed_version="v1.2.0")
    recorded = {}
    real_set = cache.set

    def recording_set(key, value, timeout="MISSING"):
        recorded[key] = timeout
        return real_set(key, value, timeout=None if timeout == "MISSING" else timeout)

    monkeypatch.setattr(svc, "cache", type("C", (), {"set": staticmethod(recording_set), "get": staticmethod(cache.get)})())
    svc.run_update_check(fetch=lambda: (_latest("v1.2.0"), "forgejo"))

    assert recorded[svc.STATUS_CACHE_KEY] == svc.STATUS_CACHE_TTL_SECONDS
    assert svc.STATUS_CACHE_TTL_SECONDS == 2 * svc.CHECK_INTERVAL_SECONDS
    # And the lifetime is finite — never the immortal None.
    assert recorded[svc.STATUS_CACHE_KEY] is not None


def test_the_check_cadence_has_exactly_one_authority():
    """Morrow on #54: a `3600` in the service and a crontab entry in celery.py
    were two authorities for one fact — the next person to change one would
    not find the other. Now the task module owns the value; the beat schedule
    and the cache TTL are both DERIVED from it, pinned here end to end."""
    from plane.bgtasks.update_check_task import UPDATE_CHECK_INTERVAL_SECONDS
    from plane.celery import app

    entry = app.conf.beat_schedule["biplane-update-check"]
    assert entry["schedule"] == UPDATE_CHECK_INTERVAL_SECONDS
    assert svc.CHECK_INTERVAL_SECONDS == UPDATE_CHECK_INTERVAL_SECONDS
    assert svc.STATUS_CACHE_TTL_SECONDS == 2 * UPDATE_CHECK_INTERVAL_SECONDS
