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
            {"email": "weak-door@example.com", "password": WEAK, "first_name": "Weak"},
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
    def test_signup_missing_first_name_is_clean_4xx(self, api_client, configured_instance):
        response = api_client.post(
            "/auth/sign-up/", {"email": "noname@example.com", "password": STRONG}
        )
        assert response.status_code == 302
        assert "REQUIRED_FIRST_NAME_SIGN_UP" in response["Location"]
        assert not User.objects.filter(email="noname@example.com").exists()

    @pytest.mark.django_db
    def test_signup_invalid_names_are_clean_4xx_not_stored(self, api_client, configured_instance):
        # RC 3028: markup, overlong, DEL and C1 controls must all REJECT — the
        # original bar was validation, not silent canonicalization.
        for bad in ("<script>x</script>", "A" * 300, "Bell\x7fCurve", "Bell\x85Curve", "Bell\u2028Curve", "Bell\u200bCurve"):
            response = api_client.post(
                "/auth/sign-up/",
                {"email": "badname@example.com", "password": STRONG, "first_name": bad},
            )
            assert response.status_code == 302, repr(bad)
            assert "INVALID_NAME_SIGN_UP" in response["Location"], repr(bad)
            assert not User.objects.filter(email="badname@example.com").exists(), repr(bad)

    @pytest.mark.django_db
    def test_signup_unicode_names_stored_verbatim(self, api_client, configured_instance):
        response = api_client.post(
            "/auth/sign-up/",
            {
                "email": "jose@example.com",
                "password": STRONG,
                "first_name": "José",
                "last_name": "O'Brien-Smith Jr.",
            },
        )
        assert response.status_code == 302
        assert "INVALID_NAME" not in response["Location"]
        user = User.objects.get(email="jose@example.com")
        assert user.first_name == "José"
        assert user.last_name == "O'Brien-Smith Jr."


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
