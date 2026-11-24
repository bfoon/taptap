"""Plans: Free, Basic, Pro and Enterprise (core/features.py). Package names from before are renamed: Starter → Basic
(it only gains modules), Business → Pro (everything, as before). Subscriptions remember the plan they bought, and
Enterprise businesses get their tailor-made feature requests."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


LEGACY = {'starter': 'basic', 'business': 'pro'}


def rename_packages(apps, schema_editor):
    Business = apps.get_model('core', 'Business')
    for old, new in LEGACY.items():
        Business.objects.filter(feature_tier=old).update(feature_tier=new)


def back(apps, schema_editor):
    Business = apps.get_model('core', 'Business')
    Business.objects.filter(feature_tier='basic').update(feature_tier='starter')
    Business.objects.filter(feature_tier__in=['pro', 'enterprise']).update(feature_tier='business')
    Business.objects.filter(feature_tier='free').update(feature_tier='starter')


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0130_router_sync_policy"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="subscription",
            name="tier",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Plan bought (basic, pro…); empty = the business keeps its plan",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="business",
            name="feature_tier",
            field=models.CharField(
                default="pro",
                help_text="Plan: free, basic, pro or enterprise (core/features.py TIERS)",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="EnterpriseFeature",
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
                ("title", models.CharField(max_length=140)),
                ("details", models.TextField(blank=True)),
                (
                    "contact",
                    models.CharField(
                        blank=True,
                        help_text="Phone or name to call about it",
                        max_length=60,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("requested", "Requested"),
                            ("planned", "Planned"),
                            ("building", "Being built"),
                            ("live", "Live"),
                            ("declined", "Not possible"),
                        ],
                        default="requested",
                        max_length=12,
                    ),
                ),
                (
                    "note",
                    models.TextField(
                        blank=True, help_text="TapTap’s answer, shown to the owner"
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "business",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="enterprise_features",
                        to="core.business",
                    ),
                ),
                (
                    "requested_by",
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
                "ordering": ["-created_at"],
            },
        ),
        migrations.RunPython(rename_packages, back),
    ]
