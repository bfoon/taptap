"""App/site control and Traffic speed-control models."""
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

    @property
    def services_json(self):
        import json
        return json.dumps(self.services or [])

    @property
    def days_json(self):
        import json
        return json.dumps(self.days or [])

    @property
    def routers_json(self):
        import json
        return json.dumps([r.pk for r in self.routers.all()])

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


class TrafficSpeedRule(models.Model):
    """Persistent base-speed rules managed from Traffic.

    Priority:
        device > agent > selected plan(s) > all plans

    Fair Usage remains a separate, stricter layer.
    """
    SCOPES = [
        ('all_plans', 'All plans'),
        ('plans', 'Selected plan(s)'),
        ('agents', 'Agent(s)'),
        ('devices', 'Device(s)'),
    ]

    business = models.ForeignKey(
        'core.Business',
        on_delete=models.CASCADE,
        related_name='traffic_speed_rules',
    )
    name = models.CharField(max_length=160)
    scope = models.CharField(max_length=20, choices=SCOPES)

    plans = models.ManyToManyField(
        'core.VoucherPlan',
        blank=True,
        related_name='traffic_speed_rules',
    )
    agents = models.ManyToManyField(
        'core.Agent',
        blank=True,
        related_name='traffic_speed_rules',
    )

    devices = models.JSONField(default=list, blank=True)

    down_mbps = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        help_text='Maximum download speed per matching online device.',
    )
    up_mbps = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        help_text='Maximum upload speed per matching online device.',
    )

    enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', '-pk']

    def __str__(self):
        return self.name

    @property
    def speed_text(self):
        return f'{self.down_mbps:g}↓ / {self.up_mbps:g}↑ Mb/s'

    @property
    def target_text(self):
        if self.scope == 'all_plans':
            return 'All plans'
        if self.scope == 'plans':
            names = list(self.plans.values_list('name', flat=True)[:5])
            more = self.plans.count() - len(names)
            return ', '.join(names) + (f' +{more}' if more > 0 else '')
        if self.scope == 'agents':
            names = list(self.agents.values_list('name', flat=True)[:5])
            more = self.agents.count() - len(names)
            return ', '.join(names) + (f' +{more}' if more > 0 else '')
        names = [
            str(x.get('name') or x.get('mac') or x.get('ip') or 'Device')
            for x in (self.devices or [])[:5]
        ]
        more = len(self.devices or []) - len(names)
        return ', '.join(names) + (f' +{more}' if more > 0 else '')
