"""Business alerts: rules on your stock, sales, routers and customers that ring the bell,
play a sound, pop up on the desktop and/or send an email (Alerts page → Business alerts)."""
from django.db import models


class EventRule(models.Model):
    KINDS = [
        ('stock_low', 'Voucher stock is low'),
        ('stock_restocked', 'Voucher stock is back (restocked)'),
        ('no_sales', 'No sale for a while'),
        ('daily_revenue', 'Today’s sales reached an amount'),
        ('router_offline', 'A router is offline'),
        ('online_high', 'Many customers online at once'),
        ('agent_debt', 'An agent owes more than an amount'),
        ('agent_stock_low', 'An agent is running out of vouchers'),
        ('agent_collection_due', 'An agent has not handed in cash for a while'),
        ('agent_collected', 'An agent handed in cash'),
        ('members_expiring', 'Members are about to expire'),
        ('fup_slowed', 'Customers slowed by fair usage'),
    ]
    LEVELS = [('info', 'Info'), ('warning', 'Warning'), ('danger', 'Urgent')]
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='event_rules')
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=30, choices=KINDS)
    params = models.JSONField(default=dict, blank=True, help_text='threshold, plan, router, hours, amount…')
    level = models.CharField(max_length=10, choices=LEVELS, default='warning')
    bell = models.BooleanField(default=True)
    sound = models.BooleanField(default=True)
    desktop = models.BooleanField(default=True)
    email = models.BooleanField(default=False)
    repeat_hours = models.PositiveSmallIntegerField(default=0, help_text='Remind again every N hours while it is still true (0 = once)')
    quiet_from = models.PositiveSmallIntegerField(null=True, blank=True, help_text='No sound / desktop pop-up from this hour…')
    quiet_to = models.PositiveSmallIntegerField(null=True, blank=True, help_text='…until this hour (the bell still counts it)')
    enabled = models.BooleanField(default=True)
    firing = models.BooleanField(default=False, help_text='The condition is true right now')
    last_fired_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_value = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-enabled', 'kind', 'name']

    def __str__(self):
        return self.name

    @property
    def params_json(self):
        import json
        return json.dumps(self.params or {})


class EventAlert(models.Model):
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='event_alerts')
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, null=True, blank=True, related_name='+', help_text='Only this person sees it (tracking); empty = everyone')
    rule = models.ForeignKey(EventRule, on_delete=models.SET_NULL, null=True, blank=True, related_name='alerts')
    kind = models.CharField(max_length=30, blank=True)
    level = models.CharField(max_length=10, default='warning')
    title = models.CharField(max_length=160)
    body = models.CharField(max_length=400, blank=True)
    link = models.CharField(max_length=200, blank=True)
    sound = models.BooleanField(default=True)
    desktop = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
