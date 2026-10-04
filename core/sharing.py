"""Internet sharing protection — spotting a laptop / phone hotspot / small router that shares one voucher.

Behind NAT many phones look like one HotSpot client, so "1 device per voucher" cannot count them.
TapTap does not try to count them; it scores how likely it is that one client is sharing:

    TTL 63 or 127 seen (traffic passed one more router)      +40
    Known router / repeater (maker of its MAC, its name)     +40
    Several operating systems behind it                      +30
    Many busy connections at once                            +15
    Unusually many different apps / sites at once            +15

    0–39 normal · 40–59 suspected · 60–79 warning · threshold (default 80) → your chosen action

Where the signals come from
* TTL — the router itself: mangle rules (tagged TT-SHARE) put the address of every logged-in customer
  whose new connections arrive with TTL 64 / 63 / 128 / 127 into short address lists. A phone or laptop
  plugged straight into the hotspot sends 64 or 128; one hop behind another router it arrives as 63 / 127.
  Seeing 64 *and* 128 from one address also means two kinds of systems (e.g. a Windows laptop + phones).
  Direct API routers are read every 2 minutes; TapTap Link routers upload the lists with the traffic samples.
* Router makers — the MAC's maker (TP-Link, Tenda, MikroTik, GL.iNet…) and names like OpenWrt / TL-WR.
* Operating systems — the per-device traffic TapTap already records: Apple + Android + Windows update /
  sync services at the same time behind one MAC.
* Busy connections and app variety — from the same traffic samples.

Actions (your choice, Security › Internet sharing): monitor only, warn the customer (the voucher pauses until
they accept your message on the login page), block (disconnect + refuse that device on the login page for N
minutes, showing your message; it is disconnected again if it comes back), or warn first and block if it
happens again within 24 hours. Plans for several devices can be left out, and devices you trust (your own
access point, the till PC) are never flagged. A single signal never blocks on its own.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('taptap.sharing')

TAG = 'TT-SHARE'
LISTS = {'tt-share-ttl64': 64, 'tt-share-ttl63': 63, 'tt-share-ttl128': 128, 'tt-share-ttl127': 127}
WEIGHTS = {'ttl': 40, 'router': 40, 'multi_os': 30, 'conns': 15, 'variety': 15}
BUSY_CONNS = 40          # busy connections at once from one customer
MANY_APPS = 18           # different apps / sites in 30 minutes behind one MAC
ROUTER_WORDS = re.compile(r'openwrt|lede|tl-wr|tl-mr|archer|deco|tenda|netis|mercusys|gl-|gl\.inet|mikrotik|routerboard|'
                          r'ubnt|unifi|netgear|d-link|dlink|huawei-b|zte|mifi|mi router|xiaomi_router|repeater|extender|'
                          r'router|ap-|access point|hotspot', re.I)
OS_APPS = {'Apple updates & iCloud': 'Apple', 'Apple TV & Music': 'Apple', 'Google Play & Android': 'Android',
           'Windows Update': 'Windows', 'Microsoft & Office': 'Windows'}


def policy(business):
    from .models_sharing import SharingPolicy
    return SharingPolicy.objects.get_or_create(business=business)[0]


def band(score, p):
    if score >= p.threshold:
        return 'action'
    if score >= 60:
        return 'warning'
    if score >= p.suspect_at:
        return 'suspected'
    return 'normal'


# ─────────────────────────────── router rules ───────────────────────────────

def rules():
    out = []
    for name, ttl in LISTS.items():
        out.append({'chain': 'prerouting', 'action': 'add-src-to-address-list', 'address-list': name, 'address-list-timeout': '10m',
                    'connection-state': 'new', 'hotspot': 'auth', 'ttl': f'equal:{ttl}', 'comment': f'{TAG}: TTL {ttl} from customers'})
    return out


def link_script(enable=True):
    lines = [f':do {{ /ip firewall mangle remove [find comment~"^{TAG}"] }} on-error={{}}',
             ':do { /ip firewall address-list remove [find list~"^tt-share-"] } on-error={}']
    if enable:
        for r in rules():
            args = ' '.join(f'{k}="{v}"' for k, v in r.items())
            lines.append(f':do {{ /ip firewall mangle add {args} }} on-error={{ :log warning "TapTap sharing: rule not added" }}')
    return '\n'.join(lines)


def apply_router(router, enable=True, user=None):
    """Install (or remove) the TTL rules on one router. Returns (ok, message)."""
    from .voucher_history import channel
    try:
        if channel(router) == 'TapTap Link':
            from .linkops import send
            send(router, 'share_rules', {'enable': bool(enable)}, label=('Turn on' if enable else 'Turn off') + ' Internet sharing detection', user=user)
            return True, f'{router.name}: queued for the next check-in'
        from .mikrotik import MikroTikService
        svc = MikroTikService(router).connect()
        try:
            m = svc.resource('/ip/firewall/mangle')
            for row in m.get():
                if str(row.get('comment', '')).startswith(TAG) and row.get('id'):
                    m.remove(id=row['id'])
            if enable:
                for r in rules():
                    m.add(**{k.replace('-', '_'): v for k, v in r.items()})
        finally:
            svc.close()
        return True, f'{router.name}: {"on" if enable else "off"}'
    except Exception as exc:
        return False, f'{router.name}: {exc}'


def apply_all(business, enable=True, user=None):
    return [apply_router(r, enable, user) for r in business.routers.all()]


# ─────────────────────────────── signals ───────────────────────────────

def ingest_lists(router, rows):
    """Address-list rows (list, address) from the router → {ip: [ttl, …]} for the next scoring pass."""
    by_ip = {}
    for r in rows or []:
        ttl = LISTS.get(str(r.get('list', '')))
        ip = str(r.get('address', '')).split('/')[0]
        if ttl and ip:
            by_ip.setdefault(ip, set()).add(ttl)
    cache.set(f'tt:share:ttl:{router.pk}', {k: sorted(v) for k, v in by_ip.items()}, 900)
    return by_ip


def note_connections(router, counts):
    """Busy connections per customer address (from the traffic collector)."""
    cache.set(f'tt:share:conns:{router.pk}', dict(counts), 600)


def read_api(router):
    """Direct API routers: read the TTL lists (at most every 2 minutes)."""
    if not cache.add(f'tt:share:read:{router.pk}', 1, 110):
        return
    from .mikrotik import MikroTikService
    try:
        svc = MikroTikService(router).connect()
        try:
            rows = [r for r in svc.safe_get('/ip/firewall/address-list') if str(r.get('list', '')).startswith('tt-share-')]
        finally:
            svc.close()
        ingest_lists(router, rows)
    except Exception as exc:
        logger.info('sharing lists %s: %s', router, exc)


def _os_and_variety(business, macs):
    from .models import DeviceAppUsage
    since = timezone.now() - timedelta(minutes=30)
    out = {}
    for mac, app in (DeviceAppUsage.objects.filter(business=business, hour__gte=since.replace(minute=0, second=0, microsecond=0),
                                                   mac__in=list(macs)).values_list('mac', 'app').distinct()):
        d = out.setdefault(mac.upper(), {'os': set(), 'apps': set()})
        d['apps'].add(app)
        if app in OS_APPS:
            d['os'].add(OS_APPS[app])
    return out


def score_client(mac, ip, ttls, conns, os_apps, hostname=''):
    """(score 0–100, reasons) for one logged-in customer."""
    from .net_vendors import brand_of
    reasons, score = [], 0
    if 63 in ttls or 127 in ttls:
        score += WEIGHTS['ttl']; reasons.append(f'TTL {"/".join(str(t) for t in ttls if t in (63, 127))} — passes through another router')
    brand = brand_of(mac)
    if brand or (hostname and ROUTER_WORDS.search(hostname)):
        score += WEIGHTS['router']; reasons.append(f'Router / repeater ({brand or hostname})')
    families = set((os_apps or {}).get('os', set()))
    if (64 in ttls or 63 in ttls) and (128 in ttls or 127 in ttls):
        families.update({'Windows', 'phone / Linux'})
    if len(families) >= 2:
        score += WEIGHTS['multi_os']; reasons.append('Several systems behind it (' + ', '.join(sorted(families)) + ')')
    if conns >= BUSY_CONNS:
        score += WEIGHTS['conns']; reasons.append(f'{conns} busy connections at once')
    apps = len((os_apps or {}).get('apps', ()))
    if apps >= MANY_APPS:
        score += WEIGHTS['variety']; reasons.append(f'{apps} different apps / sites in 30 minutes')
    return min(100, score), reasons


# ─────────────────────────────── evaluation ───────────────────────────────

def evaluate_router(router, now=None):
    """Score every logged-in customer of this router and act on the policy. Returns cases touched."""
    from .models import RouterDevice, Voucher
    from .models_sharing import SharingCase, SharingTrust
    business = router.business
    p = policy(business)
    if not p.enabled:
        return 0
    now = now or timezone.now()
    snap = cache.get(f'tt:tr:users:{router.pk}') or {}
    sessions = []
    for code, rows in (snap.get('users') or {}).items():
        if str(code).upper().startswith('BYPASS:'):
            continue
        for r in rows:
            if r.get('mac'):
                sessions.append((code, r['mac'].upper(), r.get('ip', '')))
    if not sessions:
        return 0
    ttl = cache.get(f'tt:share:ttl:{router.pk}') or {}
    conns = cache.get(f'tt:share:conns:{router.pk}') or {}
    trusted = set(SharingTrust.objects.filter(business=business).values_list('mac', flat=True))
    vouchers = {v.code.upper(): v for v in Voucher.objects.filter(business=business, code__in=[c for c, _, _ in sessions])}
    exempt = set(p.exempt_plans.values_list('name', flat=True))
    names = dict(RouterDevice.objects.filter(router=router, mac_address__in=[m for _, m, _ in sessions]).values_list('mac_address', 'hostname'))
    extra = _os_and_variety(business, {m for _, m, _ in sessions})
    touched = 0
    for code, mac, ip in sessions:
        v = vouchers.get(str(code).upper())
        if mac in trusted or v is None:
            continue
        if v.plan_name in exempt or (p.single_device_only and (v.max_devices or 1) > 1):
            continue
        s, reasons = score_client(mac, ip, ttl.get(ip, []), int(conns.get(ip, 0)), extra.get(mac), names.get(mac, ''))
        if s < p.suspect_at:
            continue
        case = (SharingCase.objects.filter(business=business, mac=mac, status__in=('suspected', 'warned', 'blocked'))
                .order_by('-last_seen').first())
        new = case is None
        if new:
            case = SharingCase(business=business, mac=mac)
        case.router, case.voucher, case.code, case.ip, case.score, case.reasons = router, v, v.code, ip, s, reasons
        case.peak = max(case.peak or 0, s)
        if not new:
            case.times += 1
        case.save()
        touched += 1
        if new:
            _tell(business, p, case, f'Internet sharing suspected on {v.code}', level='warning' if s >= 60 else 'info')
        if s >= p.threshold:
            act(case, p, auto=True)
    return touched


def act(case, p=None, auto=False, user=None, force=None):
    """Apply the policy (or a forced action: 'warn' / 'block') to a detection."""
    business = case.business
    p = p or policy(business)
    action = force or p.action
    if action == 'escalate':
        from .models_sharing import SharingCase
        earlier = SharingCase.objects.filter(business=business, mac=case.mac, status__in=('warned', 'blocked'),
                                             last_seen__gte=timezone.now() - timedelta(hours=24)).exclude(pk=case.pk).exists()
        action = 'block' if (earlier or case.status == 'warned') else 'warn'
    if action == 'monitor':
        if auto and cache.add(f'tt:share:mon:{case.pk}', 1, 3600):
            _tell(business, p, case, f'Internet sharing likely on {case.code} ({case.score}%)', level='danger')
        return 'monitor'
    if action == 'warn' and case.status not in ('warned', 'blocked'):
        if case.voucher is not None:
            from .voucher_freeze import FreezeError, freeze
            try:
                freeze([case.voucher], user=user, reason=f'Internet sharing detected ({case.score}%)', kind='warning',
                       source='auto' if auto else 'user', message=p.message)
            except FreezeError:
                pass
        case.status, case.action_taken = 'warned', ('auto: ' if auto else '') + 'customer warned'
        case.handled_by = user
        case.save(update_fields=['status', 'action_taken', 'handled_by', 'last_seen'])
        _tell(business, p, case, f'{case.code} warned for Internet sharing ({case.score}%)', level='warning')
        return 'warn'
    if action == 'block':
        until = timezone.now() + timedelta(minutes=max(1, p.block_minutes))
        case.status, case.blocked_until, case.action_taken = 'blocked', until, ('auto: ' if auto else '') + f'blocked {p.block_minutes} min'
        case.handled_by = user
        case.save(update_fields=['status', 'blocked_until', 'action_taken', 'handled_by', 'last_seen'])
        cache.set(f'tt:share:block:{business.pk}:{case.mac}', p.message, max(60, p.block_minutes * 60))
        _disconnect(case)
        if case.voucher is not None:
            from .voucher_history import record
            record(case.voucher, 'enforced', source='auto' if auto else 'user', user=user, reason='Internet sharing',
                   text=f'{case.mac} disconnected and refused for {p.block_minutes} min — sharing score {case.score}%: ' + '; '.join(case.reasons))
        _tell(business, p, case, f'{case.code} blocked for Internet sharing ({case.score}%)', level='danger')
        return 'block'
    return ''


def _disconnect(case):
    from .voucher_history import channel
    router = case.router
    if router is None:
        return
    try:
        if channel(router) == 'TapTap Link':
            from .linkops import send
            send(router, 'hotspot_kick', {'user': case.code, 'mac': case.mac}, label=f'Internet sharing: disconnect {case.mac}', minutes=30)
        else:
            from .mikrotik import MikroTikService
            svc = MikroTikService(router).connect()
            try:
                for path in ('/ip/hotspot/active', '/ip/hotspot/cookie'):
                    res = svc.resource(path)
                    for row in res.get():
                        if str(row.get('mac-address', '')).upper() == case.mac:
                            res.remove(id=row['id'])
            finally:
                svc.close()
    except Exception as exc:
        logger.info('sharing disconnect %s: %s', case.mac, exc)


def blocked_message(business, mac):
    """Login page: the message to show while this device is blocked for sharing (None = not blocked)."""
    m = str(mac or '').upper()
    return cache.get(f'tt:share:block:{business.pk}:{m}') if m else None


def enforce_blocks(router):
    """A blocked device that is back online during its block is disconnected again; ended blocks are closed."""
    from .models_sharing import SharingCase
    now = timezone.now()
    SharingCase.objects.filter(business=router.business, status='blocked', blocked_until__lte=now).update(status='cleared', action_taken='block ended')
    snap = cache.get(f'tt:tr:users:{router.pk}') or {}
    online = {r.get('mac', '').upper() for rows in (snap.get('users') or {}).values() for r in rows}
    for case in SharingCase.objects.filter(business=router.business, router=router, status='blocked', blocked_until__gt=now, mac__in=online):
        if cache.add(f'tt:share:rekick:{case.pk}', 1, 120):
            _disconnect(case)


def _tell(business, p, case, title, level='warning'):
    if p.notify == 'none':
        return
    from .models_events import EventAlert
    body = f'{case.mac} on {case.router.name if case.router else "a router"} — score {case.score}%: ' + '; '.join(case.reasons)
    EventAlert.objects.create(business=business, kind='sharing', level=level, title=title[:160], body=body[:400],
                              link='/security/#sharing', sound=level != 'info', desktop=level != 'info')
    if p.notify == 'app_email':
        try:
            from .notify import notify
            notify(business, 'rule_alert', title, body, link='/security/#sharing', key=f'sharing:{case.pk}:{case.status}')
        except Exception:
            pass


def run(router):
    """One pass for a router (live sync): read signals (API), score, enforce blocks."""
    p = policy(router.business)
    if not p.enabled:
        return 0
    from .voucher_history import channel
    if channel(router) != 'TapTap Link':
        read_api(router)
    n = evaluate_router(router)
    enforce_blocks(router)
    return n
