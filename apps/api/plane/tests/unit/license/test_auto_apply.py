# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The automatic mode (ticket 69): apply-on-flag, off by default, once per tag.

Pinned with negatives, per the file convention:
  - OFF (default): the client is NEVER consulted, whatever the check says;
  - ON + flagged: exactly one request, carrying the CHECK's payload;
  - the guard is durable and written BEFORE the request — a second check of
    the same tag sends nothing, even after the first attempt failed;
  - a NEW tag after an attempted one is attempted;
  - the guard only advances on a flagged payload — current/unknown states
    never write it.
Every never-consulted claim is asserted on the recording mock itself.
"""

import uuid
from unittest import mock

import pytest
from django.test import override_settings
from django.utils import timezone

from plane.bgtasks.update_check_task import _maybe_auto_apply
from plane.db.models import User
from plane.license.models import BiplaneAutoApplyAttempt, Instance

TASK = "plane.license.services.apply_client"


def _instance():
    return Instance.objects.create(
        instance_name="Biplane",
        instance_id=uuid.uuid4().hex,
        current_version="1.3.1",
        domain="",
        last_checked_at=timezone.now(),
        biplane_installed_version="v1.1.0",
    )


def _admin(instance):
    from plane.license.models import InstanceAdmin

    admin = User.objects.create(
        email=f"auto-{uuid.uuid4().hex}@example.com", username=uuid.uuid4().hex[:16]
    )
    InstanceAdmin.objects.create(instance=instance, user=admin, role=20)
    return admin


def _member():
    return User.objects.create(
        email=f"member-{uuid.uuid4().hex}@example.com", username=uuid.uuid4().hex[:16]
    )


def _flagged(tag="v1.2.0", level="code"):
    return {"state": "update_available", "latest_release": {"tag": tag, "level": level, "changelog_url": None}}


@pytest.mark.django_db
def test_off_by_default_the_client_is_never_consulted():
    _instance()
    with mock.patch(f"{TASK}.request_apply_of_flagged") as request:
        _maybe_auto_apply(_flagged())
    request.assert_not_called()


@pytest.mark.django_db
@override_settings(BIPLANE_APPLY_AUTO=True)
def test_on_and_flagged_requests_exactly_once_with_the_checks_payload():
    _instance()
    payload = _flagged()
    with mock.patch(f"{TASK}.is_configured", return_value=True), \
         mock.patch(f"{TASK}.request_apply_of_flagged", return_value={"kind": "requested"}) as request:
        _maybe_auto_apply(payload)
    request.assert_called_once_with(status_payload=payload)
    assert BiplaneAutoApplyAttempt.objects.filter(tag="v1.2.0").count() == 1


@pytest.mark.django_db
@override_settings(BIPLANE_APPLY_AUTO=True)
def test_the_same_tag_is_never_attempted_twice_even_after_failure():
    _instance()
    with mock.patch(f"{TASK}.is_configured", return_value=True), \
         mock.patch(f"{TASK}.request_apply_of_flagged", return_value={"kind": "unreachable", "detail": "x"}) as request:
        _maybe_auto_apply(_flagged())
        _maybe_auto_apply(_flagged())
    assert request.call_count == 1, "a failed attempt must not retry hourly"


@pytest.mark.django_db
@override_settings(BIPLANE_APPLY_AUTO=True)
def test_a_new_tag_is_attempted_after_a_previous_one():
    _instance()
    with mock.patch(f"{TASK}.is_configured", return_value=True), \
         mock.patch(f"{TASK}.request_apply_of_flagged", return_value={"kind": "requested"}) as request:
        _maybe_auto_apply(_flagged(tag="v1.2.0"))
        _maybe_auto_apply(_flagged(tag="v1.2.1"))
    assert request.call_count == 2
    assert request.call_args_list[1].kwargs["status_payload"]["latest_release"]["tag"] == "v1.2.1"


@pytest.mark.django_db
@override_settings(BIPLANE_APPLY_AUTO=True)
def test_the_guard_is_durable_BEFORE_the_request_leaves():
    """Sable RC 3826 #1, her instrument verbatim: ordering is only observable
    when the request does NOT return — the crash the field exists for. The
    send reads the DB from inside itself and then dies; the tag must already
    be there. Witnessed first: with save-after-send, all other tests stay
    green and only this one can red."""
    _instance()
    seen = {}

    def _dies_mid_send(*, status_payload):
        seen["tag_at_send_time"] = BiplaneAutoApplyAttempt.objects.filter(tag="v1.2.0").exists()
        raise RuntimeError("process killed mid-apply")

    with mock.patch(f"{TASK}.is_configured", return_value=True), \
         mock.patch(f"{TASK}.request_apply_of_flagged", side_effect=_dies_mid_send):
        with pytest.raises(RuntimeError):
            _maybe_auto_apply(_flagged())
    assert seen["tag_at_send_time"] is True, "the guard must be durable before the send"


@pytest.mark.django_db
@override_settings(BIPLANE_APPLY_AUTO=True)
def test_unconfigured_applier_never_burns_the_guard():
    """Sable RC 3826 #3: enable auto before configuring the applier and the
    release must NOT be silently lost — nothing was attempted."""
    _instance()
    with mock.patch(f"{TASK}.is_configured", return_value=False), \
         mock.patch(f"{TASK}.request_apply_of_flagged") as request:
        _maybe_auto_apply(_flagged())
    request.assert_not_called()
    assert not BiplaneAutoApplyAttempt.objects.exists()


@pytest.mark.django_db
@override_settings(BIPLANE_APPLY_AUTO=True)
def test_a_stale_worker_cannot_resurrect_an_attempted_tag():
    """Rowan review 3834, his exact ordering: newer attempted, then a STALE
    worker still holding the older payload arrives, then the newer flag
    returns. Under the single last-tag field the stale worker rolled the
    guard backward and the newer tag was attempted TWICE; the append-only
    record makes that impossible — each tag once, whatever the order."""
    _instance()
    with mock.patch(f"{TASK}.is_configured", return_value=True), \
         mock.patch(f"{TASK}.request_apply_of_flagged", return_value={"kind": "requested"}) as request:
        _maybe_auto_apply(_flagged(tag="v1.2.1"))
        _maybe_auto_apply(_flagged(tag="v1.2.0"))   # the stale worker
        _maybe_auto_apply(_flagged(tag="v1.2.1"))   # the newer flag returns
    attempted = [c.kwargs["status_payload"]["latest_release"]["tag"] for c in request.call_args_list]
    assert attempted == ["v1.2.1", "v1.2.0"], "each tag exactly once, in arrival order"
    assert BiplaneAutoApplyAttempt.objects.filter(tag="v1.2.1").count() == 1


@pytest.mark.django_db
@override_settings(BIPLANE_APPLY_AUTO=True)
def test_the_fence_survives_a_hook_that_raises():
    """Vex review 3833: the fence had no witness — the reachability test's
    Mock never raises, and the durability test raises OUTSIDE the fence.
    This enters through the TASK with a hook that dies; the check's result
    must still come back. Deleting the try/except reds exactly this."""
    from plane.bgtasks import update_check_task as task_mod

    payload = _flagged()
    with mock.patch.object(task_mod, "_maybe_auto_apply", side_effect=RuntimeError("boom")), \
         mock.patch("plane.license.services.update_check.run_update_check", return_value=payload):
        assert task_mod.run_update_check() == "update_available"


@pytest.mark.django_db
@override_settings(BIPLANE_APPLY_AUTO=True)
def test_the_task_actually_calls_the_auto_hook():
    """Vex RC 3828, the reachability gap: delete the hook call from
    run_update_check and the suite stayed green while automatic mode
    silently never ran. This enters through the TASK, not the service."""
    from plane.bgtasks import update_check_task as task_mod

    payload = _flagged()
    with mock.patch.object(task_mod, "_maybe_auto_apply") as hook, \
         mock.patch("plane.license.services.update_check.run_update_check", return_value=payload):
        result = task_mod.run_update_check()
    hook.assert_called_once_with(payload)
    assert result == "update_available"


@pytest.mark.django_db
@override_settings(BIPLANE_APPLY_AUTO=True)
def test_unflagged_states_write_nothing_and_send_nothing():
    _instance()
    with mock.patch(f"{TASK}.request_apply_of_flagged") as request:
        _maybe_auto_apply({"state": "current", "latest_release": None})
        _maybe_auto_apply({"state": "unknown", "latest_release": None})
        _maybe_auto_apply({"state": "update_available", "latest_release": {"tag": None, "level": "code"}})
    request.assert_not_called()
    assert not BiplaneAutoApplyAttempt.objects.exists()


@pytest.mark.django_db
def test_the_settings_switch_turns_auto_on_without_env():
    """John's design: the switch is real — DB on, env absent, the hook runs."""
    from plane.license.models import InstanceConfiguration

    _instance()
    InstanceConfiguration.objects.create(key="BIPLANE_APPLY_AUTO", value="1", category="BIPLANE")
    with mock.patch(f"{TASK}.is_configured", return_value=True), \
         mock.patch(f"{TASK}.request_apply_of_flagged", return_value={"kind": "requested"}) as request:
        _maybe_auto_apply(_flagged())
    request.assert_called_once()


@pytest.mark.django_db
def test_env_forced_on_wins_over_a_db_off():
    """The env var is a deployment-level force-on — an operator who set it at
    deploy keeps it, whatever the UI row says."""
    from plane.license.models import InstanceConfiguration

    _instance()
    InstanceConfiguration.objects.create(key="BIPLANE_APPLY_AUTO", value="0", category="BIPLANE")
    with override_settings(BIPLANE_APPLY_AUTO=True), \
         mock.patch(f"{TASK}.is_configured", return_value=True), \
         mock.patch(f"{TASK}.request_apply_of_flagged", return_value={"kind": "requested"}) as request:
        _maybe_auto_apply(_flagged())
    request.assert_called_once()


@pytest.mark.django_db
def test_the_switch_endpoint_is_admin_gated_and_round_trips():
    from rest_framework.test import APIClient

    instance = _instance()
    outsider = APIClient()
    outsider.force_authenticate(_member())
    assert outsider.get("/api/instances/updates/auto/").status_code in (401, 403)

    admin = APIClient()
    admin.force_authenticate(_admin(instance))
    assert admin.get("/api/instances/updates/auto/").data == {"enabled": False, "env_forced": False}
    assert admin.patch("/api/instances/updates/auto/", {"enabled": True}, format="json").data["enabled"] is True
    assert admin.get("/api/instances/updates/auto/").data["enabled"] is True
    assert admin.patch("/api/instances/updates/auto/", {"enabled": False}, format="json").data["enabled"] is False
    assert admin.patch("/api/instances/updates/auto/", {"enabled": "yes"}, format="json").status_code == 400


@pytest.mark.django_db
def test_update_source_endpoint_choices_and_the_reserved_one():
    from rest_framework.test import APIClient

    instance = _instance()
    admin = APIClient()
    admin.force_authenticate(_admin(instance))
    # default: the current server
    assert admin.get("/api/instances/updates/source/").data == {"source": "forgejo", "custom_url": None}
    # the reserved choice is refused with its reason, never silently ignored
    r = admin.patch("/api/instances/updates/source/", {"source": "biplane_dev"}, format="json")
    assert r.status_code == 400 and "coming soon" in r.data["error"]
    # github round-trips
    assert admin.patch("/api/instances/updates/source/", {"source": "github"}, format="json").data["source"] == "github"
    # custom demands a URL, then round-trips
    assert admin.patch("/api/instances/updates/source/", {"source": "custom"}, format="json").status_code == 400
    r = admin.patch(
        "/api/instances/updates/source/", {"source": "custom", "custom_url": "https://u.example/r"}, format="json"
    )
    assert r.data == {"source": "custom", "custom_url": "https://u.example/r"}
    # switching away clears the custom url
    assert admin.patch("/api/instances/updates/source/", {"source": "forgejo"}, format="json").data["custom_url"] is None

    outsider = APIClient()
    outsider.force_authenticate(_member())
    assert outsider.get("/api/instances/updates/source/").status_code in (401, 403)


@pytest.mark.django_db
def test_the_preferred_source_is_tried_first_with_fallback_intact():
    """The preference reorders, never removes: a github preference tries the
    mirror first; a dead custom URL falls back to the existing order."""
    from plane.license.models import InstanceConfiguration
    from plane.license.utils import release_source as rs

    _instance()
    calls = []

    def fake_fetch(url, credential=None, **kw):
        calls.append(url)
        return {"tag": "v9.9.9", "level": "code", "changelog_url": None} if "github" in url else None

    InstanceConfiguration.objects.create(key=rs.UPDATE_SOURCE_KEY, value="github", category="BIPLANE")
    with mock.patch.object(rs, "_fetch_release", side_effect=fake_fetch), \
         mock.patch.object(rs, "_github_releases_url", return_value="https://api.github.com/x/releases"), \
         mock.patch.object(rs, "_forgejo_releases_url", return_value="https://forge.example/releases"):
        release, source = rs.fetch_latest_release_metadata()
    assert source == rs.SOURCE_GITHUB and calls[0].startswith("https://api.github.com")

    calls.clear()
    InstanceConfiguration.objects.filter(key=rs.UPDATE_SOURCE_KEY).update(value="custom")
    InstanceConfiguration.objects.create(key=rs.UPDATE_SOURCE_URL_KEY, value="https://dead.example/r", category="BIPLANE")
    with mock.patch.object(rs, "_fetch_release", side_effect=fake_fetch), \
         mock.patch.object(rs, "_github_releases_url", return_value="https://api.github.com/x/releases"), \
         mock.patch.object(rs, "_forgejo_releases_url", return_value="https://forge.example/releases"):
        release, source = rs.fetch_latest_release_metadata()
    assert calls[0] == "https://dead.example/r", "the custom URL is tried first"
    assert source == rs.SOURCE_GITHUB, "and a dead custom falls back to the existing order"


@pytest.mark.django_db
def test_a_saved_custom_server_authorizes_its_own_origin():
    """John's ruling: forks run their own update servers, and the Settings
    page must be enough — the admin typing the URL IS the origin declaration.
    The saved custom origin joins the allowlist INPUT; the fetch primitive
    stays the sole enforcer."""
    from plane.license.models import InstanceConfiguration
    from plane.license.utils import release_source as rs

    _instance()
    InstanceConfiguration.objects.create(key=rs.UPDATE_SOURCE_KEY, value="custom", category="BIPLANE")
    InstanceConfiguration.objects.create(
        key=rs.UPDATE_SOURCE_URL_KEY, value="https://updates.fork.example/latest", category="BIPLANE"
    )
    seen = {}

    def fake_get_json(url, allowed_origins=None, credential=None, **kw):
        seen["origins"] = tuple(allowed_origins or ())
        return 404, {}

    with mock.patch.object(rs, "bounded_get_json", side_effect=fake_get_json), \
         mock.patch.object(rs, "_forgejo_releases_url", return_value=None), \
         mock.patch.object(rs, "_github_releases_url", return_value=None):
        rs.fetch_latest_release_metadata()
    assert "https://updates.fork.example/latest" in seen["origins"], (
        "the saved custom URL must reach the fetch primitive as an allowed origin"
    )


@pytest.mark.django_db
def test_apply_metadata_resolves_from_the_custom_server_first():
    """Morrow's hold on this branch: a source that can FLAG a release must be
    able to RESOLVE it for apply, or the button and automatic mode dead-end
    on custom deployments. The exact-tag convention is <saved-url>/tags/<tag>,
    with the saved URL authorizing its own origin, and fallbacks intact."""
    from plane.license.models import InstanceConfiguration
    from plane.license.utils import release_source as rs

    _instance()
    InstanceConfiguration.objects.create(key=rs.UPDATE_SOURCE_KEY, value="custom", category="BIPLANE")
    InstanceConfiguration.objects.create(
        key=rs.UPDATE_SOURCE_URL_KEY, value="https://updates.fork.example/releases", category="BIPLANE"
    )
    seen = {}

    def fake_apply_fetch(url, expected_tag, credential=None, extra_origin=None):
        seen.update(url=url, tag=expected_tag, extra_origin=extra_origin)
        return {"tag": expected_tag, "level": "code"}

    with mock.patch.object(rs, "_fetch_release_for_apply", side_effect=fake_apply_fetch):
        release, source = rs.fetch_release_metadata_by_tag("v1.2.1")
    assert source == rs.SOURCE_CUSTOM
    assert seen["url"] == "https://updates.fork.example/releases/tags/v1.2.1"
    assert seen["extra_origin"] == "https://updates.fork.example/releases"

    # And a custom server that cannot answer does NOT fall back for APPLY:
    # a fork tracking upstream versions collides on tags by construction, so
    # a fallback would install a different project's release under a tag the
    # fork's own server advertised (Vex 3905). Honest refusal instead.
    InstanceConfiguration.objects.filter(key=rs.UPDATE_SOURCE_URL_KEY).update(value="https://dead.example/r")
    calls = []

    def dead_custom(url, expected_tag, credential=None, extra_origin=None):
        calls.append(url)
        return None

    with mock.patch.object(rs, "_fetch_release_for_apply", side_effect=dead_custom), \
         mock.patch.object(rs, "_forgejo_release_tag_url", return_value="https://forge.example/tags/v1.2.1"):
        release, source = rs.fetch_release_metadata_by_tag("v1.2.1")
    assert release is None and source is None
    assert calls == ["https://dead.example/r/tags/v1.2.1"], "no fallback fetch may happen for apply"
