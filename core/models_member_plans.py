"""Reusable plans specifically for username/password members.

Member plans are deliberately separate from voucher plans. A member is still stored
as a core.Voucher with login_type='member' so all existing enforcement, finance,
device-lock, expiry, portal and reporting code keeps working. The assignment row is
the durable link from that member to the reusable MemberPlan.
"""
from django.conf import settings
from django.db import models

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
