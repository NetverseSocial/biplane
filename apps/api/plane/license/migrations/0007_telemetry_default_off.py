# Biplane: telemetry is opt-in. New instances default to telemetry disabled;
# existing rows keep whatever the operator chose.
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("license", "0006_instance_is_current_version_deprecated"),
    ]

    operations = [
        migrations.AlterField(
            model_name="instance",
            name="is_telemetry_enabled",
            field=models.BooleanField(default=False),
        ),
    ]
