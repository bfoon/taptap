"""Investors: people who fund the business (or one site) and earn from its profit.

Two kinds of terms (``Investment.terms``):

* **profit_share** — the investor earns ``share_pct`` % of the profit of the deal's scope (the whole business,
  or only the chosen sites). While the money is not paid back, ``payback_pct`` % of every earning repays the
  capital and the rest is the investor's profit return. Once the capital is back, every earning is return —
  at ``after_payback_share_pct`` % if the owner set a step-down — until the optional cap is reached.
* **fixed** — the investor is owed capital + ``fixed_return_pct`` % (e.g. 10 000 at 30 % → 13 000). It is paid
  from ``share_pct`` % of the scope's profit until fully paid. Each earning is split in proportion:
  100 / (100 + rate) repays capital, rate / (100 + rate) is return.

Earnings accrue daily (``InvestmentAccrual``, one row per deal per day); today's figure is computed live.
Money actually handed to the investor is an ``InvestorPayout``.
"""
from decimal import Decimal

from django.db import models
from django.utils import timezone

ZERO = Decimal('0')

TERMS = [('profit_share', 'Share of profit'), ('fixed', 'Fixed return')]
SCOPES = [('business', 'Whole business'), ('sites', 'Chosen sites only')]


class InvestorSettings(models.Model):
    business = models.OneToOneField('core.Business', on_delete=models.CASCADE, related_name='investor_settings')
    enabled = models.BooleanField(default=True)
    portal_intro = models.CharField(max_length=300, blank=True, help_text='Welcome line on the investor portal')
    payment_instructions = models.TextField(blank=True, help_text='Bank / mobile-money details shown after an offer is accepted')
    summary_hour = models.PositiveSmallIntegerField(default=8, help_text='Local hour the daily summary is emailed')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'core'


class Investor(models.Model):
    STATUS = [('active', 'Active'), ('paused', 'Portal paused')]
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='investors')
    name = models.CharField(max_length=150)
    email = models.EmailField()
    phone = models.CharField(max_length=60, blank=True)
    company = models.CharField(max_length=150, blank=True)
    notes = models.CharField(max_length=500, blank=True, help_text='Private — never shown to the investor')
    status = models.CharField(max_length=10, choices=STATUS, default='active')
    daily_summary = models.BooleanField(default=True)
    allow_fit = models.BooleanField(default=True, help_text='The investor may ask Fit about their own investments')
    auth_version = models.PositiveIntegerField(default=1)
    last_login_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'core'
        ordering = ['name']
        constraints = [models.UniqueConstraint(fields=['business', 'email'], name='uniq_investor_business_email')]

    def __str__(self):
        return f'{self.name} <{self.email}>'


class InvestmentOpportunity(models.Model):
    STATUS = [('draft', 'Draft'), ('open', 'Open for offers'), ('funded', 'Fully funded'), ('closed', 'Closed')]
    VISIBILITY = [('all', 'All my investors'), ('invited', 'Invited investors only')]
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='investment_opportunities')
    reference = models.CharField(max_length=20, unique=True, db_index=True)
    title = models.CharField(max_length=150)
    summary = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    use_of_funds = models.TextField(blank=True, help_text='What the money buys (one item per line)')
    risks = models.TextField(blank=True)
    scope = models.CharField(max_length=10, choices=SCOPES, default='business')
    routers = models.ManyToManyField('core.Router', blank=True, related_name='investment_opportunities')
    target_amount = models.DecimalField(max_digits=14, decimal_places=2)
    min_ticket = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    max_ticket = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    terms = models.CharField(max_length=15, choices=TERMS, default='profit_share')
    share_pct = models.DecimalField(max_digits=6, decimal_places=3, help_text='% of scope profit offered for the FULL target (pro-rata per ticket)')
    payback_pct = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('70'))
    after_payback_share_pct = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True,
                                                  help_text='% of profit for the FULL target after payback (blank = unchanged)')
    fixed_return_pct = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    cap_pct = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True,
                                  help_text='Profit share stops once total paid reaches this % of the capital (blank = no cap)')
    expected_monthly_profit = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True,
                                                  help_text='Owner estimate for a new site with no history yet')
    visibility = models.CharField(max_length=10, choices=VISIBILITY, default='all')
    invited = models.ManyToManyField(Investor, blank=True, related_name='invited_opportunities')
    status = models.CharField(max_length=10, choices=STATUS, default='draft')
    closes_at = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'core'
        ordering = ['-created_at']

    def __str__(self):
        return self.title


class Investment(models.Model):
    STATUS = [
        ('offered', 'Offer sent — waiting for the business'),
        ('awaiting_payment', 'Accepted — waiting for payment'),
        ('payment_review', 'Payment sent — being confirmed'),
        ('active', 'Active — earning'),
        ('completed', 'Completed'),
        ('rejected', 'Offer declined'),
        ('cancelled', 'Cancelled'),
    ]
    LIVE = ('active',)
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='investments')
    investor = models.ForeignKey(Investor, on_delete=models.CASCADE, related_name='investments')
    opportunity = models.ForeignKey(InvestmentOpportunity, on_delete=models.SET_NULL, null=True, blank=True, related_name='investments')
    reference = models.CharField(max_length=20, unique=True, db_index=True)
    title = models.CharField(max_length=150)
    principal = models.DecimalField(max_digits=14, decimal_places=2)
    scope = models.CharField(max_length=10, choices=SCOPES, default='business')
    routers = models.ManyToManyField('core.Router', blank=True, related_name='investments')
    terms = models.CharField(max_length=15, choices=TERMS, default='profit_share')
    share_pct = models.DecimalField(max_digits=6, decimal_places=3)
    payback_pct = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('70'))
    after_payback_share_pct = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    fixed_return_pct = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    cap_pct = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    carry_losses = models.BooleanField(default=True, help_text='A loss-making day is offset against later profit')
    show_basis = models.BooleanField(default=True, help_text='The investor sees the revenue / costs / profit of the scope')
    status = models.CharField(max_length=20, choices=STATUS, default='offered')
    offer_note = models.CharField(max_length=500, blank=True)
    decision_note = models.CharField(max_length=500, blank=True)
    payment_reference = models.CharField(max_length=120, blank=True)
    payment_method = models.CharField(max_length=60, blank=True)
    funded_at = models.DateTimeField(null=True, blank=True)
    start_date = models.DateField(null=True, blank=True, help_text='Earnings count from this day')
    accrued_through = models.DateField(null=True, blank=True)
    loss_carry = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    earned_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    principal_repaid = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    return_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'core'
        ordering = ['-created_at']
        indexes = [models.Index(fields=['business', 'status'], name='investment_biz_status')]

    def __str__(self):
        return f'{self.reference} · {self.title}'

    @property
    def is_live(self):
        return self.status in self.LIVE

    @property
    def total_due(self):
        """Fixed terms: capital + agreed return. Profit share: the cap (None = open-ended)."""
        if self.terms == 'fixed':
            return (self.principal * (1 + self.fixed_return_pct / 100)).quantize(Decimal('0.01'))
        if self.cap_pct:
            return (self.principal * self.cap_pct / 100).quantize(Decimal('0.01'))
        return None

    @property
    def payback_progress(self):
        return min(100.0, float(self.principal_repaid / self.principal * 100)) if self.principal else 0.0

    @property
    def split_now(self):
        """(capital %, return %) of the next earning — what the investor sees on every page."""
        if self.terms == 'fixed':
            r = self.fixed_return_pct
            cap = (Decimal('100') * 100 / (100 + r)).quantize(Decimal('0.1')) if r >= 0 else Decimal('100')
            return cap, Decimal('100') - cap
        if self.principal_repaid < self.principal:
            return self.payback_pct, Decimal('100') - self.payback_pct
        return ZERO, Decimal('100')


class InvestmentAccrual(models.Model):
    investment = models.ForeignKey(Investment, on_delete=models.CASCADE, related_name='accruals')
    day = models.DateField()
    revenue = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    costs = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    profit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    share_pct = models.DecimalField(max_digits=6, decimal_places=3, default=0)
    loss_offset = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    earned = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    to_principal = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    to_return = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'core'
        ordering = ['day']
        constraints = [models.UniqueConstraint(fields=['investment', 'day'], name='uniq_investment_accrual_day')]


class InvestorPayout(models.Model):
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='investor_payouts')
    investment = models.ForeignKey(Investment, on_delete=models.CASCADE, related_name='payouts')
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    method = models.CharField(max_length=60, blank=True)
    reference = models.CharField(max_length=120, blank=True)
    note = models.CharField(max_length=255, blank=True)
    paid_at = models.DateTimeField(default=timezone.now)
    recorded_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'core'
        ordering = ['-paid_at']


class InvestorMagicLink(models.Model):
    investor = models.ForeignKey(Investor, on_delete=models.CASCADE, related_name='magic_links')
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=60, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'core'
        ordering = ['-created_at']


class InvestorEvent(models.Model):
    """Audit trail, visible to the owner; the investor sees their own deal events."""
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='investor_events')
    investor = models.ForeignKey(Investor, on_delete=models.CASCADE, related_name='events')
    investment = models.ForeignKey(Investment, on_delete=models.CASCADE, null=True, blank=True, related_name='events')
    event = models.CharField(max_length=40)
    text = models.CharField(max_length=300)
    by = models.CharField(max_length=150, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        app_label = 'core'
        ordering = ['-created_at', '-pk']


class InvestorSummaryLog(models.Model):
    investor = models.ForeignKey(Investor, on_delete=models.CASCADE, related_name='summary_logs')
    day = models.DateField()
    status = models.CharField(max_length=10, default='sent')
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'core'
        constraints = [models.UniqueConstraint(fields=['investor', 'day'], name='uniq_investor_summary_day')]


class InvestorFitMessage(models.Model):
    investor = models.ForeignKey(Investor, on_delete=models.CASCADE, related_name='fit_messages')
    role = models.CharField(max_length=10)
    content = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'core'
        ordering = ['created_at', 'pk']
