"""Fair usage: slow a voucher down in steps as it uses more data.

A policy is a staircase — e.g. after 2 GB → 5 Mb/s, after 5 GB → 1 Mb/s — counted per day,
per week, or over the whole voucher (plan), for all plans or chosen ones.
"""
from django.contrib.auth.models import User
from django.db import models

PERIODS = [('day', 'Every day (resets at midnight)'), ('week', 'Every week (resets Monday)'),
           ('voucher', 'Whole voucher (the plan’s full time)')]
COUNTS = [('download', 'Download only'), ('total', 'Download + upload')]


class FairUsagePolicy(models.Model):
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='fair_usage_policies')
    name = models.CharField(max_length=80)
    active = models.BooleanField(default=True)
    period = models.CharField(max_length=10, choices=PERIODS, default='day')
    counts = models.CharField(max_length=10, choices=COUNTS, default='total')
    plans = models.ManyToManyField('core.VoucherPlan', blank=True, related_name='fair_usage_policies',
                                   help_text='Empty = every plan without its own policy')
    # [{"gb": 2, "down": 5, "up": 2}, ...] — speeds in Mb/s, sorted by gb when saved
    tiers = models.JSONField(default=list, blank=True)
    free_from = models.PositiveSmallIntegerField(null=True, blank=True, help_text='Hour (0–23): data used from here…')
    free_to = models.PositiveSmallIntegerField(null=True, blank=True, help_text='…until this hour does not count')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def has_free_hours(self):
        return self.free_from is not None and self.free_to is not None and self.free_from != self.free_to

    def is_free_hour(self, hour):
        if not self.has_free_hours:
            return False
        a, b = self.free_from, self.free_to
        return a <= hour < b if a < b else (hour >= a or hour < b)   # wraps midnight, e.g. 23 → 6


class FairUsageState(models.Model):
    """Where one voucher stands under its policy in the current period."""
    voucher = models.ForeignKey('core.Voucher', on_delete=models.CASCADE, related_name='fair_usage')
    policy = models.ForeignKey(FairUsagePolicy, on_delete=models.CASCADE, related_name='states')
    period_key = models.CharField(max_length=20)
    used_bytes = models.BigIntegerField(default=0)
    tier = models.PositiveSmallIntegerField(default=0, help_text='0 = full speed, 1 = first step down, …')
    lifted_until = models.DateTimeField(null=True, blank=True, help_text='Full speed given back by staff until then')
    lifted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    changed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['voucher', 'policy'], name='uniq_fup_state')]
