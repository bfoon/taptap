"""Network tool runs (ping, traceroute, DNS, website, speed, Internet check) — core/nettools.py."""
from django.contrib.auth.models import User
from django.db import models


class NetTest(models.Model):
    KINDS = [('doctor', 'Internet check'), ('ping', 'Ping'), ('trace', 'Traceroute'), ('dns', 'DNS lookup'),
             ('web', 'Website check'), ('speed', 'Speed test'),
             ('whoami', 'Find my device'), ('hops', 'Path check'), ('mypath', 'My connection')]
    STATUS = [('running', 'Running'), ('waiting', 'Waiting for the router'), ('done', 'Done'), ('failed', 'Failed')]
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='net_tests')
    router = models.ForeignKey('core.Router', on_delete=models.CASCADE, related_name='net_tests')
    kind = models.CharField(max_length=10, choices=KINDS)
    target = models.CharField(max_length=320, blank=True)
    params = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default='running')
    via = models.CharField(max_length=20, blank=True)
    result = models.JSONField(default=dict, blank=True)
    command_id = models.PositiveIntegerField(null=True, blank=True, help_text='TapTap Link command that runs it')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['business', '-created_at'], name='nettest_biz_idx')]

    def __str__(self):
        return f'{self.get_kind_display()} {self.target}'
