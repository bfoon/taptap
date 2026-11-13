"""Config Studio tiles: what each tile is, and the RouterOS rows it turns into.

A tile is a sentence with blanks ("Give [ether2] the network [192.168.88.1/24]"). The browser only ever sends a
tile type and the values typed into its blanks. This module checks every value and turns the tile into a list of
*operations* — plain data, never free RouterOS text:

    {"resource": "/ip/address", "action": "ensure", "find": {...}, "values": {...}, "label": "...", "tile": "t3"}

    ensure          the row matching ``find`` gets ``values`` (it is created when it does not exist)
    set             the row matching ``find`` must exist and gets ``values``
    singleton_set   a menu with one fixed row (/ip/dns, /system/identity …)
    remove_matching rows matching ``find`` are removed

``place: "top"`` puts a new firewall rule above the first rule of its chain. The simulator
(config_studio_sim), the code panel and the deploy engine (config_studio) all read the same operations.
"""
from __future__ import annotations

import ipaddress
import re
import shlex

TAG = 'TapTap-Studio:'
PRIVATE_LIST = 'tt-studio-private'
BLOCKED_LIST = 'tt-studio-blocked-sites'
NAME_RE = re.compile(r'^[A-Za-z0-9_.+@-][A-Za-z0-9_.+@ -]{0,59}$')
HOST_RE = re.compile(r'^(\*\.)?([A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,24}$')
SPEED_RE = re.compile(r'^(\d{1,6})(?:\.(\d))?\s*([kKmMgG])?(?:bps|b)?$')
KEY_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,40}$')
TIME_RE = re.compile(r'^\d{1,3}[smhd]$|^\d{1,2}:\d{2}:\d{2}$')
MAX_TILES = 60


class TileError(ValueError):
    """A value typed into a tile is not usable. ``field`` names the blank."""

    def __init__(self, message, field=''):
        super().__init__(message)
        self.field = field


# ───────────────────────────── values ─────────────────────────────
def _clean(value, field, limit=120):
    text = str(value if value is not None else '').strip()
    if len(text) > limit or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise TileError(f'{field}: too long or contains characters RouterOS cannot store.', field)
    return text


def v_name(value, field='name'):
    text = _clean(value, field, 60)
    if not NAME_RE.fullmatch(text):
        raise TileError(f'{field}: use letters, numbers, dots and dashes (for example ether2 or bridge-guest).', field)
    return text


def v_ip(value, field='address'):
    try:
        return str(ipaddress.IPv4Address(_clean(value, field)))
    except ValueError:
        raise TileError(f'{field}: “{value}” is not an IP address (for example 192.168.88.10).', field)


def v_gateway(value, field='network'):
    """An address with its size, as typed on a port: 192.168.88.1/24."""
    text = _clean(value, field)
    try:
        iface = ipaddress.IPv4Interface(text)
    except ValueError:
        raise TileError(f'{field}: write the router address and size, for example 192.168.88.1/24.', field)
    net = iface.network
    if '/' not in text or net.prefixlen > 30 or net.prefixlen < 8:
        raise TileError(f'{field}: add the network size after the address, for example 192.168.88.1/24.', field)
    if iface.ip in (net.network_address, net.broadcast_address):
        raise TileError(f'{field}: {iface.ip} is the first or last address of {net} and cannot be used by the router.', field)
    return iface


def v_net(value, field='network'):
    try:
        return str(ipaddress.IPv4Network(_clean(value, field), strict=False))
    except ValueError:
        raise TileError(f'{field}: “{value}” is not a network (for example 192.168.88.0/24).', field)


def v_target(value, field='who'):
    """An address or a network: 192.168.88.50 or 192.168.88.0/24."""
    text = _clean(value, field)
    return v_net(text, field) if '/' in text else v_ip(text, field) + '/32'


def v_mac(value, field='device'):
    text = re.sub(r'[^0-9A-Fa-f]', '', _clean(value, field))
    if len(text) != 12:
        raise TileError(f'{field}: a MAC address has 12 characters, for example AA:BB:CC:11:22:33.', field)
    return ':'.join(text[i:i + 2] for i in range(0, 12, 2)).upper()


def v_int(value, field, low, high):
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise TileError(f'{field}: a number between {low} and {high} is needed.', field)
    if not low <= n <= high:
        raise TileError(f'{field}: must be between {low} and {high}.', field)
    return n


def v_speed(value, field='speed'):
    m = SPEED_RE.fullmatch(_clean(value, field))
    if not m or int(m.group(1)) == 0:
        raise TileError(f'{field}: write a speed such as 5M, 512k or 1G.', field)
    whole, tenth, unit = m.group(1), m.group(2), (m.group(3) or 'M')
    unit = 'k' if unit in 'kK' else unit.upper()
    if tenth and tenth != '0':                 # RouterOS has no decimals: 1.5M → 1500k
        if unit == 'k':
            return f'{whole}k'
        return f'{int(whole) * 1000 + int(tenth) * 100}{"k" if unit == "M" else "M"}'
    return f'{int(whole)}{unit}'


def v_host(value, field='site'):
    text = _clean(value, field).lower()
    text = re.sub(r'^https?://', '', text).split('/')[0]
    if not HOST_RE.fullmatch(text):
        raise TileError(f'{field}: “{value}” is not a site name (for example facebook.com).', field)
    return text


def v_choice(value, field, options, default=None):
    text = str(value if value not in (None, '') else (default or '')).strip().lower()
    if text in ('true', 'on', '1'):
        text = 'yes'
    if text in ('false', 'off', '0'):
        text = 'no'
    if text not in options:
        raise TileError(f'{field}: choose one of {", ".join(options)}.', field)
    return text


def v_list(value, field, each, limit=24):
    items = value if isinstance(value, (list, tuple)) else re.split(r'[,\s]+', str(value or ''))
    out = []
    for item in items:
        if str(item).strip():
            cleaned = each(item, field)
            if cleaned not in out:
                out.append(cleaned)
    if not out:
        raise TileError(f'{field}: add at least one.', field)
    if len(out) > limit:
        raise TileError(f'{field}: at most {limit} on one tile.', field)
    return out


def v_secret(value, field='password'):
    text = str(value or '')
    if not 1 <= len(text) <= 64 or any(ord(c) < 32 for c in text):
        raise TileError(f'{field}: needed (up to 64 characters).', field)
    return text


def v_time(value, field='time', default='1h'):
    text = _clean(value or default, field)
    if not TIME_RE.fullmatch(text):
        raise TileError(f'{field}: write a time such as 30m, 1h or 1d.', field)
    return text


def slug(text, limit=24):
    return re.sub(r'[^A-Za-z0-9]+', '-', str(text)).strip('-')[:limit] or 'x'


def op(resource, action='ensure', find=None, values=None, label='', **extra):
    out = {'resource': resource, 'action': action, 'find': dict(find or {}), 'values': dict(values or {}),
           'label': label or resource}
    out.update(extra)
    return out


def tag(kind, key=''):
    return f'{TAG}{kind}:{slug(key, 40)}' if key else f'{TAG}{kind}'


# ───────────────────────────── shared building blocks ─────────────────────────────
def pool_range(gw, first='', last=''):
    """The addresses handed out: typed by the user, or everything from .10 up that is not the router."""
    net = gw.network
    lo, hi = int(net.network_address) + 1, int(net.broadcast_address) - 1
    if str(first).strip() or str(last).strip():
        a, b = ipaddress.IPv4Address(v_ip(first, 'first address')), ipaddress.IPv4Address(v_ip(last, 'last address'))
        if a not in net or b not in net:
            raise TileError(f'first/last address: must be inside {net}.', 'first')
        if int(a) > int(b):
            raise TileError('first address: must come before the last address.', 'first')
        if int(a) <= int(gw.ip) <= int(b):
            raise TileError(f'first/last address: the router itself ({gw.ip}) is inside this range. '
                            'Start after it or end before it.', 'first')
        return str(a), str(b)
    start = lo + 9 if hi - lo >= 40 else lo
    g = int(gw.ip)
    if start <= g <= hi:
        if g - start < hi - g:
            start = g + 1
        else:
            hi = g - 1
    return str(ipaddress.IPv4Address(start)), str(ipaddress.IPv4Address(hi))


def network_ops(kind, iface, gw, p, dns=None):
    """Address on the port + a DHCP server handing out the rest of the network."""
    first, last = pool_range(gw, p.get('first', ''), p.get('last', ''))
    net = str(gw.network)
    dns = dns or ','.join(v_list(p.get('dns') or str(gw.ip), 'DNS', v_ip, 3))
    name = slug(iface)
    mark = tag(kind, iface)
    return [
        leave_bridge(iface),
        op('/ip/address', 'ensure', {'address': str(gw), 'interface': iface}, {'comment': mark, 'disabled': 'no'},
           f'Router address {gw} on {iface}'),
        op('/ip/pool', 'ensure', {'name': f'pool-{name}'}, {'ranges': f'{first}-{last}', 'comment': mark},
           f'Addresses to hand out: {first} – {last}'),
        op('/ip/dhcp-server/network', 'ensure', {'address': net}, {'gateway': str(gw.ip), 'dns-server': dns, 'comment': mark},
           f'What devices are told: gateway {gw.ip}, DNS {dns}'),
        op('/ip/dhcp-server', 'ensure', {'name': f'dhcp-{name}'},
           {'interface': iface, 'address-pool': f'pool-{name}', 'lease-time': v_time(p.get('lease'), 'lease time'),
            'disabled': 'no', 'comment': mark}, f'DHCP server on {iface}'),
    ]


def nat_op(kind, out_iface):
    return op('/ip/firewall/nat', 'ensure', {'chain': 'srcnat', 'action': 'masquerade', 'out-interface': out_iface},
              {'comment': tag(kind, out_iface), 'disabled': 'no'}, f'Share the internet of {out_iface} with local devices (NAT)')


def leave_bridge(iface):
    return op('/interface/bridge/port', 'remove_matching', {'interface': iface}, {},
              f'{iface} stands alone (it leaves the group it was in, if any)')


# ───────────────────────────── builders: tile → operations ─────────────────────────────
def b_identity(p, ctx):
    return [op('/system/identity', 'singleton_set', {}, {'name': v_name(p.get('name'), 'name')}, 'Router name')]


def b_dns(p, ctx):
    servers = v_list(p.get('servers'), 'DNS servers', v_ip, 4)
    return [op('/ip/dns', 'singleton_set', {},
               {'servers': ','.join(servers), 'allow-remote-requests': v_choice(p.get('remote'), 'answer devices', ('yes', 'no'), 'yes')},
               'DNS servers')]


def b_clock(p, ctx):
    zone = _clean(p.get('zone') or 'Africa/Banjul', 'time zone', 40)
    if not re.fullmatch(r'[A-Za-z_]+(/[A-Za-z_+-]+){0,2}', zone):
        raise TileError('time zone: write it like Africa/Banjul.', 'zone')
    server = v_host(p.get('server') or 'pool.ntp.org', 'time server')
    key = 'servers' if int(ctx.get('ros_major') or 7) >= 7 else 'server-dns-names'
    return [op('/system/clock', 'singleton_set', {}, {'time-zone-name': zone}, f'Time zone {zone}'),
            op('/system/ntp/client', 'singleton_set', {}, {'enabled': 'yes', key: server}, f'Clock set from {server}')]


def b_wan_dhcp(p, ctx):
    port = v_name(p.get('port'), 'port')
    ops = [leave_bridge(port),
           op('/ip/dhcp-client', 'ensure', {'interface': port},
              {'disabled': 'no', 'add-default-route': 'yes', 'use-peer-dns': 'yes', 'comment': tag('wan', port)},
              f'Internet on {port}: address given by the provider')]
    if v_choice(p.get('share'), 'share', ('yes', 'no'), 'yes') == 'yes':
        ops.append(nat_op('wan', port))
    return ops


def b_wan_pppoe(p, ctx):
    port = v_name(p.get('port'), 'port')
    name = f'pppoe-{slug(port)}'
    ops = [leave_bridge(port),
           op('/interface/pppoe-client', 'ensure', {'name': name},
              {'interface': port, 'user': _clean(p.get('user'), 'user name', 64) or _need('user name', 'user'),
               'password': v_secret(p.get('password')), 'add-default-route': 'yes', 'use-peer-dns': 'yes',
               'disabled': 'no', 'comment': tag('wan', port)}, f'Internet on {port}: PPPoE dial-up')]
    if v_choice(p.get('share'), 'share', ('yes', 'no'), 'yes') == 'yes':
        ops.append(nat_op('wan', name))
    return ops


def _need(label, field):
    raise TileError(f'{label}: needed.', field)


def b_wan_static(p, ctx):
    port = v_name(p.get('port'), 'port')
    addr = v_gateway(p.get('address'), 'address')
    gw = ipaddress.IPv4Address(v_ip(p.get('gateway'), 'gateway'))
    if gw not in addr.network or gw == addr.ip:
        raise TileError(f'gateway: {gw} must be another address inside {addr.network}.', 'gateway')
    mark = tag('wan', port)
    ops = [leave_bridge(port),
           op('/ip/address', 'ensure', {'address': str(addr), 'interface': port}, {'comment': mark, 'disabled': 'no'},
              f'Fixed address {addr} on {port}'),
           op('/ip/route', 'ensure', {'comment': mark}, {'dst-address': '0.0.0.0/0', 'gateway': str(gw), 'disabled': 'no'},
              f'Everything else goes to the provider at {gw}')]
    if v_choice(p.get('share'), 'share', ('yes', 'no'), 'yes') == 'yes':
        ops.append(nat_op('wan', port))
    return ops


def b_bridge(p, ctx):
    name = v_name(p.get('name') or 'bridge-lan', 'name')
    ports = v_list(p.get('ports'), 'ports', v_name, 16)
    mark = tag('bridge', name)
    ops = [op('/interface/bridge', 'ensure', {'name': name}, {'comment': mark, 'disabled': 'no'}, f'Bridge {name}')]
    ops += [op('/interface/bridge/port', 'ensure', {'interface': port}, {'bridge': name, 'comment': mark},
               f'{port} joins {name}') for port in ports]
    return ops


def b_lan(p, ctx):
    iface = v_name(p.get('interface'), 'port')
    return network_ops('lan', iface, v_gateway(p.get('network')), p)


def b_vlan(p, ctx):
    vid = v_int(p.get('id'), 'VLAN number', 1, 4094)
    parent = v_name(p.get('parent'), 'port')
    name = v_name(p.get('name') or f'vlan{vid}', 'name')
    return [op('/interface/vlan', 'ensure', {'name': name},
               {'vlan-id': str(vid), 'interface': parent, 'disabled': 'no', 'comment': tag('vlan', name)},
               f'VLAN {vid} on {parent}, named {name}')]


def private_list_ops():
    return [op('/ip/firewall/address-list', 'ensure', {'list': PRIVATE_LIST, 'address': net}, {'comment': tag('guest', 'private')},
               f'Private networks list: {net}') for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]


def b_guest(p, ctx):
    iface = v_name(p.get('interface'), 'port')
    gw = v_gateway(p.get('network') or '10.10.10.1/24')
    ops = network_ops('guest', iface, gw, p, dns=','.join(v_list(p.get('dns') or '1.1.1.1,8.8.8.8', 'DNS', v_ip, 3)))
    net = str(gw.network)
    if v_choice(p.get('isolate'), 'keep away from my network', ('yes', 'no'), 'yes') == 'yes':
        ops += private_list_ops()
        ops.append(op('/ip/firewall/filter', 'ensure', {'comment': tag('guest-wall', iface)},
                      {'chain': 'forward', 'src-address': net, 'dst-address-list': PRIVATE_LIST, 'action': 'drop', 'disabled': 'no'},
                      f'Guests ({net}) cannot reach your own networks', place='top'))
    if str(p.get('down') or '').strip():
        down = v_speed(p.get('down'), 'download')
        up = v_speed(p.get('up') or p.get('down'), 'upload')
        ops.append(op('/queue/simple', 'ensure', {'name': f'guest-{slug(iface)}'},
                      {'target': net, 'max-limit': f'{up}/{down}', 'comment': tag('guest', iface), 'disabled': 'no'},
                      f'All guests together: {down} down, {up} up'))
    return ops


def b_static_lease(p, ctx):
    mac = v_mac(p.get('mac'))
    ip = v_ip(p.get('ip'), 'address')
    name = _clean(p.get('name') or 'device', 'name', 40)
    return [op('/ip/dhcp-server/lease', 'ensure', {'mac-address': mac}, {'address': ip, 'comment': f'{tag("lease")} {name}'},
               f'{name} ({mac}) always gets {ip}')]


def b_hotspot(p, ctx):
    iface = v_name(p.get('interface'), 'port')
    gw = v_gateway(p.get('network') or '10.5.50.1/24')
    name = slug(iface)
    mark = tag('hotspot', iface)
    profile = {'hotspot-address': str(gw.ip)}          # hotspot server profiles have no comment field
    if str(p.get('dns_name') or '').strip():
        profile['dns-name'] = v_host(p.get('dns_name'), 'login address')
    ops = network_ops('hotspot', iface, gw, p)
    ops += [
        op('/ip/hotspot/profile', 'ensure', {'name': f'hsprof-{name}'}, profile, f'Login page settings for {iface}'),
        op('/ip/hotspot', 'ensure', {'name': f'hotspot-{name}'},
           {'interface': iface, 'address-pool': f'pool-{name}', 'profile': f'hsprof-{name}', 'disabled': 'no'},
           f'Hotspot on {iface}: people sign in before they browse'),
        op('/ip/firewall/nat', 'ensure', {'comment': mark},
           {'chain': 'srcnat', 'src-address': str(gw.network), 'action': 'masquerade', 'disabled': 'no'},
           f'Hotspot customers ({gw.network}) share the internet'),
    ]
    return ops


def b_walled_garden(p, ctx):
    return [op('/ip/hotspot/walled-garden', 'ensure', {'dst-host': host}, {'action': 'allow', 'comment': tag('free-site')},
               f'{host} opens without signing in') for host in v_list(p.get('sites'), 'sites', v_host, 20)]


def b_bypass(p, ctx):
    mac = v_mac(p.get('mac'))
    name = _clean(p.get('name') or 'device', 'name', 40)
    return [op('/ip/hotspot/ip-binding', 'ensure', {'mac-address': mac}, {'type': 'bypassed', 'comment': f'{tag("bypass")} {name}'},
               f'{name} ({mac}) never sees the login page')]


def b_speed_limit(p, ctx):
    target = v_target(p.get('target'))
    down = v_speed(p.get('down'), 'download')
    up = v_speed(p.get('up') or p.get('down'), 'upload')
    name = v_name(p.get('name') or f'limit-{slug(target)}', 'name')
    return [op('/queue/simple', 'ensure', {'name': name},
               {'target': target, 'max-limit': f'{up}/{down}', 'comment': tag('speed', name), 'disabled': 'no'},
               f'{target}: at most {down} down and {up} up')]


def b_fw_basic(p, ctx):
    wan = v_name(p.get('wan'), 'internet port')
    mark = tag('shield', wan)
    ops = [
        op('/ip/firewall/filter', 'ensure', {'comment': mark + ':1'},
           {'chain': 'input', 'connection-state': 'established,related', 'action': 'accept', 'disabled': 'no'},
           'Answers to what the router asked for come back in'),
        op('/ip/firewall/filter', 'ensure', {'comment': mark + ':2'},
           {'chain': 'input', 'connection-state': 'invalid', 'action': 'drop', 'disabled': 'no'}, 'Broken packets are dropped'),
        op('/ip/firewall/filter', 'ensure', {'comment': mark + ':3'},
           {'chain': 'input', 'protocol': 'icmp', 'action': 'accept', 'disabled': 'no'}, 'Ping still works'),
    ]
    trusted = [v_ip(a, 'TapTap address') for a in (ctx.get('trusted') or [])][:5]
    for a in trusted:
        ops.append(op('/ip/firewall/address-list', 'ensure', {'list': 'taptap-trusted', 'address': a},
                      {'comment': tag('shield', 'taptap')}, f'TapTap itself ({a}) stays trusted'))
    ops.append(op('/ip/firewall/filter', 'ensure', {'comment': mark + ':4'},
                  {'chain': 'input', 'src-address-list': 'taptap-trusted', 'action': 'accept', 'disabled': 'no'},
                  'TapTap can always reach the router'))
    for i, extra in enumerate(v_list(p.get('allow'), 'also allow', v_target, 5) if str(p.get('allow') or '').strip() else []):
        ops.append(op('/ip/firewall/filter', 'ensure', {'comment': f'{mark}:allow{i}'},
                      {'chain': 'input', 'src-address': extra, 'action': 'accept', 'disabled': 'no'},
                      f'{extra} may manage the router from outside'))
    ops.append(op('/ip/firewall/filter', 'ensure', {'comment': mark + ':9'},
                  {'chain': 'input', 'in-interface': wan, 'action': 'drop', 'disabled': 'no'},
                  f'Everything else arriving from the internet ({wan}) is dropped'))
    return ops


def b_block_site(p, ctx):
    sites = v_list(p.get('sites'), 'sites', v_host, 20)
    ops = [op('/ip/firewall/address-list', 'ensure', {'list': BLOCKED_LIST, 'address': s.lstrip('*.')},
              {'comment': tag('block-site')}, f'{s} goes on the blocked list') for s in sites]
    ops.append(op('/ip/firewall/filter', 'ensure', {'comment': tag('block-site', 'rule')},
                  {'chain': 'forward', 'dst-address-list': BLOCKED_LIST, 'action': 'drop', 'disabled': 'no'},
                  'Traffic to blocked sites is dropped', place='top'))
    return ops


def b_port_forward(p, ctx):
    proto = v_choice(p.get('protocol'), 'protocol', ('tcp', 'udp'), 'tcp')
    port = v_int(p.get('port'), 'outside port', 1, 65535)
    to_ip = v_ip(p.get('to_ip'), 'device address')
    to_port = v_int(p.get('to_port') or port, 'device port', 1, 65535)
    values = {'chain': 'dstnat', 'protocol': proto, 'dst-port': str(port), 'action': 'dst-nat',
              'to-addresses': to_ip, 'to-ports': str(to_port), 'disabled': 'no'}
    if str(p.get('wan') or '').strip():
        values['in-interface'] = v_name(p.get('wan'), 'internet port')
    else:
        values['dst-address-type'] = 'local'
    return [op('/ip/firewall/nat', 'ensure', {'comment': tag('open-port', f'{proto}-{port}')}, values,
               f'{proto.upper()} port {port} from outside reaches {to_ip}:{to_port}')]


def b_block_device(p, ctx):
    mac = v_mac(p.get('mac'))
    name = _clean(p.get('name') or 'device', 'name', 40)
    return [op('/ip/firewall/filter', 'ensure', {'comment': f'{tag("block-device", mac)} {name}'},
               {'chain': 'forward', 'src-mac-address': mac, 'action': 'drop', 'disabled': 'no'},
               f'{name} ({mac}) gets no internet', place='top')]


SERVICE_CHOICES = ('telnet', 'ftp', 'www', 'www-ssl', 'api', 'api-ssl', 'ssh', 'winbox')


def b_services(p, ctx):
    off = v_list(p.get('off') or 'telnet,ftp', 'switch off', lambda v, f: v_choice(v, f, SERVICE_CHOICES), 8)
    ops = [op('/ip/service', 'set', {'name': s}, {'disabled': 'yes'}, f'{s} is switched off') for s in off]
    if str(p.get('winbox_from') or '').strip():
        ops.append(op('/ip/service', 'set', {'name': 'winbox'}, {'address': v_net(p.get('winbox_from'), 'WinBox only from')},
                      f'WinBox answers only to {v_net(p.get("winbox_from"))}'))
    return ops


def b_port_state(p, ctx):
    port = v_name(p.get('port'), 'port')
    on = v_choice(p.get('state'), 'state', ('on', 'off', 'yes', 'no'), 'on') in ('on', 'yes')
    return [op('/interface', 'set', {'name': port}, {'disabled': 'no' if on else 'yes'},
               f'{port} is switched {"on" if on else "off"}')]


def parse_pairs(text, field, required=False):
    """key=value pairs of the expert tile: comment="my rule" chain=forward"""
    try:
        parts = shlex.split(str(text or ''))
    except ValueError:
        raise TileError(f'{field}: a quote is not closed.', field)
    out = {}
    for part in parts:
        key, eq, value = part.partition('=')
        if not eq or not KEY_RE.fullmatch(key):
            raise TileError(f'{field}: “{part}” is not a property=value pair.', field)
        if key in ('place-before', 'numbers', '.id', 'id'):
            raise TileError(f'{field}: “{key}” cannot be set from a tile.', field)
        out[key] = _clean(value, field, 200)
    if required and not out:
        raise TileError(f'{field}: add at least one property=value.', field)
    if len(out) > 20:
        raise TileError(f'{field}: at most 20 properties.', field)
    return out


def custom_menus():
    from .mikrotik import MikroTikService
    return MikroTikService.WRITE_PREFIXES


def b_custom(p, ctx):
    from .mikrotik import MikroTikError, MikroTikService
    try:
        menu = MikroTikService.validate_resource_path(str(p.get('menu') or '').replace(' ', '/'))
    except MikroTikError:
        raise TileError('menu: write a RouterOS menu such as /ip/firewall/filter.', 'menu')
    if not any(menu == x or menu.startswith(x + '/') for x in custom_menus()):
        raise TileError(f'menu: {menu} cannot be changed from the studio. Allowed: {", ".join(custom_menus())}.', 'menu')
    find = parse_pairs(p.get('match'), 'find the row where', required=True)
    values = parse_pairs(p.get('set'), 'set')
    if not values and len(find) < 2:
        raise TileError('set: say what the row should contain.', 'set')
    return [op(menu, 'ensure', find, values, f'Expert row in {menu}')]


# ───────────────────────────── the catalogue ─────────────────────────────
def F(name, label, kind='text', default='', **more):
    return dict(name=name, label=label, type=kind, default=default, **more)


GROUPS = [
    ('internet', 'Internet', 'Where the internet comes in', '#0ea5e9'),
    ('network', 'Networks', 'Ports, addresses and who gets one', '#22c55e'),
    ('hotspot', 'Hotspot', 'Sign-in before browsing', '#f59e0b'),
    ('speed', 'Speed', 'Who gets how much', '#a855f7'),
    ('protect', 'Protection', 'What is allowed and what is not', '#ef4444'),
    ('system', 'Router', 'Name, clock, services', '#64748b'),
]

TILES = {
    'wan_dhcp': dict(group='internet', icon='bi-globe2', label='Internet line', risk='high', build=b_wan_dhcp,
                     sentence='Internet comes in on {port} — the provider gives the address. Share it: {share}',
                     why='Most fibre, 4G and cable lines work this way.',
                     fields=[F('port', 'port', 'iface', 'ether1'), F('share', 'share with local devices', 'choice', 'yes', options=['yes', 'no'])]),
    'wan_pppoe': dict(group='internet', icon='bi-telephone-outbound', label='Internet line (PPPoE)', risk='high', build=b_wan_pppoe,
                      sentence='Dial the internet on {port} with user {user} and password {password}. Share it: {share}',
                      why='For providers that give you a user name and password.',
                      fields=[F('port', 'port', 'iface', 'ether1'), F('user', 'user name'), F('password', 'password', 'secret'),
                              F('share', 'share with local devices', 'choice', 'yes', options=['yes', 'no'])]),
    'wan_static': dict(group='internet', icon='bi-pin-map', label='Internet line (fixed address)', risk='high', build=b_wan_static,
                       sentence='Internet on {port} with the fixed address {address} and gateway {gateway}. Share it: {share}',
                       why='For a leased line or a public IP from the provider.',
                       fields=[F('port', 'port', 'iface', 'ether1'), F('address', 'address/size', 'gateway', '', placeholder='41.223.10.2/30'),
                               F('gateway', 'gateway', 'ip', '', placeholder='41.223.10.1'),
                               F('share', 'share with local devices', 'choice', 'yes', options=['yes', 'no'])]),
    'dns': dict(group='internet', icon='bi-signpost-split', label='DNS', risk='normal', build=b_dns,
                sentence='Look up names with {servers}. Answer local devices: {remote}',
                why='The phone book of the internet. 1.1.1.1 and 8.8.8.8 are fast public ones.',
                fields=[F('servers', 'DNS servers', 'list', '1.1.1.1, 8.8.8.8'), F('remote', 'answer local devices', 'choice', 'yes', options=['yes', 'no'])]),
    'bridge': dict(group='network', icon='bi-diagram-3', label='Join ports', risk='normal', build=b_bridge,
                   sentence='Join the ports {ports} into one group called {name}',
                   why='Ports in one group behave like one switch: one network for all of them.',
                   fields=[F('ports', 'ports', 'ifaces', 'ether2, ether3'), F('name', 'name', 'name', 'bridge-lan')]),
    'lan': dict(group='network', icon='bi-hdd-network', label='Local network', risk='normal', build=b_lan,
                sentence='Give {interface} the network {network} and hand out addresses automatically',
                why='The router takes the address you write; devices get the others.',
                fields=[F('interface', 'port or group', 'iface', 'bridge-lan'), F('network', 'router address/size', 'gateway', '192.168.88.1/24'),
                        F('first', 'first address', 'ip', '', optional=True, more=True), F('last', 'last address', 'ip', '', optional=True, more=True),
                        F('dns', 'DNS for devices', 'list', '', optional=True, more=True), F('lease', 'address kept for', 'time', '1h', more=True)]),
    'guest': dict(group='network', icon='bi-person-walking', label='Guest network', risk='normal', build=b_guest,
                  sentence='Guests on {interface} get {network}, kept away from my network: {isolate}, sharing {down} down',
                  why='Visitors browse the internet but never see your cameras, printers or tills.',
                  fields=[F('interface', 'port or group', 'iface', 'ether5'), F('network', 'router address/size', 'gateway', '10.10.10.1/24'),
                          F('isolate', 'keep away from my network', 'choice', 'yes', options=['yes', 'no']),
                          F('down', 'download for all guests', 'speed', '10M', optional=True), F('up', 'upload', 'speed', '', optional=True, more=True),
                          F('lease', 'address kept for', 'time', '1h', more=True)]),
    'vlan': dict(group='network', icon='bi-layers', label='VLAN', risk='advanced', build=b_vlan,
                 sentence='Make VLAN {id} on {parent}',
                 why='A second network on the same cable, for an access point or a switch that tags traffic.',
                 fields=[F('id', 'VLAN number', 'int', '10', min=1, max=4094), F('parent', 'port', 'iface', 'ether2'),
                         F('name', 'name', 'name', '', optional=True, more=True)]),
    'static_lease': dict(group='network', icon='bi-bookmark-star', label='Reserve an address', risk='normal', build=b_static_lease,
                         sentence='{name} with MAC {mac} always gets {ip}',
                         why='Printers, cameras and servers should not change address.',
                         fields=[F('name', 'device name', 'text', 'Printer'), F('mac', 'MAC address', 'mac', '', placeholder='AA:BB:CC:11:22:33'),
                                 F('ip', 'address', 'ip', '', placeholder='192.168.88.20')]),
    'port_state': dict(group='network', icon='bi-toggle-on', label='Port on / off', risk='high', build=b_port_state,
                       sentence='Switch {port} {state}', why='An unused port that is off cannot be abused.',
                       fields=[F('port', 'port', 'iface', 'ether5'), F('state', 'state', 'choice', 'off', options=['on', 'off'])]),
    'hotspot': dict(group='hotspot', icon='bi-wifi', label='Hotspot', risk='advanced', build=b_hotspot,
                    sentence='People on {interface} sign in first. Their network is {network}',
                    why='TapTap vouchers and members sign in on this page.',
                    fields=[F('interface', 'port or group', 'iface', 'bridge-hotspot'), F('network', 'router address/size', 'gateway', '10.5.50.1/24'),
                            F('dns_name', 'login address', 'host', '', optional=True, more=True, placeholder='wifi.login'),
                            F('lease', 'address kept for', 'time', '1h', more=True)]),
    'walled_garden': dict(group='hotspot', icon='bi-unlock', label='Free sites', risk='normal', build=b_walled_garden,
                          sentence='Open {sites} without signing in',
                          why='Your payment page or your own website should open before login.',
                          fields=[F('sites', 'sites', 'hosts', '', placeholder='wave.com, mybusiness.gm')]),
    'bypass': dict(group='hotspot', icon='bi-person-check', label='Skip the login', risk='normal', build=b_bypass,
                   sentence='{name} with MAC {mac} never sees the login page',
                   why='For the till, a TV or staff devices.',
                   fields=[F('name', 'device name', 'text', 'Shop TV'), F('mac', 'MAC address', 'mac', '', placeholder='AA:BB:CC:11:22:33')]),
    'speed_limit': dict(group='speed', icon='bi-speedometer2', label='Speed limit', risk='normal', build=b_speed_limit,
                        sentence='Limit {target} to {down} down and {up} up',
                        why='One device or a whole network; everybody else keeps their speed.',
                        fields=[F('target', 'address or network', 'target', '', placeholder='192.168.88.50'),
                                F('down', 'download', 'speed', '5M'), F('up', 'upload', 'speed', '2M'),
                                F('name', 'name', 'name', '', optional=True, more=True)]),
    'fw_basic': dict(group='protect', icon='bi-shield-check', label='Shield the router', risk='high', build=b_fw_basic,
                     sentence='Nobody on the internet side ({wan}) may log in to the router',
                     why='Stops password guessing from the internet. TapTap and your local network still get in.',
                     fields=[F('wan', 'internet port', 'iface', 'ether1'),
                             F('allow', 'also allow from outside', 'targets', '', optional=True, more=True, placeholder='office IP, e.g. 41.223.10.9')]),
    'block_site': dict(group='protect', icon='bi-slash-circle', label='Block sites', risk='normal', build=b_block_site,
                       sentence='Block {sites} for everybody',
                       why='The router looks the names up itself. Big sites with many addresses may need their app blocked too.',
                       fields=[F('sites', 'sites', 'hosts', '', placeholder='tiktok.com, bet365.com')]),
    'block_device': dict(group='protect', icon='bi-phone-flip', label='Block a device', risk='normal', build=b_block_device,
                         sentence='{name} with MAC {mac} gets no internet',
                         why='The device stays connected to Wi-Fi but nothing goes through.',
                         fields=[F('name', 'device name', 'text', 'Unknown phone'), F('mac', 'MAC address', 'mac', '', placeholder='AA:BB:CC:11:22:33')]),
    'port_forward': dict(group='protect', icon='bi-door-open', label='Open a port', risk='advanced', build=b_port_forward,
                         sentence='Send {protocol} port {port} from outside to {to_ip} port {to_port}',
                         why='To reach a camera recorder or a server from outside.',
                         fields=[F('protocol', 'protocol', 'choice', 'tcp', options=['tcp', 'udp']), F('port', 'outside port', 'int', '8080', min=1, max=65535),
                                 F('to_ip', 'device address', 'ip', '', placeholder='192.168.88.20'),
                                 F('to_port', 'device port', 'int', '', optional=True, min=1, max=65535, placeholder='same'),
                                 F('wan', 'only on internet port', 'iface', '', optional=True, more=True)]),
    'services': dict(group='system', icon='bi-door-closed', label='Close old doors', risk='high', build=b_services,
                     sentence='Switch off {off}',
                     why='Telnet and FTP send passwords in clear text. Nobody needs them.',
                     fields=[F('off', 'services', 'services', 'telnet, ftp', options=list(SERVICE_CHOICES)),
                             F('winbox_from', 'WinBox only from', 'net', '', optional=True, more=True, placeholder='192.168.88.0/24')]),
    'identity': dict(group='system', icon='bi-tag', label='Router name', risk='normal', build=b_identity,
                     sentence='Call this router {name}', why='The name shown in WinBox, in TapTap and to neighbours.',
                     fields=[F('name', 'name', 'name', 'TapTap-Router')]),
    'clock': dict(group='system', icon='bi-clock', label='Clock', risk='normal', build=b_clock,
                  sentence='Keep the clock right for {zone}',
                  why='Vouchers, schedules and logs all depend on the right time.',
                  fields=[F('zone', 'time zone', 'text', 'Africa/Banjul'), F('server', 'time server', 'host', 'pool.ntp.org', more=True)]),
    'custom': dict(group='system', icon='bi-code-slash', label='Expert row', risk='high', build=b_custom,
                   sentence='In {menu}, the row where {match} gets {set}',
                   why='Anything the other tiles do not cover: one row of one RouterOS menu.',
                   fields=[F('menu', 'menu', 'menu', '/ip/firewall/filter'), F('match', 'property=value …', 'pairs', '', placeholder='comment="my rule"'),
                           F('set', 'property=value …', 'pairs', '', optional=True, placeholder='chain=forward action=accept')]),
}


def catalogue():
    """What the browser needs to draw the tile shelf (no functions)."""
    return {
        'groups': [dict(key=k, label=label, hint=hint, color=color) for k, label, hint, color in GROUPS],
        'tiles': [{'type': key, **{k: v for k, v in meta.items() if k != 'build'}} for key, meta in TILES.items()],
        'menus': list(custom_menus()),
    }


def secret_fields(tile_type):
    return [f['name'] for f in TILES.get(tile_type, {}).get('fields', []) if f['type'] == 'secret']


def clean_board(board):
    """The stored shape of a board: [{id, type, params, off}], nothing else."""
    out, seen = [], set()
    for raw in (board or [])[:MAX_TILES]:
        if not isinstance(raw, dict) or raw.get('type') not in TILES:
            continue
        tid = re.sub(r'[^A-Za-z0-9_-]', '', str(raw.get('id') or ''))[:24] or f't{len(out) + 1}'
        while tid in seen:
            tid += 'x'
        seen.add(tid)
        names = {f['name'] for f in TILES[raw['type']]['fields']}
        params = {k: (v if isinstance(v, (str, int, float, bool)) else ', '.join(map(str, v)) if isinstance(v, list) else '')
                  for k, v in (raw.get('params') or {}).items() if k in names}
        out.append({'id': tid, 'type': raw['type'], 'params': {k: str(v)[:400] for k, v in params.items()}, 'off': bool(raw.get('off'))})
    return out


def compile_tile(tile, ctx=None):
    meta = TILES.get(tile.get('type'))
    if not meta:
        raise TileError('This tile is not known.')
    params = dict(tile.get('params') or {})
    for f in meta['fields']:
        if f['name'] not in params or str(params[f['name']]).strip() == '':
            if f.get('default') not in (None, ''):
                params[f['name']] = f['default']
            elif not f.get('optional'):
                raise TileError(f'{f["label"]}: fill this blank.', f['name'])
    ops = meta['build'](params, ctx or {})
    for o in ops:
        o['tile'] = tile.get('id', '')
    return ops


def compile_board(board, ctx=None):
    """(operations, problems). A tile with a wrong value is reported and left out; the others still compile."""
    ops, problems = [], []
    for tile in clean_board(board):
        if tile['off']:
            continue
        try:
            ops += compile_tile(tile, ctx)
        except TileError as exc:
            problems.append({'tile': tile['id'], 'field': exc.field, 'message': str(exc)})
    # The same row described twice (two tiles both listing 10.0.0.0/8) is written once.
    seen, unique = set(), []
    for o in ops:
        key = (o['resource'], o['action'], tuple(sorted(o['find'].items())), tuple(sorted(o['values'].items())))
        if key not in seen:
            seen.add(key)
            unique.append(o)
    return unique, problems


def describe(tile):
    """The tile as one sentence, for the AI and the history."""
    meta = TILES.get(tile.get('type'))
    if not meta:
        return ''
    values = {f['name']: (tile.get('params') or {}).get(f['name'], f.get('default', '')) for f in meta['fields']}
    for name in secret_fields(tile['type']):
        values[name] = '••••'
    try:
        return meta['sentence'].format(**values)
    except (KeyError, IndexError):
        return meta['label']


# ───────────────────────────── operations → RouterOS text ─────────────────────────────
def q(value):
    """A RouterOS string literal."""
    s = str(value).replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$')
    return f'"{s}"'


def menu(resource):
    """/ip/firewall/filter → /ip firewall filter (works on RouterOS 6 and 7)."""
    return '/' + ' '.join(str(resource).strip('/').split('/'))


def _pairs(d, hide=()):
    return ' '.join(f'{k}={q("••••" if k in hide else v)}' for k, v in d.items())


def _plain(d, hide=()):
    """For reading: quotes only where RouterOS needs them."""
    return ' '.join(f'{k}=' + ('••••' if k in hide else str(v) if re.fullmatch(r'[A-Za-z0-9_.:/,!-]+', str(v)) else q(v)) for k, v in d.items())


def _where(find):
    return ' '.join(f'{k}={q(v)}' for k, v in find.items())


SECRET_KEYS = ('password', 'secret', 'passphrase', 'private-key')


def readable(ops, hide_secrets=True):
    """The plan as a person would type it in the terminal. Returns [{tile, text, label}]."""
    lines = []
    for o in ops:
        m, hide = menu(o['resource']), SECRET_KEYS if hide_secrets else ()
        if o['action'] == 'ensure':
            text = f'{m} add {_plain({**o["find"], **o["values"]}, hide)}'
        elif o['action'] == 'set':
            text = f'{m} set [find {_plain(o["find"])}] {_plain(o["values"], hide)}'
        elif o['action'] == 'singleton_set':
            text = f'{m} set {_plain(o["values"], hide)}'
        else:
            text = f'{m} remove [find {_plain(o["find"])}]'
        lines.append({'tile': o.get('tile', ''), 'label': o['label'], 'text': text})
    return lines


def safe_line(o):
    """One operation as a script line that can run twice without doing anything twice."""
    m, find, vals = menu(o['resource']), _where(o['find']), _pairs(o['values'])
    if o['action'] == 'singleton_set':
        return f'{m} set {vals}'
    if o['action'] == 'remove_matching':
        return f'{m} remove [find where {find}]'
    if o['action'] == 'set' and o.get('soft'):          # putting a value back: a row that is gone is not an error
        return f'{m} set [find where {find}] {vals}'
    if o['action'] == 'set':
        return (f'{{ :local x [{m} find where {find}]; :if ([:len $x] = 0) do={{ :error {q("TapTap Studio: not found: " + o["label"][:60])} }}; '
                f'{m} set $x {vals} }}')
    add = f'{m} add {find} {vals}'.rstrip()
    if o.get('place') == 'top':
        chain = o['values'].get('chain') or o['find'].get('chain') or 'forward'
        add = (f':local f [{m} find where chain={q(chain)} dynamic=no]; '
               f':if ([:len $f] > 0) do={{ {add} place-before=[:pick $f 0] }} else={{ {add} }}')
    change = f'{m} set $x {vals}' if vals else ':nothing'
    return f'{{ :local x [{m} find where {find}]; :if ([:len $x] = 0) do={{ {add} }} else={{ {change} }} }}'


def script(ops, title=''):
    """A complete .rsc file for the plan."""
    head = [f'# {title}'] if title else []
    head.append('# Made by TapTap Config Studio. Safe to run twice: rows that exist are updated, not duplicated.')
    return '\n'.join(head + [f'# {o["label"]}\n{safe_line(o)}' for o in ops]) + '\n'


def chunks(ops, limit=2600):
    """The plan cut into script pieces small enough for one TapTap Link command each."""
    parts, cur = [], ''
    for o in ops:
        line = safe_line(o)
        if len(line) > limit:
            raise TileError('One tile is too large to send through TapTap Link. Split its list into two tiles.')
        if cur and len(cur) + len(line) + 2 > limit:
            parts.append(cur)
            cur = ''
        cur = f'{cur}; {line}' if cur else line
    if cur:
        parts.append(cur)
    return parts
