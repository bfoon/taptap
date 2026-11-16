"""DHCP lease time — how long a phone keeps its address — for every network of a router, in one click.

Why it matters on a hotspot:

* **Too short** (under ~30 minutes): a phone asleep in a pocket loses its address and RouterOS signs it out
  ("lost dhcp lease" in *Why people drop*); the customer types the code again.
* **Too long** (a day or more) on a busy site: addresses of people who left stay taken, the pool fills up and new
  customers get no address at all — they see "connected, no internet".

So the right value follows from how full the address pool is, and TapTap says which way to go.

Reading uses the last sync (instant); "Read from the router" and every change go to the router itself and are read
back — Direct API / TapTap Tunnel at once, TapTap Link at its next check-in.
"""
from __future__ import annotations

import ipaddress
import re

from django.core.cache import cache
from django.utils import timezone

NAME_RE = re.compile(r'^[\w .@:+/<>-]{1,64}$')
PRESETS = [(10, '10 min'), (30, '30 min'), (60, '1 hour'), (240, '4 hours'), (720, '12 hours'), (1440, '1 day'), (4320, '3 days')]
MIN_MINUTES, MAX_MINUTES = 2, 43200          # 2 minutes … 30 days
HOTSPOT_GOOD = (60, 720)                       # 1 h … 12 h suits a hotspot whose pool has room


def _s(v):
    return str(v if v is not None else '').strip()


def minutes_of(value):
    from .router_time import secs
    return round(secs(value) / 60) if secs(value) else 0


def human(minutes):
    minutes = int(minutes or 0)
    if not minutes:
        return '—'
    d, rest = divmod(minutes, 1440)
    h, m = divmod(rest, 60)
    parts = [f'{d} day{"s" if d != 1 else ""}' if d else '', f'{h} hour{"s" if h != 1 else ""}' if h else '', f'{m} min' if m else '']
    return ' '.join(x for x in parts if x)


def clean(data):
    """{'minutes': 60, 'servers': ['dhcp1'] or []} (empty list = every DHCP server of the router)."""
    from .durations import routeros
    try:
        minutes = int(data.get('minutes'))
    except (TypeError, ValueError):
        raise ValueError('Choose a lease time.')
    if not MIN_MINUTES <= minutes <= MAX_MINUTES:
        raise ValueError('The lease time must be between 2 minutes and 30 days.')
    servers = data.get('servers') or []
    if not isinstance(servers, list) or len(servers) > 20:
        raise ValueError('Too many DHCP servers.')
    servers = [_s(x) for x in servers if _s(x)]
    for name in servers:
        if not NAME_RE.match(name):
            raise ValueError(f'“{name}” is not a DHCP server name TapTap can send to the router.')
    return {'minutes': minutes, 'lease': routeros(minutes), 'servers': servers, 'target': f'lease {human(minutes)}'}


# ─────────────────────────────── what the router has ───────────────────────────────

def _pool_size(ranges):
    n = 0
    for part in re.split(r'[,;\s]+', _s(ranges)):
        if not part:
            continue
        a, _, b = part.partition('-')
        try:
            if '/' in a:
                n += max(0, ipaddress.ip_network(a, strict=False).num_addresses - 2)
            else:
                lo = int(ipaddress.ip_address(a))
                hi = int(ipaddress.ip_address(b)) if b else lo
                n += max(0, hi - lo + 1)
        except ValueError:
            continue
    return n


def shape(servers, pools=(), hotspots=(), leases=None):
    """Rows from the router (any channel) → cards for the page, each with advice."""
    pool_size = {_s(p.get('name')): _pool_size(p.get('ranges')) for p in pools or []}
    hs = {_s(h.get('interface')) for h in hotspots or [] if _s(h.get('disabled')).lower() not in ('true', 'yes')}
    out = []
    for s in servers:
        name = _s(s.get('name'))
        if not name:
            continue
        minutes = minutes_of(s.get('lease-time') or s.get('lease_time'))
        used = s.get('leases')
        if used is None and leases is not None:
            used = sum(1 for l in leases if _s(l.get('server')) == name and _s(l.get('status')).lower() in ('bound', ''))
        size = pool_size.get(_s(s.get('address-pool')), 0) or int(s.get('pool') or 0)
        row = {'name': name, 'interface': _s(s.get('interface')), 'lease': _s(s.get('lease-time') or s.get('lease_time')), 'minutes': minutes,
               'human': human(minutes), 'disabled': _s(s.get('disabled')).lower() in ('true', 'yes'),
               'hotspot': _s(s.get('interface')) in hs, 'leases': int(used) if used is not None else None, 'pool': size,
               'full': round(int(used) * 100 / size) if used is not None and size else None}
        row['advice'] = advise(row)
        out.append(row)
    return out


def advise(row):
    """One plain sentence + the lease TapTap would pick: {'level', 'text', 'suggest'}."""
    m, full = row['minutes'], row.get('full')
    if not m:
        return {'level': 'unknown', 'text': 'TapTap could not read this lease time.', 'suggest': 60}
    if full is not None and full >= 85:
        if m > 30:
            return {'level': 'bad', 'suggest': 30,
                    'text': f'{full}% of the addresses are taken. A shorter lease (30 min) frees the addresses of people who have left, '
                            'before new customers are refused.'}
        return {'level': 'warn', 'suggest': m,
                'text': f'{full}% of the addresses are taken even with a short lease — this network needs a bigger address pool.'}
    if row.get('hotspot') and m < 30:
        return {'level': 'bad', 'suggest': 60,
                'text': 'Very short for a hotspot: a phone that sleeps loses its address and is signed out. 1 hour stops those sign-outs.'}
    if full is not None and full >= 60 and m > 240:
        return {'level': 'warn', 'suggest': 60,
                'text': f'{full}% of the addresses are taken and each one is held for {human(m)} after its owner leaves. 1 hour keeps room free.'}
    if row.get('hotspot') and m > 1440:
        return {'level': 'warn', 'suggest': 720,
                'text': 'Long for a hotspot: every visitor keeps an address for days. 12 hours is plenty.'}
    lo, hi = HOTSPOT_GOOD
    if row.get('hotspot') and lo <= m <= hi:
        return {'level': 'ok', 'suggest': m, 'text': 'A good value for a hotspot.'}
    return {'level': 'ok', 'suggest': m, 'text': 'Fine for this network.'}


def stored(router):
    """Instant view from the last sync (or the last live read, if newer)."""
    from .linkops import snapshot_rows
    live = cache.get(f'tt:dhcp:{router.pk}')
    rows, _l, at = snapshot_rows(router, '/ip/dhcp-server')
    if live and (not at or live.get('at', '') >= at.isoformat()):
        return {'servers': live['servers'], 'as_of': live['at'], 'live': True}
    pools = snapshot_rows(router, '/ip/pool')[0] or []
    hotspots = snapshot_rows(router, '/ip/hotspot')[0] or []
    leases = snapshot_rows(router, '/ip/dhcp-server/lease')[0]
    return {'servers': shape(rows or [], pools, hotspots, leases), 'as_of': at.isoformat() if at else None, 'live': False}


def remember(router, servers):
    cache.set(f'tt:dhcp:{router.pk}', {'at': timezone.now().isoformat(), 'servers': servers}, 6 * 3600)


# ─────────────────────────────── Direct API / TapTap Tunnel ───────────────────────────────

def _api_read(svc):
    servers = svc.safe_get('/ip/dhcp-server')
    return shape(servers, svc.safe_get('/ip/pool'), svc.safe_get('/ip/hotspot'), svc.safe_get('/ip/dhcp-server/lease'))


def api_read(svc, p):
    return {'servers': _api_read(svc)}


def api_set(svc, p):
    res = svc.resource('/ip/dhcp-server')
    changed, missing = [], []
    rows = res.get()
    want = set(p.get('servers') or [])
    for name in want - {_s(r.get('name')) for r in rows}:
        missing.append(name)
    for r in rows:
        if want and _s(r.get('name')) not in want:
            continue
        res.set(id=r['id'], lease_time=p['lease'])
        changed.append(_s(r.get('name')))
    return {'servers': _api_read(svc), 'changed': changed, 'missing': missing, 'minutes': p['minutes']}


# ─────────────────────────────── TapTap Link ───────────────────────────────

READ = (':foreach s in=[/ip dhcp-server find] do={ :do { :local n [/ip dhcp-server get $s name]; :local lc ""; :local hs 0; '
        ':do { :set out ($out . "pr=" . $n . "|" . [:tostr [/ip pool get [find name=[/ip dhcp-server get $s address-pool]] ranges]] . "\\n") } on-error={}; '
        ':do { :set lc [:len [/ip dhcp-server lease find where server=$n status=bound]] } on-error={}; '
        ':do { :set hs [:len [/ip hotspot find where interface=[/ip dhcp-server get $s interface] disabled=no]] } on-error={}; '
        ':set out ($out . "s=" . $n . "|" . [/ip dhcp-server get $s interface] . "|" . [/ip dhcp-server get $s lease-time] . "|" . '
        '[/ip dhcp-server get $s disabled] . "|" . $lc . "|" . $hs . "\\n") } on-error={} }; ')


def link_body(kind, p, rs):
    if kind == 'dhcp':
        return READ
    names = p.get('servers') or []
    if names:
        sets = ''.join(f':do {{ :if ([:len [/ip dhcp-server find name={rs(n)}]] = 0) do={{ :set out ($out . "miss=" . {rs(n)} . "\\n") }} else={{ '
                       f'/ip dhcp-server set [find name={rs(n)}] lease-time={p["lease"]}; :set out ($out . "ch=" . {rs(n)} . "\\n") }} }} '
                       f'on-error={{ :set out ($out . "err=" . {rs(n)} . "\\n") }}; ' for n in names)
    else:
        sets = (f':foreach s in=[/ip dhcp-server find] do={{ :do {{ /ip dhcp-server set $s lease-time={p["lease"]}; '
                ':set out ($out . "ch=" . [/ip dhcp-server get $s name] . "\\n") } on-error={ :set out ($out . "err=" . [/ip dhcp-server get $s name] . "\\n") } }; ')
    return sets + READ


def parse_link(kind, p, lines):
    ranges = {}
    servers = []
    for k, v in lines:
        if k == 'pr':
            n, _, rg = v.partition('|')
            ranges[n] = _pool_size(rg.replace(';', ','))
    for k, v in lines:
        if k == 's':
            a = (v.split('|') + [''] * 6)[:6]
            servers.append({'name': a[0], 'interface': a[1], 'lease-time': a[2], 'disabled': a[3],
                            'leases': int(a[4]) if a[4].isdigit() else None, 'pool': ranges.get(a[0], 0), '_hs': a[5] not in ('', '0')})
    shaped = shape(servers, hotspots=[{'interface': s['interface']} for s in servers if s['_hs']])
    out = {'servers': shaped}
    if kind == 'dhcpset':
        out.update(changed=[v for k, v in lines if k == 'ch'], missing=[v for k, v in lines if k == 'miss'],
                   refused=[v for k, v in lines if k == 'err'], minutes=p.get('minutes'))
    return out


def finish(router, kind, p, res):
    """Remember the fresh read and say what happened."""
    servers = res.get('servers') or []
    if servers:
        remember(router, servers)
    if kind == 'dhcp':
        res['title'] = f'{len(servers)} DHCP server{"s" if len(servers) != 1 else ""} on {router.name}' if servers else 'No DHCP server on this router'
        res['note'] = '' if servers else 'Addresses on this network are handed out by another device (the ISP modem or another router).'
        return res
    changed, text = res.get('changed') or [], human(p.get('minutes'))
    wrong = [s['name'] for s in servers if s['name'] in changed and s['minutes'] != p.get('minutes')]
    if changed and not wrong and not res.get('refused'):
        res['title'] = f'Lease time is now {text} on ' + (changed[0] if len(changed) == 1 else f'{len(changed)} networks')
        res['note'] = ('Read back from the router. Devices take the new time when they next renew their address — '
                       'within half of the old lease time; nobody is disconnected.')
    else:
        res['failed_step'] = True
        res['title'] = 'The lease time was not changed everywhere'
        bits = ([f'not found: {", ".join(res["missing"])}'] if res.get('missing') else []) + \
               ([f'refused by the router: {", ".join(res["refused"])}'] if res.get('refused') else []) + \
               ([f'still the old value: {", ".join(wrong)}'] if wrong else [])
        res['note'] = '; '.join(bits) or 'The router has no DHCP server to change.'
    return res
