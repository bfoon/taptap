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
        'control': _control_state(router, name),
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



# ─────────────────────────── port control, reboot, backups ───────────────────────────
import json
from django.http import HttpResponse
from django.views.decorators.http import require_POST
from .models import PortRule, RouterBackup
from .portctl import (RISK_TEXT, backup as do_backup, clear_limit, port_risk, reboot as do_reboot, restart_port,
                      set_limit, set_port, turn_off_for)
from .utils import log


def _control_state(router, name):
    risk = port_risk(router, name)
    rules = []
    for r in router.port_rules.filter(interface=name):
        rules.append({'id': r.id, 'kind': r.kind, 'active': r.active, 'enabled': r.enabled,
                      'limit_down': r.limit_down_mbps, 'limit_up': r.limit_up_mbps, 'threshold': r.threshold_mbps,
                      'direction': r.direction, 'sustain': r.sustain_seconds, 'action': r.action, 'throttle': r.throttle_mbps,
                      'hold': r.hold_minutes, 'times': r.times_triggered, 'error': r.last_error,
                      'triggered_at': r.triggered_at.isoformat() if r.triggered_at else None,
                      'restore_at': r.restore_at.isoformat() if r.restore_at else None})
    last = router.backups.first()
    return {'risk': risk, 'risk_text': RISK_TEXT.get(risk, ''), 'rules': rules, 'auto_backup': router.auto_backup,
            'last_backup': last.created_at.isoformat() if last else None, 'backups': router.backups.count()}


def _num(v, lo, hi, default):
    try:
        return max(lo, min(hi, float(str(v).replace(',', '.'))))
    except (TypeError, ValueError):
        return default


@login_required
@require_POST
def port_action(request, pk):
    router = get_object_or_404(request.user.business.routers, pk=pk)
    name = request.POST.get('name', '').strip()
    action = request.POST.get('action', '')
    if not NAME_RE.match(name) or '..' in name or not router.interfaces.filter(name=name).exists():
        return JsonResponse({'ok': False, 'message': 'Unknown port.'}, status=400)
    if router.connection_mode == 'agent':
        return _port_action_via_link(request, router, name, action)
    risk = port_risk(router, name)
    if action in ('disable', 'restart', 'off_for') and request.POST.get('confirm') != name:
        try:
            with MikroTikService(router, timeout=getattr(settings, 'MIKROTIK_LIVE_TIMEOUT', 5)) as svc:
                risk = port_risk(router, name, svc)   # exact: which port TapTap is connected through right now
        except Exception:
            pass
        if risk:
            return JsonResponse({'ok': False, 'needs_confirm': True, 'message': RISK_TEXT[risk] + f' Type {name} to confirm.'}, status=409)
    if action == 'guard_save' and request.POST.get('guard_action') == 'shutdown' and risk:
        return JsonResponse({'ok': False, 'message': 'This port carries the Internet or TapTap’s own connection, so the guard can only slow it down, not switch it off.'}, status=400)
    msg = ''
    try:
        if action == 'guard_delete':
            rule = router.port_rules.filter(interface=name, kind='guard').first()
            if rule and rule.active:
                with MikroTikService(router, timeout=getattr(settings, 'MIKROTIK_LIVE_TIMEOUT', 5)) as svc:
                    from .portctl import remove_queue, queue_name, cancel_on_router
                    remove_queue(svc, queue_name(name, 'guard'))
                    if rule.action == 'shutdown':
                        set_port(svc, name, True)
                    cancel_on_router(svc, rule.scheduler_name)
            router.port_rules.filter(interface=name, kind='guard').delete()
            msg = 'Traffic guard removed.'
        elif action == 'guard_save':
            PortRule.objects.update_or_create(router=router, interface=name, kind='guard', defaults={
                'threshold_mbps': _num(request.POST.get('threshold'), 0.1, 100000, 20), 'direction': request.POST.get('direction') if request.POST.get('direction') in ('down', 'up', 'any') else 'down',
                'sustain_seconds': int(_num(request.POST.get('sustain'), 15, 3600, 60)), 'action': 'shutdown' if request.POST.get('guard_action') == 'shutdown' else 'throttle',
                'throttle_mbps': _num(request.POST.get('throttle'), 0.1, 100000, 2), 'hold_minutes': int(_num(request.POST.get('hold'), 1, 1440, 10)),
                'enabled': request.POST.get('enabled', 'on') == 'on', 'created_by': request.user, 'last_error': ''})
            msg = 'Traffic guard saved. It is checked every live-sync pass.'
        else:
            with MikroTikService(router, timeout=getattr(settings, 'MIKROTIK_TIMEOUT', 10)) as svc:
                if action == 'enable':
                    set_port(svc, name, True)
                    from .portctl import cancel_on_router
                    cancel_on_router(svc, f'taptap-on-{name}')
                    router.port_rules.filter(interface=name, kind='timed_off').delete()
                    msg = f'{name} is on.'
                elif action == 'disable':
                    # A manual "off" must not be undone by a pending timed-shutdown job on the router.
                    from .portctl import cancel_on_router
                    cancel_on_router(svc, f'taptap-on-{name}')
                    router.port_rules.filter(interface=name, kind='timed_off').delete()
                    set_port(svc, name, False); msg = f'{name} is off. It stays off until you turn it on.'
                elif action == 'restart':
                    from .portctl import cancel_on_router
                    cancel_on_router(svc, f'taptap-on-{name}')
                    router.port_rules.filter(interface=name, kind='timed_off').delete()
                    secs = int(_num(request.POST.get('seconds'), 2, 30, 5))
                    restart_port(svc, name, secs); msg = f'{name} restarted ({secs} s off).'
                elif action == 'off_for':
                    mins = int(_num(request.POST.get('minutes'), 1, 10080, 15))
                    turn_off_for(svc, router, name, mins, request.user)
                    msg = f'{name} is off and will come back on by itself in {mins} min — the router does this even if TapTap is disconnected.'
                elif action == 'limit':
                    down, up = _num(request.POST.get('down'), 0, 100000, 0), _num(request.POST.get('up'), 0, 100000, 0)
                    if not down and not up:
                        return JsonResponse({'ok': False, 'message': 'Enter a download or upload limit.'}, status=400)
                    mins = int(_num(request.POST.get('minutes'), 0, 10080, 0))
                    set_limit(svc, router, name, down, up, mins, request.user)
                    msg = f'Limit set on {name}: {down:g} Mb/s down, {up:g} Mb/s up' + (f' for {mins} min.' if mins else '.')
                elif action == 'unlimit':
                    clear_limit(svc, router, name); msg = f'Limit removed from {name}.'
                else:
                    return JsonResponse({'ok': False, 'message': 'Unknown action.'}, status=400)
                if action in ('enable', 'disable', 'restart', 'off_for'):
                    row = svc.resource('/interface').get(name=name)
                    if row:
                        router.interfaces.filter(name=name).update(disabled=str(row[0].get('disabled', '')).lower() == 'true',
                                                                   running=str(row[0].get('running', '')).lower() == 'true')
        log(router.business, 'Port Control', f'{router.name} {name}: {action} — {msg}')
        return JsonResponse({'ok': True, 'message': msg, 'control': _control_state(router, name)})
    except Exception as exc:
        return JsonResponse({'ok': False, 'message': str(exc)[:300]}, status=502)


def _port_action_via_link(request, router, name, action):
    """Routers behind NAT: queue the change; the router applies it at its next check-in."""
    from . import agent as link
    risk = port_risk(router, name)
    if action in ('disable', 'restart', 'off_for') and risk and request.POST.get('confirm') != name:
        return JsonResponse({'ok': False, 'needs_confirm': True, 'message': RISK_TEXT[risk] + f' Type {name} to confirm.'}, status=409)
    try:
        if action == 'enable':
            link.queue(router, 'interface_set', {'name': name, 'enabled': True}, label=f'Turn on {name}', user=request.user)
        elif action == 'disable':
            link.queue(router, 'interface_set', {'name': name, 'enabled': False}, label=f'Turn off {name}', user=request.user)
        elif action == 'restart':
            link.queue(router, 'port_restart', {'name': name, 'seconds': int(_num(request.POST.get('seconds'), 2, 30, 5))}, label=f'Restart {name}', user=request.user)
        elif action == 'off_for':
            mins = int(_num(request.POST.get('minutes'), 1, 10080, 15))
            link.queue(router, 'port_off_for', {'name': name, 'minutes': mins}, label=f'Turn off {name} for {mins} min', user=request.user)
        elif action == 'limit':
            down, up = _num(request.POST.get('down'), 0, 100000, 0), _num(request.POST.get('up'), 0, 100000, 0)
            link.queue(router, 'limit', {'name': name, 'down': down, 'up': up}, label=f'Limit {name} to {down:g}/{up:g} Mb/s', user=request.user)
            PortRule.objects.update_or_create(router=router, interface=name, kind='limit', defaults={'limit_down_mbps': down, 'limit_up_mbps': up, 'active': True, 'queue_name': f'TapTap limit {name}'})
        elif action == 'unlimit':
            link.queue(router, 'unlimit', {'name': name}, label=f'Remove limit on {name}', user=request.user)
            router.port_rules.filter(interface=name, kind='limit').delete()
        else:
            return JsonResponse({'ok': False, 'message': 'The traffic guard needs a direct API connection — it measures the port every few seconds. Use a speed limit instead.'}, status=400)
    except ValueError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)}, status=400)
    return JsonResponse({'ok': True, 'message': 'Sent through TapTap Link — the router applies it at its next check-in (a few seconds).', 'control': _control_state(router, name)})


@login_required
@require_POST
def router_reboot(request, pk):
    router = get_object_or_404(request.user.business.routers, pk=pk)
    if request.POST.get('confirm', '').strip() != router.name:
        return JsonResponse({'ok': False, 'message': f'Type the router name “{router.name}” to confirm.'}, status=400)
    if router.connection_mode == 'agent':
        from . import agent as link
        link.queue(router, 'reboot', label='Reboot router', user=request.user, minutes=5)
        return JsonResponse({'ok': True, 'message': f'Reboot sent through TapTap Link — {router.name} reboots at its next check-in.'})
    try:
        with MikroTikService(router, timeout=getattr(settings, 'MIKROTIK_TIMEOUT', 10)) as svc:
            do_reboot(svc)
    except Exception as exc:
        if 'closed' not in str(exc).lower():
            return JsonResponse({'ok': False, 'message': str(exc)[:300]}, status=502)
    type(router).objects.filter(pk=router.pk).update(status='Rebooting', last_tested_at=timezone.now())
    log(router.business, 'Router Reboot', f'{router.name} rebooted by {request.user.email}')
    return JsonResponse({'ok': True, 'message': f'{router.name} is rebooting. It is usually back within 1–2 minutes; live sync will mark it online again.'})


@login_required
def router_backups(request, pk):
    router = get_object_or_404(request.user.business.routers, pk=pk)
    if request.method == 'POST':
        if request.POST.get('action') == 'auto':
            router.auto_backup = request.POST.get('on') == '1'; router.save(update_fields=['auto_backup'])
            return JsonResponse({'ok': True, 'message': 'Nightly backups are ' + ('on (between 02:00 and 05:00).' if router.auto_backup else 'off.'), 'auto_backup': router.auto_backup})
        if router.connection_mode == 'agent':
            from . import agent as link
            f = f'taptap-{router.name}-{timezone.localtime():%Y%m%d-%H%M}'.replace(' ', '-')[:60]
            link.queue(router, 'backup', {'file': f}, label='Back up configuration', user=request.user)
            return JsonResponse({'ok': True, 'message': 'Backup sent through TapTap Link. The files are saved on the router (WinBox › Files).'})
        try:
            with MikroTikService(router, timeout=getattr(settings, 'MIKROTIK_TIMEOUT', 10)) as svc:
                rec = do_backup(svc, router, user=request.user)
        except Exception as exc:
            return JsonResponse({'ok': False, 'message': str(exc)[:300]}, status=502)
        log(router.business, 'Router Backup', f'{router.name}: {rec.name}')
        parts = []
        if rec.backup_file: parts.append(f'{rec.backup_file} saved on the router')
        if rec.content: parts.append('export downloaded into TapTap')
        elif rec.export_file: parts.append(f'{rec.export_file} saved on the router (this RouterOS version does not allow downloading it through the API — copy it from Files in WinBox)')
        return JsonResponse({'ok': not rec.error or bool(rec.backup_file or rec.export_file), 'message': ('; '.join(parts) or 'Backup failed') + (f'. Problems: {rec.error}' if rec.error else '.')})
    items = [{'id': b.id, 'name': b.name, 'at': b.created_at.isoformat(), 'backup_file': b.backup_file, 'export_file': b.export_file,
              'size': b.export_size, 'downloadable': bool(b.content), 'automatic': b.automatic, 'error': b.error, 'version': b.ros_version}
             for b in router.backups.all()[:30]]
    return JsonResponse({'ok': True, 'items': items, 'auto_backup': router.auto_backup})


@login_required
def router_backup_download(request, pk, bid):
    router = get_object_or_404(request.user.business.routers, pk=pk)
    b = get_object_or_404(router.backups, pk=bid)
    if b.content:
        resp = HttpResponse(b.content, content_type='text/plain; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="{b.name}.rsc"'
        return resp
    # Fall back to TapTap's own configuration snapshot (readable, but not an importable export).
    try:
        snap = router.config_snapshot
        body = json.dumps({'router': router.name, 'captured_at': snap.captured_at.isoformat(), 'sections': snap.sections}, indent=2, default=str)
    except Exception:
        return HttpResponse('No downloadable copy. The backup files are on the router under Files.', status=404, content_type='text/plain')
    resp = HttpResponse(body, content_type='application/json')
    resp['Content-Disposition'] = f'attachment; filename="{b.name}-taptap-snapshot.json"'
    return resp
