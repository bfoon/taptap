"""Connection lab — why is this voucher / device slow, measured from the MikroTik it is connected to.

A customer's phone cannot be asked to run a speed test. The router can answer the real questions instead:

1. **Device check** (``client``): does the phone answer the router, how fast and how steadily (ping, reply by
   reply), which port / Wi-Fi it is on, its Wi-Fi signal when the MikroTik's own radio serves it, its DHCP
   lease and its hotspot session.
2. **Speed check** (``cspeed``): every speed limit that applies to its IP (the hotspot's own queue, fair usage,
   speed rules) with the speed it is doing *now* and the packets the limiter has **dropped**; which internet
   line its connections use; how the router's own internet answers at that moment; the router's CPU; and its
   last sign-outs from the router's log.
3. ``verdict`` puts both next to what TapTap already knows (plan speed, fair usage, live speed) and names the
   slow part in plain words: the phone's Wi-Fi, the plan's speed, a slowdown, or the internet line.

``slowscan`` reads the limiter drops of everyone at once (Traffic → Slow vouchers). The internet-line kinds live in
core/line_steer.py and the DHCP kinds in core/dhcp_lease.py; they travel the same way as every Network tools test
(core/nettools.py): Direct API / TapTap Tunnel now, TapTap Link at the next check-in.
"""
from __future__ import annotations

import ipaddress
import re

from . import line_steer as ls
from . import nettools as nt

LAB_KINDS = ('client', 'cspeed', 'slowscan', 'lane', 'lanepin', 'laneoff', 'dhcp', 'dhcpset')
LANE_KINDS = ('lane', 'lanepin', 'laneoff')
DHCP_KINDS = ('dhcp', 'dhcpset')
CODE_RE = re.compile(r'^[\w.@:+/<>-]{1,64}$')
LOG_SAFE = re.compile(r'^[A-Za-z0-9_-]{2,64}$')       # codes that can sit in a RouterOS regex as they are
PINGS = 6
SCAN_LINES = 110
WIFI_TABLES = (('/interface wireless registration-table', '/interface/wireless/registration-table', 'signal-strength', 'tx-ccq'),
               ('/interface wifi registration-table', '/interface/wifi/registration-table', 'signal', ''),
               ('/caps-man registration-table', '/caps-man/registration-table', 'rx-signal', ''))


def _s(v):
    return str(v if v is not None else '').strip()


# ─────────────────────────────── parameters (always built by TapTap, never free text) ───────────────────────────────

def device_params(ip, mac='', code=''):
    try:
        addr = ipaddress.ip_address(_s(ip))
    except ValueError:
        raise ValueError('This device has no IP address right now — it must be online to be checked.')
    if addr.version != 4 or not (addr.is_private or addr in ipaddress.ip_network('100.64.0.0/10')) or addr.is_loopback:
        raise ValueError('Only devices on your own network can be checked.')
    code = _s(code)
    out = {'ip': str(addr), 'code': code if CODE_RE.match(code) else '', 'target': code or str(addr)}
    if ls.clean_mac(mac):
        out['mac'] = ls.clean_mac(mac)
    return out


def clean(kind, data, router=None):
    """Validated parameters for one lab test. ``data`` comes from TapTap's own views."""
    if kind not in LAB_KINDS:
        raise ValueError('Unknown test.')
    if kind == 'slowscan':
        return {'target': 'speed limits'}
    if kind == 'dhcp':
        return {'target': 'DHCP servers'}
    if kind == 'dhcpset':
        from .dhcp_lease import clean as dhcp_clean
        return dhcp_clean(data)
    p = device_params(data.get('ip'), data.get('mac'), data.get('code'))
    if kind in LANE_KINDS or kind == 'cspeed':
        info = ls.lanes(router) if router is not None else {'lanes': []}
        p['marks'] = [l['mark'] for l in info['lanes'] if l.get('observable')][:6]
        p['key'] = ls.pin_key(p.get('mac', ''), p['ip'])
    if kind == 'lanepin':
        lane = ls.lane_by_mark(info, _s(data.get('mark')))
        if not lane:
            raise ValueError(info.get('why') or 'That internet line is not part of this router\'s load sharing.')
        p['mark'] = lane['mark']
        p['fields'] = ls.pin_fields(lane, p['key'], p.get('mac', ''), p['ip'], p['code'])
        p['target'] = f'{p["code"] or p["ip"]} → {lane["label"]}'
    return p


VALUE_RE = re.compile(r'^[\w .:/!,@+-]{1,120}$')
LEASE_RE = re.compile(r'^(\d{1,5}[dhm]){1,3}$')


def validate(kind, p):
    """Last check before a lab test is queued for a router (core/agent.queue): only values a script may carry."""
    if kind not in LAB_KINDS:
        raise ValueError('Unknown test.')
    if kind in ('slowscan', 'dhcp'):
        return
    if kind == 'dhcpset':
        from .dhcp_lease import NAME_RE
        if not LEASE_RE.match(_s(p.get('lease'))) or not all(NAME_RE.match(_s(n)) for n in p.get('servers') or []):
            raise ValueError('Invalid lease time.')
        return
    if device_params(p.get('ip'), p.get('mac', ''), p.get('code', ''))['ip'] != p.get('ip'):
        raise ValueError('Invalid device.')
    if p.get('mac') and not ls.MAC_RE.match(_s(p['mac'])):
        raise ValueError('Invalid MAC address.')
    if p.get('code') and not CODE_RE.match(_s(p['code'])):
        raise ValueError('Invalid code.')
    if not all(ls.MARK_RE.match(_s(m)) for m in p.get('marks') or []):
        raise ValueError('Invalid line.')
    if 'key' in p and p['key'] != ls.pin_key(p.get('mac', ''), p['ip']):
        raise ValueError('Invalid device key.')
    if kind == 'lanepin':
        f = p.get('fields') or {}
        allowed = set(ls.COPY) | {'src-mac-address', 'src-address', 'comment'}
        if not f or set(f) - allowed or not all(VALUE_RE.match(_s(v)) for v in f.values()) or \
                not _s(f.get('comment')).startswith(f'{ls.PIN_TAG} {p["key"]} '):
            raise ValueError('Invalid switch rule.')


# ─────────────────────────────── small parsers ───────────────────────────────

_MULT = {'': 1, 'k': 1000, 'm': 1000_000, 'g': 1000_000_000}


def _num(v):
    """'2M', '512k', '1024bps', '2000000', '1.5M' → number (0 when unreadable)."""
    m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*([kmg]?)(?:bps|b)?', _s(v).lower())
    return int(float(m.group(1)) * _MULT[m.group(2)]) if m else 0


def pair(v):
    """RouterOS 'upload/download' pair → (up, down)."""
    a, _, b = _s(v).partition('/')
    return _num(a), _num(b)


def parse_rate(text):
    """A plan's speed ('2M/5M', '512k/1M 1M/2M …', '5M') → (up bps, down bps); (0, 0) = no limit."""
    first = _s(text).split(' ')[0]
    if not first:
        return 0, 0
    if '/' not in first:
        n = _num(first)
        return n, n
    return pair(first)


def _queue(name, target, mx, rate, dropped, packets, queued=''):
    mu, md = pair(mx)
    ru, rd = pair(rate)
    du, dd = pair(dropped)
    pu, pd = pair(packets)
    qu, qd = pair(queued)
    return {'name': _s(name)[:80], 'target': _s(target).split(',')[0][:40], 'max_up': mu, 'max_down': md, 'rate_up': ru, 'rate_down': rd,
            'drop_up': du, 'drop_down': dd, 'pk_up': pu, 'pk_down': pd, 'queued': qu + qd,
            'drop_pct': round(dd * 100 / (pd + dd), 1) if pd + dd else 0.0,
            'kind': 'fup' if _s(name).startswith('TTFUP') else 'rule' if _s(name).startswith('TTSPD') else 'plan'}


def _target_ip(target):
    return _s(target).split(',')[0].split('/')[0]


def _wifi(sig, tx, rx, ccq, iface):
    m = re.match(r'-?\d+', _s(sig))
    return {'signal': int(m.group(0)) if m else None, 'tx': _s(tx)[:30], 'rx': _s(rx)[:30],
            'ccq': int(_s(ccq)) if _s(ccq).isdigit() else None, 'interface': _s(iface)[:40]}


# ─────────────────────────────── Direct API / TapTap Tunnel ───────────────────────────────

def _first(svc, path, **where):
    try:
        rows = svc.safe_get(path, **where)
    except Exception:
        rows = []
    return rows[0] if rows else None


def run_client(svc, p):
    ip, mac = p['ip'], p.get('mac', '')
    try:
        times, _ = nt._api_ping(svc, ip, PINGS, interval='250ms')
    except Exception:
        times = [None] * PINGS
    res = {'ping': nt.ping_stats(times), 'arp': None, 'port': '', 'lease': None, 'host': None, 'active': None, 'wifi': None}
    a = _first(svc, '/ip/arp', address=ip)
    if a:
        res['arp'] = {'mac': _s(a.get('mac-address')).upper(), 'interface': _s(a.get('interface'))}
        mac = mac or res['arp']['mac']
    if mac:
        h = _first(svc, '/interface/bridge/host', **{'mac-address': mac})
        if h:
            res['port'] = _s(h.get('on-interface') or h.get('interface'))
        l = _first(svc, '/ip/dhcp-server/lease', **{'mac-address': mac})
        if l:
            res['lease'] = _lease(l.get('status'), l.get('expires-after'), l.get('server'), l.get('host-name'), l.get('dynamic'))
        hh = _first(svc, '/ip/hotspot/host', **{'mac-address': mac})
        if hh:
            res['host'] = {'authorized': _s(hh.get('authorized')).lower() == 'true', 'bypassed': _s(hh.get('bypassed')).lower() == 'true',
                           'idle': _s(hh.get('idle-time'))}
        for _cli, path, sig, ccq in WIFI_TABLES:
            w = _first(svc, path, **{'mac-address': mac})
            if w:
                res['wifi'] = _wifi(w.get(sig), w.get('tx-rate'), w.get('rx-rate'), w.get(ccq) if ccq else '', w.get('interface'))
                break
    s = _first(svc, '/ip/hotspot/active', address=ip)
    if s:
        res['active'] = {'user': _s(s.get('user')), 'uptime': _s(s.get('uptime')), 'idle': _s(s.get('idle-time')),
                         'left': _s(s.get('session-time-left'))}
    return res


def _lease(status, expires, server, host, dynamic):
    from .router_time import secs
    return {'status': _s(status), 'expires': _s(expires), 'expires_s': secs(expires), 'server': _s(server), 'host': _s(host)[:60],
            'dynamic': _s(dynamic).lower() in ('true', 'yes')}


def _queues(svc):
    res = svc.resource('/queue/simple')
    rows = res.get() or []
    if rows and 'dropped' not in rows[0]:
        try:
            rows = res.call('print', {'stats': ''}) or rows
        except Exception:
            pass
    return rows


def run_cspeed(svc, p):
    ip = p['ip']
    res = {'queues': [], 'lane': None, 'internet': None, 'cpu': None, 'drops': None}
    try:
        res['queues'] = [_queue(q.get('name'), q.get('target'), q.get('max-limit'), q.get('rate'), q.get('dropped'), q.get('packets'),
                                q.get('queued-packets')) for q in _queues(svc)
                         if _target_ip(q.get('target')) == ip and _s(q.get('disabled')).lower() not in ('true', 'yes')]
    except Exception:
        res['queue_error'] = True
    if p.get('marks'):
        try:
            res['lane'] = ls.api_look(svc, p)
        except Exception:
            pass
    try:
        t, _ = nt._api_ping(svc, nt.CHECK_IPS[0], 3, interval='300ms')
        res['internet'] = nt._short(t)
    except Exception:
        res['internet'] = {'sent': 3, 'received': 0, 'avg': None, 'loss': 100}
    r = _first(svc, '/system/resource')
    if r and _s(r.get('cpu-load')).isdigit():
        res['cpu'] = int(r['cpu-load'])
    if p.get('code'):
        try:
            code = p['code'].lower()
            entries = [(row.get('time', ''), _s(row.get('message'))) for row in svc.resource('/log').get()
                       if 'logged out' in _s(row.get('message')) and code in _s(row.get('message')).lower()]
            res['drops'] = nt.drops_result(entries[-8:], total=len(entries))
        except Exception:
            pass
    return res


def run_slowscan(svc, p):
    rows = _queues(svc)
    out = [_queue(q.get('name'), q.get('target'), q.get('max-limit'), q.get('rate'), q.get('dropped'), q.get('packets'), q.get('queued-packets'))
           for q in rows if _s(q.get('disabled')).lower() not in ('true', 'yes')]
    return scan_result([q for q in out if q['drop_up'] or q['drop_down'] or q['queued']], len(rows), True)


def scan_result(queues, total, readable):
    queues.sort(key=lambda q: -(q['drop_down'] + q['drop_up']))
    n = len(queues)
    return {'queues': queues[:SCAN_LINES], 'total': total, 'readable': readable,
            'title': (f'{n} speed limit{"s are" if n != 1 else " is"} dropping packets' if n else
                      'No speed limit is dropping packets' if readable else 'This router did not report limiter drops'),
            'note': ('A limiter drops packets when the customer asks for more than the limit gives: these customers feel “slow”.' if n else
                     f'{total} speed limits were read; none has dropped a packet.' if readable else
                     'The speed limits could not be read on this RouterOS version.')}


def run_api(test, svc):
    k, p = test.kind, test.params
    if k == 'client':
        return run_client(svc, p)
    if k == 'cspeed':
        res = run_cspeed(svc, p)
        if res.get('lane') is not None:
            res['lane'] = ls.result(test.router, 'lane', p, res['lane'])
        return res
    if k == 'slowscan':
        return run_slowscan(svc, p)
    if k in LANE_KINDS:
        raw = {'lane': ls.api_look, 'lanepin': ls.api_pin, 'laneoff': ls.api_unpin}[k](svc, p)
        return ls.result(test.router, k, p, raw)
    from . import dhcp_lease as dl
    return dl.finish(test.router, k, p, dl.api_read(svc, p) if k == 'dhcp' else dl.api_set(svc, p))


# ─────────────────────────────── TapTap Link: router-side scripts ───────────────────────────────

def _get(menu, var, *fields):
    return ' . "|" . '.join(f'[{menu} get {var} {f}]' for f in fields)


def _client_body(p, rs):
    ip, mac = p['ip'], p.get('mac', '')
    body = (f':do {{ :local a ([/ip arp find where address={ip}]->0); '
            f':set out ($out . "arp=" . {_get("/ip arp", "$a", "mac-address", "interface")} . "\\n") }} on-error={{}}; '
            f':for i from=1 to={PINGS} do={{ ' + nt._ping_snippet(ip, 1, 0, 'r=') + ':delay 250ms }; '
            f':do {{ :local a ([/ip hotspot active find where address={ip}]->0); '
            f':set out ($out . "act=" . {_get("/ip hotspot active", "$a", "user", "uptime", "idle-time", "session-time-left")} . "\\n") }} on-error={{}}; ')
    if not mac:
        return body
    body += (f':do {{ :local h ([/interface bridge host find where mac-address={mac}]->0); '
             ':set out ($out . "port=" . [/interface bridge host get $h on-interface] . "\\n") } on-error={}; '
             f':do {{ :local l ([/ip dhcp-server lease find where mac-address={mac}]->0); '
             f':set out ($out . "lease=" . {_get("/ip dhcp-server lease", "$l", "status", "expires-after", "server", "dynamic", "host-name")} . "\\n") }} on-error={{}}; '
             f':do {{ :local h ([/ip hotspot host find where mac-address={mac}]->0); '
             f':set out ($out . "host=" . {_get("/ip hotspot host", "$h", "authorized", "bypassed", "idle-time")} . "\\n") }} on-error={{}}; ')
    # Wi-Fi menus differ by package; :parse keeps a missing menu from stopping the whole script (core/pathtrace.py).
    for menu, _path, sig, ccq in WIFI_TABLES:
        g = lambda f: f'[{menu} get $r {f}]'
        inner = (f':local i [{menu} find mac-address={mac}]; :if ([:len $i] > 0) do={{ :local r ($i->0); '
                 f':return ({g(sig)} . "|" . {g("tx-rate")} . "|" . {g("rx-rate")} . "|" . ' + (g(ccq) if ccq else '""') +
                 f' . "|" . {g("interface")}) }}; :return ""')
        body += (f':do {{ :local f [:parse {rs(inner)}]; :local v [$f]; '
                 ':if ([:len $v] > 0) do={ :set out ($out . "wifi=" . $v . "\\n") } } on-error={}; ')
    return body


_Q = '/queue simple'


def _queue_line(var, prefix='q='):
    return (f':set out ($out . "{prefix}" . ' + _get(_Q, var, 'name') + ' . "|" . $t . "|" . ' +
            _get(_Q, var, 'max-limit', 'rate', 'dropped', 'packets') + ' . "\\n")')


def _cspeed_body(p, rs):
    ip = p['ip']
    body = (f':foreach q in=[{_Q} find] do={{ :do {{ :local t [:tostr [{_Q} get $q target]]; '
            f':if ($t = "{ip}/32" || $t = "{ip}") do={{ ' + _queue_line('$q') + ' } } on-error={} }; ')
    if p.get('marks'):
        body += ls.link_body('lane', p)
    body += nt._ping_snippet(nt.CHECK_IPS[0], 3, 0, 'i=')
    body += ':do { :set out ($out . "cpu=" . [/system resource get cpu-load] . "\\n") } on-error={}; '
    if LOG_SAFE.match(p.get('code') or ''):
        body += (':do { :local rows [/log print as-value where message~"logged out" and message~' + rs(p['code']) + ']; '
                 ':local n [:len $rows]; :local s 0; :if ($n > 8) do={ :set s ($n - 8) }; :set out ($out . "t=" . $n . "\\n"); '
                 ':if ($n > 0) do={ :for i from=$s to=($n - 1) do={ :do { :local e ($rows->$i); '
                 ':set out ($out . "l=" . ($e->"time") . "|" . ($e->"message") . "\\n") } on-error={} } } } on-error={}; ')
    return body


def _slowscan_body():
    return (f':local n 0; :local tq 0; :do {{ :set tq [:len [{_Q} find]] }} on-error={{}}; :set out ($out . "qn=" . $tq . "\\n"); '
            f':do {{ :set out ($out . "qp=" . [:tostr [{_Q} get ([{_Q} find]->0) dropped]] . "\\n") }} on-error={{ :set out ($out . "qp=x\\n") }}; '
            f':foreach q in=[{_Q} find where disabled=no] do={{ :if ($n < {SCAN_LINES}) do={{ :do {{ '
            f':local d [:tostr [{_Q} get $q dropped]]; :if ($d != "0/0") do={{ :local t [:tostr [{_Q} get $q target]]; '
            + _queue_line('$q') + '; :set n ($n + 1) } } on-error={} } }; ')


def link_body(kind, p, rs):
    """RouterOS that appends ``key=value`` lines to $out for one lab kind (core/nettools.link_body sends it)."""
    if kind == 'client':
        return _client_body(p, rs)
    if kind == 'cspeed':
        return _cspeed_body(p, rs)
    if kind == 'slowscan':
        return _slowscan_body()
    if kind in LANE_KINDS:
        return ls.link_body(kind, p)
    from .dhcp_lease import link_body as dhcp_body
    return dhcp_body(kind, p, rs)


def _split(v, n):
    return (v.split('|') + [''] * n)[:n]


def parse_link(test_router, kind, p, lines):
    get = lambda k: next((v for kk, v in lines if kk == k), '')
    if kind == 'client':
        times = [nt._link_reply(v) for k, v in lines if k == 'r']
        res = {'ping': nt.ping_stats(times or [None] * PINGS), 'arp': None, 'port': get('port'), 'lease': None, 'host': None, 'active': None, 'wifi': None}
        if get('arp'):
            mac, iface = _split(get('arp'), 2)
            res['arp'] = {'mac': mac.upper(), 'interface': iface}
        if get('lease'):
            st, ex, srv, dyn, host = _split(get('lease'), 5)
            res['lease'] = _lease(st, ex, srv, host, dyn)
        if get('host'):
            au, by, idle = _split(get('host'), 3)
            res['host'] = {'authorized': au == 'true', 'bypassed': by == 'true', 'idle': idle}
        if get('act'):
            user, up, idle, left = _split(get('act'), 4)
            res['active'] = {'user': user, 'uptime': up, 'idle': idle, 'left': left}
        if get('wifi'):
            res['wifi'] = _wifi(*_split(get('wifi'), 5))
        return res
    if kind in ('cspeed', 'slowscan'):
        queues = [_queue(*_split(v, 6)) for k, v in lines if k == 'q']
        if kind == 'slowscan':
            return scan_result(queues, nt._int(get('qn')), get('qp') != 'x')
        res = {'queues': queues, 'lane': None, 'internet': None, 'cpu': int(get('cpu')) if get('cpu').isdigit() else None, 'drops': None}
        if p.get('marks'):
            res['lane'] = ls.result(test_router, 'lane', p, ls.parse_link(lines))
        if get('i'):
            rc, _, av = get('i').partition('|')
            n = min(3, nt._int(rc))
            ms = nt.parse_ms(av)
            res['internet'] = {'sent': 3, 'received': n, 'avg': round(ms, 1) if n and ms is not None else None, 'loss': round((3 - n) * 100 / 3)}
        if any(k == 't' for k, _ in lines):
            entries = [tuple(v.partition('|')[::2]) for k, v in lines if k == 'l']
            res['drops'] = nt.drops_result(entries, total=nt._int(get('t')))
        return res
    if kind in LANE_KINDS:
        return ls.result(test_router, kind, p, ls.parse_link(lines))
    from . import dhcp_lease as dl
    return dl.finish(test_router, kind, p, dl.parse_link(kind, p, lines))


def summary(test):
    r = test.result or {}
    if test.kind == 'client':
        pg = r.get('ping') or {}
        return f'{pg.get("received", 0)}/{pg.get("sent", 0)} replies' + (f' · {pg["avg"]} ms' if pg.get('avg') is not None else '')
    if test.kind == 'cspeed':
        q = binding(r.get('queues') or [])
        return (f'{bps_text(q["rate_down"])} of {bps_text(q["max_down"])}' + (f' · {q["drop_pct"]}% dropped' if q['drop_pct'] else '')) if q else 'No speed limit'
    return r.get('title', '')


# ─────────────────────────────── the verdict ───────────────────────────────

def bps_text(v):
    from .traffic_report import human_bps
    return human_bps(v)


def binding(queues):
    """The limit that actually holds the device back: the lowest download limit among its queues."""
    limited = [q for q in queues if q.get('max_down')]
    return min(limited, key=lambda q: q['max_down']) if limited else None


def _f(level, key, title, text='', action=''):
    return {'level': level, 'key': key, 'title': title, 'text': text, 'action': action}


def verdict(ctx, dev=None, spd=None):
    """What TapTap knows (ctx) + the two router checks → stations along the way, findings worst first, one headline.

    ctx: {'online', 'down_bps', 'up_bps', 'limit_down', 'limit_up', 'limit_source', 'fup', 'rule', 'quiet_s', 'lane'}"""
    dev, spd = dev or {}, spd or {}
    out = []
    st = {k: {'state': 'idle', 'detail': ''} for k in ('device', 'link', 'limit', 'line', 'internet')}

    # 1 — the phone itself and the link to it
    pg = dev.get('ping')
    if pg:
        wifi = dev.get('wifi')
        where = (wifi or {}).get('interface') or dev.get('port') or (dev.get('arp') or {}).get('interface') or ''
        st['device']['detail'] = where
        if not pg.get('received'):
            st['device'].update(state='bad', detail='no reply')
            st['link']['state'] = 'bad'
            gone = not dev.get('arp')
            out.append(_f('bad', 'silent', 'The phone does not answer the router' if not gone else 'The phone is not on the network right now',
                          ('The router no longer sees its address at all: it left, lost Wi-Fi or is switched off.' if gone else
                           'The router still knows its address but gets no reply — the phone is asleep, out of Wi-Fi range, or it '
                           'blocks ping (some iPhones do while the screen is off).') +
                          (' Its hotspot session is still open, so its time keeps running.' if dev.get('active') else ''), 'fix' if gone else ''))
        else:
            q, note = pg.get('quality'), pg.get('quality_note', '')
            level = {'poor': 'bad', 'fair': 'warn'}.get(q, 'ok')
            st['device']['state'] = 'ok'
            st['link'].update(state=level, detail=(f'{pg["avg"]} ms' if pg.get('avg') is not None else 'answers') + (f' · {pg["loss"]}% lost' if pg.get('loss') else ''))
            if level != 'ok':
                out.append(_f(level, 'link', 'The link between the router and the phone is ' + ('bad' if level == 'bad' else 'unsteady'),
                              f'{pg["loss"]}% of the pings were lost' + (f', replies take {pg["avg"]} ms' if pg.get('avg') is not None else '') +
                              (f' and vary by {pg["jitter"]} ms' if pg.get('jitter') else '') + f'. {note} ' +
                              ('This is Wi-Fi: the phone is far from the access point, behind walls, or the channel is crowded.' if not dev.get('port', '').startswith('ether')
                               else 'On a cable this points at the cable, the switch or the access point behind this port.')))
        if wifi and wifi.get('signal') is not None:
            sig = wifi['signal']
            st['link']['detail'] = f'{sig} dBm' + (f' · {st["link"]["detail"]}' if st['link']['detail'] else '')
            if sig <= -80:
                st['link']['state'] = 'bad'
                out.append(_f('bad', 'signal', f'Very weak Wi-Fi signal ({sig} dBm)',
                              'At this level every speed is slow whatever the plan. The phone must come closer, or this spot needs an access point.'))
            elif sig <= -72:
                if st['link']['state'] == 'ok':
                    st['link']['state'] = 'warn'
                out.append(_f('warn', 'signal', f'Weak Wi-Fi signal ({sig} dBm)', 'Browsing works, video and calls will stutter. Closer to the access point it improves.'))
            if wifi.get('ccq') is not None and wifi['ccq'] < 60:
                out.append(_f('warn', 'ccq', f'The radio repeats a lot ({wifi["ccq"]}% quality)', 'Interference or too many phones on this access point.'))
    lease = dev.get('lease')
    if lease and lease.get('status') and lease['status'] not in ('bound',):
        out.append(_f('warn', 'lease', f'Its address lease is “{lease["status"]}”', 'The phone has not completed getting its address. Wi-Fi off and on again fixes it.', 'dhcp'))
    host = dev.get('host')
    if pg and pg.get('received') and host and not host.get('authorized') and not host.get('bypassed'):
        out.append(_f('bad', 'login', 'The phone is connected but not logged in', 'It is on the Wi-Fi and sees the login page — it has no internet until the code is entered.', 'fix'))

    # 2 — the speed limits
    if spd:
        b = binding(spd.get('queues') or [])
        down, up = ctx.get('down_bps') or 0, ctx.get('up_bps') or 0
        if b:
            down = max(down, b['rate_down'])
            pct = round(down * 100 / b['max_down']) if b['max_down'] else 0
            src = {'fup': 'fair usage', 'rule': 'a speed rule', 'plan': 'its plan'}[b['kind']]
            st['limit'].update(state='ok', detail=f'{bps_text(b["max_down"])} · {pct}% used')
            if b['kind'] in ('fup', 'rule'):
                st['limit']['state'] = 'warn'
                out.append(_f('warn', b['kind'], f'Slowed to {bps_text(b["max_down"])} by {src}',
                              ('It passed a data step of its fair usage policy.' if b['kind'] == 'fup' else 'A Traffic → Speed control rule applies to it.') +
                              f' It is doing {bps_text(down)} now.', 'lift' if b['kind'] == 'fup' else ''))
            if b['drop_pct'] >= 2 or (pct >= 85 and b['kind'] == 'plan'):
                st['limit']['state'] = 'warn' if st['limit']['state'] == 'ok' else st['limit']['state']
                out.append(_f('warn', 'atlimit', f'It is using all the speed it is allowed ({bps_text(down)} of {bps_text(b["max_down"])})',
                              (f'The limiter has dropped {b["drop_down"]:,} download packets ({b["drop_pct"]}%) — the customer asks for more than this limit gives. '
                               if b['drop_down'] else '') + 'The network is doing exactly what the limit says: a faster plan is the fix, not the router.'))
        elif 'queues' in spd:
            st['limit'].update(state='ok', detail='no limit')
        inet = spd.get('internet')
        if inet:
            if not inet.get('received'):
                st['internet'].update(state='warn', detail='no ping reply')
            else:
                bad = inet.get('loss', 0) >= 30 or (inet.get('avg') or 0) >= 300
                st['internet'].update(state='bad' if bad else 'ok', detail=f'{inet["avg"]} ms' if inet.get('avg') is not None else 'answers')
                if bad:
                    out.append(_f('bad', 'internet', 'The internet line itself is slow right now',
                                  f'From the router, the internet answers in {inet.get("avg")} ms with {inet.get("loss", 0)}% lost — everyone on this line feels it, '
                                  'not only this customer. Check Traffic for a heavy user, or the ISP.', 'switch'))
        if (spd.get('cpu') or 0) >= 90:
            out.append(_f('bad', 'cpu', f'The router is at {spd["cpu"]}% CPU', 'A router this busy slows everybody. Too many queues or firewall rules for this model.'))
        lane = spd.get('lane')
        if lane and lane.get('lanes'):
            st['line'].update(state='ok', detail=lane.get('label') or ('no traffic' if lane.get('state') == 'none' else ''))
        dr = spd.get('drops')
        if dr and dr.get('total'):
            top = dr['reasons'][0]
            lvl = 'ok' if top['key'] in ('user', 'uptime', 'traffic') else 'warn'
            out.append(_f(lvl if dr['total'] >= 2 or lvl == 'ok' else 'info', 'signouts',
                          f'Signed out {dr["total"]} time{"s" if dr["total"] != 1 else ""} in the router\'s log — mostly: {top["label"].lower()}',
                          top.get('hint', ''), 'dhcp' if top['key'] == 'dhcp' else ''))
    if ctx.get('lane') and st['line']['state'] == 'idle':
        st['line'].update(state='ok', detail=ctx['lane'].get('label', ''))

    quiet = ctx.get('quiet_s') or 0
    if quiet >= 600 and ctx.get('online') and not any(f['key'] in ('silent', 'login') for f in out):
        out.append(_f('info', 'idle', f'No traffic for {quiet // 60} minutes', 'It is signed in but sends nothing: screen off, or the customer walked away. Its time still runs.'))

    order = {'bad': 0, 'warn': 1, 'info': 2, 'ok': 3}
    out.sort(key=lambda f: order[f['level']])
    done = bool(dev) and bool(spd)
    if out and out[0]['level'] in ('bad', 'warn'):
        head = {'level': out[0]['level'], 'title': out[0]['title']}
    elif done:
        head = {'level': 'ok', 'title': 'Nothing between the router and this phone is slowing it down'}
        out.append(_f('ok', 'clean', 'The path is healthy',
                      'The phone answers quickly and steadily, no limit is being hit and the internet line answers. If the customer still '
                      'finds it slow, it is the site or app they use — or their phone.'))
    else:
        head = {'level': 'idle', 'title': 'Checking…' if (dev or spd) else 'Ready to check'}
    return {'head': head, 'findings': out, 'stations': st, 'done': done}
