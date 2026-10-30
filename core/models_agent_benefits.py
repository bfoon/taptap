"""Agent benefits: refunds for vouchers an agent could not sell, free internet for the agent, and a short history.

Commission settings live on core.Agent itself (commission_enabled / commission_mode / commission_amount) because
every sale reads them (core/finance.py commission_for). See core/agent_benefits.py for the rules.
"""
from django.conf import settings
from django.db import models


class AgentBenefit(models.Model):
    REFUND_ACTIONS = [('disable', 'Switch the vouchers off'), ('return', 'Put them back in the shop’s stock')]
    REFUND_PAYOUTS = [('cash', 'Pay the agent back'), ('credit', 'Credit toward their next vouchers')]

    agent = models.OneToOneField('core.Agent', on_delete=models.CASCADE, related_name='benefit')
    refund_enabled = models.BooleanField(default=False)
    refund_after_days = models.PositiveSmallIntegerField(default=30, help_text='Unsold for at least this many days')
    refund_action = models.CharField(max_length=10, choices=REFUND_ACTIONS, default='disable')
    refund_payout = models.CharField(max_length=10, choices=REFUND_PAYOUTS, default='cash')
    free_internet = models.BooleanField(default=False)
    member = models.ForeignKey('core.Voucher', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                               help_text='The member account created for the agent')
    member_plan = models.ForeignKey('core.MemberPlan', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    history = models.JSONField(default=list, blank=True, help_text='Newest first: [{at, by, text}]')
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        app_label = 'core'


class AgentRefund(models.Model):
    """One refund of unsold vouchers to an agent (the vouchers' codes are kept for the record)."""
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='agent_refunds')
    agent = models.ForeignKey('core.Agent', on_delete=models.CASCADE, related_name='refunds')
    count = models.PositiveIntegerField(default=0)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payout = models.CharField(max_length=10, choices=AgentBenefit.REFUND_PAYOUTS, default='cash')
    action = models.CharField(max_length=10, choices=AgentBenefit.REFUND_ACTIONS, default='disable')
    min_days = models.PositiveSmallIntegerField(default=0)
    codes = models.JSONField(default=list, blank=True)
    collection = models.ForeignKey('core.CashCollection', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                   help_text='The negative hand-in that records cash paid back')
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        app_label = 'core'
        ordering = ['-created_at']

    @property
    def number(self):
        return f'RFD-{self.created_at:%Y%m%d}-{self.pk:05d}'


class AgentBenefitDefaults(models.Model):
    """Business-wide: the member plan picked by default when an agent gets free internet."""
    business = models.OneToOneField('core.Business', on_delete=models.CASCADE, related_name='agent_benefit_defaults')
    member_plan = models.ForeignKey('core.MemberPlan', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        app_label = 'core'
