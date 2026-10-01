"""App & site control: block, slow down or always allow services (YouTube, TikTok…) on your
routers — always, at set times, in peak / off-peak hours, for a while or permanently."""
from django.contrib.auth.models import User
from django.db import models


class AppRule(models.Model):
    ACTIONS = [('block', 'Block'), ('slow', 'Slow down'), ('allow', 'Always allow')]
    WHEN = [('always', 'All day'), ('window', 'Between set hours'), ('peak', 'Peak hours'), ('offpeak', 'Off-peak hours')]
    SCOPES = [('customers', 'Logged-in hotspot customers'), ('everyone', 'Everyone on the router')]
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='app_rules')
    name = models.CharField(max_length=120)
    services = models.JSONField(default=list, help_text='Service keys from core/app_catalog.py')
    custom_domains = models.JSONField(default=list, blank=True, help_text='Extra sites, e.g. ["example.com"]')
    action = models.CharField(max_length=10, choices=ACTIONS, default='block')
    down_mbps = models.DecimalField(max_digits=7, decimal_places=2, default=1, help_text='Slow down: download speed per device')
    up_mbps = models.DecimalField(max_digits=7, decimal_places=2, default=1, help_text='Slow down: upload speed per device')
    when = models.CharField(max_length=10, choices=WHEN, default='always')
    from_time = models.TimeField(null=True, blank=True)
    to_time = models.TimeField(null=True, blank=True)
    days = models.JSONField(default=list, blank=True, help_text='["mon","tue",…]; empty = every day')
    starts_at = models.DateTimeField(null=True, blank=True, help_text='Temporary rules: starts')
    ends_at = models.DateTimeField(null=True, blank=True, help_text='Temporary rules: ends (empty = permanent)')
    scope = models.CharField(max_length=10, choices=SCOPES, default='customers')
    block_quic = models.BooleanField(default=True, help_text='Also stop QUIC (UDP 443) for these services so the rule catches all their traffic')
    routers = models.ManyToManyField('core.Router', blank=True, related_name='app_rules', help_text='Empty = every router')
    enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['action', 'name']

    def __str__(self):
        return self.name

    # for the edit form
    @property
    def services_json(self):
        import json; return json.dumps(self.services or [])

    @property
    def days_json(self):
        import json; return json.dumps(self.days or [])

    @property
    def routers_json(self):
        import json; return json.dumps([r.pk for r in self.routers.all()])

    @property
    def custom_text(self):
        return ' '.join(self.custom_domains or [])


class AppControlState(models.Model):
    """What TapTap last sent to one router (so it only resends when something changed)."""
    router = models.OneToOneField('core.Router', on_delete=models.CASCADE, related_name='app_control')
    digest = models.CharField(max_length=64, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, blank=True)
    message = models.CharField(max_length=255, blank=True)
