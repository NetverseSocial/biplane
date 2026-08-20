# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""BIP-21: the PROFILE write path must store the canonical name.

Morrow RC 3105. Sign-up canonicalises names, but `PATCH /users/me/` is the
other door to the same column — onboarding and profile settings both use it —
and `UserSerializer` returned the value untouched. So a name the client had
validated in canonical form was persisted RAW, and the control characters the
policy exists to strip survived through this door while being stripped on
sign-up. Same defect as the original, different entrance.

His sharper point, which is why this file exists at all: my previous "what is
validated is what is stored" test asserted on `normalize_name` and never
touched storage. It could not have caught this, because the pure function was
never the thing that was broken.

So these go THROUGH the serializer and read the value back off the model
instance. If the serializer stops normalising, they fail.
"""

import pytest

from plane.app.serializers.user import UserSerializer
from plane.db.models import User

BOM = "﻿"
NEL = ""

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(db):
    return User.objects.create(email="profile-storage@netverse.invalid", username="profstore")


@pytest.mark.parametrize(
    "raw,expected,label",
    [
        (BOM + "7of9", "7of9", "leading BOM"),
        ("7of9" + NEL, "7of9", "trailing NEL"),
        (BOM + "7of9" + NEL, "7of9", "one blank from each runtime's set, both ends"),
        ("  Amelia  ", "Amelia", "ordinary spaces"),
        ("\t\nWright\r", "Wright", "tab, newline, carriage return"),
        ("Mary Jane", "Mary Jane", "an internal space is NOT touched"),
        ("7of9", "7of9", "already canonical, unchanged"),
    ],
)
def test_first_name_is_stored_canonical(user, raw, expected, label):
    serializer = UserSerializer(user, data={"first_name": raw}, partial=True)
    assert serializer.is_valid(), f"{label}: {serializer.errors}"
    saved = serializer.save()

    # Read back off the instance, and again off a fresh DB fetch — the claim is
    # about what is STORED, not what the serializer happened to return.
    assert saved.first_name == expected, label
    assert User.objects.get(pk=user.pk).first_name == expected, label


@pytest.mark.parametrize(
    "raw,expected",
    [(BOM + "Wright" + NEL, "Wright"), ("   ", ""), ("", "")],
)
def test_last_name_is_stored_canonical(user, raw, expected):
    serializer = UserSerializer(user, data={"last_name": raw}, partial=True)
    assert serializer.is_valid(), serializer.errors
    serializer.save()
    assert User.objects.get(pk=user.pk).last_name == expected


def test_a_blank_only_last_name_is_stored_as_absent(user):
    # The optional field. Blank-after-normalisation must land as empty rather
    # than as a string of invisible characters that later reads as "present".
    serializer = UserSerializer(user, data={"last_name": BOM + NEL + "  "}, partial=True)
    assert serializer.is_valid(), serializer.errors
    serializer.save()
    stored = User.objects.get(pk=user.pk).last_name
    assert stored == "", repr(stored)


def test_the_url_rejection_still_fires(user):
    # The validator's pre-existing job. Normalising must not have replaced it.
    serializer = UserSerializer(user, data={"first_name": "http://example.com"}, partial=True)
    assert serializer.is_valid() is False
    assert "first_name" in serializer.errors


def test_an_ordinary_name_round_trips_untouched(user):
    # Without this, a validator that returned "" for everything would pass all
    # the canonicalisation assertions above.
    serializer = UserSerializer(user, data={"first_name": "Amelia", "last_name": "Wright"}, partial=True)
    assert serializer.is_valid(), serializer.errors
    serializer.save()
    fresh = User.objects.get(pk=user.pk)
    assert (fresh.first_name, fresh.last_name) == ("Amelia", "Wright")
