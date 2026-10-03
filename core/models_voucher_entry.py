from django.db import models
from django.utils import timezone


class VoucherEntryPolicy(models.Model):
    business = models.OneToOneField(
        'core.Business',
        on_delete=models.CASCADE,
        related_name='voucher_entry_policy',
    )
    enabled = models.BooleanField(
        default=False,
        help_text='Count wrong voucher/member credentials and temporarily block that device from trying again.',
    )
    max_attempts = models.PositiveSmallIntegerField(
        default=5,
        help_text='Wrong entries allowed inside the attempt window before the device is blocked.',
    )
    warning_remaining = models.PositiveSmallIntegerField(
        default=2,
        help_text='Start showing a strong warning when this many attempts remain.',
    )
    window_minutes = models.PositiveSmallIntegerField(
        default=10,
        help_text='Wrong attempts older than this are forgotten.',
    )
    block_minutes = models.PositiveIntegerField(
        default=30,
        help_text='How long voucher entry stays blocked after the threshold is reached.',
    )
    warning_text = models.TextField(
        blank=True,
        help_text='Optional custom warning shown before the threshold is reached.',
    )
    blocked_text = models.TextField(
        blank=True,
        help_text='Optional custom message shown while the device is blocked.',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'voucher entry security policy'
        verbose_name_plural = 'voucher entry security policies'

    def __str__(self):
        return f'Voucher-entry protection — {self.business}'

    @property
    def safe_max_attempts(self):
        return max(2, min(int(self.max_attempts or 5), 50))

    @property
    def safe_warning_remaining(self):
        return max(1, min(int(self.warning_remaining or 1), self.safe_max_attempts - 1))


class VoucherEntryDevice(models.Model):
    business = models.ForeignKey(
        'core.Business',
        on_delete=models.CASCADE,
        related_name='voucher_entry_devices',
    )
    device_key = models.CharField(max_length=80)
    mac_address = models.CharField(max_length=32, blank=True)
    fingerprint_hash = models.CharField(max_length=64, blank=True)
    ip_address = models.CharField(max_length=64, blank=True)

    attempts = models.PositiveSmallIntegerField(default=0)
    window_started_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)

    blocked_at = models.DateTimeField(null=True, blank=True)
    blocked_until = models.DateTimeField(null=True, blank=True)
    last_unblocked_at = models.DateTimeField(null=True, blank=True)
    last_unblocked_by = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )

    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['business', 'device_key'],
                name='uniq_voucher_entry_device_business_key',
            ),
        ]
        indexes = [
            models.Index(fields=['business', 'blocked_until']),
            models.Index(fields=['business', 'mac_address']),
        ]
        ordering = ['-blocked_until', '-last_attempt_at', '-pk']

    def __str__(self):
        return self.mac_address or self.ip_address or self.device_key

    @property
    def is_blocked(self):
        return bool(self.blocked_until and self.blocked_until > timezone.now())
