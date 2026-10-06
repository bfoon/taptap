from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0083_business_roaming"),
    ]

    operations = [
        migrations.CreateModel(
            name="RouterVisualDeployment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("scope", models.CharField(choices=[("router","Router"),("port","Port")], max_length=12)),
                ("target", models.CharField(blank=True, max_length=120)),
                ("recipe", models.CharField(max_length=80)),
                ("recipe_label", models.CharField(max_length=140)),
                ("risk", models.CharField(default="normal", max_length=20)),
                ("parameters", models.JSONField(blank=True, default=dict)),
                ("plan", models.JSONField(blank=True, default=list)),
                ("before_state", models.JSONField(blank=True, default=list)),
                ("after_state", models.JSONField(blank=True, default=list)),
                ("rollback_plan", models.JSONField(blank=True, default=list)),
                ("status", models.CharField(choices=[("draft","Draft"),("queued","Queued"),("success","Applied"),("failed","Failed"),("rolled_back","Rolled back")], default="draft", max_length=20)),
                ("transport", models.CharField(blank=True, max_length=30)),
                ("error", models.TextField(blank=True)),
                ("agent_command_id", models.BigIntegerField(blank=True, db_index=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                ("rolled_back_at", models.DateTimeField(blank=True, null=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="auth.user")),
                ("business", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="visual_config_deployments", to="core.business")),
                ("router", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="visual_config_deployments", to="core.router")),
            ],
            options={"ordering":["-created_at","-pk"]},
        ),
        migrations.AddIndex(
            model_name="routervisualdeployment",
            index=models.Index(fields=["router","status"], name="visdeploy_router_status"),
        ),
        migrations.AddIndex(
            model_name="routervisualdeployment",
            index=models.Index(fields=["router","scope","target"], name="visdeploy_target"),
        ),
    ]
