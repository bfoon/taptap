"""Restore a router's configuration from a backup (core/router_restore.py)."""
from django.contrib.auth.models import User
from django.db import models


class RouterRestore(models.Model):
    SOURCES = [('upload', 'My computer'), ('server', 'TapTap server'), ('router', 'On the router')]
    KINDS = [('backup', 'Binary backup (.backup)'), ('rsc', 'Export (.rsc)')]
    STATUS = [('draft', 'Ready to start'), ('sending', 'Sending to the router'), ('checking', 'Checking the file'),
              ('restoring', 'Restoring'), ('rebooting', 'Rebooting'), ('done', 'Restored'), ('failed', 'Failed')]
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='router_restores')
    router = models.ForeignKey('core.Router', on_delete=models.CASCADE, related_name='restores')
    source = models.CharField(max_length=10, choices=SOURCES)
    kind = models.CharField(max_length=10, choices=KINDS, default='backup')
    file_name = models.CharField(max_length=200, help_text='Name of the file on the router')
    original_name = models.CharField(max_length=200, blank=True)
    size = models.BigIntegerField(default=0)
    backup = models.ForeignKey('core.RouterBackup', on_delete=models.SET_NULL, null=True, blank=True, related_name='restores')
    upload_path = models.CharField(max_length=500, blank=True)
    token_sha = models.CharField(max_length=64, blank=True)
    token_expires = models.DateTimeField(null=True, blank=True)
    safety_copy = models.BooleanField(default=True)
    status = models.CharField(max_length=10, choices=STATUS, default='draft', db_index=True)
    error = models.CharField(max_length=500, blank=True)
    events = models.JSONField(default=list, blank=True)
    via = models.CharField(max_length=20, blank=True)
    command_id = models.PositiveIntegerField(null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    loading_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Restore {self.file_name} on {self.router_id} ({self.status})'
