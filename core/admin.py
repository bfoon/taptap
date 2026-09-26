from datetime import timedelta
from django.contrib import admin, messages
from django.utils import timezone
from .models import Business,Subscription,VoucherPlan,Router,VoucherBatch,Voucher,VoucherDeviceBinding,IPBindingAccessExpiry,Activity,RouterHotspotUser,SyncedIPBinding,RouterInterface,RouterNeighbor

PLAN_DAYS={'1 Month':30,'2 Months':60,'3 Months':90,'6 Months':180,'1 Year':365}
@admin.action(description='Mark selected subscriptions as Paid and activate business')
def mark_paid(modeladmin,request,queryset):
    count=0
    for sub in queryset.select_related('business'):
        if sub.payment_status=='Paid': continue
        days=PLAN_DAYS.get(sub.plan)
        if not days: continue
        now=timezone.now(); base=max(now,sub.business.subscription_expires_at or now)
        sub.payment_status='Paid'; sub.starts_at=now; sub.expires_at=base+timedelta(days=days); sub.save(update_fields=['payment_status','starts_at','expires_at'])
        sub.business.subscription_status='active'; sub.business.subscription_expires_at=sub.expires_at; sub.business.save(update_fields=['subscription_status','subscription_expires_at']); count+=1
    modeladmin.message_user(request,f'{count} subscription(s) activated.',messages.SUCCESS)

@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display=('business','plan','amount','payment_status','created_at','expires_at'); list_filter=('payment_status','plan'); actions=[mark_paid]
for model in [Business,VoucherPlan,Router,VoucherBatch,Voucher,VoucherDeviceBinding,IPBindingAccessExpiry,Activity,RouterHotspotUser,SyncedIPBinding,RouterInterface,RouterNeighbor]:
    admin.site.register(model)
