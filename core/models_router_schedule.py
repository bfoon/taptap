"""Scheduled router restarts and shutdowns (core/router_schedules.py does the work).

A schedule belongs to a router (never to a business directly: a router moved to another business takes its
schedules along and they follow the new owner's team). Each time it is due, one RouterScheduleRun records what
happened: the heads-up, the wait for a quiet moment, the command, and whether the router came back.
"""
from django.contrib.auth.models import User
from django.db import models


class RouterSchedule(models.Model):
    ACTIONS = [('restart', 'Restart'), ('shutdown', 'Shut down (power off)')]
    REPEATS = [('once', 'Once'), ('daily', 'Every day'), ('weekly', 'Every week'), ('monthly', 'Every month')]

    router = models.ForeignKey('core.Router', on_delete=models.CASCADE, related_name='schedules')
    action = models.CharField(max_length=10, choices=ACTIONS, default='restart')
    repeat = models.CharField(max_length=10, choices=REPEATS, default='daily')
    run_on = models.DateField(null=True, blank=True, help_text='Once: the day')
    at_time = models.TimeField(help_text='Local time it is due')
    weekdays = models.JSONField(default=list, blank=True, help_text='Weekly: 0 = Monday … 6 = Sunday')
    month_day = models.PositiveSmallIntegerField(default=1, help_text='Monthly: 1–31 (a shorter month uses its last day)')
    window_minutes = models.PositiveSmallIntegerField(default=30, help_text='Wait up to this long for a quiet moment (0 = exactly on time)')
    warn_minutes = models.PositiveSmallIntegerField(default=10, help_text='Heads-up this long before (0 = none)')
    email = models.BooleanField(default=True)
    note = models.CharField(max_length=160, blank=True)
    active = models.BooleanField(default=True)
    skip_at = models.DateTimeField(null=True, blank=True, help_text='This one occurrence is skipped')
    next_at = models.DateTimeField(null=True, blank=True, db_index=True, help_text='When it is next due (start of its window)')
    last_run_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['next_at', 'id']

    def __str__(self):
        return f'{self.get_action_display()} {self.router} ({self.get_repeat_display()})'


class RouterScheduleRun(models.Model):
    STATUS = [('warned', 'Heads-up sent'), ('waiting', 'Waiting for a quiet moment'), ('sent', 'Command sent'),
              ('back', 'Back online'), ('off', 'Switched off'), ('late', 'Not back yet'), ('failed', 'Failed'),
              ('skipped', 'Skipped'), ('missed', 'Missed')]

    schedule = models.ForeignKey(RouterSchedule, on_delete=models.SET_NULL, null=True, blank=True, related_name='runs')
    router = models.ForeignKey('core.Router', on_delete=models.CASCADE, related_name='schedule_runs')
    action = models.CharField(max_length=10, default='restart')
    planned_at = models.DateTimeField(db_index=True)
    status = models.CharField(max_length=10, choices=STATUS, default='warned')
    online_seen = models.JSONField(default=list, blank=True, help_text='People online at each check while waiting')
    online_at_run = models.IntegerField(null=True, blank=True)
    ran_at = models.DateTimeField(null=True, blank=True)
    back_at = models.DateTimeField(null=True, blank=True)
    down_seconds = models.PositiveIntegerField(null=True, blank=True)
    result = models.CharField(max_length=300, blank=True)
    by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                           help_text='Someone who skipped it or ran it now')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-planned_at', '-id']
        constraints = [models.UniqueConstraint(fields=['schedule', 'planned_at'], name='router_schedule_run_once')]

    def __str__(self):
        return f'{self.router} {self.action} @ {self.planned_at:%Y-%m-%d %H:%M} — {self.status}'
