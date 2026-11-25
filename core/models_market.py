"""TapTap Market — network owners sell batches of vouchers that keep running on their own network (core/market.py).

    MarketWallet   a mobile-money wallet that receives money: the seller's (for a batch) and the buyer's (for every voucher)
    MarketListing  a batch on offer: plan, quantity, price, the router it runs on, how much the due diligence reveals
    MarketDeal     a batch bought (or being bought): reserved → paid → active → completed; it is the buyer's stock
    MarketShop     the buyer's public page where customers buy those vouchers, paying the buyer's wallet directly
    MarketOrder    one customer buying one voucher from a buyer
    RouterOutage   every time a router was unreachable and came back (durable, for the uptime in due diligence)
"""
import secrets
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

WALLET_PROVIDERS = [('wave', 'Wave'), ('qmoney', 'QMoney'), ('afrimoney', 'Afrimoney')]


def _code(prefix, n=8):
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return prefix + ''.join(secrets.choice(alphabet) for _ in range(n))


def listing_code():
    return _code('MKT-', 6)


def deal_reference():
    return _code('MKT-D', 8)


def order_reference():
    return _code('MKT-O', 8)


def payout_reference():
    return _code('MKT-P', 10)


def token():
    return secrets.token_urlsafe(24)


def shop_slug():
    return secrets.token_urlsafe(8).replace('-', '').replace('_', '').lower()[:10]


class MarketWallet(models.Model):
    MODES = [('confirm', 'I confirm each payment in TapTap'), ('api', 'Automatic — my own Wave Business API'),
             ('taptap', "Automatic — through TapTap's Wave, paid out to this number at once")]
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='market_wallets')
    provider = models.CharField(max_length=20, choices=WALLET_PROVIDERS)
    number = models.CharField(max_length=40, help_text='The wallet number customers send money to')
    holder = models.CharField(max_length=120, help_text='Name shown by the wallet when paying, so payers know it is right')
    pay_link = models.URLField(max_length=300, blank=True, help_text='Optional https payment link (e.g. a Wave merchant link)')
    mode = models.CharField(max_length=10, choices=MODES, default='confirm')
    api_key = models.TextField(blank=True, help_text='Encrypted')
    webhook_secret = models.TextField(blank=True, help_text='Encrypted')
    signing_secret = models.TextField(blank=True, help_text='Encrypted (optional)')
    currency = models.CharField(max_length=3, blank=True, help_text='ISO currency of the Wave account, e.g. GMD')
    hook_token = models.CharField(max_length=60, unique=True, default=token, editable=False)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['provider', 'pk']
        constraints = [models.UniqueConstraint(fields=['business', 'provider'], name='uniq_market_wallet_provider')]

    def __str__(self):
        return f'{self.get_provider_display()} {self.masked}'

    @property
    def masked(self):
        n = ''.join(ch for ch in self.number if ch.isdigit() or ch == '+')
        return '•' * (len(n) - 4) + n[-4:] if len(n) > 4 else n

    @property
    def own_api(self):
        return self.mode == 'api' and self.provider == 'wave' and bool(self.api_key and self.webhook_secret and self.currency)

    @property
    def route(self):
        """How a payment to this wallet is confirmed: 'own' (its own Wave key), 'platform' (TapTap's Wave + payout) or 'confirm'."""
        if self.own_api:
            return 'own'
        if self.mode == 'taptap' and self.provider == 'wave':
            from .market_wave import platform_ready
            if platform_ready():
                return 'platform'
        return 'confirm'

    @property
    def automatic(self):
        return self.route in ('own', 'platform')


class MarketListing(models.Model):
    STATUS = [('open', 'On the market'), ('reserved', 'Reserved by a buyer'), ('sold', 'Sold'), ('withdrawn', 'Withdrawn')]
    PRECISION = [('rounded', 'Rounded money figures'), ('exact', 'Exact money figures'), ('index', 'Trends only (no money amounts)')]
    code = models.CharField(max_length=20, unique=True, default=listing_code, editable=False)
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='market_listings')
    # SET_NULL, not PROTECT: deleting a router or a plan elsewhere in TapTap must keep working. A batch whose router is
    # gone cannot make vouchers any more: its paid orders are held for a person to look at (core/market.py issue()).
    router = models.ForeignKey('core.Router', on_delete=models.SET_NULL, null=True, related_name='market_listings',
                               help_text='The router the vouchers run on')
    plan = models.ForeignKey('core.VoucherPlan', on_delete=models.SET_NULL, null=True, related_name='market_listings')
    plan_name = models.CharField(max_length=120)
    duration_minutes = models.PositiveIntegerField(default=1440)
    max_devices = models.PositiveIntegerField(default=1)
    face_price = models.DecimalField(max_digits=10, decimal_places=2, help_text='Price of one voucher on this network')
    min_retail = models.DecimalField(max_digits=10, decimal_places=2, help_text='Lowest price the buyer may sell one at')
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=12, decimal_places=2, help_text='Price of the whole batch')
    title = models.CharField(max_length=120, blank=True)
    area = models.CharField(max_length=80, help_text='Where the network is, e.g. "Brikama, near the market"')
    pitch = models.CharField(max_length=400, blank=True)
    show_name = models.BooleanField(default=False, help_text='Show the business name before a buyer commits')
    precision = models.CharField(max_length=10, choices=PRECISION, default='rounded')
    share_costs = models.BooleanField(default=False, help_text='Show monthly running costs and margin')
    sell_by = models.DateField(null=True, blank=True, help_text='Vouchers must be sold to customers by this date (blank = no limit)')
    status = models.CharField(max_length=12, choices=STATUS, default='open', db_index=True)
    views = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-pk']

    def __str__(self):
        return f'{self.code} {self.quantity}× {self.plan_name}'

    @property
    def face_value(self):
        return self.face_price * self.quantity

    @property
    def unit_cost(self):
        return (self.price / self.quantity).quantize(Decimal('0.01')) if self.quantity else self.price

    @property
    def margin(self):
        return self.face_value - self.price

    @property
    def margin_pct(self):
        return round(float(self.margin / self.face_value * 100), 1) if self.face_value else 0


class MarketDeal(models.Model):
    STATUS = [('awaiting_payment', 'Waiting for the buyer to pay'), ('claimed', 'Buyer says paid — seller to confirm'),
              ('active', 'Active — selling'), ('completed', 'Sold — vouchers in use'), ('handed_over', 'Finished — moved to the buyer'),
              ('cancelled', 'Cancelled by the buyer'), ('rejected', 'Payment not received'), ('expired', 'Not paid in time')]
    OWNED = ('active', 'completed')         # the buyer owns it and the seller may only support it
    OPEN = ('awaiting_payment', 'claimed')
    LIVE = ('active',)
    reference = models.CharField(max_length=24, unique=True, default=deal_reference, editable=False)
    listing = models.ForeignKey(MarketListing, on_delete=models.CASCADE, related_name='deals')
    seller = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='market_sales')
    buyer = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='market_purchases')
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=12, decimal_places=2)
    retail_price = models.DecimalField(max_digits=10, decimal_places=2, help_text="The buyer's price for one voucher")
    issued = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS, default='awaiting_payment', db_index=True)
    paid_with = models.CharField(max_length=20, choices=WALLET_PROVIDERS, blank=True)
    payment_ref = models.CharField(max_length=80, blank=True, help_text='Transaction ID the buyer gave')
    diligence = models.JSONField(default=dict, blank=True, help_text='The due diligence the buyer saw when reserving')
    batch = models.ForeignKey('core.VoucherBatch', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    route = models.CharField(max_length=10, blank=True, help_text='How the batch was paid: own / platform (Wave API) or confirm')
    currency = models.CharField(max_length=8, blank=True)
    provider_session_id = models.CharField(max_length=120, blank=True)
    provider_payload = models.JSONField(default=dict, blank=True)
    handed_over_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    note = models.CharField(max_length=255, blank=True)
    rating = models.PositiveSmallIntegerField(null=True, blank=True)
    review = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)
    claimed_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-pk']

    def __str__(self):
        return f'{self.reference} {self.status}'

    @property
    def remaining(self):
        return max(0, self.quantity - self.issued)

    # what a payment adapter expects on a "purchase" (the batch is paid to the seller)
    @property
    def business(self):
        return self.seller

    @property
    def amount(self):
        return self.price

    @property
    def customer_phone(self):
        return self.buyer.phone or ''


class MarketShop(models.Model):
    business = models.OneToOneField('core.Business', on_delete=models.CASCADE, related_name='market_shop')
    slug = models.CharField(max_length=20, unique=True, default=shop_slug, editable=False)
    heading = models.CharField(max_length=120, default='Buy Wi-Fi vouchers')
    note = models.CharField(max_length=255, blank=True)
    enabled = models.BooleanField(default=True)
    own_share = models.PositiveSmallIntegerField(default=0, help_text='As a seller: % of online sales kept for yourself while batches you sold are selling')
    created_at = models.DateTimeField(default=timezone.now)


class MarketOrder(models.Model):
    STATUS = [('pending', 'Waiting for payment'), ('claimed', 'Customer says paid — to confirm'), ('paid', 'Paid'),
              ('issued', 'Voucher sent'), ('rejected', 'Payment not received'), ('expired', 'Not paid in time'),
              ('failed', 'Payment failed'), ('review', 'Needs a look')]
    HOLDING = ('pending', 'claimed', 'paid')
    reference = models.CharField(max_length=24, unique=True, default=order_reference, editable=False)
    access_token = models.CharField(max_length=60, unique=True, default=token, editable=False)
    deal = models.ForeignKey(MarketDeal, on_delete=models.CASCADE, related_name='orders')
    wallet = models.ForeignKey(MarketWallet, on_delete=models.SET_NULL, null=True, related_name='orders')
    provider = models.CharField(max_length=20, choices=WALLET_PROVIDERS)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=8, blank=True)
    status = models.CharField(max_length=12, choices=STATUS, default='pending', db_index=True)
    customer_name = models.CharField(max_length=120, blank=True)
    customer_phone = models.CharField(max_length=40)
    payment_ref = models.CharField(max_length=80, blank=True, help_text='Transaction ID the customer gave or the provider sent')
    provider_session_id = models.CharField(max_length=120, blank=True)
    provider_payload = models.JSONField(default=dict, blank=True)
    voucher = models.OneToOneField('core.Voucher', on_delete=models.SET_NULL, null=True, blank=True, related_name='market_order')
    CHANNELS = [('shop', "The buyer's shop page"), ('network', "The seller's online shop (customers on the network)")]
    channel = models.CharField(max_length=10, choices=CHANNELS, default='shop')
    route = models.CharField(max_length=10, blank=True, help_text='own / platform (Wave API) or confirm')
    voucher_code = models.CharField(max_length=120, blank=True, help_text='Kept after the batch leaves the seller')
    used_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=255, blank=True)
    ip = models.CharField(max_length=64, blank=True)
    decided_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    issued_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-pk']

    @property
    def business(self):
        """The business paid (the buyer of the batch) — what payment adapters expect on a purchase."""
        return self.deal.buyer


class MarketPayout(models.Model):
    """Money TapTap's Wave received for an owner, sent on to the owner's Wave number (Wave Payout API)."""
    STATUS = [('queued', 'Waiting to send'), ('processing', 'Sending'), ('succeeded', 'Paid out'), ('failed', 'Failed — will retry'),
              ('stuck', 'Needs TapTap support')]
    reference = models.CharField(max_length=40, unique=True, default=payout_reference, editable=False)
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='market_payouts')
    order = models.ForeignKey(MarketOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name='payouts')
    deal = models.ForeignKey(MarketDeal, on_delete=models.SET_NULL, null=True, blank=True, related_name='payouts')
    mobile = models.CharField(max_length=40)
    name = models.CharField(max_length=120, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, help_text='What the owner receives')
    received = models.DecimalField(max_digits=12, decimal_places=2, help_text='What the customer paid')
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=12, choices=STATUS, default='queued', db_index=True)
    idempotency_key = models.CharField(max_length=80, blank=True)
    wave_id = models.CharField(max_length=80, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']


class RouterOutage(models.Model):
    router = models.ForeignKey('core.Router', on_delete=models.CASCADE, related_name='outages')
    started_at = models.DateTimeField(db_index=True)
    ended_at = models.DateTimeField()
    seconds = models.PositiveIntegerField(default=0)
    why = models.CharField(max_length=160, blank=True)

    class Meta:
        ordering = ['-started_at']
        constraints = [models.UniqueConstraint(fields=['router', 'started_at'], name='uniq_router_outage_start')]


class MarketSeller(models.Model):
    """TapTap vets a business before it may list batches on the Market (core/market_vetting.py)."""
    STATUS = [('applied', 'Waiting for TapTap'), ('info', 'TapTap asked for more'), ('approved', 'Vetted seller'),
              ('rejected', 'Not approved'), ('suspended', 'Suspended')]
    LEVELS = [('starter', 'Starter seller'), ('trusted', 'Trusted seller'), ('partner', 'Partner seller')]
    business = models.OneToOneField('core.Business', on_delete=models.CASCADE, related_name='market_seller')
    status = models.CharField(max_length=12, choices=STATUS, default='applied', db_index=True)
    level = models.CharField(max_length=10, choices=LEVELS, default='starter')
    max_open_listings = models.PositiveSmallIntegerField(null=True, blank=True, help_text='Blank = what the level allows')
    max_batch_vouchers = models.PositiveIntegerField(null=True, blank=True, help_text='Blank = what the level allows')
    # what the applicant tells TapTap
    contact_name = models.CharField(max_length=120)
    contact_phone = models.CharField(max_length=40)
    town = models.CharField(max_length=80)
    operating_since = models.CharField(max_length=40, help_text='e.g. "2023" or "8 months"')
    sites = models.PositiveSmallIntegerField(default=1, help_text='How many places the network covers')
    how_sell = models.CharField(max_length=400, help_text='How customers buy today')
    support_plan = models.CharField(max_length=400, help_text="How buyers' customers get help when a voucher fails")
    site_photo = models.TextField(blank=True, help_text='Photo of the installation, as a data: URL')
    document = models.TextField(blank=True, help_text='Business registration or licence (optional), as a data: URL')
    agreed_at = models.DateTimeField(null=True, blank=True, help_text='Accepted the Market seller rules')
    # what TapTap measured and decided
    checks = models.JSONField(default=list, blank=True, help_text='Automatic checks when last submitted')
    score = models.PositiveSmallIntegerField(default=0)
    note = models.CharField(max_length=500, blank=True, help_text='Last message from TapTap to the seller')
    reapply_after = models.DateField(null=True, blank=True)
    watch = models.BooleanField(default=False, help_text='Health checks found something TapTap should look at')
    watch_note = models.CharField(max_length=300, blank=True)
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-submitted_at']

    def __str__(self):
        return f'{self.business} — {self.get_status_display()}'


class MarketVettingEvent(models.Model):
    seller = models.ForeignKey(MarketSeller, on_delete=models.CASCADE, related_name='events')
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    action = models.CharField(max_length=40)
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at', '-pk']
