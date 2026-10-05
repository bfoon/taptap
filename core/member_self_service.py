"""TapTap Member Self-Service: magic links, audit, device control and secure billing."""
from datetime import timedelta
from decimal import Decimal
import hashlib, hmac, json, logging, secrets
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.urls import path, reverse
from django.utils import timezone

from .models import Activity, Voucher, VoucherDeviceBinding
from .models_member_plans import MemberNotificationSettings
from .models_member_portal import (
    MemberDeviceControl, MemberPaymentIntent, MemberPortalEvent,
    MemberPortalMagicLink, MemberPortalProfile,
)

logger=logging.getLogger("taptap.member_self_service")
AGREEMENT_VERSION="2026-10-v1"
AGREEMENT_TEXT=(
    "TapTap member service is provided only for the named member and the devices permitted by the member's plan. "
    "Reselling, redistributing, sublicensing, or commercially sharing this Internet service with other persons is "
    "strictly prohibited. Confirmed resale or deliberate circumvention of device limits may result in immediate "
    "suspension and permanent banning of the member account. The member is responsible for activity performed "
    "through the account and for keeping the account access link and password secure."
)
MAGIC_MINUTES=20
SESSION_SECONDS=8*60*60
_ORIGINAL_CLAIM=None
_INSTALLED=False

def client_ip(request):
    x=(request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    return x or request.META.get("REMOTE_ADDR") or None

def user_agent(request):
    return (request.META.get("HTTP_USER_AGENT") or "")[:255]

def audit(member,event,request=None,detail=None):
    detail=dict(detail or {})
    row=MemberPortalEvent.objects.create(
        business=member.business,member=member,event=event,detail=detail,
        ip_address=client_ip(request) if request else None,
        user_agent=user_agent(request) if request else "",
    )
    label=dict(MemberPortalEvent.EVENTS).get(event,event)
    Activity.objects.create(
        business=member.business,type=f"Member Portal · {label}",
        details=f"{member.code}: {label}"[:255],status="Success",actor=f"Member {member.code}",
    )
    return row

def profile(member):
    return MemberPortalProfile.objects.get_or_create(member=member)[0]

def notification_email(member):
    try: return (member.member_notification_settings.email or "").strip().lower()
    except MemberNotificationSettings.DoesNotExist: return ""

def _hash(raw): return hashlib.sha256(raw.encode()).hexdigest()

def issue_magic_link(member,request=None,reason="Member portal access"):
    email=notification_email(member)
    if not email: raise ValueError("This member does not have an email address.")
    raw=secrets.token_urlsafe(36); now=timezone.now()
    MemberPortalMagicLink.objects.filter(member=member,used_at__isnull=True,expires_at__gt=now).update(expires_at=now)
    MemberPortalMagicLink.objects.create(
        business=member.business,member=member,email=email,token_hash=_hash(raw),
        expires_at=now+timedelta(minutes=MAGIC_MINUTES),
        request_ip=client_ip(request) if request else None,
        user_agent=user_agent(request) if request else "",
    )
    site=(getattr(settings,"SITE_URL","") or "").rstrip("/")
    route=reverse("member_portal_access",args=[raw])
    url=f"{site}{route}" if site else (request.build_absolute_uri(route) if request else route)
    from .member_notifications import _send
    _send(email,f"[{member.business.business_name}] Your member account access link",
          "core/email/member_portal_magic.txt","core/email/member_portal_magic.html",
          {"business":member.business,"member":member,"access_url":url,"minutes":MAGIC_MINUTES,"reason":reason})
    audit(member,"link_sent",request,{"email":email,"reason":reason})
    return url

def request_magic_link(request,email):
    normalized=(email or "").strip().lower()
    if not normalized: return True
    ipkey=_hash(client_ip(request) or "unknown")[:24]; emkey=_hash(normalized)[:24]
    for key,limit in ((f"mpl-ip:{ipkey}",20),(f"mpl-em:{emkey}",5)):
        n=int(cache.get(key,0) or 0)
        if n>=limit: return True
        cache.set(key,n+1,3600)
    qs=(MemberNotificationSettings.objects.filter(email__iexact=normalized,member__login_type="member")
        .select_related("member__business"))
    for pref in qs[:5]:
        m=pref.member
        if m.deleted_at: continue
        audit(m,"link_requested",request,{"email":normalized})
        try: issue_magic_link(m,request=request)
        except Exception: logger.exception("Member magic link failed for %s",m.pk)
    return True

@transaction.atomic
def consume_magic_link(raw):
    now=timezone.now()
    row=(MemberPortalMagicLink.objects.select_for_update().select_related("member__business")
         .filter(token_hash=_hash(raw)).first())
    if not row or row.used_at or row.expires_at<=now or row.member.deleted_at: return None
    row.used_at=now; row.save(update_fields=["used_at"])
    p=profile(row.member); p.last_portal_login_at=now
    p.save(update_fields=["last_portal_login_at","updated_at"])
    return row.member

def start_session(request,member):
    request.session.cycle_key(); p=profile(member)
    request.session["member_portal_member_id"]=member.pk
    request.session["member_portal_auth_version"]=p.auth_version
    request.session.set_expiry(SESSION_SECONDS)

def current_member(request):
    pk=request.session.get("member_portal_member_id")
    if not pk: return None
    m=(Voucher.objects.filter(pk=pk,login_type="member",deleted_at__isnull=True)
       .select_related("business","router","member_plan_assignment__plan","member_notification_settings").first())
    if not m: return None
    if int(request.session.get("member_portal_auth_version") or 0)!=profile(m).auth_version: return None
    return m

def agreement_ok(member):
    p=profile(member)
    return p.agreement_version==AGREEMENT_VERSION and bool(p.agreement_accepted_at)

def accept_agreement(member,request):
    p=profile(member); p.agreement_version=AGREEMENT_VERSION
    p.agreement_accepted_at=timezone.now(); p.agreement_ip=client_ip(request)
    p.save(update_fields=["agreement_version","agreement_accepted_at","agreement_ip","updated_at"])
    audit(member,"agreement",request,{"version":AGREEMENT_VERSION})

def _stopped_binding(voucher,mac="",fp=""):
    qs=VoucherDeviceBinding.objects.filter(voucher=voucher); b=None
    if fp: b=qs.filter(device_token_hash=fp).first()
    if b is None and mac: b=qs.filter(current_mac__iexact=mac).first()
    if b is None: return None
    try: return b if b.member_control.stopped else None
    except MemberDeviceControl.DoesNotExist: return None

def protected_claim(voucher,mac="",fp="",source="portal",label="",hints=None):
    stopped=_stopped_binding(voucher,mac=mac,fp=fp)
    if stopped:
        from .device_lock import Outcome
        return Outcome("denied",binding=stopped,message="This device is stopped in your TapTap Member Portal.")
    return _ORIGINAL_CLAIM(voucher,mac=mac,fp=fp,source=source,label=label,hints=hints)

def _kick_binding(member,binding):
    from . import device_lock
    results=[]
    for mac in dict.fromkeys(x for x in ((binding.current_mac or "").strip(),(binding.previous_mac or "").strip()) if x):
        try: results.append(str(device_lock._forget_mac(member,mac) or "requested"))
        except Exception as exc: results.append(str(exc)[:160])
    return results

def get_member_binding(member,binding_id):
    try:
        return VoucherDeviceBinding.objects.select_for_update().get(pk=int(binding_id),voucher=member)
    except (VoucherDeviceBinding.DoesNotExist,TypeError,ValueError):
        raise ValueError("That device is not part of this member account.")

@transaction.atomic
def device_action(member,binding_id,action,request=None):
    b=get_member_binding(member,binding_id)
    ctl,_=MemberDeviceControl.objects.select_for_update().get_or_create(binding=b)
    if action=="stop":
        ctl.stopped=True; ctl.stopped_at=timezone.now(); ctl.save(update_fields=["stopped","stopped_at","updated_at"])
        r=_kick_binding(member,b); audit(member,"device_stop",request,{"binding":b.pk,"mac":b.current_mac,"router":r})
        return "Device stopped. It cannot reconnect until you start it."
    if action=="start":
        ctl.stopped=False; ctl.stopped_at=None; ctl.save(update_fields=["stopped","stopped_at","updated_at"])
        audit(member,"device_start",request,{"binding":b.pk,"mac":b.current_mac})
        return "Device started. It may reconnect now."
    if action=="restart":
        ctl.stopped=False; ctl.stopped_at=None; ctl.save(update_fields=["stopped","stopped_at","updated_at"])
        r=_kick_binding(member,b); audit(member,"device_restart",request,{"binding":b.pk,"mac":b.current_mac,"router":r})
        return "Device session restarted. Reconnect if it does not reconnect automatically."
    if action=="remove":
        old={"binding":b.pk,"slot":b.slot_no,"mac":b.current_mac,"label":b.label}
        from .shared_voucher_device_control import remove_one
        ok,msg=remove_one(member,b.pk,user=None)
        if not ok: raise ValueError(msg)
        audit(member,"device_remove",request,old); return "Device removed and its slot is now free."
    raise ValueError("Unsupported device action.")

def change_member_password(member,new_password,confirm,request):
    if new_password!=confirm: raise ValueError("The two passwords do not match.")
    if len(new_password or "")<8: raise ValueError("Use at least 8 characters.")
    if len(new_password)>64: raise ValueError("Password cannot exceed 64 characters.")
    from . import members as mem
    ok,result=mem.change_password(member,password=new_password,same=False,user=None,reason="Member self-service")
    member.refresh_from_db()
    try:
        from .member_router_alignment import aligned_push_one
        aligned=str(aligned_push_one(member))[:200]
    except Exception as exc: aligned=str(exc)[:200]
    for b in member.device_bindings.all(): _kick_binding(member,b)
    p=profile(member); p.auth_version+=1; p.save(update_fields=["auth_version","updated_at"])
    request.session["member_portal_auth_version"]=p.auth_version
    audit(member,"password_changed",request,{"router_result":str(result)[:200],"alignment":aligned})
    return ok,result

def bill(member):
    from .member_arrears import total_arrears
    a=total_arrears(member)
    if a>0: return "arrears",Decimal(a).quantize(Decimal("0.01")),"Outstanding balance"
    try: plan=member.member_plan_assignment.plan
    except Exception: plan=None
    if not plan or not plan.duration_minutes or plan.price<=0: return None,Decimal("0.00"),""
    return "renewal",Decimal(plan.price).quantize(Decimal("0.01")),f"Renew {plan.name}"

def payment_configured():
    return bool(getattr(settings,"MEMBER_PAYMENT_CHECKOUT_URL","") and getattr(settings,"MEMBER_PAYMENT_WEBHOOK_SECRET",""))

def _append_query(url,params):
    p=list(urlparse(url)); q=dict(parse_qsl(p[4],keep_blank_values=True)); q.update(params); p[4]=urlencode(q); return urlunparse(p)

def create_payment_intent(member,request):
    kind,amount,label=bill(member)
    if not kind or amount<=0: raise ValueError("There is currently no bill to pay.")
    if not payment_configured(): raise ValueError("Online payment is not configured yet. Connect a payment provider first.")
    ref="MP-"+secrets.token_hex(10).upper()
    intent=MemberPaymentIntent.objects.create(business=member.business,member=member,reference=ref,kind=kind,
        amount=amount,currency=member.business.currency,detail={"label":label})
    site=(getattr(settings,"SITE_URL","") or "").rstrip("/")
    ret=f"{site}{reverse('member_portal')}" if site else request.build_absolute_uri(reverse("member_portal"))
    hook=f"{site}{reverse('member_portal_payment_webhook')}" if site else request.build_absolute_uri(reverse("member_portal_payment_webhook"))
    intent.checkout_url=_append_query(getattr(settings,"MEMBER_PAYMENT_CHECKOUT_URL"),{
        "reference":ref,"amount":f"{amount:.2f}","currency":member.business.currency,
        "email":notification_email(member),"member":member.code,"return_url":ret,"webhook_url":hook})
    intent.save(update_fields=["checkout_url","updated_at"])
    audit(member,"payment_started",request,{"reference":ref,"kind":kind,"amount":str(amount)})
    return intent

def verify_webhook(raw,signature):
    secret=str(getattr(settings,"MEMBER_PAYMENT_WEBHOOK_SECRET","") or "")
    if not secret or not signature: return False
    expected=hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected,signature.removeprefix("sha256=").strip())

@transaction.atomic
def settle_payment(payload):
    ref=str(payload.get("reference") or "").strip(); status=str(payload.get("status") or "").lower()
    provider_ref=str(payload.get("provider_reference") or "")[:120]
    amount=Decimal(str(payload.get("amount") or "0")).quantize(Decimal("0.01"))
    intent=(MemberPaymentIntent.objects.select_for_update().select_related("member__business")
            .filter(reference=ref).first())
    if not intent: raise ValueError("Unknown payment reference.")
    if intent.status=="paid": return intent
    if status not in {"paid","successful","success","completed"}:
        intent.status="failed"; intent.provider_reference=provider_ref
        intent.detail={**(intent.detail or {}),"callback":payload}; intent.save()
        audit(intent.member,"payment_failed",None,{"reference":ref,"provider_status":status}); return intent
    if amount!=intent.amount:
        intent.status="review"; intent.detail={**(intent.detail or {}),"error":"Amount mismatch","callback":payload}; intent.save()
        raise ValueError("Payment amount does not match the bill.")
    member=intent.member
    if intent.kind=="arrears":
        from .member_arrears import total_arrears,collect_balance
        current=Decimal(total_arrears(member)).quantize(Decimal("0.01"))
        if current!=intent.amount:
            intent.status="review"; intent.detail={**(intent.detail or {}),"error":f"Current arrears {current}"}; intent.save()
            raise ValueError("Member balance changed after checkout started; payment requires review.")
        payment=collect_balance(member,amount=intent.amount,method="online",reference=provider_ref or ref,agent=None,user=None)
        detail={"balance_payment_id":payment.pk}
    else:
        from .member_arrears import total_arrears
        from .members import plan_for_member,renew_with_receipt
        if total_arrears(member)>0: raise ValueError("Member now has arrears; payment requires review.")
        plan=plan_for_member(member)
        if not plan or Decimal(plan.price).quantize(Decimal("0.01"))!=intent.amount: raise ValueError("Member plan or price changed.")
        ok,result,sale,minutes,renewal=renew_with_receipt(member,amount=intent.amount,method="online",
            reference=provider_ref or ref,agent=None,user=None,require_amount=True)
        try:
            from .member_notifications import send_renewal_receipt
            send_renewal_receipt(renewal)
        except Exception: logger.exception("Renewal receipt email failed")
        detail={"renewal_id":renewal.pk,"sale_id":sale.pk if sale else None,"router_result":str(result)[:200]}
    intent.status="paid"; intent.provider_reference=provider_ref; intent.paid_at=timezone.now()
    intent.detail={**(intent.detail or {}),**detail,"callback":payload}; intent.save()
    audit(member,"payment_paid",None,{"reference":ref,"amount":str(intent.amount),"kind":intent.kind}); return intent

def _install_device_guard():
    global _ORIGINAL_CLAIM
    from . import device_lock
    if getattr(device_lock.claim,"_member_portal_guard",False): return
    _ORIGINAL_CLAIM=device_lock.claim; protected_claim._member_portal_guard=True; device_lock.claim=protected_claim

def _install_urls():
    from . import urls
    from . import views_member_portal as v
    names={getattr(p,"name",None) for p in urls.urlpatterns}
    routes=[
      ("member_portal",path("member/",v.portal,name="member_portal")),
      ("member_portal_request",path("member/link/",v.request_link,name="member_portal_request")),
      ("member_portal_access",path("member/access/<str:token>/",v.access_link,name="member_portal_access")),
      ("member_portal_agreement",path("member/agreement/",v.agreement,name="member_portal_agreement")),
      ("member_portal_device",path("member/device/<int:pk>/<str:action>/",v.device,name="member_portal_device")),
      ("member_portal_password",path("member/password/",v.password,name="member_portal_password")),
      ("member_portal_payment_start",path("member/payment/start/",v.payment_start,name="member_portal_payment_start")),
      ("member_portal_payment_webhook",path("member/payment/webhook/",v.payment_webhook,name="member_portal_payment_webhook")),
      ("member_portal_logout",path("member/logout/",v.portal_logout,name="member_portal_logout")),
    ]
    for name,r in routes:
        if name not in names: urls.urlpatterns.append(r)

def _install_new_member_invite():
    from django.db.models.signals import post_save
    def created(sender,instance,created,**kwargs):
        if not created or not instance.email or instance.member.login_type!="member": return
        transaction.on_commit(lambda: _safe_invite(instance.member))
    post_save.connect(created,sender=MemberNotificationSettings,dispatch_uid="taptap-member-portal-invite",weak=False)

def _safe_invite(member):
    try: issue_magic_link(member,reason="Welcome to your TapTap Member Self-Service Portal")
    except Exception: logger.exception("New-member portal invite failed for %s",member.pk)

def install():
    global _INSTALLED
    if _INSTALLED: return
    _install_device_guard(); _install_urls(); _install_new_member_invite(); _INSTALLED=True
