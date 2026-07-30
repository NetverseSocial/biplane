# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# biplane: route-level regressions for EVERY weak-password door (Morrow RC 3027).
# The server (zxcvbn) is the strength authority; each door must reject a weak
# password without the flag and accept it WITH the explicit override — so the UI
# flag can never regress into a dead checkbox. Witness password: Password1!
# (frontend character heuristic calls it valid; zxcvbn scores 1).

import pytest
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils import timezone
from django.utils.encoding import smart_bytes
from django.utils.http import urlsafe_base64_encode

from plane.db.models import User
from plane.license.models import Instance

WEAK = "Password1!"
STRONG = "korrekt-hest-batteri-stift-9931"


@pytest.fixture
def configured_instance(db):
    return Instance.objects.create(
        instance_name="test",
        instance_id="test-instance",
        is_setup_done=True,
        is_signup_screen_visited=True,
        last_checked_at=timezone.now(),
    )


@pytest.mark.contract
class TestSignUpDoor:
    @pytest.mark.django_db
    def test_weak_password_bounces_without_flag(self, api_client, configured_instance):
        response = api_client.post(
            "/auth/sign-up/",
            {"email": "weak-door@example.com", "password": WEAK},
        )
        assert response.status_code == 302
        assert "PASSWORD_TOO_WEAK" in response["Location"]
        assert not User.objects.filter(email="weak-door@example.com").exists()

    @pytest.mark.django_db
    def test_weak_password_with_override_creates_user(self, api_client, configured_instance):
        response = api_client.post(
            "/auth/sign-up/",
            {
                "email": "weak-ok@example.com",
                "password": WEAK,
                "accept_weak_password": "True",
                "first_name": "Amelia",
                "last_name": "Earhart",
            },
        )
        assert response.status_code == 302
        assert "PASSWORD_TOO_WEAK" not in response["Location"]
        user = User.objects.get(email="weak-ok@example.com")
        assert user.first_name == "Amelia"
        assert user.last_name == "Earhart"
        assert user.check_password(WEAK)

    @pytest.mark.django_db
    def test_signup_names_are_canonicalized(self, api_client, configured_instance):
        response = api_client.post(
            "/auth/sign-up/",
            {
                "email": "canon@example.com",
                "password": STRONG,
                "first_name": "A" * 300 + "\x07",
                "last_name": "  Bell\x00Curve  ",
            },
        )
        assert response.status_code == 302
        user = User.objects.get(email="canon@example.com")
        assert len(user.first_name) <= 150
        assert "\x07" not in user.first_name
        assert user.last_name == "BellCurve"


@pytest.mark.contract
class TestChangePasswordDoor:
    @pytest.mark.django_db
    def test_weak_rejected_then_override_accepted(self, session_client, create_user):
        create_user.set_password("Old-Password-991!")
        create_user.is_password_autoset = False
        create_user.save()
        session_client.force_authenticate(user=create_user)

        rejected = session_client.post(
            "/auth/change-password/",
            {"old_password": "Old-Password-991!", "new_password": WEAK},
            format="json",
        )
        assert rejected.status_code == 400

        accepted = session_client.post(
            "/auth/change-password/",
            {"old_password": "Old-Password-991!", "new_password": WEAK, "accept_weak_password": True},
            format="json",
        )
        assert accepted.status_code == 200
        create_user.refresh_from_db()
        assert create_user.check_password(WEAK)


@pytest.mark.contract
class TestSetPasswordDoor:
    @pytest.mark.django_db
    def test_weak_rejected_then_override_accepted(self, session_client, create_user):
        create_user.is_password_autoset = True
        create_user.save()
        session_client.force_authenticate(user=create_user)

        rejected = session_client.post("/auth/set-password/", {"password": WEAK}, format="json")
        assert rejected.status_code == 400

        create_user.refresh_from_db()
        create_user.is_password_autoset = True
        create_user.save()
        accepted = session_client.post(
            "/auth/set-password/", {"password": WEAK, "accept_weak_password": True}, format="json"
        )
        assert accepted.status_code == 200
        create_user.refresh_from_db()
        assert create_user.check_password(WEAK)


@pytest.mark.contract
class TestResetPasswordDoor:
    def _token_for(self, user):
        uidb64 = urlsafe_base64_encode(smart_bytes(user.id))
        token = PasswordResetTokenGenerator().make_token(user)
        return uidb64, token

    @pytest.mark.django_db
    def test_weak_bounce_preserves_uid_and_token(self, api_client, create_user, configured_instance):
        uidb64, token = self._token_for(create_user)
        response = api_client.post(f"/auth/reset-password/{uidb64}/{token}/", {"password": WEAK})
        assert response.status_code == 302
        location = response["Location"]
        assert "PASSWORD_TOO_WEAK" in location
        # The retry MUST still be possible: uid + token survive the bounce.
        assert f"uidb64={uidb64}" in location
        assert f"token={token}" in location

    @pytest.mark.django_db
    def test_weak_with_override_resets_password(self, api_client, create_user, configured_instance):
        uidb64, token = self._token_for(create_user)
        response = api_client.post(
            f"/auth/reset-password/{uidb64}/{token}/",
            {"password": WEAK, "accept_weak_password": "True"},
        )
        assert response.status_code == 302
        assert "PASSWORD_TOO_WEAK" not in response["Location"]
        create_user.refresh_from_db()
        assert create_user.check_password(WEAK)
