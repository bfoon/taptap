"""Fit — the AI technician (core/fit_ai.py).

FitSettings      what the business owner allows Fit to do (one row per business).
FitConversation  one person's chat with Fit inside one business. Never shared across businesses.
FitMessage       the turns of a conversation (kept short: only what the model needs to continue).
FitAction        a change Fit *proposed*. Nothing is changed until a person with the right
                 permission presses "Take action"; the row is the audit trail of who approved what.
"""
from django.contrib.auth.models import User
from django.db import models


class FitSettings(models.Model):
    business = models.OneToOneField('core.Business', on_delete=models.CASCADE, related_name='fit_settings')
    enabled = models.BooleanField(default=True, help_text='Show Fit to the team of this business')
    # Action groups the owner allows (see fit_actions.GROUPS). Empty = Fit can only read and advise.
    allowed = models.JSONField(default=list, blank=True)
    # Customer reports: Fit may close a report itself when the voucher is diagnosed as fine again.
    auto_close_reports = models.BooleanField(default=False)
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    def allows(self, group):
        return group in (self.allowed or [])

    def __str__(self):
        return f'Fit settings for {self.business}'


class FitConversation(models.Model):
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='fit_conversations')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='fit_conversations')
    title = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ['-updated_at']
        indexes = [models.Index(fields=['business', 'user', '-updated_at'], name='fitconv_owner_idx')]


class FitMessage(models.Model):
    ROLES = [('user', 'Person'), ('assistant', 'Fit'), ('tool', 'Tool result')]
    conversation = models.ForeignKey(FitConversation, on_delete=models.CASCADE, related_name='messages')
    role = models.CharField(max_length=10, choices=ROLES)
    content = models.TextField(blank=True)
    # assistant: the tool calls it made (OpenAI format) · tool: {"id", "name"} of the call it answers
    meta = models.JSONField(default=dict, blank=True)
    # What the browser shows next to the text: action cards, links, tests (only for assistant turns).
    cards = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']


class FitAction(models.Model):
    STATUS = [('proposed', 'Waiting for you'), ('done', 'Done'), ('failed', 'Failed'),
              ('dismissed', 'Dismissed'), ('expired', 'Expired')]
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='fit_actions')
    conversation = models.ForeignKey(FitConversation, on_delete=models.SET_NULL, null=True, blank=True, related_name='actions')
    proposed_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    kind = models.CharField(max_length=40)
    params = models.JSONField(default=dict, blank=True)
    title = models.CharField(max_length=200)
    detail = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default='proposed', db_index=True)
    result = models.CharField(max_length=500, blank=True)
    decided_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['business', 'status'], name='fitaction_biz_status_idx')]

    def __str__(self):
        return f'{self.title} ({self.get_status_display()})'


class FitMonitor(models.Model):
    """A standing task: Fit watches something for you and alerts — and, if you chose, acts.

    Event monitors react the moment something happens (a device refused because the slots are full);
    state monitors are checked every live cycle (core/fit_monitors.py). Created from a Fit conversation
    (after the person approved the card) or from the Monitors page.
    """
    KINDS = [
        ('slot_refused', 'A new device is refused because all slots are full'),
        ('slot_filled', 'A voucher or member fills its last device slot'),
        ('idle_slots', 'Free slots held by devices that have not been seen for days'),
        ('voucher_online', 'A voucher or member comes online'),
        ('sharing', 'More devices online on one voucher than it allows'),
        ('heavy_user', 'One customer downloads very fast for a long time'),
        ('online_drop', 'Customers online suddenly drop on a router'),
        ('router_offline', 'A router is offline'),
    ]
    RESPONSES = [
        ('notify', 'Only tell me'),
        ('free_slot', 'Free one idle slot (the new device gets in)'),
        ('disconnect', 'Disconnect the extra device'),
    ]
    AUDIENCES = [('me', 'Only me'), ('team', 'The whole team')]

    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='fit_monitors')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=20, choices=KINDS)
    # plans [names], codes [codes], routers [ids], members_only, threshold, minutes, days, min_idle_minutes, once…
    params = models.JSONField(default=dict, blank=True)
    response = models.CharField(max_length=12, choices=RESPONSES, default='notify')
    audience = models.CharField(max_length=6, choices=AUDIENCES, default='me')
    email = models.BooleanField(default=False, help_text='Also email the business notification recipients')
    notify_member = models.BooleanField(default=False, help_text='Email the member / customer when it is about their account')
    cooldown_minutes = models.PositiveSmallIntegerField(default=30)
    active = models.BooleanField(default=True)
    expires_at = models.DateTimeField(null=True, blank=True, help_text='Stop watching after this time (empty = until paused)')
    source = models.CharField(max_length=10, default='fit')
    hits = models.PositiveIntegerField(default=0)
    firing = models.BooleanField(default=False)
    state = models.JSONField(default=dict, blank=True, help_text='What is true right now (so each thing is told once)')
    last_fired_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-active', '-created_at']
        indexes = [models.Index(fields=['business', 'kind', 'active'], name='fitmon_biz_kind_idx')]

    def __str__(self):
        return self.name


class FitMonitorEvent(models.Model):
    """One thing a monitor saw (and what it did about it) — the monitor's log and Fit's memory of it."""
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='fit_monitor_events')
    monitor = models.ForeignKey(FitMonitor, on_delete=models.SET_NULL, null=True, blank=True, related_name='events')
    kind = models.CharField(max_length=20, blank=True)
    title = models.CharField(max_length=160)
    body = models.CharField(max_length=500, blank=True)
    link = models.CharField(max_length=200, blank=True)
    voucher_code = models.CharField(max_length=64, blank=True)
    mac = models.CharField(max_length=32, blank=True)
    action = models.CharField(max_length=20, blank=True, help_text='admitted, freed_slot, disconnected, nothing_idle…')
    result = models.CharField(max_length=300, blank=True)
    member_emailed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
