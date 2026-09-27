from datetime import timedelta
from django.contrib import admin, messages
from django.utils import timezone
from .models import (
    Business,Subscription,VoucherPlan,Router,VoucherBatch,Voucher,VoucherDeviceBinding,
    IPBindingAccessExpiry,Activity,RouterHotspotProfile,RouterHotspotUser,SyncedIPBinding,
    RouterInterface,RouterNeighbor,RouterDevice,RouterInterfaceRole,RouterConfigSnapshot,RouterConfigChange,RouterSyncJob,SecurityAck,
)

PLAN_DAYS={'1 Month':30,'2 Months':60,'3 Months':90,'6 Months':180,'1 Year':365}

@admin.action(description='Mark selected subscriptions as Paid and activate business')
def mark_paid(modeladmin,request,queryset):
    count=0
    for sub in queryset.select_related('business'):
        if sub.payment_status=='Paid': continue
        days=PLAN_DAYS.get(sub.plan)
        if not days: continue
        now=timezone.now();base=max(now,sub.business.subscription_expires_at or now)
        sub.payment_status='Paid';sub.starts_at=now;sub.expires_at=base+timedelta(days=days);sub.save(update_fields=['payment_status','starts_at','expires_at'])
        sub.business.subscription_status='active';sub.business.subscription_expires_at=sub.expires_at;sub.business.save(update_fields=['subscription_status','subscription_expires_at']);count+=1
    modeladmin.message_user(request,f'{count} subscription(s) activated.',messages.SUCCESS)

@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display=('business','plan','amount','payment_status','created_at','expires_at');list_filter=('payment_status','plan');actions=[mark_paid]

@admin.register(RouterDevice)
class RouterDeviceAdmin(admin.ModelAdmin):
    list_display=('router','hostname','mac_address','ip_address','interface_name','connection_type','is_online','last_seen_at')
    list_filter=('is_online','connection_type','router')
    search_fields=('hostname','mac_address','ip_address','interface_name','parent_identity')

@admin.register(RouterConfigChange)
class RouterConfigChangeAdmin(admin.ModelAdmin):
    list_display=('router','operation','resource_path','target_id','status','actor','created_at')
    list_filter=('status','operation','router')
    readonly_fields=('created_at',)

for model in [Business,VoucherPlan,Router,VoucherBatch,Voucher,VoucherDeviceBinding,IPBindingAccessExpiry,Activity,RouterHotspotProfile,RouterHotspotUser,SyncedIPBinding,RouterInterface,RouterNeighbor,RouterInterfaceRole,RouterConfigSnapshot,SecurityAck]:
    admin.site.register(model)


@admin.register(RouterSyncJob)
class RouterSyncJobAdmin(admin.ModelAdmin):
    list_display=('router','status','progress','phase','requested_by','created_at','finished_at')
    list_filter=('status','router')
    search_fields=('router__name','phase','error','celery_task_id')
    readonly_fields=('business','router','requested_by','celery_task_id','status','progress','phase','summary','error','created_at','started_at','finished_at','updated_at')


# ── Finance & Studios ──
from .models import Agent, VoucherSale, Expense, CashCollection, PortalPage, VoucherDesign


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = ('name', 'business', 'phone', 'commission_percent', 'active'); list_filter = ('active',); search_fields = ('name', 'phone', 'business__business_name')


@admin.register(VoucherSale)
class VoucherSaleAdmin(admin.ModelAdmin):
    list_display = ('sold_at', 'business', 'voucher_code', 'plan_name', 'amount', 'payment_method', 'agent'); list_filter = ('payment_method',)
    search_fields = ('voucher_code', 'reference', 'customer_phone', 'business__business_name'); date_hierarchy = 'sold_at'; raw_id_fields = ('voucher',)


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ('paid_at', 'business', 'category', 'description', 'amount', 'recurring'); list_filter = ('category', 'recurring'); date_hierarchy = 'paid_at'


@admin.register(CashCollection)
class CashCollectionAdmin(admin.ModelAdmin):
    list_display = ('collected_at', 'business', 'agent', 'amount', 'payment_method')


@admin.register(PortalPage)
class PortalPageAdmin(admin.ModelAdmin):
    list_display = ('name', 'business', 'kind', 'slug', 'is_published', 'is_default', 'views', 'connects', 'updated_at'); list_filter = ('kind', 'is_published')


@admin.register(VoucherDesign)
class VoucherDesignAdmin(admin.ModelAdmin):
    list_display = ('name', 'business', 'template_key', 'is_default', 'updated_at')


from .models import WanSetup


@admin.register(WanSetup)
class WanSetupAdmin(admin.ModelAdmin):
    list_display = ('router', 'status', 'run_id', 'applied_at', 'confirmed_at'); list_filter = ('status',); readonly_fields = ('original', 'last_result', 'facts')
