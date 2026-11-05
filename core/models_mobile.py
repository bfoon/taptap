"""Phones signed in to the TapTap mobile app (core/mobile_auth.py)."""
from django.contrib.auth.models import User
from django.db import models


class MobileDevice(models.Model):
    """One signed-in phone. The app holds only a random token; TapTap keeps its sha256 and a normal Django session
    for it, so the app goes through exactly the same permission checks as the website — and changing the password,
    removing someone from the team or pressing "Sign out" on the device list ends it."""
    PLATFORMS = [('android', 'Android'), ('ios', 'iPhone'), ('other', 'Other')]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='mobile_devices')
    token_hash = models.CharField(max_length=64, unique=True)
    session_key = models.CharField(max_length=40)
    name = models.CharField(max_length=80, blank=True)
    platform = models.CharField(max_length=10, choices=PLATFORMS, default='other')
    app_version = models.CharField(max_length=20, blank=True)
    push_token = models.CharField(max_length=300, blank=True, db_index=True)
    # What this phone wants to be told (core/mobile_push.py KINDS): {"chat": false, ...}. A missing key means on.
    notify = models.JSONField(default=dict, blank=True)
    # Which Firebase project the app on this phone belongs to (the app says so when it registers its push token).
    # The server's key must come from the same project, or Google refuses every push (SENDER_ID_MISMATCH).
    push_project = models.CharField(max_length=100, blank=True)
    # The last word on pushes to this phone (core/mobile_push.py): {"at", "ok", "code", "reason", "fix", "last_ok",
    # "perm"} — "perm" is the phone's own notification permission as the app last saw it ('allowed', 'denied'…).
    push_health = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_ip = models.CharField(max_length=64, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        app_label = 'core'
        ordering = ['-last_seen_at', '-created_at']

    def __str__(self):
        return f'{self.name or self.get_platform_display()} · {self.user}'

    def wants(self, kind):
        """True unless this phone switched that kind of notification off."""
        return kind is None or (self.notify or {}).get(kind, True) is not False
