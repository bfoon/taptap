"""Internet sharing (NAT / tethering / hotspot) protection — policy, detections and trusted devices."""
from django.contrib.auth.models import User
from django.db import models

DEFAULT_MESSAGE = ('This voucher is for one device only. Internet sharing, mobile hotspot, tethering or connecting '
                   'another router is not allowed. Please disconnect the shared connection and reconnect this device.')


class SharingPolicy(models.Model):
    ACTIONS = [('monitor', 'Monitor only — just tell me'), ('warn', 'Warn the customer'),
               ('block', 'Block sharing'), ('escalate', 'Warn first, block if it happens again')]
    NOTIFY = [('none', 'Don’t notify me'), ('app', 'In the app'), ('app_email', 'In the app and by email')]
    business = models.OneToOneField('core.Business', on_delete=models.CASCADE, related_name='sharing_policy')
    enabled = models.BooleanField(default=False)
    action = models.CharField(max_length=10, choices=ACTIONS, default='monitor')
    threshold = models.PositiveSmallIntegerField(default=80, help_text='Score (0–100) that triggers the action')
    suspect_at = models.PositiveSmallIntegerField(default=40, help_text='Score from which a device is listed as suspected')
    block_minutes = models.PositiveSmallIntegerField(default=30)
    message = models.TextField(default=DEFAULT_MESSAGE, max_length=600)
    single_device_only = models.BooleanField(default=True, help_text='Only check vouchers allowing one device')
    exempt_plans = models.ManyToManyField('core.VoucherPlan', blank=True, related_name='+')
    notify = models.CharField(max_length=10, choices=NOTIFY, default='app')
    updated_at = models.DateTimeField(auto_now=True)


class SharingCase(models.Model):
    STATUS = [('suspected', 'Suspected'), ('warned', 'Warned'), ('blocked', 'Blocked'), ('trusted', 'Trusted'), ('cleared', 'Cleared')]
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='sharing_cases')
    router = models.ForeignKey('core.Router', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    voucher = models.ForeignKey('core.Voucher', on_delete=models.SET_NULL, null=True, blank=True, related_name='sharing_cases')
    code = models.CharField(max_length=120, blank=True)
    mac = models.CharField(max_length=32, db_index=True)
    ip = models.CharField(max_length=45, blank=True)
    score = models.PositiveSmallIntegerField(default=0)
    peak = models.PositiveSmallIntegerField(default=0)
    reasons = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default='suspected')
    action_taken = models.CharField(max_length=40, blank=True)
    times = models.PositiveIntegerField(default=1, help_text='How many times it was detected')
    blocked_until = models.DateTimeField(null=True, blank=True)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    handled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['-last_seen']


class SharingTrust(models.Model):
    """A device that may share (your own access point, the cashier PC…): never flagged."""
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='sharing_trust')
    mac = models.CharField(max_length=32)
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['business', 'mac'], name='uniq_sharing_trust')]
