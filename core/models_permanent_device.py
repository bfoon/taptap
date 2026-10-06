"""Persistent staff-approved device ownership for one-device vouchers."""
from django.contrib.auth.models import User
from django.db import models


class PermanentVoucherDevice(models.Model):
    """Marks one existing VoucherDeviceBinding as intentionally permanent.

    The binding itself remains the source of device identity (stable portal
    device token plus current/previous MAC). This row changes the policy:
    unknown devices may never replace that binding until staff remove the
    permanent flag.
    """

    binding = models.OneToOneField(
        "core.VoucherDeviceBinding",
        on_delete=models.CASCADE,
        related_name="permanent_lock",
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    note = models.CharField(max_length=160, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "core"
        ordering = ["-created_at", "-pk"]

    @property
    def voucher(self):
        return self.binding.voucher

    def __str__(self):
        return f"{self.binding.voucher.code}: permanent device {self.binding_id}"
