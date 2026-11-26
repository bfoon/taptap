"""Router twins (a main router with a standby that has everything) and router-to-router migrations."""
from django.contrib.auth.models import User
from django.db import models


class RouterTwin(models.Model):
    """A main router and its standby. TapTap keeps the standby holding the same vouchers, members, profiles and
    IP bindings, so customers carry on there when the main router goes down (core/router_twin.py)."""
    SWITCH = [('ssid', 'Same Wi-Fi name on both'), ('vrrp', 'Shared gateway address (VRRP)'), ('manual', 'I move the cable myself')]
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='router_twins')
    primary = models.OneToOneField('core.Router', on_delete=models.CASCADE, related_name='twin_as_primary')
    standby = models.OneToOneField('core.Router', on_delete=models.CASCADE, related_name='twin_as_standby')
    copy_vouchers = models.BooleanField(default=True)
    copy_members = models.BooleanField(default=True)
    copy_router_users = models.BooleanField(default=True, help_text='Users made on the router itself (Mikhmon, WinBox)')
    copy_profiles = models.BooleanField(default=True)
    copy_bindings = models.BooleanField(default=True)
    switch_over = models.CharField(max_length=10, choices=SWITCH, default='ssid')
    vrrp_interface = models.CharField(max_length=60, blank=True)
    vrrp_address = models.CharField(max_length=45, blank=True)
    vrrp_id = models.PositiveSmallIntegerField(default=0)
    paused = models.BooleanField(default=False)
    failover_since = models.DateTimeField(null=True, blank=True)
    last_mirror_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.primary_id} ⇄ {self.standby_id}'


class RouterTwinItem(models.Model):
    """One thing TapTap put on the standby: what it sent last (hash) and whether the router has it."""
    KINDS = [('user', 'Hotspot user'), ('profile', 'User profile'), ('binding', 'IP binding')]
    STATUS = [('sent', 'On the standby'), ('queued', 'Waiting for the standby'), ('error', 'Not saved yet'), ('conflict', 'Name taken on the standby')]
    twin = models.ForeignKey(RouterTwin, on_delete=models.CASCADE, related_name='items')
    kind = models.CharField(max_length=10, choices=KINDS)
    key = models.CharField(max_length=160)
    state_hash = models.CharField(max_length=40, blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default='queued', db_index=True)
    note = models.CharField(max_length=200, blank=True)
    command_id = models.PositiveIntegerField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['twin', 'kind', 'key'], name='uniq_router_twin_item')]


class RouterTwinEvent(models.Model):
    KINDS = [('paired', 'Twins paired'), ('failover', 'Main router down — standby carrying customers'),
             ('standby_down', 'Standby offline'), ('promoted', 'Standby became the main router'),
             ('paused', 'Mirroring paused'), ('resumed', 'Mirroring resumed'), ('mirror', 'Mirrored now')]
    twin = models.ForeignKey(RouterTwin, on_delete=models.CASCADE, related_name='events')
    kind = models.CharField(max_length=15, choices=KINDS)
    detail = models.CharField(max_length=300, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    activated = models.PositiveIntegerField(default=0, help_text='Vouchers first used on the standby during this event')

    class Meta:
        ordering = ['-started_at']


class RouterMigration(models.Model):
    """Bringing profiles, vouchers, members, router users and IP bindings from one router to another."""
    STATUS = [('running', 'Running'), ('done', 'Done'), ('partly', 'Done, with some left over'), ('failed', 'Failed')]
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='router_migrations')
    source = models.ForeignKey('core.Router', on_delete=models.SET_NULL, null=True, blank=True, related_name='migrations_out')
    target = models.ForeignKey('core.Router', on_delete=models.SET_NULL, null=True, blank=True, related_name='migrations_in')
    source_name = models.CharField(max_length=120, blank=True)
    target_name = models.CharField(max_length=120, blank=True)
    parts = models.JSONField(default=list, blank=True)
    clean_source = models.BooleanField(default=False, help_text='Take moved vouchers and members off the old router')
    status = models.CharField(max_length=10, choices=STATUS, default='running', db_index=True)
    counts = models.JSONField(default=dict, blank=True)
    log = models.JSONField(default=list, blank=True)
    error = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Migration {self.source_name} → {self.target_name} ({self.status})'
