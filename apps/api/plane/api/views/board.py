# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""M8 board service boundary (BIP-37).

This is the §M8.1 query a caller makes after an unknown transport result,
BEFORE any retry. Its 404 is a SAFE answer — "never committed" — because the
`BoardOperation` row commits in the same transaction as the mutation it
records; there is no window where the mutation exists and the outcome does
not. Every caller queries before retrying an unknown transport result."""

# Module imports
from plane.api.serializers import BoardOperationCreateSerializer, IssueSerializer
from plane.api.views.base import BaseAPIView
from plane.app.permissions import ProjectEntityPermission
from plane.board import (
    BoardOperationConflict,
    BoardOperationNotFound,
    BoardOperationPermissionDenied,
    execute_transition,
)
from plane.db.models import BoardOperation, Issue

# Third party imports
from rest_framework import status
from rest_framework.response import Response

_MAX_PAGE_LIMIT = 1000
_MAX_SEQUENCE_ID = 2_147_483_647


def _operation_response(row, replayed):
    return {
        "op_key": row.op_key,
        "source": row.source,
        "verb": row.verb,
        "workspace": row.workspace_slug,
        "project": row.project_identifier,
        "outcome": row.outcome,
        "replayed": replayed,
    }


class BoardOperationCreateAPIEndpoint(BaseAPIView):
    """POST /api/v1/board/ops/ — the only board mutation door."""

    def post(self, request):
        serializer = BoardOperationCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            result = execute_transition(principal=request.user, envelope=serializer.validated_data)
        except BoardOperationConflict:
            return Response(
                {"detail": "operation key is already bound to a different request"},
                status=status.HTTP_409_CONFLICT,
            )
        except BoardOperationPermissionDenied:
            return Response(
                {"detail": "principal cannot mutate this project"},
                status=status.HTTP_403_FORBIDDEN,
            )
        except BoardOperationNotFound:
            return Response(
                {"detail": "project, work item, or target state not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        response_status = status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED
        return Response(_operation_response(result.row, result.replayed), status=response_status)


class BoardOperationDetailAPIEndpoint(BaseAPIView):
    """GET /api/v1/board/ops/<op_key>/ — the stored outcome, principal-scoped.

    The principal is the SERVER-BOUND token identity (M7): it is never read
    from the request body or query string, so one principal can neither replay
    against nor read another's outcomes — a wrong-principal probe and a
    never-committed key are indistinguishable 404s, deliberately."""

    def get(self, request, op_key):
        row = BoardOperation.objects.filter(principal=str(request.user.id), op_key=op_key).first()
        if row is None:
            return Response(
                {"detail": "no committed operation under this key for this principal"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(_operation_response(row, True), status=status.HTTP_200_OK)


def _positive_int(value, name, maximum):
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive integer")
    if parsed <= 0 or str(parsed) != value:
        raise ValueError(f"{name} must be a positive integer")
    if parsed > maximum:
        raise ValueError(f"{name} must not exceed {maximum}")
    return parsed


class BoardWorkItemMixin:
    """Shared, path-bound scope for the complete board read surface."""

    permission_classes = [ProjectEntityPermission]

    @property
    def workspace_slug(self):
        return self.kwargs["workspace_slug"]

    @property
    def project_identifier(self):
        return self.kwargs["project_identifier"]

    def work_items(self):
        return (
            Issue.issue_objects.filter(
                workspace__slug=self.workspace_slug,
                project__identifier=self.project_identifier,
            )
            .select_related("workspace", "project", "state", "parent")
            .prefetch_related("assignees", "labels")
            .order_by("sequence_id", "id")
        )


class BoardWorkItemListAPIEndpoint(BoardWorkItemMixin, BaseAPIView):
    """Return the complete project set unless the caller explicitly bounds it.

    ``limit`` is the only truncation mechanism in v1. A bounded response always
    says whether more rows exist and carries the next sequence cursor; an
    unbounded response exhausts the queryset and is explicitly complete.
    """

    def get(self, request, workspace_slug, project_identifier):
        try:
            cursor = _positive_int(request.query_params.get("cursor"), "cursor", _MAX_SEQUENCE_ID)
            limit = _positive_int(request.query_params.get("limit"), "limit", _MAX_PAGE_LIMIT)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        queryset = self.work_items()
        if cursor is not None:
            queryset = queryset.filter(sequence_id__gt=cursor)

        if limit is None:
            rows = list(queryset)
            truncated = False
        else:
            rows = list(queryset[: limit + 1])
            truncated = len(rows) > limit
            rows = rows[:limit]

        return Response(
            {
                "items": IssueSerializer(rows, many=True).data,
                "count": len(rows),
                "complete": not truncated,
                "truncated": truncated,
                "next_cursor": str(rows[-1].sequence_id) if truncated else None,
            },
            status=status.HTTP_200_OK,
        )


class BoardWorkItemDetailAPIEndpoint(BoardWorkItemMixin, BaseAPIView):
    """Read back one work item from the store by stable board identifiers."""

    def get(self, request, workspace_slug, project_identifier, sequence_id):
        row = self.work_items().filter(sequence_id=sequence_id).first()
        if row is None:
            return Response(
                {"detail": "work item not found in this project"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(IssueSerializer(row).data, status=status.HTTP_200_OK)
