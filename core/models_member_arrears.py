"""Supplemental payments against MemberRenewal arrears."""
from django.conf import settings
from django.db import models
from django.utils import timezone


class MemberBalancePayment(models.Model):
    """One cash/card/etc. collection against outstanding member renewal arrears.

    A renewal remains the event that adds membership time. This row is only an
    accounting/payment event and NEVER changes a member's expiry date.
    """

    business = models.ForeignKey(
        "core.Business",
        on_delete=models.CASCADE,
        related_name="member_balance_payments",
    )
    member = models.ForeignKey(
        "core.Voucher",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="member_balance_payments",
    )
    sale = models.OneToOneField(
        "core.VoucherSale",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="member_balance_payment",
    )
    amount_collected = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    balance_before = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    balance_after = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    currency = models.CharField(
        max_length=8,
        default="D",
    )
    payment_method = models.CharField(
        max_length=30,
        default="cash",
    )
    reference = models.CharField(
        max_length=120,
        blank=True,
    )
    allocations = models.JSONField(
        default=list,
        blank=True,
        help_text="Renewal receipts and amounts this collection settled.",
    )
    paid_at = models.DateTimeField(
        default=timezone.now,
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        app_label = "core"
        ordering = ["-paid_at", "-pk"]

    @property
    def receipt_number(self):
        stamp = timezone.localtime(self.paid_at).strftime("%Y%m%d")
        return f"MBP-{stamp}-{self.pk:06d}"

    def __str__(self):
        member = self.member.code if self.member_id and self.member else "member"
        return f"{self.receipt_number}: {member} {self.currency}{self.amount_collected}"
