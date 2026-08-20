# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Authorship is bound at the write boundary, not taken from the request body.

BIP-18. The bug these pin was live on our own board: a POST carrying
`created_by` returned the honest author in the response and stored the forged
one, because the overwrite happened after the response was serialised.

So the assertions here deliberately check the value that would be WRITTEN, and
the companion integration test reads back from storage rather than trusting a
response. Checking the response is the mistake that hid this for as long as it
was hidden.
"""

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

import pytest
from django.utils import timezone

from plane.api.write_identity import (
    InvalidAssertedIdentity,
    caller_may_assert_authorship,
    creation_identity,
)

ME = "99445e05-9c93-45fd-8143-2ef9412c0948"
SOMEONE_ELSE = "c3f5c450-5341-4bcc-8495-0d0e0a65d135"
LONG_AGO = "2001-01-01T00:00:00Z"


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeRequest:
    def __init__(self, data=None, auth=None, user_id=ME):
        self.data = data or {}
        self.auth = auth
        self.user = FakeUser(user_id)


@pytest.fixture
def not_a_service_token():
    with patch("plane.api.write_identity.APIToken") as token_model:
        token_model.objects.filter.return_value.exists.return_value = False
        yield


@pytest.fixture
def is_a_service_token():
    with patch("plane.api.write_identity.APIToken") as token_model:
        token_model.objects.filter.return_value.exists.return_value = True
        yield


@pytest.fixture
def asserted_user_exists():
    with patch("plane.api.write_identity.User") as user_model:
        user_model.objects.filter.return_value.exists.return_value = True
        yield


@pytest.fixture
def asserted_user_missing():
    with patch("plane.api.write_identity.User") as user_model:
        user_model.objects.filter.return_value.exists.return_value = False
        yield


class TestOrdinaryToken:
    """Every per-agent token lands here. Caller input must be ignored."""

    def test_forged_author_is_ignored(self, not_a_service_token):
        req = FakeRequest({"created_by": SOMEONE_ELSE}, auth="tok")
        actor, _ = creation_identity(req)
        assert actor == ME, "caller's created_by must never reach the row"

    def test_backdating_is_ignored(self, not_a_service_token):
        req = FakeRequest({"created_at": LONG_AGO}, auth="tok")
        _, stamped = creation_identity(req)
        assert stamped != LONG_AGO
        # and it is a real server clock reading, not merely "not the input"
        assert abs((timezone.now() - stamped).total_seconds()) < 10

    def test_both_at_once_are_ignored(self, not_a_service_token):
        req = FakeRequest({"created_by": SOMEONE_ELSE, "created_at": LONG_AGO}, auth="tok")
        actor, stamped = creation_identity(req)
        assert actor == ME
        assert stamped != LONG_AGO

    def test_clean_request_still_gets_identity(self, not_a_service_token):
        # Without this, a function that returned (None, None) would pass every
        # test above and silently break every ordinary write.
        actor, stamped = creation_identity(FakeRequest(auth="tok"))
        assert actor == ME
        assert stamped is not None

    def test_explicit_default_actor_wins_over_request_user(self, not_a_service_token):
        actor, _ = creation_identity(FakeRequest(auth="tok"), default_actor_id="fixed")
        assert actor == "fixed"


class TestServiceToken:
    """An importer preserving real history. The one case that may assert."""

    def test_may_set_author(self, is_a_service_token, asserted_user_exists):
        req = FakeRequest({"created_by": SOMEONE_ELSE}, auth="svc")
        actor, _ = creation_identity(req)
        assert str(actor) == SOMEONE_ELSE

    def test_may_backdate(self, is_a_service_token):
        req = FakeRequest({"created_at": LONG_AGO}, auth="svc")
        _, stamped = creation_identity(req)
        assert stamped == datetime(2001, 1, 1, tzinfo=dt_timezone.utc)

    def test_a_nonexistent_asserted_author_is_refused(self, is_a_service_token, asserted_user_missing):
        # Morrow 10161 blocking 3: canonical-but-nonexistent must be a
        # controlled refusal at the boundary, never a deferred FK failure.
        req = FakeRequest({"created_by": SOMEONE_ELSE}, auth="svc")
        with pytest.raises(InvalidAssertedIdentity):
            creation_identity(req)

    def test_a_non_uuid_asserted_author_is_refused(self, is_a_service_token):
        req = FakeRequest({"created_by": "not-a-uuid"}, auth="svc")
        with pytest.raises(InvalidAssertedIdentity):
            creation_identity(req)

    def test_an_unparseable_asserted_timestamp_is_refused(self, is_a_service_token):
        req = FakeRequest({"created_at": "yesterday-ish"}, auth="svc")
        with pytest.raises(InvalidAssertedIdentity):
            creation_identity(req)

    def test_falls_back_when_it_asserts_nothing(self, is_a_service_token):
        actor, stamped = creation_identity(FakeRequest(auth="svc"))
        assert actor == ME
        assert stamped is not None


class TestFailsClosed:
    """Anything not provably a service token cannot assert."""

    def test_no_token_at_all_cannot_assert(self):
        # A session-authenticated request has no token. It must not be able to
        # assert authorship, and must not blow up either.
        assert caller_may_assert_authorship(FakeRequest(auth=None)) is False

    def test_empty_token_cannot_assert(self):
        assert caller_may_assert_authorship(FakeRequest(auth="")) is False

    def test_session_request_still_gets_bound_identity(self):
        actor, stamped = creation_identity(FakeRequest({"created_by": SOMEONE_ELSE}, auth=None))
        assert actor == ME
        assert stamped is not None
