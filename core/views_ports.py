"""Front-panel port inspector.

GET /routers/<id>/port/?name=ether2          → everything TapTap has saved (instant)
GET /routers/<id>/port/?name=ether2&live=1   → plus live speed, link rate and fresh counters from the router
"""
import re

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone

from .mikrotik import MikroTikService, redact

NAME_RE = re.compile(r'^[\w.@<>/:+-]{1,64}$')
HIDE = {'.id', 'id', '.nextid', 'nextid'}
COUNTER_KEYS = ['rx-byte', 'tx-byte', 'rx-packet', 'tx-packet', 'rx-drop', 'tx-drop', 'rx-error', 'tx-error', 'tx-queue-drop',
                'fp-rx-byte', 'fp-tx-byte', 'link-downs', 'last-link-up-time', 'last-link-down-time']
LIVE_ETH_KEYS = ['status', 'rate', 'full-duplex', 'auto-negotiation', 'advertising', 'link-partner-advertising', 'tx-flow-control',
                 'rx-flow-control', 'sfp-module-present', 'sfp-type', 'sfp-vendor-name', 'sfp-vendor-part-number', 'sfp-wavelength',
                 'sfp-temperature', 'sfp-tx-power', 'sfp-rx-power', 'sfp-supply-voltage', 'poe-out', 'cable-length']


def _clean(row):
    return {str(k).lstrip('.'): v for k, v in (row or {}).items() if k not in HIDE and v not in (None, '')}


def _int(v):
    try:
        return int(str(v).strip() or 0)
    except (TypeError, ValueError):
        return 0


def _rows(snap, label):
    if not snap or not snap.sections:
        return []
    return (snap.sections.get(label) or {}).get('rows') or []


def _related_config(snap, name, bridge):
    """Config sections that mention this port (directly or through its bridge / VLANs)."""
    names = {name} | ({bridge} if bridge else set())
    vlans = [_clean(r) for r in _rows(snap, 'VLAN interfaces') if str(r.get('interface', '')) in names]
    names |= {v.get('name', '') for v in vlans}
    out = []
    add = lambda title, rows: rows and out.append({'title': title, 'rows': rows})
    add('IP addresses', [_clean(r) for r in _rows(snap, 'IP addresses') if str(r.get('interface', r.get('actual-interface', ''))) in names])
    add('VLANs on this port', vlans)
    add('Bridge VLAN table', [_clean(r) for r in _rows(snap, 'Bridge VLANs')
                              if name in str(r.get('tagged', '')).split(',') or name in str(r.get('untagged', '')).split(',')
                              or name in str(r.get('current-tagged', '')).split(',') or name in str(r.get('current-untagged', '')).split(',')])
    add('DHCP server', [_clean(r) for r in _rows(snap, 'DHCP servers') if str(r.get('interface', '')) in names])
    add('DHCP client', [_clean(r) for r in _rows(snap, 'DHCP clients') if str(r.get('interface', '')) in names])
    add('HotSpot server', [_clean(r) for r in _rows(snap, 'HotSpot servers') if str(r.get('interface', '')) in names])
    add('Interface lists', [_clean(r) for r in _rows(snap, 'Interface list members') if str(r.get('interface', '')) in names])
    add('Queues', [_clean(r) for r in _rows(snap, 'Simple queues') if str(r.get('target', '')) in names])
    add('Firewall rules', [_clean(r) for r in _rows(snap, 'Firewall filter') + _rows(snap, 'Firewall NAT') + _rows(snap, 'Firewall mangle')
                           if str(r.get('in-interface', '')).lstrip('!') in names or str(r.get('out-interface', '')).lstrip('!') in names][:30])
    add('PPPoE client', [_clean(r) for r in _rows(snap, 'PPPoE clients') if str(r.get('interface', '')) in names])
    return out


@login_required
def router_port(request, pk):
    router = get_object_or_404(request.user.business.routers, pk=pk)
    name = request.GET.get('name', '').strip()
    if not NAME_RE.match(name) or '..' in name:
        return JsonResponse({'success': False, 'message': 'Unknown port.'}, status=400)
    obj = router.interfaces.filter(name=name).first()
    if not obj:
        return JsonResponse({'success': False, 'message': f'{name} has not been discovered yet. Run discovery first.'}, status=404)
    try:
        snap = router.config_snapshot
    except Exception:
        snap = None
    raw = obj.raw_data or {}
    iface = _clean({k: v for k, v in raw.items() if k not in {'ethernet', 'bridge_port'}})
    eth, bport = _clean(raw.get('ethernet', {})), _clean(raw.get('bridge_port', {}))
    role = router.interface_roles.filter(interface_name=name).first()
    counters = {k: iface.pop(k) for k in COUNTER_KEYS if k in iface}
    counters.setdefault('rx-byte', obj.rx_byte); counters.setdefault('tx-byte', obj.tx_byte)

    devices = [{'name': d.hostname, 'mac': d.mac_address, 'ip': d.ip_address, 'kind': d.connection_type, 'via': d.parent_identity,
                'sources': d.sources, 'online': d.is_online, 'first_seen': d.first_seen_at.isoformat(), 'last_seen': d.last_seen_at.isoformat()}
               for d in router.devices.filter(interface_name=name).order_by('-is_online', 'hostname', 'mac_address')[:500]]
    sig_by_mac = {}
    macs = [d['mac'].upper() for d in devices if d['mac']]
    if macs:
        for s in router.business.device_signatures.filter(last_mac__in=macs).only('last_mac', 'model', 'os', 'label'):
            sig_by_mac[s.last_mac] = s.label or ' '.join(x for x in (s.model, s.os) if x)
    for d in devices:
        d['identified'] = sig_by_mac.get((d['mac'] or '').upper(), '')
    neighbors = [{'identity': n.identity, 'address': n.address, 'mac': n.mac_address, 'board': n.board, 'version': n.version,
                  'online': n.is_online, 'kind': n.device_kind} for n in router.neighbors.filter(interface_name=name).order_by('-is_online')]
    wan = next((l for l in ((snap.load_balancing if snap else {}) or {}).get('wan_links', []) if l.get('interface') == name), None)

    data = {
        'success': True, 'router': {'id': router.id, 'name': router.name, 'status': router.status}, 'name': name,
        'comment': obj.comment, 'type': obj.interface_type, 'mac': obj.mac_address, 'mtu': obj.mtu, 'running': obj.running, 'disabled': obj.disabled,
        'role': role.role if role else 'unused', 'role_label': role.get_role_display() if role else 'Unused', 'bridge': bport.get('bridge', ''),
        'counters': counters, 'saved_at': obj.last_seen_at.isoformat(), 'wan': wan,
        'sections': [s for s in [{'title': 'Interface', 'rows': [iface]}, {'title': 'Ethernet', 'rows': [eth] if eth else []},
                                 {'title': 'Bridge port', 'rows': [bport] if bport else []}] if s['rows']] + _related_config(snap, name, bport.get('bridge', '')),
        'devices': devices, 'neighbors': neighbors, 'live': None,
    }

    if request.GET.get('live') and router.status == 'Online':
        live = {'at': timezone.now().isoformat()}
        try:
            with MikroTikService(router, timeout=getattr(settings, 'MIKROTIK_LIVE_TIMEOUT', 5)) as svc:
                traffic = svc.live_traffic([name]).get(name, {})
                live.update(traffic)
                fresh = [r for r in svc.resource('/interface').get(name=name)]
                if fresh:
                    f = _clean(fresh[0])
                    live['counters'] = {k: f[k] for k in COUNTER_KEYS if k in f}
                    live['running'] = str(f.get('running', '')).lower() == 'true'
                if obj.interface_type.lower() in {'ether', 'ethernet'} or name.startswith(('ether', 'sfp', 'combo', 'qsfp')):
                    try:
                        mon = svc.resource('/interface/ethernet').call('monitor', {'numbers': name, 'once': ''})
                        if mon:
                            m = _clean(mon[0])
                            live['ethernet'] = {k: m[k] for k in LIVE_ETH_KEYS if k in m}
                    except Exception:
                        live['ethernet'] = {}
        except Exception as exc:
            live = {'error': str(exc)[:300]}
        data['live'] = redact(live)
    return JsonResponse(data)
