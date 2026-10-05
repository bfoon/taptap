"""Public member-only views for TapTap Member Self-Service."""
import json
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect,render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from . import member_self_service as svc

def _auth(request,agreement=True):
    m=svc.current_member(request)
    if not m: return None,redirect("member_portal")
    if agreement and not svc.agreement_ok(m): return None,redirect("member_portal_agreement")
    return m,None

def portal(request):
    m=svc.current_member(request)
    if not m: return render(request,"core/member_portal_login.html")
    if not svc.agreement_ok(m): return redirect("member_portal_agreement")
    from .member_arrears import total_arrears
    from .members import plan_for_member
    devices=[]
    for b in m.device_bindings.all().order_by("slot_no"):
        try: stopped=bool(b.member_control.stopped)
        except Exception: stopped=False
        devices.append({"binding":b,"stopped":stopped})
    kind,amount,label=svc.bill(m)
    return render(request,"core/member_portal_dashboard.html",{
        "member":m,"business":m.business,"plan":plan_for_member(m),"devices":devices,
        "arrears":total_arrears(m),"bill_kind":kind,"bill_amount":amount,"bill_label":label,
        "payment_configured":svc.payment_configured(),"events":m.member_portal_events.all()[:50],
        "payments":m.member_payment_intents.all()[:10],"agreement_version":svc.AGREEMENT_VERSION,
    })

@require_POST
def request_link(request):
    svc.request_magic_link(request,request.POST.get("email"))
    messages.success(request,"If that email belongs to an active TapTap member, a secure single-use access link has been sent.")
    return redirect("member_portal")

def access_link(request,token):
    m=svc.consume_magic_link(token)
    if not m:
        messages.error(request,"That access link is invalid, already used, or expired. Request a new link.")
        return redirect("member_portal")
    svc.start_session(request,m); svc.audit(m,"login",request); return redirect("member_portal")

def agreement(request):
    m,r=_auth(request,agreement=False)
    if r:return r
    if request.method=="POST":
        if request.POST.get("accept")!="1": messages.error(request,"You must accept the agreement to use self-service.")
        else:
            svc.accept_agreement(m,request); messages.success(request,"Agreement accepted."); return redirect("member_portal")
    return render(request,"core/member_portal_agreement.html",{
        "member":m,"business":m.business,"agreement_text":svc.AGREEMENT_TEXT,"agreement_version":svc.AGREEMENT_VERSION})

@require_POST
def device(request,pk,action):
    m,r=_auth(request)
    if r:return r
    try: messages.success(request,svc.device_action(m,pk,action,request))
    except ValueError as exc: messages.error(request,str(exc))
    return redirect("member_portal")

@require_POST
def password(request):
    m,r=_auth(request)
    if r:return r
    try:
        ok,result=svc.change_member_password(m,request.POST.get("password") or "",request.POST.get("confirm") or "",request)
        messages.success(request,"Password changed. Connected devices were signed out so the new password takes effect.")
    except (ValueError,Exception) as exc: messages.error(request,f"Password could not be changed: {str(exc)[:200]}")
    return redirect("member_portal")

@require_POST
def payment_start(request):
    m,r=_auth(request)
    if r:return r
    try: intent=svc.create_payment_intent(m,request)
    except ValueError as exc:
        messages.error(request,str(exc)); return redirect("member_portal")
    return redirect(intent.checkout_url)

@csrf_exempt
@require_POST
def payment_webhook(request):
    sig=request.headers.get("X-TapTap-Signature","")
    if not svc.verify_webhook(request.body,sig):
        return JsonResponse({"ok":False,"error":"invalid signature"},status=403)
    try:
        payload=json.loads(request.body.decode("utf-8")); intent=svc.settle_payment(payload)
    except Exception as exc:
        return JsonResponse({"ok":False,"error":str(exc)[:300]},status=400)
    return JsonResponse({"ok":True,"reference":intent.reference,"status":intent.status})

@require_POST
def portal_logout(request):
    m=svc.current_member(request)
    if m: svc.audit(m,"logout",request)
    request.session.pop("member_portal_member_id",None); request.session.pop("member_portal_auth_version",None)
    messages.success(request,"You have been signed out."); return redirect("member_portal")
