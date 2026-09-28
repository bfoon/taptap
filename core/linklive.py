"""Live data for TapTap Link routers (behind NAT — TapTap never opens a connection to them).

Heartbeat v3 reports the byte counters of the router's running interfaces every few
seconds. The difference between two heartbeats gives real speeds, which feed:
  * the live load-balancing view (router telemetry)
  * the live topology map and the port panel
  * the Traffic report (WAN samples), exactly like direct-API routers
Active sessions come from the same heartbeat; configuration comes from the latest
Link inventory sync.
"""
from django.core.cache import cache
from django.utils import timezone

RATES_TTL = 180


def _int(v):
    try:
        return int(str(v or 0).strip() or 0)
    except ValueError:
        return 0


def parse_counters(raw):
    out = {}
    for rec in str(raw or '').split(';'):
        f = rec.split(',')
        if len(f) >= 3 and f[0]:
            out[f[0]] = (_int(f[1]), _int(f[2]))
    return out


def ingest_counters(router, raw, now=None):
    """Turn two heartbeats' counters into speeds; record WAN traffic for the Traffic report."""
    from .traffic import MAX_GAP, _add_sample, floor_bucket, wan_interfaces
    now = now or timezone.now()
    cur = parse_counters(raw)
    if not cur:
        return {}
    _ensure_ports(router, cur, now)
    ts = now.timestamp()
    key = f'tt:linkctr:{router.pk}'
    prev = cache.get(key) or {}
    rates = {}
    for name, (rx, tx) in cur.items():
        p = prev.get(name)
        if p and 2 <= ts - p[2] <= MAX_GAP and rx >= p[0] and tx >= p[1]:
            dt = ts - p[2]
            rates[name] = {'rx_bps': int((rx - p[0]) * 8 / dt), 'tx_bps': int((tx - p[1]) * 8 / dt), 'rx_pps': 0, 'tx_pps': 0}
    cache.set(key, {n: (rx, tx, ts) for n, (rx, tx) in cur.items()}, 3600)
    if rates:
        cache.set(f'tt:linkrates:{router.pk}', {'at': now.isoformat(), 'rates': rates}, RATES_TTL)
        bucket = floor_bucket(now)
        for name in wan_interfaces(router):
            p, c = prev.get(name), cur.get(name)
            if p and c and name in rates:
                _add_sample(router, name, bucket, c[0] - p[0], c[1] - p[1], rates[name]['rx_bps'], rates[name]['tx_bps'])
    return rates


def _ensure_ports(router, cur, now):
    """Save the router's running interfaces from the heartbeat, so its ports appear before a full sync."""
    from .models import RouterInterface
    if not cache.add(f'tt:linkports:{router.pk}', 1, 300):
        return
    known = set(RouterInterface.objects.filter(router=router).values_list('name', flat=True))
    new = [RouterInterface(router=router, name=n, running=True, rx_byte=rx, tx_byte=tx, last_seen_at=now,
                           interface_type=('bridge' if n.startswith('bridge') else 'ether' if n.startswith(('ether', 'sfp')) else 'wlan' if n.startswith(('wlan', 'wifi')) else ''))
           for n, (rx, tx) in cur.items() if n not in known]
    if new:
        RouterInterface.objects.bulk_create(new, ignore_conflicts=True)
    RouterInterface.objects.filter(router=router, name__in=list(cur)).update(running=True, last_seen_at=now, is_present=True)
    # Keep the saved byte totals current so the port panel isn't stuck at the last full sync (or 0).
    stale = []
    for obj in RouterInterface.objects.filter(router=router, name__in=list(cur)).only('pk', 'name', 'rx_byte', 'tx_byte', 'raw_data'):
        rx, tx = cur[obj.name]
        if (rx, tx) != (obj.rx_byte, obj.tx_byte):
            obj.rx_byte, obj.tx_byte = rx, tx
            obj.raw_data = {**(obj.raw_data or {}), 'rx-byte': rx, 'tx-byte': tx}
            stale.append(obj)
    if stale:
        RouterInterface.objects.bulk_update(stale, ['rx_byte', 'tx_byte', 'raw_data'])


def counters(router, name):
    """Byte counters from the most recent heartbeat (a few seconds old), or None."""
    c = (cache.get(f'tt:linkctr:{router.pk}') or {}).get(name)
    return {'rx-byte': c[0], 'tx-byte': c[1]} if c else None


def rates(router, names=None):
    data = cache.get(f'tt:linkrates:{router.pk}') or {}
    r = data.get('rates') or {}
    return {n: v for n, v in r.items() if not names or n in names}


def store_sessions(router, rows):
    cache.set(f'tt:linkactive:{router.pk}', rows, RATES_TTL)


def sessions(router):
    return cache.get(f'tt:linkactive:{router.pk}') or []


def link_state(router):
    """(online, message) for pages that would otherwise try the router's IP."""
    try:
        agent = router.agent
    except Exception:
        return False, f'{router.name} is set to TapTap Link but no Link is set up yet.'
    if agent.revoked:
        return False, f'TapTap Link for {router.name} is revoked.'
    if agent.online:
        return True, ''
    when = timezone.localtime(agent.last_seen_at).strftime('%d %b %H:%M') if agent.last_seen_at else 'never'
    return False, f'{router.name} has not checked in through TapTap Link since {when}.'


def telemetry(router):
    """Same shape as MikroTikService.wan_telemetry(), built from Link data."""
    from .models import RouterConfigSnapshot
    snap = RouterConfigSnapshot.objects.filter(router=router).first()
    lb = (snap.load_balancing if snap else None) or {}
    wan = [l.get('interface') for l in lb.get('wan_links', []) if l.get('interface')]
    for bond in lb.get('bonds', []) or []:
        wan += bond.get('slaves', [])
    traffic = rates(router, set(wan))
    return {'load_balancing': lb, 'traffic': traffic, 'interfaces': [], 'transport': 'link',
            'captured_at': (cache.get(f'tt:linkrates:{router.pk}') or {}).get('at') or timezone.now().isoformat()}
