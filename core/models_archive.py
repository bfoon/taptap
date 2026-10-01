"""Per-business policy for removing old expired vouchers from MikroTik.

This model intentionally lives in its own module so the large core/models.py file
does not need to be replaced just for this feature.
"""
from django.db import models


class VoucherArchivePolicy(models.Model):
    business = models.OneToOneField(
        'core.Business',
        on_delete=models.CASCADE,
        related_name='voucher_archive_policy',
    )
    enabled = models.BooleanField(
        default=True,
        help_text='Automatically remove old expired vouchers from MikroTik and keep them archived in TapTap.',
    )
    retention_days = models.PositiveSmallIntegerField(
        default=7,
        help_text='How many days an expired voucher stays on MikroTik before TapTap removes it.',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Voucher archive policy'
        verbose_name_plural = 'Voucher archive policies'

    def __str__(self):
        return f'{self.business}: {"on" if self.enabled else "off"} / {self.retention_days} days'


# The database column already accepts any string up to max_length=20.
# Add Archived to the runtime choices without replacing the very large models.py.
try:
    from .models import Voucher
    _field = Voucher._meta.get_field('status')
    _choices = list(_field.choices or [])
    if not any(value == 'archived' for value, _label in _choices):
        _field.choices = _choices + [('archived', 'Archived')]
except Exception:
    # Model import/migration tooling must never fail because of display choices.
    pass
