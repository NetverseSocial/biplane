# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""biplane (BIP-36): give Biplane its own version fields.

Split out of BIP-32, whose PR was narrowed in review (Morrow RC 3259) precisely
because there was nowhere correct to put a Biplane release tag: `current_version`
and `latest_version` are a pair in PLANE's namespace, and `InstanceSerializer`
exposes both as one comparable sequence.

All four columns are nullable and additive. On PostgreSQL 11+ (the stack pins
postgres:15.7-alpine) `ADD COLUMN` with no default — or a constant default — is
catalog-only, so there is no table rewrite regardless of row count. `instances`
holds a single row in practice anyway. Brief ACCESS EXCLUSIVE on that one table;
reverses cleanly; no data is read or written.

NULL means UNKNOWN in every one of these columns, and that is load-bearing
rather than incidental: the defect BIP-32 fixed was an update check that
returned the RUNNING version when it could not reach its source, so a network
error rendered as "you are up to date". Nothing may default these to a
present-looking value.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("license", "0007_telemetry_default_off")]

    operations = [
        migrations.AddField(
            model_name="instance",
            name="biplane_installed_build",
            field=models.CharField(max_length=255, null=True, blank=True),
        ),
        migrations.AddField(
            model_name="instance",
            name="biplane_latest_version",
            field=models.CharField(max_length=255, null=True, blank=True),
        ),
        migrations.AddField(
            model_name="instance",
            name="biplane_latest_source",
            field=models.CharField(max_length=32, null=True, blank=True),
        ),
        migrations.AddField(
            model_name="instance",
            name="biplane_latest_checked_at",
            field=models.DateTimeField(null=True, blank=True),
        ),
    ]
