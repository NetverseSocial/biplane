# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.
"""Poll cursor for the git-bridge polling transport (BIP-46 PR-B2).

Identity is (provider_instance, repo_stable_id), both NOT NULL. The display
path and the provider family are each ruled out by ADR 010 §1 — see
plane/db/models/poll_cursor.py for the reasoning and the two losslessness
failures the earlier (forge, repo_full_name) key admitted.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    dependencies = [
        ("db", "0129_backfill_issue_description_stripped"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PollCursor",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Created At")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Last Modified At")),
                ("deleted_at", models.DateTimeField(blank=True, null=True, verbose_name="Deleted At")),
                (
                    "id",
                    models.UUIDField(
                        db_index=True,
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                        unique=True,
                    ),
                ),
                ("provider_instance", models.CharField(max_length=255)),
                ("repo_stable_id", models.BigIntegerField()),
                ("forge", models.CharField(max_length=32)),
                ("repo_full_name", models.CharField(blank=True, default="", max_length=512)),
                ("position", models.JSONField(blank=True, default=dict)),
                ("watermark_at", models.DateTimeField(blank=True, null=True)),
                ("last_polled_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.TextField(blank=True, null=True)),
                ("gap_detected", models.BooleanField(default=False)),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_created_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Created By",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_updated_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Last Modified By",
                    ),
                ),
            ],
            options={
                "verbose_name": "Poll Cursor",
                "verbose_name_plural": "Poll Cursors",
                "db_table": "git_bridge_poll_cursors",
                "ordering": ("created_at",),
            },
        ),
        migrations.AddConstraint(
            model_name="pollcursor",
            constraint=models.UniqueConstraint(
                fields=("provider_instance", "repo_stable_id"),
                name="uniq_poll_cursor_instance_repo",
            ),
        ),
    ]
