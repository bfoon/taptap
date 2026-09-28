"""Team accounts, usage analytics and platform audit records."""
from django.contrib.auth.models import User
from django.db import models

from .permissions import STAFF_ROLES, ROLES, role_permissions


class TeamMember(models.Model):
    """A staff login that works inside someone else's business with a limited role."""
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='team')
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='membership')
    role = models.CharField(max_length=30, choices=STAFF_ROLES, default='viewer')
    extra_permissions = models.JSONField(default=list, blank=True, help_text='Extra permission codes on top of the role')
    phone = models.CharField(max_length=60, blank=True)
    is_active = models.BooleanField(default=True)
    must_change_password = models.BooleanField(default=False, help_text='Set when TapTap generated the password')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['user__first_name', 'user__email']

    @property
    def permissions(self):
        return role_permissions(self.role, self.extra_permissions)

    @property
    def role_label(self):
        return ROLES.get(self.role, ('Staff',))[0]

    @property
    def name(self):
        return self.user.get_full_name() or self.user.email

    def __str__(self):
        return f'{self.name} ({self.role_label}) @ {self.business}'


class UsageDaily(models.Model):
    """One row per business, user and day: page views, sign-ins and which areas were used."""
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='usage_days')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='usage_days')
    date = models.DateField(db_index=True)
    page_views = models.PositiveIntegerField(default=0)
    logins = models.PositiveIntegerField(default=0)
    sections = models.JSONField(default=dict, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [('business', 'user', 'date')]
        indexes = [models.Index(fields=['date', 'business'])]


class PlatformAudit(models.Model):
    """Everything the platform owner does to a customer account (support actions, 'view as', payments)."""
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    business = models.ForeignKey('core.Business', on_delete=models.SET_NULL, null=True, blank=True, related_name='platform_audit')
    action = models.CharField(max_length=80)
    details = models.CharField(max_length=500, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
