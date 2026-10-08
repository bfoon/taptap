"""My connection: from the Wi-Fi you are on, hop by hop to the MikroTik, then on to the Internet.

A browser cannot ping, traceroute or read MAC addresses (no web page can). So the work is split:

1. **Find you** (``whoami``). Your phone is talking to TapTap right now, so the MikroTik's connection table
   holds a connection from your private IP to TapTap's address. TapTap reads it, then ARP / DHCP / the
   bridge host table give your MAC, device name and the MikroTik port you come in on. The browser may also
   offer its private IP (WebRTC); it is only used to choose between several candidates, never trusted alone.
2. **Map the way** (``build_path``, database only). The boxes between you and the MikroTik come from
   TapTap's site-router inventory (TP-Link, Tenda, Ubiquiti… with IP, MAC, maker, and the parent chain the
   owner placed on Topology) and from LLDP/MNDP neighbours learned on the same port. When the chain was
   never placed, the boxes on that port are put in order by their response time (nearest answers first).
3. **Measure every hop** (``hops``), from the MikroTik: your device, every box on the way, the ISP gateway
   and the Internet — once with the line idle, and once while your phone downloads flat out.
4. **Find the slow link** (``analyze``). Under load the queue builds at the slowest link: every box *beyond*
   it suddenly answers slowly, the boxes before it do not. That points at the exact Wi-Fi link or cable,
   even on routers TapTap cannot log in to. Your phone's own speed against the router's own Internet speed
   tells whether the chain or the Internet line is the limit.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import statistics
import time
from urllib.parse import urlsplit

from django.conf import settings

from . import nettools as nt

PORT_RE = re.compile(r'^[A-Za-z0-9._/:@+-]{1,64}$')
MAC_RE = re.compile(r'^[0-9A-F]{2}(:[0-9A-F]{2}){5}$')
MAX_HOPS = 14
WIFI_PREFIXES = ('wlan', 'wifi', 'cap', 'wl-')


# ─────────────────────────────── parameters ───────────────────────────────

def taptap_ips():
    """TapTap's public IPv4 addresses (what the phone's connection to TapTap points at)."""
    host = urlsplit(getattr(settings, 'SITE_URL', '') or '').hostname or ''
    ips = []
    if host:
        try:
            for info in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM):
                ip = info[4][0]
                if ip not in ips and ipaddress.ip_address(ip).is_global:
                    ips.append(ip)
        except (OSError, ValueError):
            pass
    return host, ips[:6]


def clean_mac(v):
    m = str(v or '').strip().upper().replace('-', ':')
    if m and not MAC_RE.match(m):
        raise ValueError('Invalid MAC address.')
    return m


def clean(kind, data):
    if kind == 'whoami':
        host = str(data.get('host') or '')
        ips = [str(i) for i in (data.get('ips') or [])][:6]
        for ip in ips:
            if not nt.is_ip(ip) or not ipaddress.ip_address(ip).is_global or ':' in ip:
                raise ValueError('Invalid TapTap address.')
        return {'target': 'this device', 'host': nt.clean_host(host) if host else '', 'ips': ips}
    if kind == 'hops':
        hops = data.get('hops') or []
        if not isinstance(hops, list) or len(hops) > MAX_HOPS:
            raise ValueError(f'Up to {MAX_HOPS} stops can be measured.')
        clean_hops = []
        for h in hops:
            ip = nt.clean_host(h)
            if not nt.is_ip(ip) or ':' in ip:
                raise ValueError(f'{h} is not an IPv4 address.')
            if ip not in clean_hops:
                clean_hops.append(ip)
        phase = 'load' if data.get('phase') == 'load' else 'idle'
        port = str(data.get('port') or '').strip()
        if port and not PORT_RE.match(port):
            raise ValueError('Invalid port name.')
        try:
            rounds = max(2, min(12, int(data.get('rounds') or (8 if phase == 'load' else 5))))
        except (TypeError, ValueError):
            raise ValueError('Rounds must be a number.')
        return {'target': f'{len(clean_hops)} stops', 'hops': clean_hops, 'phase': phase, 'port': port,
                'mac': clean_mac(data.get('mac')), 'upstream': bool(data.get('upstream', True)), 'rounds': rounds}
    raise ValueError('Unknown test.')


# ─────────────────────────────── API runners (Direct API / TapTap Tunnel) ───────────────────────────────

def run_whoami(svc, p):
    clients = {}
    for ip in p['ips']:
        for row in svc.safe_get('/ip/firewall/connection', **{'dst-address': f'{ip}:443'}):
            src = str(row.get('src-address') or '').rsplit(':', 1)[0]
            if nt.is_ip(src):
                clients.setdefault(src, 0)
                clients[src] += 1
    live = {}
    for ip in list(clients)[:12]:
        arp = svc.safe_get('/ip/arp', address=ip)
        a = arp[0] if arp else {}
        mac = str(a.get('mac-address') or '').upper()
        info = {'mac': mac, 'interface': str(a.get('interface') or '')}
        if mac:
            host = svc.safe_get('/interface/bridge/host', **{'mac-address': mac})
            if host:
                info['port'] = str(host[0].get('on-interface') or host[0].get('interface') or '')
            lease = svc.safe_get('/ip/dhcp-server/lease', **{'mac-address': mac})
            if lease:
                info['hostname'] = str(lease[0].get('host-name') or lease[0].get('comment') or '')
        live[ip] = info
    return {'clients': [{'ip': ip, 'conns': n, **live.get(ip, {})} for ip, n in clients.items()]}


def _gateway(svc):
    try:
        for r in svc.resource('/ip/route').get():
            if str(r.get('dst-address')) == '0.0.0.0/0' and str(r.get('active', 'false')).lower() in ('true', 'yes'):
                return nt.first_ipv4(r.get('immediate-gw') or r.get('gateway') or '')
    except Exception:
        pass
    return ''


def run_hops(svc, p):
    targets = list(p['hops'])
    gw = _gateway(svc) if p['upstream'] else ''
    extra = ([gw] if gw and gw not in targets else []) + (['1.1.1.1'] if p['upstream'] else [])
    order = targets + extra
    samples = {ip: [] for ip in order}
    deadline = time.monotonic() + (13 if p['phase'] == 'load' else 15)
    for _ in range(p['rounds']):
        for ip in order:
            if time.monotonic() > deadline:
                break
            try:
                t, _h = nt._api_ping(svc, ip, 1, interval='200ms')
                samples[ip].append(t[0] if t else None)
            except Exception:
                samples[ip].append(None)
        if time.monotonic() > deadline:
            break
    res = {'phase': p['phase'], 'samples': {ip: samples[ip] for ip in targets}, 'gateway': gw,
           'gw_samples': samples.get(gw) if gw else None, 'inet_samples': samples.get('1.1.1.1') if p['upstream'] else None}
    if p['port'] and not p['port'].lower().startswith(WIFI_PREFIXES):
        try:
            rows = svc.resource('/interface/ethernet').call('monitor', {'numbers': p['port'], 'once': ''}) or []
            if rows:
                res['eth'] = {'rate': str(rows[-1].get('rate') or ''), 'full_duplex': str(rows[-1].get('full-duplex') or ''),
                              'status': str(rows[-1].get('status') or '')}
        except Exception:
            pass
    if p['mac']:
        from .mikrotik import MikroTikService
        for path in MikroTikService.WIFI_REG_TABLES:
            rows = svc.safe_get(path, **{'mac-address': p['mac']})
            if rows:
                r = rows[0]
                res['wifi'] = {'signal': str(r.get('signal-strength') or r.get('signal') or ''), 'tx': str(r.get('tx-rate') or ''),
                               'rx': str(r.get('rx-rate') or ''), 'ccq': str(r.get('tx-ccq') or r.get('ccq') or ''),
                               'interface': str(r.get('interface') or '')}
                break
    return res


# ─────────────────────────────── TapTap Link scripts ───────────────────────────────

def link_body(kind, p, rs):
    if kind == 'whoami':
        body = ':local ips [:toarray ""]; ' + ''.join(f':set ips ($ips , {rs(ip)}); ' for ip in p['ips'])
        if p.get('host'):
            body += f':do {{ :set ips ($ips , [:tostr [:resolve {rs(p["host"])}]]) }} on-error={{}}; '
        body += (':local n 0; :foreach i in=$ips do={ :foreach c in=[/ip firewall connection find where dst-address=($i . ":443")] do={ '
                 ':if ($n < 40) do={ :do { :set out ($out . "c=" . [/ip firewall connection get $c src-address] . "\\n"); :set n ($n + 1) } on-error={} } } }; ')
        return body
    if kind == 'hops':
        body = ''
        if p['upstream']:
            body += (':local g ""; :do { :set g [:tostr [/ip route get ([/ip route find where dst-address="0.0.0.0/0" active]->0) gateway]] } on-error={}; '
                     ':set out ($out . "gw=" . $g . "\\n"); :local gip [:toip $g]; ')
        body += f':for r from=1 to={int(p["rounds"])} do={{ '
        for i, ip in enumerate(p['hops']):
            body += nt._ping_snippet(ip, 1, 0, f'h{i}=')
        if p['upstream']:
            body += ':if ([:typeof $gip] = "ip") do={ ' + nt._ping_snippet('$gip', 1, 0, 'hg=') + '}; '
            body += nt._ping_snippet('1.1.1.1', 1, 0, 'hi=')
        body += '}; '
        if p['port'] and not p['port'].lower().startswith(WIFI_PREFIXES):
            body += (f':do {{ :local m [/interface ethernet monitor [find name={rs(p["port"])}] once as-value]; '
                     ':set out ($out . "eth=" . ($m->"rate") . "|" . ($m->"full-duplex") . "|" . ($m->"status") . "\\n") } on-error={}; ')
        if p['mac']:
            # Wi-Fi menus differ by package; :parse keeps a missing menu from stopping the whole script.
            for menu, sig, ccq in (('/interface wireless registration-table', 'signal-strength', 'tx-ccq'),
                                   ('/interface wifi registration-table', 'signal', ''),
                                   ('/caps-man registration-table', 'rx-signal', '')):
                get = lambda f: f'[{menu} get $r {f}]'
                inner = (f':local i [{menu} find mac-address={p["mac"]}]; :if ([:len $i] > 0) do={{ :local r ($i->0); '
                         f':return ({get(sig)} . "|" . {get("tx-rate")} . "|" . {get("rx-rate")} . "|" . '
                         + (f'{get(ccq)}' if ccq else '""') + f' . "|" . {get("interface")}) }}; :return ""')
                body += (f':do {{ :local f [:parse {rs(inner)}]; :local v [$f]; '
                         ':if ([:len $v] > 0) do={ :set out ($out . "wifi=" . $v . "\\n") } } on-error={}; ')
        return body
    raise ValueError('Unknown test.')


def parse_link(kind, p, lines):
    get = lambda k: next((v for kk, v in lines if kk == k), '')
    if kind == 'whoami':
        clients = {}
        for k, v in lines:
            if k == 'c':
                src = v.rsplit(':', 1)[0]
                if nt.is_ip(src):
                    clients[src] = clients.get(src, 0) + 1
        return {'clients': [{'ip': ip, 'conns': n} for ip, n in clients.items()]}
    if kind == 'hops':
        samples = {ip: [nt._link_reply(v) for k, v in lines if k == f'h{i}'] for i, ip in enumerate(p['hops'])}
        res = {'phase': p['phase'], 'samples': samples, 'gateway': nt.first_ipv4(get('gw')),
               'gw_samples': [nt._link_reply(v) for k, v in lines if k == 'hg'] or None,
               'inet_samples': [nt._link_reply(v) for k, v in lines if k == 'hi'] if p['upstream'] else None}
        eth = get('eth')
        if eth:
            a = (eth.split('|') + ['', ''])[:3]
            res['eth'] = {'rate': a[0], 'full_duplex': a[1], 'status': a[2]}
        wifi = get('wifi')
        if wifi:
            a = (wifi.split('|') + ['', '', '', ''])[:5]
            res['wifi'] = {'signal': a[0], 'tx': a[1], 'rx': a[2], 'ccq': a[3], 'interface': a[4]}
        return res
    raise ValueError('Unknown test.')


# ─────────────────────────────── who am I: enrich the router's answer with TapTap's inventory ───────────────────────────────

def enrich_whoami(router, result, hint=''):
    """Candidates (newest knowledge first) and, when it is clear, the one that is this device."""
    from .models import RouterDevice
    from .site_routers import physical_port
    out = []
    for c in result.get('clients', []):
        dev = RouterDevice.objects.filter(router=router, ip_address=c['ip']).order_by('-is_online', '-last_seen_at').first()
        mac = (c.get('mac') or (dev.mac_address if dev else '') or '').upper()
        out.append({'ip': c['ip'], 'mac': mac, 'name': c.get('hostname') or (dev.hostname if dev else '') or '',
                    'port': c.get('port') or (physical_port(dev) if dev else '') or c.get('interface', ''),
                    'conns': c.get('conns', 0), 'known': bool(dev)})
    chosen = ''
    if hint and any(c['ip'] == hint for c in out):
        chosen = hint
    elif len(out) == 1:
        chosen = out[0]['ip']
    result['candidates'], result['chosen'] = out, chosen
    if not out:
        result['note'] = (f'No device on {router.name} is talking to TapTap right now. Make sure this phone or laptop is on the Wi-Fi '
                          f'behind {router.name} (not mobile data), then try again — or pick a device from the list.')
    elif not chosen:
        result['note'] = 'Several devices on this router are using TapTap right now. Pick yours.'
    return result


# ─────────────────────────────── the way from the device to the MikroTik (database only) ───────────────────────────────

def _router_lan_ip(router, near_ip):
    nets = []
    try:
        from .routeros_analysis import g
        for row in ((router.config_snapshot.sections or {}).get('IP addresses') or {}).get('rows', []):
            nets.append(ipaddress.ip_interface(str(g(row, 'address'))))
    except Exception:
        return ''
    try:
        a = ipaddress.ip_address(near_ip)
    except ValueError:
        return ''
    for n in nets:
        if a in n.network:
            return str(n.ip)
    return ''


def _hop_from_entry(e, why):
    return {'key': e['key'], 'kind': 'box', 'name': e['name'], 'ip': e['ip'], 'mac': e['mac'], 'brand': e['brand'],
            'model': e.get('model', ''), 'role': e.get('role', 'router'), 'mode': e.get('mode') or e.get('mode_guess') or '',
            'confirmed': e['status'] == 'confirmed', 'source': why, 'online': e.get('online', False)}


def build_path(router, ip='', mac=''):
    """Device → … → MikroTik, ordered from the MikroTik outwards (the first box is the one cabled to it)."""
    from .models import RouterDevice, RouterNeighbor
    from .site_routers import collect, mac_norm, physical_port
    from . import geomap
    business = router.business
    qs = RouterDevice.objects.filter(router=router)
    dev = (qs.filter(ip_address=ip).order_by('-is_online', '-last_seen_at').first() if ip else None) or \
          (qs.filter(mac_address__iexact=mac).order_by('-is_online', '-last_seen_at').first() if mac else None)
    port = physical_port(dev) if dev else ''
    entries = collect(business)
    by_key = {e['key']: e for e in entries}
    me_mac = mac_norm(mac or (dev.mac_address if dev else ''))
    device = {'key': 'device', 'kind': 'device', 'name': (dev.hostname if dev else '') or 'This device', 'ip': ip or (dev.ip_address if dev else ''),
              'mac': me_mac, 'online': bool(dev and dev.is_online), 'connection': (dev.connection_type if dev else '') or ''}
    nat = next((e for e in entries if e['ip'] and e['ip'] == device['ip'] and e['router_id'] == router.pk), None)
    wifi_direct = bool(port) and port.lower().startswith(WIFI_PREFIXES)

    chain, guessed, others = [], False, []
    if not wifi_direct:
        start = None
        if nat:
            start = nat
        else:
            found = geomap.phone_target(business, device['ip'], site=router) if device['ip'] else {}
            start = by_key.get(found.get('key') or '')
            if not start and port:
                start = _saved_chain_end(entries, by_key, router, port)
        seen = set()
        while start and start['key'] not in seen and len(chain) < MAX_HOPS:
            seen.add(start['key'])
            chain.append(_hop_from_entry(start, 'Topology' if start['status'] == 'confirmed' else 'Found automatically'))
            start = by_key.get(start.get('parent_key') or '')
        chain.reverse()
        ordered = len(chain) > 1 or any(by_key.get(h['key'], {}).get('parent_key') for h in chain)
        # Other boxes on the same MikroTik port: in a series chain they are on the way too. Without a placed chain,
        # TapTap lists them and orders them by response time once measured.
        line = [e for e in entries if port and e['router_id'] == router.pk and e['port'] == port
                and e['key'] not in seen and e['mac'] != me_mac
                and (e['status'] == 'confirmed' or e.get('confidence') in ('likely', 'possible'))]
        if line and not ordered:
            extra = [_hop_from_entry(e, 'Same line (port ' + port + ')') for e in line]
            chain = (chain[:-1] + extra + chain[-1:]) if nat else (chain + extra)      # a NAT box is always the last stop
            guessed = len(chain) > 1
        elif line:
            others = [_hop_from_entry(e, 'Same port, not on the saved chain') for e in line]
        have = {h['mac'] for h in chain if h['mac']} | {me_mac}
        for n in RouterNeighbor.objects.filter(router=router, is_online=True).exclude(mac_address=''):
            nm = mac_norm(n.mac_address)
            if port and n.interface_name in (port,) and nm not in have and n.address and nt.is_ip(n.address.split(',')[0]) and not (chain and not guessed and len(chain) > 1):
                chain.append({'key': f'nb:{n.pk}', 'kind': 'box', 'name': n.identity or n.board or n.address, 'ip': n.address.split(',')[0],
                              'mac': nm, 'brand': n.platform, 'model': n.board, 'role': 'ap', 'mode': '', 'confirmed': False,
                              'source': f'Announces itself ({n.discovered_by or "LLDP/MNDP"})', 'online': True})
                have.add(nm)
                if nat and len(chain) > 1:
                    chain.insert(len(chain) - 2, chain.pop())
                guessed = guessed or len(chain) > 1
    if nat and chain and chain[-1]['key'] == nat['key']:
        chain[-1]['nat'] = True
    router_node = {'key': f'mt:{router.pk}', 'kind': 'mikrotik', 'name': router.name, 'ip': _router_lan_ip(router, device['ip']) if device['ip'] else '',
                   'port': port, 'link': 'wifi' if wifi_direct else ('cable' if port else '')}
    return {'device': device, 'hops': chain, 'router': router_node, 'port': port, 'wifi_direct': wifi_direct,
            'guessed': guessed, 'behind_nat': bool(nat), 'others': others,
            'note': _path_note(router, device, chain, port, wifi_direct, nat, guessed)}


def _saved_chain_end(entries, by_key, router, port):
    """The far end of a chain the owner placed on this port (series Wi-Fi: every box sits on the same MikroTik port)."""
    def depth(e, guard=0):
        d, cur, seen = 0, e, set()
        while cur and cur.get('parent_key') and cur['key'] not in seen and d < MAX_HOPS:
            seen.add(cur['key'])
            cur = by_key.get(cur['parent_key'])
            d += 1
        return d
    placed = [e for e in entries if e['status'] == 'confirmed' and e['router_id'] == router.pk and e['port'] == port
              and (e.get('parent_key') or any(x.get('parent_key') == e['key'] for x in entries))]
    if not placed:
        return None
    best = max(placed, key=depth)
    return best if depth(best) > 0 else None


def _path_note(router, device, chain, port, wifi_direct, nat, guessed):
    if not device['ip']:
        return 'TapTap could not find this device on the router.'
    if wifi_direct:
        return f'This device is connected straight to {router.name}’s own Wi-Fi ({port}).'
    if nat:
        return (f'Your device sits behind {nat["name"]} ({nat["ip"]}), a router that hides devices behind it (NAT). TapTap measures up '
                f'to that router; the last stretch from it to you is covered by the speed test from this device.')
    if not chain:
        return (f'No routers or access points are known on {router.name} › {port or "this port"}. If there are some, open Topology and add '
                f'them (or wait for the next inventory) so TapTap can measure each one.')
    if guessed:
        return (f'{len(chain)} boxes found on {router.name} › {port}. Their order is worked out from response times — save it '
                f'once it looks right, and future checks will use it.')
    return f'{len(chain)} box{"es" if len(chain) != 1 else ""} between you and {router.name}, as placed on Topology.'


def measured_ips(path):
    """The addresses the router pings, nearest first; the device last."""
    ips = [h['ip'] for h in path['hops'] if h.get('ip') and nt.is_ip(h['ip'])]
    if path['device'].get('ip') and nt.is_ip(path['device']['ip']) and not path['behind_nat']:
        ips.append(path['device']['ip'])
    out = []
    for ip in ips:
        if ip not in out:
            out.append(ip)
    return out[:MAX_HOPS]


# ─────────────────────────────── finding the slow link ───────────────────────────────

def _stats(samples):
    samples = samples or []
    got = [x for x in samples if x is not None]
    timed = [x for x in got if isinstance(x, (int, float))]
    return {'sent': len(samples), 'received': len(got), 'loss': round((len(samples) - len(got)) * 100 / len(samples)) if samples else None,
            'med': round(statistics.median(timed), 1) if timed else None, 'max': round(max(timed), 1) if timed else None,
            'jitter': round(statistics.pstdev(timed), 1) if len(timed) > 1 else None}


def _rate_mbps(text):
    m = re.match(r'([\d.]+)\s*([GMK])?', str(text or '').replace('bps', ''))
    if not m:
        return None
    v = float(m.group(1))
    return v * {'G': 1000, 'M': 1, 'K': 0.001, None: 1}[m.group(2)]


def _signal(text):
    m = re.search(r'-\d+', str(text or ''))
    return int(m.group(0)) if m else None


def analyze(path, idle, load=None, device=None, router_speed=None):
    """Every stop with its idle and loaded response, every link between stops judged, and one verdict.

    idle / load: the 'hops' results. device: {'down','up','rtt'} from the browser (Mbit/s, ms).
    router_speed: Mbit/s the MikroTik itself got to the Internet (speed test), if known."""
    idle, load, device = idle or {}, load or {}, device or {}
    stops = []
    for h in path['hops']:
        stops.append(dict(h))
    if not path['behind_nat'] and path['device'].get('ip'):
        stops.append(dict(path['device']))
    for s in stops:
        ip = s.get('ip')
        s['idle'] = _stats((idle.get('samples') or {}).get(ip)) if ip else None
        s['load'] = _stats((load.get('samples') or {}).get(ip)) if ip and load else None
    # No chain placed: put the boxes in order of their idle response (nearest answers first).
    if path.get('guessed'):
        boxes = [s for s in stops if s['kind'] == 'box']
        rest = [s for s in stops if s['kind'] != 'box']
        boxes.sort(key=lambda s: (bool(s.get('nat')), s['idle'] is None or s['idle']['med'] is None, (s['idle'] or {}).get('med') or 0))
        stops = boxes + rest
    findings = []
    links, prev = [], {'name': path['router']['name'], 'idle': {'med': 0.0, 'loss': 0}, 'load': {'med': 0.0, 'loss': 0}}
    for s in stops:
        li = {'from': prev['name'], 'to': s['name'], 'state': 'ok', 'why': '', 'idle_add': None, 'load_add': None, 'loss': None}
        si, sl = s.get('idle') or {}, s.get('load') or {}
        pi, pl = prev.get('idle') or {}, prev.get('load') or {}
        if si.get('received') == 0 and (not sl or sl.get('received') == 0):
            li['state'], li['why'] = 'unknown', f'{s["name"]} does not answer ping — this link is judged together with the next one.'
            s['silent'] = True
            links.append(li)
            continue                                    # keep `prev` so the next stop is compared with the last one that answered
        if si.get('med') is not None and pi.get('med') is not None:
            li['idle_add'] = round(si['med'] - pi['med'], 1)
        if sl.get('med') is not None and pl.get('med') is not None:
            li['load_add'] = round(sl['med'] - pl['med'], 1)
        loss_now = max([x for x in (si.get('loss'), sl.get('loss')) if x is not None] or [0])
        loss_before = max([x for x in (pi.get('loss'), pl.get('loss')) if x is not None] or [0])
        li['loss'] = loss_now
        is_device = s['kind'] == 'device'
        bloat = (li['load_add'] or 0) - max(li['idle_add'] or 0, 0)
        if loss_now >= 15 and loss_now - loss_before >= 10:
            li['state'] = 'bad'
            li['why'] = f'{loss_now}% of replies are lost from here on — the link into {s["name"]} drops packets.'
        elif bloat >= 60:
            li['state'] = 'bad'
            li['why'] = f'Under load this link adds about {round(bloat)} ms — it is the bottleneck: it cannot carry the traffic.'
        elif (li['idle_add'] or 0) >= (60 if is_device else 25):
            li['state'] = 'warn'
            li['why'] = f'Adds {li["idle_add"]} ms even when quiet — a weak or busy Wi-Fi link, or a long cable run with problems.'
        elif bloat >= 25 or (loss_now >= 5 and loss_now > loss_before):
            li['state'] = 'warn'
            li['why'] = (f'Slows down a little under load (+{round(bloat)} ms).' if bloat >= 25 else f'Loses a few replies ({loss_now}%).')
        if is_device and li['state'] != 'ok' and not load:
            li['why'] += ' (Phones save power, so their own replies can be slow when idle — run the full check while using this phone.)'
        links.append(li)
        prev = s
    # Upstream: the Internet line itself.
    gw_i, gw_l = _stats(idle.get('gw_samples')), _stats(load.get('gw_samples')) if load else None
    in_i, in_l = _stats(idle.get('inet_samples')), _stats(load.get('inet_samples')) if load else None
    upstream = {'gateway': idle.get('gateway') or load.get('gateway') or '', 'gw_idle': gw_i, 'gw_load': gw_l, 'inet_idle': in_i, 'inet_load': in_l}
    wan_bloat = ((in_l or {}).get('med') or 0) - ((in_i or {}).get('med') or 0) if in_l and in_l.get('med') is not None and in_i.get('med') is not None else 0
    # Port and radio the chain hangs from.
    port_note = None
    eth = idle.get('eth') or load.get('eth')
    if eth:
        rate = _rate_mbps(eth.get('rate'))
        half = str(eth.get('full_duplex', '')).lower() in ('false', 'no')
        if rate is not None and rate < 100 or half:
            port_note = (f'{path["router"]["name"]} › {path["port"]} runs at {eth.get("rate") or "?"}' + (' half duplex' if half else '') +
                         ' — a damaged cable, a bad connector or an old switch. A good cable gives 100 Mbps or 1 Gbps full duplex.')
            findings.append({'level': 'bad' if (rate or 100) <= 10 or half else 'warn', 'text': port_note})
    wifi = idle.get('wifi') or load.get('wifi')
    if wifi:
        sig = _signal(wifi.get('signal'))
        if sig is not None and sig <= -75:
            findings.append({'level': 'bad' if sig <= -82 else 'warn',
                             'text': f'The Wi-Fi signal into {path["router"]["name"]} is weak ({sig} dBm). Move or aim the antenna, or add a box in between; '
                                     f'better than −70 dBm is solid.'})
    # Speeds.
    down, rs_ = device.get('down'), router_speed
    chain_limit = None
    if down and rs_:
        ratio = down / rs_ if rs_ else 1
        chain_limit = ratio < 0.6
        if chain_limit:
            findings.append({'level': 'bad' if ratio < 0.3 else 'warn',
                             'text': f'This device gets {down} Mbit/s while {path["router"]["name"]} itself gets {rs_} Mbit/s from the Internet: '
                                     f'{round((1 - ratio) * 100)}% is lost between the router and you.'})
        else:
            findings.append({'level': 'ok', 'text': f'This device gets {down} Mbit/s — close to the router’s own {rs_} Mbit/s, so the Wi-Fi chain keeps up.'})
    if wan_bloat >= 80 and not any(li['state'] == 'bad' for li in links):
        findings.append({'level': 'warn', 'text': f'While you download, the Internet line itself slows down (+{round(wan_bloat)} ms to the Internet): '
                                                  f'the line from the ISP is full — the Wi-Fi chain is not the limit.'})
    worst = next((li for li in links if li['state'] == 'bad'), None) or next((li for li in links if li['state'] == 'warn'), None)
    if worst:
        level = 'bad' if worst['state'] == 'bad' else 'warn'
        title = f'Problem between {worst["from"]} and {worst["to"]}'
        detail = worst['why']
    elif port_note:
        level, title, detail = 'warn', f'Check the cable on {path["port"]}', port_note
    elif chain_limit:
        level, title, detail = 'warn', 'Your Wi-Fi chain is slower than the Internet line', 'Every stop answers well, but this device is much slower than the router.'
    elif wan_bloat >= 80:
        level, title, detail = 'warn', 'The Internet line is the limit', 'The Wi-Fi chain to the router is fine; the line from the ISP is full when traffic flows.'
    elif in_i and in_i.get('received') == 0 and gw_i and gw_i.get('received') == 0 and not down:
        level, title, detail = 'bad', 'The router does not reach the Internet', 'Every stop on your side answers, but the ISP side does not. Run the Internet check.'
    else:
        level, title = 'ok', 'Your connection looks healthy'
        detail = ('Every stop between you and the router answers quickly' + (' even under load.' if load else '.')) if stops else 'The router and the Internet answer well.'
    findings.extend({'level': li['state'], 'text': f'{li["from"]} → {li["to"]}: {li["why"]}'} for li in links if li['state'] in ('bad', 'warn') and li is not worst)
    return {'level': level, 'title': title, 'detail': detail, 'stops': stops, 'links': links, 'upstream': upstream,
            'findings': findings, 'device': device, 'router_speed': router_speed, 'eth': eth, 'wifi': wifi, 'ordered_by_time': bool(path.get('guessed'))}
