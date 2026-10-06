from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0079_sticky_exclusion_rules"),
    ]

    operations = [
        migrations.CreateModel(
            name="FairUsageManualStep",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("period_key", models.CharField(max_length=20)),
                (
                    "tier",
                    models.PositiveSmallIntegerField(
                        default=0,
                        help_text="Manual effective step: 0 = full speed, 1 = first FUP step, etc.",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "set_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="auth.user",
                    ),
                ),
                (
                    "state",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="manual_override",
                        to="core.fairusagestate",
                    ),
                ),
            ],
            options={"ordering": ["-updated_at", "-pk"]},
        ),
    ]
