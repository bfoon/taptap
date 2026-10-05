from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0078_member_self_service"),
    ]

    operations = [
        migrations.CreateModel(
            name="StickyExclusionRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("match_field", models.CharField(choices=[("model", "Phone / device model"), ("os", "Operating system"), ("device_type", "Device type"), ("browser", "Browser")], default="model", max_length=20)),
                ("value", models.CharField(max_length=120)),
                ("skip_device_lock", models.BooleanField(default=False, help_text="Matching devices do not stay permanently bound to a voucher slot. Use carefully: this deliberately relaxes TapTap's anti-sharing device lock.")),
                ("skip_sticky_sessions", models.BooleanField(default=True, help_text="Matching devices do not keep a HotSpot MAC cookie for automatic re-login.")),
                ("enabled", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("business", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sticky_exclusion_rules", to="core.business")),
            ],
            options={"ordering": ["match_field", "value", "pk"]},
        ),
        migrations.AddConstraint(
            model_name="stickyexclusionrule",
            constraint=models.UniqueConstraint(fields=("business", "match_field", "value"), name="uniq_sticky_exclusion_rule"),
        ),
    ]
