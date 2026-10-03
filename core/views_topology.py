"""Topology → Detail tab: the list of routers (managed MikroTiks and TP-Link & co), confirming or
ignoring what TapTap found, adding routers by IP, and saying how they are linked."""
import json
import re

from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from .models import SiteRouter
from .net_vendors import MIXED_BRANDS, ROUTER_BRANDS, brand_of
from .site_routers import collect, find_by_ip, mac_norm, parse_ips
from .utils import log

PORT_RE = re.compile(r'^[\w./:+@<>-]{1,120}$')
MAC_RE = re.compile(r'^([0-9A-F]{2}:){5}[0-9A-F]{2}$')
QUICK_IPS = ['192.168.0.1', '192.168.1.1', '192.168.0.254', '10.0.0.1']


def _b(request):
    return request.user.business


def detail_payload(business):
    routers = []
    for r in business.routers.all().order_by('name'):
        ports = sorted({i.name for i in r.interfaces.filter(is_present=True)
                        if i.name.lower().startswith(('ether', 'sfp', 'combo', 'wlan', 'wifi', 'cap'))},
                       key=lambda n: (re.sub(r'\d+', '', n), int(re.sub(r'\D', '', n) or 0)))
        from .topology_links import parent_of
        kind, pk, port = parent_of(r)
        routers.append({'id': r.id, 'name': r.name, 'ip': r.ip_address, 'online': r.status == 'Online', 'ports': ports,
                        'devices': r.devices.filter(is_online=True).count(),
                        'uplink': {'kind': kind, 'id': pk, 'port': port}})
    ignored = [{'id': s.pk, 'mac': s.mac_address, 'ip': s.ip_address, 'name': s.name or s.mac_address or s.ip_address}
               for s in business.site_routers.filter(status='ignored')]
    from .topology_links import suggestions
    links = [{'child_id': s['child'].pk, 'child': s['child'].name, 'parent_id': s['parent'].pk, 'parent': s['parent'].name,
              'port': s['port'], 'reasons': s['reasons'], 'score': s['score']} for s in suggestions(business)]
    return {'routers': routers, 'entries': collect(business), 'ignored': ignored, 'links': links,
            'brands': sorted(ROUTER_BRANDS | MIXED_BRANDS) + ['Other'],
            'roles': SiteRouter.ROLES, 'modes': SiteRouter.MODES, 'quick_ips': QUICK_IPS}


def _err(msg, status=400):
    return JsonResponse({'success': False, 'message': msg}, status=status)


@login_required
def topology_routers(request):
    return JsonResponse({'success': True, **detail_payload(_b(request))})


@login_required
def topology_router_find(request):
    business = _b(request)
    text = request.GET.get('ip', '')
    if not parse_ips(text):
        return _err('Type an IP address, a list or a range — e.g. 192.168.0.1 or 192.168.0.1-20.')
    return JsonResponse({'success': True, 'rows': find_by_ip(business, text), 'ips': sorted(parse_ips(text))[:50]})


def _clean_fields(business, data, obj=None):
    """Validated editable fields from the browser."""
    out = {}
    for f, n in (('name', 120), ('model', 80), ('notes', 255)):
        if f in data:
            out[f] = str(data.get(f) or '').strip()[:n]
    if 'brand' in data:
        out['brand'] = str(data.get('brand') or '').strip()[:40]
    if 'role' in data:
        out['role'] = data['role'] if data['role'] in dict(SiteRouter.ROLES) else 'router'
    if 'mode' in data:
        out['mode'] = data['mode'] if data['mode'] in dict(SiteRouter.MODES) else ''
    if 'ip' in data:
        ip = str(data.get('ip') or '').strip()
        if ip and not parse_ips(ip):
            raise ValueError('That IP address is not valid.')
        out['ip_address'] = ip[:64]
    if 'router_id' in data:
        rid = data.get('router_id')
        router = business.routers.filter(pk=rid).first() if str(rid or '').isdigit() else None
        if rid not in (None, '') and not router:
            raise ValueError('Choose one of your MikroTik routers.')
        out['router'] = router
    if 'port' in data:
        port = str(data.get('port') or '').strip()
        if port and not PORT_RE.match(port):
            raise ValueError('That port name is not valid.')
        out['port'] = port
    if 'parent_id' in data:
        pid = data.get('parent_id')
        parent = business.site_routers.filter(pk=pid, status='confirmed').first() if str(pid or '').isdigit() else None
        if pid not in (None, '') and not parent:
            raise ValueError('Choose one of your routers as the one it is connected to.')
        node = parent
        while node is not None:           # no loops: A → B → A
            if obj is not None and node.pk == obj.pk:
                raise ValueError('A router cannot be connected through itself.')
            node = node.parent
        out['parent'] = parent
        if parent:
            out['router'], out['port'] = None, ''     # it follows its parent now
    return out


@login_required
@require_POST
def topology_router_probe(request):
    """Probe one router through its MikroTik (ping + web page) and TapTap's tables. No password."""
    from .router_probe import probe
    business = _b(request)
    try:
        key = str(json.loads(request.body or '{}').get('key') or '')
    except ValueError:
        return _err('Could not read the request.')
    if not re.match(r'^(sr:\d+|auto:([0-9A-F]{2}:){5}[0-9A-F]{2})$', key):
        return _err('Unknown router.')
    try:
        result = probe(business, key, user=request.user)
    except ValueError as exc:
        return _err(str(exc), 404)
    log(business, 'Topology', f'Probed router {result.get("ip") or result.get("mac")}')
    return JsonResponse({'success': True, 'key': key, 'result': result, **detail_payload(business)})


@login_required
@require_POST
def topology_router_action(request):
    business = _b(request)
    try:
        data = json.loads(request.body or '{}')
    except ValueError:
        return _err('Could not read the request.')
    action = data.get('action')
    user = request.user if request.user.is_authenticated else None
    try:
        if action in ('place_router', 'accept_link', 'ignore_link'):
            # MikroTiks on the topology (core/topology_links.py)
            from .topology_links import ignore as ignore_link, place
            r = business.routers.filter(pk=data.get('router_id') or 0).first()
            if not r:
                return _err('That router is not one of yours.', 404)
            if action == 'ignore_link':
                ignore_link(r, int(data.get('parent_id') or 0))
            elif action == 'accept_link':
                place(r, f'router:{int(data.get("parent_id") or 0)}', data.get('port', ''), user)
            else:
                place(r, str(data.get('target') or ''), data.get('port', ''), user)
            return JsonResponse({'success': True, **detail_payload(business)})
        if action in ('confirm', 'ignore') and data.get('mac'):
            mac = mac_norm(data['mac'])
            if not MAC_RE.match(mac):
                return _err('That MAC address is not valid.')
            s = business.site_routers.filter(mac_address=mac).first() or SiteRouter(business=business, mac_address=mac, created_by=user)
            s.status = 'confirmed' if action == 'confirm' else 'ignored'
            if action == 'confirm':
                for k, v in _clean_fields(business, data, s if s.pk else None).items():
                    setattr(s, k, v)
                s.brand = s.brand or brand_of(mac)
                s.ip_address = s.ip_address or str(data.get('ip') or '')[:64]
            s.save()
            log(business, 'Topology', f'{"Confirmed" if action == "confirm" else "Not a router"}: {s}')
        elif action == 'add':
            # From "Find by IP": each ticked device (one MAC each), or an IP that was not seen yet.
            added = 0
            for item in (data.get('items') or [])[:100]:
                mac = mac_norm(item.get('mac'))
                ip = str(item.get('ip') or '').strip()[:64]
                if mac and not MAC_RE.match(mac):
                    continue
                if ip and not parse_ips(ip):
                    continue
                if not mac and not ip:
                    continue
                s = business.site_routers.filter(mac_address=mac).first() if mac else None
                s = s or SiteRouter(business=business, mac_address=mac, created_by=user, source='ip')
                s.status = 'confirmed'; s.ip_address = s.ip_address or ip; s.brand = s.brand or brand_of(mac)
                if not mac and str(data.get('router_id') or '').isdigit():
                    s.router = business.routers.filter(pk=data['router_id']).first()
                s.save(); added += 1
            if not added:
                return _err('Nothing to add — tick at least one device, or type an IP address.')
            log(business, 'Topology', f'Added {added} router(s) by IP')
        elif action in ('update', 'remove', 'bind'):
            s = business.site_routers.filter(pk=data.get('id') or 0).first()
            if not s:
                return _err('That router is no longer in your list.', 404)
            if action == 'remove':
                s.delete()
            elif action == 'bind':
                mac = mac_norm(data.get('mac'))
                if not MAC_RE.match(mac):
                    return _err('That MAC address is not valid.')
                s.mac_address = mac; s.brand = s.brand or brand_of(mac); s.save()
            else:
                fields = _clean_fields(business, data, s)
                if fields.get('router') is not None:          # MikroTiks can hang from site routers: no loops
                    from .topology_links import site_place_check
                    site_place_check(business, s, ('router', fields['router'].pk))
                for k, v in fields.items():
                    setattr(s, k, v)
                s.save()
        else:
            return _err('Unknown action.')
    except ValueError as exc:
        return _err(str(exc))
    except IntegrityError:
        return _err('That device is already in your list.')
    return JsonResponse({'success': True, **detail_payload(business)})
