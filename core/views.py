from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
import re

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import transaction, DatabaseError
from django.db.models import Count, Sum, Q
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .models import PAYMENT_METHODS
from .forms import RegisterForm, RouterForm, PlanForm
from .models import (
    Business, Subscription, VoucherPlan, Router, VoucherBatch, Voucher, Activity,
    SyncedIPBinding, RouterHotspotProfile, RouterHotspotUser, RouterNeighbor, RouterDevice, RouterInterfaceRole,
    RouterConfigSnapshot, RouterConfigChange, RouterSyncJob, SecurityAck,
)
from .netgraph import build_graph
from .security import audit_business, summarize
from .mikrotik import MikroTikService, MikroTikError, redact
from .sync import sync_router, refresh_router_topology, snapshot_from_database
from .tasks import enqueue_router_sync
from .utils import (generate_codes, code_format_from_post, code_format_ctx, describe_format, portal_code_length, CodeFormatError,
                    duration_to_routeros, log, code_search_q)
from .portal_deploy import default_pages
from . import serials
from .utils import voucher_profile as _vprofile
from .profile_time import profile_state as _profile_state

import logging


def _on_link(router):
    """True when this router must be handled through TapTap Link right now
    (enrolled in Link and its TapTap Tunnel is not healthy)."""
    from .linkops import uses_link
    return uses_link(router)


logger=logging.getLogger('taptap')

SUBSCRIPTION_PACKAGES = {
    '1m': ('1 Month', Decimal('700'), 30), '2m': ('2 Months', Decimal('1350'), 60),
    '3m': ('3 Months', Decimal('2000'), 90), '6m': ('6 Months', Decimal('4000'), 180),
    '1y': ('1 Year', Decimal('8000'), 365),
}
DEFAULT_PLANS = [('12 Hours',25,12,1),('24 Hours',40,24,1),('Weekly',150,168,1),('Monthly',500,720,1),('Family Monthly',700,720,5)]


def home(request): return render(request, 'core/home.html')


def register(request):
    if request.user.is_authenticated: return redirect('dashboard')
    form = RegisterForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        email = form.cleaned_data['email'].lower()
        if User.objects.filter(username=email).exists():
            messages.error(request, 'An account with this email already exists.')
        else:
            with transaction.atomic():
                user = User.objects.create_user(username=email,email=email,password=form.cleaned_data['password'],first_name=form.cleaned_data['owner_name'])
                business = Business.objects.create(user=user,business_name=form.cleaned_data['business_name'],owner_name=form.cleaned_data['owner_name'],phone=form.cleaned_data['phone'],trial_ends_at=timezone.now()+timedelta(days=settings.TRIAL_DAYS))
                from .durations import best_unit
                for n,p,h,d in DEFAULT_PLANS: VoucherPlan.objects.create(business=business,name=n,price=p,duration_minutes=h*60,duration_unit=best_unit(h*60),max_devices=d,created_by_label='TapTap starter plans')
            login(request,user); messages.success(request,f'Welcome to TapTap. Your {settings.TRIAL_DAYS}-day trial is active.'); return redirect('dashboard')
    return render(request,'core/register.html',{'form':form})


def login_view(request):
    if request.user.is_authenticated: return redirect('dashboard')
    if request.method == 'POST':
        user = authenticate(request,username=request.POST.get('email','').strip().lower(),password=request.POST.get('password',''))
        if user: login(request,user); return redirect('dashboard')
        messages.error(request,'Invalid email or password.')
    return render(request,'core/login.html')


def logout_view(request): logout(request); return redirect('home')
def b(request): return request.user.business


@login_required
def dashboard(request):
    business=b(request); vouchers=business.vouchers.all(); routers=business.routers.all()
    month_start=timezone.localtime().replace(day=1,hour=0,minute=0,second=0,microsecond=0)
    revenue=business.sales.filter(sold_at__gte=month_start).aggregate(v=Sum('amount'))['v'] or 0
    stats={'vouchers':vouchers.count(),'available':vouchers.filter(status='active',used_at__isnull=True,sold_at__isnull=True).count(),'active':vouchers.filter(status='active',used_at__isnull=False).count(),'routers':routers.count(),'online':routers.filter(status='Online').count(),'revenue':revenue}
    return render(request,'core/dashboard.html',{'stats':stats,'activities':business.activities.order_by('-created_at')[:8],'routers':routers[:5],'top_plans':vouchers.values('plan_name').annotate(total=Count('id')).order_by('-total')[:5]})


@login_required
def subscription(request): return render(request,'core/subscription.html',{'packages':SUBSCRIPTION_PACKAGES})


@login_required
def subscription_select(request,code):
    if code not in SUBSCRIPTION_PACKAGES:return redirect('subscription')
    name,amount,days=SUBSCRIPTION_PACKAGES[code]; business=b(request)
    Subscription.objects.create(business=business,plan=name,amount=amount,payment_method='Manual',payment_status='Pending')
    messages.success(request,f'{name} subscription request created for D{amount}. Mark it paid from Django Admin after payment confirmation.')
    return redirect('subscription')


@login_required
def vouchers(request):
    from django.core.paginator import Paginator
    business=b(request);qs=business.vouchers.select_related('router','batch','agent').order_by('-created_at')
    state=request.GET.get('state','');plan=request.GET.get('plan','');q=request.GET.get('q','').strip()
    if state=='unsold': qs=qs.filter(status='active',sold_at__isnull=True,used_at__isnull=True)
    elif state=='sold': qs=qs.filter(sold_at__isnull=False,used_at__isnull=True)
    elif state=='used': qs=qs.filter(used_at__isnull=False)
    elif state=='disabled': qs=qs.exclude(status__in=['active','archived']).filter(frozen_at__isnull=True)
    elif state=='archived': qs=qs.filter(status='archived')
    elif state=='frozen': qs=qs.filter(frozen_at__isnull=False)
    if plan: qs=qs.filter(plan_name=plan)
    holder=request.GET.get('holder','')
    if holder=='shop': qs=qs.filter(agent__isnull=True)
    elif holder=='individual': qs=qs.filter(batch__isnull=True,source='taptap')
    elif holder.isdigit(): qs=qs.filter(agent_id=holder)
    if q: qs=qs.filter(code_search_q(q,'code',('batch__name','customer_name','customer_phone','serial'),also_codes=('code_aliases__code',))).distinct()
    counts=business.vouchers.aggregate(all=Count('id'),unsold=Count('id',filter=Q(status='active',sold_at__isnull=True,used_at__isnull=True)),
        sold=Count('id',filter=Q(sold_at__isnull=False,used_at__isnull=True)),used=Count('id',filter=Q(used_at__isnull=False)),disabled=Count('id',filter=~Q(status__in=['active','archived'])&Q(frozen_at__isnull=True)),
        frozen=Count('id',filter=Q(frozen_at__isnull=False)),archived=Count('id',filter=Q(status='archived')))
    params=request.GET.copy();params.pop('page',None)
    state_tabs=[('','All',counts['all']),('unsold','In stock',counts['unsold']),('sold','Sold, not used',counts['sold']),('used','Used',counts['used']),('frozen','Frozen / warned',counts['frozen']),('disabled','Disabled / expired',counts['disabled']),('archived','Archived',counts['archived'])]
    return render(request,'core/vouchers.html',{'page_obj':Paginator(qs,100).get_page(request.GET.get('page')),'counts':counts,'state_tabs':state_tabs,'state':state,'plan':plan,'q':q,
        'plans':business.vouchers.exclude(plan_name__startswith='*').exclude(plan_name='').values_list('plan_name',flat=True).distinct().order_by('plan_name'),
        'agents':business.agents.filter(active=True),'all_agents':business.agents.all(),'holder':holder,
        'designs':business.voucher_designs.all(),'params':params.urlencode()})


@login_required
def generate_vouchers(request):
    business=b(request); plans=business.plans.filter(active=True).exclude(name__startswith='*'); routers=business.routers.all()
    portal_len=portal_code_length(business)
    ctx={'plans':plans,'routers':routers,'designs':business.voucher_designs.all(),
         'agents':business.agents.filter(active=True),'owner':request.GET.get('agent',''),'methods':[m for m in PAYMENT_METHODS if m[0]!='auto'],
         'cf':code_format_ctx(business),'sn':serials.settings_ctx(business)}
    if request.method=='POST':
        plan=get_object_or_404(plans,pk=request.POST.get('plan'))
        try: qty=max(1,min(500,int(request.POST.get('quantity','1'))))
        except ValueError: qty=1
        router_id=(request.POST.get('router') or '').strip()
        router=routers.filter(pk=int(router_id)).first() if router_id.isdigit() else None
        batch_name=request.POST.get('batch_name','').strip() or f'{plan.name} {timezone.localtime():%Y-%m-%d %H:%M}'
        agent=business.agents.filter(pk=request.POST.get('owner') or 0).first()
        try:
            fmt=code_format_from_post(request.POST,business)
            codes=generate_codes(qty,fmt['length'],fmt['charset'],fmt['prefix'],fmt['suffix'],business)
            P=request.POST
            sfmt,sdig,sreset=serials.clean(P.get('serial_format',business.serial_format),P.get('serial_digits',business.serial_digits),P.get('serial_reset',business.serial_reset))
            sstart=int(P['serial_start']) if (P.get('serial_start') or '').strip().isdigit() and int(P['serial_start'])>0 else None
        except (CodeFormatError,serials.SerialError) as e:
            messages.error(request,str(e)); ctx['cf']=code_format_ctx(business,request.POST); ctx['sn']=serials.settings_ctx(business,request.POST)
            return render(request,'core/generate_vouchers.html',ctx,status=400)
        with transaction.atomic():
            if P.get('serial_save'):
                business.serial_format,business.serial_digits,business.serial_reset=sfmt,sdig,sreset
                business.save(update_fields=['serial_format','serial_digits','serial_reset'])
            batch=VoucherBatch.objects.create(business=business,name=batch_name,plan=plan,quantity=qty,note=request.POST.get('note','')[:255])
            sns=serials.allocate(business,qty,fmt=sfmt,digits=sdig,reset=sreset,start=sstart,batch=batch,plan=plan.name)
            for c,sn in zip(codes,sns):
                Voucher.objects.create(business=business,batch=batch,router=router,code=c,serial=sn,plan_name=plan.name,price=plan.price,duration_minutes=plan.duration_minutes,max_devices=plan.max_devices,source='taptap')
            log(business,'Voucher Generated',f'Batch {batch.name}: {qty} voucher(s), {describe_format(fmt)}, serials {sns[0]}–{sns[-1]}'+(f' for {agent.name}' if agent else ''))
            if agent:
                from .finance import assign_batch
                settlement=request.POST.get('settlement') if request.POST.get('settlement') in {'credit','prepaid'} else 'credit'
                _,sales=assign_batch(batch,agent,settlement,request.POST.get('pay_method','cash'),request.user,request.POST.get('reference','')[:120])
                messages.info(request,f'Batch issued to {agent.name}'+(f' — bought upfront for {business.currency}{sum(x.amount for x in sales):,.2f}.' if sales else ' on credit: each voucher is credited to them as it sells.'))
        if router:
            # Send just this batch to the router — not a full router sync.
            try:
                from .voucher_push import push_vouchers
                for msg in push_vouchers(list(batch.vouchers.all()),request.user).values():
                    messages.info(request,msg+'.')
            except Exception as e:
                messages.warning(request,f'Vouchers were created in TapTap; sending them to {router.name} failed for now ({e}). They go with the next sync.')
        messages.success(request,f'{qty} voucher(s) created successfully.')
        try:
            from .tracking import notify as _track
            _track(business,'vouchers_added',f'{qty} new voucher(s) of {plan.name} in batch {batch.name}'+(f' by {request.user.get_full_name() or request.user.username}' if request.user.is_authenticated else ''),
                   batch=batch,plan=plan,actor=request.user,count=qty)
        except Exception: pass
        oid=request.POST.get('order','')
        if oid.isdigit():
            # an agent's order from the voucher checker: done — and the agent sees "Ready" on their phone
            from .models import AgentOrder
            AgentOrder.objects.filter(pk=int(oid),business=business,status='new').update(status='done',batch=batch,done_at=timezone.now(),done_by=request.user)
            business.event_alerts.filter(kind='agent_order',link__contains=f'order={oid}',read_at__isnull=True).update(read_at=timezone.now())
        if fmt['length']!=portal_len and 'login' in default_pages(business):
            messages.warning(request,f'These codes have {fmt["length"]} characters but your default customer portal shows {portal_len} letter boxes. '
                                     f'Customers can still log in, but set the boxes to {fmt["length"]} in Portal Studio if you want them to match.')
        sheet=None
        if request.POST.get('print_after'):
            design=request.POST.get('design','')
            sheet=f"/studio/vouchers/print/?batch={batch.pk}"+(f"&design={design}" if design else '')
        if request.POST.get('print_receipt'):
            from urllib.parse import quote
            from django.urls import reverse
            return redirect(reverse('batch_receipt',args=[batch.pk])+'?autoprint=1'+(f'&next={quote(sheet)}' if sheet else ''))
        if sheet:
            return redirect(sheet)
        return redirect('vouchers')
    return render(request,'core/generate_vouchers.html',ctx)


@login_required
def _voucher_back(request, v):
    nxt=request.POST.get('next','')
    if nxt.startswith('/') and not nxt.startswith('//'): return redirect(nxt)
    return redirect('voucher_detail',pk=v.pk)


def _voucher_result(request, v, ok, result, done):
    if ok: messages.success(request,f'{v.code} {done}. {result}.' if result else f'{v.code} {done}.')
    else: messages.warning(request,f'{v.code} {done} in TapTap, but the router was not updated: {result}')


@login_required
def disable_voucher(request,pk):
    from . import voucher_history as vh
    v=get_object_or_404(b(request).vouchers,pk=pk)
    if request.method!='POST': return redirect('voucher_detail',pk=pk)
    try: ok,result=vh.disable(v,request.user,request.POST.get('reason','').strip())
    except vh.VoucherActionError as e: messages.error(request,str(e)); return _voucher_back(request,v)
    log(b(request),'Voucher Disabled',v.code);_voucher_result(request,v,ok,result,'disabled')
    return _voucher_back(request,v)


@login_required
def enable_voucher(request,pk):
    from . import voucher_history as vh
    v=get_object_or_404(b(request).vouchers,pk=pk)
    if request.method!='POST': return redirect('voucher_detail',pk=pk)
    P=request.POST
    try:
        minutes=vh.parse_added_time(P.get('add_days'),P.get('add_hrs'),P.get('add_mins'))
        ok,result=vh.enable(v,request.user,P.get('reason','').strip(),P.get('add_hours') if not minutes else None,add_minutes=minutes or None)
    except vh.VoucherActionError as e: messages.error(request,str(e)); return _voucher_back(request,v)
    log(b(request),'Voucher Time Added' if minutes else 'Voucher Enabled',v.code+(f' +{vh._mtext(minutes)}' if minutes else ''))
    _voucher_result(request,v,ok,result,f'given {vh._mtext(minutes)} more' if minutes else 'enabled')
    return _voucher_back(request,v)


@login_required
def change_voucher_code(request,pk):
    """Give a voucher a new code. Same voucher record: sale, history and usage stay attached."""
    from . import voucher_codes as vc
    v=get_object_or_404(b(request).vouchers.select_related('router'),pk=pk)
    if request.method!='POST': return redirect('voucher_detail',pk=pk)
    old=v.code
    try: ok,result=vc.change_code(v,request.POST.get('new_code',''),request.user,request.POST.get('reason',''))
    except vc.CodeChangeError as e: messages.error(request,str(e)); return redirect('voucher_detail',pk=pk)
    messages.success(request,f'Code changed from {old} to {v.code}. The sale, history and usage stay with this voucher.')
    (messages.info if ok else messages.warning)(request,result)
    return redirect('voucher_detail',pk=pk)


@login_required
def reset_mac(request,pk):
    from . import voucher_history as vh
    v=get_object_or_404(b(request).vouchers,pk=pk)
    if request.method!='POST': return redirect('voucher_detail',pk=pk)
    ok,result=vh.reset_devices(v,request.user,request.POST.get('reason','').strip())
    log(b(request),'Voucher MAC Reset',v.code);_voucher_result(request,v,ok,result,'device binding reset')
    return _voucher_back(request,v)


def _fup_status(v):
    if v.deleted_at: return None
    try:
        from .fair_usage import status
        return status(v)
    except Exception:
        return None


@login_required
def voucher_detail(request,pk):
    """Everything about one voucher: details, devices, sale, router state and full history."""
    from . import voucher_history as vh
    from .shared_use import case_for
    from .voucher_freeze import MANUAL_WARNING
    from .models import SessionIncident, VoucherSale
    business=b(request)
    v=get_object_or_404(Voucher.all_objects.filter(business=business).select_related('router','batch','agent','deleted_by','frozen_by'),pk=pk)
    now=timezone.now();end=vh.ends_at(v);state_key,state_label=vh.display_state(v,now)
    left=(end-now) if end and end>now else None
    mirror=RouterHotspotUser.objects.filter(router=v.router,username=v.code).first() if v.router_id else None
    return render(request,'core/voucher_detail.html',{
        'v':v,'state_key':state_key,'state_label':state_label,'ends_at':end,'time_left':left,'time_is_up':vh.time_is_up(v,now),
        'timeline':vh.timeline(v,now),'bindings':v.device_bindings.order_by('slot_no'),
        'profile_now':_vprofile(v,business.plans.filter(name=v.plan_name).first())[0],
        'profile_router':_profile_state(v),
        'router_profiles':list(v.router.hotspot_profiles.filter(is_present=True).order_by('name').values('name','shared_users','rate_limit')) if v.router_id else [],
        'profile_plans':business.plans.filter(active=True).order_by('name'),
        'free_slots':max(0,max(1,v.max_devices or 1)-v.device_bindings.count()),
        'empty_slots':[i for i in range(1,max(1,v.max_devices or 1)+1) if i not in set(v.device_bindings.values_list('slot_no',flat=True))][:10],
        'sale':VoucherSale.objects.filter(voucher=v).select_related('agent','recorded_by').first(),
        'incidents':SessionIncident.objects.filter(voucher=v).select_related('router').order_by('-first_seen')[:20],
        'mirror':mirror,'channel':vh.channel(v.router),'max_extend':vh.MAX_EXTEND_HOURS,
        'extend_choices':[('30 min',0,0,30),('1 hour',0,1,0),('3 hours',0,3,0),('12 hours',0,12,0),('1 day',1,0,0),('3 days',3,0,0),('1 week',7,0,0),('30 days',30,0,0)],
        'old_codes':v.code_aliases.select_related('changed_by') if v.pk else [],
        'shared_case':case_for(v) if not v.deleted_at else None,
        'fup':_fup_status(v),
        'manual_warning':MANUAL_WARNING,
    })


@login_required
def delete_expired(request):
    from .models import VoucherEvent
    business=b(request);qs=business.vouchers.filter(Q(status='expired')|Q(expires_at__lt=timezone.now())).filter(frozen_at__isnull=True)
    user=request.user if request.user.is_authenticated else None
    VoucherEvent.objects.bulk_create([VoucherEvent(business=business,voucher_id=v.pk,voucher_code=v.code,event='deleted',user=user,
        status_before=v.status,status_after='deleted',reason='Delete expired vouchers',detail={'plan':v.plan_name,'price':str(v.price)})
        for v in qs.only('pk','code','status','plan_name','price')],batch_size=500)
    n=qs.count();qs.delete();messages.success(request,f'{n} expired voucher(s) deleted. Their history is kept.');return redirect('vouchers')


@login_required
def batches(request):
    batch_list=list(b(request).batches.select_related('plan','agent').annotate(actual=Count('vouchers',filter=Q(vouchers__deleted_at__isnull=True)),left=Count('vouchers',filter=Q(vouchers__sold_at__isnull=True,vouchers__used_at__isnull=True,vouchers__status='active',vouchers__deleted_at__isnull=True)),
        sold=Count('vouchers',filter=Q(vouchers__sold_at__isnull=False,vouchers__deleted_at__isnull=True)),used=Count('vouchers',filter=Q(vouchers__used_at__isnull=False,vouchers__deleted_at__isnull=True)),
        unused=Count('vouchers',filter=Q(vouchers__used_at__isnull=True,vouchers__deleted_at__isnull=True)),
        sold_unused=Count('vouchers',filter=Q(vouchers__sold_at__isnull=False,vouchers__used_at__isnull=True,vouchers__deleted_at__isnull=True)),
        frozen=Count('vouchers',filter=Q(vouchers__frozen_at__isnull=False,vouchers__deleted_at__isnull=True)),
        freezable=Count('vouchers',filter=Q(vouchers__frozen_at__isnull=True,vouchers__status='active',vouchers__deleted_at__isnull=True))).order_by('-created_at'))
    open_reports={}
    for r in b(request).missing_voucher_reports.filter(batch__isnull=False).exclude(status='resolved').order_by('-reported_at'):
        open_reports.setdefault(r.batch_id,r)
    for x in batch_list:
        x.missing_report=open_reports.get(x.id)
        x.missing_count=x.missing_report.voucher_count if x.missing_report else 0
    return render(request,'core/batches.html',{'agents':b(request).agents.filter(active=True),'methods':[m for m in PAYMENT_METHODS if m[0]!='auto'],
        'batches':batch_list,
        'designs':b(request).voucher_designs.all()})


@login_required
def plans(request):
    business=b(request);form=PlanForm(request.POST or None,instance=VoucherPlan(business=business))
    if request.method=='POST' and form.is_valid():
        obj=form.save(commit=False);obj.business=business;obj.source='taptap';obj.price_source='manual';obj.save();messages.success(request,'Plan saved.')
        from .portal_deploy import schedule_redeploy; schedule_redeploy(business)
        return redirect('plans')
    plan_list=list(business.plans.select_related('imported_from_router').all().order_by('price','name'))
    zero=business.vouchers.filter(price=0,sold_at__isnull=True).values('plan_name').annotate(n=Count('id'))
    zero_map={r['plan_name']:r['n'] for r in zero}
    usage={r['plan_name']:r for r in business.vouchers.values('plan_name').annotate(total=Count('id'),used=Count('id',filter=Q(used_at__isnull=False)),
        unused=Count('id',filter=Q(used_at__isnull=True)),sold_unused=Count('id',filter=Q(used_at__isnull=True,sold_at__isnull=False)),
        live=Count('id',filter=Q(status__in=['active','disabled'])),live_used=Count('id',filter=Q(status__in=['active','disabled'],used_at__isnull=False)))}
    perms=getattr(request,'tt_perms',frozenset())
    for p in plan_list:
        p.zero_vouchers=zero_map.get(p.name,0); u=usage.get(p.name,{})
        p.v_total,p.v_used,p.v_unused,p.v_sold_unused=u.get('total',0),u.get('used',0),u.get('unused',0),u.get('sold_unused',0)
        p.v_live,p.v_live_used=u.get('live',0),u.get('live_used',0)
        p.can_delete=not p.v_live and (not p.v_used or 'plans.delete_used' in perms)   # only an empty plan
    from .orphan_profiles import groups_with_traces as orphan_groups_t
    return render(request,'core/plans.html',{'plans':plan_list,'form':form,'missing':[p for p in plan_list if not p.price and not p.is_free],
        'orphans':orphan_groups_t(business)})


@login_required
def plan_update(request,pk):
    """Edit a plan in place. A price typed here is 'manual' and survives future router syncs."""
    from decimal import Decimal, InvalidOperation
    business=b(request);plan=get_object_or_404(business.plans,pk=pk)
    if request.method!='POST': return redirect('plans')
    old_price,old_free,old_minutes=plan.price,plan.is_free,plan.duration_minutes
    free=request.POST.get('is_free')=='1'
    try: price=Decimal('0') if free else max(Decimal('0'),Decimal(request.POST.get('price','0').replace(',','') or '0'))
    except (InvalidOperation,ValueError): messages.error(request,'Enter a valid price.');return redirect('plans')
    plan.price=price;plan.is_free=free
    if price!=old_price or free!=old_free: plan.price_source='manual'
    unit=request.POST.get('duration_unit') or plan.duration_unit
    if unit=='unlimited' or request.POST.get('duration_value','').strip():
        from .durations import to_minutes
        try:
            plan.duration_minutes=to_minutes(request.POST.get('duration_value'),unit);plan.duration_unit=unit
        except ValueError as e: messages.error(request,str(e));return redirect('plans')
    elif request.POST.get('duration_hours','').isdigit():
        plan.duration_minutes=max(1,int(request.POST['duration_hours']))*60;plan.duration_unit='hours'
    old_devices=plan.max_devices
    if request.POST.get('max_devices','').isdigit(): plan.max_devices=max(1,int(request.POST['max_devices']))
    plan.active=request.POST.get('active')=='1'
    plan.save()
    unsold=business.vouchers.filter(plan_name=plan.name,sold_at__isnull=True,used_at__isnull=True)
    fixed=business.vouchers.filter(plan_name=plan.name,price=0,sold_at__isnull=True).update(price=price) if price else 0
    moved=unsold.exclude(price=price).update(price=price) if request.POST.get('apply_unsold') and (price or free) else 0
    retimed=0
    if request.POST.get('apply_duration') and plan.duration_minutes!=old_minutes:
        qs=business.vouchers.filter(plan_name=plan.name,used_at__isnull=True,expires_at__isnull=True,frozen_at__isnull=True)
        ids=list(qs.values_list('pk',flat=True))
        retimed=qs.update(duration_minutes=plan.duration_minutes,mikrotik_sync_status='Pending')
        try:   # send just these vouchers to their routers (no full sync)
            from .voucher_push import push_vouchers; push_vouchers(list(business.vouchers.filter(pk__in=ids).select_related('router')),request.user)
        except Exception: pass
    if plan.max_devices!=old_devices:
        # every voucher of this plan allows the plan's devices (the router profile's shared-users follows)
        dqs=business.vouchers.filter(plan_name=plan.name,status='active',router_profile='').exclude(login_type='member')
        dids=list(dqs.values_list('pk',flat=True)); dqs.update(max_devices=plan.max_devices)
        try:
            from .voucher_push import push_vouchers; push_vouchers(list(business.vouchers.filter(pk__in=dids,source='taptap').select_related('router')),request.user)
        except Exception: pass
    msg=f'{plan.name} saved — {"free" if free else f"{business.currency}{price}"}, {plan.duration_text.lower() if plan.duration_minutes else "no time limit"}.'
    if fixed or moved: msg+=f' {fixed+moved} voucher price{"s" if fixed+moved!=1 else ""} updated.'
    if retimed: msg+=f' {retimed} unused voucher{"s" if retimed!=1 else ""} now {"unlimited" if not plan.duration_minutes else plan.duration_text} (being sent to the routers).'
    messages.success(request,msg)
    from .portal_deploy import schedule_redeploy; schedule_redeploy(plan.business)
    return redirect('plans')


@login_required
def routers(request):
    business=b(request);form=RouterForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        obj=form.save(commit=False);obj.business=business;obj.save();messages.success(request,'Router added.');return redirect('routers')
    router_rows=list(business.routers.all().order_by('name'))
    router_ids=[router.id for router in router_rows]

    def grouped_counts(queryset):
        return {row['router_id']: row['total'] for row in queryset.values('router_id').annotate(total=Count('id'))}

    hotspot_counts=grouped_counts(RouterHotspotUser.objects.filter(business=business,router_id__in=router_ids))
    binding_counts=grouped_counts(SyncedIPBinding.objects.filter(business=business,router_id__in=router_ids))
    neighbor_counts=grouped_counts(RouterNeighbor.objects.filter(router_id__in=router_ids,is_online=True))
    device_counts=grouped_counts(RouterDevice.objects.filter(router_id__in=router_ids,is_online=True))

    for router in router_rows:
        router.hotspot_user_count=hotspot_counts.get(router.id,0)
        router.binding_count=binding_counts.get(router.id,0)
        router.online_neighbor_count=neighbor_counts.get(router.id,0)
        router.device_count=device_counts.get(router.id,0)

    latest_jobs={}
    recent_jobs=[]
    sync_schema_ready=True
    try:
        for router in router_rows:
            job=business.router_sync_jobs.filter(router_id=router.id).order_by('-created_at').first()
            if job:
                latest_jobs[router.id]=job
        recent_jobs=list(business.router_sync_jobs.select_related('router').order_by('-created_at')[:12])
    except DatabaseError:
        sync_schema_ready=False

    for router in router_rows:
        router.latest_sync_job=latest_jobs.get(router.id)
    return render(request,'core/routers.html',{
        'routers':router_rows,'form':form,'recent_sync_jobs':recent_jobs,
        'sync_schema_ready':sync_schema_ready,
    })


@login_required
def router_sync(request,pk):
    r=get_object_or_404(b(request).routers,pk=pk)
    if request.method!='POST': return redirect('routers')
    try:
        job,created=enqueue_router_sync(r,request.user)
        if created:
            messages.success(request,f'{r.name} synchronization is running in the background. You can continue using TapTap.')
        else:
            messages.info(request,f'{r.name} already has a synchronization job in progress ({job.progress}%).')
    except Exception as e:
        messages.error(request,f'Could not queue {r.name} synchronization: {e}')
    nxt=request.POST.get('next','')
    if nxt.startswith('/') and not nxt.startswith('//'): return redirect(nxt)
    return redirect('routers')


@login_required
def routers_sync_all(request):
    if request.method!='POST': return redirect('routers')
    business=b(request);queued=0;already=0;failed=[]
    for r in business.routers.all():
        try:
            _,created=enqueue_router_sync(r,request.user)
            if created: queued+=1
            else: already+=1
        except Exception as e:
            failed.append(f'{r.name}: {e}')
    if queued: messages.success(request,f'{queued} router synchronization job(s) queued in the background. You can leave this page while they run.')
    if already: messages.info(request,f'{already} router(s) already had a sync running, so duplicate jobs were not created.')
    if failed: messages.warning(request,'Could not queue: '+' | '.join(failed[:3]))
    return redirect('routers')


@login_required
def router_sync_status(request):
    business=b(request)
    try:
        jobs=business.router_sync_jobs.select_related('router').all()[:30]
        data=[]
        for job in jobs:
            summary=job.summary or {}
            data.append({
                'id':job.id,'router_id':job.router_id,'router_name':job.router.name,
                'status':job.status,'progress':job.progress,'phase':job.phase,
                'error':job.error,'summary':summary,
                'created_at':job.created_at.isoformat(),
                'started_at':job.started_at.isoformat() if job.started_at else None,
                'finished_at':job.finished_at.isoformat() if job.finished_at else None,
            })
        return JsonResponse({'success':True,'active_count':sum(1 for x in data if x['status'] in ('queued','running')),'jobs':data})
    except DatabaseError:
        return JsonResponse({
            'success':False,'active_count':0,'jobs':[],
            'migration_required':True,
            'message':'Background sync database migration is not applied yet. Run python manage.py migrate.'
        },status=503)


@login_required
def router_inventory(request):
    business=b(request)
    return render(request,'core/router_inventory.html',{
        'profiles':business.router_hotspot_profiles.select_related('router').order_by('router__name','name'),
        'hotspot_users':business.router_hotspot_users.select_related('router').order_by('router__name','username'),
        'synced_bindings':business.synced_ip_bindings.select_related('router').order_by('router__name','mac_address'),
        'devices':RouterDevice.objects.filter(router__business=business).select_related('router').order_by('-is_online','router__name','interface_name','hostname'),
    })


@login_required
def router_test(request,pk):
    r=get_object_or_404(b(request).routers,pk=pk)
    if r.connection_mode=='agent':
        from .linklive import link_state
        from . import agent as link
        online,why=link_state(r)
        if online:
            link.queue(r,'ping',label='Test connection',user=request.user)
            messages.success(request,f'{r.name} is connected through TapTap Link (last check-in {timezone.localtime(r.agent.last_seen_at):%H:%M:%S}). A test command was sent; see it on the TapTap Link page.')
        else:
            messages.error(request,why)
        return redirect('routers')
    try: svc=MikroTikService(r).connect();svc.test();svc.close();r.status='Online';r.last_error='';messages.success(request,f'{r.name} connected successfully. RouterOS resource data received.')
    except Exception as e: r.status='Offline';r.last_error=str(e);messages.error(request,f'Connection failed: {e}')
    r.last_tested_at=timezone.now();r.save(update_fields=['status','last_error','last_tested_at']);return redirect('routers')


@login_required
def router_delete(request,pk):
    get_object_or_404(b(request).routers,pk=pk).delete();messages.success(request,'Router deleted.');return redirect('routers')


@login_required
def router_control(request,pk):
    router=get_object_or_404(b(request).routers,pk=pk)
    snapshot=RouterConfigSnapshot.objects.filter(router=router).first()
    if not snapshot and _on_link(router):
        from .linkops import refresh
        try:
            refresh(router,request.user);messages.info(request,f'{router.name} is on TapTap Link: its configuration is being collected through the Link. Reload in a minute.')
        except ValueError as e: messages.warning(request,str(e))
    elif not snapshot:
        try:
            svc=MikroTikService(router).connect();cfg=svc.configuration_snapshot();svc.close()
            snapshot=RouterConfigSnapshot.objects.create(router=router,sections=cfg['sections'],load_balancing=cfg['load_balancing'],captured_at=cfg['captured_at'])
        except Exception as e: messages.warning(request,f'Live configuration could not be loaded yet: {e}')
    interfaces=list(router.interfaces.filter(is_present=True).order_by('name'))
    role_map={x.interface_name:x for x in router.interface_roles.all()}
    for iface in interfaces:
        role=role_map.get(iface.name);iface.ui_role=role.role if role else 'unused';iface.ui_label=role.label if role else ''
    bridges=[]
    if snapshot and snapshot.sections.get('Bridges'): bridges=snapshot.sections['Bridges'].get('rows',[])
    changes=router.config_changes.select_related('actor').order_by('-created_at')[:20]
    sections=[(label,sec) for label,sec in (snapshot.sections.items() if snapshot and snapshot.sections else []) if not str(label).startswith('_')]
    return render(request,'core/router_control.html',{'router':router,'snapshot':snapshot,'sections':sections,'interfaces':interfaces,'bridges':bridges,'changes':changes,'role_choices':RouterInterfaceRole.ROLES,'catalog':MikroTikService.CONFIG_CATALOG,'lb_config':_lb_config(router,snapshot)})


@login_required
def router_config_refresh(request,pk):
    router=get_object_or_404(b(request).routers,pk=pk)
    if request.method!='POST': return redirect('router_control',pk=pk)
    if _on_link(router):
        from .linkops import refresh
        try:
            created=refresh(router,request.user)
            messages.success(request,'Collecting the configuration through TapTap Link — sections update as the router sends them.' if created else 'A TapTap Link sync is already running.')
        except ValueError as e: messages.error(request,str(e))
        return redirect('router_control',pk=pk)
    try:
        svc=MikroTikService(router).connect();cfg=svc.configuration_snapshot();svc.close()
        RouterConfigSnapshot.objects.update_or_create(router=router,defaults={'sections':cfg['sections'],'load_balancing':cfg['load_balancing'],'captured_at':cfg['captured_at']})
        messages.success(request,f'{router.name} configuration snapshot refreshed ({len(cfg["sections"])} sections).')
    except Exception as e: messages.error(request,f'Could not refresh configuration: {e}')
    return redirect('router_control',pk=pk)


@login_required
def router_resource_api(request,pk):
    router=get_object_or_404(b(request).routers,pk=pk);path=request.GET.get('path','/interface')
    if _on_link(router):
        from .linkops import snapshot_rows
        rows,label,at=snapshot_rows(router,path)
        if rows is None:
            return JsonResponse({'success':False,'message':f'{path} is not part of the TapTap Link sync. The explorer shows the menus collected at the last sync.'},status=400)
        return JsonResponse({'success':True,'path':path,'count':len(rows),'rows':rows,'source':f'TapTap Link sync {timezone.localtime(at):%d %b %H:%M}' if at else 'TapTap Link'})
    try:
        svc=MikroTikService(router).connect();rows=svc.browse_resource(path);svc.close();return JsonResponse({'success':True,'path':path,'count':len(rows),'rows':rows})
    except Exception as e: return JsonResponse({'success':False,'message':str(e)},status=400)


@login_required
def router_config_apply(request,pk):
    router=get_object_or_404(b(request).routers,pk=pk)
    if request.method!='POST': return redirect('router_control',pk=pk)
    path=request.POST.get('resource_path','').strip();operation=request.POST.get('operation','').strip();target=request.POST.get('target_id','').strip();fields_raw=request.POST.get('fields_json','{}').strip() or '{}'
    try: fields=json.loads(fields_raw)
    except Exception:
        messages.error(request,'Fields must be valid JSON.');return redirect('router_control',pk=pk)
    if request.POST.get('confirm')!='APPLY':
        messages.error(request,'Advanced RouterOS changes require the APPLY confirmation.');return redirect('router_control',pk=pk)
    if _on_link(router):
        messages.error(request,f'{router.name} is on TapTap Link. Raw RouterOS changes are not sent over the Link; use the built-in actions, or enable custom scripts on the TapTap Link page and run the change there.')
        return redirect('router_control',pk=pk)
    change=RouterConfigChange.objects.create(business=router.business,router=router,actor=request.user,resource_path=path,operation=operation,target_id=target,fields=redact(fields),status='success')
    try:
        svc=MikroTikService(router).connect();svc.apply_change(path,operation,target,fields);cfg=svc.configuration_snapshot();svc.close()
        RouterConfigSnapshot.objects.update_or_create(router=router,defaults={'sections':cfg['sections'],'load_balancing':cfg['load_balancing'],'captured_at':cfg['captured_at']})
        log(router.business,'Advanced Router Config',f'{router.name}: {operation} {path}')
        messages.success(request,f'RouterOS change applied: {operation.upper()} {path}.')
    except Exception as e:
        change.status='failed';change.error=str(e);change.save(update_fields=['status','error']);messages.error(request,f'RouterOS change failed: {e}')
    return redirect('router_control',pk=pk)


@login_required
def router_interface_role(request,pk):
    router=get_object_or_404(b(request).routers,pk=pk)
    if request.method!='POST': return redirect('router_control',pk=pk)
    name=request.POST.get('interface_name','').strip();role=request.POST.get('role','unused').strip();label=request.POST.get('label','').strip();valid={x[0] for x in RouterInterfaceRole.ROLES}
    if role not in valid or not router.interfaces.filter(name=name).exists():
        messages.error(request,'Invalid interface or role.');return redirect('router_control',pk=pk)
    RouterInterfaceRole.objects.update_or_create(router=router,interface_name=name,defaults={'role':role,'label':label})
    if request.POST.get('apply')=='yes' and _on_link(router):
        from .linkops import send, QUEUED
        try:
            send(router,'interface_set',{'name':name,'enabled':role!='disabled'},label=f'{name}: {"disable" if role=="disabled" else "enable"}',user=request.user)
            bridge=request.POST.get('bridge_name','').strip()
            if role in {'lan','hotspot','trunk','management'} and bridge: send(router,'bridge_port',{'name':name,'bridge':bridge},label=f'Add {name} to {bridge}',user=request.user)
            if role=='wan' and request.POST.get('remove_from_bridge')=='yes': send(router,'bridge_port',{'name':name},label=f'Remove {name} from its bridge',user=request.user)
            messages.success(request,f'{name} assigned to {dict(RouterInterfaceRole.ROLES).get(role)}. {QUEUED}')
        except ValueError as e: messages.warning(request,f'Role saved in TapTap; not sent to the router: {e}')
    elif request.POST.get('apply')=='yes':
        try:
            svc=MikroTikService(router).connect();svc.set_interface_disabled(name,role=='disabled')
            bridge=request.POST.get('bridge_name','').strip()
            if role in {'lan','hotspot','trunk','management'} and bridge: svc.ensure_bridge_port(name,bridge)
            if role=='wan' and request.POST.get('remove_from_bridge')=='yes': svc.remove_bridge_port(name)
            svc.close();messages.success(request,f'{name} assigned to {dict(RouterInterfaceRole.ROLES).get(role)} and applied to RouterOS.')
        except Exception as e: messages.warning(request,f'Role saved in TapTap, but RouterOS apply failed: {e}')
    else: messages.success(request,f'{name} visual role updated.')
    return redirect('router_control',pk=pk)


@login_required
def router_quick_recipe(request,pk):
    router=get_object_or_404(b(request).routers,pk=pk)
    if request.method!='POST': return redirect('router_control',pk=pk)
    recipe=request.POST.get('recipe');iface=request.POST.get('interface_name','').strip()
    if not router.interfaces.filter(name=iface).exists(): messages.error(request,'Interface was not found.');return redirect('router_control',pk=pk)
    if _on_link(router):
        from .linkops import send, QUEUED
        try:
            if recipe=='wan_dhcp_nat':
                send(router,'wan_dhcp_nat',{'name':iface},label=f'{iface}: DHCP WAN with NAT',user=request.user);RouterInterfaceRole.objects.update_or_create(router=router,interface_name=iface,defaults={'role':'wan'})
            elif recipe in ('enable','disable'):
                send(router,'interface_set',{'name':iface,'enabled':recipe=='enable'},label=f'{recipe.capitalize()} {iface}',user=request.user)
                if recipe=='disable': RouterInterfaceRole.objects.update_or_create(router=router,interface_name=iface,defaults={'role':'disabled'})
            else: raise ValueError('Unknown quick configuration recipe.')
            RouterConfigChange.objects.create(business=router.business,router=router,actor=request.user,resource_path='/visual-studio',operation=recipe,target_id=iface,fields={'via':'TapTap Link'},status='success')
            messages.success(request,QUEUED)
        except ValueError as e: messages.error(request,f'Quick configuration not sent: {e}')
        return redirect('router_control',pk=pk)
    try:
        svc=MikroTikService(router).connect()
        if recipe=='wan_dhcp_nat':
            result=svc.configure_wan_dhcp_nat(iface);RouterInterfaceRole.objects.update_or_create(router=router,interface_name=iface,defaults={'role':'wan'});messages.success(request,f'{iface} configured as a DHCP WAN with masquerade NAT ({result["dhcp_client"]} DHCP / {result["nat"]} NAT).')
        elif recipe=='enable': svc.set_interface_disabled(iface,False);messages.success(request,f'{iface} enabled.')
        elif recipe=='disable': svc.set_interface_disabled(iface,True);RouterInterfaceRole.objects.update_or_create(router=router,interface_name=iface,defaults={'role':'disabled'});messages.success(request,f'{iface} disabled.')
        else: raise MikroTikError('Unknown quick configuration recipe.')
        cfg=svc.configuration_snapshot();svc.close();RouterConfigSnapshot.objects.update_or_create(router=router,defaults={'sections':cfg['sections'],'load_balancing':cfg['load_balancing'],'captured_at':cfg['captured_at']})
        RouterConfigChange.objects.create(business=router.business,router=router,actor=request.user,resource_path='/visual-studio',operation=recipe,target_id=iface,fields={},status='success')
    except Exception as e: messages.error(request,f'Quick configuration failed: {e}')
    return redirect('router_control',pk=pk)


def _safe_cache_get(key):
    try: return cache.get(key)
    except Exception: return None


def _safe_cache_set(key,value,ttl):
    try: cache.set(key,value,ttl)
    except Exception: pass


def _lb_config(router,snapshot=None):
    """Everything the load-balancing engine needs to draw instantly, before the first poll."""
    if snapshot is None:
        snapshot=RouterConfigSnapshot.objects.filter(router=router).first()
    lb=(snapshot.load_balancing if snapshot else None) or {}
    from django.urls import reverse
    return {'router_id':router.id,'router_name':router.name,'router_ip':router.ip_address or ('TapTap Link' if router.connection_mode=='agent' else ''),'status':router.status,
            'dom_id':f'lb-cfg-{router.id}','telemetry_url':reverse('router_telemetry',args=[router.id]),'load_balancing':lb}


def _lb_signature(lb):
    return json.dumps([lb.get('method')]+[(l.get('id'),l.get('state'),l.get('expected_share')) for l in lb.get('wan_links',[])],sort_keys=True)


@login_required
def router_telemetry(request,pk):
    """Live WAN telemetry. Cached briefly so many open tabs cost one RouterOS read."""
    router=get_object_or_404(b(request).routers,pk=pk)
    key=f'tt:lb:{router.id}'
    cached=_safe_cache_get(key)
    if cached:
        return JsonResponse(cached)
    snapshot=RouterConfigSnapshot.objects.filter(router=router).first()
    if _on_link(router):
        from .linklive import link_state, telemetry as link_telemetry
        online,why=link_state(router)
        payload={'success':True,**link_telemetry(router)} if online else {'success':False,'message':why}
        _safe_cache_set(key,payload,settings.LIVE_CACHE_SECONDS)
        return JsonResponse(payload)
    try:
        with MikroTikService(router,timeout=settings.MIKROTIK_LIVE_TIMEOUT) as svc:
            data=svc.telemetry(snapshot.sections if snapshot else None)
        lb=data['load_balancing']
        if snapshot is None:
            RouterConfigSnapshot.objects.create(router=router,sections={},load_balancing=lb)
        elif _lb_signature(snapshot.load_balancing or {})!=_lb_signature(lb):
            snapshot.load_balancing=lb;snapshot.save(update_fields=['load_balancing','updated_at'])
        if router.status!='Online':
            Router.objects.filter(pk=router.pk).update(status='Online',last_error='',last_tested_at=timezone.now())
        payload={'success':True,**data}
        _safe_cache_set(key,payload,settings.LIVE_CACHE_SECONDS)
        return JsonResponse(payload)
    except Exception as e:
        payload={'success':False,'message':str(e)}
        _safe_cache_set(key,payload,max(5,settings.LIVE_CACHE_SECONDS))
        return JsonResponse(payload)


def _router_rows(business,method):
    rows=[];errors=[]
    for r in business.routers.all():
        if _on_link(r):
            from .linklive import link_state, sessions
            online,why=link_state(r)
            if method=='active_users' and online:
                for x in sessions(r):
                    rows.append({'id':x.get('id',''),'user':x.get('user',''),'mac_address':x.get('mac-address',''),'address':x.get('address',''),
                                 'uptime':x.get('uptime',''),'bytes_in':x.get('bytes-in',''),'bytes_out':x.get('bytes-out',''),'router_name':r.name,'router_id':r.id,'via_link':True})
            elif not online:
                errors.append(why)
            continue
        try:
            svc=MikroTikService(r).connect();data=getattr(svc,method)();svc.close()
            for x in data:
                clean={str(k).replace('-','_'):v for k,v in dict(x).items()};clean.update(router_name=r.name,router_id=r.id);rows.append(clean)
        except Exception as e: errors.append(f'{r.name}: {e}')
    return rows,errors


@login_required
def active_users(request):
    from .voucher_freeze import MANUAL_WARNING
    rows,errors=_router_rows(b(request),'active_users')
    try:
        from .models_fup import FairUsageState
        codes=[str(x.get('user','')) for x in rows if x.get('user')]
        recent=timezone.now()-timedelta(minutes=30)
        slowed={s.voucher.code.upper():s for s in FairUsageState.objects.filter(voucher__business=b(request),voucher__code__in=codes,tier__gt=0,updated_at__gte=recent)
                .select_related('voucher','policy')}
        for x in rows:
            s=slowed.get(str(x.get('user','')).upper())
            if s and s.tier<=len(s.policy.tiers):
                x['fup_speed']=s.policy.tiers[s.tier-1]['down']; x['fup_gb']=round(s.used_bytes/1024**3,2)
    except Exception:
        pass
    return render(request,'core/active_users.html',{'rows':rows,'errors':errors,'manual_warning':MANUAL_WARNING})


@login_required
def disconnect_user(request):
    r=get_object_or_404(b(request).routers,pk=request.POST.get('router_id'))
    if _on_link(r):
        from .linkops import send, session_user, QUEUED
        user=request.POST.get('user') or session_user(r,request.POST.get('item_id'))
        try:
            if not user: raise ValueError('That session has already ended.')
            send(r,'disconnect',{'user':user},label=f'Disconnect {user}',user=request.user);messages.success(request,f'{user}: {QUEUED}')
        except ValueError as e: messages.error(request,str(e))
        return redirect('active_users')
    try: svc=MikroTikService(r).connect();svc.disconnect(request.POST.get('item_id'));svc.close();messages.success(request,'User disconnected.')
    except Exception as e: messages.error(request,str(e))
    return redirect('active_users')


@login_required
def ip_bindings(request):
    business=b(request)
    if request.method=='POST':
        r=get_object_or_404(business.routers,pk=request.POST.get('router_id'))
        binding=SyncedIPBinding.objects.create(business=business,router=r,mac_address=request.POST.get('mac_address','').strip().upper(),address=request.POST.get('address','').strip(),server=request.POST.get('server','all').strip() or 'all',binding_type=request.POST.get('type','bypassed'),comment=request.POST.get('comment','TapTap').strip() or 'TapTap',source='taptap',sync_status='Pending')
        try:
            svc=MikroTikService(r).connect();action,item_id=svc.upsert_binding(binding);svc.close();binding.mikrotik_id=str(item_id or '');binding.sync_status='Synced';binding.sync_error='';binding.save(update_fields=['mikrotik_id','sync_status','sync_error','updated_at']);messages.success(request,'IP binding saved in TapTap and synchronized to MikroTik.')
        except Exception as e:
            binding.sync_status='Error';binding.sync_error=str(e);binding.save(update_fields=['sync_status','sync_error','updated_at']);messages.warning(request,f'IP binding saved in TapTap but router sync failed: {e}')
        return redirect('ip_bindings')
    rows,errors=_router_rows(business,'bindings');return render(request,'core/ip_bindings.html',{'rows':rows,'errors':errors,'routers':business.routers.all(),'mirrored':business.synced_ip_bindings.select_related('router').order_by('router__name','mac_address')})


@login_required
def ip_binding_action(request):
    r=get_object_or_404(b(request).routers,pk=request.POST.get('router_id'));action=request.POST.get('action');item=request.POST.get('item_id')
    if action!='delete':
        messages.info(request,'Use the switch on the IP Binding page — a bypass needs its payment recorded before it goes on.')
        return redirect('ip_bindings')
    if _on_link(r):
        from .linkops import send, QUEUED
        bnd=SyncedIPBinding.objects.filter(router=r,mikrotik_id=item).first()
        try:
            if not bnd or not bnd.mac_address: raise ValueError('Binding not found (or it has no MAC address).')
            if action=='delete': send(r,'binding_remove',{'mac':bnd.mac_address},label=f'Delete binding {bnd.mac_address}',user=request.user);bnd.delete()
            else:
                send(r,'binding_set',{'mac':bnd.mac_address,'enabled':bnd.disabled},label=f'{"Enable" if bnd.disabled else "Disable"} binding {bnd.mac_address}',user=request.user)
                SyncedIPBinding.objects.filter(pk=bnd.pk).update(disabled=not bnd.disabled)
            messages.success(request,QUEUED)
        except ValueError as e: messages.error(request,str(e))
        return redirect('ip_bindings')
    try:
        svc=MikroTikService(r).connect()
        if action=='delete': svc.delete_binding(item);SyncedIPBinding.objects.filter(router=r,mikrotik_id=item).delete()
        else:
            current_disabled=str(request.POST.get('disabled','')).strip().lower() in {'true','yes','1','on'};new_disabled=not current_disabled;svc.toggle_binding(item,new_disabled);SyncedIPBinding.objects.filter(router=r,mikrotik_id=item).update(disabled=new_disabled,sync_status='Synced',sync_error='')
        svc.close();messages.success(request,'IP binding updated.')
    except Exception as e: messages.error(request,str(e))
    return redirect('ip_bindings')


@login_required
def topology(request):
    """Renders instantly from the database; the browser runs live discovery per router."""
    business=b(request);routers=list(business.routers.all().order_by('name'))
    snapshots=[snapshot_from_database(r) for r in routers]
    for snap in snapshots:
        snap['lb_config']=_lb_config(snap['router'])
        snap['wifi_clients']=[d for d in snap['devices'] if d.connection_type=='wifi']
        if snap['router'].status=='Online': snap['error']=''
    stale_after=timezone.now()-timedelta(seconds=settings.TOPOLOGY_STALE_SECONDS)
    auto=[r.id for r in routers if not r.last_tested_at or r.last_tested_at<stale_after]
    from django.urls import reverse
    graph=build_graph(business)
    netmap_config={'graph':graph,'graphUrl':reverse('topology_graph'),'liveUrl':reverse('topology_live'),
        'refreshUrl':reverse('topology_refresh',args=[0]),'controlUrl':reverse('router_control',args=[0]),
        'routers':[{'id':r.id,'name':r.name} for r in routers],'autoRefreshIds':auto}
    from .views_topology import detail_payload
    detail={**detail_payload(business),'urls':{'list':reverse('topology_routers'),'find':reverse('topology_router_find'),'action':reverse('topology_router_action'),'probe':reverse('topology_router_probe')}}
    return render(request,'core/topology.html',{'snapshots':snapshots,'routers':routers,'graph':graph,'netmap_config':netmap_config,'detail':detail})


@login_required
def topology_graph(request):
    return JsonResponse(build_graph(b(request),include_clients=request.GET.get('clients','1')!='0'))


@login_required
@require_POST
def topology_refresh(request,pk):
    """Live discovery for ONE router, bounded by MIKROTIK_TIMEOUT. The page calls these in parallel."""
    router=get_object_or_404(b(request).routers,pk=pk)
    started=timezone.now()
    if _on_link(router):
        from .linklive import link_state
        from .tasks import enqueue_router_sync
        online,why=link_state(router)
        if not online:
            return JsonResponse({'success':False,'router_id':router.id,'name':router.name,'message':why})
        job,created=enqueue_router_sync(router,request.user)
        return JsonResponse({'success':True,'router_id':router.id,'name':router.name,'via_link':True,
            'devices':router.devices.filter(is_online=True).count(),'neighbors':router.neighbors.filter(is_online=True).count(),
            'ports':router.interfaces.count(),'lb_method':'','seconds':0,
            'message':'Refreshing through TapTap Link — the map updates as the router sends its tables.' if created else 'A TapTap Link sync is already running.'})
    try:
        snap=refresh_router_topology(router)
        return JsonResponse({'success':True,'router_id':router.id,'name':router.name,
            'devices':len(snap.get('devices',[])),'neighbors':len([n for n in snap.get('neighbors',[]) if n.is_online]),
            'ports':len(snap.get('ports',[])),'lb_method':(snap.get('load_balancing') or {}).get('method',''),
            'seconds':round((timezone.now()-started).total_seconds(),1)})
    except Exception as e:
        logger.warning('Topology refresh failed for %s: %s',router.name,e)
        Router.objects.filter(pk=router.pk).exclude(connection_mode='agent').update(status='Offline',last_error=str(e)[:2000],last_tested_at=timezone.now())
        return JsonResponse({'success':False,'router_id':router.id,'name':router.name,'message':str(e)},status=200)


IFACE_NAME_RE=re.compile(r'^[\w.@<>/:+-]{1,64}$')


def _live_for_router(router,names):
    key='tt:live:%s:%s'%(router.id,hashlib.md5(','.join(names).encode()).hexdigest()[:10])
    cached=_safe_cache_get(key)
    if cached is not None:
        return cached
    if _on_link(router):
        from .linklive import link_state, rates
        online,why=link_state(router)
        return {'ok':online,'interfaces':rates(router,set(names)) if online else {},'error':why}
    try:
        with MikroTikService(router,timeout=settings.MIKROTIK_LIVE_TIMEOUT) as svc:
            result={'ok':True,'interfaces':svc.live_traffic(names)}
    except Exception as e:
        result={'ok':False,'error':str(e)[:300],'interfaces':{}}
    _safe_cache_set(key,result,settings.LIVE_CACHE_SECONDS)
    return result


@login_required
def topology_live(request):
    """Live bps for the interfaces drawn on the map. ?r=<router_id>:<iface,iface>&r=..."""
    business=b(request);wanted={}
    for item in request.GET.getlist('r')[:25]:
        rid,_,names=item.partition(':')
        if not rid.isdigit(): continue
        clean=[n for n in names.split(',') if IFACE_NAME_RE.match(n)][:32]
        if clean: wanted[int(rid)]=clean
    routers={r.id:r for r in business.routers.filter(id__in=list(wanted.keys()))}
    out={}
    if routers:
        with ThreadPoolExecutor(max_workers=min(8,len(routers))) as pool:
            futures={pool.submit(_live_for_router,r,wanted[rid]):rid for rid,r in routers.items()}
            for fut in as_completed(futures):
                try: out[str(futures[fut])]=fut.result()
                except Exception as e: out[str(futures[fut])]={'ok':False,'error':str(e),'interfaces':{}}
    return JsonResponse({'success':True,'routers':out,'ts':timezone.now().isoformat()})


@login_required
def security(request):
    business=b(request)
    findings=audit_business(business)
    acks={a.finding_key:a for a in business.security_acks.select_related('acknowledged_by')}
    summary=summarize(findings,set(acks))
    for f in summary['acked']:
        f['ack']=acks.get(f['key'])
    routers=list(business.routers.all().order_by('name'))
    for r in routers:
        pr=summary['per_router'].get(r.id,{'score':100,'grade':'A','counts':{}})
        r.sec_score=pr['score'];r.sec_grade=pr['grade'];r.sec_counts=pr['counts']
        try: r.sec_scanned=r.config_snapshot.captured_at if r.config_snapshot.sections.get('IP services') else None
        except Exception: r.sec_scanned=None
    incidents=list(business.session_incidents.select_related('router','voucher').filter(status__in=['open','ignored']).order_by('status','fix_due_at'))
    recent_fixed=list(business.session_incidents.select_related('router').filter(status__in=['fixed','ended']).order_by('-fixed_at')[:12])
    from .views_fup import security_context
    from .protection import status as protection_status
    protection=[(r,protection_status(r)) for r in routers]
    from .models import VoucherDeviceBinding
    sticky={'locked':VoucherDeviceBinding.objects.filter(business=business,voucher__status='active').count(),
            'vouchers':VoucherDeviceBinding.objects.filter(business=business,voucher__status='active').values('voucher').distinct().count(),
            'keepalive_choices':business._meta.get_field('sticky_keepalive').choices}
    return render(request,'core/security.html',{'sticky':sticky,'incidents':incidents,'recent_fixed':recent_fixed,'summary':summary,'routers':routers,'protection':protection,
        'fix_labels':{k:v[3] for k,v in MikroTikService.SECURITY_FIXES.items()},**security_context(business)})


@login_required
@require_POST
def security_rescan(request,pk):
    router=get_object_or_404(b(request).routers,pk=pk)
    if _on_link(router):
        from .linkops import refresh
        try:
            refresh(router,request.user)
            return JsonResponse({'success':True,'router_id':router.id,'sections':0,'message':'Collecting the configuration through TapTap Link; results update in a minute.'})
        except ValueError as e: return JsonResponse({'success':False,'router_id':router.id,'message':str(e)})
    try:
        with MikroTikService(router) as svc:
            cfg=svc.configuration_snapshot()
        existing=RouterConfigSnapshot.objects.filter(router=router).first()
        sections=cfg['sections']
        if existing and existing.sections.get('_identity'):
            sections['_identity']=existing.sections['_identity']
        RouterConfigSnapshot.objects.update_or_create(router=router,defaults={'sections':sections,'load_balancing':cfg['load_balancing'],'captured_at':cfg['captured_at']})
        Router.objects.filter(pk=router.pk).update(status='Online',last_error='',last_tested_at=timezone.now())
        return JsonResponse({'success':True,'router_id':router.id,'sections':len(sections)})
    except Exception as e:
        Router.objects.filter(pk=router.pk).exclude(connection_mode='agent').update(status='Offline',last_error=str(e)[:2000],last_tested_at=timezone.now())
        return JsonResponse({'success':False,'router_id':router.id,'name':router.name,'message':str(e)})


@login_required
@require_POST
def security_fix(request,pk):
    router=get_object_or_404(b(request).routers,pk=pk)
    key=request.POST.get('fix','').strip()
    if key not in MikroTikService.SECURITY_FIXES:
        return JsonResponse({'success':False,'message':'Unknown fix.'},status=400)
    path,lookup,fields,label=MikroTikService.SECURITY_FIXES[key]
    if _on_link(router):
        from .linkops import send, refresh, QUEUED
        try:
            send(router,'security_fix',{'key':key},label=label,user=request.user)
            RouterConfigChange.objects.create(business=router.business,router=router,actor=request.user,resource_path=path,operation='security-fix',target_id=key,fields={**fields,'via':'TapTap Link'},status='success')
            log(router.business,'Security Fix',f'{router.name}: {label} (TapTap Link)')
            try: refresh(router,request.user)
            except ValueError: pass
            return JsonResponse({'success':True,'message':f'{label} — {QUEUED} The finding clears after the next sync.'})
        except ValueError as e: return JsonResponse({'success':False,'message':str(e)})
    change=RouterConfigChange.objects.create(business=router.business,router=router,actor=request.user,resource_path=path,
        operation='security-fix',target_id=key,fields=fields,status='success')
    try:
        with MikroTikService(router) as svc:
            svc.apply_security_fix(key)
            cfg=svc.configuration_snapshot()
        existing=RouterConfigSnapshot.objects.filter(router=router).first()
        if existing and existing.sections.get('_identity'):
            cfg['sections']['_identity']=existing.sections['_identity']
        RouterConfigSnapshot.objects.update_or_create(router=router,defaults={'sections':cfg['sections'],'load_balancing':cfg['load_balancing'],'captured_at':cfg['captured_at']})
        log(router.business,'Security Fix',f'{router.name}: {label}')
        return JsonResponse({'success':True,'message':f'{label} — applied on {router.name}.'})
    except Exception as e:
        change.status='failed';change.error=str(e);change.save(update_fields=['status','error'])
        return JsonResponse({'success':False,'message':str(e)})


@login_required
@require_POST
def security_ack(request):
    business=b(request);key=request.POST.get('finding_key','').strip()[:160]
    if not key:
        return JsonResponse({'success':False},status=400)
    if request.POST.get('action')=='unack':
        business.security_acks.filter(finding_key=key).delete()
    else:
        SecurityAck.objects.update_or_create(business=business,finding_key=key,defaults={'note':request.POST.get('note','').strip()[:255],'acknowledged_by':request.user})
    if request.headers.get('x-requested-with')=='fetch':
        return JsonResponse({'success':True})
    return redirect('security')


@login_required
def settings_view(request):
    business=b(request)
    if request.method=='POST':
        f=request.POST
        business.business_name=f.get('business_name',business.business_name).strip()[:180] or business.business_name
        business.owner_name=f.get('owner_name',business.owner_name).strip()[:180] or business.owner_name
        business.phone=f.get('phone',business.phone).strip()[:60]
        business.wifi_ssid=f.get('wifi_ssid','').strip()[:80]
        business.hotspot_url=f.get('hotspot_url','').strip()[:200]
        business.support_phone=f.get('support_phone','').strip()[:60]
        if 'hotspot_dns_name' in f:
            import re as _re
            n=f.get('hotspot_dns_name','').strip().lower()
            business.hotspot_dns_name=n if (not n or _re.match(r'^[a-z0-9-]+(\.[a-z0-9-]+)+$',n)) else business.hotspot_dns_name
        pending_email=None
        if 'business_email' in f:
            em=f.get('business_email','').strip().lower()
            if not em: business.email=''
            elif em!=(business.email or '').lower(): pending_email=em
        business.currency=(f.get('currency','D').strip() or 'D')[:8]
        color=f.get('brand_color','#1769e0').strip()
        if re.fullmatch(r'#[0-9a-fA-F]{6}',color): business.brand_color=color
        sticky_changed=False
        if f.get('devices_form'):
            new=(bool(f.get('device_lock')),bool(f.get('sticky_sessions')),f.get('sticky_keepalive') if f.get('sticky_keepalive') in ('none','30m','2h','12h') else '2h')
            sticky_changed=new[1:]!=(business.sticky_sessions,business.sticky_keepalive)
            business.device_lock,business.sticky_sessions,business.sticky_keepalive=new
        if 'serial_format' in f:
            try:
                business.serial_format,business.serial_digits,business.serial_reset=serials.clean(f.get('serial_format'),f.get('serial_digits'),f.get('serial_reset'))
                st=(f.get('serial_next') or '').strip()
                if st.isdigit() and int(st)>0 and business.serial_reset!='batch':
                    c=dict(business.serial_counters or {}); c[serials.period_key(business.serial_reset)]=int(st)-1; business.serial_counters=c
            except serials.SerialError as e:
                messages.error(request,str(e)); return redirect('settings')
        logo=f.get('logo_data','')
        if f.get('remove_logo'): business.logo_data=''
        elif logo.startswith('data:image/') and len(logo)<400_000: business.logo_data=logo
        business.save()
        messages.success(request,'Settings saved. Portal pages and voucher designs use the new details straight away.')
        if sticky_changed or f.get('sticky_apply'):
            from .sticky import apply_all
            for ok,msg in apply_all(business,request.user):
                (messages.info if ok else messages.warning)(request,'Sticky sessions — '+msg)
        from .portal_deploy import schedule_redeploy; schedule_redeploy(business)
        if pending_email:
            from .views_auth import start_email_change
            resp=start_email_change(request,pending_email,'/settings/')
            if resp: return resp
        return redirect('settings')
    return render(request,'core/settings.html',{'sn':serials.settings_ctx(business),'keepalive_choices':business._meta.get_field('sticky_keepalive').choices})
@login_required
def support(request):
    from .views_help import support as help_center
    return help_center(request)


@login_required
def api_subscription_warning(request,business_id):
    business=get_object_or_404(Business,pk=business_id,user=request.user);return JsonResponse({'success':True,'has_access':business.has_access,'status':business.subscription_status,'days_left':business.days_left,'expires_at':business.access_expires_at()})


@csrf_exempt
def api_voucher_login(request):
    if request.method!='POST': return JsonResponse({'success':False},status=405)
    try: data=json.loads(request.body or '{}')
    except Exception: data=request.POST
    code=str(data.get('voucher') or data.get('code') or '').strip().upper();v=Voucher.objects.filter(code=code,status='active').first()
    if not v: return JsonResponse({'success':False,'message':'Invalid or inactive voucher'},status=404)
    if v.expires_at and v.expires_at<=timezone.now(): return JsonResponse({'success':False,'message':'Voucher expired'},status=403)
    return JsonResponse({'success':True,'voucher':v.code,'plan':v.plan_name,'max_devices':v.max_devices,'duration_hours':v.duration_hours,'duration_minutes':v.duration_minutes,'duration':v.duration_text})


@login_required
@require_POST
def plan_fix_profile(request):
    """Plans → "Fix unknown profiles": give vouchers whose plan is a RouterOS ID (*1, *C…) a real plan."""
    from .orphan_profiles import delete as delete_orphan, fix, groups_with_traces, is_orphan
    business = b(request)
    action = request.POST.get('action', 'fix')
    bad = request.POST.get('profile', '')
    router_id = request.POST.get('router_id', '')
    if action != 'delete_all' and not is_orphan(bad):
        messages.error(request, 'That is not an unknown profile.'); return redirect('/plans/#fix')
    plan = None
    if request.POST.get('plan') == 'new':
        name = request.POST.get('new_name', '').strip()[:120]
        try:
            price = Decimal(request.POST.get('new_price') or '0')
            minutes = int(request.POST.get('new_minutes') or 0)
            devices = max(1, min(20, int(request.POST.get('new_devices') or 1)))
        except Exception:
            messages.error(request, 'Check the new plan’s price, time and devices.'); return redirect('/plans/#fix')
        if not name or price < 0 or minutes < 0:
            messages.error(request, 'Give the new plan a name, a price and a time.'); return redirect('/plans/#fix')
        if business.plans.filter(name__iexact=name).exists():
            plan = business.plans.get(name__iexact=name)
        else:
            plan = VoucherPlan.objects.create(business=business, name=name, price=price, duration_minutes=minutes, max_devices=devices,
                                              source='taptap', price_source='manual')
            from .portal_deploy import schedule_redeploy; schedule_redeploy(business)
    else:
        plan = business.plans.filter(pk=request.POST.get('plan') or 0).first()
    rid = int(router_id) if str(router_id).isdigit() and int(router_id) else None
    try:
        if action == 'delete_all':
            # Fix every unknown profile with one plan, then delete them all
            done, errors = 0, []
            for g in groups_with_traces(business):
                try:
                    delete_orphan(business, g['router_id'] or None, g['profile'], plan, request.user); done += 1
                except ValueError as exc:
                    errors.append(f'{g["profile"]}: {exc}')
            if done:
                messages.success(request, f'{done} unknown profile{"s" if done != 1 else ""} corrected and deleted' + (f' — vouchers moved to “{plan.name}”.' if plan else '.'))
            for e in errors:
                messages.error(request, e)
        elif action == 'delete':
            messages.success(request, delete_orphan(business, rid, bad, plan, request.user))
        else:
            if not plan:
                messages.error(request, 'Choose a plan, or create a new one.'); return redirect('/plans/#fix')
            messages.success(request, fix(business, rid, bad, plan, request.user))
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect('/plans/#fix')



@require_POST
def security_sticky(request):
    """Security › Sticky vouchers: turn device lock / sticky sessions on or off (and send it to the routers)."""
    business=b(request)
    lock=request.POST.get('device_lock')=='on'
    sessions=request.POST.get('sticky_sessions')=='on'
    ka=request.POST.get('sticky_keepalive') if request.POST.get('sticky_keepalive') in ('none','30m','2h','12h') else business.sticky_keepalive
    router_changed=(sessions,ka)!=(business.sticky_sessions,business.sticky_keepalive)
    before='lock '+('on' if business.device_lock else 'off')+', sessions '+('on' if business.sticky_sessions else 'off')
    business.device_lock,business.sticky_sessions,business.sticky_keepalive=lock,sessions,ka
    business.save(update_fields=['device_lock','sticky_sessions','sticky_keepalive'])
    log(business,'Sticky Vouchers',f'{before} → lock {"on" if lock else "off"}, sessions {"on" if sessions else "off"} (keep-alive {ka})')
    msg=f'Device lock {"on" if lock else "off"} · sticky sessions {"on" if sessions else "off"}.'
    if router_changed or request.POST.get('apply')=='1':
        from .sticky import apply_all
        res=apply_all(business,request.user)
        if res: msg+=' '+' · '.join(m for _,m in res)
    messages.success(request,msg)
    from django.urls import reverse
    return redirect(reverse('security')+'#sticky')
