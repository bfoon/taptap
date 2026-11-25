"""Adverts on TapTap's public landing page, managed from the platform console (core/landing.py)."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0131_plans_free_basic_pro_enterprise"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="LandingAd",
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
                ("title", models.CharField(max_length=100)),
                ("text", models.CharField(blank=True, max_length=260)),
                (
                    "advertiser",
                    models.CharField(
                        blank=True,
                        help_text="Who pays for it (blank = a TapTap message)",
                        max_length=100,
                    ),
                ),
                (
                    "badge",
                    models.CharField(
                        blank=True,
                        help_text="Small label, e.g. NEW or -20 % (blank = Sponsored / News)",
                        max_length=24,
                    ),
                ),
                ("cta", models.CharField(default="Learn more", max_length=30)),
                ("link", models.URLField(blank=True, max_length=500)),
                (
                    "placement",
                    models.CharField(
                        choices=[
                            ("strip", "Announcement bar — top of the page"),
                            ("hero", "Hero card — next to the headline"),
                            ("spotlight", "Spotlight — the offers carousel"),
                            ("banner", "Wide banner — between sections"),
                        ],
                        db_index=True,
                        default="spotlight",
                        max_length=12,
                    ),
                ),
                (
                    "style",
                    models.CharField(
                        choices=[
                            ("brand", "TapTap blue"),
                            ("night", "Night"),
                            ("sunset", "Sunset"),
                            ("mint", "Mint"),
                            ("gold", "Gold"),
                        ],
                        default="brand",
                        max_length=10,
                    ),
                ),
                (
                    "image",
                    models.TextField(
                        blank=True,
                        help_text="WebP picture as a data: URL (served by /landing/ad/<id>/image/)",
                    ),
                ),
                ("starts_at", models.DateTimeField(blank=True, null=True)),
                ("ends_at", models.DateTimeField(blank=True, null=True)),
                ("active", models.BooleanField(default=True)),
                (
                    "weight",
                    models.PositiveSmallIntegerField(
                        default=1,
                        help_text="Higher = shown more often when several share a place",
                    ),
                ),
                ("views", models.PositiveIntegerField(default=0)),
                ("clicks", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-weight", "-updated_at"],
            },
        ),
    ]
