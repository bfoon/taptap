"""Customers moved to another internet line by hand (core/line_steer.py)."""
from django.contrib.auth.models import User
from django.db import models


class LinePin(models.Model):
    """One device kept on one internet line of a load-balanced router.

    On the router it is one mangle rule whose comment starts with "TapTap line-pin <key>"; this row is TapTap's
    record of who asked for it, for which voucher, and when it was lifted."""
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='line_pins')
    router = models.ForeignKey('core.Router', on_delete=models.CASCADE, related_name='line_pins')
    key = models.CharField(max_length=40, help_text='MAC address, or ip-a-b-c-d when the MAC is not known')
    mac = models.CharField(max_length=32, blank=True)
    ip = models.CharField(max_length=64, blank=True)
    code = models.CharField(max_length=120, blank=True, help_text='Voucher code or member username')
    mark = models.CharField(max_length=60, help_text='Connection / routing mark of the line')
    label = models.CharField(max_length=80, blank=True)
    from_label = models.CharField(max_length=80, blank=True)
    confirmed = models.BooleanField(default=False, help_text='The router answered that the rule is in place')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['router', 'removed_at'], name='linepin_router_idx')]

    def __str__(self):
        return f'{self.code or self.key} → {self.label or self.mark}'


class DeviceLane(models.Model):
    """TapTap itself gives each device its own queue at the plan speed (core/device_lanes.py).

    * scope 'voucher': every device of one voucher / member, until its time ends (or "Stop").
    * scope 'shared': every device of every voucher or member allowed 2+ devices — the always-on guarantee.

    Enforced by core/traffic_speed.py on every live-sync pass: one TTSPD queue per device, above the hotspot's own
    queues, removed when the device leaves. An owner's own speed rule always wins over a lane."""
    SCOPES = [('voucher', 'One voucher'), ('shared', 'Every shared voucher')]
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='device_lanes')
    router = models.ForeignKey('core.Router', on_delete=models.CASCADE, null=True, blank=True, related_name='device_lanes',
                               help_text='Empty = every router of the business')
    scope = models.CharField(max_length=10, choices=SCOPES, default='voucher')
    voucher = models.ForeignKey('core.Voucher', on_delete=models.CASCADE, null=True, blank=True, related_name='device_lanes')
    reason = models.CharField(max_length=160, blank=True)
    until = models.DateTimeField(null=True, blank=True, help_text='Empty = until stopped')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['business', 'removed_at'], name='devicelane_biz_idx')]

    def __str__(self):
        return f'{self.get_scope_display()} {self.voucher_id or ""}'.strip()
