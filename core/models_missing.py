from django.conf import settings
from django.db import models
from django.utils import timezone


class MissingVoucherReport(models.Model):
    REPORT_TYPES = [
        ('single', 'Single voucher'),
        ('group', 'Group of vouchers'),
        ('batch', 'Entire batch'),
    ]
    ACTION_STATES = [
        ('planned', 'Action planned'),
        ('done', 'Action already taken'),
    ]
    STATUSES = [
        ('open', 'Open'),
        ('resolved', 'Resolved'),
    ]

    business = models.ForeignKey(
        'core.Business',
        on_delete=models.CASCADE,
        related_name='missing_voucher_reports',
    )
    batch = models.ForeignKey(
        'core.VoucherBatch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='missing_reports',
    )
    report_type = models.CharField(
        max_length=20,
        choices=REPORT_TYPES,
        default='single',
    )
    voucher_codes = models.JSONField(default=list, blank=True)
    voucher_count = models.PositiveIntegerField(default=0)

    details = models.TextField(
        blank=True,
        help_text='What is missing / what happened.',
    )
    action_state = models.CharField(
        max_length=20,
        choices=ACTION_STATES,
        default='planned',
    )
    action_notes = models.TextField(
        blank=True,
        help_text='What was done or what will be done.',
    )
    reference = models.CharField(
        max_length=160,
        blank=True,
        help_text='Optional reference, incident number, police report number, etc.',
    )

    status = models.CharField(
        max_length=20,
        choices=STATUSES,
        default='open',
        db_index=True,
    )
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='missing_voucher_reports_created',
    )
    reported_at = models.DateTimeField(default=timezone.now)

    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='missing_voucher_reports_resolved',
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-reported_at']
        indexes = [
            models.Index(
                fields=['business', 'status', '-reported_at'],
                name='missing_voucher_status_idx',
            ),
        ]

    def __str__(self):
        label = self.batch.name if self.batch else f'{self.voucher_count} voucher(s)'
        return f'{label} — {self.get_status_display()}'
