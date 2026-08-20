# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Board sessions on the updates endpoints (the second-login bug).

The session middleware binds any path containing "instances" to the
short-lived admin-session-id cookie, so an instance admin browsing the
board was anonymous here: the Settings → Updates page 401'd, bounced to
"/?next_path=...", and rendered the demo's gray page until John logged in
a second time on the admin site (2026-08-17). These tests go through the
REAL cookie path — no force_authenticate, which bypasses authentication
entirely and can never see this bug:

  - an instance admin carrying only the BOARD session cookie gets 200;
  - a plain member with the same kind of cookie is still 403 — the fix
    changes who is RECOGNIZED, never who is ALLOWED;
  - no cookie at all stays 401.
"""

import uuid
from unittest import mock

import pytest
from django.test import Client
from django.utils import timezone

from plane.db.models import User
from plane.license.models import Instance, InstanceAdmin


def _instance():
    return Instance.objects.create(
        instance_name="Biplane",
        instance_id=uuid.uuid4().hex,
        current_version="1.3.1",
        domain="",
        last_checked_at=timezone.now(),
        biplane_installed_version="v1.1.0",
    )


def _user():
    return User.objects.create(
        email=f"board-{uuid.uuid4().hex}@example.com",
        username=uuid.uuid4().hex[:16],
    )


def _board_client(user):
    """A client whose ONLY credential is the regular board session cookie —
    exactly what a board tab carries onto /api/instances/... paths."""
    client = Client()
    client.force_login(user)
    assert "admin-session-id" not in client.cookies
    return client


@pytest.mark.django_db
def test_an_instance_admin_board_session_reaches_updates_status():
    instance = _instance()
    admin = _user()
    InstanceAdmin.objects.create(instance=instance, user=admin, role=20)
    # The payload itself needs the check cache (redis); this test is about
    # who is RECOGNIZED at the door, so the room behind it is stubbed.
    with mock.patch(
        "plane.license.api.views.update_check.check_status_payload",
        return_value={"state": "up_to_date"},
    ):
        response = _board_client(admin).get("/api/instances/updates/status/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_a_member_board_session_is_recognized_but_still_forbidden():
    _instance()
    response = _board_client(_user()).get("/api/instances/updates/status/")
    assert response.status_code == 403


@pytest.mark.django_db
def test_no_cookie_at_all_stays_unauthenticated():
    _instance()
    response = Client().get("/api/instances/updates/status/")
    assert response.status_code == 401


@pytest.mark.django_db
def test_an_instance_admin_board_session_can_post_an_apply():
    """The POST that triggers a deployment — the exact request the demo's
    click sends — must work through the board cookie path, where CSRF
    handling participates in a way no GET can prove (Sable, review 3957)."""
    instance = _instance()
    admin = _user()
    InstanceAdmin.objects.create(instance=instance, user=admin, role=20)
    with mock.patch(
        "plane.license.api.views.apply_update.apply_client.request_apply_of_flagged",
        return_value={"kind": "requested", "status_code": 202, "body": {"started": "v9.9.9"}},
    ):
        response = _board_client(admin).post("/api/instances/updates/apply/")
    assert response.status_code == 202


@pytest.mark.django_db
def test_a_member_board_session_cannot_post_an_apply():
    """The destructive arm: a recognized-but-unprivileged board session must
    stop at the permission, and the applier must never be contacted."""
    _instance()
    with mock.patch(
        "plane.license.api.views.apply_update.apply_client.request_apply_of_flagged"
    ) as upstream:
        response = _board_client(_user()).post("/api/instances/updates/apply/")
    assert response.status_code == 403
    upstream.assert_not_called()


@pytest.mark.django_db
def test_an_instance_admin_board_session_can_flip_auto_apply():
    instance = _instance()
    admin = _user()
    InstanceAdmin.objects.create(instance=instance, user=admin, role=20)
    response = _board_client(admin).patch(
        "/api/instances/updates/auto/", {"enabled": True}, content_type="application/json"
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is True
