"""What agents do on their voucher checker: every check, every help request and every order."""
from django.contrib.auth.models import User
from django.db import models


class AgentCheck(models.Model):
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='agent_checks')
    agent = models.ForeignKey('core.Agent', on_delete=models.CASCADE, related_name='checks')
    entered = models.CharField(max_length=60, help_text='What the agent typed or scanned (shortened)')
    voucher = models.ForeignKey('core.Voucher', on_delete=models.SET_NULL, null=True, blank=True, related_name='agent_checks')
    result = models.CharField(max_length=12, help_text='unused / in_use / finished / unknown')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']


class AgentHelp(models.Model):
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='agent_help')
    agent = models.ForeignKey('core.Agent', on_delete=models.CASCADE, related_name='help_requests')
    voucher = models.ForeignKey('core.Voucher', on_delete=models.SET_NULL, null=True, blank=True, related_name='agent_help')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    handled_at = models.DateTimeField(null=True, blank=True)
    handled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-created_at']


class AgentOrder(models.Model):
    STATUS = [('new', 'New'), ('done', 'Vouchers made'), ('rejected', 'Declined')]
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='agent_orders')
    agent = models.ForeignKey('core.Agent', on_delete=models.CASCADE, related_name='orders')
    plan = models.ForeignKey('core.VoucherPlan', on_delete=models.SET_NULL, null=True, related_name='agent_orders')
    plan_name = models.CharField(max_length=120)
    quantity = models.PositiveIntegerField()
    note = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default='new')
    batch = models.ForeignKey('core.VoucherBatch', on_delete=models.SET_NULL, null=True, blank=True, related_name='agent_orders')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    done_at = models.DateTimeField(null=True, blank=True)
    done_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['-created_at']
