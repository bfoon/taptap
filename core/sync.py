from collections import defaultdict
from django.db import transaction
from django.utils import timezone

from .mikrotik import MikroTikService, ros_bool
from .models import (
    RouterHotspotUser,
    SyncedIPBinding,
    RouterInterface,
    RouterNeighbor,
)
from .utils import duration_to_routeros, log


def _clean(row):
    return {str(k).lstrip('.'): v for k, v in dict(row or {}).items()}


def _safe_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _neighbor_kind(row):
    hay = ' '.join(str(row.get(k, '')) for k in ('identity', 'platform', 'board')).lower()
    if any(word in hay for word in ('cap', 'wap', 'hap', 'access point', 'wifi', 'wireless', 'unifi', 'omada')):
        return 'wifi'
    if any(word in hay for word in ('switch', 'sw-', 'crs')):
        return 'switch'
    if any(word in hay for word in ('router', 'rb', 'ccr', 'hex')):
        return 'router'
    return 'network'


def sync_router(router):
    """Two-way synchronization between TapTap and one MikroTik router.

    TapTap vouchers and TapTap-origin IP bindings are database-authoritative.
    MikroTik-only objects are mirrored into dedicated tables but are not silently
    converted into commercial vouchers.
    """
    summary = {
        'pulled_users': 0,
        'pulled_bindings': 0,
        'pushed_vouchers': 0,
        'updated_vouchers': 0,
        'pushed_bindings': 0,
        'updated_bindings': 0,
        'errors': [],
        'unassigned_vouchers': router.business.vouchers.filter(router__isnull=True).count(),
    }
    svc = MikroTikService(router).connect()
    now = timezone.now()
    try:
        # ---- Pull hotspot users from MikroTik ----
        RouterHotspotUser.objects.filter(router=router).update(is_present=False)
        mikrotik_users = svc.hotspot_users()
        for raw in mikrotik_users:
            row = _clean(raw)
            username = str(row.get('name', '')).strip()
            if not username:
                continue
            voucher = router.business.vouchers.filter(code__iexact=username).first()
            source = 'taptap' if voucher else 'mikrotik'
            RouterHotspotUser.objects.update_or_create(
                router=router,
                username=username,
                defaults={
                    'business': router.business,
                    'mikrotik_id': row.get('id', ''),
                    'profile': row.get('profile', ''),
                    'mac_address': row.get('mac-address', row.get('mac_address', '')),
                    'comment': row.get('comment', ''),
                    'limit_uptime': row.get('limit-uptime', row.get('limit_uptime', '')),
                    'uptime': row.get('uptime', ''),
                    'disabled': ros_bool(row.get('disabled', False)),
                    'source': source,
                    'is_present': True,
                    'raw_data': row,
                    'last_seen_at': now,
                },
            )
            if voucher:
                fields = []
                if voucher.router_id is None:
                    voucher.router = router
                    fields.append('router')
                voucher.mikrotik_sync_status = 'Synced'
                voucher.mikrotik_sync_error = ''
                fields += ['mikrotik_sync_status', 'mikrotik_sync_error']
                voucher.save(update_fields=fields)
            summary['pulled_users'] += 1

        # ---- Push TapTap vouchers into MikroTik ----
        for voucher in router.vouchers.all():
            try:
                profile_name = f'taptap-{voucher.max_devices}-devices'
                plan = router.business.plans.filter(name=voucher.plan_name).first()
                rate_limit = plan.speed_limit if plan else ''
                svc.ensure_hotspot_profile(profile_name, voucher.max_devices, rate_limit)
                action, _ = svc.upsert_voucher(
                    voucher.code,
                    profile_name,
                    limit_uptime=duration_to_routeros(voucher.duration_hours),
                    comment=f'TapTap voucher {voucher.code}',
                    disabled=(voucher.status != 'active'),
                )
                voucher.mikrotik_sync_status = 'Synced'
                voucher.mikrotik_sync_error = ''
                voucher.save(update_fields=['mikrotik_sync_status', 'mikrotik_sync_error'])
                RouterHotspotUser.objects.update_or_create(
                    router=router,
                    username=voucher.code,
                    defaults={
                        'business': router.business,
                        'profile': profile_name,
                        'comment': f'TapTap voucher {voucher.code}',
                        'limit_uptime': duration_to_routeros(voucher.duration_hours),
                        'disabled': voucher.status != 'active',
                        'source': 'taptap',
                        'is_present': True,
                        'last_seen_at': now,
                    },
                )
                if action == 'created':
                    summary['pushed_vouchers'] += 1
                else:
                    summary['updated_vouchers'] += 1
            except Exception as exc:
                voucher.mikrotik_sync_status = 'Error'
                voucher.mikrotik_sync_error = str(exc)
                voucher.save(update_fields=['mikrotik_sync_status', 'mikrotik_sync_error'])
                summary['errors'].append(f'Voucher {voucher.code}: {exc}')

        # ---- Pull IP bindings from MikroTik ----
        SyncedIPBinding.objects.filter(router=router).update(is_present=False)
        for raw in svc.bindings():
            row = _clean(raw)
            item_id = str(row.get('id', ''))
            mac = str(row.get('mac-address', row.get('mac_address', '')))
            address = str(row.get('address', ''))
            existing = None
            if item_id:
                existing = SyncedIPBinding.objects.filter(router=router, mikrotik_id=item_id).first()
            if not existing and mac:
                existing = SyncedIPBinding.objects.filter(router=router, mac_address__iexact=mac, address=address).first()
            defaults = {
                'business': router.business,
                'mikrotik_id': item_id,
                'mac_address': mac,
                'address': address,
                'server': row.get('server', ''),
                'binding_type': row.get('type', 'bypassed'),
                'comment': row.get('comment', ''),
                'disabled': ros_bool(row.get('disabled', False)),
                'source': existing.source if existing else 'mikrotik',
                'sync_status': 'Synced',
                'sync_error': '',
                'is_present': True,
                'raw_data': row,
                'last_seen_at': now,
            }
            if existing:
                for key, value in defaults.items():
                    setattr(existing, key, value)
                existing.save()
            else:
                SyncedIPBinding.objects.create(router=router, **defaults)
            summary['pulled_bindings'] += 1

        # ---- Push bindings created/owned by TapTap ----
        for binding in router.synced_ip_bindings.filter(source='taptap'):
            try:
                action, item_id = svc.upsert_binding(binding)
                binding.mikrotik_id = str(item_id or binding.mikrotik_id)
                binding.sync_status = 'Synced'
                binding.sync_error = ''
                binding.is_present = True
                binding.last_seen_at = now
                binding.save(update_fields=['mikrotik_id','sync_status','sync_error','is_present','last_seen_at','updated_at'])
                if action == 'created':
                    summary['pushed_bindings'] += 1
                else:
                    summary['updated_bindings'] += 1
            except Exception as exc:
                binding.sync_status = 'Error'
                binding.sync_error = str(exc)
                binding.save(update_fields=['sync_status','sync_error','updated_at'])
                summary['errors'].append(f'IP binding {binding.mac_address or binding.address}: {exc}')

        router.status = 'Online'
        router.last_error = ''
        router.last_tested_at = now
        router.save(update_fields=['status','last_error','last_tested_at'])
        log(router.business, 'Router Sync', f'{router.name}: {summary["pulled_users"]} users, {summary["pulled_bindings"]} bindings pulled; {summary["pushed_vouchers"] + summary["updated_vouchers"]} vouchers synchronized')
        return summary
    finally:
        svc.close()


def refresh_router_topology(router):
    """Capture live topology from RouterOS and persist interfaces/neighbors."""
    svc = MikroTikService(router).connect()
    now = timezone.now()
    try:
        data = svc.topology_data()
        router.status = 'Online'
        router.last_error = ''
        router.last_tested_at = now
        router.save(update_fields=['status','last_error','last_tested_at'])

        interfaces = [_clean(x) for x in data['interfaces']]
        ethernet = {_clean(x).get('name'): _clean(x) for x in data['ethernet']}
        bridge_ports = {_clean(x).get('interface'): _clean(x) for x in data['bridge_ports']}
        bridge_hosts = [_clean(x) for x in data['bridge_hosts']]
        dhcp = [_clean(x) for x in data['dhcp_leases']]
        arp = [_clean(x) for x in data['arp']]
        wifi_regs = [_clean(x) for x in data['wifi_registrations']]
        wifi_clients_view = [{**x, 'mac_address': x.get('mac-address', x.get('mac_address', '')), 'last_activity': x.get('last-activity', x.get('last_activity', ''))} for x in wifi_regs]
        remote_caps = [_clean(x) for x in data['remote_caps']]

        RouterInterface.objects.filter(router=router).update(is_present=False)
        interface_map = {}
        for row in interfaces:
            name = str(row.get('name', '')).strip()
            if not name:
                continue
            eth = ethernet.get(name, {})
            obj, _ = RouterInterface.objects.update_or_create(
                router=router,
                name=name,
                defaults={
                    'default_name': eth.get('default-name', eth.get('default_name', '')),
                    'interface_type': row.get('type', ''),
                    'mac_address': row.get('mac-address', row.get('mac_address', eth.get('mac-address', ''))),
                    'comment': row.get('comment', ''),
                    'running': ros_bool(row.get('running', False)),
                    'disabled': ros_bool(row.get('disabled', False)),
                    'mtu': str(row.get('actual-mtu', row.get('mtu', ''))),
                    'rx_byte': _safe_int(row.get('rx-byte', row.get('rx_byte', 0))),
                    'tx_byte': _safe_int(row.get('tx-byte', row.get('tx_byte', 0))),
                    'is_present': True,
                    'raw_data': {**row, 'ethernet': eth, 'bridge_port': bridge_ports.get(name, {})},
                    'last_seen_at': now,
                },
            )
            interface_map[name] = obj

        RouterNeighbor.objects.filter(router=router).update(is_online=False)
        neighbors = []
        for raw in data['neighbors']:
            row = _clean(raw)
            identity = str(row.get('identity', '')).strip()
            address = str(row.get('address', '')).strip()
            mac = str(row.get('mac-address', row.get('mac_address', ''))).strip().upper()
            iface = str(row.get('interface', '')).split(',')[0].strip()
            key = mac or '|'.join([identity, address, iface])
            if not key:
                continue
            obj, _ = RouterNeighbor.objects.update_or_create(
                router=router,
                neighbor_key=key,
                defaults={
                    'identity': identity,
                    'address': address,
                    'mac_address': mac,
                    'interface_name': iface,
                    'platform': row.get('platform', ''),
                    'board': row.get('board', ''),
                    'version': row.get('version', ''),
                    'discovered_by': row.get('discovered-by', row.get('discovered_by', '')),
                    'device_kind': _neighbor_kind(row),
                    'is_online': True,
                    'raw_data': row,
                    'last_seen_at': now,
                },
            )
            neighbors.append(obj)

        # CAPsMAN remote CAPs are Wi-Fi routers/APs even when normal neighbor
        # discovery does not expose a useful identity.
        for row in remote_caps:
            identity = str(row.get('identity', row.get('name', 'Remote CAP')))
            mac = str(row.get('base-mac', row.get('base_mac', ''))).upper()
            address = str(row.get('address', ''))
            key = mac or f'cap|{identity}|{address}'
            RouterNeighbor.objects.update_or_create(
                router=router,
                neighbor_key=key,
                defaults={
                    'identity': identity,
                    'address': address,
                    'mac_address': mac,
                    'interface_name': str(row.get('interface', 'CAPsMAN')),
                    'platform': 'CAPsMAN',
                    'board': row.get('board-name', row.get('board_name', '')),
                    'version': row.get('version', ''),
                    'discovered_by': 'capsman',
                    'device_kind': 'wifi',
                    'is_online': True,
                    'raw_data': row,
                    'last_seen_at': now,
                },
            )

        # Index learned clients so each physical port can show what is attached.
        dhcp_by_mac = {str(x.get('mac-address', '')).upper(): x for x in dhcp if x.get('mac-address')}
        arp_by_mac = {str(x.get('mac-address', '')).upper(): x for x in arp if x.get('mac-address')}
        wifi_by_mac = {str(x.get('mac-address', '')).upper(): x for x in wifi_regs if x.get('mac-address')}
        clients_by_interface = defaultdict(list)
        for host in bridge_hosts:
            if ros_bool(host.get('local', False)):
                continue
            mac = str(host.get('mac-address', '')).upper()
            iface = str(host.get('on-interface', host.get('on_interface', '')))
            lease = dhcp_by_mac.get(mac, {})
            arp_row = arp_by_mac.get(mac, {})
            wifi = wifi_by_mac.get(mac, {})
            clients_by_interface[iface].append({
                'mac': mac,
                'name': lease.get('host-name', lease.get('comment', wifi.get('comment', 'Client'))),
                'ip': lease.get('address', arp_row.get('address', '')),
                'kind': 'wifi-client' if wifi else 'client',
            })

        ports = []
        physical_names = []
        for row in interfaces:
            name = str(row.get('name', ''))
            itype = str(row.get('type', '')).lower()
            if itype in {'ether', 'ethernet'} or name.lower().startswith(('ether','sfp','qsfp','combo')):
                physical_names.append(name)
        for name in physical_names:
            obj = interface_map.get(name)
            attached_neighbors = [n for n in RouterNeighbor.objects.filter(router=router, is_online=True, interface_name=name)]
            ports.append({
                'name': name,
                'running': bool(obj.running) if obj else False,
                'disabled': bool(obj.disabled) if obj else False,
                'mac_address': obj.mac_address if obj else '',
                'comment': obj.comment if obj else '',
                'rx_byte': obj.rx_byte if obj else 0,
                'tx_byte': obj.tx_byte if obj else 0,
                'neighbors': attached_neighbors,
                'clients': clients_by_interface.get(name, [])[:8],
                'client_count': len(clients_by_interface.get(name, [])),
                'bridge': bridge_ports.get(name, {}).get('bridge', ''),
                'pvid': bridge_ports.get(name, {}).get('pvid', ''),
            })

        return {
            'router': router,
            'identity': _clean(data['identity']),
            'routerboard': _clean(data['routerboard']),
            'ports': ports,
            'neighbors': list(RouterNeighbor.objects.filter(router=router).order_by('-is_online','identity')),
            'wifi_clients': wifi_clients_view,
            'captured_at': now,
            'error': '',
        }
    except Exception:
        raise
    finally:
        svc.close()


def snapshot_from_database(router):
    """Fallback topology when a router cannot currently be reached."""
    ports = []
    for obj in router.interfaces.filter(is_present=True).order_by('name'):
        name = obj.name
        if obj.interface_type.lower() in {'ether','ethernet'} or name.lower().startswith(('ether','sfp','qsfp','combo')):
            ports.append({
                'name': name,
                'running': obj.running,
                'disabled': obj.disabled,
                'mac_address': obj.mac_address,
                'comment': obj.comment,
                'rx_byte': obj.rx_byte,
                'tx_byte': obj.tx_byte,
                'neighbors': list(router.neighbors.filter(interface_name=name).order_by('-is_online','identity')),
                'clients': [],
                'client_count': 0,
                'bridge': obj.raw_data.get('bridge_port', {}).get('bridge', '') if obj.raw_data else '',
                'pvid': obj.raw_data.get('bridge_port', {}).get('pvid', '') if obj.raw_data else '',
            })
    return {
        'router': router,
        'identity': {},
        'routerboard': {},
        'ports': ports,
        'neighbors': list(router.neighbors.all().order_by('-is_online','identity')),
        'wifi_clients': [],
        'captured_at': router.last_tested_at,
        'error': router.last_error or 'Router currently unreachable. Showing the last saved discovery snapshot.',
    }
