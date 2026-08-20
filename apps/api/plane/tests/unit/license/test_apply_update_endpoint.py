# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The apply endpoints (ticket 69 / BIP-42 tail): a strict pass-through.

What the Django layer must guarantee, each pinned here with its negative:
  - only an instance admin can ask for an apply;
  - unconfigured applier ⇒ 501, and the applier is NEVER contacted;
  - the tag sent upstream is the CHECK's flagged tag — the client cannot
    supply one (a client tag would make this endpoint a second authority);
  - no flagged update, or a full-level flag ⇒ 409, applier never contacted;
  - the applier's verdict passes through verbatim, status code included;
  - an unreachable applier is 502, not a crash and not a silent success.

Every "the applier was never contacted" claim is asserted on the recording
mock itself, not inferred from the response code.
"""

import uuid
from unittest import mock

import pytest
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from plane.db.models import User
from plane.license.models import Instance, InstanceAdmin

VIEWS = "plane.license.api.views.apply_update"
CLIENT = "plane.license.services.apply_client"


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
    admin = User.objects.create(
        email=f"apply-{uuid.uuid4().hex}@example.com", username=uuid.uuid4().hex[:16]
    )
    InstanceAdmin.objects.create(instance=instance, user=admin, role=20)
    return admin


def _member():
    return User.objects.create(
        email=f"member-{uuid.uuid4().hex}@example.com", username=uuid.uuid4().hex[:16]
    )


def _flagged(state="update_available", tag="v1.2.0", level="code"):
    payload = {"state": state, "reason": None, "latest_release": None}
    if tag is not None:
        payload["latest_release"] = {"tag": tag, "level": level, "changelog_url": None}
    return payload


CONFIGURED = override_settings(
    BIPLANE_APPLY_SERVICE_URL="http://apply.test:7671",
    BIPLANE_APPLY_SERVICE_TOKEN="test-token",
)


@pytest.mark.django_db
def test_a_non_admin_cannot_ask_for_an_apply():
    _instance()
    client = APIClient()
    client.force_authenticate(_member())
    with mock.patch(f"{CLIENT}.http_requests") as transport:
        response = client.post("/api/instances/updates/apply/")
    assert response.status_code in (401, 403)
    transport.post.assert_not_called()


@pytest.mark.django_db
@CONFIGURED
def test_the_flagged_tag_is_what_goes_upstream_never_the_clients():
    instance = _instance()
    client = APIClient()
    client.force_authenticate(_admin(instance))
    upstream = mock.Mock(status_code=202)
    upstream.json.return_value = {"started": "v1.2.0"}
    with mock.patch(f"{CLIENT}.check_status_payload", return_value=_flagged()), \
         mock.patch(f"{CLIENT}.http_requests") as transport:
        transport.post.return_value = upstream
        response = client.post(
            "/api/instances/updates/apply/",
            {"tag": "v9.9.9-attacker-chosen"},
            format="json",
        )
    assert response.status_code == 202
    assert response.data == {"started": "v1.2.0"}
    sent = transport.post.call_args
    assert sent.kwargs["json"] == {"tag": "v1.2.0"}, (
        "the check's flagged tag is the only tag this endpoint may send"
    )
    assert "v9.9.9" not in str(sent), "the client-supplied tag must not travel"
    # The WRITE path carries the bearer token — the state-changing request is
    # the one that must never ship unauthenticated (Sable RC 3798 #1: this was
    # pinned on the read path and not here, which was backwards).
    assert sent.kwargs["headers"]["Authorization"] == "Bearer test-token"


@pytest.mark.django_db
def test_unconfigured_applier_is_501_and_never_contacted():
    instance = _instance()
    client = APIClient()
    client.force_authenticate(_admin(instance))
    with mock.patch(f"{CLIENT}.check_status_payload", return_value=_flagged()), \
         mock.patch(f"{CLIENT}.http_requests") as transport:
        response = client.post("/api/instances/updates/apply/")
    assert response.status_code == 501
    transport.post.assert_not_called()


@pytest.mark.django_db
@CONFIGURED
def test_no_flagged_update_is_409_and_never_contacted():
    instance = _instance()
    client = APIClient()
    client.force_authenticate(_admin(instance))
    with mock.patch(f"{CLIENT}.check_status_payload", return_value=_flagged(state="current", tag=None)), \
         mock.patch(f"{CLIENT}.http_requests") as transport:
        response = client.post("/api/instances/updates/apply/")
    assert response.status_code == 409
    transport.post.assert_not_called()


@pytest.mark.django_db
@CONFIGURED
def test_each_arm_of_the_409_guard_refuses_alone():
    """state and tag are refused INDEPENDENTLY (Sable RC 3798 #2): the only
    prior 409 test had both arms true, so `or`→`and` survived — under which
    update_available-with-a-null-tag posts {"tag": None} upstream and
    current-with-a-tag applies while current."""
    instance = _instance()
    client = APIClient()
    client.force_authenticate(_admin(instance))
    # Arm 1 alone: flagged state, but no tag.
    with mock.patch(f"{CLIENT}.check_status_payload", return_value=_flagged(tag=None)), \
         mock.patch(f"{CLIENT}.http_requests") as transport:
        response = client.post("/api/instances/updates/apply/")
    assert response.status_code == 409
    transport.post.assert_not_called()
    # Arm 2 alone: a tag present, but the state is current.
    with mock.patch(f"{CLIENT}.check_status_payload", return_value=_flagged(state="current")), \
         mock.patch(f"{CLIENT}.http_requests") as transport:
        response = client.post("/api/instances/updates/apply/")
    assert response.status_code == 409
    transport.post.assert_not_called()


@pytest.mark.django_db
@CONFIGURED
def test_a_full_level_flag_is_409_manual_path_and_never_contacted():
    instance = _instance()
    client = APIClient()
    client.force_authenticate(_admin(instance))
    with mock.patch(f"{CLIENT}.check_status_payload", return_value=_flagged(level="full")), \
         mock.patch(f"{CLIENT}.http_requests") as transport:
        response = client.post("/api/instances/updates/apply/")
    assert response.status_code == 409
    assert "manual" in response.data["error"]
    transport.post.assert_not_called()


@pytest.mark.django_db
@CONFIGURED
def test_the_appliers_refusal_passes_through_verbatim():
    instance = _instance()
    client = APIClient()
    client.force_authenticate(_admin(instance))
    upstream = mock.Mock(status_code=409)
    upstream.json.return_value = {"error": "an apply is already running"}
    with mock.patch(f"{CLIENT}.check_status_payload", return_value=_flagged()), \
         mock.patch(f"{CLIENT}.http_requests") as transport:
        transport.post.return_value = upstream
        response = client.post("/api/instances/updates/apply/")
    assert response.status_code == 409
    assert response.data == {"error": "an apply is already running"}


@pytest.mark.django_db
@CONFIGURED
def test_an_unreachable_applier_is_502():
    import requests as real_requests

    instance = _instance()
    client = APIClient()
    client.force_authenticate(_admin(instance))
    with mock.patch(f"{CLIENT}.check_status_payload", return_value=_flagged()), \
         mock.patch(f"{CLIENT}.http_requests") as transport:
        transport.RequestException = real_requests.RequestException
        transport.post.side_effect = real_requests.ConnectionError("refused")
        response = client.post("/api/instances/updates/apply/")
    assert response.status_code == 502


@pytest.mark.django_db
@CONFIGURED
def test_a_non_json_upstream_body_is_502_not_a_crash():
    """Every stub sets json.return_value, which is exactly why this case was
    invisible (Vex RC 3801): a real applier answering HTML or an empty body
    raised out of the view as a 500. Same class as unreachable: 502."""
    instance = _instance()
    client = APIClient()
    client.force_authenticate(_admin(instance))
    upstream = mock.Mock(status_code=200)
    upstream.json.side_effect = ValueError("no JSON could be decoded")
    with mock.patch(f"{CLIENT}.check_status_payload", return_value=_flagged()), \
         mock.patch(f"{CLIENT}.http_requests") as transport:
        transport.post.return_value = upstream
        response = client.post("/api/instances/updates/apply/")
    assert response.status_code == 502
    assert "non-JSON" in response.data["error"]


@pytest.mark.django_db
@CONFIGURED
def test_status_passes_through_with_the_token_and_only_to_admins():
    instance = _instance()
    client = APIClient()
    client.force_authenticate(_admin(instance))
    upstream = mock.Mock(status_code=200)
    upstream.json.return_value = {"running": False, "last_result": None, "log_tail": ""}
    with mock.patch(f"{CLIENT}.http_requests") as transport:
        transport.get.return_value = upstream
        response = client.get("/api/instances/updates/apply/status/")
    assert response.status_code == 200
    sent = transport.get.call_args
    assert sent.kwargs["headers"]["Authorization"] == "Bearer test-token"

    outsider = APIClient()
    outsider.force_authenticate(_member())
    with mock.patch(f"{CLIENT}.http_requests") as transport:
        refused = outsider.get("/api/instances/updates/apply/status/")
    assert refused.status_code in (401, 403)
    transport.get.assert_not_called()
