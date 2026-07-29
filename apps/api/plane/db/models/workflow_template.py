# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# biplane: reusable workflow-state templates. A template is a named set of states
# (name/group/color/sequence) that a new project can adopt instead of the single
# hardcoded default set. System templates (workspace=None, is_system=True) ship with
# Biplane; workspace templates are user-created (Phase 2). This exists so a workflow
# lives in the DB as reusable data AND is seeded from code — it can never silently
# vanish with a volume the way ad-hoc per-project states did.

from django.db import models

from .base import BaseModel


class WorkflowTemplate(BaseModel):
    workspace = models.ForeignKey(
        "db.Workspace",
        on_delete=models.CASCADE,
        related_name="workflow_templates",
        null=True,
        blank=True,
        help_text="Null for system/built-in templates shared across the instance.",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    # Built-in templates are read-only and shown to every workspace.
    is_system = models.BooleanField(default=False)
    # [{ "name": str, "group": str, "color": str, "sequence": int, "default"?: bool }]
    states = models.JSONField(default=list)

    class Meta:
        db_table = "workflow_templates"
        verbose_name = "Workflow Template"
        verbose_name_plural = "Workflow Templates"
        ordering = ("-is_system", "name")

    def __str__(self):
        return self.name
