"""Tracking: a person follows a plan, a batch or a voucher and is told when something happens to it."""
from django.contrib.auth.models import User
from django.db import models


class Watch(models.Model):
    KINDS = [('plan', 'Plan'), ('batch', 'Batch'), ('voucher', 'Voucher')]
    MODES = [('all', 'Everything'), ('some', 'Only what I choose')]
    CHANNELS = [('app', 'In the app'), ('app_email', 'In the app and by email')]
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='watches')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='taptap_watches')
    kind = models.CharField(max_length=10, choices=KINDS)
    object_id = models.PositiveIntegerField()
    label = models.CharField(max_length=160, help_text='Name shown in alerts (kept if the item is renamed or deleted)')
    mode = models.CharField(max_length=6, choices=MODES, default='all')
    events = models.JSONField(default=list, blank=True)
    channel = models.CharField(max_length=10, choices=CHANNELS, default='app')
    note = models.CharField(max_length=200, blank=True, help_text='Why you track it (shown in each alert)')
    active = models.BooleanField(default=True)
    hits = models.PositiveIntegerField(default=0)
    last_hit_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'kind', 'object_id'], name='uniq_watch_user_item')]
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_kind_display()} {self.label}'
