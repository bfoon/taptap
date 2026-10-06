"""Models for business collaboration and public online voucher purchases."""
from __future__ import annotations

import secrets

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


def collaboration_token():
    return "col_" + secrets.token_urlsafe(18)


def store_slug():
    return secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12].lower()


def purchase_reference():
    return "BUY-" + secrets.token_hex(10).upper()


def purchase_access_token():
    return secrets.token_urlsafe(24)


class BusinessCollaboration(models.Model):
    STATUS = [
        ("pending", "Pending"),
        ("active", "Active"),
        ("rejected", "Rejected"),
        ("ended", "Ended"),
    ]

    source_business = models.ForeignKey(
        "core.Business",
        on_delete=models.CASCADE,
        related_name="collaborations_sent",
    )
    target_business = models.ForeignKey(
        "core.Business",
        on_delete=models.CASCADE,
        related_name="collaborations_received",
    )
    invite_token = models.CharField(
        max_length=80,
        unique=True,
        default=collaboration_token,
        editable=False,
    )
    status = models.CharField(max_length=20, choices=STATUS, default="pending")
    message = models.CharField(max_length=255, blank=True)

    # Permissions the target business owner gets inside the source business.
    source_grants = models.JSONField(default=list, blank=True)
    # Permissions the source business owner gets inside the target business.
    target_grants = models.JSONField(default=list, blank=True)

    source_membership = models.ForeignKey(
        "core.TeamMember",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    target_membership = models.ForeignKey(
        "core.TeamMember",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    invited_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    accepted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(default=timezone.now)
    accepted_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "core"
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["source_business", "status"], name="collab_source_status"),
            models.Index(fields=["target_business", "status"], name="collab_target_status"),
        ]

    def other_business(self, business):
        if business.pk == self.source_business_id:
            return self.target_business
        return self.source_business


class OnlineStorefront(models.Model):
    business = models.OneToOneField(
        "core.Business",
        on_delete=models.CASCADE,
        related_name="online_storefront",
    )
    public_slug = models.CharField(
        max_length=40,
        unique=True,
        default=store_slug,
        editable=False,
    )
    enabled = models.BooleanField(default=False)
    router = models.ForeignKey(
        "core.Router",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="online_storefronts",
        help_text="Router that receives vouchers bought through this online shop.",
    )
    plan_ids = models.JSONField(
        default=list,
        blank=True,
        help_text="Optional VoucherPlan ids to publish. Empty means all active paid plans.",
    )
    heading = models.CharField(max_length=120, default="Buy Wi-Fi online")
    note = models.CharField(max_length=255, blank=True)
    require_phone = models.BooleanField(default=True)
    require_email = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "core"

    def __str__(self):
        return f"{self.business.business_name} online shop"


class OnlineVoucherPurchase(models.Model):
    STATUS = [
        ("pending", "Pending payment"),
        ("paid", "Paid"),
        ("issued", "Voucher issued"),
        ("failed", "Failed"),
        ("review", "Needs review"),
        ("expired", "Expired"),
    ]

    business = models.ForeignKey(
        "core.Business",
        on_delete=models.CASCADE,
        related_name="online_purchases",
    )
    storefront = models.ForeignKey(
        OnlineStorefront,
        on_delete=models.CASCADE,
        related_name="purchases",
    )
    plan = models.ForeignKey(
        "core.VoucherPlan",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    router = models.ForeignKey(
        "core.Router",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    voucher = models.OneToOneField(
        "core.Voucher",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="online_purchase",
    )

    reference = models.CharField(
        max_length=80,
        unique=True,
        db_index=True,
        default=purchase_reference,
        editable=False,
    )
    access_token = models.CharField(
        max_length=120,
        unique=True,
        default=purchase_access_token,
        editable=False,
    )
    provider = models.CharField(max_length=40)
    provider_session_id = models.CharField(max_length=120, blank=True)
    provider_transaction_id = models.CharField(max_length=120, blank=True)

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=12)
    status = models.CharField(max_length=20, choices=STATUS, default="pending")

    customer_name = models.CharField(max_length=120, blank=True)
    customer_phone = models.CharField(max_length=60, blank=True)
    customer_email = models.EmailField(blank=True)

    provider_payload = models.JSONField(default=dict, blank=True)
    error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    issued_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "core"
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["business", "status"], name="online_buy_business_status"),
            models.Index(fields=["provider", "provider_session_id"], name="online_buy_provider_session"),
        ]

    def __str__(self):
        return f"{self.reference} {self.status}"
