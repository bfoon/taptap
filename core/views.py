from datetime import timedelta
from decimal import Decimal
import json

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count, Sum, Q
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .forms import RegisterForm, RouterForm, PlanForm
from .models import (
    Business, Subscription, VoucherPlan, Router, VoucherBatch, Voucher, Activity,
    SyncedIPBinding, RouterHotspotProfile, RouterDevice, RouterInterfaceRole,
    RouterConfigSnapshot, RouterConfigChange, RouterSyncJob,
)
from .mikrotik import MikroTikService, MikroTikError, redact
from .sync import sync_router, refresh_router_topology, snapshot_from_database
from .tasks import enqueue_router_sync
from .utils import generate_code, duration_to_routeros, log

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
                for n,p,h,d in DEFAULT_PLANS: VoucherPlan.objects.create(business=business,name=n,price=p,duration_hours=h,max_devices=d)
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
    business=b(request); vouchers=business.vouchers.all(); routers=business.routers.all(); revenue=vouchers.aggregate(v=Sum('price'))['v'] or 0
    stats={'vouchers':vouchers.count(),'available':vouchers.filter(status='active',used_at__isnull=True).count(),'active':vouchers.filter(status='active',used_at__isnull=False).count(),'routers':routers.count(),'online':routers.filter(status='Online').count(),'revenue':revenue}
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
    return render(request,'core/vouchers.html',{'vouchers':b(request).vouchers.select_related('router','batch').order_by('-created_at')})


@login_required
def generate_vouchers(request):
    business=b(request); plans=business.plans.filter(active=True); routers=business.routers.all()
    if request.method=='POST':
        plan=get_object_or_404(plans,pk=request.POST.get('plan')); qty=max(1,min(500,int(request.POST.get('quantity','1')))); router=routers.filter(pk=request.POST.get('router')).first(); batch_name=request.POST.get('batch_name','').strip() or f'{plan.name} {timezone.localtime():%Y-%m-%d %H:%M}'
        with transaction.atomic():
            batch=VoucherBatch.objects.create(business=business,name=batch_name,plan=plan,quantity=qty); made=[]
            for _ in range(qty):
                made.append(Voucher.objects.create(business=business,batch=batch,router=router,code=generate_code(),plan_name=plan.name,price=plan.price,duration_hours=plan.duration_hours,max_devices=plan.max_devices,source='taptap'))
            log(business,'Voucher Generated',f'Batch {batch.name}: {qty} voucher(s)')
        if router:
            try:
                _,created=enqueue_router_sync(router,request.user)
                if created:
                    messages.info(request,f'{router.name} background sync was queued to publish the new vouchers to RouterOS.')
                else:
                    messages.info(request,f'{router.name} already has a background sync running; the new vouchers will be picked up by that sync or the next one.')
            except Exception as e:
                messages.warning(request,f'Vouchers were created in TapTap, but the router background sync could not be queued: {e}')
        messages.success(request,f'{qty} voucher(s) created successfully.'); return redirect('vouchers')
    return render(request,'core/generate_vouchers.html',{'plans':plans,'routers':routers})


@login_required
def disable_voucher(request,pk):
    v=get_object_or_404(b(request).vouchers,pk=pk);v.status='disabled';v.save(update_fields=['status'])
    if v.router:
        try: svc=MikroTikService(v.router).connect();svc.disable_voucher(v.code);svc.close()
        except Exception as e: messages.warning(request,f'Disabled locally; router update failed: {e}')
    log(b(request),'Voucher Disabled',v.code);return redirect('vouchers')


@login_required
def reset_mac(request,pk):
    v=get_object_or_404(b(request).vouchers,pk=pk);v.device_bindings.all().delete()
    if v.router:
        try: svc=MikroTikService(v.router).connect();svc.reset_active_by_name(v.code);svc.close()
        except Exception as e: messages.warning(request,f'Device binding reset locally; router session removal failed: {e}')
    log(b(request),'Voucher MAC Reset',v.code);messages.success(request,'Voucher device binding reset.');return redirect('vouchers')


@login_required
def delete_expired(request):
    qs=b(request).vouchers.filter(Q(status='expired')|Q(expires_at__lt=timezone.now()));n=qs.count();qs.delete();messages.success(request,f'{n} expired voucher(s) deleted.');return redirect('vouchers')


@login_required
def batches(request): return render(request,'core/batches.html',{'batches':b(request).batches.select_related('plan').annotate(actual=Count('vouchers')).order_by('-created_at')})


@login_required
def plans(request):
    business=b(request);form=PlanForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        obj=form.save(commit=False);obj.business=business;obj.source='taptap';obj.save();messages.success(request,'Plan saved.');return redirect('plans')
    return render(request,'core/plans.html',{'plans':business.plans.select_related('imported_from_router').all().order_by('name'),'form':form})


@login_required
def routers(request):
    business=b(request);form=RouterForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        obj=form.save(commit=False);obj.business=business;obj.save();messages.success(request,'Router added.');return redirect('routers')
    router_rows=list(business.routers.annotate(hotspot_user_count=Count('hotspot_users',distinct=True),binding_count=Count('synced_ip_bindings',distinct=True),online_neighbor_count=Count('neighbors',filter=Q(neighbors__is_online=True),distinct=True),device_count=Count('devices',filter=Q(devices__is_online=True),distinct=True)))
    latest_jobs={}
    for job in business.router_sync_jobs.select_related('router').order_by('router_id','-created_at'):
        latest_jobs.setdefault(job.router_id,job)
    for router in router_rows:
        router.latest_sync_job=latest_jobs.get(router.id)
    recent_jobs=business.router_sync_jobs.select_related('router').all()[:12]
    return render(request,'core/routers.html',{'routers':router_rows,'form':form,'recent_sync_jobs':recent_jobs})


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
    if not snapshot:
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
    return render(request,'core/router_control.html',{'router':router,'snapshot':snapshot,'interfaces':interfaces,'bridges':bridges,'changes':changes,'role_choices':RouterInterfaceRole.ROLES,'catalog':MikroTikService.CONFIG_CATALOG})


@login_required
def router_config_refresh(request,pk):
    router=get_object_or_404(b(request).routers,pk=pk)
    if request.method!='POST': return redirect('router_control',pk=pk)
    try:
        svc=MikroTikService(router).connect();cfg=svc.configuration_snapshot();svc.close()
        RouterConfigSnapshot.objects.update_or_create(router=router,defaults={'sections':cfg['sections'],'load_balancing':cfg['load_balancing'],'captured_at':cfg['captured_at']})
        messages.success(request,f'{router.name} configuration snapshot refreshed ({len(cfg["sections"])} sections).')
    except Exception as e: messages.error(request,f'Could not refresh configuration: {e}')
    return redirect('router_control',pk=pk)


@login_required
def router_resource_api(request,pk):
    router=get_object_or_404(b(request).routers,pk=pk);path=request.GET.get('path','/interface')
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
    if request.POST.get('apply')=='yes':
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


@login_required
def router_telemetry(request,pk):
    router=get_object_or_404(b(request).routers,pk=pk)
    try:
        svc=MikroTikService(router).connect();data=svc.telemetry();svc.close();return JsonResponse({'success':True,**data})
    except Exception as e: return JsonResponse({'success':False,'message':str(e)},status=503)


def _router_rows(business,method):
    rows=[];errors=[]
    for r in business.routers.all():
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
    business=b(request);snapshots=[]
    for r in business.routers.all():
        try: snapshots.append(refresh_router_topology(r))
        except Exception as e:
            r.status='Offline';r.last_error=str(e);r.last_tested_at=timezone.now();r.save(update_fields=['status','last_error','last_tested_at']);snap=snapshot_from_database(r);snap['error']=str(e);snapshots.append(snap)
    managed_by_ip={r.ip_address:r for r in business.routers.all()};managed_links=[]
    for snap in snapshots:
        for neighbor in snap['neighbors']:
            peer=managed_by_ip.get(neighbor.address);neighbor.managed_peer=peer
            if peer and peer.id!=snap['router'].id: managed_links.append({'source':snap['router'],'target':peer,'interface':neighbor.interface_name,'online':neighbor.is_online})
    return render(request,'core/topology.html',{'snapshots':snapshots,'routers':business.routers.all(),'managed_links':managed_links})


@login_required
def reports(request):
    business=b(request);rows=business.vouchers.values('plan_name').annotate(count=Count('id'),revenue=Sum('price')).order_by('-revenue');return render(request,'core/reports.html',{'rows':rows})


@login_required
def finance(request):
    business=b(request);total=business.vouchers.aggregate(v=Sum('price'))['v'] or 0;return render(request,'core/finance.html',{'total':total,'subs':business.subscriptions.order_by('-created_at')})


@login_required
def security(request):
    business=b(request);shared=business.vouchers.annotate(devices=Count('device_bindings')).filter(devices__gt=1);return render(request,'core/security.html',{'shared':shared,'offline':business.routers.exclude(status='Online')})


@login_required
def settings_view(request): return render(request,'core/settings.html')
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
    return JsonResponse({'success':True,'voucher':v.code,'plan':v.plan_name,'max_devices':v.max_devices,'duration_hours':v.duration_hours})
