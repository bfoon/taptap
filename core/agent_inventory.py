"""Full RouterOS inventory synchronization over TapTap Link.

A Link router sits behind NAT, so TapTap cannot open RouterOS API to it.  Instead
TapTap queues small, signed inventory commands.  The router reads each RouterOS
resource locally, serializes it to JSON and POSTs it back over HTTPS.

The normal TapTap database mirrors are then updated so the rest of the app can
use the same data regardless of whether the router is connected by Direct API or
TapTap Link.
"""
import time
from datetime import timedelta
from collections import OrderedDict

from django.core.cache import cache
from django.db import transaction
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
from .durations import parse_routeros as _routeros_minutes
from .routeros_analysis import analyze_wan
from .sync import (
    _clean,
    _has_uptime,
    _normalize_mac,
    _persist_topology,
    _profile_to_plan,
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
    # `/interface print as-value` leaves out the traffic counters; `print stats` has them.
    ('interface_stats', 'Interface counters', '/interface'),
]

# Tables that only exist with the wifi / wireless / CAPsMAN packages.
OPTIONAL_KINDS = {'wifi_registrations', 'wireless_registrations', 'remote_caps_wifi', 'remote_caps_legacy'}

SOURCES = OrderedDict((key, {'label': label, 'path': path}) for key, label, path in CONFIG_SOURCES + EXTRA_SOURCES)
SOURCES['interface_stats']['args'] = 'stats'

# Interface statistics copied onto the interface rows (rx-byte, packets, errors, drops, link drops).
STAT_KEYS = ('rx-byte', 'tx-byte', 'rx-packet', 'tx-packet', 'rx-drop', 'tx-drop', 'rx-error', 'tx-error', 'tx-queue-drop',
             'fp-rx-byte', 'fp-tx-byte', 'fp-rx-packet', 'fp-tx-packet', 'link-downs', 'last-link-up-time', 'last-link-down-time')

# Periodic traffic samples for TapTap Link routers (not part of a full sync).
SAMPLE_SOURCES = OrderedDict([
    ('traffic_dns', {'label': 'DNS cache (traffic sample)', 'path': '/ip/dns/cache', 'sample': True}),
    ('traffic_conns', {'label': 'Busy connections (traffic sample)', 'path': '/ip/firewall/connection', 'where': 'repl-bytes>20000', 'sample': True}),
])


def source(kind):
    return SOURCES.get(kind) or SAMPLE_SOURCES.get(kind)


TOPOLOGY_KINDS = {
    'system_identity', 'routerboard', 'interfaces', 'ethernet', 'bridge_ports', 'bridge_hosts',
    'neighbors', 'dhcp_leases', 'dhcp_clients', 'arp', 'hotspot_hosts', 'active_users',
    'wifi_registrations', 'wireless_registrations', 'remote_caps_wifi', 'remote_caps_legacy',
}


def _rs(value):
    """RouterOS string literal; duplicated here to avoid an agent.py import cycle."""
    v = str(value).replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('\r', '').replace('\n', '\\n')
    return f'"{v}"'


def send_function():
    """RouterOS helper defined once per batch: uploads one table in ~30 KB chunks.

    Works on RouterOS 6 and 7 (no :serialize needed). Each row is written as
    key=value fields separated by 0x1F and rows by 0x1E, so values containing
    commas, quotes or newlines (e.g. Mikhmon on-login scripts) survive intact.
    The leading '#' keeps the body non-empty for tables without rows.
    """
    post = ('/tool fetch url=($u . "&part=" . $part . "&final={final}&fmt=t") http-method=post '
            'http-header-field="Content-Type: text/plain" http-data=$buf output=none check-certificate=$chk duration=20s idle-timeout=15s')
    return (':local ttSend do={ :local buf "#"; :local part 0; '
            ':foreach row in=$rows do={ :local line ""; '
            ':foreach k,v in=$row do={ :set line ($line . $k . "=" . [:tostr $v] . "\\1F") }; '
            ':set buf ($buf . $line . "\\1E"); '
            ':if ([:len $buf] > 30000) do={ ' + post.format(final=0) + '; :set part ($part + 1); :set buf "#" } }; '
            + post.format(final=1) + ' }')


def inventory_piece_script(cmd, url, check, nonce_value):
    """RouterOS body for one table.

    RouterOS print filters belong after ``print as-value``.  Keeping the resource
    path and the optional ``where`` expression separate is important for periodic
    traffic samples such as /ip/firewall/connection.
    """
    kind = str((cmd.params or {}).get('kind', ''))
    src = source(kind)
    if not src:
        raise ValueError(f'Unknown inventory source: {kind}')

    menu = src['path']
    where = f' where {src["where"]}' if src.get('where') else ''
    upload = f'{url}/api/agent/v1/inventory?c={cmd.pk}&n={nonce_value}'

    # Compile the RouterOS command at run time so optional menus missing on a
    # particular RouterOS version become a caught run-time error instead of
    # invalidating the entire returned command batch.
    args = f' {src["args"]}' if src.get('args') else ''
    command = f'{menu} print{args} as-value{where}'
    return (
        f':local rows [:toarray ""]; '
        f':do {{ :set rows [[:parse ":return [{command}]"]] }} '
        f'on-error={{ :set rows [:toarray ""] }}; '
        f'$ttSend rows=$rows u={_rs(upload)} chk="{check}"'
    )


def parse_text_rows(body):
    """Decode the 0x1E/0x1F format uploaded by send_function()."""
    text = body.decode('utf-8', 'ignore') if isinstance(body, (bytes, bytearray)) else str(body or '')
    if text.startswith('#'):
        text = text[1:]
    rows = []
    for rec in text.split('\x1e'):
        if not rec:
            continue
        row = {}
        for field in rec.split('\x1f'):
            if '=' in field:
                k, v = field.split('=', 1)
                row[k] = v
        if row:
            rows.append(row)
    return rows


def start_agent_inventory_sync(job):
    """Turn a RouterSyncJob into signed TapTap-Link inventory commands."""
    router = job.router
    try:
        agent = router.agent
    except Exception:
        agent = None
    if not agent or agent.revoked:
        raise ValueError('TapTap Link is not enrolled for this router.')

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
    from .voucher_bin import deleted_codes, heal
    from .voucher_codes import aliases as code_aliases, heal as heal_codes
    binned, binned_seen = deleted_codes(router.business), []
    renamed, renamed_seen = code_aliases(router.business), []
    present = {str(_clean(r).get('name', '')).upper() for r in rows}
    for raw in rows:
        row = _clean(raw)
        username = str(row.get('name', '')).strip()
        if not username:
            continue
        if username.upper() in binned:
            binned_seen.append(username)   # deleted in TapTap: queue its removal, never re-import
            continue
        if username.upper() in renamed:
            renamed_seen.append((username, renamed[username.upper()].code))   # old code: rename, never import twice
            continue
        profile_name = str(row.get('profile', 'default') or 'default')
        plan = router.business.plans.filter(name__iexact=profile_name).first()
        max_devices = plan.max_devices if plan else 1
        duration_minutes = _routeros_minutes(row.get('limit-uptime', row.get('limit_uptime', '')), plan.duration_minutes if plan else 1440)
        disabled = ros_bool(row.get('disabled', False))
        existing_voucher = Voucher.all_objects.filter(code__iexact=username).first()
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
            if existing_voucher.source == 'mikrotik' and not existing_voucher.frozen_at:
                existing_voucher.mikrotik_id = str(row.get('id', ''))
                existing_voucher.plan_name = profile_name
                existing_voucher.duration_minutes = duration_minutes
                existing_voucher.max_devices = max_devices
                was = existing_voucher.status
                # Time ran out stays ran out, whatever the router says. Only "Add time" in TapTap reopens it.
                existing_voucher.status = 'expired' if was == 'expired' else ('disabled' if disabled else 'active')
                if was != existing_voucher.status:
                    from .voucher_history import record
                    record(existing_voucher, 'router_disabled' if disabled else 'router_enabled', source='router',
                           via='Full sync (TapTap Link)', status_before=was, status_after=existing_voucher.status,
                           text=f'Changed on {router.name}')
                fields += ['mikrotik_id', 'plan_name', 'duration_minutes', 'max_devices', 'status']
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
                    duration_minutes=duration_minutes,
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
    if renamed_seen:
        try:
            heal_codes(router, renamed_seen, present)
        except Exception as exc:
            summary['errors'].append(f'Could not queue renaming of changed voucher codes: {exc}')
    try:
        from .voucher_codes import repair_passwords, stale_passwords
        fixed = repair_passwords(router, stale_passwords(router.business, router, [_clean(r) for r in rows]))
        if fixed:
            summary['code_logins_repaired'] = fixed
    except Exception as exc:
        summary['errors'].append(f'Could not queue the login repair of changed voucher codes: {exc}')
    if binned_seen:
        try:
            heal(router, binned_seen)
        except Exception as exc:
            summary['errors'].append(f'Could not queue removal of deleted vouchers: {exc}')
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


def merge_interface_stats(interfaces, stats):
    """Copy counters from `/interface print stats` rows onto the matching interface rows (by name)."""
    by_name = {str(r.get('name', '')): r for r in stats or [] if r.get('name')}
    if not by_name:
        return interfaces
    out = []
    for row in interfaces:
        s = by_name.get(str(row.get('name', '')))
        out.append({**row, **{k: s[k] for k in STAT_KEYS if s and s.get(k) not in (None, '')}} if s else row)
    return out


def apply_interface_stats(router, stats):
    """Save the latest counters on the stored ports even when the topology itself did not change."""
    from .models import RouterInterface
    by_name = {str(r.get('name', '')): r for r in stats or [] if r.get('name')}
    if not by_name:
        return 0
    changed = []
    for obj in RouterInterface.objects.filter(router=router, name__in=list(by_name)):
        s = by_name[obj.name]
        fresh = {k: s[k] for k in STAT_KEYS if s.get(k) not in (None, '')}
        if not fresh:
            continue
        obj.raw_data = {**(obj.raw_data or {}), **fresh}
        obj.rx_byte = _count(fresh.get('rx-byte'), obj.rx_byte)
        obj.tx_byte = _count(fresh.get('tx-byte'), obj.tx_byte)
        changed.append(obj)
    if changed:
        RouterInterface.objects.bulk_update(changed, ['raw_data', 'rx_byte', 'tx_byte'])
    return len(changed)


def _count(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _rows(sections, label):
    sec = sections.get(label) or {}
    rows = sec.get('rows') or []
    return rows if isinstance(rows, list) else []


def _topology_digest(snap):
    """Fingerprint of the tables the topology is built from."""
    import hashlib
    import json
    sections = snap.sections or {}
    labels = sorted(SOURCES[k]['label'] for k in TOPOLOGY_KINDS if k in SOURCES)
    blob = json.dumps([(sections.get(label) or {}).get('rows') or [] for label in labels], sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()


def _topology_with_alerts(router, snap, now, job):
    """Rebuild topology + WAN analysis, and raise device offline/online alerts for
    the change. Skips the (heavier) topology write when its tables are unchanged
    since the last build in this job; the WAN analysis always runs because it also
    depends on routes/mangle tables that may have arrived since."""
    digest = _topology_digest(snap)
    if job.summary.get('topology_digest') == digest:
        _rebuild_topology_and_analysis(router, snap, now, topology=False)
        return
    from .presence import apply_changes, online_snapshot
    before = online_snapshot(router)
    _rebuild_topology_and_analysis(router, snap, now)
    try:
        apply_changes(router, before, online_snapshot(router))
    except Exception as exc:
        job.summary = _merge_summary(job.summary, {'errors': [f'Device alerts: {exc}']})
    job.summary['topology_digest'] = digest
    job.summary['topology_built'] = True
    job.summary['devices_discovered'] = router.devices.filter(is_online=True).count()


def _rebuild_topology_and_analysis(router, snap, now, topology=True):
    sections = snap.sections or {}
    wifi = _rows(sections, 'WiFi registrations') + _rows(sections, 'Wireless registrations')
    caps = _rows(sections, 'WiFi CAPsMAN remote CAPs') + _rows(sections, 'Legacy CAPsMAN remote CAPs')
    identity_rows = _rows(sections, 'System identity')
    board_rows = _rows(sections, 'RouterBOARD')
    data = {
        'identity': identity_rows[0] if identity_rows else {},
        'routerboard': board_rows[0] if board_rows else {},
        'interfaces': merge_interface_stats(_rows(sections, 'Interfaces'), _rows(sections, 'Interface counters')),
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
    if topology:
        _persist_topology(router, data, now)
    apply_interface_stats(router, _rows(sections, 'Interface counters'))

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
    """Accept one uploaded chunk. On the final chunk, processing is handed to the
    background worker so the router's upload returns at once (no timeouts on big tables)."""
    params = dict(cmd.params or {})
    kind = str(params.get('kind', ''))
    if not source(kind):
        raise ValueError('Unknown inventory source.')
    if not isinstance(rows, list):
        raise ValueError('Inventory payload must be a list of rows.')
    if len(rows) > 20000:
        raise ValueError('Inventory chunk is too large.')
    key = f'tt:agent-inventory:{cmd.pk}'
    parts = cache.get(key) or {}
    parts[str(max(0, int(part or 0)))] = rows
    cache.set(key, parts, 3600)
    if not final:
        return {'accepted': len(rows), 'complete': False}
    combined = []
    for pkey in sorted(parts, key=lambda x: int(x)):
        combined.extend(parts[pkey])
    cache.delete(key)
    cache.set(f'tt:agent-inventory-rows:{cmd.pk}', combined, 3600)
    from .tasks import process_inventory_piece
    try:
        process_inventory_piece.delay(cmd.pk)
    except Exception:
        process_piece(cmd.pk)
    return {'accepted': len(rows), 'complete': True, 'queued_for_processing': True}


def process_piece(cmd_id):
    """Import one uploaded table into TapTap. One router at a time, with row locks,
    so two tables never overwrite each other's part of the configuration snapshot."""
    cmd = AgentCommand.objects.select_related('router__business').filter(pk=cmd_id).first()
    if not cmd:
        return None
    rows = cache.get(f'tt:agent-inventory-rows:{cmd.pk}')
    if rows is None:
        return None
    lock = f'tt:agent-inventory-lock:{cmd.router_id}'
    for _ in range(120):
        if cache.add(lock, 1, 300):
            break
        time.sleep(0.5)
    try:
        params = dict(cmd.params or {})
        kind = str(params.get('kind', ''))
        router = cmd.router
        now = timezone.now()
        clean_rows = [_clean(x) for x in rows if isinstance(x, dict)]
        if kind in SAMPLE_SOURCES:
            from .traffic import dns_map_from_rows, ingest_connections
            if kind == 'traffic_dns':
                cache.set(f'tt:linkdns:{router.pk}', dns_map_from_rows(clean_rows), 900)
            else:
                ingest_connections(router, clean_rows, cache.get(f'tt:linkdns:{router.pk}') or {}, now)
            params.update(received=True, row_count=len(clean_rows))
            AgentCommand.objects.filter(pk=cmd.pk).update(params=params)
            cache.delete(f'tt:agent-inventory-rows:{cmd.pk}')
            return len(clean_rows)
        with transaction.atomic():
            RouterConfigSnapshot.objects.get_or_create(router=router)
            snap = RouterConfigSnapshot.objects.select_for_update().get(router=router)
            sections = dict(snap.sections or {})
            src = SOURCES[kind]
            limit = 2000 if kind in TOPOLOGY_KINDS else 500
            sections[src['label']] = {'path': src['path'], 'rows': redact(clean_rows[:limit]), 'count': len(clean_rows), 'transport': 'agent'}
            snap.sections, snap.captured_at = sections, now
            snap.save(update_fields=['sections', 'captured_at', 'updated_at'])
        piece_summary = {}
        try:
            if kind == 'hotspot_user_profiles':
                piece_summary = _profiles(router, clean_rows, now)
            elif kind == 'hotspot_users':
                piece_summary = _users(router, clean_rows, now)
            elif kind == 'hotspot_ip_bindings':
                piece_summary = _bindings(router, clean_rows, now)
            elif kind == 'active_users':
                from .agent import ingest_sessions
                ingest_sessions(router, clean_rows, now)
        except Exception as exc:
            piece_summary = {'errors': [f'{src["label"]}: {exc}']}
        params.update(received=True, row_count=len(clean_rows))
        AgentCommand.objects.filter(pk=cmd.pk).update(params=params)
        cache.delete(f'tt:agent-inventory-rows:{cmd.pk}')
        _record_piece(router, params.get('job_id'), piece_summary)
        return len(clean_rows)
    finally:
        cache.delete(lock)


def _record_piece(router, job_id, piece_summary):
    """Add one table's result to the sync job and finish the job when every table is accounted for."""
    now = timezone.now()
    with transaction.atomic():
        job = RouterSyncJob.objects.select_for_update().filter(pk=job_id, router=router).first()
        if not job or job.status not in {'queued', 'running'}:
            return
        job.summary = _merge_summary(job.summary, piece_summary)
        pieces = [c for c in router.agent_commands.filter(kind='inventory_piece') if (c.params or {}).get('job_id') == job.pk]
        received = [c for c in pieces if (c.params or {}).get('received')]
        failed = [c for c in pieces if not (c.params or {}).get('received') and c.status in ('failed', 'expired', 'cancelled')]
        done = len(received) + len(failed)
        job.summary['inventory_parts'] = len(pieces)
        job.summary['inventory_received'] = len(received)
        job.summary['inventory_skipped'] = len(failed)
        job.progress = min(95, 5 + int(done / max(1, len(pieces)) * 90))
        job.phase = f'TapTap Link inventory: {done}/{len(pieces)} sections'
        finished = pieces and done >= len(pieces)
        if not finished:
            if not job.summary.get('topology_built'):
                accounted = {(c.params or {}).get('kind') for c in received + failed}
                if (TOPOLOGY_KINDS - OPTIONAL_KINDS) <= accounted:
                    snap = RouterConfigSnapshot.objects.filter(router=router).first()
                    if snap:
                        try:
                            _topology_with_alerts(router, snap, now, job)
                        except Exception as exc:
                            job.summary = _merge_summary(job.summary, {'errors': [f'Topology rebuild: {exc}']})
            job.save(update_fields=['progress', 'phase', 'summary', 'updated_at'])
            return
        snap = RouterConfigSnapshot.objects.filter(router=router).first()
        if snap:
            try:
                _topology_with_alerts(router, snap, now, job)
            except Exception as exc:
                job.summary = _merge_summary(job.summary, {'errors': [f'Topology rebuild: {exc}']})
        if not received:
            job.status, job.finished_at = 'failed', now
            job.phase = 'No RouterOS data arrived (router offline or busy) — will retry automatically'
            job.save(update_fields=['status', 'phase', 'summary', 'finished_at', 'updated_at'])
            return
        job.status, job.progress, job.finished_at = 'success', 100, now
        job.phase = 'Synchronization complete through TapTap Link' + (f' ({len(failed)} section(s) not available on this router)' if failed else '')
        job.summary['config_sections'] = len((snap.sections if snap else {}) or {})
        job.save(update_fields=['status', 'progress', 'phase', 'summary', 'finished_at', 'updated_at'])
    Router.objects.filter(pk=router.pk).update(
        status='Online',
        last_error='',
        last_tested_at=now,
        last_watch_at=now,
        connection_mode='agent',
        ip_address='',
        username='',
        password='',
        use_ssl=False,
    )
    if not router.sales_baseline_at:
        Router.objects.filter(pk=router.pk, sales_baseline_at__isnull=True).update(sales_baseline_at=now)
    log(router.business, 'Router Sync', f'{router.name}: full inventory synchronized through TapTap Link')


def inventory_command_ack(cmd, ok):
    """A table RouterOS could not provide is noted, never fatal: the sync still completes."""
    if ok:
        return
    params = cmd.params or {}
    label = SOURCES.get(params.get('kind'), {}).get('label', params.get('kind', 'section'))
    _record_piece(cmd.router, params.get('job_id'), {'errors': [f'{label}: {cmd.result or "not available on this router"}']})


STALL_MINUTES = 12


def settle_stalled_jobs(router=None):
    """Finish Link syncs that stopped making progress."""
    now = timezone.now()
    cutoff = now - timedelta(minutes=STALL_MINUTES)
    jobs = RouterSyncJob.objects.filter(
        status__in=['queued', 'running'],
        updated_at__lt=cutoff,
        router__connection_mode='agent',
    ).select_related('router')
    if router is not None:
        jobs = jobs.filter(router=router)
    settled = 0
    for job in jobs[:50]:
        pieces = [
            c for c in job.router.agent_commands.filter(kind='inventory_piece')
            if (c.params or {}).get('job_id') == job.pk
        ]
        if not pieces:
            RouterSyncJob.objects.filter(pk=job.pk, status__in=['queued', 'running']).update(
                status='failed',
                phase='Sync stopped responding — will retry automatically',
                finished_at=now,
            )
            settled += 1
            continue
        pending = [c for c in pieces if c.status in ('queued', 'sent') and not (c.params or {}).get('received')]
        if pending:
            AgentCommand.objects.filter(pk__in=[c.pk for c in pending]).update(
                status='cancelled',
                done_at=now,
                result='No answer from the router in time',
            )
        _record_piece(
            job.router,
            job.pk,
            {'errors': [f'{len(pending)} section(s) did not arrive in time.'] if pending else []},
        )
        RouterSyncJob.objects.filter(pk=job.pk, status__in=['queued', 'running']).update(
            status='failed',
            phase='No answer from the router — will retry automatically',
            finished_at=now,
        )
        settled += 1
    return settled
