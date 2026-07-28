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
from plane.app.permissions import WorkspaceViewerPermission


class WorkspaceWorkflowTemplateEndpoint(BaseAPIView):
    """biplane: list the workflow templates a project in this workspace can adopt —
    the shared system built-ins plus any this workspace has created (Phase 2)."""

    permission_classes = [WorkspaceViewerPermission]
    use_read_replica = True

    def get(self, request, slug):
        workspace = Workspace.objects.filter(slug=slug).first()
        templates = WorkflowTemplate.objects.filter(
            Q(is_system=True) | Q(workspace=workspace)
        )
        serializer = WorkflowTemplateSerializer(templates, many=True).data
        return Response(serializer, status=status.HTTP_200_OK)
