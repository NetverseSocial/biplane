from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("db", "0121_alter_estimate_type")]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="time_format",
            field=models.CharField(default="12-hour", max_length=10),
        ),
    ]
