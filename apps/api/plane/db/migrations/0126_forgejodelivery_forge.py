# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("db", "0125_forgejodelivery"),
    ]

    operations = [
        migrations.AddField(
            model_name="forgejodelivery",
            name="forge",
            field=models.CharField(default="forgejo", max_length=32),
        ),
    ]
