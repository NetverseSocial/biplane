# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import os
import sys

import pytest
from rest_framework.test import APIClient
from pytest_django.fixtures import django_db_setup

from plane.db.models import User, Workspace, WorkspaceMember
from plane.db.models.api import APIToken

_PG_IDENT_MAX_BYTES = 63  # Postgres truncates identifiers past this many BYTES, not chars.


def isolated_test_db_name(configured_name, environ, testrun_uid, worker_id):
    """A CROSS-CONTAINER, INVOCATION-UNIQUE, PER-WORKER test-database name (BIP-63).

    Consumes xdist's OWN identifiers (Morrow RC 3649), not a per-process token:
      * ``testrun_uid`` is shared across every worker of one invocation and is
        unique per run — so the database is INVOCATION-owned: one run's workers
        agree on it and no other run can reach it. (A per-process token instead
        made each -n worker pick a different name and left the header naming none
        of them.)
      * ``worker_id`` (``gw0``.. under -n, ``master`` otherwise) distinguishes the
        workers of one run, which each need their own database.

    Both are ASCII and are kept WHOLE — uniqueness is load-bearing. The name is
    bounded to the Postgres 63-BYTE identifier limit (isalnum admits multibyte
    Unicode); only the base+label head is trimmed, on a UTF-8 boundary.

    ``BIP_TEST_DB_SUFFIX`` (e.g. the agent name) is a readable label so an operator
    can see whose database is whose; the database is dropped at session end
    (pytest_configure), so the label is for the SIGKILL-residue case, not reuse.
    """
    base = configured_name or "test"
    label = "".join(ch for ch in (environ.get("BIP_TEST_DB_SUFFIX") or "") if ch.isalnum())
    tail = "_{}_{}".format(worker_id, testrun_uid)  # ASCII, kept whole
    head = base if not label else "{}_{}".format(base, label)
    budget = _PG_IDENT_MAX_BYTES - len(tail.encode("utf-8"))
    head = head.encode("utf-8")[:budget].decode("utf-8", "ignore")  # drop a split multibyte tail
    return head + tail


def pytest_configure(config):
    """Own the per-invocation database's whole lifecycle through pytest-django.

    The name is deliberately unique per run, so ``--reuse-db`` (from addopts) would
    only mean "keep a database the next run can never reach" — a leak on a shared
    server (Aria, BIP-63). Force reuse off so pytest-django's OWN database fixture
    creates and then DROPS it, keeping connection handling and teardown order with
    the fixture that owns them — no separate session dropper, no global drop-loop
    that could hit another agent's live database (Morrow RC ruling).
    """
    config.option.reuse_db = False


@pytest.fixture(scope="session")
def django_db_modify_db_settings(django_db_modify_db_settings, testrun_uid, worker_id):  # noqa: F811
    """Point this worker at its own isolated test database (BIP-63), named from
    xdist's shared ``testrun_uid`` + this ``worker_id``, and EMIT the exact name.

    Every worker emits its OWN name (the controller cannot know them all), so a
    hard-kill leftover is recoverable BY EXACT NAME — never a prefix sweep. The
    write goes past pytest's capture so it surfaces in this process's log.

    The ``plane`` role already has CREATEDB, so no new grants are needed (the
    per-agent DBs Morrow and Sia already use prove the shape).
    """
    from django.conf import settings

    default = settings.DATABASES["default"]
    test = default.setdefault("TEST", {})
    base = "test_{}".format(default["NAME"])
    name = isolated_test_db_name(base, os.environ, testrun_uid, worker_id)
    test["NAME"] = name
    print("BIP-63 isolated test database [{}]: {}".format(worker_id, name), file=sys.__stderr__, flush=True)


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup):  # noqa: F811
    """Set up the Django database for the test session"""
    pass


@pytest.fixture
def api_client():
    """Return an unauthenticated API client"""
    return APIClient()


@pytest.fixture
def user_data():
    """Return standard user data for tests"""
    return {
        "email": "test@plane.so",
        "password": "test-password",
        "first_name": "Test",
        "last_name": "User",
    }


@pytest.fixture
def create_user(db, user_data):
    """Create and return a user instance"""
    user = User.objects.create(
        email=user_data["email"],
        first_name=user_data["first_name"],
        last_name=user_data["last_name"],
    )
    user.set_password(user_data["password"])
    user.save()
    return user


@pytest.fixture
def api_token(db, create_user):
    """Create and return an API token for testing the external API"""
    token = APIToken.objects.create(
        user=create_user,
        label="Test API Token",
        token="test-api-token-12345",
    )
    return token


@pytest.fixture
def api_key_client(api_client, api_token):
    """Return an API key authenticated client for external API testing"""
    api_client.credentials(HTTP_X_API_KEY=api_token.token)
    return api_client


@pytest.fixture
def session_client(api_client, create_user):
    """Return a session authenticated API client for app API testing, which is what plane.app uses"""
    api_client.force_authenticate(user=create_user)
    return api_client


@pytest.fixture
def create_bot_user(db):
    """Create and return a bot user instance"""
    from uuid import uuid4

    unique_id = uuid4().hex[:8]
    user = User.objects.create(
        email=f"bot-{unique_id}@plane.so",
        username=f"bot_user_{unique_id}",
        first_name="Bot",
        last_name="User",
        is_bot=True,
    )
    user.set_password("bot@123")
    user.save()
    return user


@pytest.fixture
def api_token_data():
    """Return sample API token data for testing"""
    from django.utils import timezone
    from datetime import timedelta

    return {
        "label": "Test API Token",
        "description": "Test description for API token",
        "expired_at": (timezone.now() + timedelta(days=30)).isoformat(),
    }


@pytest.fixture
def create_api_token_for_user(db, create_user):
    """Create and return an API token for a specific user"""
    return APIToken.objects.create(
        label="Test Token",
        description="Test token description",
        user=create_user,
        user_type=0,
    )


@pytest.fixture
def plane_server(live_server):
    """
    Renamed version of live_server fixture to avoid name clashes.
    Returns a live Django server for testing HTTP requests.
    """
    return live_server


@pytest.fixture
def workspace(create_user):
    """
    Create a new workspace and return the
    corresponding Workspace model instance.
    """
    # Create the workspace using the model
    created_workspace = Workspace.objects.create(
        name="Test Workspace",
        owner=create_user,
        slug="test-workspace",
    )

    WorkspaceMember.objects.create(workspace=created_workspace, member=create_user, role=20)

    return created_workspace
