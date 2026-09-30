"""Routers and access points TapTap does not manage (mostly TP-Link) — find them and place them.

MikroTik only tells us about its own neighbours (MNDP/LLDP). A TP-Link plugged into a port is,
from the MikroTik's point of view, just another device in the inventory (``RouterDevice``): a MAC
learned on a bridge port, often with a DHCP lease and a host name. This module turns those rows
into routers:

1. **Signature** — each device gets a score from several signals, and the reasons are kept so the
   owner can see *why* TapTap thinks it is a router:
   * the MAC belongs to a router maker (IEEE registry, core/net_vendors.py);
   * the host name looks like a router (``TL-WR840N``, ``Archer_C6``, ``Deco``…) or clearly
     like something else (``Tapo_C200`` camera, ``iPhone``, ``android-…``);
   * the DHCP client is router firmware (``udhcp``) or a phone/PC;
   * it uses a gateway-like address (``192.168.0.1``, ``….254``);
   * other devices are learned on the same MikroTik port: customers connect through it.
   A brand alone is never enough — TP-Link also sells Tapo cameras and smart plugs.
2. **Owner's list** (``SiteRouter``) — confirmed routers (from a suggestion, by IP or by hand),
   and devices marked "not a router" so they are never suggested again. The owner can rename,
   set the maker/model, the MikroTik port and which router it is connected to.
3. **Linking** — a router hangs from the MikroTik port it was learned on (the bridge's
   ``on-interface``, not the bridge name). Customers learned on that port go under it. Two or more
   routers on one port without a known order hang from a "shared cable / switch" node, unless the
   owner says which one feeds which.

Everything is read from the database, so the topology page stays instant.
"""
from __future__ import annotations

import ipaddress
import re
from collections import defaultdict

from .net_vendors import MIXED_BRANDS, ROUTER_BRANDS, brand_of, is_random_mac

LIKELY, POSSIBLE = 60, 35
ROUTER_HOST = re.compile(r'tl-|tl_|archer|deco|tp-?link|tplink|mercusys|tenda|netis|cudy|totolink|router|\bap\b|^ap[-_ ]|'
                         r'access.?point|repeater|extender|mesh|\bwr\d{3}|\bre\d{3}|\beap\d|\bcpe\d|nanostation|nanobeam|litebeam|'
                         r'\bloco|unifi|\bont\b|hg8\d|f6\d\d|wifi.?\d|wlan|hotspot', re.I)
NOT_ROUTER_HOST = re.compile(r'tapo|kasa|cam(era)?\b|\bc\d{3}\b|plug|bulb|light|iphone|ipad|android|galaxy|redmi|xiaomi|oppo|'
                             r'infinix|tecno|itel|samsung|huawei-|honor|vivo|realme|pixel|laptop|desktop|\bpc\b|macbook|'
                             r'windows|tv\b|printer|watch|phone', re.I)
PHONE_DHCP = re.compile(r'android-dhcp|msft|dhcpcd', re.I)
ROUTER_DHCP = re.compile(r'udhcp', re.I)
WIFI_PORT = ('wlan', 'wifi', 'cap', 'wl-')


def mac_norm(value):
    return str(value or '').strip().upper().replace('-', ':')


def physical_port(dev):
    """The MikroTik port a device was learned on: the bridge host's on-interface (ARP overwrites
    ``interface_name`` with the bridge name), else the stored interface."""
    raw = dev.raw_data or {}
    b = raw.get('bridge') or {}
    w = raw.get('wifi') or {}
    return str(b.get('on-interface') or b.get('on_interface') or w.get('interface') or dev.interface_name or '').strip()


def _dhcp_class(dev):
    d = (dev.raw_data or {}).get('dhcp') or {}
    return str(d.get('class-id') or d.get('class_id') or '')


def _gatewayish(ip):
    try:
        a = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return False
    last = int(str(a).rsplit('.', 1)[-1]) if a.version == 4 else -1
    return a.is_private and last in (1, 254)


def score(dev, peers_on_port=0):
    """(score, reasons, brand, mode_guess) for one inventory device."""
    pts, why = 0, []
    mac = mac_norm(dev.mac_address)
    brand = brand_of(mac)
    host = str(dev.hostname or '')
    if is_random_mac(mac):
        pts -= 50; why.append(('-', 'Private (random) MAC — phones and laptops do this, routers do not'))
    if brand in ROUTER_BRANDS:
        pts += 50; why.append(('+', f'MAC belongs to {brand}, a router maker'))
    elif brand in MIXED_BRANDS:
        pts += 15; why.append(('+', f'MAC belongs to {brand} (makes routers and phones)'))
    if host and ROUTER_HOST.search(host) and not NOT_ROUTER_HOST.search(host):
        pts += 40; why.append(('+', f'Name “{host}” looks like a router'))
    elif host and NOT_ROUTER_HOST.search(host):
        pts -= 60; why.append(('-', f'Name “{host}” looks like a phone, computer, camera or plug'))
    cls = _dhcp_class(dev)
    if cls and ROUTER_DHCP.search(cls):
        pts += 15; why.append(('+', f'DHCP client “{cls}” is router firmware'))
    elif cls and PHONE_DHCP.search(cls):
        pts -= 40; why.append(('-', f'DHCP client “{cls}” is a phone or computer'))
    if dev.ip_address and _gatewayish(dev.ip_address):
        pts += 25; why.append(('+', f'Uses a router address ({dev.ip_address})'))
    wifi = dev.connection_type == 'wifi' or physical_port(dev).lower().startswith(WIFI_PORT)
    if peers_on_port and not wifi:
        pts += 20; why.append(('+', f'{peers_on_port} other device{"s" if peers_on_port != 1 else ""} connect through the same port'))
    if wifi:
        pts -= 15; why.append(('-', 'Joined the MikroTik by Wi-Fi (repeaters do this, most routers are cabled)'))
    mode = 'ap' if peers_on_port and not wifi else ('nat' if brand in ROUTER_BRANDS and not peers_on_port and not wifi else '')
    return pts, why, brand, mode


def _confidence(pts):
    return 'likely' if pts >= LIKELY else ('possible' if pts >= POSSIBLE else '')


def collect(business, include_offline=True):
    """Every site router the owner confirmed plus TapTap's suggestions, ready for the map and the list."""
    from .models import RouterDevice, RouterNeighbor, RouterInterface
    routers = {r.id: r for r in business.routers.all()}
    managed_macs = {mac_norm(m) for m in RouterInterface.objects.filter(router__business=business).exclude(mac_address='')
                    .values_list('mac_address', flat=True)}
    neighbour_macs = {mac_norm(m) for m in RouterNeighbor.objects.filter(router__business=business).exclude(mac_address='')
                      .values_list('mac_address', flat=True)}
    qs = RouterDevice.objects.filter(router__business=business).exclude(mac_address='')
    if not include_offline:
        qs = qs.filter(is_online=True)
    # One row per MAC: prefer online, then most recent (a device can be in two routers' tables).
    devices = {}
    for d in qs.order_by('is_online', 'last_seen_at'):
        devices[mac_norm(d.mac_address)] = d
    port_of = {m: (d.router_id, physical_port(d)) for m, d in devices.items()}
    on_port = defaultdict(set)
    for m, key in port_of.items():
        if devices[m].is_online and key[1]:
            on_port[key].add(m)

    saved = list(business.site_routers.select_related('router', 'parent'))
    ignored = {mac_norm(s.mac_address) for s in saved if s.status == 'ignored' and s.mac_address}
    by_ip = defaultdict(list)
    for m, d in devices.items():
        if d.ip_address:
            by_ip[d.ip_address].append(d)

    entries, taken = [], set()

    def entry_from(dev, s=None, pts=None, why=(), brand='', mode=''):
        mac = mac_norm(dev.mac_address) if dev else mac_norm(getattr(s, 'mac_address', ''))
        rid, port = (dev.router_id, physical_port(dev)) if dev else (None, '')
        if s and s.router_id:
            rid = s.router_id
        if s and s.port:
            port = s.port
        brand = (s.brand if s and s.brand else '') or brand or brand_of(mac)
        host = dev.hostname if dev else ''
        name = (s.name if s and s.name else '') or host or (f'{brand} router' if brand else (dev.ip_address if dev else '') or mac or 'Router')
        r = routers.get(rid)
        return {
            'key': f'sr:{s.pk}' if s else f'auto:{mac}', 'id': s.pk if s else None,
            'status': 'confirmed' if s else 'suggested', 'confidence': None if s else _confidence(pts),
            'source': s.source if s else 'auto', 'mac': mac, 'ip': (dev.ip_address if dev else '') or (s.ip_address if s else ''),
            'hostname': host, 'name': name, 'brand': brand, 'model': s.model if s else '',
            'role': s.role if s else 'router', 'mode': (s.mode if s and s.mode else '') or '', 'mode_guess': mode,
            'router_id': rid, 'router_name': r.name if r else '', 'port': port,
            'parent_key': f'sr:{s.parent_id}' if s and s.parent_id else '',
            'online': bool(dev and dev.is_online), 'seen': bool(dev),
            'last_seen': dev.last_seen_at.isoformat() if dev and dev.last_seen_at else None,
            'score': pts, 'reasons': [{'sign': a, 'text': b} for a, b in why], 'notes': s.notes if s else '',
            'candidates': [],
        }

    for s in saved:
        if s.status != 'confirmed':
            continue
        mac = mac_norm(s.mac_address)
        dev = devices.get(mac) if mac else None
        cands = []
        if not dev and not mac and s.ip_address:
            cands = by_ip.get(s.ip_address, [])
            if len(cands) == 1:           # the IP points at exactly one device: remember its MAC from now on
                dev = cands[0]; mac = mac_norm(dev.mac_address)
                if mac not in taken and not type(s).objects.filter(business=business, mac_address=mac).exclude(pk=s.pk).exists():
                    type(s).objects.filter(pk=s.pk).update(mac_address=mac)
                cands = []
        peers = len(on_port.get(port_of.get(mac, (None, '')), set()) - {mac}) if dev else 0
        pts, why, brand, mode = score(dev, peers) if dev else (None, [], '', '')
        e = entry_from(dev, s, pts, why, brand, mode)
        e['candidates'] = [{'mac': mac_norm(c.mac_address), 'ip': c.ip_address, 'hostname': c.hostname, 'brand': brand_of(c.mac_address),
                            'router': routers[c.router_id].name if c.router_id in routers else '', 'port': physical_port(c),
                            'online': c.is_online} for c in cands]
        entries.append(e)
        if mac:
            taken.add(mac)

    for mac, dev in devices.items():
        if mac in taken or mac in ignored or mac in managed_macs or mac in neighbour_macs:
            continue
        peers = len(on_port.get(port_of[mac], set()) - {mac})
        pts, why, brand, mode = score(dev, peers)
        if pts >= POSSIBLE:
            entries.append(entry_from(dev, None, pts, why, brand, mode))

    # Customers per router: devices on its port that are not routers themselves.
    router_macs = {e['mac'] for e in entries if e['mac'] and (e['status'] == 'confirmed' or e['confidence'] == 'likely')}
    for e in entries:
        key = (e['router_id'], e['port'])
        e['clients'] = len([m for m in on_port.get(key, set()) if m not in router_macs]) if e['port'] else 0
    entries.sort(key=lambda e: (e['status'] != 'confirmed', e['confidence'] != 'likely', e['router_name'], e['port'], e['name'].lower()))
    return entries


def on_map(entries):
    """What the topology map draws: confirmed routers and likely suggestions (drawn dashed)."""
    return [e for e in entries if e['status'] == 'confirmed' or e['confidence'] == 'likely']


def find_by_ip(business, text):
    """Devices whose IP matches what the owner typed: one address, a list, or a range
    (192.168.0.1, 192.168.0.1-20, 192.168.0.0/24). Each MAC is its own router — several TP-Links
    can all still answer on 192.168.0.1."""
    from .models import RouterDevice
    wanted = parse_ips(text)
    if not wanted:
        return []
    routers = {r.id: r.name for r in business.routers.all()}
    saved = {mac_norm(s.mac_address): s for s in business.site_routers.exclude(mac_address='')}
    rows, seen = [], set()
    devs = RouterDevice.objects.filter(router__business=business).exclude(ip_address='').order_by('-is_online', '-last_seen_at')
    for d in devs:
        if d.ip_address not in wanted:
            continue
        mac = mac_norm(d.mac_address)
        if (mac or d.ip_address) in seen:
            continue
        seen.add(mac or d.ip_address)
        pts, why, brand, _ = score(d)
        s = saved.get(mac)
        rows.append({'mac': mac, 'ip': d.ip_address, 'hostname': d.hostname, 'brand': brand, 'router_id': d.router_id,
                     'router': routers.get(d.router_id, ''), 'port': physical_port(d), 'online': d.is_online,
                     'score': pts, 'state': s.status if s else ''})
    return rows[:200]


def parse_ips(text):
    out = set()
    for part in re.split(r'[\s,;]+', str(text or '').strip()):
        if not part:
            continue
        try:
            if '/' in part:
                net = ipaddress.ip_network(part, strict=False)
                if net.num_addresses > 1024:
                    continue
                out.update(str(a) for a in (net.hosts() if net.num_addresses > 2 else net))
            elif '-' in part:
                start, end = part.split('-', 1)
                a = ipaddress.ip_address(start)
                b = ipaddress.ip_address(end) if '.' in end else ipaddress.ip_address(start.rsplit('.', 1)[0] + '.' + end)
                if int(b) - int(a) > 1024 or int(b) < int(a):
                    continue
                out.update(str(ipaddress.ip_address(i)) for i in range(int(a), int(b) + 1))
            else:
                out.add(str(ipaddress.ip_address(part)))
        except ValueError:
            continue
    return out
