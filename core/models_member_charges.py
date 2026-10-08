"""Charges on a member's account that are not a renewal: late fees, reconnection, equipment… (core/member_arrears.py)."""
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


class MemberCharge(models.Model):
    """Money a member owes that adds no time. Paid like any arrears (MemberBalancePayment allocations: charge_id)."""
    KINDS = [('late_fee', 'Late fee'), ('reconnection', 'Reconnection fee'), ('equipment', 'Equipment'),
             ('installation', 'Installation'), ('other', 'Other charge')]
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='member_charges')
    member = models.ForeignKey('core.Voucher', on_delete=models.SET_NULL, null=True, blank=True, related_name='member_charges')
    member_username = models.CharField(max_length=120, blank=True)
    kind = models.CharField(max_length=20, choices=KINDS, default='late_fee')
    description = models.CharField(max_length=200, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=12, default='GMD')
    charged_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    waived_at = models.DateTimeField(null=True, blank=True)
    waived_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    waive_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['-charged_at', '-id']

    @property
    def receipt_number(self):
        return f'CHG-{timezone.localtime(self.charged_at):%Y%m%d}-{self.pk:06d}'

    @property
    def label(self):
        base = self.get_kind_display()
        return f'{base} — {self.description}' if self.description else base

    def __str__(self):
        return f'{self.receipt_number}: {self.label} {self.amount}'

    @property
    def is_waived(self):
        return self.waived_at is not None
