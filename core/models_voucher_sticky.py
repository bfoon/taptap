"""Per-voucher exception to TapTap's normal sticky HotSpot login."""
from django.conf import settings
from django.db import models


class VoucherStickyExemption(models.Model):
    voucher = models.OneToOneField(
        "core.Voucher", on_delete=models.CASCADE, related_name="sticky_exemption"
    )
    expires_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text="Empty means exempt until normal sticky is restored manually.",
    )
    reason = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "core"
        ordering = ["-updated_at"]

    def __str__(self):
        until = self.expires_at.isoformat() if self.expires_at else "until restored"
        return f"{self.voucher.code}: no sticky login ({until})"
