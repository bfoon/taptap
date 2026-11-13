"""Config Studio: a board of tiles per router and the deploys made from it (core/config_studio.py)."""

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0120_portal_caller"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RouterStudioBoard",
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
                ("tiles", models.JSONField(blank=True, default=list)),
                (
                    "base",
                    models.CharField(
                        choices=[
                            ("router", "This router, as TapTap last read it"),
                            ("lab-default", "Practice router: factory default"),
                            ("lab-empty", "Practice router: empty"),
                        ],
                        default="router",
                        max_length=16,
                    ),
                ),
                (
                    "chat",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text="The last few things asked of the AI helper",
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "router",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="studio_board",
                        to="core.router",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="RouterStudioDeploy",
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
                ("tiles", models.JSONField(blank=True, default=list)),
                ("summary", models.CharField(blank=True, max_length=255)),
                ("plan", models.JSONField(blank=True, default=list)),
                ("rollback_plan", models.JSONField(blank=True, default=list)),
                (
                    "manual_undo",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text="Rows the automatic undo cannot put back",
                    ),
                ),
                (
                    "link_parts",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Script pieces for TapTap Link, built by the server",
                    ),
                ),
                ("link_step", models.CharField(blank=True, max_length=24)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Being prepared"),
                            ("sending", "On its way to the router"),
                            ("applied", "On the router — waiting for “Keep”"),
                            ("kept", "Kept"),
                            ("failed", "Failed"),
                            ("undone", "Undone"),
                        ],
                        db_index=True,
                        default="draft",
                        max_length=12,
                    ),
                ),
                ("transport", models.CharField(blank=True, max_length=30)),
                ("undo_minutes", models.PositiveSmallIntegerField(default=5)),
                ("undo_until", models.DateTimeField(blank=True, null=True)),
                (
                    "undone_by",
                    models.CharField(
                        blank=True,
                        help_text="user | router (the timer) | failure",
                        max_length=20,
                    ),
                ),
                ("error", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "backup",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="core.routerbackup",
                    ),
                ),
                (
                    "business",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="studio_deploys",
                        to="core.business",
                    ),
                ),
                (
                    "router",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="studio_deploys",
                        to="core.router",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-pk"],
                "indexes": [
                    models.Index(
                        fields=["router", "status"], name="studiodeploy_router_status"
                    )
                ],
            },
        ),
    ]
