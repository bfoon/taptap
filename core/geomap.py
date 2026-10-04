"""Field mapping: put every MikroTik and access point on a real map, and show how they are connected.

In the field (phone): Topology → **Map a router** (/topology/field/). The technician scans the QR label
stuck on the router (Detail → Print map labels) — or picks it from the list, the site's routers first
(recognised from the internet address the phone comes from) — taps **Use my location**, drags the pin if
needed and saves. A router TapTap only suggested is confirmed on the way (standing next to it is the best
confirmation there is).

In the office: Topology → **Geo map**: every mapped router at its real place, coloured online/offline,
with lines for the connections the topology knows (ports, stacks, manual placements) and the distance of
each line, "Directions" for the next visit, and the list of routers not mapped yet.
"""
from __future__ import annotations

import math
import re

from django.utils import timezone

KEY_RE = re.compile(r'^(mt:\d+|sr:\d+|auto:([0-9A-F]{2}:){5}[0-9A-F]{2})$')


def haversine_m(a_lat, a_lng, b_lat, b_lng):
    r = 6371000.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lng - a_lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def distance_text(m):
    return f'{m / 1000:.2f} km' if m >= 1000 else f'{round(m)} m'


def _geo(obj):
    if obj is None or obj.geo_lat is None or obj.geo_lng is None:
        return None
    return {'lat': obj.geo_lat, 'lng': obj.geo_lng, 'accuracy': obj.geo_accuracy, 'note': obj.geo_note,
            'at': obj.geo_at.isoformat() if obj.geo_at else None, 'by': (obj.geo_by.get_full_name() or obj.geo_by.email) if obj.geo_by_id else ''}


def targets(business):
    """Every router that can be mapped: the MikroTiks, then the routers TapTap knows behind them."""
    from .site_routers import collect
    out = []
    routers = {r.pk: r for r in business.routers.select_related('geo_by').order_by('name')}
    for r in routers.values():
        out.append({'key': f'mt:{r.pk}', 'kind': 'mikrotik', 'name': r.name, 'ip': r.ip_address, 'online': r.status == 'Online',
                    'detail': 'MikroTik', 'site_id': r.pk, 'site': r.name, 'geo': _geo(r)})
    saved = {s.pk: s for s in business.site_routers.select_related('geo_by')}
    for e in collect(business):
        if e['status'] != 'confirmed' and e.get('confidence') != 'likely':
            continue
        s = saved.get(e['id']) if e.get('id') else None
        site = routers.get(e.get('router_id'))
        out.append({'key': e['key'], 'kind': 'router', 'name': e['name'], 'ip': e.get('ip') or '', 'online': bool(e.get('online')),
                    'detail': ' '.join(x for x in (e.get('brand'), e.get('model')) if x) or 'Wi-Fi router',
                    'site_id': e.get('router_id'), 'site': site.name if site else '', 'port': e.get('port') or '',
                    'suggested': e['status'] != 'confirmed', 'geo': _geo(s), 'parent_key': e.get('parent_key') or ''})
    return out


def auto_pick(items, site):
    """(key, why): the router to preselect on "Map a router" — from what the routers tell TapTap.

    The phone's connection comes through the site's MikroTik (its internet address), so that MikroTik is
    picked when it is not on the map yet; otherwise the first router behind it that is not mapped (the one
    most likely next to you). Nothing is picked when the phone is not on one of your sites."""
    if site is None:
        return '', ''
    on_site = [t for t in items if t.get('site_id') == site.pk]
    mt = next((t for t in on_site if t['key'] == f'mt:{site.pk}'), None)
    if mt and not mt['geo']:
        return mt['key'], f'You are on {site.name}’s Wi-Fi — your phone’s connection comes through this MikroTik.'
    rest = [t for t in on_site if t['kind'] != 'mikrotik' and not t['geo']]
    rest.sort(key=lambda t: (not t.get('online'), t.get('suggested', False), t['name'].lower()))
    if rest:
        return rest[0]['key'], f'You are on {site.name}’s Wi-Fi and {site.name} is already mapped — this is the next router behind it that is not on the map.'
    if mt:
        return mt['key'], f'You are on {site.name}’s Wi-Fi (already mapped — saving moves it).'
    return '', ''


def site_from_ip(business, ip):
    """The MikroTik whose internet address the phone is coming from (it is on that site's Wi-Fi)."""
    from .models import RouterAgent
    ip = str(ip or '').strip()
    if not ip:
        return None
    r = business.routers.filter(ip_address=ip).first()
    if r:
        return r
    a = RouterAgent.objects.filter(router__business=business, last_ip=ip).select_related('router').first()
    return a.router if a else None


def _obj(business, key, user=None, create=False):
    from .models import SiteRouter
    from .net_vendors import brand_of
    from .site_routers import collect
    if key.startswith('mt:'):
        return business.routers.filter(pk=key[3:]).first()
    if key.startswith('sr:'):
        return business.site_routers.filter(pk=key[3:]).first()
    if key.startswith('auto:') and create:            # mapping a suggested router confirms it
        e = next((x for x in collect(business) if x['key'] == key), None)
        if not e:
            return None
        s = business.site_routers.filter(mac_address=e['mac']).first() or SiteRouter(business=business, mac_address=e['mac'], created_by=user)
        s.status = 'confirmed'
        s.brand = s.brand or e.get('brand') or brand_of(e['mac'])
        s.ip_address = s.ip_address or e.get('ip') or ''
        s.name = s.name or (e.get('name') or '')[:120]
        s.save()
        return s
    return None


def save(business, key, lat, lng, accuracy=None, note='', user=None):
    """Store where a router stands. Returns (key, name). Raises ValueError for bad input."""
    from .utils import log
    if not KEY_RE.match(str(key or '')):
        raise ValueError('Choose a router first.')
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        raise ValueError('The location is missing — tap “Use my location” again.')
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        raise ValueError('That location is not valid.')
    try:
        accuracy = max(0, min(100000, int(float(accuracy)))) if accuracy not in (None, '') else None
    except (TypeError, ValueError):
        accuracy = None
    obj = _obj(business, key, user, create=True)
    if obj is None:
        raise ValueError('That router is not one of yours any more.')
    type(obj).objects.filter(pk=obj.pk).update(geo_lat=round(lat, 7), geo_lng=round(lng, 7), geo_accuracy=accuracy,
                                               geo_note=str(note or '')[:160], geo_at=timezone.now(),
                                               geo_by=user if getattr(user, 'is_authenticated', False) else None)
    name = getattr(obj, 'name', '') or str(obj)
    log(business, 'Topology', f'Mapped {name} at {lat:.5f}, {lng:.5f}' + (f' (±{accuracy} m)' if accuracy else ''))
    new_key = f'mt:{obj.pk}' if key.startswith('mt:') else f'sr:{obj.pk}'
    return new_key, name


def clear(business, key):
    obj = _obj(business, key)
    if obj is not None:
        type(obj).objects.filter(pk=obj.pk).update(geo_lat=None, geo_lng=None, geo_accuracy=None, geo_note='', geo_at=None, geo_by=None)


def payload(business):
    """For the Geo map: points, connection lines (with distance) and what is not mapped yet."""
    from .topology_links import parent_of
    items = targets(business)
    by_key = {t['key']: t for t in items}
    lines = []

    def line(a_key, b_key, how):
        a, b = by_key.get(a_key), by_key.get(b_key)
        if not a or not b or not a['geo'] or not b['geo']:
            return
        d = haversine_m(a['geo']['lat'], a['geo']['lng'], b['geo']['lat'], b['geo']['lng'])
        lines.append({'from': a_key, 'to': b_key, 'how': how, 'online': a['online'] and b['online'],
                      'distance_m': round(d), 'distance': distance_text(d),
                      'path': [[a['geo']['lat'], a['geo']['lng']], [b['geo']['lat'], b['geo']['lng']]]})

    for t in items:
        if t['kind'] == 'router':
            if t.get('parent_key'):
                line(t['parent_key'], t['key'], 'behind ' + (by_key.get(t['parent_key'], {}).get('name') or 'a router'))
            elif t.get('site_id'):
                line(f'mt:{t["site_id"]}', t['key'], t.get('port') or 'cable')
    for r in business.routers.all():
        kind, pk, port = parent_of(r)
        if kind == 'router':
            line(f'mt:{pk}', f'mt:{r.pk}', port or 'link')
        elif kind == 'site':
            line(f'sr:{pk}', f'mt:{r.pk}', 'behind ' + (by_key.get(f'sr:{pk}', {}).get('name') or 'a router'))
    mapped = [t for t in items if t['geo']]
    return {'points': mapped, 'lines': lines, 'unmapped': [t for t in items if not t['geo']],
            'counts': {'mapped': len(mapped), 'total': len(items)}}
