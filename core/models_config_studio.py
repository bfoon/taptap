"""Config Studio: the board of tiles kept per router, and every deploy made from it (core/config_studio.py)."""
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


class RouterStudioBoard(models.Model):
    """The tiles on a router's board. Saved as you work; nothing here has touched the router."""
    BASES = [('router', 'This router, as TapTap last read it'), ('lab-default', 'Practice router: factory default'),
             ('lab-empty', 'Practice router: empty')]

    router = models.OneToOneField('core.Router', on_delete=models.CASCADE, related_name='studio_board')
    tiles = models.JSONField(default=list, blank=True)
    base = models.CharField(max_length=16, choices=BASES, default='router')
    chat = models.JSONField(default=list, blank=True, help_text='The last few things asked of the AI helper')
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'core'

    def __str__(self):
        return f'Studio board of {self.router}'


class RouterStudioDeploy(models.Model):
    """One board put on a router: what was sent, what was there before, and how to take it back."""
    STATUS = [
        ('draft', 'Being prepared'),
        ('sending', 'On its way to the router'),
        ('applied', 'On the router — waiting for “Keep”'),
        ('kept', 'Kept'),
        ('failed', 'Failed'),
        ('undone', 'Undone'),
    ]

    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='studio_deploys')
    router = models.ForeignKey('core.Router', on_delete=models.CASCADE, related_name='studio_deploys')
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    backup = models.ForeignKey('core.RouterBackup', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    tiles = models.JSONField(default=list, blank=True)
    summary = models.CharField(max_length=255, blank=True)
    plan = models.JSONField(default=list, blank=True)
    rollback_plan = models.JSONField(default=list, blank=True)
    manual_undo = models.JSONField(default=list, blank=True, help_text='Rows the automatic undo cannot put back')
    link_parts = models.JSONField(default=dict, blank=True, help_text='Script pieces for TapTap Link, built by the server')
    link_step = models.CharField(max_length=24, blank=True)
    status = models.CharField(max_length=12, choices=STATUS, default='draft', db_index=True)
    transport = models.CharField(max_length=30, blank=True)
    undo_minutes = models.PositiveSmallIntegerField(default=5)
    undo_until = models.DateTimeField(null=True, blank=True)
    undone_by = models.CharField(max_length=20, blank=True, help_text='user | router (the timer) | failure')
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    applied_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = 'core'
        ordering = ['-created_at', '-pk']
        indexes = [models.Index(fields=['router', 'status'], name='studiodeploy_router_status')]

    def __str__(self):
        return f'Studio deploy #{self.pk} on {self.router}'
