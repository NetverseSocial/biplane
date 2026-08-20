# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.urls import path

from plane.api.views.board import (
    BoardOperationCreateAPIEndpoint,
    BoardOperationDetailAPIEndpoint,
    BoardWorkItemDetailAPIEndpoint,
    BoardWorkItemListAPIEndpoint,
)

urlpatterns = [
    path(
        "board/ops/",
        BoardOperationCreateAPIEndpoint.as_view(),
        name="board-operation-create",
    ),
    path(
        "board/ops/<str:op_key>/",
        BoardOperationDetailAPIEndpoint.as_view(),
        name="board-operation-detail",
    ),
    path(
        "board/work-items/<str:workspace_slug>/<str:project_identifier>/",
        BoardWorkItemListAPIEndpoint.as_view(),
        name="board-work-item-list",
    ),
    path(
        "board/work-items/<str:workspace_slug>/<str:project_identifier>/<int:sequence_id>/",
        BoardWorkItemDetailAPIEndpoint.as_view(),
        name="board-work-item-detail",
    ),
]
