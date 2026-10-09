"""Clean router memory jobs (core/router_cleanup.py): one scan or one clean batch on one router."""
from django.contrib.auth.models import User
from django.db import models


class RouterCleanup(models.Model):
    ACTIONS = [('scan', 'Scan'), ('clean', 'Clean'), ('copy', 'Save to computer')]
    STATUS = [('running', 'Running'), ('waiting', 'Waiting for the router'), ('done', 'Done'), ('failed', 'Failed')]
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='router_cleanups')
    router = models.ForeignKey('core.Router', on_delete=models.CASCADE, related_name='cleanups')
    action = models.CharField(max_length=10, choices=ACTIONS)
    status = models.CharField(max_length=10, choices=STATUS, default='running')
    via = models.CharField(max_length=20, blank=True)
    params = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    command_id = models.PositiveIntegerField(null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_action_display()} {self.router_id} ({self.status})'
