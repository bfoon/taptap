from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


class Business(models.Model):
    SUBS=[('trial','Trial'),('active','Active'),('expired','Expired'),('pending','Pending')]
    user=models.OneToOneField(User,on_delete=models.CASCADE,related_name='business')
    business_name=models.CharField(max_length=180)
    owner_name=models.CharField(max_length=180)
    phone=models.CharField(max_length=60)
    subscription_status=models.CharField(max_length=20,choices=SUBS,default='trial')
    trial_ends_at=models.DateTimeField()
    subscription_expires_at=models.DateTimeField(null=True,blank=True)
    is_unlimited=models.BooleanField(default=False)
    created_at=models.DateTimeField(auto_now_add=True)
    # Branding + hotspot facts used by the Portal Studio and the Voucher Design Studio.
    wifi_ssid=models.CharField(max_length=80,blank=True)
    hotspot_url=models.CharField(max_length=200,blank=True,help_text='e.g. http://wifi.local/login')
    support_phone=models.CharField(max_length=60,blank=True)
    brand_color=models.CharField(max_length=20,default='#1769e0')
    logo_data=models.TextField(blank=True,help_text='Small logo as a data: URL')
    currency=models.CharField(max_length=8,default='D')
    # Finance
    monthly_revenue_target=models.DecimalField(max_digits=12,decimal_places=2,default=0)
    auto_record_sales=models.BooleanField(default=True,help_text='Record a sale automatically when an unsold voucher is first used on the router')
    # Live sync & enforcement
    live_sync=models.BooleanField(default=True,help_text='Check routers every few seconds for voucher, session and binding changes')
    auto_enforce=models.BooleanField(default=True,help_text='Automatically disconnect sessions whose voucher has expired or been disabled')
    enforce_grace_minutes=models.PositiveSmallIntegerField(default=5,help_text='Wait this long before fixing automatically')
    def access_expires_at(self): return self.subscription_expires_at if self.subscription_status=='active' else self.trial_ends_at
    @property
    def has_access(self):
        if self.is_unlimited:return True
        exp=self.access_expires_at(); return bool(exp and exp>timezone.now())
    @property
    def days_left(self):
        if self.is_unlimited:return 9999
        exp=self.access_expires_at(); return max(0,(exp-timezone.now()).days+1) if exp else 0
    def __str__(self): return self.business_name


class Subscription(models.Model):
    business=models.ForeignKey(Business,on_delete=models.CASCADE,related_name='subscriptions')
    plan=models.CharField(max_length=60); amount=models.DecimalField(max_digits=10,decimal_places=2)
    payment_method=models.CharField(max_length=60,default='Manual'); payment_status=models.CharField(max_length=30,default='Pending')
    transaction_id=models.CharField(max_length=120,blank=True); starts_at=models.DateTimeField(null=True,blank=True); expires_at=models.DateTimeField(null=True,blank=True); created_at=models.DateTimeField(auto_now_add=True)


class VoucherPlan(models.Model):
    SOURCE=[('taptap','TapTap'),('mikrotik','MikroTik')]
    business=models.ForeignKey(Business,on_delete=models.CASCADE,related_name='plans',null=True,blank=True)
    name=models.CharField(max_length=80)
    price=models.DecimalField(max_digits=10,decimal_places=2,default=0)
    duration_hours=models.PositiveIntegerField(default=24)
    max_devices=models.PositiveIntegerField(default=1)
    speed_limit=models.CharField(max_length=50,blank=True)
    data_limit_mb=models.PositiveIntegerField(null=True,blank=True)
    active=models.BooleanField(default=True)
    source=models.CharField(max_length=20,choices=SOURCE,default='taptap')
    imported_from_router=models.ForeignKey('Router',on_delete=models.SET_NULL,null=True,blank=True,related_name='imported_plans')
    mikrotik_profile_name=models.CharField(max_length=120,blank=True)
    # Where the price came from: '' (none yet), 'router' (Mikhmon script / comment) or 'manual' (typed in TapTap — never overwritten by sync).
    price_source=models.CharField(max_length=20,blank=True,default='')
    class Meta: unique_together=('business','name')
    def __str__(self): return self.name


class Router(models.Model):
    business=models.ForeignKey(Business,on_delete=models.CASCADE,related_name='routers')
    name=models.CharField(max_length=120)
    ip_address=models.CharField(max_length=120)
    username=models.CharField(max_length=120)
    password=models.CharField(max_length=255)
    api_port=models.PositiveIntegerField(default=8728)
    use_ssl=models.BooleanField(default=False)
    status=models.CharField(max_length=40,default='Not connected')
    last_error=models.TextField(blank=True)
    last_tested_at=models.DateTimeField(null=True,blank=True)
    # When TapTap first finished reading this router. Vouchers first seen already-used after this
    # moment were sold while TapTap was watching, so their sales are booked; older history is not.
    sales_baseline_at=models.DateTimeField(null=True,blank=True)
    last_watch_at=models.DateTimeField(null=True,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
    def __str__(self): return self.name


class VoucherBatch(models.Model):
    SETTLEMENT=[('credit','On credit — agent pays as vouchers sell'),('prepaid','Paid upfront — agent bought the batch')]
    business=models.ForeignKey(Business,on_delete=models.CASCADE,related_name='batches')
    name=models.CharField(max_length=120)
    plan=models.ForeignKey(VoucherPlan,on_delete=models.SET_NULL,null=True)
    quantity=models.PositiveIntegerField(default=1)
    # Owner of the batch: blank = the shop's own stock, otherwise the agent holding these vouchers.
    agent=models.ForeignKey('Agent',on_delete=models.SET_NULL,null=True,blank=True,related_name='batches')
    settlement=models.CharField(max_length=20,choices=SETTLEMENT,default='credit')
    issued_at=models.DateTimeField(null=True,blank=True)
    note=models.CharField(max_length=255,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)


class Voucher(models.Model):
    STATUS=[('active','Active'),('disabled','Disabled'),('expired','Expired')]
    SOURCE=[('taptap','TapTap'),('mikrotik','MikroTik')]
    business=models.ForeignKey(Business,on_delete=models.CASCADE,related_name='vouchers')
    batch=models.ForeignKey(VoucherBatch,on_delete=models.SET_NULL,null=True,blank=True,related_name='vouchers')
    router=models.ForeignKey(Router,on_delete=models.SET_NULL,null=True,blank=True,related_name='vouchers')
    code=models.CharField(max_length=120,unique=True)
    plan_name=models.CharField(max_length=120)
    price=models.DecimalField(max_digits=10,decimal_places=2,default=0)
    duration_hours=models.PositiveIntegerField(default=24)
    max_devices=models.PositiveIntegerField(default=1)
    status=models.CharField(max_length=20,choices=STATUS,default='active')
    source=models.CharField(max_length=20,choices=SOURCE,default='taptap')
    mikrotik_id=models.CharField(max_length=120,blank=True)
    expires_at=models.DateTimeField(null=True,blank=True)
    used_at=models.DateTimeField(null=True,blank=True)
    sold_at=models.DateTimeField(null=True,blank=True)
    # Who holds this voucher (inherited from its batch; can be reassigned). Sales of it are credited to this agent.
    agent=models.ForeignKey('Agent',on_delete=models.SET_NULL,null=True,blank=True,related_name='vouchers')
    # Standalone vouchers made for one person
    customer_name=models.CharField(max_length=120,blank=True)
    customer_phone=models.CharField(max_length=60,blank=True)
    note=models.CharField(max_length=255,blank=True)
    rate_limit=models.CharField(max_length=50,blank=True,help_text='Speed for a custom voucher with no plan, e.g. 5M/5M')
    mikrotik_sync_status=models.CharField(max_length=30,default='Pending')
    mikrotik_sync_error=models.TextField(blank=True)
    created_at=models.DateTimeField(auto_now_add=True)


class VoucherDeviceBinding(models.Model):
    business=models.ForeignKey(Business,on_delete=models.CASCADE)
    voucher=models.ForeignKey(Voucher,on_delete=models.CASCADE,related_name='device_bindings')
    slot_no=models.PositiveIntegerField(default=1)
    device_token_hash=models.CharField(max_length=128)
    current_mac=models.CharField(max_length=32)
    previous_mac=models.CharField(max_length=32,blank=True)
    first_bound_at=models.DateTimeField(auto_now_add=True)
    last_seen_at=models.DateTimeField(auto_now=True)
    class Meta: unique_together=('voucher','slot_no')


class IPBindingAccessExpiry(models.Model):
    business=models.ForeignKey(Business,on_delete=models.CASCADE)
    router=models.ForeignKey(Router,on_delete=models.CASCADE)
    binding_id=models.CharField(max_length=120)
    mac_address=models.CharField(max_length=32,blank=True)
    enabled_at=models.DateTimeField(auto_now_add=True)
    expires_at=models.DateTimeField()
    updated_at=models.DateTimeField(auto_now=True)
    class Meta: unique_together=('business','router','binding_id')


class Activity(models.Model):
    business=models.ForeignKey(Business,on_delete=models.CASCADE,related_name='activities')
    type=models.CharField(max_length=80)
    details=models.CharField(max_length=255)
    status=models.CharField(max_length=40,default='Success')
    created_at=models.DateTimeField(auto_now_add=True)


class RouterHotspotProfile(models.Model):
    """A direct mirror of /ip/hotspot/user/profile."""
    business=models.ForeignKey(Business,on_delete=models.CASCADE,related_name='router_hotspot_profiles')
    router=models.ForeignKey(Router,on_delete=models.CASCADE,related_name='hotspot_profiles')
    name=models.CharField(max_length=120)
    mikrotik_id=models.CharField(max_length=120,blank=True)
    rate_limit=models.CharField(max_length=120,blank=True)
    shared_users=models.PositiveIntegerField(default=1)
    session_timeout=models.CharField(max_length=80,blank=True)
    idle_timeout=models.CharField(max_length=80,blank=True)
    keepalive_timeout=models.CharField(max_length=80,blank=True)
    address_pool=models.CharField(max_length=120,blank=True)
    is_present=models.BooleanField(default=True)
    raw_data=models.JSONField(default=dict,blank=True)
    last_seen_at=models.DateTimeField(default=timezone.now)
    updated_at=models.DateTimeField(auto_now=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['router','name'],name='uniq_router_hotspot_profile')]
    def __str__(self): return f'{self.router.name}: {self.name}'


class RouterHotspotUser(models.Model):
    SOURCE=[('mikrotik','MikroTik'),('taptap','TapTap')]
    business=models.ForeignKey(Business,on_delete=models.CASCADE,related_name='router_hotspot_users')
    router=models.ForeignKey(Router,on_delete=models.CASCADE,related_name='hotspot_users')
    username=models.CharField(max_length=120)
    mikrotik_id=models.CharField(max_length=120,blank=True)
    profile=models.CharField(max_length=120,blank=True)
    mac_address=models.CharField(max_length=32,blank=True)
    comment=models.CharField(max_length=255,blank=True)
    limit_uptime=models.CharField(max_length=80,blank=True)
    uptime=models.CharField(max_length=80,blank=True)
    disabled=models.BooleanField(default=False)
    source=models.CharField(max_length=20,choices=SOURCE,default='mikrotik')
    is_present=models.BooleanField(default=True)
    raw_data=models.JSONField(default=dict,blank=True)
    last_seen_at=models.DateTimeField(default=timezone.now)
    updated_at=models.DateTimeField(auto_now=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['router','username'],name='uniq_router_hotspot_username')]
    def __str__(self): return f'{self.router.name}: {self.username}'


class SyncedIPBinding(models.Model):
    SOURCE=[('mikrotik','MikroTik'),('taptap','TapTap')]
    business=models.ForeignKey(Business,on_delete=models.CASCADE,related_name='synced_ip_bindings')
    router=models.ForeignKey(Router,on_delete=models.CASCADE,related_name='synced_ip_bindings')
    mikrotik_id=models.CharField(max_length=120,blank=True)
    mac_address=models.CharField(max_length=32,blank=True)
    address=models.CharField(max_length=120,blank=True)
    server=models.CharField(max_length=120,blank=True)
    binding_type=models.CharField(max_length=40,default='bypassed')
    comment=models.CharField(max_length=255,blank=True)
    disabled=models.BooleanField(default=False)
    source=models.CharField(max_length=20,choices=SOURCE,default='mikrotik')
    sync_status=models.CharField(max_length=30,default='Synced')
    sync_error=models.TextField(blank=True)
    is_present=models.BooleanField(default=True)
    raw_data=models.JSONField(default=dict,blank=True)
    last_seen_at=models.DateTimeField(default=timezone.now)
    updated_at=models.DateTimeField(auto_now=True)
    def __str__(self): return f'{self.router.name}: {self.mac_address or self.address or self.mikrotik_id}'


class RouterInterface(models.Model):
    router=models.ForeignKey(Router,on_delete=models.CASCADE,related_name='interfaces')
    name=models.CharField(max_length=120)
    default_name=models.CharField(max_length=120,blank=True)
    interface_type=models.CharField(max_length=80,blank=True)
    mac_address=models.CharField(max_length=32,blank=True)
    comment=models.CharField(max_length=255,blank=True)
    running=models.BooleanField(default=False)
    disabled=models.BooleanField(default=False)
    mtu=models.CharField(max_length=40,blank=True)
    rx_byte=models.BigIntegerField(default=0)
    tx_byte=models.BigIntegerField(default=0)
    is_present=models.BooleanField(default=True)
    raw_data=models.JSONField(default=dict,blank=True)
    last_seen_at=models.DateTimeField(default=timezone.now)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['router','name'],name='uniq_router_interface_name')]
    def __str__(self): return f'{self.router.name}: {self.name}'


class RouterNeighbor(models.Model):
    router=models.ForeignKey(Router,on_delete=models.CASCADE,related_name='neighbors')
    neighbor_key=models.CharField(max_length=255)
    identity=models.CharField(max_length=180,blank=True)
    address=models.CharField(max_length=120,blank=True)
    mac_address=models.CharField(max_length=32,blank=True)
    interface_name=models.CharField(max_length=120,blank=True)
    platform=models.CharField(max_length=180,blank=True)
    board=models.CharField(max_length=180,blank=True)
    version=models.CharField(max_length=120,blank=True)
    discovered_by=models.CharField(max_length=120,blank=True)
    device_kind=models.CharField(max_length=40,default='network')
    is_online=models.BooleanField(default=True)
    raw_data=models.JSONField(default=dict,blank=True)
    last_seen_at=models.DateTimeField(default=timezone.now)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['router','neighbor_key'],name='uniq_router_neighbor_key')]
    def __str__(self): return self.identity or self.mac_address or self.address or self.neighbor_key


class RouterDevice(models.Model):
    """Unified MAC/IP device inventory built from multiple RouterOS tables."""
    router=models.ForeignKey(Router,on_delete=models.CASCADE,related_name='devices')
    device_key=models.CharField(max_length=255)
    mac_address=models.CharField(max_length=32,blank=True)
    ip_address=models.CharField(max_length=120,blank=True)
    hostname=models.CharField(max_length=180,blank=True)
    interface_name=models.CharField(max_length=120,blank=True)
    parent_identity=models.CharField(max_length=180,blank=True)
    connection_type=models.CharField(max_length=40,default='wired')
    sources=models.CharField(max_length=255,blank=True)
    is_online=models.BooleanField(default=True)
    raw_data=models.JSONField(default=dict,blank=True)
    first_seen_at=models.DateTimeField(default=timezone.now)
    last_seen_at=models.DateTimeField(default=timezone.now)
    updated_at=models.DateTimeField(auto_now=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['router','device_key'],name='uniq_router_device_key')]
    def __str__(self): return self.hostname or self.mac_address or self.ip_address or self.device_key


class RouterInterfaceRole(models.Model):
    ROLES=[('wan','WAN / Internet'),('lan','LAN'),('hotspot','HotSpot'),('trunk','Trunk'),('management','Management'),('unused','Unused'),('disabled','Disabled')]
    router=models.ForeignKey(Router,on_delete=models.CASCADE,related_name='interface_roles')
    interface_name=models.CharField(max_length=120)
    role=models.CharField(max_length=30,choices=ROLES,default='unused')
    label=models.CharField(max_length=120,blank=True)
    updated_at=models.DateTimeField(auto_now=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['router','interface_name'],name='uniq_router_interface_role')]


class RouterConfigSnapshot(models.Model):
    router=models.OneToOneField(Router,on_delete=models.CASCADE,related_name='config_snapshot')
    sections=models.JSONField(default=dict,blank=True)
    load_balancing=models.JSONField(default=dict,blank=True)
    captured_at=models.DateTimeField(default=timezone.now)
    updated_at=models.DateTimeField(auto_now=True)


class RouterConfigChange(models.Model):
    STATUS=[('success','Success'),('failed','Failed')]
    business=models.ForeignKey(Business,on_delete=models.CASCADE,related_name='router_config_changes')
    router=models.ForeignKey(Router,on_delete=models.CASCADE,related_name='config_changes')
    actor=models.ForeignKey(User,on_delete=models.SET_NULL,null=True,blank=True)
    resource_path=models.CharField(max_length=255)
    operation=models.CharField(max_length=20)
    target_id=models.CharField(max_length=120,blank=True)
    fields=models.JSONField(default=dict,blank=True)
    status=models.CharField(max_length=20,choices=STATUS,default='success')
    error=models.TextField(blank=True)
    created_at=models.DateTimeField(auto_now_add=True)

class RouterSyncJob(models.Model):
    STATUS=[('queued','Queued'),('running','Running'),('success','Success'),('failed','Failed')]
    business=models.ForeignKey(Business,on_delete=models.CASCADE,related_name='router_sync_jobs')
    router=models.ForeignKey(Router,on_delete=models.CASCADE,related_name='sync_jobs')
    requested_by=models.ForeignKey(User,on_delete=models.SET_NULL,null=True,blank=True,related_name='router_sync_jobs')
    celery_task_id=models.CharField(max_length=255,blank=True)
    status=models.CharField(max_length=20,choices=STATUS,default='queued')
    progress=models.PositiveSmallIntegerField(default=0)
    phase=models.CharField(max_length=180,default='Waiting for background worker')
    summary=models.JSONField(default=dict,blank=True)
    error=models.TextField(blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
    started_at=models.DateTimeField(null=True,blank=True)
    finished_at=models.DateTimeField(null=True,blank=True)
    updated_at=models.DateTimeField(auto_now=True)

    class Meta:
        ordering=['-created_at']
        indexes=[
            models.Index(fields=['business','status','-created_at'],name='syncjob_business_status_idx'),
            models.Index(fields=['router','status','-created_at'],name='syncjob_router_status_idx'),
        ]

    @property
    def is_active(self):
        return self.status in {'queued','running'}

    def __str__(self):
        return f'{self.router.name} sync {self.status}'


class SecurityAck(models.Model):
    """A Security Center finding the operator reviewed and accepted (e.g. an intentional bypass)."""
    business=models.ForeignKey(Business,on_delete=models.CASCADE,related_name='security_acks')
    finding_key=models.CharField(max_length=160)
    note=models.CharField(max_length=255,blank=True)
    acknowledged_by=models.ForeignKey(User,on_delete=models.SET_NULL,null=True,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints=[models.UniqueConstraint(fields=['business','finding_key'],name='uniq_security_ack')]

    def __str__(self):
        return f'{self.business}: {self.finding_key}'


# ─────────────────────────────── Finance ───────────────────────────────
PAYMENT_METHODS=[('cash','Cash'),('wave','Wave'),('qmoney','QMoney'),('afrimoney','Afrimoney'),('bank','Bank transfer'),('card','Card'),('auto','Auto (router activation)'),('other','Other')]


class Agent(models.Model):
    """A reseller / sales point that sells vouchers on commission and hands cash back."""
    business=models.ForeignKey(Business,on_delete=models.CASCADE,related_name='agents')
    name=models.CharField(max_length=120)
    phone=models.CharField(max_length=60,blank=True)
    location=models.CharField(max_length=160,blank=True)
    commission_percent=models.DecimalField(max_digits=5,decimal_places=2,default=10)
    active=models.BooleanField(default=True)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta: ordering=['name']
    def __str__(self): return self.name


class VoucherSale(models.Model):
    business=models.ForeignKey(Business,on_delete=models.CASCADE,related_name='sales')
    voucher=models.OneToOneField(Voucher,on_delete=models.SET_NULL,null=True,blank=True,related_name='sale')
    router=models.ForeignKey(Router,on_delete=models.SET_NULL,null=True,blank=True,related_name='sales')
    agent=models.ForeignKey(Agent,on_delete=models.SET_NULL,null=True,blank=True,related_name='sales')
    plan_name=models.CharField(max_length=120)
    voucher_code=models.CharField(max_length=120,blank=True)
    amount=models.DecimalField(max_digits=10,decimal_places=2,default=0)
    discount=models.DecimalField(max_digits=10,decimal_places=2,default=0)
    commission=models.DecimalField(max_digits=10,decimal_places=2,default=0)
    payment_method=models.CharField(max_length=20,choices=PAYMENT_METHODS,default='cash')
    customer_name=models.CharField(max_length=120,blank=True)
    customer_phone=models.CharField(max_length=60,blank=True)
    reference=models.CharField(max_length=120,blank=True)
    notes=models.CharField(max_length=255,blank=True)
    sold_at=models.DateTimeField(default=timezone.now,db_index=True)
    recorded_by=models.ForeignKey(User,on_delete=models.SET_NULL,null=True,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=['-sold_at']
        indexes=[models.Index(fields=['business','sold_at'],name='sale_business_date_idx')]
    @property
    def net(self): return self.amount-self.commission


EXPENSE_CATEGORIES=[('bandwidth','Internet / bandwidth'),('power','Electricity (NAWEC)'),('fuel','Generator fuel'),('rent','Rent & site fees'),
    ('equipment','Equipment'),('salaries','Staff & wages'),('maintenance','Repairs & maintenance'),('marketing','Marketing & printing'),
    ('software','Software & licences'),('transport','Transport'),('tax','Tax & fees'),('other','Other')]


class Expense(models.Model):
    business=models.ForeignKey(Business,on_delete=models.CASCADE,related_name='expenses')
    category=models.CharField(max_length=20,choices=EXPENSE_CATEGORIES,default='other')
    description=models.CharField(max_length=200)
    vendor=models.CharField(max_length=120,blank=True)
    amount=models.DecimalField(max_digits=12,decimal_places=2)
    payment_method=models.CharField(max_length=20,choices=PAYMENT_METHODS,default='cash')
    router=models.ForeignKey(Router,on_delete=models.SET_NULL,null=True,blank=True,related_name='expenses',help_text='Site this cost belongs to')
    reference=models.CharField(max_length=120,blank=True)
    recurring=models.BooleanField(default=False)
    paid_at=models.DateTimeField(default=timezone.now,db_index=True)
    recorded_by=models.ForeignKey(User,on_delete=models.SET_NULL,null=True,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta: ordering=['-paid_at']


class CashCollection(models.Model):
    """Money an agent hands back to the business."""
    business=models.ForeignKey(Business,on_delete=models.CASCADE,related_name='collections')
    agent=models.ForeignKey(Agent,on_delete=models.CASCADE,related_name='collections')
    amount=models.DecimalField(max_digits=12,decimal_places=2)
    payment_method=models.CharField(max_length=20,choices=PAYMENT_METHODS,default='cash')
    reference=models.CharField(max_length=120,blank=True)
    note=models.CharField(max_length=255,blank=True)
    collected_at=models.DateTimeField(default=timezone.now)
    recorded_by=models.ForeignKey(User,on_delete=models.SET_NULL,null=True,blank=True)
    class Meta: ordering=['-collected_at']


# ─────────────────────────────── Studios ───────────────────────────────
class PortalPage(models.Model):
    """A customer-facing page designed in the Portal Studio (hotspot login, post-login redirect, status)."""
    KINDS=[('login','Login page'),('redirect','Redirect page'),('status','Status page')]
    business=models.ForeignKey(Business,on_delete=models.CASCADE,related_name='portal_pages')
    name=models.CharField(max_length=120)
    slug=models.SlugField(max_length=80,unique=True)
    kind=models.CharField(max_length=20,choices=KINDS,default='login')
    template_key=models.CharField(max_length=60,blank=True)
    config=models.JSONField(default=dict,blank=True)
    is_published=models.BooleanField(default=False)
    is_default=models.BooleanField(default=False)
    views=models.PositiveIntegerField(default=0)
    connects=models.PositiveIntegerField(default=0)
    created_at=models.DateTimeField(auto_now_add=True)
    updated_at=models.DateTimeField(auto_now=True)
    class Meta: ordering=['kind','-updated_at']
    def __str__(self): return self.name


class VoucherDesign(models.Model):
    business=models.ForeignKey(Business,on_delete=models.CASCADE,related_name='voucher_designs')
    name=models.CharField(max_length=120)
    template_key=models.CharField(max_length=60,blank=True)
    config=models.JSONField(default=dict,blank=True)
    is_default=models.BooleanField(default=False)
    created_at=models.DateTimeField(auto_now_add=True)
    updated_at=models.DateTimeField(auto_now=True)
    class Meta: ordering=['-is_default','-updated_at']
    def __str__(self): return self.name


class WanSetup(models.Model):
    """The owner's Internet Lines design for one router, and what TapTap last applied."""
    STATUS=[('draft','Draft'),('pending','Applied — waiting for confirmation'),('active','Active'),('undone','Undone'),('failed','Failed')]
    router=models.OneToOneField(Router,on_delete=models.CASCADE,related_name='wan_setup')
    config=models.JSONField(default=dict,blank=True)
    facts=models.JSONField(default=dict,blank=True)
    run_id=models.CharField(max_length=12,blank=True)
    status=models.CharField(max_length=20,choices=STATUS,default='draft')
    original=models.JSONField(default=dict,blank=True,help_text='Router settings before TapTap changed them, used by undo')
    last_result=models.JSONField(default=dict,blank=True)
    applied_at=models.DateTimeField(null=True,blank=True)
    confirm_by=models.DateTimeField(null=True,blank=True)
    confirmed_at=models.DateTimeField(null=True,blank=True)
    applied_by=models.ForeignKey(User,on_delete=models.SET_NULL,null=True,blank=True)
    updated_at=models.DateTimeField(auto_now=True)
    def __str__(self): return f'{self.router} — {self.config.get("strategy","draft")}'


# ─────────────────────────────── Adverts ───────────────────────────────
AD_PLACEMENTS=[('login','Login page'),('redirect','After login'),('status','Status page'),('voucher','Printed vouchers')]


class Advert(models.Model):
    """A campaign shown on portal pages and/or printed vouchers — yours or one you sell to a local business."""
    business=models.ForeignKey(Business,on_delete=models.CASCADE,related_name='adverts')
    name=models.CharField(max_length=120,help_text='Internal name')
    advertiser=models.CharField(max_length=120,blank=True,help_text='Who the ad is for (blank = your own promotion)')
    advertiser_phone=models.CharField(max_length=60,blank=True)
    headline=models.CharField(max_length=120,blank=True)
    body=models.CharField(max_length=280,blank=True)
    cta=models.CharField(max_length=40,blank=True,default='Learn more')
    link=models.URLField(max_length=500,blank=True)
    image=models.TextField(blank=True,help_text='Compressed image as a data: URL so it works offline on the router')
    theme=models.JSONField(default=dict,blank=True,help_text='Colours for text-only ads')
    placements=models.JSONField(default=list,blank=True)
    weight=models.PositiveSmallIntegerField(default=1,help_text='Higher weight = shown more often')
    starts_on=models.DateField(null=True,blank=True)
    ends_on=models.DateField(null=True,blank=True)
    active=models.BooleanField(default=True)
    price=models.DecimalField(max_digits=10,decimal_places=2,default=0,help_text='What the advertiser pays for this campaign')
    paid=models.BooleanField(default=False)
    impressions=models.PositiveIntegerField(default=0)
    clicks=models.PositiveIntegerField(default=0)
    created_at=models.DateTimeField(auto_now_add=True)
    updated_at=models.DateTimeField(auto_now=True)
    class Meta: ordering=['-active','-updated_at']
    def __str__(self): return self.name

    def is_live(self, on=None):
        on=on or timezone.localdate()
        return self.active and (not self.starts_on or self.starts_on<=on) and (not self.ends_on or self.ends_on>=on)


class AdStat(models.Model):
    advert=models.ForeignKey(Advert,on_delete=models.CASCADE,related_name='stats')
    day=models.DateField()
    impressions=models.PositiveIntegerField(default=0)
    clicks=models.PositiveIntegerField(default=0)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['advert','day'],name='uniq_ad_stat_day')]


# ─────────────────────────────── Device signatures ───────────────────────────────
class DeviceSignature(models.Model):
    """A device recognised from browser characteristics collected on the portal.

    Phones randomise their MAC address; the signature stays the same, so one
    device seen under several MACs is still one device, and one voucher used
    on several signatures is being shared.
    """
    business=models.ForeignKey(Business,on_delete=models.CASCADE,related_name='device_signatures')
    fingerprint=models.CharField(max_length=64)
    label=models.CharField(max_length=120,blank=True,help_text='Your own name for this device')
    device_type=models.CharField(max_length=20,blank=True)
    os=models.CharField(max_length=60,blank=True)
    os_version=models.CharField(max_length=40,blank=True)
    browser=models.CharField(max_length=60,blank=True)
    model=models.CharField(max_length=80,blank=True)
    user_agent=models.CharField(max_length=500,blank=True)
    components=models.JSONField(default=dict,blank=True)
    macs=models.JSONField(default=list,blank=True)
    ips=models.JSONField(default=list,blank=True)
    vouchers=models.JSONField(default=list,blank=True)
    last_mac=models.CharField(max_length=32,blank=True)
    last_ip=models.CharField(max_length=64,blank=True)
    router=models.ForeignKey(Router,on_delete=models.SET_NULL,null=True,blank=True,related_name='device_signatures')
    portal=models.ForeignKey('PortalPage',on_delete=models.SET_NULL,null=True,blank=True,related_name='device_signatures')
    visits=models.PositiveIntegerField(default=1)
    flagged=models.BooleanField(default=False)
    note=models.CharField(max_length=255,blank=True)
    first_seen=models.DateTimeField(default=timezone.now)
    last_seen=models.DateTimeField(default=timezone.now,db_index=True)
    class Meta:
        ordering=['-last_seen']
        constraints=[models.UniqueConstraint(fields=['business','fingerprint'],name='uniq_business_fingerprint')]
    def __str__(self): return self.label or self.model or self.fingerprint[:10]

    @property
    def random_macs(self):
        """MACs with the locally-administered bit set are randomised by the phone."""
        out=[]
        for m in self.macs or []:
            try:
                if int(str(m).replace('-',':').split(':')[0],16)&2: out.append(m)
            except ValueError: pass
        return out


class SessionIncident(models.Model):
    """A hotspot session that should not be online (voucher expired, disabled or removed)."""
    REASONS=[('expired','Voucher expired'),('disabled','Voucher disabled in TapTap'),('unknown','Not a TapTap or router voucher')]
    STATUS=[('open','Open'),('fixed','Fixed'),('ended','Session ended by itself'),('ignored','Allowed by you')]
    business=models.ForeignKey(Business,on_delete=models.CASCADE,related_name='session_incidents')
    router=models.ForeignKey(Router,on_delete=models.CASCADE,related_name='session_incidents')
    voucher=models.ForeignKey(Voucher,on_delete=models.SET_NULL,null=True,blank=True,related_name='incidents')
    username=models.CharField(max_length=120)
    mac_address=models.CharField(max_length=32,blank=True)
    ip_address=models.CharField(max_length=64,blank=True)
    session_id=models.CharField(max_length=60,blank=True)
    reason=models.CharField(max_length=20,choices=REASONS,default='expired')
    detail=models.CharField(max_length=255,blank=True)
    status=models.CharField(max_length=20,choices=STATUS,default='open',db_index=True)
    first_seen=models.DateTimeField(default=timezone.now)
    last_seen=models.DateTimeField(default=timezone.now)
    fix_due_at=models.DateTimeField(null=True,blank=True)
    fixed_at=models.DateTimeField(null=True,blank=True)
    fixed_by=models.CharField(max_length=20,blank=True,help_text='auto or user')
    fixed_user=models.ForeignKey(User,on_delete=models.SET_NULL,null=True,blank=True)
    error=models.CharField(max_length=255,blank=True)
    class Meta:
        ordering=['-last_seen']
        indexes=[models.Index(fields=['business','status'],name='incident_business_status_idx')]
    def __str__(self): return f'{self.username} on {self.router}: {self.get_reason_display()}'

from .models_missing import MissingVoucherReport