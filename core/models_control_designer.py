"""Persistent visual Control Center deployments and rollback history."""
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


class RouterVisualDeployment(models.Model):
    STATUS = [
        ("draft", "Draft"),
        ("queued", "Queued"),
        ("success", "Applied"),
        ("failed", "Failed"),
        ("rolled_back", "Rolled back"),
    ]
    SCOPE = [("router", "Router"), ("port", "Port")]

    business = models.ForeignKey(
        "core.Business", on_delete=models.CASCADE, related_name="visual_config_deployments"
    )
    router = models.ForeignKey(
        "core.Router", on_delete=models.CASCADE, related_name="visual_config_deployments"
    )
    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    scope = models.CharField(max_length=12, choices=SCOPE)
    target = models.CharField(max_length=120, blank=True)
    recipe = models.CharField(max_length=80)
    recipe_label = models.CharField(max_length=140)
    risk = models.CharField(max_length=20, default="normal")
    parameters = models.JSONField(default=dict, blank=True)
    plan = models.JSONField(default=list, blank=True)
    before_state = models.JSONField(default=list, blank=True)
    after_state = models.JSONField(default=list, blank=True)
    rollback_plan = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="draft")
    transport = models.CharField(max_length=30, blank=True)
    error = models.TextField(blank=True)
    agent_command_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    applied_at = models.DateTimeField(null=True, blank=True)
    rolled_back_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "core"
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["router", "status"], name="visdeploy_router_status"),
            models.Index(fields=["router", "scope", "target"], name="visdeploy_target"),
        ]

    def __str__(self):
        where = self.target or self.router.name
        return f"{self.recipe_label} → {where}"
