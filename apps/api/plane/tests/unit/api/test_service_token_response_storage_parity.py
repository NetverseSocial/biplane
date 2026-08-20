# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""BIP-18: the 201 body must agree with the database — through the real route.

Morrow's preflight on PR #22: DRF caches `serializer.data` on first access, and
the identity stamped by `creation_identity` landed on a separately refetched
object. An ordinary token cannot see the split (both identities are the
caller); a SERVICE token asserting another author stored the asserted values
while the response reported the pre-override ones. Helper-level tests cannot
see serializer caching, so these go through the URLconf with a real APIToken.
"""

import uuid as uuid_lib

import pytest
from django.utils import timezone as dj_timezone
from django.utils.dateparse import parse_datetime
from rest_framework.test import APIClient

from plane.db.models import (
    Issue,
    IssueAssignee,
    IssueLabel,
    Label,
    Project,
    ProjectMember,
    User,
    Workspace,
    WorkspaceMember,
)
from plane.db.models.api import APIToken

ASSERTED_AT = "2020-05-04T03:02:01Z"


def _project_fixture():
    caller = User.objects.create(
        email=f"svc-{uuid_lib.uuid4().hex[:8]}@example.com", username=uuid_lib.uuid4().hex[:12]
    )
    other = User.objects.create(
        email=f"imp-{uuid_lib.uuid4().hex[:8]}@example.com", username=uuid_lib.uuid4().hex[:12]
    )
    ws = Workspace.objects.create(slug=f"s{uuid_lib.uuid4().hex[:10]}", name="S", owner=caller)
    WorkspaceMember.objects.create(workspace=ws, member=caller, role=20)
    proj = Project.objects.create(workspace=ws, name="P", identifier="SVC" + uuid_lib.uuid4().hex[:2].upper())
    ProjectMember.objects.create(workspace=ws, project=proj, member=caller, role=20, is_active=True)
    return caller, other, ws, proj


def _client_for(user, is_service):
    token = APIToken.objects.create(
        user=user, label="t", token=f"tok-{uuid_lib.uuid4().hex}", is_service=is_service
    )
    client = APIClient()
    client.credentials(HTTP_X_API_KEY=token.token)
    return client


def _post_issue(client, ws, proj, other):
    return client.post(
        f"/api/v1/workspaces/{ws.slug}/projects/{proj.id}/issues/",
        {
            "name": "parity probe",
            "created_by": str(other.id),
            "created_at": ASSERTED_AT,
        },
        format="json",
    )


@pytest.mark.django_db
class TestServiceTokenResponseStorageParity:
    def test_asserted_identity_agrees_between_response_and_row(self):
        caller, other, ws, proj = _project_fixture()
        r = _post_issue(_client_for(caller, is_service=True), ws, proj, other)
        assert r.status_code == 201, r.content

        row = Issue.objects.get(pk=r.data["id"])
        # Storage honoured the assertion...
        assert str(row.created_by_id) == str(other.id)
        assert row.created_at == parse_datetime(ASSERTED_AT)
        # ...and the response reports THE SAME values, both fields. This is
        # the regression: the cached representation used to report the caller
        # and the server clock while the row said otherwise.
        assert str(r.data["created_by"]) == str(other.id)
        assert parse_datetime(str(r.data["created_at"])) == row.created_at

    def test_child_rows_carry_the_asserted_author_too(self):
        # Morrow 10161 blocking 1: IssueSerializer.create copies
        # issue.created_by_id into every assignee/label row AT CREATION, so a
        # post-save stamp left them attributed to the pre-override caller —
        # internally contradictory audit data. Read the child rows from the
        # DB, not the response.
        caller, other, ws, proj = _project_fixture()
        label = Label.objects.create(project=proj, workspace=ws, name="import", created_by=caller)
        r = _client_for(caller, is_service=True).post(
            f"/api/v1/workspaces/{ws.slug}/projects/{proj.id}/issues/",
            {
                "name": "child parity probe",
                "created_by": str(other.id),
                "created_at": ASSERTED_AT,
                "assignees": [str(caller.id)],
                "labels": [str(label.id)],
            },
            format="json",
        )
        assert r.status_code == 201, r.content
        assignee_rows = IssueAssignee.objects.filter(issue_id=r.data["id"])
        label_rows = IssueLabel.objects.filter(issue_id=r.data["id"])
        assert assignee_rows.count() == 1 and label_rows.count() == 1
        assert str(assignee_rows.get().created_by_id) == str(other.id)
        assert str(label_rows.get().created_by_id) == str(other.id)

    def test_child_rows_carry_the_callers_identity_for_an_ordinary_token(self):
        caller, other, ws, proj = _project_fixture()
        r = _client_for(caller, is_service=False).post(
            f"/api/v1/workspaces/{ws.slug}/projects/{proj.id}/issues/",
            {
                "name": "child refusal probe",
                "created_by": str(other.id),
                "assignees": [str(caller.id)],
            },
            format="json",
        )
        assert r.status_code == 201, r.content
        rows = IssueAssignee.objects.filter(issue_id=r.data["id"])
        assert rows.count() == 1
        assert str(rows.get().created_by_id) == str(caller.id)

    def test_ordinary_token_response_and_row_both_ignore_the_assertion(self):
        caller, other, ws, proj = _project_fixture()
        before = dj_timezone.now()
        r = _post_issue(_client_for(caller, is_service=False), ws, proj, other)
        assert r.status_code == 201, r.content

        row = Issue.objects.get(pk=r.data["id"])
        # Parity in the refusing direction: row AND body both carry the
        # caller's identity and the server clock, not the asserted values.
        assert str(row.created_by_id) == str(caller.id)
        assert row.created_at >= before
        assert str(r.data["created_by"]) == str(caller.id)
        assert parse_datetime(str(r.data["created_at"])) == row.created_at
