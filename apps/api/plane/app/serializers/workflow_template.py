# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from .base import BaseSerializer
from plane.db.models import WorkflowTemplate


class WorkflowTemplateSerializer(BaseSerializer):
    class Meta:
        model = WorkflowTemplate
        fields = [
            "id",
            "name",
            "description",
            "is_system",
            "states",
            "workspace",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "is_system", "workspace", "created_at", "updated_at"]
