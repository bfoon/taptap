"""Reusable plans, contact preferences, renewal receipts and reminders for members.

Member plans are deliberately separate from voucher plans. A member is still stored
as a core.Voucher with login_type='member' so all existing enforcement, finance,
device-lock, expiry, portal and reporting code keeps working. The assignment row is
the durable link from that member to the reusable MemberPlan.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

from .durations import split, text


class MemberPlan(models.Model):
    business = models.ForeignKey(
        'core.Business',
        on_delete=models.CASCADE,
        related_name='member_plans',
    )
    name = models.CharField(max_length=80)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    duration_minutes = models.PositiveIntegerField(
        default=43200,
        help_text='0 means unlimited.',
    )
    duration_unit = models.CharField(
        max_length=10,
        default='months',
        choices=[
            ('minutes', 'Minutes'),
            ('hours', 'Hours'),
            ('days', 'Days'),
            ('months', 'Months'),
            ('unlimited', 'Unlimited'),
        ],
    )
    max_devices = models.PositiveSmallIntegerField(default=1)
    speed_limit = models.CharField(
        max_length=50,
        blank=True,
        help_text='RouterOS rate-limit, e.g. 5M/5M. Blank means full speed.',
    )
    active = models.BooleanField(
        default=True,
        help_text='Inactive plans stay on existing members but cannot be assigned to new members.',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'core'
        ordering = ['price', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['business', 'name'],
                name='uniq_member_plan_business_name',
            ),
        ]

    @property
    def router_profile_name(self):
        # Stable across renames. Unlimited plans deliberately keep the
        # ``taptap-unlimited-`` prefix because the existing router writers use
        # that prefix to clear session-timeout/on-login limits.
        prefix = (
            'taptap-unlimited-member-plan'
            if not self.duration_minutes
            else 'taptap-member-plan'
        )
        return f'{prefix}-{self.pk}'

    @property
    def duration_text(self):
        return text(self.duration_minutes)

    @property
    def duration_value(self):
        return split(self.duration_minutes, self.duration_unit)[0]

    @property
    def effective_duration_unit(self):
        return split(self.duration_minutes, self.duration_unit)[1]

    def __str__(self):
        return self.name


class MemberPlanAssignment(models.Model):
    member = models.OneToOneField(
        'core.Voucher',
        on_delete=models.CASCADE,
        related_name='member_plan_assignment',
    )
    plan = models.ForeignKey(
        MemberPlan,
        on_delete=models.PROTECT,
        related_name='assignments',
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    assigned_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'core'
        ordering = ['-updated_at']

    def __str__(self):
        return f'{self.member.code} → {self.plan.name}'


class MemberNotificationSettings(models.Model):
    """Member email address and opt-in expiry reminder choices."""

    member = models.OneToOneField(
        'core.Voucher',
        on_delete=models.CASCADE,
        related_name='member_notification_settings',
    )
    email = models.EmailField(blank=True)
    reminders_enabled = models.BooleanField(
        default=False,
        help_text='Send expiry reminders to this member when an email address is present.',
    )
    remind_7_days = models.BooleanField(default=True)
    remind_2_days = models.BooleanField(default=True)
    remind_1_day = models.BooleanField(default=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'core'
        ordering = ['member__code']

    def __str__(self):
        return f'{self.member.code}: {self.email or "no email"}'


class MemberRenewal(models.Model):
    """Permanent receipt/audit row for one member renewal.

    Finance remains the accounting source of truth through VoucherSale. This row
    freezes the member/plan/payment details needed to reproduce a receipt even if
    the plan, email address or username changes later.
    """

    business = models.ForeignKey(
        'core.Business',
        on_delete=models.CASCADE,
        related_name='member_renewals',
    )
    member = models.ForeignKey(
        'core.Voucher',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='member_renewals',
    )
    plan = models.ForeignKey(
        MemberPlan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='renewals',
    )
    sale = models.OneToOneField(
        'core.VoucherSale',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='member_renewal',
    )

    member_username = models.CharField(max_length=120)
    customer_name = models.CharField(max_length=120, blank=True)
    plan_name = models.CharField(max_length=120)
    plan_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    amount_collected = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    currency = models.CharField(max_length=12, default='GMD')
    payment_method = models.CharField(max_length=30, default='cash')
    reference = models.CharField(max_length=120, blank=True)

    duration_minutes = models.PositiveIntegerField(default=0)
    max_devices = models.PositiveSmallIntegerField(default=1)
    speed_limit = models.CharField(max_length=50, blank=True)
    old_expires_at = models.DateTimeField(null=True, blank=True)
    new_expires_at = models.DateTimeField(null=True, blank=True)

    renewed_at = models.DateTimeField(default=timezone.now, db_index=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )

    email_to = models.EmailField(blank=True)
    email_sent_at = models.DateTimeField(null=True, blank=True)
    email_error = models.CharField(max_length=500, blank=True)

    class Meta:
        app_label = 'core'
        ordering = ['-renewed_at', '-id']
        indexes = [
            models.Index(
                fields=['business', 'renewed_at'],
                name='member_renewal_business_at',
            ),
        ]

    @property
    def receipt_number(self):
        when = timezone.localtime(self.renewed_at)
        return f'MRN-{when:%Y%m%d}-{self.pk:06d}'

    @property
    def amount_difference(self):
        return self.amount_collected - self.plan_price

    def __str__(self):
        return f'{self.receipt_number}: {self.member_username}'


class MemberReminderLog(models.Model):
    """One send record per member, expiry period and reminder threshold."""

    STATUS = [
        ('pending', 'Pending'),
        ('sent', 'Sent'),
        ('failed', 'Failed'),
    ]

    member = models.ForeignKey(
        'core.Voucher',
        on_delete=models.CASCADE,
        related_name='member_reminder_logs',
    )
    expiry_at = models.DateTimeField()
    days_before = models.PositiveSmallIntegerField()
    email_to = models.EmailField()
    status = models.CharField(max_length=12, choices=STATUS, default='pending')
    attempts = models.PositiveSmallIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'core'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['member', 'expiry_at', 'days_before'],
                name='uniq_member_reminder_period',
            ),
        ]

    def __str__(self):
        return f'{self.member.code}: {self.days_before}d before {self.expiry_at}'
