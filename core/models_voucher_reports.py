"""Vouchers customers sent to staff from the online portal (core/online_portal.py)."""
from django.contrib.auth.models import User
from django.db import models


class VoucherReport(models.Model):
    STATUS = [('open', 'New'), ('resolved', 'Resolved')]
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='voucher_reports')
    voucher = models.ForeignKey('core.Voucher', on_delete=models.SET_NULL, null=True, blank=True, related_name='reports')
    code = models.CharField(max_length=80)
    name = models.CharField(max_length=80, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    message = models.CharField(max_length=500, blank=True)
    shown_error = models.CharField(max_length=300, blank=True, help_text='What the portal told the customer')
    diagnosis = models.JSONField(default=dict, blank=True)
    mac = models.CharField(max_length=20, blank=True)
    ip = models.CharField(max_length=64, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    portal_slug = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default='open', db_index=True)
    staff_note = models.CharField(max_length=500, blank=True)
    resolved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.code} ({self.get_status_display()})'
