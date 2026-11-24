"""Sync control (core/sync_control.py): how and when TapTap reads each router and sends it new vouchers."""

import datetime
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0129_nettest_blinks"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RouterSyncPolicy",
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
                (
                    "full_mode",
                    models.CharField(
                        choices=[
                            ("auto", "Automatic"),
                            ("quiet", "Only in quiet hours"),
                            ("manual", "Only when I press Sync"),
                        ],
                        default="auto",
                        max_length=10,
                    ),
                ),
                (
                    "full_every",
                    models.PositiveSmallIntegerField(
                        default=30, help_text="Minutes between automatic full syncs"
                    ),
                ),
                ("quiet_from", models.TimeField(default=datetime.time(1, 0))),
                ("quiet_to", models.TimeField(default=datetime.time(5, 0))),
                (
                    "busy_guard",
                    models.BooleanField(
                        default=True,
                        help_text="Automatic syncs wait while the router is busy",
                    ),
                ),
                ("busy_cpu", models.PositiveSmallIntegerField(default=75)),
                (
                    "pace",
                    models.CharField(
                        choices=[
                            ("normal", "Normal"),
                            ("gentle", "Gentle — one table at a time"),
                        ],
                        default="normal",
                        max_length=10,
                    ),
                ),
                (
                    "voucher_mode",
                    models.CharField(
                        choices=[
                            ("instant", "At once"),
                            ("quiet", "In quiet hours"),
                            ("manual", "When I press Send"),
                        ],
                        default="instant",
                        help_text="New voucher batches: when they go to the router",
                        max_length=10,
                    ),
                ),
                (
                    "samples_mode",
                    models.CharField(
                        choices=[
                            ("auto", "Follow the router’s load"),
                            ("quiet", "Only in quiet hours"),
                            ("off", "Off"),
                        ],
                        default="auto",
                        max_length=10,
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "router",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sync_policy",
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
    ]
