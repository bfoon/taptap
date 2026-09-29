"""Live chat: team room, direct messages and TapTap support.

Threads
    team     one room per business: the owner and every active team member.
    direct   two people of the same business.
    support  one per business: that business's people <-> the TapTap support team
             (superusers and the users a superuser marks as support agents).
"""
from django.contrib.auth.models import User
from django.db import models


class SupportAgent(models.Model):
    """A user a superuser has put on the TapTap support team (superusers always are)."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='support_agent')
    title = models.CharField(max_length=60, blank=True, default='TapTap Support')
    active = models.BooleanField(default=True)
    added_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self): return self.user.get_full_name() or self.user.email or self.user.username


class ChatThread(models.Model):
    KINDS = [('team', 'Team room'), ('direct', 'Direct message'), ('support', 'TapTap support')]
    kind = models.CharField(max_length=10, choices=KINDS)
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='chat_threads')
    user_a = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name='+')
    user_b = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name='+')
    last_at = models.DateTimeField(null=True, blank=True, db_index=True)
    support_status = models.CharField(max_length=10, blank=True, default='', choices=[('', '—'), ('open', 'Open'), ('solved', 'Solved')])
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['business', 'kind'])]


class ChatMessage(models.Model):
    thread = models.ForeignKey(ChatThread, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='chat_messages')
    body = models.TextField(max_length=4000)
    page_url = models.CharField(max_length=300, blank=True, help_text='"Share this page" link')
    page_title = models.CharField(max_length=120, blank=True)
    mentions = models.JSONField(default=list, blank=True)
    system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['id']


class ChatRead(models.Model):
    """How far a person has read a thread, and how far we already emailed them about it."""
    thread = models.ForeignKey(ChatThread, on_delete=models.CASCADE, related_name='reads')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='+')
    last_read = models.PositiveBigIntegerField(default=0)
    emailed_upto = models.PositiveBigIntegerField(default=0)
    muted = models.BooleanField(default=False)

    class Meta:
        unique_together = [('thread', 'user')]


class ChatPrefs(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='chat_prefs')
    sound = models.BooleanField(default=True)
    popups = models.BooleanField(default=True)
    email_missed = models.BooleanField(default=True, help_text='Email me messages I have not read after a few minutes')
    email_after_minutes = models.PositiveSmallIntegerField(default=10)
    last_email_at = models.DateTimeField(null=True, blank=True)
