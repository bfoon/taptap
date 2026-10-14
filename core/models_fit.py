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
