"""Business-to-business voucher roaming, usage billing and clearing."""
from __future__ import annotations

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from .models import PAYMENT_METHODS


class RoamingAgreement(models.Model):
    STATUS = [
        ("pending", "Terms pending"),
        ("active", "Active"),
        ("paused", "Paused"),
    ]

    collaboration = models.OneToOneField(
        "core.BusinessCollaboration",
        on_delete=models.CASCADE,
        related_name="roaming_agreement",
    )
    status = models.CharField(max_length=20, choices=STATUS, default="pending")
    enabled = models.BooleanField(default=False)

    # If SOURCE hosts TARGET's voucher, TARGET owes SOURCE this rate.
    source_host_rate_per_hour = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )
    # If TARGET hosts SOURCE's voucher, SOURCE owes TARGET this rate.
    target_host_rate_per_hour = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )
    billing_increment_minutes = models.PositiveSmallIntegerField(default=1)
    currency = models.CharField(max_length=12, default="D")

    proposed_by_business = models.ForeignKey(
        "core.Business",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    proposed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    accepted_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    proposed_at = models.DateTimeField(default=timezone.now)
    accepted_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "core"

    @property
    def source_business(self):
        return self.collaboration.source_business

    @property
    def target_business(self):
        return self.collaboration.target_business


class RoamingMirror(models.Model):
    STATUS = [
        ("pending", "Pending"),
        ("queued", "Queued"),
        ("synced", "Synced"),
        ("removing", "Removing"),
        ("error", "Error"),
    ]

    agreement = models.ForeignKey(
        RoamingAgreement, on_delete=models.CASCADE, related_name="mirrors"
    )
    voucher = models.ForeignKey(
        "core.Voucher", on_delete=models.CASCADE, related_name="roaming_mirrors"
    )
    router = models.ForeignKey(
        "core.Router", on_delete=models.CASCADE, related_name="roaming_mirrors"
    )
    issuer_business = models.ForeignKey(
        "core.Business", on_delete=models.CASCADE, related_name="+"
    )
    visited_business = models.ForeignKey(
        "core.Business", on_delete=models.CASCADE, related_name="+"
    )
    status = models.CharField(max_length=20, choices=STATUS, default="pending")
    desired_hash = models.CharField(max_length=64, blank=True)
    last_error = models.CharField(max_length=255, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "core"
        constraints = [
            models.UniqueConstraint(
                fields=["voucher", "router"], name="uniq_roam_voucher_router"
            )
        ]
        indexes = [
            models.Index(fields=["router", "status"], name="roam_mirror_router"),
        ]


class RoamingSession(models.Model):
    STATUS = [("open", "Open"), ("ended", "Ended"), ("void", "Void")]

    agreement = models.ForeignKey(
        RoamingAgreement, on_delete=models.CASCADE, related_name="sessions"
    )
    voucher = models.ForeignKey(
        "core.Voucher", on_delete=models.PROTECT, related_name="roaming_sessions"
    )
    issuer_business = models.ForeignKey(
        "core.Business",
        on_delete=models.PROTECT,
        related_name="roaming_customer_sessions",
    )
    visited_business = models.ForeignKey(
        "core.Business",
        on_delete=models.PROTECT,
        related_name="roaming_hosted_sessions",
    )
    router = models.ForeignKey(
        "core.Router", on_delete=models.PROTECT, related_name="roaming_sessions"
    )

    session_key = models.CharField(max_length=180, unique=True)
    router_session_id = models.CharField(max_length=80, blank=True)
    username = models.CharField(max_length=120)
    mac_address = models.CharField(max_length=32, blank=True)
    ip_address = models.CharField(max_length=64, blank=True)

    started_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    seconds_used = models.PositiveBigIntegerField(default=0)
    billable_minutes = models.PositiveBigIntegerField(default=0)

    rate_per_hour = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS, default="open")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "core"
        ordering = ["-started_at", "-pk"]
        indexes = [
            models.Index(fields=["agreement", "status"], name="roam_session_status"),
            models.Index(fields=["visited_business", "started_at"], name="roam_session_visit"),
        ]


class RoamingPayment(models.Model):
    agreement = models.ForeignKey(
        RoamingAgreement, on_delete=models.CASCADE, related_name="payments"
    )
    payer_business = models.ForeignKey(
        "core.Business", on_delete=models.PROTECT, related_name="+"
    )
    payee_business = models.ForeignKey(
        "core.Business", on_delete=models.PROTECT, related_name="+"
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    payment_method = models.CharField(
        max_length=20, choices=PAYMENT_METHODS, default="cash"
    )
    reference = models.CharField(max_length=120, blank=True)
    note = models.CharField(max_length=255, blank=True)
    paid_at = models.DateTimeField(default=timezone.now)
    recorded_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = "core"
        ordering = ["-paid_at", "-pk"]


class RoamingOffset(models.Model):
    """Mutual amount cancelled from both directions of the same agreement."""
    agreement = models.ForeignKey(
        RoamingAgreement, on_delete=models.CASCADE, related_name="offsets"
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = "core"
        ordering = ["-created_at", "-pk"]
