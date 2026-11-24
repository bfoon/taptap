"""How and when TapTap reads a router and sends it vouchers — the owner's controls (core/sync_control.py does the work).

A router with no saved policy keeps TapTap's defaults (automatic full sync every 30 minutes over TapTap Link, read on
demand over the direct API; vouchers at once; apps & sites samples following the router's load).
"""
from datetime import time

from django.contrib.auth.models import User
from django.db import models


class RouterSyncPolicy(models.Model):
    FULL_MODES = [('auto', 'Automatic'), ('quiet', 'Only in quiet hours'), ('manual', 'Only when I press Sync')]
    PACES = [('normal', 'Normal'), ('gentle', 'Gentle — one table at a time')]
    VOUCHER_MODES = [('instant', 'At once'), ('quiet', 'In quiet hours'), ('manual', 'When I press Send')]
    SAMPLE_MODES = [('auto', 'Follow the router’s load'), ('quiet', 'Only in quiet hours'), ('off', 'Off')]

    router = models.OneToOneField('core.Router', on_delete=models.CASCADE, related_name='sync_policy')
    full_mode = models.CharField(max_length=10, choices=FULL_MODES, default='auto')
    full_every = models.PositiveSmallIntegerField(default=30, help_text='Minutes between automatic full syncs')
    quiet_from = models.TimeField(default=time(1, 0))
    quiet_to = models.TimeField(default=time(5, 0))
    busy_guard = models.BooleanField(default=True, help_text='Automatic syncs wait while the router is busy')
    busy_cpu = models.PositiveSmallIntegerField(default=75)
    pace = models.CharField(max_length=10, choices=PACES, default='normal')
    voucher_mode = models.CharField(max_length=10, choices=VOUCHER_MODES, default='instant',
                                    help_text='New voucher batches: when they go to the router')
    samples_mode = models.CharField(max_length=10, choices=SAMPLE_MODES, default='auto')
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Sync policy of {self.router}'
