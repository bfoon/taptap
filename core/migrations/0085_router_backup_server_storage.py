from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0084_visual_control_designer"),
    ]

    operations = [
        migrations.CreateModel(
            name="RouterBackupStorage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(
                    choices=[
                        ("queued", "Queued on router"),
                        ("uploading", "Copying to server"),
                        ("ready", "Saved on server"),
                        ("partial", "Partly saved on server"),
                        ("failed", "Server copy failed"),
                        ("legacy", "Legacy router-only record"),
                    ],
                    db_index=True,
                    default="queued",
                    max_length=20,
                )),
                ("backup_path", models.CharField(blank=True, max_length=500)),
                ("export_path", models.CharField(blank=True, max_length=500)),
                ("expected_backup_size", models.BigIntegerField(default=0)),
                ("expected_export_size", models.BigIntegerField(default=0)),
                ("backup_size", models.BigIntegerField(default=0)),
                ("server_export_size", models.BigIntegerField(default=0)),
                ("backup_sha256", models.CharField(blank=True, max_length=64)),
                ("export_sha256", models.CharField(blank=True, max_length=64)),
                ("upload_token_hash", models.CharField(blank=True, max_length=64)),
                ("upload_expires_at", models.DateTimeField(blank=True, null=True)),
                ("error", models.TextField(blank=True)),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("backup", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="server_storage",
                    to="core.routerbackup",
                )),
            ],
            options={"ordering": ["-started_at"]},
        ),
    ]
