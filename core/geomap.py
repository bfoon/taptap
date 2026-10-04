"""Field mapping: put every MikroTik and access point on a real map, and show how they are connected.

In the field (phone): Topology → **Map a router** (/topology/field/). TapTap first tries to identify the
technician phone on the synchronized RouterOS device inventory. If the phone is learned behind exactly
one router/AP on a MikroTik physical port, that router is selected automatically. If that router is only
an automatic TapTap suggestion, saving its map position confirms/adds it. If the exact AP cannot be
proved, TapTap falls back to site/public-IP/gateway detection and never guesses between ambiguous routers.

The technician can still scan the QR label stuck on the router (Detail → Print map labels), pick manually,
or use the gateway/router shown in the phone's Wi-Fi details. The phone then supplies GPS position and the
technician can drag the pin before saving.

In the office: Topology → **Geo map**: every mapped router at its real place, coloured online/offline,
with lines for the connections the topology knows (ports, stacks, manual placements) and the distance of
each line, "Directions" for the next visit, and the list of routers not mapped yet.
"""
from __future__ import annotations

import ipaddress
import math
import re

from django.utils import timezone

KEY_RE = re.compile(r'^(mt:\d+|sr:\d+|auto:([0-9A-F]{2}:){5}[0-9A-F]{2})$')
WIFI_PORT_PREFIXES = ('wlan', 'wifi', 'cap', 'wl-')


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
    return {
        'lat': obj.geo_lat,
        'lng': obj.geo_lng,
        'accuracy': obj.geo_accuracy,
        'note': obj.geo_note,
        'at': obj.geo_at.isoformat() if obj.geo_at else None,
        'by': (obj.geo_by.get_full_name() or obj.geo_by.email) if obj.geo_by_id else '',
    }


def _private_ipv4(value):
    """Return a normalized private IPv4 address, or '' when it is not usable for phone-path detection."""
    try:
        addr = ipaddress.ip_address(str(value or '').strip())
    except ValueError:
        return ''
    if addr.version != 4 or not addr.is_private or addr.is_loopback or addr.is_link_local:
        return ''
    return str(addr)


def targets(business, include_keys=None):
    """Every router that can be mapped.

    Normally automatic suggestions are shown only when TapTap rates them as "likely".
    ``include_keys`` lets the phone-path detector temporarily expose one exact automatic
    candidate even when its generic router score was only "possible". The candidate is
    not written to the database until the technician actually saves its map position.
    """
    from .site_routers import collect

    forced = set(include_keys or ())
    out = []
    routers = {r.pk: r for r in business.routers.select_related('geo_by').order_by('name')}

    for r in routers.values():
        out.append({
            'key': f'mt:{r.pk}',
            'kind': 'mikrotik',
            'name': r.name,
            'ip': r.ip_address,
            'online': r.status == 'Online',
            'detail': 'MikroTik',
            'site_id': r.pk,
            'site': r.name,
            'geo': _geo(r),
        })

    saved = {s.pk: s for s in business.site_routers.select_related('geo_by')}
    for e in collect(business):
        if e['status'] != 'confirmed' and e.get('confidence') != 'likely' and e['key'] not in forced:
            continue
        s = saved.get(e['id']) if e.get('id') else None
        site = routers.get(e.get('router_id'))
        out.append({
            'key': e['key'],
            'kind': 'router',
            'name': e['name'],
            'ip': e.get('ip') or '',
            'online': bool(e.get('online')),
            'detail': ' '.join(x for x in (e.get('brand'), e.get('model')) if x) or 'Wi-Fi router',
            'site_id': e.get('router_id'),
            'site': site.name if site else '',
            'port': e.get('port') or '',
            'suggested': e['status'] != 'confirmed',
            'geo': _geo(s),
            'parent_key': e.get('parent_key') or '',
        })
    return out


def auto_pick(items, site):
    """(key, why): conservative site-level fallback for "Map a router".

    Exact phone/AP matching is handled by :func:`phone_target` first. This function is
    only the fallback when TapTap can identify the site but cannot prove the exact AP.
    """
    if site is None:
        return '', ''

    on_site = [t for t in items if t.get('site_id') == site.pk]
    mt = next((t for t in on_site if t['key'] == f'mt:{site.pk}'), None)

    if mt and not mt['geo']:
        return (
            mt['key'],
            f'You are on {site.name}’s network. TapTap could identify the site but not prove a separate access point, so the MikroTik is selected.',
        )

    rest = [t for t in on_site if t['kind'] != 'mikrotik' and not t['geo']]
    rest.sort(key=lambda t: (not t.get('online'), t.get('suggested', False), t['name'].lower()))

    if rest:
        return (
            rest[0]['key'],
            f'You are on {site.name}’s network and {site.name} is already mapped — this is the next unmapped router on that site. Confirm it before saving if TapTap could not detect the exact AP.',
        )

    if mt:
        return mt['key'], f'You are on {site.name}’s network (already mapped — saving moves it).'

    return '', ''


def sites_from_ip(business, ip):
    """Every MikroTik the phone may be connected through from the request's Internet address.

    Several MikroTiks can legitimately share one public address behind the same ISP modem,
    so this function returns all candidates rather than silently choosing the first.
    """
    from .models import RouterAgent

    ip = str(ip or '').strip()
    if not ip:
        return []

    out = list(business.routers.filter(ip_address=ip))
    out += [
        a.router
        for a in RouterAgent.objects.filter(
            router__business=business,
            last_ip=ip,
        ).select_related('router')
        if a.router not in out
    ]
    return out


def site_from_gateway(business, gateway):
    """Find the MikroTik owning the phone's Wi-Fi gateway/router address."""
    from .models import RouterConfigSnapshot

    try:
        gw = ipaddress.ip_address(str(gateway or '').strip())
    except ValueError:
        return None

    inside = []
    for snap in RouterConfigSnapshot.objects.filter(
        router__business=business
    ).select_related('router'):
        for row in (((snap.sections or {}).get('IP addresses') or {}).get('rows') or []):
            try:
                iface = ipaddress.ip_interface(str(row.get('address', '')))
            except ValueError:
                continue

            if iface.ip == gw:
                return snap.router

            if gw in iface.network and iface.network.prefixlen < 32:
                inside.append(snap.router)

    inside = list(dict.fromkeys(inside))
    return inside[0] if len(inside) == 1 else None


def site_from_ip(business, ip):
    """The first MikroTik whose Internet address matches the browser request.

    Kept for compatibility. New field mapping code uses :func:`sites_from_ip` because a
    public IP can be shared by several routers.
    """
    from .models import RouterAgent

    ip = str(ip or '').strip()
    if not ip:
        return None

    r = business.routers.filter(ip_address=ip).first()
    if r:
        return r

    a = RouterAgent.objects.filter(
        router__business=business,
        last_ip=ip,
    ).select_related('router').first()
    return a.router if a else None


def phone_target(business, local_ip, site=None):
    """Identify the topology router/AP carrying the technician phone.

    The browser can sometimes expose its private Wi-Fi IPv4 through a host ICE candidate.
    TapTap never trusts that value by itself. It correlates the address with the *server-side*
    RouterDevice inventory already synchronized from RouterOS.

    Detection rules:
      1. The phone IP must match an online RouterDevice belonging to this business.
      2. If a site is already known, only device rows from that MikroTik are considered.
      3. If no site is known, the phone IP must identify exactly one MikroTik.
      4. If the device is learned directly on a MikroTik Wi-Fi interface, select that MikroTik.
      5. Otherwise, inspect confirmed/suggested routers learned on the same physical bridge port.
         Exactly one candidate is required. Multiple candidates are deliberately left ambiguous.
      6. An ``auto:MAC`` candidate is returned without writing anything. geomap.save() already
         promotes it to a confirmed SiteRouter when the technician saves the location.

    Returns a dict so the view can expose a useful explanation without duplicating logic.
    """
    from .models import RouterDevice
    from .site_routers import collect, mac_norm, physical_port

    ip = _private_ipv4(local_ip)
    result = {
        'ip': ip,
        'key': '',
        'site': site,
        'why': '',
        'port': '',
        'auto_add': False,
        'ambiguous': False,
    }
    if not ip:
        return result

    qs = RouterDevice.objects.filter(
        router__business=business,
        ip_address=ip,
        is_online=True,
    ).select_related('router').order_by('-last_seen_at')

    if site is not None:
        devices = list(qs.filter(router=site))
    else:
        devices = list(qs)

    if not devices:
        return result

    router_ids = {d.router_id for d in devices}
    if len(router_ids) != 1:
        result['ambiguous'] = True
        return result

    # Prefer the newest row if the same phone IP is present more than once for the same router.
    phone = devices[0]
    result['site'] = phone.router

    port = physical_port(phone)
    result['port'] = port
    port_l = port.lower()
    phone_mac = mac_norm(phone.mac_address)

    # A client learned directly on wlan/wifi/cap belongs to the managed MikroTik itself.
    if (
        str(phone.connection_type or '').lower() == 'wifi'
        or port_l.startswith(WIFI_PORT_PREFIXES)
    ):
        result['key'] = f'mt:{phone.router_id}'
        result['why'] = (
            f'TapTap found this phone at {ip} directly on {phone.router.name}'
            + (f' ({port})' if port else '')
            + '.'
        )
        return result

    if not port:
        return result

    # The same physical MikroTik port is the strongest evidence available for a transparent
    # AP/bridge: the phone and the AP MAC are both learned behind that port.
    same_port = []
    for entry in collect(business):
        if entry.get('router_id') != phone.router_id:
            continue
        if str(entry.get('port') or '').strip() != port:
            continue
        if phone_mac and mac_norm(entry.get('mac')) == phone_mac:
            continue
        if entry.get('status') != 'confirmed' and entry.get('confidence') not in ('likely', 'possible'):
            continue
        same_port.append(entry)

    # collect() already folds sibling MACs from the same physical unit. Still dedupe by key
    # because a stale inventory row should never create two choices for the same topology node.
    unique = {}
    for entry in same_port:
        unique[entry['key']] = entry
    same_port = list(unique.values())

    if len(same_port) != 1:
        if len(same_port) > 1:
            result['ambiguous'] = True
        return result

    entry = same_port[0]
    result['key'] = entry['key']
    result['auto_add'] = entry['key'].startswith('auto:')
    action = 'TapTap will add it when you save its map position.' if result['auto_add'] else 'It is already in your router list.'
    result['why'] = (
        f'TapTap found this phone at {ip} behind {entry["name"]} on '
        f'{phone.router.name} {port}. {action}'
    )
    return result


def _obj(business, key, user=None, create=False):
    from .models import SiteRouter
    from .net_vendors import brand_of
    from .site_routers import collect

    if key.startswith('mt:'):
        return business.routers.filter(pk=key[3:]).first()

    if key.startswith('sr:'):
        return business.site_routers.filter(pk=key[3:]).first()

    if key.startswith('auto:') and create:
        # Mapping an exact/suggested router confirms it in the owner's list.
        e = next((x for x in collect(business) if x['key'] == key), None)
        if not e:
            return None

        s = (
            business.site_routers.filter(mac_address=e['mac']).first()
            or SiteRouter(
                business=business,
                mac_address=e['mac'],
                created_by=user,
            )
        )
        s.status = 'confirmed'
        s.brand = s.brand or e.get('brand') or brand_of(e['mac'])
        s.ip_address = s.ip_address or e.get('ip') or ''
        s.name = s.name or (e.get('name') or '')[:120]

        # Preserve the physical relationship TapTap used to identify it.
        if not s.router_id and e.get('router_id'):
            s.router_id = e['router_id']
        if not s.port and e.get('port'):
            s.port = str(e['port'])[:120]

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
        accuracy = (
            max(0, min(100000, int(float(accuracy))))
            if accuracy not in (None, '')
            else None
        )
    except (TypeError, ValueError):
        accuracy = None

    obj = _obj(business, key, user, create=True)
    if obj is None:
        raise ValueError('That router is not one of yours any more.')

    type(obj).objects.filter(pk=obj.pk).update(
        geo_lat=round(lat, 7),
        geo_lng=round(lng, 7),
        geo_accuracy=accuracy,
        geo_note=str(note or '')[:160],
        geo_at=timezone.now(),
        geo_by=user if getattr(user, 'is_authenticated', False) else None,
    )

    name = getattr(obj, 'name', '') or str(obj)
    log(
        business,
        'Topology',
        f'Mapped {name} at {lat:.5f}, {lng:.5f}'
        + (f' (±{accuracy} m)' if accuracy else ''),
    )

    new_key = f'mt:{obj.pk}' if key.startswith('mt:') else f'sr:{obj.pk}'
    return new_key, name


def clear(business, key):
    obj = _obj(business, key)
    if obj is not None:
        type(obj).objects.filter(pk=obj.pk).update(
            geo_lat=None,
            geo_lng=None,
            geo_accuracy=None,
            geo_note='',
            geo_at=None,
            geo_by=None,
        )


def payload(business):
    """For the Geo map: points, connection lines and what is not mapped yet."""
    from .topology_links import parent_of

    items = targets(business)
    by_key = {t['key']: t for t in items}
    lines = []

    def line(a_key, b_key, how):
        a, b = by_key.get(a_key), by_key.get(b_key)
        if not a or not b or not a['geo'] or not b['geo']:
            return

        d = haversine_m(
            a['geo']['lat'],
            a['geo']['lng'],
            b['geo']['lat'],
            b['geo']['lng'],
        )
        lines.append({
            'from': a_key,
            'to': b_key,
            'how': how,
            'online': a['online'] and b['online'],
            'distance_m': round(d),
            'distance': distance_text(d),
            'path': [
                [a['geo']['lat'], a['geo']['lng']],
                [b['geo']['lat'], b['geo']['lng']],
            ],
        })

    for t in items:
        if t['kind'] == 'router':
            if t.get('parent_key'):
                line(
                    t['parent_key'],
                    t['key'],
                    'behind ' + (by_key.get(t['parent_key'], {}).get('name') or 'a router'),
                )
            elif t.get('site_id'):
                line(
                    f'mt:{t["site_id"]}',
                    t['key'],
                    t.get('port') or 'cable',
                )

    for r in business.routers.all():
        kind, pk, port = parent_of(r)
        if kind == 'router':
            line(f'mt:{pk}', f'mt:{r.pk}', port or 'link')
        elif kind == 'site':
            line(
                f'sr:{pk}',
                f'mt:{r.pk}',
                'behind ' + (by_key.get(f'sr:{pk}', {}).get('name') or 'a router'),
            )

    mapped = [t for t in items if t['geo']]
    return {
        'points': mapped,
        'lines': lines,
        'unmapped': [t for t in items if not t['geo']],
        'counts': {
            'mapped': len(mapped),
            'total': len(items),
        },
    }
