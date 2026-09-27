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
from .utils import generate_code, duration_to_routeros, log

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
                for n,p,h,d in DEFAULT_PLANS: VoucherPlan.objects.create(business=business,name=n,price=p,duration_minutes=h*60,duration_unit=best_unit(h*60),max_devices=d)
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
    elif state=='disabled': qs=qs.exclude(status='active')
    if plan: qs=qs.filter(plan_name=plan)
    holder=request.GET.get('holder','')
    if holder=='shop': qs=qs.filter(agent__isnull=True)
    elif holder=='individual': qs=qs.filter(batch__isnull=True,source='taptap')
    elif holder.isdigit(): qs=qs.filter(agent_id=holder)
    if q: qs=qs.filter(Q(code__icontains=q)|Q(batch__name__icontains=q)|Q(customer_name__icontains=q)|Q(customer_phone__icontains=q))
    counts=business.vouchers.aggregate(all=Count('id'),unsold=Count('id',filter=Q(status='active',sold_at__isnull=True,used_at__isnull=True)),
        sold=Count('id',filter=Q(sold_at__isnull=False,used_at__isnull=True)),used=Count('id',filter=Q(used_at__isnull=False)),disabled=Count('id',filter=~Q(status='active')))
    params=request.GET.copy();params.pop('page',None)
    state_tabs=[('','All',counts['all']),('unsold','In stock',counts['unsold']),('sold','Sold, not used',counts['sold']),('used','Used',counts['used']),('disabled','Disabled / expired',counts['disabled'])]
    return render(request,'core/vouchers.html',{'page_obj':Paginator(qs,100).get_page(request.GET.get('page')),'counts':counts,'state_tabs':state_tabs,'state':state,'plan':plan,'q':q,
        'plans':business.vouchers.values_list('plan_name',flat=True).distinct().order_by('plan_name'),'agents':business.agents.filter(active=True),'all_agents':business.agents.all(),'holder':holder,
        'designs':business.voucher_designs.all(),'params':params.urlencode()})


@login_required
def generate_vouchers(request):
    business=b(request); plans=business.plans.filter(active=True); routers=business.routers.all()
    if request.method=='POST':
        plan=get_object_or_404(plans,pk=request.POST.get('plan')); qty=max(1,min(500,int(request.POST.get('quantity','1')))); router=routers.filter(pk=request.POST.get('router')).first(); batch_name=request.POST.get('batch_name','').strip() or f'{plan.name} {timezone.localtime():%Y-%m-%d %H:%M}'
        agent=business.agents.filter(pk=request.POST.get('owner') or 0).first()
        with transaction.atomic():
            batch=VoucherBatch.objects.create(business=business,name=batch_name,plan=plan,quantity=qty,note=request.POST.get('note','')[:255]); made=[]
            for _ in range(qty):
                made.append(Voucher.objects.create(business=business,batch=batch,router=router,code=generate_code(),plan_name=plan.name,price=plan.price,duration_minutes=plan.duration_minutes,max_devices=plan.max_devices,source='taptap'))
            log(business,'Voucher Generated',f'Batch {batch.name}: {qty} voucher(s)'+(f' for {agent.name}' if agent else ''))
            if agent:
                from .finance import assign_batch
                settlement=request.POST.get('settlement') if request.POST.get('settlement') in {'credit','prepaid'} else 'credit'
                _,sales=assign_batch(batch,agent,settlement,request.POST.get('pay_method','cash'),request.user,request.POST.get('reference','')[:120])
                messages.info(request,f'Batch issued to {agent.name}'+(f' — bought upfront for {business.currency}{sum(x.amount for x in sales):,.2f}.' if sales else ' on credit: each voucher is credited to them as it sells.'))
        if router:
            try:
                _,created=enqueue_router_sync(router,request.user)
                if created:
                    messages.info(request,f'{router.name} background sync was queued to publish the new vouchers to RouterOS.')
                else:
                    messages.info(request,f'{router.name} already has a background sync running; the new vouchers will be picked up by that sync or the next one.')
            except Exception as e:
                messages.warning(request,f'Vouchers were created in TapTap, but the router background sync could not be queued: {e}')
        messages.success(request,f'{qty} voucher(s) created successfully.')
        if request.POST.get('print_after'):
            design=request.POST.get('design','')
            return redirect(f"/studio/vouchers/print/?batch={batch.pk}"+(f"&design={design}" if design else ''))
        return redirect('vouchers')
    return render(request,'core/generate_vouchers.html',{'plans':plans,'routers':routers,'designs':business.voucher_designs.all(),
        'agents':business.agents.filter(active=True),'owner':request.GET.get('agent',''),'methods':[m for m in PAYMENT_METHODS if m[0]!='auto']})


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
    try: ok,result=vh.enable(v,request.user,request.POST.get('reason','').strip(),request.POST.get('add_hours'))
    except vh.VoucherActionError as e: messages.error(request,str(e)); return _voucher_back(request,v)
    log(b(request),'Voucher Enabled',v.code);_voucher_result(request,v,ok,result,'enabled')
    return _voucher_back(request,v)


@login_required
def reset_mac(request,pk):
    from . import voucher_history as vh
    v=get_object_or_404(b(request).vouchers,pk=pk)
    if request.method!='POST': return redirect('voucher_detail',pk=pk)
    ok,result=vh.reset_devices(v,request.user,request.POST.get('reason','').strip())
    log(b(request),'Voucher MAC Reset',v.code);_voucher_result(request,v,ok,result,'device binding reset')
    return _voucher_back(request,v)


@login_required
def voucher_detail(request,pk):
    """Everything about one voucher: details, devices, sale, router state and full history."""
    from . import voucher_history as vh
    from .models import SessionIncident, VoucherSale
    business=b(request)
    v=get_object_or_404(business.vouchers.select_related('router','batch','agent'),pk=pk)
    now=timezone.now();end=vh.ends_at(v);state_key,state_label=vh.display_state(v,now)
    left=(end-now) if end and end>now else None
    mirror=RouterHotspotUser.objects.filter(router=v.router,username=v.code).first() if v.router_id else None
    return render(request,'core/voucher_detail.html',{
        'v':v,'state_key':state_key,'state_label':state_label,'ends_at':end,'time_left':left,'time_is_up':vh.time_is_up(v,now),
        'timeline':vh.timeline(v,now),'bindings':v.device_bindings.order_by('slot_no'),
        'sale':VoucherSale.objects.filter(voucher=v).select_related('agent','recorded_by').first(),
        'incidents':SessionIncident.objects.filter(voucher=v).select_related('router').order_by('-first_seen')[:20],
        'mirror':mirror,'channel':vh.channel(v.router),'max_extend':vh.MAX_EXTEND_HOURS,
        'extend_choices':[(1,'1 hour'),(3,'3 hours'),(24,'1 day'),(72,'3 days'),(168,'1 week'),(720,'30 days')],
    })


@login_required
def delete_expired(request):
    from .models import VoucherEvent
    business=b(request);qs=business.vouchers.filter(Q(status='expired')|Q(expires_at__lt=timezone.now()))
    user=request.user if request.user.is_authenticated else None
    VoucherEvent.objects.bulk_create([VoucherEvent(business=business,voucher_id=v.pk,voucher_code=v.code,event='deleted',user=user,
        status_before=v.status,status_after='deleted',reason='Delete expired vouchers',detail={'plan':v.plan_name,'price':str(v.price)})
        for v in qs.only('pk','code','status','plan_name','price')],batch_size=500)
    n=qs.count();qs.delete();messages.success(request,f'{n} expired voucher(s) deleted. Their history is kept.');return redirect('vouchers')


@login_required
def batches(request):
    batch_list=list(b(request).batches.select_related('plan','agent').annotate(actual=Count('vouchers'),left=Count('vouchers',filter=Q(vouchers__sold_at__isnull=True,vouchers__used_at__isnull=True,vouchers__status='active')),
        sold=Count('vouchers',filter=Q(vouchers__sold_at__isnull=False)),used=Count('vouchers',filter=Q(vouchers__used_at__isnull=False))).order_by('-created_at'))
    # Open missing-voucher reports per batch (the template shows "N reported missing").
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
    business=b(request);form=PlanForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        obj=form.save(commit=False);obj.business=business;obj.source='taptap';obj.price_source='manual';obj.save();messages.success(request,'Plan saved.');return redirect('plans')
    plan_list=list(business.plans.select_related('imported_from_router').all().order_by('price','name'))
    zero=business.vouchers.filter(price=0,sold_at__isnull=True).values('plan_name').annotate(n=Count('id'))
    zero_map={r['plan_name']:r['n'] for r in zero}
    for p in plan_list: p.zero_vouchers=zero_map.get(p.name,0)
    return render(request,'core/plans.html',{'plans':plan_list,'form':form,'missing':[p for p in plan_list if not p.price]})


@login_required
def plan_update(request,pk):
    """Edit a plan in place. A price typed here is 'manual' and survives future router syncs."""
    from decimal import Decimal, InvalidOperation
    business=b(request);plan=get_object_or_404(business.plans,pk=pk)
    if request.method!='POST': return redirect('plans')
    old_price=plan.price
    try: price=max(Decimal('0'),Decimal(request.POST.get('price','0').replace(',','') or '0'))
    except (InvalidOperation,ValueError): messages.error(request,'Enter a valid price.');return redirect('plans')
    plan.price=price
    if price!=old_price: plan.price_source='manual'
    if request.POST.get('duration_value','').strip():
        from .durations import to_minutes
        try:
            unit=request.POST.get('duration_unit') or plan.duration_unit
            plan.duration_minutes=to_minutes(request.POST['duration_value'],unit);plan.duration_unit=unit
        except ValueError as e: messages.error(request,str(e));return redirect('plans')
    elif request.POST.get('duration_hours','').isdigit():  # older clients
        plan.duration_minutes=max(1,int(request.POST['duration_hours']))*60;plan.duration_unit='hours'
    if request.POST.get('max_devices','').isdigit(): plan.max_devices=max(1,int(request.POST['max_devices']))
    plan.active=request.POST.get('active')=='1'
    plan.save()
    unsold=business.vouchers.filter(plan_name=plan.name,sold_at__isnull=True,used_at__isnull=True)
    fixed=business.vouchers.filter(plan_name=plan.name,price=0,sold_at__isnull=True).update(price=price) if price else 0
    moved=unsold.exclude(price=price).update(price=price) if request.POST.get('apply_unsold') and price else 0
    msg=f'{plan.name} saved at {business.currency}{price}.'
    if fixed or moved: msg+=f' {fixed+moved} voucher price{"s" if fixed+moved!=1 else ""} updated.'
    messages.success(request,msg);return redirect('plans')


@login_required
def routers(request):
    business=b(request);form=RouterForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        obj=form.save(commit=False);obj.business=business;obj.save();messages.success(request,'Router added.');return redirect('routers')
    # IMPORTANT: do not annotate all inventory relations in one SQL query.
    # Joining hotspot users + IP bindings + neighbors + devices creates a
    # multiplicative Cartesian result set on real routers and can make the
    # /routers/ request run for minutes.  Use four small GROUP BY queries
    # instead; each relation is counted independently and remains fast even
    # when the synchronized MAC/device inventory is large.
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

    # Keep the router page usable even if a newly deployed background-sync
    # migration has not yet been applied.  Also never scan the full sync-job
    # history just to draw this page; fetch only the latest job per router and
    # the 12 rows shown by the monitor.
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
    except Exception as e: r.status='Offline';r.last_error=str(e);messages.error(request,f'Connection failed: {e}')  # direct-API routers only (Link handled above)
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
    rows,errors=_router_rows(b(request),'active_users');return render(request,'core/active_users.html',{'rows':rows,'errors':errors})


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
    return render(request,'core/topology.html',{'snapshots':snapshots,'routers':routers,'graph':graph,'netmap_config':netmap_config})


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
    return render(request,'core/security.html',{'incidents':incidents,'recent_fixed':recent_fixed,'summary':summary,'routers':routers,
        'fix_labels':{k:v[3] for k,v in MikroTikService.SECURITY_FIXES.items()}})


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
        return JsonResponse({'success':False,'router_id':router.id,'message':str(e)})


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
        if 'business_email' in f:
            em=f.get('business_email','').strip()
            business.email=em if re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',em) else ''
        business.currency=(f.get('currency','D').strip() or 'D')[:8]
        color=f.get('brand_color','#1769e0').strip()
        if re.fullmatch(r'#[0-9a-fA-F]{6}',color): business.brand_color=color
        logo=f.get('logo_data','')
        if f.get('remove_logo'): business.logo_data=''
        elif logo.startswith('data:image/') and len(logo)<400_000: business.logo_data=logo
        business.save()
        messages.success(request,'Settings saved. Portal pages and voucher designs use the new details straight away.')
        return redirect('settings')
    return render(request,'core/settings.html')
@login_required
def support(request): return render(request,'core/support.html')


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
