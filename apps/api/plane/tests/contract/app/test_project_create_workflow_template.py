# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# biplane: adversarial coverage for project creation from a workflow template.
# The invariant under test: project + members + states land together or NOT AT ALL —
# a bad template must never leave a half-created, stateless project behind (states
# hold the project's name/identifier hostage and Issue.save() would write work items
# with state=None).

import pytest
from rest_framework import status

from plane.db.models import Project, ProjectMember, State, WorkflowTemplate

BIPLANE_STATES = [
    {"name": "Backlog", "group": "backlog", "color": "#60646C", "sequence": 15000, "default": True},
    {"name": "Todo", "group": "unstarted", "color": "#3b82f6", "sequence": 30000},
    {"name": "Design", "group": "started", "color": "#F59E0B", "sequence": 45000},
    {"name": "Code & TDD", "group": "started", "color": "#F59E0B", "sequence": 60000},
    {"name": "Review", "group": "started", "color": "#F59E0B", "sequence": 75000},
    {"name": "Deploy", "group": "started", "color": "#F59E0B", "sequence": 90000},
    {"name": "Done", "group": "completed", "color": "#46A758", "sequence": 105000},
    {"name": "Cancelled", "group": "cancelled", "color": "#ef4444", "sequence": 120000},
]


def _project_url(slug):
    return f"/api/workspaces/{slug}/projects/"


def _post_project(client, workspace, **extra):
    payload = {"name": "Template Proof", "identifier": "TPROOF", **extra}
    return client.post(_project_url(workspace.slug), payload, format="json")


@pytest.mark.contract
class TestProjectCreateFromWorkflowTemplate:
    @pytest.mark.django_db
    def test_valid_template_states_adopted(self, session_client, workspace, create_user):
        """Adoption proof: the created project carries exactly the template's states."""
        session_client.force_authenticate(user=create_user)
        template = WorkflowTemplate.objects.create(
            workspace=workspace, name="Biplane-ish", is_system=False, states=BIPLANE_STATES
        )

        response = _post_project(session_client, workspace, workflow_template_id=str(template.id))

        assert response.status_code == status.HTTP_201_CREATED
        project = Project.objects.get(name="Template Proof")
        names = set(State.objects.filter(project=project).values_list("name", flat=True))
        assert names == {s["name"] for s in BIPLANE_STATES}
        default_states = State.objects.filter(project=project, default=True)
        assert default_states.count() == 1
        assert default_states.first().name == "Backlog"

    @pytest.mark.django_db
    def test_legacy_duplicate_name_template_rolls_back_everything(self, session_client, workspace, create_user):
        """Rollback proof: a template with duplicate state names (inserted directly,
        bypassing the API validator — the legacy-data case) must produce a 400 and
        leave NO project, member, or state rows behind."""
        session_client.force_authenticate(user=create_user)
        bad_states = BIPLANE_STATES + [{"name": "Todo", "group": "started", "color": "#000000", "sequence": 135000}]
        template = WorkflowTemplate.objects.create(
            workspace=workspace, name="Legacy Dup", is_system=False, states=bad_states
        )

        response = _post_project(session_client, workspace, workflow_template_id=str(template.id))

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert Project.objects.count() == 0
        assert ProjectMember.objects.count() == 0
        assert State.objects.filter(workspace=workspace).count() == 0

    @pytest.mark.django_db
    def test_legacy_over_long_name_template_rolls_back_everything(self, session_client, workspace, create_user):
        """Rollback proof for the DataError path (name > 255 chars): 400, zero residue."""
        session_client.force_authenticate(user=create_user)
        bad_states = BIPLANE_STATES + [{"name": "x" * 300, "group": "started", "color": "#000000", "sequence": 135000}]
        template = WorkflowTemplate.objects.create(
            workspace=workspace, name="Legacy Long", is_system=False, states=bad_states
        )

        response = _post_project(session_client, workspace, workflow_template_id=str(template.id))

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert Project.objects.count() == 0
        assert State.objects.filter(workspace=workspace).count() == 0

    @pytest.mark.django_db
    def test_malformed_template_id_is_400_not_500_and_creates_nothing(self, session_client, workspace, create_user):
        session_client.force_authenticate(user=create_user)

        response = _post_project(session_client, workspace, workflow_template_id="not-a-uuid")

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert Project.objects.count() == 0
        assert ProjectMember.objects.count() == 0

    @pytest.mark.django_db
    def test_unknown_template_id_is_400_not_silent_default_fallback(self, session_client, workspace, create_user):
        """An id that resolves to nothing must be a client error — silently falling
        back to default states would hide client bugs (and deleted templates)."""
        session_client.force_authenticate(user=create_user)

        response = _post_project(
            session_client, workspace, workflow_template_id="00000000-0000-0000-0000-000000000000"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert Project.objects.count() == 0

    @pytest.mark.django_db
    def test_no_template_id_still_creates_default_states(self, session_client, workspace, create_user):
        """The OSS default path is untouched: no template id → stock Plane states."""
        session_client.force_authenticate(user=create_user)

        response = _post_project(session_client, workspace)

        assert response.status_code == status.HTTP_201_CREATED
        project = Project.objects.get(name="Template Proof")
        assert State.objects.filter(project=project).count() == 5
