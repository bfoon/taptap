"""App & site control on the router.

TapTap turns your rules into RouterOS objects, all tagged ``TT-APP`` so it can replace them
cleanly each time something changes:

* an address list per rule with the service's domains (the router resolves them itself),
* firewall rules that match the HTTPS server name (tls-host) for the many sub-domains,
* Block  → drop in the forward chain;  Always allow → accept above every block;
* Slow   → connections are marked in mangle and shaped per device with PCQ queues
           (download and upload separately);
* "QUIC" → UDP 443 is stopped while a block/slow rule is active, so apps fall back to HTTPS
           where the server name can be seen;
* times  → the router's own ``time=`` matcher (from–to, days), so schedules keep working even
           when TapTap can't reach the router. Temporary rules are added and removed by TapTap
           when they start and end.
* FastTrack would skip all of this, so marked and new HTTPS connections are kept out of it.

Set the router's clock and time zone (System › Clock / NTP) — schedules use the router's time.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import time as dtime

from django.utils import timezone

from .app_catalog import SERVICES

logger = logging.getLogger('taptap.apps')

TAG = 'TT-APP'
LAN = ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10']
DAYS = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat']
RESEND_EVERY = 3600   # seconds: full resend even without changes (repairs anything removed by hand)


def _t(t):
    return f'{t.hour:02d}:{t.minute:02d}:00'


def windows(rule, business):
    """RouterOS time= values for the rule ([None] = all day)."""
    def span(a, b, days):
        d = ','.join(x for x in DAYS if x in (days or []))
        if a is None or b is None or a == b:
            return [d or None] if d else [None]
        if a < b:
            return [f'{_t(a)}-{_t(b)}' + (f',{d}' if d else '')]
        # crosses midnight: two windows
        return [f'{_t(a)}-23:59:59' + (f',{d}' if d else ''), f'00:00:00-{_t(b)}' + (f',{d}' if d else '')]
    if rule.when == 'window':
        return span(rule.from_time, rule.to_time, rule.days)
    if rule.when == 'peak':
        if not business.peak_from or not business.peak_to:
            return []          # peak hours not set yet: nothing to do
        return span(business.peak_from, business.peak_to, business.peak_days)
    if rule.when == 'offpeak':
        if not business.peak_from or not business.peak_to:
            return []
        return span(business.peak_to, business.peak_from, [])
    days = [x for x in DAYS if x in (rule.days or [])]
    return [','.join(days)] if days else [None]


def active_rules(router, now=None):
    now = now or timezone.now()
    out = []
    for r in router.business.app_rules.filter(enabled=True).prefetch_related('routers'):
        if r.starts_at and r.starts_at > now:
            continue
        if r.ends_at and r.ends_at <= now:
            continue
        ids = [x.pk for x in r.routers.all()]
        if ids and router.pk not in ids:
            continue
        out.append(r)
    order = {'allow': 0, 'block': 1, 'slow': 2}
    return sorted(out, key=lambda r: (order.get(r.action, 9), r.pk))


def _domains(rule):
    d, s = [], []
    for k in rule.services or []:
        svc = SERVICES.get(k)
        if svc:
            d += svc['domains']; s += svc['sni']
    for x in rule.custom_domains or []:
        x = str(x).strip().lower().lstrip('*.').strip('/')
        if x:
            d.append(x); s.append('*' + x)
    return list(dict.fromkeys(d)), list(dict.fromkeys(s))


def compile_spec(router, now=None):
    """Everything TapTap wants on this router, as plain data (also used for the digest)."""
    business = router.business
    lists, flt, mng, qtypes, qtrees, guard = [], [], [], [], [], []
    rules = active_rules(router, now)
    if rules:
        lists += [('TT-APP-LAN', n) for n in LAN]
    for r in rules:
        win = windows(r, business)
        if not win:
            continue
        domains, sni = _domains(r)
        if not domains and not sni:
            continue
        lst = f'TT-APP-{r.pk}'
        lists += [(lst, d) for d in domains]
        auth = {'hotspot': 'auth'} if r.scope == 'customers' else {}
        c = f'{TAG} {r.pk} {r.name}'[:120]
        for w in win:
            tm = {'time': w} if w else {}
            if r.action in ('allow', 'block'):
                act = 'accept' if r.action == 'allow' else 'drop'
                flt.append({'chain': 'forward', 'action': act, 'dst-address-list': lst, **auth, **tm, 'comment': c})
                for p in sni:
                    flt.append({'chain': 'forward', 'action': act, 'protocol': 'tcp', 'tls-host': p, **auth, **tm, 'comment': c})
                if r.action == 'allow':
                    mng.append({'chain': 'forward', 'action': 'accept', 'dst-address-list': lst, **tm, 'comment': c})
                elif r.block_quic:
                    flt.append({'chain': 'forward', 'action': 'drop', 'protocol': 'udp', 'dst-port': '443', **auth, **tm, 'comment': c + ' (QUIC)'})
            else:   # slow down: mark the connection, then shape each device's download / upload
                mark = f'tt-app-{r.pk}'
                mng.append({'chain': 'forward', 'action': 'mark-connection', 'new-connection-mark': mark, 'passthrough': 'yes',
                            'connection-mark': 'no-mark', 'dst-address-list': lst, **auth, **tm, 'comment': c})
                for p in sni:
                    mng.append({'chain': 'forward', 'action': 'mark-connection', 'new-connection-mark': mark, 'passthrough': 'yes',
                                'connection-mark': 'no-mark', 'protocol': 'tcp', 'tls-host': p, **auth, **tm, 'comment': c})
                mng.append({'chain': 'forward', 'action': 'mark-packet', 'new-packet-mark': mark + '-d', 'passthrough': 'no',
                            'connection-mark': mark, 'dst-address-list': 'TT-APP-LAN', **tm, 'comment': c})
                mng.append({'chain': 'forward', 'action': 'mark-packet', 'new-packet-mark': mark + '-u', 'passthrough': 'no',
                            'connection-mark': mark, 'src-address-list': 'TT-APP-LAN', **tm, 'comment': c})
                if r.block_quic:
                    flt.append({'chain': 'forward', 'action': 'drop', 'protocol': 'udp', 'dst-port': '443', **auth, **tm, 'comment': c + ' (QUIC)'})
        if r.action == 'slow':
            mark = f'tt-app-{r.pk}'
            dk, uk = max(64, int(float(r.down_mbps) * 1000)), max(32, int(float(r.up_mbps) * 1000))
            qtypes += [{'name': mark + '-d', 'kind': 'pcq', 'pcq-rate': f'{dk}k', 'pcq-classifier': 'dst-address'},
                       {'name': mark + '-u', 'kind': 'pcq', 'pcq-rate': f'{uk}k', 'pcq-classifier': 'src-address'}]
            qtrees += [{'name': mark + '-d', 'parent': 'global', 'packet-mark': mark + '-d', 'queue': mark + '-d', 'comment': f'{TAG} {r.pk}'},
                       {'name': mark + '-u', 'parent': 'global', 'packet-mark': mark + '-u', 'queue': mark + '-u', 'comment': f'{TAG} {r.pk}'}]
            guard.append({'chain': 'forward', 'action': 'accept', 'connection-mark': mark, 'comment': f'{TAG} no fasttrack'})
    if flt or mng:
        # the HTTPS server name is in the first data packets: keep young HTTPS connections out of FastTrack
        guard.append({'chain': 'forward', 'action': 'accept', 'protocol': 'tcp', 'dst-port': '443', 'connection-bytes': '0-20000', 'comment': f'{TAG} no fasttrack'})
    return {'lists': lists, 'filter': flt, 'mangle': mng, 'qtypes': qtypes, 'qtrees': qtrees, 'guard': guard}


def digest(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()


# ─────────────────────────────── RouterOS script (TapTap Link) ───────────────────────────────

def _q(v):
    v = str(v).replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$')
    return f'"{v}"'


def _args(d):
    return ' '.join(f'{k}={_q(v)}' for k, v in d.items())


def link_script(spec):
    out = [
        f':do {{ /ip firewall filter remove [find comment~"^{TAG}"] }} on-error={{}}',
        f':do {{ /ip firewall mangle remove [find comment~"^{TAG}"] }} on-error={{}}',
        f':do {{ /queue tree remove [find comment~"^{TAG}"] }} on-error={{}}',
        ':do { /queue type remove [find name~"^tt-app-"] } on-error={}',
        ':do { /ip firewall address-list remove [find list~"^TT-APP-"] } on-error={}',
    ]
    for lst, addr in spec['lists']:
        out.append(f':do {{ /ip firewall address-list add list={_q(lst)} address={_q(addr)} comment="{TAG}" }} on-error={{}}')
    for qt in spec['qtypes']:
        out.append(f'/queue type add {_args(qt)}')
    for qt in spec['qtrees']:
        out.append(f'/queue tree add {_args(qt)}')
    # TapTap's rules go above the existing ones, in order
    for menu, rows in (('filter', spec['filter']), ('mangle', spec['mangle'])):
        if rows:
            out.append(f':global ttf0 [:pick [/ip firewall {menu} find] 0]')
            for row in rows:
                out.append(f':if ([:len $ttf0] > 0) do={{ /ip firewall {menu} add {_args(row)} place-before=$ttf0 }} else={{ /ip firewall {menu} add {_args(row)} }}')
    if spec['guard']:
        out.append(':global ttft [:pick [/ip firewall filter find action=fasttrack-connection] 0]')
        for row in spec['guard']:
            out.append(f':if ([:len $ttft] > 0) do={{ /ip firewall filter add {_args(row)} place-before=$ttft }}')
    out.append(f':log info "TapTap app control: {len(spec["filter"]) + len(spec["mangle"])} rules"')
    return '\n'.join(out)


# ─────────────────────────────── apply ───────────────────────────────

def _apply_api(svc, spec):
    def res(p):
        return svc.resource(p)
    kw = lambda d: {k.replace('-', '_'): v for k, v in d.items()}
    for path in ('/ip/firewall/filter', '/ip/firewall/mangle', '/queue/tree'):
        r = res(path)
        for row in r.get():
            if str(row.get('comment', '')).startswith(TAG) and row.get('id'):
                r.remove(id=row['id'])
    qt = res('/queue/type')
    for row in qt.get():
        if str(row.get('name', '')).startswith('tt-app-') and row.get('id'):
            qt.remove(id=row['id'])
    al = res('/ip/firewall/address-list')
    for row in al.get():
        if str(row.get('list', '')).startswith('TT-APP-') and row.get('id'):
            al.remove(id=row['id'])
    for lst, addr in spec['lists']:
        try:
            al.add(list=lst, address=addr, comment=TAG)
        except Exception as exc:          # a name that does not resolve must not stop the rest
            logger.info('address list %s %s: %s', lst, addr, exc)
    for row in spec['qtypes']:
        qt.add(**kw(row))
    qtree = res('/queue/tree')
    for row in spec['qtrees']:
        qtree.add(**kw(row))
    for path, rows in (('/ip/firewall/filter', spec['filter']), ('/ip/firewall/mangle', spec['mangle'])):
        if not rows:
            continue
        r = res(path)
        first = next((x for x in r.get() if x.get('id')), None)
        for row in rows:
            r.add(place_before=first['id'], **kw(row)) if first else r.add(**kw(row))
    if spec['guard']:
        r = res('/ip/firewall/filter')
        ft = next((x for x in r.get() if str(x.get('action', '')) == 'fasttrack-connection'), None)
        if ft:
            for row in spec['guard']:
                r.add(place_before=ft['id'], **kw(row))


def push(router, force=False, user=None, now=None):
    """Send this router its app-control rules if they changed (or force). Returns (ok, message)."""
    from .models_apps import AppControlState
    from .voucher_history import channel
    spec = compile_spec(router, now)
    dg = digest(spec)
    st, _ = AppControlState.objects.get_or_create(router=router)
    stale = not st.applied_at or (timezone.now() - st.applied_at).total_seconds() > RESEND_EVERY
    if not force and st.digest == dg and st.status in ('applied', 'queued') and not stale:
        return True, 'unchanged'
    if not spec['filter'] and not spec['mangle'] and not st.digest:
        return True, 'nothing to do'
    n = len(spec['filter']) + len(spec['mangle'])
    try:
        if channel(router) == 'TapTap Link':
            from .linkops import send
            send(router, 'app_control', {'spec': spec}, label=f'App control: {n} router rules', user=user, minutes=60 * 6)
            st.status, st.message = 'queued', f'{n} rules queued for the next check-in'
        else:
            from .mikrotik import MikroTikService
            svc = MikroTikService(router).connect()
            try:
                _apply_api(svc, spec)
            finally:
                svc.close()
            st.status, st.message = 'applied', f'{n} rules on the router'
        st.digest, st.applied_at = dg, timezone.now()
        st.save()
        return True, st.message
    except Exception as exc:
        st.status, st.message = 'error', str(exc)[:255]
        st.save(update_fields=['status', 'message'])
        return False, f'{router.name}: {exc}'


def push_all(business, force=False, user=None):
    return [(r, *push(r, force=force, user=user)) for r in business.routers.all()]


def describe_when(rule, business):
    if rule.when == 'always':
        w = 'All day'
    elif rule.when == 'window':
        w = f'{rule.from_time:%H:%M}–{rule.to_time:%H:%M}' if rule.from_time and rule.to_time else 'Set hours'
    elif rule.when == 'peak':
        w = f'Peak hours ({business.peak_from:%H:%M}–{business.peak_to:%H:%M})' if business.peak_from and business.peak_to else 'Peak hours (not set yet)'
    else:
        w = f'Off-peak ({business.peak_to:%H:%M}–{business.peak_from:%H:%M})' if business.peak_from and business.peak_to else 'Off-peak (not set yet)'
    if rule.days and rule.when in ('always', 'window'):
        w += ' · ' + ', '.join(d.title() for d in DAYS if d in rule.days)
    return w
