# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""M5.2 (Morrow RC 3392 #2): the comparable installed RELEASE VERSION,
distinct from the exact commit-derived build id. Additive, nullable —
NULL means the deployment cannot honestly name a release version and the
update check reports UNKNOWN."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("license", "0008_biplane_version_fields")]

    operations = [
        migrations.AddField(
            model_name="instance",
            name="biplane_installed_version",
            field=models.CharField(max_length=255, null=True, blank=True),
        ),
    ]
