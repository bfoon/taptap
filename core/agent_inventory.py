"""Full RouterOS inventory synchronization over TapTap Link.

A Link router sits behind NAT, so TapTap cannot open RouterOS API to it.  Instead
TapTap queues small, signed inventory commands.  The router reads each RouterOS
resource locally, serializes it to JSON and POSTs it back over HTTPS.

The normal TapTap database mirrors are then updated so the rest of the app can
use the same data regardless of whether the router is connected by Direct API or
TapTap Link.
"""
from collections import OrderedDict

from django.core.cache import cache
from django.utils import timezone

from .finance import mark_activated
from .mikrotik import redact, ros_bool
from .models import (
    AgentCommand,
    Router,
    RouterConfigSnapshot,
    RouterHotspotProfile,
    RouterHotspotUser,
    RouterSyncJob,
    SyncedIPBinding,
    Voucher,
)
from .routeros_analysis import analyze_wan
from .sync import (
    _clean,
    _has_uptime,
    _normalize_mac,
    _persist_topology,
    _profile_to_plan,
    _routeros_hours,
    first_use_estimate,
    price_from_text,
)
from .utils import log


# Keep the same labels used by MikroTikService.CONFIG_CATALOG so existing pages
# can read RouterConfigSnapshot without caring how the snapshot was collected.
CONFIG_SOURCES = [
    ('system_identity', 'System identity', '/system/identity'),
    ('system_resources', 'System resources', '/system/resource'),
    ('interfaces', 'Interfaces', '/interface'),
    ('ethernet', 'Ethernet', '/interface/ethernet'),
    ('interface_lists', 'Interface lists', '/interface/list'),
    ('interface_list_members', 'Interface list members', '/interface/list/member'),
    ('bridges', 'Bridges', '/interface/bridge'),
    ('bridge_ports', 'Bridge ports', '/interface/bridge/port'),
    ('bridge_vlans', 'Bridge VLANs', '/interface/bridge/vlan'),
    ('vlan_interfaces', 'VLAN interfaces', '/interface/vlan'),
    ('bonding', 'Bonding', '/interface/bonding'),
    ('wireguard', 'WireGuard', '/interface/wireguard'),
    ('ip_addresses', 'IP addresses', '/ip/address'),
    ('arp', 'ARP', '/ip/arp'),
    ('routes', 'Routes', '/ip/route'),
    ('routing_tables', 'Routing tables', '/routing/table'),
    ('routing_rules', 'Routing rules', '/routing/rule'),
    ('dns', 'DNS', '/ip/dns'),
    ('dhcp_clients', 'DHCP clients', '/ip/dhcp-client'),
    ('dhcp_servers', 'DHCP servers', '/ip/dhcp-server'),
    ('dhcp_networks', 'DHCP networks', '/ip/dhcp-server/network'),
    ('dhcp_leases', 'DHCP leases', '/ip/dhcp-server/lease'),
    ('ip_pools', 'IP pools', '/ip/pool'),
    ('hotspot_servers', 'HotSpot servers', '/ip/hotspot'),
    ('hotspot_server_profiles', 'HotSpot server profiles', '/ip/hotspot/profile'),
    ('hotspot_user_profiles', 'HotSpot user profiles', '/ip/hotspot/user/profile'),
    ('hotspot_users', 'HotSpot users', '/ip/hotspot/user'),
    ('hotspot_ip_bindings', 'HotSpot IP bindings', '/ip/hotspot/ip-binding'),
    ('firewall_filter', 'Firewall filter', '/ip/firewall/filter'),
    ('firewall_nat', 'Firewall NAT', '/ip/firewall/nat'),
    ('firewall_mangle', 'Firewall mangle', '/ip/firewall/mangle'),
    ('firewall_raw', 'Firewall raw', '/ip/firewall/raw'),
    ('firewall_address_lists', 'Firewall address lists', '/ip/firewall/address-list'),
    ('simple_queues', 'Simple queues', '/queue/simple'),
    ('queue_tree', 'Queue tree', '/queue/tree'),
    ('ppp_profiles', 'PPP profiles', '/ppp/profile'),
    ('ppp_secrets', 'PPP secrets', '/ppp/secret'),
    ('ip_services', 'IP services', '/ip/service'),
    ('snmp', 'SNMP', '/snmp'),
    ('snmp_communities', 'SNMP communities', '/snmp/community'),
    ('neighbors', 'Neighbors', '/ip/neighbor'),
    ('neighbor_discovery', 'Neighbor discovery settings', '/ip/neighbor/discovery-settings'),
    ('users', 'Users', '/user'),
    ('mac_server', 'MAC server', '/tool/mac-server'),
    ('mac_winbox', 'MAC Winbox', '/tool/mac-server/mac-winbox'),
    ('bandwidth_server', 'Bandwidth server', '/tool/bandwidth-server'),
    ('socks', 'SOCKS', '/ip/socks'),
    ('web_proxy', 'Web proxy', '/ip/proxy'),
    ('upnp', 'UPnP', '/ip/upnp'),
    ('cloud', 'Cloud', '/ip/cloud'),
    ('ssh', 'SSH', '/ip/ssh'),
    ('routerboard', 'RouterBOARD', '/system/routerboard'),
    ('pppoe_clients', 'PPPoE clients', '/interface/pppoe-client'),
]

# Extra live/topology tables that are not part of the broad configuration
# catalogue but are needed by the device/topology/active-user views.
EXTRA_SOURCES = [
    ('bridge_hosts', 'Bridge hosts', '/interface/bridge/host'),
    ('hotspot_hosts', 'HotSpot hosts', '/ip/hotspot/host'),
    ('active_users', 'HotSpot active users', '/ip/hotspot/active'),
    ('wifi_registrations', 'WiFi registrations', '/interface/wifi/registration-table'),
    ('wireless_registrations', 'Wireless registrations', '/interface/wireless/registration-table'),
    ('remote_caps_wifi', 'WiFi CAPsMAN remote CAPs', '/interface/wifi/capsman/remote-cap'),
    ('remote_caps_legacy', 'Legacy CAPsMAN remote CAPs', '/caps-man/remote-cap'),
]

SOURCES = OrderedDict((key, {'label': label, 'path': path}) for key, label, path in CONFIG_SOURCES + EXTRA_SOURCES)

TOPOLOGY_KINDS = {
    'system_identity', 'routerboard', 'interfaces', 'ethernet', 'bridge_ports', 'bridge_hosts',
    'neighbors', 'dhcp_leases', 'dhcp_clients', 'arp', 'hotspot_hosts', 'active_users',
    'wifi_registrations', 'wireless_registrations', 'remote_caps_wifi', 'remote_caps_legacy',
}


def _rs(value):
    """RouterOS string literal; duplicated here to avoid an agent.py import cycle."""
    v = str(value).replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('\r', '').replace('\n', '\\n')
    return f'"{v}"'


def inventory_piece_script(cmd, url, check, nonce_value):
    """RouterOS body for one resource, chunked so large tables stay below Fetch limits."""
    kind = str((cmd.params or {}).get('kind', ''))
    src = SOURCES.get(kind)
    if not src:
        raise ValueError(f'Unknown inventory source: {kind}')
    path = src['path']
    # path comes only from SOURCES above.  RouterOS cannot execute a menu path held in
    # a variable, therefore the trusted literal path is emitted into the script.
    upload = f'{url}/api/agent/v1/inventory?c={cmd.pk}&n={nonce_value}'
    return f''':local rows [:toarray ""]
:do {{ :set rows [{path} print as-value] }} on-error={{ :set rows [:toarray ""] }}
:local buf "["
:local first true
:local part 0
:foreach row in=$rows do={{
  :local item [:serialize to=json value=$row options=json.no-string-conversion]
  :if (([:len $buf] + [:len $item]) > 45000) do={{
    :set buf ($buf . "]")
    /tool fetch url=({_rs(upload)} . "&part=" . $part . "&final=0") http-method=post http-header-field="Content-Type: application/json" http-data=$buf output=none check-certificate={check} duration=12s idle-timeout=8s
    :set part ($part + 1)
    :set buf "["
    :set first true
  }}
  :if ($first = false) do={{ :set buf ($buf . ",") }}
  :set buf ($buf . $item)
  :set first false
}}
:set buf ($buf . "]")
/tool fetch url=({_rs(upload)} . "&part=" . $part . "&final=1") http-method=post http-header-field="Content-Type: application/json" http-data=$buf output=none check-certificate={check} duration=12s idle-timeout=8s'''


def start_agent_inventory_sync(job):
    """Turn a RouterSyncJob into signed TapTap-Link inventory commands."""
    router = job.router
    try:
        agent = router.agent
    except Exception:
        agent = None
    if not agent or agent.revoked:
        raise ValueError('TapTap Link is not enrolled for this router.')

    # Do not allow old inventory commands to mix with a new full synchronization.
    router.agent_commands.filter(kind='inventory_piece', status__in=['queued', 'sent']).update(
        status='cancelled', done_at=timezone.now(), result='Superseded by a newer full synchronization'
    )

    for kind, src in SOURCES.items():
        from .agent import queue
        queue(
            router,
            'inventory_piece',
            {'job_id': job.pk, 'kind': kind},
            label=f'Sync {src["label"]}',
            user=job.requested_by,
            minutes=30,
        )

    job.status = 'running'
    job.progress = 5
    job.phase = 'Waiting for TapTap Link — collecting RouterOS inventory'
    job.summary = {
        'transport': 'TapTap Link',
        'inventory_parts': len(SOURCES),
        'inventory_received': 0,
        'pulled_plans': 0,
        'pulled_vouchers': 0,
        'pulled_users': 0,
        'pulled_bindings': 0,
        'devices_discovered': 0,
        'errors': [],
    }
    job.save(update_fields=['status', 'progress', 'phase', 'summary', 'updated_at'])
    return {'queued_inventory_parts': len(SOURCES), 'transport': 'TapTap Link'}


def _snapshot_store(router, kind, rows, now):
    src = SOURCES[kind]
    snap, _ = RouterConfigSnapshot.objects.get_or_create(router=router)
    sections = dict(snap.sections or {})
    limit = 2000 if kind in TOPOLOGY_KINDS else 500
    sections[src['label']] = {
        'path': src['path'],
        'rows': redact(rows[:limit]),
        'count': len(rows),
        'transport': 'agent',
    }
    snap.sections = sections
    snap.captured_at = now
    snap.save(update_fields=['sections', 'captured_at', 'updated_at'])
    return snap


def _profiles(router, rows, now):
    summary = {'pulled_plans': 0, 'duplicate_plans_skipped': 0, 'prices_found': 0, 'plans_without_price': [], 'errors': []}
    RouterHotspotProfile.objects.filter(router=router).update(is_present=False)
    for raw in rows:
        try:
            _profile_to_plan(router, _clean(raw), summary, now)
        except Exception as exc:
            summary['errors'].append(f'HotSpot profile import: {exc}')
    return summary


def _users(router, rows, now):
    summary = {
        'pulled_users': 0, 'pulled_vouchers': 0, 'duplicate_vouchers_skipped': 0,
        'prices_repaired': 0, 'activated_vouchers': 0, 'errors': [],
    }
    RouterHotspotUser.objects.filter(router=router).update(is_present=False)
    for raw in rows:
        row = _clean(raw)
        username = str(row.get('name', '')).strip()
        if not username:
            continue
        profile_name = str(row.get('profile', 'default') or 'default')
        plan = router.business.plans.filter(name__iexact=profile_name).first()
        max_devices = plan.max_devices if plan else 1
        duration_hours = _routeros_hours(row.get('limit-uptime', row.get('limit_uptime', '')), plan.duration_hours if plan else 24)
        disabled = ros_bool(row.get('disabled', False))
        existing_voucher = Voucher.objects.filter(code__iexact=username).first()
        source = 'taptap' if existing_voucher and existing_voucher.business_id == router.business_id and existing_voucher.source == 'taptap' else 'mikrotik'
        RouterHotspotUser.objects.update_or_create(
            router=router,
            username=username,
            defaults={
                'business': router.business,
                'mikrotik_id': str(row.get('id', '')),
                'profile': profile_name,
                'mac_address': _normalize_mac(row.get('mac-address', row.get('mac_address', ''))),
                'comment': str(row.get('comment', '')),
                'limit_uptime': str(row.get('limit-uptime', row.get('limit_uptime', ''))),
                'uptime': str(row.get('uptime', '')),
                'disabled': disabled,
                'source': source,
                'is_present': True,
                'raw_data': row,
                'last_seen_at': now,
            },
        )
        summary['pulled_users'] += 1

        user_price = price_from_text(row.get('comment', ''), router.business.currency)
        plan_price = plan.price if plan and plan.price else None
        if existing_voucher:
            if existing_voucher.business_id != router.business_id:
                summary['errors'].append(f'Voucher name {username} belongs to another TapTap business; import skipped.')
                continue
            summary['duplicate_vouchers_skipped'] += 1
            fields = ['mikrotik_sync_status', 'mikrotik_sync_error']
            existing_voucher.mikrotik_sync_status = 'Synced'
            existing_voucher.mikrotik_sync_error = ''
            if existing_voucher.router_id is None:
                existing_voucher.router = router
                fields.append('router')
            if existing_voucher.source == 'mikrotik':
                existing_voucher.mikrotik_id = str(row.get('id', ''))
                existing_voucher.plan_name = profile_name
                existing_voucher.duration_hours = duration_hours
                existing_voucher.max_devices = max_devices
                existing_voucher.status = 'disabled' if disabled else 'active'
                fields += ['mikrotik_id', 'plan_name', 'duration_hours', 'max_devices', 'status']
            new_price = user_price or plan_price
            if new_price and not existing_voucher.price and not existing_voucher.sold_at:
                existing_voucher.price = new_price
                fields.append('price')
                summary['prices_repaired'] += 1
            existing_voucher.save(update_fields=list(dict.fromkeys(fields)))
            if _has_uptime(row.get('uptime')) and not existing_voucher.used_at:
                try:
                    when = first_use_estimate(row.get('uptime'), now, existing_voucher.created_at if existing_voucher.source == 'taptap' else None)
                    if mark_activated(existing_voucher, when):
                        summary['activated_vouchers'] += 1
                except Exception as exc:
                    summary['errors'].append(f'Activation tracking for {username}: {exc}')
        else:
            try:
                new_voucher = Voucher.objects.create(
                    business=router.business,
                    router=router,
                    code=username,
                    plan_name=profile_name,
                    price=user_price or plan_price or 0,
                    duration_hours=duration_hours,
                    max_devices=max_devices,
                    status='disabled' if disabled else 'active',
                    source='mikrotik',
                    mikrotik_id=str(row.get('id', '')),
                    mikrotik_sync_status='Synced',
                    mikrotik_sync_error='',
                )
                summary['pulled_vouchers'] += 1
                if _has_uptime(row.get('uptime')):
                    when = first_use_estimate(row.get('uptime'), now)
                    if router.sales_baseline_at:
                        if mark_activated(new_voucher, when):
                            summary['activated_vouchers'] += 1
                    else:
                        Voucher.objects.filter(pk=new_voucher.pk).update(used_at=when)
            except Exception as exc:
                summary['errors'].append(f'Could not import RouterOS voucher {username}: {exc}')
    return summary


def _bindings(router, rows, now):
    summary = {'pulled_bindings': 0, 'errors': []}
    SyncedIPBinding.objects.filter(router=router).update(is_present=False)
    for raw in rows:
        row = _clean(raw)
        item_id = str(row.get('id', ''))
        mac = _normalize_mac(row.get('mac-address', row.get('mac_address', '')))
        address = str(row.get('address', ''))
        existing = SyncedIPBinding.objects.filter(router=router, mikrotik_id=item_id).first() if item_id else None
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
    return summary


def _rows(sections, label):
    sec = sections.get(label) or {}
    rows = sec.get('rows') or []
    return rows if isinstance(rows, list) else []


def _rebuild_topology_and_analysis(router, snap, now):
    sections = snap.sections or {}
    wifi = _rows(sections, 'WiFi registrations') + _rows(sections, 'Wireless registrations')
    caps = _rows(sections, 'WiFi CAPsMAN remote CAPs') + _rows(sections, 'Legacy CAPsMAN remote CAPs')
    identity_rows = _rows(sections, 'System identity')
    board_rows = _rows(sections, 'RouterBOARD')
    data = {
        'identity': identity_rows[0] if identity_rows else {},
        'routerboard': board_rows[0] if board_rows else {},
        'interfaces': _rows(sections, 'Interfaces'),
        'ethernet': _rows(sections, 'Ethernet'),
        'bridge_ports': _rows(sections, 'Bridge ports'),
        'bridge_hosts': _rows(sections, 'Bridge hosts'),
        'neighbors': _rows(sections, 'Neighbors'),
        'dhcp_leases': _rows(sections, 'DHCP leases'),
        'dhcp_clients': _rows(sections, 'DHCP clients'),
        'arp': _rows(sections, 'ARP'),
        'hotspot_hosts': _rows(sections, 'HotSpot hosts'),
        'active_users': _rows(sections, 'HotSpot active users'),
        'wifi_registrations': wifi,
        'remote_caps': caps,
        'routes': _rows(sections, 'Routes'),
        'mangle': _rows(sections, 'Firewall mangle'),
        'routing_tables': _rows(sections, 'Routing tables'),
        'routing_rules': _rows(sections, 'Routing rules'),
        'bonding': _rows(sections, 'Bonding'),
        'addresses': _rows(sections, 'IP addresses'),
        'hotspot_servers': _rows(sections, 'HotSpot servers'),
    }
    _persist_topology(router, data, now)

    declared = list(router.interface_roles.filter(role='wan').values_list('interface_name', flat=True))
    try:
        snap.load_balancing = redact(analyze_wan(
            routes=data['routes'],
            mangle=data['mangle'],
            bonding=data['bonding'],
            routing_tables=data['routing_tables'],
            routing_rules=data['routing_rules'],
            addresses=data['addresses'],
            dhcp_clients=data['dhcp_clients'],
            interfaces=data['interfaces'],
            hotspot_servers=data['hotspot_servers'],
            declared_wans=declared,
        ))
        snap.save(update_fields=['load_balancing', 'updated_at'])
    except Exception:
        # The raw snapshot remains useful even if a particular RouterOS version
        # returns fields the WAN analyzer does not understand.
        pass


def _merge_summary(base, extra):
    out = dict(base or {})
    for key, value in (extra or {}).items():
        if isinstance(value, int) and isinstance(out.get(key), int):
            out[key] += value
        elif isinstance(value, list):
            out[key] = list(out.get(key) or []) + value
        else:
            out[key] = value
    return out


def receive_inventory_chunk(cmd, rows, part=0, final=False):
    """Accept one JSON chunk uploaded by a signed inventory_piece command."""
    params = dict(cmd.params or {})
    kind = str(params.get('kind', ''))
    if kind not in SOURCES:
        raise ValueError('Unknown inventory source.')
    if not isinstance(rows, list):
        raise ValueError('Inventory payload must be a JSON array.')
    if len(rows) > 10000:
        raise ValueError('Inventory chunk is too large.')

    key = f'tt:agent-inventory:{cmd.pk}'
    parts = cache.get(key) or {}
    parts[str(max(0, int(part or 0)))] = rows
    cache.set(key, parts, 1800)
    if not final:
        return {'accepted': len(rows), 'complete': False}

    combined = []
    for pkey in sorted(parts, key=lambda x: int(x)):
        combined.extend(parts[pkey])
    cache.delete(key)

    router = cmd.router
    now = timezone.now()
    clean_rows = [_clean(x) for x in combined if isinstance(x, dict)]
    snap = _snapshot_store(router, kind, clean_rows, now)

    piece_summary = {}
    if kind == 'hotspot_user_profiles':
        piece_summary = _profiles(router, clean_rows, now)
    elif kind == 'hotspot_users':
        piece_summary = _users(router, clean_rows, now)
    elif kind == 'hotspot_ip_bindings':
        piece_summary = _bindings(router, clean_rows, now)
    elif kind == 'active_users':
        try:
            from .agent import ingest_sessions
            ingest_sessions(router, clean_rows, now)
        except Exception as exc:
            piece_summary = {'errors': [f'Active-session ingest: {exc}']}

    params['received'] = True
    params['row_count'] = len(clean_rows)
    cmd.params = params
    cmd.save(update_fields=['params'])

    job_id = params.get('job_id')
    job = RouterSyncJob.objects.filter(pk=job_id, router=router).first()
    if not job:
        return {'accepted': len(clean_rows), 'complete': True}

    job.summary = _merge_summary(job.summary, piece_summary)
    pieces = list(router.agent_commands.filter(kind='inventory_piece'))
    pieces = [c for c in pieces if (c.params or {}).get('job_id') == job.pk]
    received = [c for c in pieces if (c.params or {}).get('received')]
    job.summary['inventory_parts'] = len(pieces)
    job.summary['inventory_received'] = len(received)
    progress = 5 + int((len(received) / max(1, len(pieces))) * 90)
    job.progress = min(95, progress)
    job.phase = f'TapTap Link inventory: {len(received)}/{len(pieces)} sections received'

    received_kinds = {(c.params or {}).get('kind') for c in received}
    if TOPOLOGY_KINDS.issubset(received_kinds):
        try:
            _rebuild_topology_and_analysis(router, snap, now)
            job.summary['devices_discovered'] = router.devices.filter(is_online=True).count()
        except Exception as exc:
            job.summary = _merge_summary(job.summary, {'errors': [f'Topology rebuild: {exc}']})

    if len(received) >= len(pieces) and pieces:
        job.status = 'success'
        job.progress = 100
        job.phase = 'Synchronization complete through TapTap Link'
        job.finished_at = now
        job.summary['config_sections'] = len(snap.sections or {})
        Router.objects.filter(pk=router.pk).update(
            status='Online', last_error='', last_tested_at=now, last_watch_at=now,
            connection_mode='agent', ip_address='', username='', password='', use_ssl=False,
        )
        if not router.sales_baseline_at:
            Router.objects.filter(pk=router.pk, sales_baseline_at__isnull=True).update(sales_baseline_at=now)
        log(router.business, 'Router Sync', f'{router.name}: full inventory synchronized through TapTap Link')
        job.save(update_fields=['status', 'progress', 'phase', 'summary', 'finished_at', 'updated_at'])
    else:
        job.save(update_fields=['progress', 'phase', 'summary', 'updated_at'])

    return {'accepted': len(clean_rows), 'complete': True, 'progress': job.progress}


def inventory_command_ack(cmd, ok):
    """Fail the parent sync job if RouterOS explicitly rejects an inventory piece."""
    params = cmd.params or {}
    job = RouterSyncJob.objects.filter(pk=params.get('job_id'), router=cmd.router).first()
    if not job or job.status not in {'queued', 'running'}:
        return
    if not ok:
        job.status = 'failed'
        job.phase = f'Agent inventory failed: {SOURCES.get(params.get("kind"), {}).get("label", params.get("kind", "section"))}'
        job.error = cmd.result or 'The router reported an inventory error.'
        job.finished_at = timezone.now()
        job.save(update_fields=['status', 'phase', 'error', 'finished_at', 'updated_at'])
