# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Third party modules
from rest_framework import status
from rest_framework.response import Response
from django.db.models import Q

# Module imports
from plane.app.serializers.workflow_template import WorkflowTemplateSerializer
from plane.app.views.base import BaseAPIView
from plane.db.models import WorkflowTemplate, Workspace
from plane.app.permissions import WorkspaceViewerPermission, WorkspaceEntityPermission

# Validation lives in plane.utils so project creation can revalidate persisted
# templates at point of use without a view-to-view import. Re-exported here for
# existing importers/tests.
from plane.utils.workflow_template_validation import (  # noqa: F401
    MAX_TEMPLATE_STATES,
    STATE_NAME_MAX_LENGTH,
    _normalize_states,
    _validate_states,
)


class WorkspaceWorkflowTemplateEndpoint(BaseAPIView):
    """biplane: list/create/update/delete workflow templates for a workspace.
    System (built-in) templates are shared and read-only; workspace templates are
    created and edited here (Phase 2)."""

    def get_permissions(self):
        if self.request.method == "GET":
            return [WorkspaceViewerPermission()]
        return [WorkspaceEntityPermission()]

    def get(self, request, slug):
        workspace = Workspace.objects.filter(slug=slug).first()
        templates = WorkflowTemplate.objects.filter(Q(is_system=True) | Q(workspace=workspace))
        return Response(WorkflowTemplateSerializer(templates, many=True).data, status=status.HTTP_200_OK)

    def post(self, request, slug):
        workspace = Workspace.objects.filter(slug=slug).first()
        states = request.data.get("states", [])
        err = _validate_states(states)
        if err:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
        template = WorkflowTemplate.objects.create(
            workspace=workspace,
            name=str(request.data.get("name", "")).strip() or "Untitled workflow",
            description=str(request.data.get("description", "")).strip(),
            is_system=False,
            states=_normalize_states(states),
        )
        return Response(WorkflowTemplateSerializer(template).data, status=status.HTTP_201_CREATED)

    def patch(self, request, slug, pk):
        template = WorkflowTemplate.objects.filter(pk=pk, workspace__slug=slug, is_system=False).first()
        if not template:
            return Response(
                {"error": "Template not found, or it is a read-only built-in."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if "states" in request.data:
            err = _validate_states(request.data["states"])
            if err:
                return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
            template.states = _normalize_states(request.data["states"])
        if "name" in request.data:
            template.name = str(request.data["name"]).strip() or template.name
        if "description" in request.data:
            template.description = str(request.data["description"]).strip()
        template.save()
        return Response(WorkflowTemplateSerializer(template).data, status=status.HTTP_200_OK)

    def delete(self, request, slug, pk):
        template = WorkflowTemplate.objects.filter(pk=pk, workspace__slug=slug, is_system=False).first()
        if not template:
            return Response(
                {"error": "Template not found, or it is a read-only built-in."},
                status=status.HTTP_404_NOT_FOUND,
            )
        template.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
