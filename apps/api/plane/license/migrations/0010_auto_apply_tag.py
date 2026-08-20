# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The automatic mode's once-per-tag guard (ticket 69): an APPEND-ONLY
attempts table with a DB unique constraint. The first design — a single
last-tag column — regresses under newer -> stale-worker -> newer ordering
(Rowan, review 3834): a stale worker's compare-and-set rolls the field
backward and the newer tag is attempted twice. An insert into a unique
column cannot regress; the database is the arbiter whatever order workers
arrive in. The claim is written before the request goes out, so a failed
or process-killing apply is attempted once, not hourly."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("license", "0009_biplane_installed_version"),
    ]

    operations = [
        migrations.CreateModel(
            name="BiplaneAutoApplyAttempt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("tag", models.CharField(max_length=255, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "db_table": "biplane_auto_apply_attempts",
                "ordering": ("-created_at",),
            },
        ),
    ]
