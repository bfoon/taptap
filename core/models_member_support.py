"""Scheduled pause / unpause for members (core/member_support.py)."""
from django.contrib.auth.models import User
from django.db import models


class MemberSchedule(models.Model):
    """Pause a member on a date (or when N days are left) and, optionally, unpause on a later date.

    A pause is a freeze (core/voucher_freeze.py): internet stops and the time left stands still until unpaused."""
    ACTIONS = [('pause', 'Pause'), ('resume', 'Unpause')]
    STATUS = [('pending', 'Waiting'), ('running', 'Running'), ('done', 'Done'), ('skipped', 'Skipped'), ('cancelled', 'Cancelled')]
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='member_schedules')
    member = models.ForeignKey('core.Voucher', on_delete=models.CASCADE, related_name='member_schedules')
    action = models.CharField(max_length=10, choices=ACTIONS)
    run_at = models.DateTimeField(db_index=True)
    days_left = models.PositiveSmallIntegerField(null=True, blank=True,
                                                 help_text='Pause when this many days are left (follows renewals); empty = fixed date')
    reason = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default='pending', db_index=True)
    result = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['run_at', 'pk']
        indexes = [models.Index(fields=['status', 'run_at'], name='member_sched_due')]

    def __str__(self):
        return f'{self.get_action_display()} {self.member_id} at {self.run_at}'
