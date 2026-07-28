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


def _validate_states(states):
    """Every template must cover the required groups so a created project is valid."""
    required = {"backlog", "unstarted", "started", "completed", "cancelled"}
    if not isinstance(states, list) or not states:
        return "A workflow needs at least one state."
    groups = set()
    for s in states:
        if not isinstance(s, dict) or not s.get("name") or not s.get("group"):
            return "Each state needs a name and a group."
        groups.add(s["group"])
    missing = required - groups
    if missing:
        return f"Missing a state for: {', '.join(sorted(missing))}."
    return None


def _normalize_states(states):
    out = []
    for i, s in enumerate(states):
        entry = {
            "name": str(s["name"]).strip(),
            "group": s["group"],
            "color": s.get("color") or "#60646C",
            "sequence": (i + 1) * 15000,
        }
        if s.get("default"):
            entry["default"] = True
        out.append(entry)
    # guarantee exactly one default (first backlog, else first)
    if not any(e.get("default") for e in out):
        backlog = next((e for e in out if e["group"] == "backlog"), out[0])
        backlog["default"] = True
    return out


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
