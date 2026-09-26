import re
from collections import defaultdict
from django.utils import timezone

from .mikrotik import MikroTikService, ros_bool, redact
from .models import (
    Voucher, VoucherPlan, RouterHotspotProfile, RouterHotspotUser,
    SyncedIPBinding, RouterInterface, RouterNeighbor, RouterDevice,
    RouterInterfaceRole, RouterConfigSnapshot,
)
from .utils import duration_to_routeros, log
from .finance import mark_activated


def _has_uptime(value):
    text = str(value or '').strip().lower()
    return bool(text) and text not in {'0', '0s', '00:00:00', 'none'} and any(ch.isdigit() and ch != '0' for ch in text)


def _clean(row):
    return {str(k).lstrip('.'): v for k, v in dict(row or {}).items()}


def _safe_int(value):
    try: return int(value or 0)
    except (TypeError, ValueError): return 0


def _routeros_hours(value, default=24):
    """Best-effort RouterOS duration parser: 1w2d3h4m, 12:30:00, etc."""
    text = str(value or '').strip().lower()
    if not text or text in {'0', 'none', 'unlimited'}:
        return default
    total_seconds = 0
    for number, unit in re.findall(r'(\d+)(w|d|h|m|s)', text):
        n = int(number)
        total_seconds += n * {'w':604800,'d':86400,'h':3600,'m':60,'s':1}[unit]
    if total_seconds:
        return max(1, int((total_seconds + 3599) // 3600))
    match = re.search(r'(?:(\d+)d)?\s*(\d{1,2}):(\d{2}):(\d{2})', text)
    if match:
        days, hours, mins, secs = [int(x or 0) for x in match.groups()]
        total_seconds = days*86400 + hours*3600 + mins*60 + secs
        return max(1, int((total_seconds + 3599)//3600))
    return default


def _neighbor_kind(row):
    hay = ' '.join(str(row.get(k, '')) for k in ('identity', 'platform', 'board')).lower()
    if any(word in hay for word in ('cap', 'wap', 'hap', 'access point', 'wifi', 'wireless', 'unifi', 'omada')): return 'wifi'
    if any(word in hay for word in ('switch', 'sw-', 'crs')): return 'switch'
    if any(word in hay for word in ('router', 'rb', 'ccr', 'hex')): return 'router'
    return 'network'


def _normalize_mac(value):
    return str(value or '').strip().upper().replace('-', ':')


def _profile_to_plan(router, row, summary, now):
    name = str(row.get('name', '')).strip()
    if not name:
        return None
    shared = max(1, _safe_int(row.get('shared-users', row.get('shared_users', 1))) or 1)
    rate = str(row.get('rate-limit', row.get('rate_limit', '')) or '')
    session = str(row.get('session-timeout', row.get('session_timeout', '')) or '')
    RouterHotspotProfile.objects.update_or_create(
        router=router, name=name,
        defaults={
            'business': router.business, 'mikrotik_id': str(row.get('id', '')), 'rate_limit': rate,
            'shared_users': shared, 'session_timeout': session,
            'idle_timeout': str(row.get('idle-timeout', row.get('idle_timeout', '')) or ''),
            'keepalive_timeout': str(row.get('keepalive-timeout', row.get('keepalive_timeout', '')) or ''),
            'address_pool': str(row.get('address-pool', row.get('address_pool', '')) or ''),
            'is_present': True, 'raw_data': row, 'last_seen_at': now,
        },
    )
    plan = VoucherPlan.objects.filter(business=router.business, name__iexact=name).first()
    if plan:
        summary['duplicate_plans_skipped'] += 1
        # A MikroTik-imported plan follows RouterOS; a native TapTap plan keeps its commercial settings.
        if plan.source == 'mikrotik':
            plan.max_devices = shared
            plan.speed_limit = rate
            plan.duration_hours = _routeros_hours(session, plan.duration_hours or 24)
            plan.mikrotik_profile_name = name
            if not plan.imported_from_router_id: plan.imported_from_router = router
            plan.save(update_fields=['max_devices','speed_limit','duration_hours','mikrotik_profile_name','imported_from_router'])
        return plan
    plan = VoucherPlan.objects.create(
        business=router.business, name=name, price=0,
        duration_hours=_routeros_hours(session, 24), max_devices=shared,
        speed_limit=rate, active=True, source='mikrotik', imported_from_router=router,
        mikrotik_profile_name=name,
    )
    summary['pulled_plans'] += 1
    return plan


def sync_router(router, progress=None):
    """Two-way RouterOS sync with de-duplication and full MikroTik inventory import.

    ``progress`` is an optional callback accepting ``(percent, phase)``. It is
    deliberately best-effort so UI/job progress can never break the sync itself.
    """
    def notify(percent, phase):
        if not progress:
            return
        try:
            progress(max(0, min(100, int(percent))), str(phase))
        except Exception:
            pass

    notify(2, 'Opening RouterOS connection')
    summary = {
        'pulled_plans': 0, 'duplicate_plans_skipped': 0,
        'pulled_vouchers': 0, 'duplicate_vouchers_skipped': 0, 'pulled_users': 0,
        'pulled_bindings': 0, 'pushed_vouchers': 0, 'updated_vouchers': 0,
        'pushed_bindings': 0, 'updated_bindings': 0, 'devices_discovered': 0,
        'config_sections': 0, 'errors': [],
        'unassigned_vouchers': router.business.vouchers.filter(router__isnull=True, source='taptap').count(),
    }
    svc = MikroTikService(router).connect()
    now = timezone.now()
    notify(8, 'Connected — reading HotSpot plans')
    try:
        # 1) Pull RouterOS HotSpot user profiles -> TapTap plans, with business/name de-duplication.
        RouterHotspotProfile.objects.filter(router=router).update(is_present=False)
        profile_map = {}
        for raw in svc.hotspot_profiles():
            row = _clean(raw)
            plan = _profile_to_plan(router, row, summary, now)
            if plan: profile_map[plan.name.lower()] = plan

        notify(22, 'Plans imported — reading vouchers and HotSpot users')

        # 2) Pull every RouterOS HotSpot user into both the mirror and the main Voucher app.
        RouterHotspotUser.objects.filter(router=router).update(is_present=False)
        for raw in svc.hotspot_users():
            row = _clean(raw)
            username = str(row.get('name', '')).strip()
            if not username: continue
            profile_name = str(row.get('profile', 'default') or 'default')
            plan = profile_map.get(profile_name.lower()) or router.business.plans.filter(name__iexact=profile_name).first()
            max_devices = plan.max_devices if plan else 1
            duration_hours = _routeros_hours(row.get('limit-uptime', row.get('limit_uptime', '')), plan.duration_hours if plan else 24)
            disabled = ros_bool(row.get('disabled', False))
            existing_voucher = Voucher.objects.filter(code__iexact=username).first()
            source = 'taptap' if existing_voucher and existing_voucher.business_id == router.business_id and existing_voucher.source == 'taptap' else 'mikrotik'
            RouterHotspotUser.objects.update_or_create(
                router=router, username=username,
                defaults={
                    'business':router.business, 'mikrotik_id':str(row.get('id','')), 'profile':profile_name,
                    'mac_address':_normalize_mac(row.get('mac-address', row.get('mac_address',''))),
                    'comment':str(row.get('comment','')), 'limit_uptime':str(row.get('limit-uptime', row.get('limit_uptime',''))),
                    'uptime':str(row.get('uptime','')), 'disabled':disabled, 'source':source,
                    'is_present':True, 'raw_data':row, 'last_seen_at':now,
                },
            )
            summary['pulled_users'] += 1

            if existing_voucher:
                if existing_voucher.business_id != router.business_id:
                    summary['errors'].append(f'Voucher name {username} already belongs to another TapTap business; import skipped.')
                    continue
                summary['duplicate_vouchers_skipped'] += 1
                fields = ['mikrotik_sync_status','mikrotik_sync_error']
                existing_voucher.mikrotik_sync_status='Synced'; existing_voucher.mikrotik_sync_error=''
                if existing_voucher.router_id is None:
                    existing_voucher.router=router; fields.append('router')
                if existing_voucher.source == 'mikrotik':
                    existing_voucher.mikrotik_id=str(row.get('id','')); existing_voucher.plan_name=profile_name
                    existing_voucher.duration_hours=duration_hours; existing_voucher.max_devices=max_devices
                    existing_voucher.status='disabled' if disabled else 'active'
                    fields += ['mikrotik_id','plan_name','duration_hours','max_devices','status']
                existing_voucher.save(update_fields=list(dict.fromkeys(fields)))
                if _has_uptime(row.get('uptime')) and not existing_voucher.used_at:
                    try:
                        if mark_activated(existing_voucher, now): summary['activated_vouchers'] = summary.get('activated_vouchers', 0) + 1
                    except Exception as exc:
                        summary['errors'].append(f'Activation tracking for {username}: {exc}')
            else:
                try:
                    new_voucher = Voucher.objects.create(
                        business=router.business, router=router, code=username, plan_name=profile_name,
                        price=plan.price if plan else 0, duration_hours=duration_hours, max_devices=max_devices,
                        status='disabled' if disabled else 'active', source='mikrotik', mikrotik_id=str(row.get('id','')),
                        mikrotik_sync_status='Synced', mikrotik_sync_error='',
                    )
                    summary['pulled_vouchers'] += 1
                    if _has_uptime(row.get('uptime')):
                        # Imported users that were already used before TapTap saw them: mark used, never back-book revenue.
                        Voucher.objects.filter(pk=new_voucher.pk).update(used_at=now)
                except Exception as exc:
                    summary['errors'].append(f'Could not import RouterOS voucher {username}: {exc}')

        notify(40, 'Router vouchers imported — pushing TapTap vouchers')

        # 3) Push only TapTap-authored vouchers. MikroTik imports are already authoritative on the router.
        for voucher in router.vouchers.filter(source='taptap'):
            try:
                plan = router.business.plans.filter(name__iexact=voucher.plan_name).first()
                profile_name = plan.mikrotik_profile_name if plan and plan.mikrotik_profile_name else voucher.plan_name
                if not profile_name: profile_name = f'taptap-{voucher.max_devices}-devices'
                svc.ensure_hotspot_profile(profile_name, voucher.max_devices, plan.speed_limit if plan else '')
                action, item_id = svc.upsert_voucher(
                    voucher.code, profile_name, limit_uptime=duration_to_routeros(voucher.duration_hours),
                    comment=f'TapTap voucher {voucher.code}', disabled=(voucher.status != 'active'),
                )
                voucher.mikrotik_id=str(item_id or voucher.mikrotik_id); voucher.mikrotik_sync_status='Synced'; voucher.mikrotik_sync_error=''
                voucher.save(update_fields=['mikrotik_id','mikrotik_sync_status','mikrotik_sync_error'])
                if action=='created': summary['pushed_vouchers'] += 1
                else: summary['updated_vouchers'] += 1
            except Exception as exc:
                voucher.mikrotik_sync_status='Error'; voucher.mikrotik_sync_error=str(exc)
                voucher.save(update_fields=['mikrotik_sync_status','mikrotik_sync_error'])
                summary['errors'].append(f'Voucher {voucher.code}: {exc}')

        notify(55, 'TapTap vouchers pushed — importing RouterOS IP bindings')

        # 4) Pull IP bindings without duplicates.
        SyncedIPBinding.objects.filter(router=router).update(is_present=False)
        for raw in svc.bindings():
            row=_clean(raw); item_id=str(row.get('id','')); mac=_normalize_mac(row.get('mac-address',row.get('mac_address',''))); address=str(row.get('address',''))
            existing = SyncedIPBinding.objects.filter(router=router,mikrotik_id=item_id).first() if item_id else None
            if not existing and mac: existing=SyncedIPBinding.objects.filter(router=router,mac_address__iexact=mac,address=address).first()
            defaults={
                'business':router.business,'mikrotik_id':item_id,'mac_address':mac,'address':address,
                'server':row.get('server',''),'binding_type':row.get('type','bypassed'),'comment':row.get('comment',''),
                'disabled':ros_bool(row.get('disabled',False)),'source':existing.source if existing else 'mikrotik',
                'sync_status':'Synced','sync_error':'','is_present':True,'raw_data':row,'last_seen_at':now,
            }
            if existing:
                for key,value in defaults.items(): setattr(existing,key,value)
                existing.save()
            else: SyncedIPBinding.objects.create(router=router,**defaults)
            summary['pulled_bindings'] += 1

        # 5) Push TapTap-authored bindings.
        for binding in router.synced_ip_bindings.filter(source='taptap'):
            try:
                action,item_id=svc.upsert_binding(binding); binding.mikrotik_id=str(item_id or binding.mikrotik_id)
                binding.sync_status='Synced';binding.sync_error='';binding.is_present=True;binding.last_seen_at=now
                binding.save(update_fields=['mikrotik_id','sync_status','sync_error','is_present','last_seen_at','updated_at'])
                summary['pushed_bindings' if action=='created' else 'updated_bindings'] += 1
            except Exception as exc:
                binding.sync_status='Error';binding.sync_error=str(exc);binding.save(update_fields=['sync_status','sync_error','updated_at'])
                summary['errors'].append(f'IP binding {binding.mac_address or binding.address}: {exc}')

        notify(70, 'Bindings synchronized — scanning MACs, ports and neighbors')

        # 6) Pull ports, neighbors, MAC/IP inventory and topology in the same sync.
        topo_data = svc.topology_data()
        topology = _persist_topology(router, topo_data, now)
        summary['devices_discovered'] = router.devices.filter(is_online=True).count()

        notify(86, 'Topology discovered — reading RouterOS configuration')

        # 7) Save a broad, redacted RouterOS configuration snapshot and LB analysis.
        try:
            cfg=svc.configuration_snapshot()
            RouterConfigSnapshot.objects.update_or_create(
                router=router,
                defaults={'sections':cfg['sections'],'load_balancing':cfg['load_balancing'],'captured_at':cfg['captured_at']},
            )
            summary['config_sections']=len(cfg['sections'])
        except Exception as exc:
            summary['errors'].append(f'Configuration snapshot: {exc}')

        notify(96, 'Finalizing database inventory')
        router.status='Online';router.last_error='';router.last_tested_at=now
        router.save(update_fields=['status','last_error','last_tested_at'])
        log(router.business,'Router Sync',f'{router.name}: {summary["pulled_plans"]} new plans, {summary["pulled_vouchers"]} new vouchers, {summary["devices_discovered"]} devices; de-duplicated existing records')
        notify(100, 'Synchronization complete')
        return summary
    finally:
        svc.close()


def _persist_topology(router, data, now=None):
    now = now or timezone.now()
    interfaces=[_clean(x) for x in data.get('interfaces',[])]
    ethernet={_clean(x).get('name'):_clean(x) for x in data.get('ethernet',[])}
    bridge_ports={_clean(x).get('interface'):_clean(x) for x in data.get('bridge_ports',[])}
    bridge_hosts=[_clean(x) for x in data.get('bridge_hosts',[])]
    dhcp=[_clean(x) for x in data.get('dhcp_leases',[])]
    arp=[_clean(x) for x in data.get('arp',[])]
    hotspot_hosts=[_clean(x) for x in data.get('hotspot_hosts',[])]
    active=[_clean(x) for x in data.get('active_users',[])]
    wifi_regs=[_clean(x) for x in data.get('wifi_registrations',[])]
    remote_caps=[_clean(x) for x in data.get('remote_caps',[])]

    RouterInterface.objects.filter(router=router).update(is_present=False)
    interface_map={}
    for row in interfaces:
        name=str(row.get('name','')).strip()
        if not name: continue
        eth=ethernet.get(name,{})
        obj,_=RouterInterface.objects.update_or_create(
            router=router,name=name,
            defaults={
                'default_name':eth.get('default-name',eth.get('default_name','')),'interface_type':row.get('type',''),
                'mac_address':_normalize_mac(row.get('mac-address',row.get('mac_address',eth.get('mac-address','')))),
                'comment':row.get('comment',''),'running':ros_bool(row.get('running',False)),'disabled':ros_bool(row.get('disabled',False)),
                'mtu':str(row.get('actual-mtu',row.get('mtu',''))),'rx_byte':_safe_int(row.get('rx-byte',row.get('rx_byte',0))),
                'tx_byte':_safe_int(row.get('tx-byte',row.get('tx_byte',0))),'is_present':True,
                'raw_data':{**row,'ethernet':eth,'bridge_port':bridge_ports.get(name,{})},'last_seen_at':now,
            },
        )
        interface_map[name]=obj
        RouterInterfaceRole.objects.get_or_create(router=router,interface_name=name,defaults={'role':'unused'})

    RouterNeighbor.objects.filter(router=router).update(is_online=False)
    for raw in data.get('neighbors',[]):
        row=_clean(raw);identity=str(row.get('identity','')).strip();address=str(row.get('address','')).strip();mac=_normalize_mac(row.get('mac-address',row.get('mac_address','')));iface=str(row.get('interface','')).split(',')[0].strip()
        key=mac or '|'.join([identity,address,iface])
        if not key: continue
        RouterNeighbor.objects.update_or_create(
            router=router,neighbor_key=key,
            defaults={'identity':identity,'address':address,'mac_address':mac,'interface_name':iface,'platform':row.get('platform',''),'board':row.get('board',''),'version':row.get('version',''),'discovered_by':row.get('discovered-by',row.get('discovered_by','')),'device_kind':_neighbor_kind(row),'is_online':True,'raw_data':row,'last_seen_at':now},
        )
    for row in remote_caps:
        identity=str(row.get('identity',row.get('name','Remote CAP')));mac=_normalize_mac(row.get('base-mac',row.get('base_mac','')));address=str(row.get('address',''));key=mac or f'cap|{identity}|{address}'
        RouterNeighbor.objects.update_or_create(
            router=router,neighbor_key=key,
            defaults={'identity':identity,'address':address,'mac_address':mac,'interface_name':str(row.get('interface','CAPsMAN')),'platform':'CAPsMAN','board':row.get('board-name',row.get('board_name','')),'version':row.get('version',''),'discovered_by':'capsman','device_kind':'wifi','is_online':True,'raw_data':row,'last_seen_at':now},
        )

    # Merge all MAC/IP sources into one device inventory.
    RouterDevice.objects.filter(router=router).update(is_online=False)
    merged={}
    def touch(mac='',ip='',hostname='',iface='',source='',kind='',raw=None):
        mac=_normalize_mac(mac);ip=str(ip or '').strip();hostname=str(hostname or '').strip();iface=str(iface or '').strip()
        key=mac or (f'ip:{ip}' if ip else (f'name:{hostname}' if hostname else ''))
        if not key: return
        item=merged.setdefault(key,{'mac':mac,'ip':ip,'hostname':hostname,'iface':iface,'sources':set(),'kind':kind or 'wired','raw':{}})
        if mac:item['mac']=mac
        if ip:item['ip']=ip
        if hostname:item['hostname']=hostname
        if iface:item['iface']=iface
        if kind:item['kind']=kind
        if source:item['sources'].add(source)
        if raw:item['raw'][source or 'source']=raw
    for x in bridge_hosts:
        if not ros_bool(x.get('local',False)): touch(x.get('mac-address'),iface=x.get('on-interface',x.get('on_interface','')),source='bridge',kind='wired',raw=x)
    for x in dhcp: touch(x.get('mac-address'),x.get('address'),x.get('host-name',x.get('comment','')),x.get('interface',''),source='dhcp',raw=x)
    for x in arp: touch(x.get('mac-address'),x.get('address'),'',x.get('interface',''),source='arp',raw=x)
    for x in hotspot_hosts: touch(x.get('mac-address'),x.get('address'),x.get('user',''),'',source='hotspot-host',raw=x)
    for x in active: touch(x.get('mac-address'),x.get('address'),x.get('user',''),'',source='hotspot-active',raw=x)
    for x in wifi_regs: touch(x.get('mac-address'),'',x.get('comment',''),x.get('interface',''),source='wifi',kind='wifi',raw=x)
    for n in RouterNeighbor.objects.filter(router=router,is_online=True): touch(n.mac_address,n.address,n.identity,n.interface_name,source='neighbor',kind=n.device_kind,raw=n.raw_data)

    neighbors_by_iface={}
    for n in RouterNeighbor.objects.filter(router=router,is_online=True).order_by('identity'):
        if n.interface_name and n.interface_name not in neighbors_by_iface: neighbors_by_iface[n.interface_name]=n
    for key,item in merged.items():
        parent=neighbors_by_iface.get(item['iface'])
        RouterDevice.objects.update_or_create(
            router=router,device_key=key,
            defaults={'mac_address':item['mac'],'ip_address':item['ip'],'hostname':item['hostname'],'interface_name':item['iface'],'parent_identity':(parent.identity or parent.address or parent.mac_address) if parent else '', 'connection_type':item['kind'],'sources':', '.join(sorted(item['sources'])),'is_online':True,'raw_data':item['raw'],'last_seen_at':now},
        )

    clients_by_interface=defaultdict(list)
    for dev in RouterDevice.objects.filter(router=router,is_online=True):
        if dev.interface_name:
            clients_by_interface[dev.interface_name].append({'mac':dev.mac_address,'name':dev.hostname,'ip':dev.ip_address,'kind':dev.connection_type,'parent':dev.parent_identity})

    role_map={x.interface_name:x for x in router.interface_roles.all()}
    ports=[]
    for row in interfaces:
        name=str(row.get('name',''));itype=str(row.get('type','')).lower()
        if itype not in {'ether','ethernet'} and not name.lower().startswith(('ether','sfp','qsfp','combo')): continue
        obj=interface_map.get(name);role=role_map.get(name)
        ports.append({'name':name,'running':bool(obj.running) if obj else False,'disabled':bool(obj.disabled) if obj else False,'mac_address':obj.mac_address if obj else '', 'comment':obj.comment if obj else '', 'rx_byte':obj.rx_byte if obj else 0,'tx_byte':obj.tx_byte if obj else 0,'neighbors':list(router.neighbors.filter(is_online=True,interface_name=name)),'clients':clients_by_interface.get(name,[])[:8],'client_count':len(clients_by_interface.get(name,[])),'bridge':bridge_ports.get(name,{}).get('bridge',''),'pvid':bridge_ports.get(name,{}).get('pvid',''),'role':role.role if role else 'unused','role_label':role.get_role_display() if role else 'Unused'})

    wifi_clients_view=[{**x,'mac_address':x.get('mac-address',x.get('mac_address','')),'last_activity':x.get('last-activity',x.get('last_activity',''))} for x in wifi_regs]
    return {'router':router,'identity':_clean(data.get('identity',{})),'routerboard':_clean(data.get('routerboard',{})),'ports':ports,'neighbors':list(router.neighbors.all().order_by('-is_online','identity')),'wifi_clients':wifi_clients_view,'devices':list(router.devices.filter(is_online=True).order_by('interface_name','hostname','mac_address')),'captured_at':now,'error':''}


def refresh_router_topology(router, timeout=None):
    """Live discovery for one router. Persists ports/neighbors/devices and the WAN analysis."""
    svc=MikroTikService(router,timeout=timeout).connect();now=timezone.now()
    try:
        data=svc.topology_data();snap=_persist_topology(router,data,now)
        lb=svc.analyze_load_balancing(
            routes=data.get('routes',[]),mangle=data.get('mangle',[]),bonding=data.get('bonding',[]),
            routing_tables=data.get('routing_tables',[]),routing_rules=data.get('routing_rules',[]),
            addresses=data.get('addresses',[]),dhcp_clients=data.get('dhcp_clients',[]),
            interfaces=data.get('interfaces',[]),hotspot_servers=data.get('hotspot_servers',[]),
        )
        snap['load_balancing']=lb
        identity=_clean(data.get('identity',{}));board=_clean(data.get('routerboard',{}))
        cfg,created=RouterConfigSnapshot.objects.get_or_create(router=router,defaults={'sections':{},'load_balancing':lb,'captured_at':now})
        if not created:
            cfg.load_balancing=lb
        # Keep a small identity block so the graph can label and match routers without a full config snapshot.
        sections=dict(cfg.sections or {})
        sections['_identity']={'path':'/system/identity','count':1,'rows':[{'name':identity.get('name',''),'model':board.get('model',''),'serial':board.get('serial-number','')}]}
        sections['IP addresses']={'path':'/ip/address','count':len(data.get('addresses',[])),'rows':redact([dict(x) for x in data.get('addresses',[])[:500]])}
        cfg.sections=sections
        cfg.save(update_fields=['load_balancing','sections','updated_at'])
        router.status='Online';router.last_error='';router.last_tested_at=now;router.save(update_fields=['status','last_error','last_tested_at'])
        return snap
    finally: svc.close()


def snapshot_from_database(router):
    ports=[];role_map={x.interface_name:x for x in router.interface_roles.all()}
    for obj in router.interfaces.filter(is_present=True).order_by('name'):
        name=obj.name
        if obj.interface_type.lower() not in {'ether','ethernet'} and not name.lower().startswith(('ether','sfp','qsfp','combo')): continue
        role=role_map.get(name)
        devices=list(router.devices.filter(is_online=True,interface_name=name)[:8])
        ports.append({'name':name,'running':obj.running,'disabled':obj.disabled,'mac_address':obj.mac_address,'comment':obj.comment,'rx_byte':obj.rx_byte,'tx_byte':obj.tx_byte,'neighbors':list(router.neighbors.filter(interface_name=name).order_by('-is_online','identity')),'clients':[{'mac':d.mac_address,'name':d.hostname,'ip':d.ip_address,'kind':d.connection_type,'parent':d.parent_identity} for d in devices],'client_count':router.devices.filter(is_online=True,interface_name=name).count(),'bridge':obj.raw_data.get('bridge_port',{}).get('bridge','') if obj.raw_data else '','pvid':obj.raw_data.get('bridge_port',{}).get('pvid','') if obj.raw_data else '','role':role.role if role else 'unused','role_label':role.get_role_display() if role else 'Unused'})
    try: lb=router.config_snapshot.load_balancing
    except Exception: lb={}
    return {'router':router,'identity':{},'routerboard':{},'ports':ports,'neighbors':list(router.neighbors.all().order_by('-is_online','identity')),'wifi_clients':[],'devices':list(router.devices.filter(is_online=True).order_by('interface_name','hostname')),'load_balancing':lb,'captured_at':router.last_tested_at,'error':router.last_error or 'Router currently unreachable. Showing the last saved discovery snapshot.'}
