"""Network test tools — run from the MikroTik itself, so results show what the router (and its customers) see.

Tools: ping, traceroute, DNS lookup, website check, download speed test, and the one-click Internet check
(router → ISP modem → Internet → DNS → web) that explains in plain words where the problem is.

How a test reaches the router (same rules as everything else in TapTap):

* **Direct API / TapTap Tunnel** — run now over the RouterOS API; the answer comes back in the same request.
* **TapTap Link** — a ``nettest`` command is queued. The router runs a short script at its next check-in and
  uploads the answer (signed with the command's one-time code) to ``/api/agent/v1/nettest``. The page shows
  "waiting for the router" and fills in when it arrives.

Every target is checked here before it goes anywhere near a router: host names / IPs and URLs only, never
free text, and every value is quoted with ``agent.rs`` in Link scripts.
"""
from __future__ import annotations

import ipaddress
import re
import time
from urllib.parse import urlsplit

from django.core.cache import cache
from django.utils import timezone

KINDS = {
    'doctor': 'Internet check',
    'ping': 'Ping',
    'trace': 'Traceroute',
    'dns': 'DNS lookup',
    'web': 'Website check',
    'speed': 'Speed test',
}
CHECK_IPS = ('1.1.1.1', '8.8.8.8')
CHECK_NAME = 'google.com'
CHECK_URL = 'http://connectivitycheck.gstatic.com/generate_204'
SPEED_URL = 'https://speed.cloudflare.com/__down?bytes={n}'
SPEED_PROBE = 1_000_000          # first, small download: decides how big the real one is
SPEED_SIZES = ((40, 25_000_000), (4, 10_000_000), (0, 3_000_000))   # (Mbps from the probe ≥, bytes)
LINK_SPEED_BYTES = 3_000_000     # TapTap Link: one fixed download (the script must finish within a check-in)
RATE_TESTS = (40, 600)           # per business: 40 tests per 10 minutes
RATE_PINGS = (150, 600)          # pings have their own allowance (Keep pinging runs every few seconds)
SPEED_GAP = 120                  # seconds between two speed tests on one router (they use real data)

HOST_RE = re.compile(r'^(?=.{1,253}$)(?!-)([a-z0-9-]{1,63}\.)*[a-z0-9-]{1,63}$')
PATH_RE = re.compile(r'^[A-Za-z0-9._~/%+:@!$,;=-]*$')
QUERY_RE = re.compile(r'^[A-Za-z0-9._~/%+:@!,;=&-]*$')


# ─────────────────────────────── checking what the user typed ───────────────────────────────

def clean_host(value):
    """A host name or IP address (no ports, no paths). Returns it normalised or raises ValueError."""
    v = str(value or '').strip().lower()
    v = re.sub(r'^[a-z]+://', '', v).split('/')[0]
    if v.startswith('[') and v.endswith(']'):
        v = v[1:-1]
    if not v:
        raise ValueError('Type an address, e.g. 8.8.8.8 or google.com.')
    try:
        ip = ipaddress.ip_address(v)
    except ValueError:
        if not HOST_RE.match(v) or v.endswith('-') or '..' in v:
            raise ValueError(f'“{value}” is not an address TapTap can test. Use something like 8.8.8.8 or google.com.')
        return v
    if ip.is_unspecified or ip.is_multicast or ip.is_loopback:
        raise ValueError(f'{v} cannot be tested.')
    return str(ip)


def clean_url(value):
    v = str(value or '').strip()
    if not v:
        raise ValueError('Type a website, e.g. google.com or https://example.com/page.')
    if re.match(r'^[a-z][a-z0-9+.-]*://', v, re.I) and not re.match(r'^https?://', v, re.I):
        raise ValueError('Only http:// and https:// websites can be checked.')
    if not re.match(r'^https?://', v, re.I):
        v = 'https://' + v
    if len(v) > 300 or any(c in v for c in ' "\'\\<>{}|^`$\n\r\t'):
        raise ValueError('That web address has characters TapTap cannot send to the router.')
    p = urlsplit(v)
    host = clean_host(p.hostname or '')
    try:
        port = p.port
    except ValueError:
        raise ValueError('That web address has an invalid port.')
    if p.username or p.password or not PATH_RE.match(p.path or '') or not QUERY_RE.match(p.query or ''):
        raise ValueError('That web address has characters TapTap cannot send to the router.')
    netloc = (f'[{host}]' if ':' in host else host) + (f':{port}' if port else '')
    return f'{p.scheme.lower()}://{netloc}{p.path or "/"}' + (f'?{p.query}' if p.query else '')


def is_ip(v):
    try:
        ipaddress.ip_address(str(v))
        return True
    except ValueError:
        return False


def clean_params(kind, data):
    """Validated parameters for one test (raises ValueError with a readable message)."""
    if kind not in KINDS:
        raise ValueError('Unknown test.')
    out = {}
    if kind in ('ping', 'trace', 'dns'):
        out['target'] = clean_host(data.get('target'))
    if kind == 'ping':
        try:
            out['count'] = max(1, min(20, int(data.get('count') or 5)))
            out['size'] = max(28, min(1500, int(data.get('size') or 56)))
        except (TypeError, ValueError):
            raise ValueError('Count and size must be numbers.')
    if kind == 'web':
        out['target'] = clean_url(data.get('target'))
    if kind == 'speed':
        out['target'] = 'speed.cloudflare.com'
    if kind == 'doctor':
        out['target'] = 'Internet'
    return out


def limit_check(business, router, kind):
    """Refuse floods: a busy hand on the button must not hammer the router (or the customer's data bundle)."""
    n, window = RATE_PINGS if kind == 'ping' else RATE_TESTS     # "Keep pinging" sends many small pings
    key = f'tt:nt:rate:{business.pk}:{"p" if kind == "ping" else "t"}'
    count = cache.get(key, 0)
    if count >= n:
        raise ValueError('Too many tests in the last few minutes — wait a little and try again.')
    if kind == 'speed':
        k2 = f'tt:nt:speed:{router.pk}'
        if cache.get(k2):
            raise ValueError('A speed test ran on this router a moment ago. Wait two minutes — each test uses real data.')
        cache.set(k2, 1, SPEED_GAP)
    cache.set(key, count + 1, window)


# ─────────────────────────────── small parsers ───────────────────────────────

_UNITS = {'h': 3_600_000, 'm': 60_000, 's': 1000, 'ms': 1, 'us': 0.001}


def parse_ms(value):
    """RouterOS durations as milliseconds: '12ms', '12ms345us', '1s200ms', '345us', '00:00:00.012', '12' → float."""
    v = str(value or '').strip()
    if not v or v in ('-', 'timeout'):
        return None
    m = re.fullmatch(r'(\d+):(\d{2}):(\d{2})(?:\.(\d+))?', v)
    if m:
        h, mi, s, frac = m.groups()
        return (int(h) * 3600 + int(mi) * 60 + int(s)) * 1000 + (float('0.' + frac) * 1000 if frac else 0)
    if re.fullmatch(r'\d+(\.\d+)?', v):
        return float(v)
    parts = re.findall(r'(\d+(?:\.\d+)?)(ms|us|h|m|s)', v)
    if not parts or ''.join(a + b for a, b in parts) != v:
        return None
    return sum(float(a) * _UNITS[b] for a, b in parts)


def parse_seconds(value):
    ms = parse_ms(value)
    return ms / 1000 if ms is not None else None


def parse_kib(value):
    """Fetch's 'downloaded' (KiB, sometimes '1234KiB' / '1.2MiB') → bytes."""
    v = str(value or '').strip()
    m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(KiB|MiB|GiB|B)?', v)
    if not m:
        return 0
    mult = {'B': 1, 'KiB': 1024, 'MiB': 1024 ** 2, 'GiB': 1024 ** 3, None: 1024}[m.group(2)]
    return int(float(m.group(1)) * mult)


def first_ipv4(text):
    m = re.search(r'\b(\d{1,3}(?:\.\d{1,3}){3})\b', str(text or ''))
    if m and is_ip(m.group(1)):
        return m.group(1)
    return ''


def ping_stats(times):
    """times: list of ms or None (lost) → summary with a plain-words quality."""
    sent = len(times)
    received = len([t for t in times if t is not None])
    got = [t for t in times if isinstance(t, (int, float))]          # replies with a time
    loss = round((sent - received) * 100 / sent) if sent else 100
    st = {'sent': sent, 'received': received, 'loss': loss,
          'times': [t if t is None or t == 'ok' else round(t, 1) for t in times],
          'min': round(min(got), 1) if got else None, 'avg': round(sum(got) / len(got), 1) if got else None,
          'max': round(max(got), 1) if got else None, 'jitter': None}
    if len(got) > 1:
        st['jitter'] = round(sum(abs(a - b) for a, b in zip(got, got[1:])) / (len(got) - 1), 1)
    st['quality'], st['quality_note'] = quality(st)
    return st


def quality(st):
    if not st['received']:
        return 'down', 'No replies at all.'
    if st['avg'] is None:
        return ('good' if st['loss'] < 5 else 'fair' if st['loss'] < 20 else 'poor'), 'Answers (this router did not report reply times).'
    avg, loss, jit = st['avg'] or 0, st['loss'], st['jitter'] or 0
    if loss >= 20 or avg >= 400:
        return 'poor', 'Customers will notice: pages hang, calls drop.'
    if loss >= 5 or avg >= 150 or jit >= 60:
        return 'fair', 'Browsing works; video calls may stutter.'
    if avg >= 60 or jit >= 20:
        return 'good', 'Fine for browsing, video and calls.'
    return 'excellent', 'Fast and steady.'


def zone(addr):
    """Where an address sits, for the traceroute path."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return 'unknown'
    if ip.is_private and not str(ip).startswith('100.'):
        return 'local'
    if ipaddress.ip_address(addr) in ipaddress.ip_network('100.64.0.0/10'):
        return 'isp'
    return 'internet'


# ─────────────────────────────── running over the RouterOS API ───────────────────────────────

def _api_ping(svc, target, count, size=56, interval='500ms'):
    rows = svc.resource('/').call('ping', {'address': target, 'count': str(count), 'size': str(size), 'interval': interval}) or []
    times, host = [], ''
    for r in rows:
        if 'seq' not in r and 'time' not in r and 'status' not in r:
            continue
        host = host or str(r.get('host') or '')
        status = str(r.get('status') or '').lower()
        t = parse_ms(r.get('time')) if not status or status == 'ok' else None
        times.append(t)
    times = times[:count] or [None] * count
    return times, host


def run_ping(svc, p):
    times, host = _api_ping(svc, p['target'], p['count'], p['size'])
    res = ping_stats(times)
    res['host'] = host or (p['target'] if is_ip(p['target']) else '')
    return res


def run_trace(svc, p):
    rows = svc.resource('/tool').call('traceroute', {'address': p['target'], 'count': '1', 'max-hops': '20', 'timeout': '1s'}) or []
    sections = {}
    for i, r in enumerate(rows):
        sections.setdefault(str(r.get('.section', '0')), []).append(r)
    final = list(sections.values())[-1] if sections else []
    hops = [{'address': str(r.get('address') or ''), 'loss': _num(r.get('loss')),
             'ms': parse_ms(r.get('last') or r.get('avg') or r.get('best')), 'status': str(r.get('status') or '')} for r in final]
    return trace_result(hops)


def _num(v):
    try:
        return int(float(str(v or '0').rstrip('%')))
    except ValueError:
        return 100


def trace_result(hops):
    hops = [h for h in hops][:30]
    # drop the empty tail RouterOS keeps after the destination answered
    while len(hops) > 1 and not hops[-1]['address'] and hops[-2]['address']:
        if any(x['address'] for x in hops[:-1]):
            hops.pop()
        else:
            break
    for n, h in enumerate(hops, 1):
        h['n'] = n
        h['zone'] = zone(h['address']) if h['address'] else 'silent'
        h['ms'] = round(h['ms'], 1) if h.get('ms') is not None else None
    reached = bool(hops) and bool(hops[-1]['address']) and hops[-1]['loss'] < 100
    worst = None
    for h in hops:
        if h['address'] and h['ms'] is not None and h['ms'] > 150 and (not worst or h['ms'] > worst['ms']):
            worst = h
    note = ('Reached the destination in %d hops.' % len(hops)) if reached else (
        'The path stops answering after hop %d.' % max([h['n'] for h in hops if h['address']] or [0]))
    if worst:
        note += f' Latency jumps at hop {worst["n"]} ({worst["address"]}, {worst["ms"]} ms).'
    return {'hops': hops, 'reached': reached, 'note': note}


def _dns_servers(svc):
    rows = svc.safe_get('/ip/dns') if hasattr(svc, 'safe_get') else []
    row = rows[0] if rows else {}
    return _servers(row.get('servers')), _servers(row.get('dynamic-servers'))


def _servers(v):
    return [x for x in re.split(r'[,;\s]+', str(v or '')) if x]


def run_dns(svc, p):
    started = time.monotonic()
    res = {'name': p['target'], 'ip': '', 'ok': False, 'ms': None}
    try:
        _, host = _api_ping(svc, p['target'], 1, interval='100ms')
        res['ip'] = host
        res['ok'] = bool(host)
        res['ms'] = round((time.monotonic() - started) * 1000)
    except Exception as exc:
        res['error'] = _ros_error(exc)
    res['servers'], res['dynamic'] = _dns_servers(svc)
    return dns_result(res)


def dns_result(res):
    used = res.get('servers') or res.get('dynamic') or []
    if res['ok']:
        res['note'] = f'{res["name"]} → {res["ip"]}' + (f' (asked {", ".join(used[:3])})' if used else '')
    elif not used:
        res['note'] = 'The router has no DNS server set. Add 1.1.1.1 and 8.8.8.8 under IP › DNS (and allow remote requests for customers).'
    else:
        res['note'] = f'{res["name"]} could not be looked up through {", ".join(used[:3])}. Try other DNS servers such as 1.1.1.1 and 8.8.8.8.'
    return res


def _fetch(svc, url, seconds):
    started = time.monotonic()
    try:
        rows = svc.resource('/tool').call('fetch', {'url': url, 'output': 'none', 'duration': f'{seconds}s'}) or []
    except Exception as exc:
        return {'ok': False, 'error': _ros_error(exc), 'seconds': round(time.monotonic() - started, 2)}
    last = rows[-1] if rows else {}
    return {'ok': str(last.get('status', 'finished')).lower() == 'finished', 'bytes': parse_kib(last.get('downloaded')),
            'seconds': round(time.monotonic() - started, 2), 'status': str(last.get('status') or '')}


def _ros_error(exc):
    text = str(exc)
    m = re.search(r"message[\"']?\s*[:=]\s*b?[\"']([^\"']+)", text)
    return (m.group(1) if m else text)[:200]


def web_result(url, f):
    res = {'url': url, 'ok': f['ok'], 'ms': round(f['seconds'] * 1000) if f.get('seconds') is not None else None,
           'bytes': f.get('bytes', 0), 'error': f.get('error', '')}
    code = re.search(r'<(\d{3})[^>]*>', res['error'] or '')
    res['http'] = int(code.group(1)) if code else (200 if f['ok'] else None)
    if f['ok']:
        res['note'] = f'The website answered in {res["ms"]} ms.' + (' That is slow — customers will feel it.' if (res['ms'] or 0) > 3000 else '')
    elif res['http']:
        res['note'] = f'The website answered with an error ({res["http"]}). The router reaches it, but the page itself has a problem.'
    elif 'resolve' in (res['error'] or '').lower():
        res['note'] = 'The website name could not be looked up — a DNS problem (see the DNS tool).'
    elif 'timeout' in (res['error'] or '').lower() or 'timed out' in (res['error'] or '').lower():
        res['note'] = 'The website did not answer in time — the line may be down or very slow, or the site blocks this connection.'
    else:
        res['note'] = 'The website could not be reached' + (f': {res["error"]}' if res['error'] else '.')
    return res


def run_web(svc, p):
    return web_result(p['target'], _fetch(svc, p['target'], 20))


def run_speed(svc, p):
    probe = _fetch(svc, SPEED_URL.format(n=SPEED_PROBE), 25)
    if not probe['ok'] or not probe.get('bytes'):
        return speed_result([], probe.get('error') or 'The test download did not finish.')
    mbps = probe['bytes'] * 8 / max(probe['seconds'], 0.05) / 1e6
    size = next(b for floor, b in SPEED_SIZES if mbps >= floor)
    main = _fetch(svc, SPEED_URL.format(n=size), 30)
    runs = [probe] + ([main] if main['ok'] and main.get('bytes') else [])
    return speed_result(runs, '' if main['ok'] else main.get('error', ''))


def speed_result(runs, error=''):
    good = [r for r in runs if r.get('bytes') and r.get('seconds')]
    if not good:
        return {'ok': False, 'error': error or 'The test download did not finish.', 'note': 'The speed test could not download anything — run the Internet check first.'}
    best = max(good, key=lambda r: r['bytes'])          # the big download is the honest number
    mbps = round(best['bytes'] * 8 / max(best['seconds'], 0.05) / 1e6, 1)
    used = sum(r['bytes'] for r in good)
    people = max(1, int(mbps // 1)) if mbps >= 1 else 0
    res = {'ok': True, 'mbps': mbps, 'bytes_used': used, 'mb_used': round(used / 1e6, 1), 'seconds': round(best['seconds'], 1),
           'people': people, 'error': error}
    res['rating'] = 'excellent' if mbps >= 50 else 'good' if mbps >= 15 else 'fair' if mbps >= 5 else 'poor'
    res['note'] = (f'{mbps} Mbit/s download from the router to the Internet. Rough guide: about {people} customer'
                   f'{"s" if people != 1 else ""} can browse comfortably at the same time (≈1 Mbit/s each).') if people else (
        f'{mbps} Mbit/s — under 1 Mbit/s. Even one customer will find browsing slow.')
    return res


def run_doctor(svc):
    facts = {'gateway': '', 'gw': None, 'wan': [], 'dns': {}, 'web': {}}
    gw = ''
    try:
        for r in svc.resource('/ip/route').get():
            if str(r.get('dst-address')) == '0.0.0.0/0' and str(r.get('active', 'false')).lower() in ('true', 'yes'):
                gw = str(r.get('immediate-gw') or r.get('gateway') or '')
                break
    except Exception:
        pass
    facts['gateway'] = gw
    facts['has_route'] = bool(gw)
    gip = first_ipv4(gw)
    if gip:
        try:
            t, _ = _api_ping(svc, gip, 3, interval='300ms')
            facts['gw'] = {'ip': gip, **_short(t)}
        except Exception:
            facts['gw'] = {'ip': gip, 'sent': 3, 'received': 0, 'avg': None}
    for ip in CHECK_IPS:
        try:
            t, _ = _api_ping(svc, ip, 3, interval='300ms')
            facts['wan'].append({'ip': ip, **_short(t)})
        except Exception as exc:
            facts['wan'].append({'ip': ip, 'sent': 3, 'received': 0, 'avg': None, 'error': _ros_error(exc)})
    d = run_dns(svc, {'target': CHECK_NAME})
    facts['dns'] = {'ok': d['ok'], 'ip': d['ip'], 'servers': d['servers'] or d['dynamic']}
    f = _fetch(svc, CHECK_URL, 10)
    facts['web'] = {'ok': f['ok'], 'ms': round(f['seconds'] * 1000), 'error': f.get('error', '')}
    return diagnose(facts)


def _short(times):
    s = ping_stats(times)
    return {'sent': s['sent'], 'received': s['received'], 'avg': s['avg'], 'loss': s['loss']}


def diagnose(facts):
    """Facts from either channel → a chain of checks and one plain-words verdict with fixes."""
    gw, wan, dns, web = facts.get('gw'), facts.get('wan') or [], facts.get('dns') or {}, facts.get('web') or {}
    wan_ok = any(w.get('received') for w in wan)
    wan_avg = [w['avg'] for w in wan if w.get('avg') is not None]
    wan_loss = max([w.get('loss', 0) for w in wan if w.get('received')] or [0])
    # A web page that loads proves the Internet works, whatever ping says: many ISPs and firewalls drop ping.
    pings_blocked = not wan_ok and bool(web.get('ok'))
    online = wan_ok or pings_blocked
    chain = [
        {'key': 'router', 'label': 'Your router', 'state': 'ok', 'detail': 'Answered TapTap'},
        {'key': 'modem', 'label': 'ISP / modem', 'state': 'unknown', 'detail': ''},
        {'key': 'internet', 'label': 'Internet', 'state': 'ok' if wan_ok else ('warn' if pings_blocked else 'bad'),
         'detail': (f'{round(sum(wan_avg) / len(wan_avg))} ms' if wan_avg else ('answers' if wan_ok else ('ping blocked' if pings_blocked else 'no reply')))},
        {'key': 'dns', 'label': 'Names (DNS)', 'state': 'ok' if dns.get('ok') else 'bad', 'detail': dns.get('ip') or 'fails'},
        {'key': 'web', 'label': 'Websites', 'state': 'ok' if web.get('ok') else 'bad',
         'detail': (web.get('text') or (f'{web["ms"]} ms' if web.get('ms') is not None else 'loads')) if web.get('ok') else 'fails'},
    ]
    if not facts.get('has_route', True) and not online:
        chain[1].update(state='bad', detail='no default route')
    elif gw:
        chain[1].update(state='ok' if gw.get('received') else ('warn' if online else 'bad'),
                        detail=(f'{gw["ip"]} · {gw["avg"]} ms' if gw.get('avg') is not None else f'{gw["ip"]} · answers') if gw.get('received') else f'{gw["ip"]} · no reply')
    elif facts.get('gateway'):
        chain[1].update(state='ok' if online else 'unknown', detail=facts['gateway'][:40])
    elif online:
        chain[1].update(state='ok', detail='working')
    fixes = []
    if pings_blocked:
        level, title = 'ok', 'The Internet is working'
        detail = ('Websites load and names are looked up, but pings get no reply. Your ISP or a firewall rule is blocking ping — '
                  'customers are not affected, but the Ping and Traceroute tools will show no replies.') if dns.get('ok') else (
                  'A web page loads, but pings get no reply (blocked by the ISP or a firewall rule).')
        fixes = ['Nothing to fix for customers.',
                 'If you want ping to work: under IP › Firewall › Filter, look for a rule that drops ICMP on the output chain, '
                 'or ask your ISP whether they block ping.']
    elif not facts.get('has_route', True):
        level, title = 'bad', 'No Internet line on the router'
        detail = 'The router has no active default route, so nothing can leave your network.'
        fixes = ['Check the cable from the ISP modem/antenna to the router’s Internet port.',
                 'Open Internet lines: the DHCP client or PPPoE on the Internet port must show “bound”/“connected”.',
                 'Restart the ISP modem, wait two minutes, then run this check again.']
    elif not wan_ok and gw and gw.get('received'):
        level, title = 'bad', 'The modem answers, but nothing beyond it'
        detail = f'Your router reaches the ISP equipment ({gw["ip"]}) but no Internet address answers.'
        fixes = ['The ISP line is down or the data bundle has run out — check with your ISP.',
                 'Restart the ISP modem/antenna and run the check again.',
                 'If you have a second line, set it up under Internet lines so customers fail over automatically.']
    elif not wan_ok:
        level, title = 'bad', 'No Internet'
        detail = 'Neither the ISP equipment nor any Internet address answers the router.'
        fixes = ['Check the power and cable of the ISP modem/antenna.',
                 'Restart the ISP modem and the router’s Internet port (Ports page).',
                 'Call your ISP if it stays down after a restart.']
    elif not dns.get('ok'):
        level, title = 'bad', 'Internet works, but names do not (DNS)'
        detail = 'Addresses like 1.1.1.1 answer, but google.com cannot be looked up. To customers this looks exactly like “no Internet”.'
        fixes = ['Under IP › DNS set servers 1.1.1.1 and 8.8.8.8 and tick “Allow remote requests”.',
                 'If your ISP’s DNS is set there, it may be down — replace it.']
    elif not web.get('ok'):
        level, title = 'warn', 'Websites do not load'
        detail = 'The Internet and DNS answer, but a test web page did not load.'
        fixes = ['A firewall rule or web proxy on the router may block web traffic.',
                 'The ISP may be filtering — try the Website tool on a few different sites.']
    elif wan_loss >= 20 or (wan_avg and min(wan_avg) >= 300):
        level, title = 'warn', 'Internet works but is unstable'
        detail = f'Replies are slow or getting lost (up to {wan_loss}% loss). Customers will see slow pages and failed calls.'
        fixes = ['Run a Speed test and a Traceroute to see where it slows down.',
                 'Check the signal of the ISP antenna/4G modem, and whether a customer is downloading heavily (Traffic page).']
    else:
        level, title = 'ok', 'The Internet is working'
        avg = round(sum(wan_avg) / len(wan_avg)) if wan_avg else None
        detail = 'The router reaches the Internet, looks up names and loads web pages' + (f' (about {avg} ms).' if avg else '.')
        fixes = ['If a customer still has no Internet, the problem is between them and the router: Wi-Fi signal, the login page, or their voucher (Voucher checker / Active users).']
    return {'level': level, 'title': title, 'detail': detail, 'fixes': fixes, 'chain': chain, 'facts': facts}


RUNNERS = {'ping': run_ping, 'trace': run_trace, 'dns': run_dns, 'web': run_web, 'speed': run_speed}


def run_api(test):
    """Run one test now over the RouterOS API (Direct API or TapTap Tunnel)."""
    from .mikrotik import MikroTikService
    svc = MikroTikService(test.router, timeout=45).connect()
    try:
        if test.kind == 'doctor':
            return run_doctor(svc)
        return RUNNERS[test.kind](svc, test.params)
    finally:
        svc.close()


def summary(test):
    """One line for the history list."""
    r = test.result or {}
    if test.status == 'waiting':
        return 'Waiting for the router…'
    if test.status == 'failed':
        return r.get('error') or 'Failed'
    if test.kind == 'doctor':
        return r.get('title', '')
    if test.kind == 'ping':
        return f'{r.get("received", 0)}/{r.get("sent", 0)} replies' + (f' · {r["avg"]} ms' if r.get('avg') is not None else '')
    if test.kind == 'trace':
        return f'{len(r.get("hops", []))} hops' + (' · reached' if r.get('reached') else ' · stopped')
    if test.kind == 'dns':
        return r.get('ip') or 'Not found'
    if test.kind == 'web':
        return f'OK · {r.get("ms")} ms' if r.get('ok') else (f'Error {r["http"]}' if r.get('http') else 'Not reachable')
    if test.kind == 'speed':
        return f'{r["mbps"]} Mbit/s' if r.get('ok') else 'Did not finish'
    return ''


# ─────────────────────────────── TapTap Link: the router-side script ───────────────────────────────

def _resolve(rs, target):
    """RouterOS snippet that puts the target's IP in $ip (or a resolve error in $out)."""
    if is_ip(target):
        return f':local ip {rs(target)}; '
    return f':local ip ""; :do {{ :set ip [:resolve {rs(target)}] }} on-error={{ :set out "err=resolve\\n" }}; '


def _ping_snippet(addr, count, size, prefix):
    """RouterOS: ping ``addr`` and append ``<prefix><received>|<avg ms>`` to $out.

    ``/tool flood-ping`` reports only through ``do={}`` (it has no ``as-value``), and it gives the reply time.
    If it errors or reports nothing, plain ``/ping`` (returns the number of replies) still tells whether the
    address answers; the time is then left empty. Each ping runs in its own block so names never clash."""
    size_arg = f' size={int(size)}' if size else ''
    return ('{ :local rc 0; :local av ""; '
            f':do {{ /tool flood-ping address={addr} count={int(count)}{size_arg} do={{ '
            f':if ($sent = {int(count)}) do={{ :set rc $received; :set av $"avg-rtt" }} }} }} on-error={{}}; '
            f':if ($rc = 0) do={{ :set av ""; :do {{ :set rc [/ping {addr} count={int(count)}{size_arg}] }} on-error={{}} }}; '
            f':set out ($out . "{prefix}" . $rc . "|" . $av . "\\n") }}; ')


def link_body(cmd, url, check, nonce_value):
    from .agent import rs
    p = cmd.params
    kind, target = p.get('kind'), p.get('target', '')
    upload = rs(f'{url}/api/agent/v1/nettest?c={cmd.pk}&n={nonce_value}')
    head = ':local out ""; '
    if kind == 'ping':
        body = (_resolve(rs, target) + ':if ([:len $ip] > 0) do={ :set out ("ip=" . $ip . "\\n"); '
                f':for i from=1 to={int(p["count"])} do={{ ' + _ping_snippet('$ip', 1, p['size'], 'r=') + ':delay 400ms } }; ')
    elif kind == 'trace':
        body = (_resolve(rs, target) + ':if ([:len $ip] > 0) do={ :do { '
                ':foreach h in=[/tool traceroute address=$ip count=1 max-hops=20 timeout=1s as-value] do={ '
                ':set out ($out . "h=" . ($h->"address") . "|" . ($h->"loss") . "|" . ($h->"last") . "\\n") } } '
                'on-error={ :set out "err=traceroute\\n" } }; ')
    elif kind == 'dns':
        body = (f':do {{ :set out ("ip=" . [:resolve {rs(target)}] . "\\n") }} on-error={{ :set out "err=resolve\\n" }}; '
                + _dns_lines())
    elif kind in ('web', 'speed'):
        u = target if kind == 'web' else SPEED_URL.format(n=LINK_SPEED_BYTES)
        secs = 20 if kind == 'web' else 40
        body = (f':do {{ :local r [/tool fetch url={rs(u)} output=none as-value duration={secs}s]; '
                ':set out ("st=" . ($r->"status") . "\\n" . "dl=" . ($r->"downloaded") . "\\n" . "du=" . ($r->"duration") . "\\n") } '
                'on-error={ :set out "err=fetch\\n" }; ')
    elif kind == 'doctor':
        body = (':local g ""; :do { :set g [:tostr [/ip route get ([/ip route find where dst-address="0.0.0.0/0" active]->0) gateway]] } on-error={}; '
                ':set out ("gw=" . $g . "\\n"); '
                ':local gip [:toip $g]; :if ([:typeof $gip] = "ip") do={ ' + _ping_snippet('$gip', 3, 0, 'gwp=') + '} else={ :set out ($out . "gwp=x\\n") }; '
                + ''.join(_ping_snippet(ip, 3, 0, f'p={ip}|') for ip in CHECK_IPS)
                + f':do {{ :set out ($out . "dns=" . [:resolve {rs(CHECK_NAME)}] . "\\n") }} on-error={{ :set out ($out . "dns=x\\n") }}; '
                + _dns_lines()
                + f':do {{ :local r [/tool fetch url={rs(CHECK_URL)} output=none as-value duration=10s]; '
                ':set out ($out . "web=" . ($r->"status") . "|" . ($r->"duration") . "\\n") } on-error={ :set out ($out . "web=x\\n") }; ')
    else:
        raise ValueError('Unknown test.')
    send = (f'/tool fetch url={upload} http-method=post http-header-field="Content-Type: text/plain" '
            f'http-data=$out output=none check-certificate={check} duration=15s idle-timeout=10s')
    return '{ ' + head + body + send + ' }'


def _dns_lines():
    return (':do { :set out ($out . "servers=" . [:tostr [/ip dns get servers]] . "\\n") } on-error={}; '
            ':do { :set out ($out . "dyn=" . [:tostr [/ip dns get dynamic-servers]] . "\\n") } on-error={}; ')


def _lines(body):
    out = []
    for line in (body or b'').decode('utf-8', 'replace').splitlines()[:200]:
        k, _, v = line.partition('=')
        out.append((k.strip(), v.strip()))
    return out


def parse_link(kind, params, body):
    """The router's upload → the same result shape the API runners produce."""
    lines = _lines(body)
    get = lambda k: next((v for kk, v in lines if kk == k), '')
    if get('err') == 'resolve' and kind in ('ping', 'trace', 'dns'):
        if kind == 'dns':
            return dns_result({'name': params['target'], 'ip': '', 'ok': False, 'ms': None,
                               'servers': _servers(get('servers')), 'dynamic': _servers(get('dyn'))})
        return {'error': f'{params["target"]} could not be looked up (DNS).', 'failed': True}
    if kind == 'ping':
        times = [_link_reply(v) for k, v in lines if k == 'r']
        res = ping_stats(times or [None] * int(params.get('count', 1)))
        res['host'] = get('ip')
        return res
    if kind == 'trace':
        if get('err'):
            return {'error': 'This router could not run a traceroute for TapTap (it needs RouterOS 7).', 'failed': True}
        hops = []
        for k, v in lines:
            if k == 'h':
                a = (v.split('|') + ['', '', ''])[:3]
                hops.append({'address': a[0], 'loss': _num(a[1] or '100'), 'ms': parse_ms(a[2]), 'status': ''})
        return trace_result(hops)
    if kind == 'dns':
        ip = get('ip')
        return dns_result({'name': params['target'], 'ip': ip, 'ok': bool(ip), 'ms': None,
                           'servers': _servers(get('servers')), 'dynamic': _servers(get('dyn'))})
    if kind in ('web', 'speed'):
        if get('err'):
            f = {'ok': False, 'error': 'did not answer', 'seconds': None}
        else:
            secs = parse_seconds(get('du'))
            f = {'ok': get('st') == 'finished', 'bytes': parse_kib(get('dl')), 'seconds': max(secs or 0, 1.0) if secs is not None else None}
        if kind == 'web':
            res = web_result(params['target'], f)
            if res['ok']:
                _, text = _whole_seconds(get('du'))
                res['ms'], res['time_text'] = None, text
                res['note'] = f'The website answered ({text}; this router reports whole seconds).' if text else 'The website answered.'
            return res
        return speed_result([f] if f.get('ok') else [], 'The test download did not finish.')
    if kind == 'doctor':
        g = get('gw')
        facts = {'gateway': g, 'has_route': bool(g), 'gw': None, 'wan': [], 'dns': {}, 'web': {}}
        gwp = get('gwp')
        if first_ipv4(g) and gwp and gwp != 'x':
            rcv, _, avg = gwp.partition('|')
            facts['gw'] = {'ip': first_ipv4(g), 'sent': 3, 'received': _int(rcv), 'avg': parse_ms(avg) if _int(rcv) else None}
        elif first_ipv4(g):
            facts['gw'] = {'ip': first_ipv4(g), 'sent': 3, 'received': 0, 'avg': None}
        for k, v in lines:
            if k == 'p':
                ip, rcv, avg = (v.split('|') + ['', ''])[:3]
                n = _int(rcv)
                facts['wan'].append({'ip': ip, 'sent': 3, 'received': n, 'avg': round(parse_ms(avg), 1) if n and parse_ms(avg) is not None else None,
                                     'loss': round((3 - min(n, 3)) * 100 / 3)})
        d = get('dns')
        facts['dns'] = {'ok': bool(d) and d != 'x', 'ip': '' if d == 'x' else d, 'servers': _servers(get('servers')) or _servers(get('dyn'))}
        w = get('web')
        st, _, du = w.partition('|')
        ms, text = _whole_seconds(du)
        facts['web'] = {'ok': st == 'finished', 'ms': ms, 'text': text}
        return diagnose(facts)
    raise ValueError('Unknown test.')


def _link_reply(v):
    """One Link ping reply → ms, 'ok' (answered, time unknown) or None (lost)."""
    if '|' in v:
        rc, _, av = v.partition('|')
        if _int(rc) <= 0:
            return None
        ms = parse_ms(av)
        return ms if ms is not None else 'ok'
    return None if v in ('', '-') else parse_ms(v)


def _whole_seconds(du):
    """RouterOS fetch reports whole seconds ('00:00:01'): (ms for sorting, readable text)."""
    sec = parse_seconds(du)
    if sec is None:
        return None, ''
    return round(sec * 1000), ('under 1 s' if sec < 1 else f'about {round(sec)} s')


def _int(v):
    try:
        return int(str(v).strip())
    except ValueError:
        return 0


def receive_link(cmd, body):
    """Store the router's answer on its NetTest."""
    from .models_nettools import NetTest
    t = NetTest.objects.filter(pk=cmd.params.get('test_id'), router=cmd.router).first()
    if not t:
        return None
    res = parse_link(t.kind, t.params, body)
    failed = bool(res.pop('failed', False))
    t.result, t.status, t.finished_at = res, ('failed' if failed else 'done'), timezone.now()
    t.save(update_fields=['result', 'status', 'finished_at'])
    return t


def expire_waiting(test):
    """A Link test whose command failed, expired or never came back is marked failed with a reason."""
    if test.status != 'waiting':
        return test
    from .models import AgentCommand
    cmd = AgentCommand.objects.filter(pk=test.command_id).first() if test.command_id else None
    reason = ''
    if not cmd:
        reason = 'The command to the router was lost.'
    elif cmd.status in ('failed', 'expired', 'cancelled'):
        reason = f'The router did not run the test ({cmd.get_status_display().lower()}).'
    elif cmd.status == 'done' and cmd.done_at and (timezone.now() - cmd.done_at).total_seconds() > 60:
        reason = 'The router ran the test but its answer never reached TapTap (it may not reach TapTap over HTTPS).'
    elif (timezone.now() - test.created_at).total_seconds() > 360:
        reason = 'The router did not answer within 6 minutes — is TapTap Link online?'
    if reason:
        test.status, test.result, test.finished_at = 'failed', {'error': reason}, timezone.now()
        test.save(update_fields=['status', 'result', 'finished_at'])
    return test
