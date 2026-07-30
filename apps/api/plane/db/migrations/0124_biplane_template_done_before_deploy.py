# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# biplane: John's actual workflow is …Integration Test, DONE, Deploy — work completes,
# deployment is optional/after (stated twice; 0123 seeded Deploy before Done in error).
# Reorders the built-in Biplane template's state sequences. Projects already created
# from the old order keep their own state rows untouched.

from django.db import migrations

ORDERED = [
    "Backlog",
    "Todo",
    "Design",
    "Code & TDD",
    "Review",
    "Integration Test",
    "Done",
    "Deploy",
    "Cancelled",
    "Triage",
]


def reorder_biplane_template(apps, schema_editor):
    WorkflowTemplate = apps.get_model("db", "WorkflowTemplate")
    template = WorkflowTemplate.objects.filter(is_system=True, name="Biplane").first()
    if template is None or not template.states:
        return
    by_name = {s.get("name"): s for s in template.states if isinstance(s, dict)}
    if set(by_name) != set(ORDERED):
        # Template was altered out-of-band — do not guess.
        return
    states = []
    for i, name in enumerate(ORDERED):
        entry = dict(by_name[name])
        entry["sequence"] = (i + 1) * 15000
        states.append(entry)
    template.states = states
    template.save(update_fields=["states"])


def restore_deploy_before_done(apps, schema_editor):
    WorkflowTemplate = apps.get_model("db", "WorkflowTemplate")
    template = WorkflowTemplate.objects.filter(is_system=True, name="Biplane").first()
    if template is None or not template.states:
        return
    old = [
        "Backlog", "Todo", "Design", "Code & TDD", "Review",
        "Integration Test", "Deploy", "Done", "Cancelled", "Triage",
    ]
    by_name = {s.get("name"): s for s in template.states if isinstance(s, dict)}
    if set(by_name) != set(old):
        return
    states = []
    for i, name in enumerate(old):
        entry = dict(by_name[name])
        entry["sequence"] = (i + 1) * 15000
        states.append(entry)
    template.states = states
    template.save(update_fields=["states"])


class Migration(migrations.Migration):
    dependencies = [
        ("db", "0123_workflow_template"),
    ]

    operations = [
        migrations.RunPython(reorder_biplane_template, restore_deploy_before_done),
    ]
