"""Partner accounts: self-registered agents (buy before they resell) and resellers (buy on the TapTap Market).

A partner account is a ``Business`` whose ``account_kind`` is ``agent`` or ``reseller`` — so sign-in, trial, the D50
monthly fee (a normal ``Subscription``), email notifications and Market purchases all work as they do for Wi-Fi owners.
See core/partners.py for the rules.

    AgentProgram  a Wi-Fi owner opens their network to self-registered agents (areas, discount, minimum top-up…)
    AgentLink     one agent ↔ one owner: applied, accepted (with the owner's discount), declined, paused or left.
                  Accepting creates a normal ``core.Agent`` in the owner's business: batches, prepaid sales, the
                  voucher checker and statements all reuse what agents already have.
    AgentTopUp    "I want to spend D2,000": the owner accepts or lowers it; once paid (cash, Wave, QMoney, Afrimoney)
                  a batch is created for the agent automatically, bought upfront.
    AgentRetail   the agent's own notebook: which voucher they resold, for how much, to whom.
"""
import secrets
import string

from django.db import models
from django.utils import timezone

KINDS = [('wifi', 'Wi-Fi owner'), ('agent', 'Agent'), ('reseller', 'Reseller')]


def invite_code():
    return ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))


def topup_reference():
    return 'TOP-' + ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(8))


class PartnerProfile(models.Model):
    business = models.OneToOneField('core.Business', on_delete=models.CASCADE, related_name='partner_profile')
    area = models.CharField(max_length=120, blank=True, help_text='Town or area where the partner sells')
    about = models.CharField(max_length=240, blank=True)
    whatsapp = models.CharField(max_length=60, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f'{self.business} · {self.area}'


class AgentProgram(models.Model):
    business = models.OneToOneField('core.Business', on_delete=models.CASCADE, related_name='agent_program')
    open = models.BooleanField(default=False, help_text='Self-registered agents can find this network and apply')
    areas = models.CharField(max_length=240, blank=True, help_text='Towns / areas the Wi-Fi covers, comma separated')
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=20,
                                           help_text='What an agent saves on each voucher, e.g. 20 = pays D2,000 for D2,500 of vouchers')
    min_topup = models.DecimalField(max_digits=12, decimal_places=2, default=500, help_text='Smallest top-up an agent may ask for')
    max_topup = models.DecimalField(max_digits=12, decimal_places=2, default=50000)
    plans = models.ManyToManyField('core.VoucherPlan', blank=True, related_name='agent_programs',
                                   help_text='Plans agents may buy (none ticked = every open plan with a price)')
    router = models.ForeignKey('core.Router', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                               help_text='Router agent batches are sent to (blank = the first router)')
    take_cash = models.BooleanField(default=True, help_text='Agents may pay cash in person')
    auto_approve = models.BooleanField(default=False, help_text='Top-ups up to the limit below are approved without asking')
    auto_approve_limit = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    note = models.CharField(max_length=300, blank=True, help_text='Shown to agents: where to find you, rules…')
    invite_code = models.CharField(max_length=12, unique=True, default=invite_code, editable=False)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Agent programme of {self.business}'


class AgentLink(models.Model):
    STATUS = [('pending', 'Applied'), ('active', 'Accepted'), ('declined', 'Declined'), ('paused', 'Paused'), ('left', 'Left')]
    agent_account = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='agent_links')
    owner = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='agent_network')
    agent = models.OneToOneField('core.Agent', on_delete=models.SET_NULL, null=True, blank=True, related_name='network_link')
    status = models.CharField(max_length=10, choices=STATUS, default='pending', db_index=True)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    message = models.CharField(max_length=300, blank=True, help_text='What the agent wrote when applying')
    owner_note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['agent_account', 'owner'], name='uniq_agent_link')]
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.agent_account} → {self.owner} ({self.status})'


class AgentTopUp(models.Model):
    STATUS = [('requested', 'Waiting for the owner'), ('approved', 'Approved — waiting for payment'),
              ('claimed', 'Paid — owner checking'), ('review', 'Payment needs a look'), ('issued', 'Batch ready'),
              ('declined', 'Declined'), ('cancelled', 'Cancelled'), ('expired', 'Expired')]
    OPEN = ('requested', 'approved', 'claimed', 'review')
    METHODS = [('cash', 'Cash'), ('wave', 'Wave'), ('qmoney', 'QMoney'), ('afrimoney', 'Afrimoney')]
    reference = models.CharField(max_length=20, unique=True, default=topup_reference, editable=False)
    link = models.ForeignKey(AgentLink, on_delete=models.CASCADE, related_name='topups')
    owner = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='agent_topups')
    plan = models.ForeignKey('core.VoucherPlan', on_delete=models.SET_NULL, null=True, related_name='+')
    plan_name = models.CharField(max_length=120)
    face_price = models.DecimalField(max_digits=10, decimal_places=2, help_text='Price of one voucher to customers')
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, help_text='What the agent pays for one voucher')
    requested_amount = models.DecimalField(max_digits=12, decimal_places=2)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text='To pay: quantity × unit cost')
    quantity = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=10, choices=STATUS, default='requested', db_index=True)
    method = models.CharField(max_length=12, blank=True)
    route = models.CharField(max_length=10, blank=True, help_text='Wave: own / platform / confirm')
    payment_ref = models.CharField(max_length=80, blank=True)
    router = models.ForeignKey('core.Router', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    currency = models.CharField(max_length=3, blank=True)
    provider_session_id = models.CharField(max_length=120, blank=True)
    provider_payload = models.JSONField(default=dict, blank=True)
    error = models.CharField(max_length=255, blank=True)
    note = models.CharField(max_length=255, blank=True, help_text="The agent's note, or the owner's reason")
    batch = models.ForeignKey('core.VoucherBatch', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)
    approved_at = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    issued_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.reference} · {self.plan_name} · {self.status}'

    # what Wave checkouts expect of a payment (core/online_buy.WaveProvider.create_checkout)
    @property
    def business(self):
        return self.owner

    @property
    def customer_phone(self):
        return ''

    @property
    def face_value(self):
        return self.face_price * self.quantity

    @property
    def profit(self):
        return self.face_value - self.amount


class AgentRetail(models.Model):
    """The agent's own record of a voucher they resold (their notebook; the owner's books already have the sale)."""
    agent_account = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='agent_retail')
    voucher = models.OneToOneField('core.Voucher', on_delete=models.CASCADE, related_name='agent_retail')
    price = models.DecimalField(max_digits=10, decimal_places=2)
    customer = models.CharField(max_length=120, blank=True)
    sold_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-sold_at']
