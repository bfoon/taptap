"""Speed per device — every device of a voucher or member gets the plan's whole speed, its own lane.

How the speed is issued today
-----------------------------
A plan's speed (``VoucherPlan.speed_limit``), a member plan's speed (``MemberPlan.speed_limit`` → the member's
``rate_limit`` + ``router_profile``) or a custom voucher's ``rate_limit`` is written to the router as the hotspot
**user profile's** ``rate-limit``, together with ``shared-users`` = the number of devices (core/utils.voucher_profile).

RouterOS then makes one dynamic simple queue **per signed-in device**, targeting only that device's address. So a
2M/2M plan with 3 devices is already 3 × 2M/2M — *when nothing on the router gets in the way.* These do:

* **A parent queue on the profile** (Mikhmon's "Parent queue", or set by hand): every device of the profile becomes
  a child of that queue and they all share its limit.
* **A hand-made queue above the hotspot's** that targets a whole network or an interface: it catches the devices
  first, so they all share it.
* **The hotspot server's own queue** (``<hs-…>``): a profile without a speed falls through to it — one queue for
  everybody on that hotspot.
* **A speed change while devices are online**: RouterOS reads the profile only at login; devices already online
  keep their old queue until they sign in again.
* **FastTrack**: fast-tracked traffic skips *every* simple queue — the device gets more than its plan.

What this module adds
---------------------
* ``lanes`` — a router check: for each device online (of a voucher, or of every voucher allowed 2+ devices), which
  queue really carries its traffic, at what speed, shared with whom. Direct API / Tunnel at once, TapTap Link at the
  next check-in (the same NetTest path as the Connection lab, core/conn_lab.py).
* ``lanefix`` — profiles back to TapTap's speed, a squeezing parent queue removed from TapTap's own profiles, and
  (when chosen) hotspot customers kept out of FastTrack. Read back from the router.
* **Lanes** (``DeviceLane``) — TapTap itself puts each device in its own queue at the plan speed: one TTSPD queue per
  device, above the hotspot's queues, every live-sync pass (core/traffic_speed.py). For one voucher, or always for
  every shared voucher. Applies at once to devices already online; an owner's own speed rule still wins.
"""
from __future__ import annotations

import ipaddress
import re

from django.core.cache import cache
from django.utils import timezone

PROFILE_RE = re.compile(r'^[\w .@:+/<>()-]{1,120}$')
RATE_RE = re.compile(r'^[0-9A-Za-z/. ]{1,120}$')
NET_RE = re.compile(r'^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$')
IP_RE = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')
MAX_IPS = 40        # keeps one router answer well under the upload limit
MAX_STATIC = 40
MAX_PROFILES = 50
GUARD = 'TapTap per-device speed: no fasttrack'
NETLIST = 'TTHS-net'
LANE_LABEL = 'Own lane: plan speed per device'
OWN_QUEUE_PREFIXES = ('TTSPD-', 'TTFUP-')


def _s(v):
    return str(v if v is not None else '').strip()


def _yes(v):
    return _s(v).lower() in ('true', 'yes')


# ─────────────────────────────── speeds ───────────────────────────────

def speed_of(v, plans=None):
    """(rate text, up bps, down bps, profile) TapTap gives one device of this voucher / member ('' / 0 = no limit)."""
    from .conn_lab import parse_rate
    from .utils import voucher_profile
    plan = None
    if v.plan_name:
        plan = (plans or {}).get(v.plan_name.lower()) if plans is not None else \
            v.business.plans.filter(name__iexact=v.plan_name).first()
    try:
        profile, _shared, rate = voucher_profile(v, plan)
    except Exception:
        profile, rate = '', (v.rate_limit or '')
    rate = _s(rate)
    up, down = parse_rate(rate)
    return rate, up, down, _s(profile)


def plans_map(business):
    return {p.name.lower(): p for p in business.plans.all()}


def human(bps):
    from .traffic_report import human_bps
    return human_bps(bps) if bps else 'full speed'


def math(rate, devices):
    """'2M/2M each · 3 devices → up to 6 Mb/s down for the whole voucher'."""
    from .conn_lab import parse_rate
    up, down = parse_rate(rate)
    n = max(1, int(devices or 1))
    if not down:
        return {'each': 'full speed', 'total': '', 'text': 'No speed limit: every device gets whatever the line gives.'}
    each = f'{human(down)} down · {human(up)} up'
    if n == 1:
        return {'each': each, 'total': each, 'text': f'{each} for its one device.'}
    total = f'{human(down * n)} down · {human(up * n)} up'
    return {'each': each, 'total': total,
            'text': f'Each of its {n} devices gets {each} of its own — up to {total} for the whole voucher when all {n} are busy.'}


# ─────────────────────────────── which devices to look at ───────────────────────────────

def online(router, codes=None, shared_only=True, business=None, now=None):
    """[(code, session)] online on a router: of the given codes, else of every voucher allowed 2+ devices."""
    from .traffic_idle import _snap, _vouchers
    users, age = _snap(router, now or timezone.now())
    if age is None:
        return [], None
    want = {c.upper() for c in codes or []}
    rows = [(c, s) for c, ss in users.items() for s in ss if s.get('ip') and (not want or c.upper() in want)]
    if not codes and shared_only:
        vs = _vouchers(business or router.business, list({c for c, _s2 in rows}))
        rows = [(c, s) for c, s in rows if (vs.get(c.upper()) and (vs[c.upper()].max_devices or 1) > 1)]
    rows.sort(key=lambda x: (x[0], x[1].get('ip', '')))
    return rows, age


# ─────────────────────────────── reading the router ───────────────────────────────

CODE_RE = re.compile(r'^[\w.@:+/<>-]{1,64}$')


def _local(ip):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.version == 4 and (addr.is_private or addr in ipaddress.ip_network('100.64.0.0/10')) and not addr.is_loopback


def clean(data):
    ips = [i for i in dict.fromkeys(_s(x) for x in (data.get('ips') or [])) if IP_RE.match(i)][:MAX_IPS]
    for ip in ips:
        if not _local(ip):
            raise ValueError('Only devices on your own network can be checked.')
    codes = [c for c in dict.fromkeys(_s(x) for x in (data.get('codes') or [])) if CODE_RE.match(c)][:20]
    out = {'ips': ips, 'target': (codes[0] if len(codes) == 1 else f'{len(ips)} device{"s" if len(ips) != 1 else ""}') if ips else 'speed per device'}
    if codes:
        out['codes'] = codes
    return out


def _q(kind, i, name, target, mx, parent):
    return {'kind': kind, 'i': int(i), 'name': _s(name)[:80], 'target': _s(target).replace(';', ',')[:300], 'max': _s(mx)[:60],
            'parent': '' if _s(parent) in ('', 'none') else _s(parent)[:80]}


def link_body(kind, p, rs):
    """RouterOS for 'lanes' (read) and 'lanefix' (fix + read back). Appends key=value lines to $out."""
    want = '|' + '|'.join(f'{ip}/32' for ip in p.get('ips') or []) + '|'
    read = (
        f':local want {rs(want)}; :local i 0; :local ns 0; '
        ':do { :set out ($out . "n=" . [:len [/queue simple find]] . "\\n") } on-error={}; '
        ':foreach q in=[/queue simple find] do={ :do { '
        ':local nm [/queue simple get $q name]; :local t [:tostr [/queue simple get $q target]]; '
        ':local dy [/queue simple get $q dynamic]; :local k ""; '
        ':if ($dy) do={ :if ([:typeof [:find $want ("|" . $t . "|")]] != "nil") do={ :set k "d" } else={ '
        ':if ([:typeof [:find $t "/32"]] = "nil") do={ :set k "h" } } } else={ '
        ':if ([:pick $nm 0 6] = "TTSPD-" || [:pick $nm 0 6] = "TTFUP-") do={ '
        ':if ([:typeof [:find $want ("|" . $t . "|")]] != "nil") do={ :set k "t" } } else={ '
        f':if ($ns < {MAX_STATIC} && [/queue simple get $q disabled] = false) do={{ :set k "s"; :set ns ($ns + 1) }} }} }}; '
        ':if ([:len $k] > 0) do={ :set out ($out . $k . "=" . $i . "|" . $nm . "|" . $t . "|" . [/queue simple get $q max-limit] . "|" . '
        '[:tostr [/queue simple get $q parent]] . "\\n") }; :set i ($i + 1) } on-error={} }; '
    )
    profiles = (
        f':local np 0; :foreach r in=[/ip hotspot user profile find] do={{ :if ($np < {MAX_PROFILES}) do={{ :do {{ '
        ':local pq ""; :do { :set pq [:tostr [/ip hotspot user profile get $r parent-queue]] } on-error={}; '
        ':local iq ""; :do { :set iq [:tostr [/ip hotspot user profile get $r insert-queue-before]] } on-error={}; '
        ':set out ($out . "p=" . [/ip hotspot user profile get $r name] . "|" . [:tostr [/ip hotspot user profile get $r rate-limit]] . "|" . '
        '$pq . "|" . [:tostr [/ip hotspot user profile get $r shared-users]] . "|" . $iq . "\\n"); :set np ($np + 1) } on-error={} } }; '
        ':foreach r in=[/ip hotspot profile find] do={ :do { :set out ($out . "sp=" . [/ip hotspot profile get $r name] . "|" . '
        '[:tostr [/ip hotspot profile get $r rate-limit]] . "\\n") } on-error={} }; '
        ':do { :set out ($out . "ft=" . [:len [/ip firewall filter find action=fasttrack-connection disabled=no]] . "\\n") } on-error={}; '
        f':do {{ :set out ($out . "fg=" . [:len [/ip firewall filter find comment={rs(GUARD)}]] . "\\n") }} on-error={{}}; '
        ':foreach h in=[/ip hotspot find disabled=no] do={ :do { :local ifc [/ip hotspot get $h interface]; '
        ':foreach a in=[/ip address find interface=$ifc] do={ :local ad [:tostr [/ip address get $a address]]; '
        ':set out ($out . "hn=" . [/ip address get $a network] . [:pick $ad [:find $ad "/"] [:len $ad]] . "\\n") } } on-error={} }; '
    )
    if kind == 'lanes':
        return read + profiles
    fix = ''
    for name, rate in p.get('rates') or []:
        fix += (f':do {{ /ip hotspot user profile set [find name={rs(name)}] rate-limit={rs(rate)}; '
                f':set out ($out . "ch=" . {rs(name)} . "\\n") }} on-error={{ :set out ($out . "er=" . {rs(name)} . "\\n") }}; ')
    for name in p.get('unparent') or []:
        fix += (f':do {{ /ip hotspot user profile unset [find name={rs(name)}] parent-queue; :set out ($out . "up=" . {rs(name)} . "\\n") }} '
                f'on-error={{ :do {{ /ip hotspot user profile set [find name={rs(name)}] parent-queue=none; :set out ($out . "up=" . {rs(name)} . "\\n") }} '
                f'on-error={{ :set out ($out . "er=" . {rs(name)} . "\\n") }} }}; ')
    if p.get('guard'):
        for net in p.get('nets') or []:
            fix += (f':do {{ :if ([:len [/ip firewall address-list find list={NETLIST} address={net}]] = 0) do={{ '
                    f'/ip firewall address-list add list={NETLIST} address={net} comment={rs("TapTap hotspot network")} }} }} on-error={{}}; ')
        fix += (':do { :local ft [/ip firewall filter find action=fasttrack-connection disabled=no]; '
                f':if ([:len $ft] > 0 && [:len [/ip firewall filter find comment={rs(GUARD)}]] = 0) do={{ '
                f'/ip firewall filter add chain=forward action=accept connection-state=established,related src-address-list={NETLIST} '
                f'comment={rs(GUARD)} place-before=[:pick $ft 0]; '
                f'/ip firewall filter add chain=forward action=accept connection-state=established,related dst-address-list={NETLIST} '
                f'comment={rs(GUARD)} place-before=[:pick $ft 0]; :set out ($out . "gd=1\\n") }} }} on-error={{ :set out ($out . "er=fasttrack\\n") }}; ')
    return fix + profiles


def _lines_raw(lines):
    raw = {'n': 0, 'queues': [], 'profiles': [], 'servers': [], 'ft': 0, 'fg': 0, 'nets': [],
           'changed': [], 'unparented': [], 'errors': [], 'guarded': False}
    num = lambda v: int(v) if _s(v).isdigit() else 0
    for k, v in lines:
        if k == 'n':
            raw['n'] = num(v)
        elif k in ('d', 'h', 't', 's'):
            a = (v.split('|') + [''] * 5)[:5]
            if a[0].isdigit():
                raw['queues'].append(_q(k, a[0], a[1], a[2], a[3], a[4]))
        elif k == 'p':
            a = (v.split('|') + [''] * 5)[:5]
            raw['profiles'].append({'name': a[0], 'rate': a[1], 'parent': '' if a[2] in ('', 'none') else a[2], 'shared': a[3], 'insert': a[4]})
        elif k == 'sp':
            n, _, r = v.partition('|')
            raw['servers'].append({'name': n, 'rate': r})
        elif k == 'ft':
            raw['ft'] = num(v)
        elif k == 'fg':
            raw['fg'] = num(v)
        elif k == 'hn':
            if NET_RE.match(v):
                raw['nets'].append(v)
        elif k == 'ch':
            raw['changed'].append(v)
        elif k == 'up':
            raw['unparented'].append(v)
        elif k == 'er':
            raw['errors'].append(v)
        elif k == 'gd':
            raw['guarded'] = True
    return raw


def api_read(svc, p):
    """The same picture over the RouterOS API."""
    want = {f'{ip}/32' for ip in p.get('ips') or []}
    raw = {'n': 0, 'queues': [], 'profiles': [], 'servers': [], 'ft': 0, 'fg': 0, 'nets': [],
           'changed': [], 'unparented': [], 'errors': [], 'guarded': False}
    rows = svc.resource('/queue/simple').get() or []
    raw['n'] = len(rows)
    statics = 0
    for i, r in enumerate(rows):
        name, target = _s(r.get('name')), _s(r.get('target')).replace(';', ',')
        if _yes(r.get('dynamic')):
            kind = 'd' if target in want else ('h' if '/32' not in target else '')
        elif name.startswith(OWN_QUEUE_PREFIXES):
            kind = 't' if target in want else ''
        elif statics < MAX_STATIC and not _yes(r.get('disabled')):
            kind, statics = 's', statics + 1
        else:
            kind = ''
        if kind:
            raw['queues'].append(_q(kind, i, name, target, r.get('max-limit'), r.get('parent')))
    for r in (svc.safe_get('/ip/hotspot/user/profile') or [])[:MAX_PROFILES]:
        raw['profiles'].append({'name': _s(r.get('name')), 'rate': _s(r.get('rate-limit')),
                                'parent': '' if _s(r.get('parent-queue')) in ('', 'none') else _s(r.get('parent-queue')),
                                'shared': _s(r.get('shared-users')), 'insert': _s(r.get('insert-queue-before'))})
    raw['servers'] = [{'name': _s(r.get('name')), 'rate': _s(r.get('rate-limit'))} for r in svc.safe_get('/ip/hotspot/profile') or []]
    filt = svc.safe_get('/ip/firewall/filter') or []
    raw['ft'] = sum(1 for r in filt if _s(r.get('action')) == 'fasttrack-connection' and not _yes(r.get('disabled')))
    raw['fg'] = sum(1 for r in filt if _s(r.get('comment')) == GUARD)
    ifaces = {_s(h.get('interface')) for h in svc.safe_get('/ip/hotspot') or [] if not _yes(h.get('disabled'))}
    for a in svc.safe_get('/ip/address') or []:
        if _s(a.get('interface')) in ifaces and '/' in _s(a.get('address')):
            net = f'{_s(a.get("network"))}/{_s(a.get("address")).split("/")[1]}'
            if NET_RE.match(net):
                raw['nets'].append(net)
    return raw


def api_fix(svc, p):
    profiles = svc.resource('/ip/hotspot/user/profile')
    rows = {_s(r.get('name')): r for r in profiles.get() or []}
    changed, unparented, errors, guarded = [], [], [], False
    for name, rate in p.get('rates') or []:
        r = rows.get(name)
        try:
            if not r:
                raise ValueError('missing')
            profiles.set(id=r['id'], rate_limit=rate)
            changed.append(name)
        except Exception:
            errors.append(name)
    for name in p.get('unparent') or []:
        r = rows.get(name)
        try:
            if not r:
                raise ValueError('missing')
            try:
                profiles.call('unset', {'numbers': r['id'], 'value-name': 'parent-queue'})
            except Exception:
                profiles.set(id=r['id'], parent_queue='none')
            unparented.append(name)
        except Exception:
            errors.append(name)
    if p.get('guard'):
        try:
            lists = svc.resource('/ip/firewall/address-list')
            have = {_s(r.get('address')) for r in lists.get() if _s(r.get('list')) == NETLIST}
            for net in p.get('nets') or []:
                if net not in have:
                    lists.add(list=NETLIST, address=net, comment='TapTap hotspot network')
            flt = svc.resource('/ip/firewall/filter')
            rules = flt.get() or []
            ft = next((r for r in rules if _s(r.get('action')) == 'fasttrack-connection' and not _yes(r.get('disabled'))), None)
            if ft and not any(_s(r.get('comment')) == GUARD for r in rules):
                for direction in ('src_address_list', 'dst_address_list'):
                    flt.add(chain='forward', action='accept', connection_state='established,related', comment=GUARD,
                            place_before=ft['id'], **{direction: NETLIST})
                guarded = True
        except Exception:
            errors.append('fasttrack')
    raw = api_read(svc, p)
    raw.update(changed=changed, unparented=unparented, errors=errors, guarded=guarded)
    return raw


# ─────────────────────────────── what it means ───────────────────────────────

def _limit(text):
    """'2M/4M' (simple queue: target upload / target download) → (up, down) bps; '0/0' or '' → (0, 0)."""
    from .conn_lab import pair
    return pair(text) if '/' in _s(text) else (0, 0)


def _covers(target, ip):
    """Does a queue target take this device's traffic? 'yes' / 'iface' (an interface: maybe everyone) / ''."""
    addr = ipaddress.ip_address(ip)
    hit = ''
    for part in [x.strip() for x in _s(target).split(',') if x.strip()]:
        try:
            if addr in ipaddress.ip_network(part, strict=False):
                return 'yes'
        except ValueError:
            hit = 'iface'                       # an interface name: catches whoever is on it
    return hit


def _targets(target):
    return len([x for x in _s(target).split(',') if x.strip()])


def analyze(router, raw, sessions, business=None, now=None):
    """Raw router picture + who is online → per voucher / per device verdict, router findings and the fix to offer.

    ``sessions``: [(code, session dict with ip/mac/down_bps…)]."""
    from .traffic_idle import _labels, _vouchers
    business = business or router.business
    plans = plans_map(business)
    vouchers = _vouchers(business, list({c for c, _s2 in sessions}))
    profiles = {p['name']: p for p in raw.get('profiles') or []}
    queues = sorted(raw.get('queues') or [], key=lambda q: q['i'])
    statics = {q['name']: q for q in queues if q['kind'] == 's'}
    labels = _labels(business, [s.get('mac') for _c, s in sessions])
    ft_open = raw.get('ft', 0) > 0 and raw.get('fg', 0) == 0
    per_voucher, rates, unparent, problems = {}, {}, set(), 0

    for code, s in sessions:
        ip = s.get('ip')
        v = vouchers.get(code.upper())
        rate, up, down, profile = speed_of(v, plans) if v is not None else ('', 0, 0, '')
        prof = profiles.get(profile) or {}
        first = None
        for q in queues:
            hit = _covers(q['target'], ip) if q['kind'] != 'd' else ('yes' if q['target'] == f'{ip}/32' else '')
            if hit:
                first = (q, hit)
                break
        dev = {'ip': ip, 'mac': s.get('mac', ''), 'down_bps': s.get('down_bps', 0), 'up_bps': s.get('up_bps', 0),
               'label': (labels.get((s.get('mac') or '').upper()) or (None, ''))[1], 'queue': '', 'limit': '', 'state': '', 'text': ''}
        if first is None:
            state = 'none' if down else 'free'
            dev['text'] = ('No queue carries it: it gets the whole line, not its plan speed.' if down else
                           'Its plan has no speed limit — it gets whatever the line gives.')
        else:
            q, hit = first
            qu, qd = _limit(q['max'])
            dev.update(queue=q['name'], limit=f'{human(qd)} down · {human(qu)} up' if qd else 'no limit')
            if q['kind'] == 't':
                state = 'taptap'
                dev['text'] = (('Its own TapTap lane' if q['name'].startswith('TTSPD-') else 'Its own fair-usage cap') +
                               f' — {human(qd)}, for this device only.')
            elif q['kind'] == 'd':
                parent = statics.get(q['parent']) if q['parent'] else None
                pd = _limit(parent['max'])[1] if parent else 0
                online_n = sum(1 for c, _x in sessions if c.upper() == code.upper())
                if parent and pd and down and pd < down * max(1, online_n):
                    state = 'parent'
                    dev['text'] = (f'It has its own queue, but under the parent queue “{parent["name"]}” ({human(pd)}): every customer of '
                                   f'profile “{profile or "?"}” shares that.')
                    if profile and prof.get('parent'):
                        unparent.add(profile)
                elif down and qd and abs(qd - down) > down * 0.02:
                    state = 'old'
                    dev['text'] = (f'Its own queue, but at {human(qd)} instead of the plan’s {human(down)}: the speed changed while it was '
                                   'online, or the router profile has another speed.')
                elif down and not qd:
                    state = 'none'
                    dev['text'] = 'Its own queue has no limit — it gets the whole line.'
                else:
                    state = 'own'
                    dev['text'] = f'Its own lane at {human(qd)} — not shared with any other device.'
            else:
                n = _targets(q['target'])
                state = 'shared'
                what = ('the hotspot server’s queue' if q['kind'] == 'h' else
                        f'the queue “{q["name"]}”' + (' on a whole interface' if hit == 'iface' else
                                                      f' ({n} addresses)' if n > 1 else ' (a whole network)'))
                dev['text'] = (f'It shares {what}' + (f' ({human(qd)})' if qd else ' (no limit at all)') +
                               ' with every other device that queue catches — not its own lane.')
            if ft_open and state in ('own', 'old', 'parent', 'taptap', 'shared'):
                dev['fasttrack'] = True
        if v is not None and down and prof and profile:
            from .conn_lab import parse_rate
            if parse_rate(_s(prof.get('rate'))) != (up, down):
                rates[profile] = rate
        dev['state'] = state
        if state in ('none', 'shared', 'old', 'parent'):
            problems += 1
        row = per_voucher.setdefault(code.upper(), {
            'code': v.code if v else code, 'id': v.pk if v else None, 'member': bool(getattr(v, 'is_member', False)) if v else False,
            'plan': v.plan_name if v else '', 'devices_allowed': (v.max_devices or 1) if v else 1, 'rate': rate,
            'profile': profile, 'math': math(rate, (v.max_devices or 1) if v else 1), 'devices': []})
        row['devices'].append(dev)

    for row in per_voucher.values():
        states = [d['state'] for d in row['devices']]
        bad = [s for s in states if s in ('none', 'shared', 'old', 'parent')]
        row['ok'] = not bad
        n = len(states)
        if not bad:
            row['verdict'] = (f'Each of its {n} devices online has its own {row["math"]["each"]}.' if n > 1 else
                              f'Its device has its own {row["math"]["each"]}.') if row['rate'] else 'No speed limit on its plan.'
        else:
            row['verdict'] = f'{len(bad)} of its {n} device{"s" if n != 1 else ""} {"do" if len(bad) != 1 else "does"} not have its own lane at the plan speed.'

    findings = []
    nets = raw.get('nets') or []
    if ft_open:
        findings.append({'level': 'bad', 'key': 'fasttrack', 'title': 'FastTrack skips every speed limit',
                         'text': 'This router fast-tracks traffic, and fast-tracked traffic passes no queue at all — not the hotspot’s own lanes, '
                                 'not TapTap’s. Devices then get more than their plan. ' +
                                 ('The fix keeps hotspot customers out of FastTrack (the router works a little harder).' if nets else
                                  'TapTap could not find the hotspot’s network on this router (no address on the hotspot interface), so it cannot '
                                  'add the guard itself: in IP › Firewall › Filter, add an accept rule for established,related traffic of your '
                                  'customers’ network just above the fasttrack rule.'),
                         'fix': 'guard' if nets else ''})
    for name, rate in rates.items():
        have = _s((profiles.get(name) or {}).get('rate')) or 'no speed'
        findings.append({'level': 'warn', 'key': 'rate', 'title': f'Profile “{name}” has {have}, TapTap says {rate}',
                         'text': 'Every device that signs in with this profile gets the router’s value. The fix writes TapTap’s speed.', 'fix': 'rates'})
    for name in sorted(unparent):
        findings.append({'level': 'bad', 'key': 'parent', 'title': f'Profile “{name}” puts every device under one parent queue',
                         'text': f'All its customers share the parent “{profiles[name]["parent"]}”. The fix removes the parent from this TapTap '
                                 'profile, so each device keeps its own lane (applies at their next sign-in; lanes cover devices online now).',
                         'fix': 'unparent'})
    used = {r['profile'] for r in per_voucher.values() if r['profile']}
    no_rate = sorted(n for n in used if not _s((profiles.get(n) or {}).get('rate')) and n not in rates)
    limited_servers = [sv for sv in raw.get('servers') or [] if _s(sv.get('rate'))]
    if no_rate and limited_servers:
        findings.append({'level': 'warn', 'key': 'server', 'title': 'Devices without a speed fall into the hotspot’s shared queue',
                         'text': f'Profile{"s" if len(no_rate) > 1 else ""} {", ".join(no_rate)} {"have" if len(no_rate) > 1 else "has"} no speed, '
                                 f'so {"their" if len(no_rate) > 1 else "its"} devices share the server limit '
                                 f'({", ".join(sv["rate"] for sv in limited_servers)}) with everybody on the hotspot.'})
    broad = [q for q in queues if q['kind'] == 's' and q['max'] not in ('', '0/0') and
             (_targets(q['target']) > 1 or not all('/32' in x for x in q['target'].split(',') if x))]
    first_lane = next((q['i'] for q in queues if q['kind'] == 'd'), None)
    above = [q for q in broad if first_lane is None or q['i'] < first_lane]
    if above and any(d['state'] == 'shared' for r in per_voucher.values() for d in r['devices']):
        findings.append({'level': 'warn', 'key': 'broad', 'title': f'Hand-made queue{"s" if len(above) > 1 else ""} above the per-device lanes',
                         'text': ', '.join(f'“{q["name"]}” ({q["max"]})' for q in above[:4]) +
                                 ' catch devices before their own lane. TapTap does not delete queues you made: lanes put each device above them.'})
    vs = list(per_voucher.values())
    devices = [d for r in vs for d in r['devices']]
    count = lambda st: sum(1 for d in devices if d['state'] == st)
    summary = {'vouchers': len(vs), 'devices': len(devices), 'own': count('own') + count('taptap'), 'shared': count('shared'),
               'none': count('none'), 'old': count('old'), 'parent': count('parent'), 'free': count('free'), 'problems': problems}
    if not devices:
        title, level = 'Nobody to check right now', 'idle'
    elif not problems and not ft_open:
        title, level = (f'Every device has its own lane — {summary["own"]} of {summary["devices"]}', 'ok')
    elif not problems:
        title, level = 'Every device has its own lane, but FastTrack skips them', 'bad'
    else:
        title, level = f'{problems} of {summary["devices"]} devices do not get their own plan speed', 'bad' if summary['shared'] + summary['none'] else 'warn'
    fix = {'rates': [[n, r] for n, r in rates.items()][:30], 'unparent': sorted(unparent)[:30], 'guard': ft_open and bool(nets),
           'nets': nets[:10], 'lanes': [r['id'] for r in vs if not r['ok'] and r['id']][:200]}
    vs.sort(key=lambda r: (r['ok'], r['code']))
    return {'title': title, 'level': level, 'summary': summary, 'findings': findings, 'vouchers': vs[:200], 'fix': fix,
            'queues_read': raw.get('n', 0), 'fasttrack': {'rules': raw.get('ft', 0), 'guarded': raw.get('fg', 0) > 0 or raw.get('guarded')},
            'router_said': {'changed': raw.get('changed') or [], 'unparented': raw.get('unparented') or [], 'errors': raw.get('errors') or []}}


def remember(router, result):
    cache.set(f'tt:lanes:{router.pk}', {'at': timezone.now().isoformat(), 'result': result}, 6 * 3600)


def last(router):
    return cache.get(f'tt:lanes:{router.pk}')


def finish(router, kind, p, raw):
    """Router answer → the page's result (and the fix summary for 'lanefix')."""
    rows, _age = online(router, codes=p.get('codes') or None, shared_only=not p.get('codes'))
    asked = set(p.get('ips') or [])
    rows = [(c, s) for c, s in rows if s.get('ip') in asked]      # a device that came online since was not looked up
    res = analyze(router, raw, rows)
    if kind == 'lanefix':
        said = res['router_said']
        bits = ([f'speed written on {", ".join(said["changed"])}'] if said['changed'] else []) + \
               ([f'parent queue removed from {", ".join(said["unparented"])}'] if said['unparented'] else []) + \
               (['hotspot customers kept out of FastTrack'] if raw.get('guarded') else [])
        res['fixed'] = bits
        if said['errors']:
            res['failed_step'] = True
            res['fix_title'] = 'The router refused part of the fix'
            res['fix_note'] = 'Refused: ' + ', '.join(said['errors']) + '. Run a sync, then check again.'
        else:
            res['fix_title'] = 'Done — ' + ('; '.join(bits) if bits else 'nothing on the router needed changing')
            res['fix_note'] = ('Profiles apply to every device at its next sign-in. Devices online now are covered by their own TapTap lanes '
                               'within a few seconds (Direct API) or at the router’s next check-in (TapTap Link).')
    remember(router, res)
    return res


def validate(kind, p):
    """Checked again by core/agent.queue before a lanes command can reach a router."""
    if not isinstance(p.get('ips') or [], list) or len(p.get('ips') or []) > MAX_IPS or \
            not all(IP_RE.match(_s(i)) and _local(_s(i)) for i in p.get('ips') or []):
        raise ValueError('Invalid devices.')
    if not isinstance(p.get('codes') or [], list) or not all(CODE_RE.match(_s(c)) for c in p.get('codes') or []):
        raise ValueError('Invalid voucher.')
    if kind == 'lanes':
        return
    rates = p.get('rates') or []
    if not isinstance(rates, list) or len(rates) > 30 or not all(
            isinstance(r, list) and len(r) == 2 and PROFILE_RE.match(_s(r[0])) and RATE_RE.match(_s(r[1])) for r in rates):
        raise ValueError('Invalid profile speed.')
    if not all(PROFILE_RE.match(_s(n)) for n in p.get('unparent') or []) or len(p.get('unparent') or []) > 30:
        raise ValueError('Invalid profile.')
    if not all(NET_RE.match(_s(n)) for n in p.get('nets') or []) or len(p.get('nets') or []) > 10:
        raise ValueError('Invalid network.')


# ─────────────────────────────── lanes: TapTap's own per-device queues ───────────────────────────────

def ready():
    from .line_steer import pins_ready
    if not pins_ready():
        return False
    try:
        from django.db import connection
        from .models_flow import DeviceLane
        key = 'tt:lanes:table'
        hit = cache.get(key)
        if hit is None:
            hit = DeviceLane._meta.db_table in connection.introspection.table_names()
            cache.set(key, hit, 30 if not hit else 3600)
        return bool(hit)
    except Exception:
        return False


def active(business, router=None, now=None):
    """The lanes in force: (shared_policy or None, {voucher id: lane})."""
    if not ready():
        return None, {}
    from django.db.models import Q
    from .models_flow import DeviceLane
    now = now or timezone.now()
    qs = DeviceLane.objects.filter(business=business, removed_at__isnull=True).filter(Q(until__isnull=True) | Q(until__gt=now))
    if router is not None:
        qs = qs.filter(Q(router__isnull=True) | Q(router=router))
    shared, per = None, {}
    for lane in qs.select_related('voucher'):
        if lane.scope == 'shared':
            shared = shared or lane
        elif lane.voucher_id:
            per[lane.voucher_id] = lane
    return shared, per


def lane_caps(router, rows, vouchers):
    """For core/traffic_speed.desired_caps: {ip: (up_kbps, down_kbps)} for devices that must get their own lane.

    rows: [(user, ip)] of sessions no speed rule picked; vouchers: {CODE: Voucher}. Cheap when nothing is laned."""
    key = f'tt:lanes:any:{router.business_id}'
    if cache.get(key) is False:
        return {}
    shared, per = active(router.business, router)
    if shared is None and not per:
        cache.set(key, False, 60)
        return {}
    plans = plans_map(router.business)
    out = {}
    for user, ip in rows:
        v = vouchers.get(user.upper())
        if v is None:
            continue
        if v.pk not in per and not (shared is not None and (v.max_devices or 1) > 1):
            continue
        _rate, up, down, _p = speed_of(v, plans)
        if down:
            out[ip] = (max(32, round(up / 1000)) if up else max(32, round(down / 1000)), max(32, round(down / 1000)))
    return out


def forget_cache(business):
    cache.delete(f'tt:lanes:any:{business.pk}')


def add_lanes(business, vouchers, user, router=None, reason=''):
    """One lane per voucher, until its time ends."""
    from .models_flow import DeviceLane
    from .traffic_idle import _ends
    now = timezone.now()
    made = 0
    for v in vouchers:
        if DeviceLane.objects.filter(business=business, voucher=v, removed_at__isnull=True).exists():
            continue
        end, _left = _ends(v, now)
        DeviceLane.objects.create(business=business, router=router, scope='voucher', voucher=v, reason=reason[:160],
                                  until=end, created_by=user)
        made += 1
    forget_cache(business)
    return made


def set_shared(business, on, user):
    from .models_flow import DeviceLane
    now = timezone.now()
    cur = DeviceLane.objects.filter(business=business, scope='shared', removed_at__isnull=True)
    if on and not cur.exists():
        DeviceLane.objects.create(business=business, scope='shared', reason='Always: every device of a shared voucher', created_by=user)
    elif not on:
        cur.update(removed_at=now, removed_by=user)
    forget_cache(business)


def stop(business, lane_id, user):
    from .models_flow import DeviceLane
    n = DeviceLane.objects.filter(business=business, pk=lane_id, removed_at__isnull=True).update(removed_at=timezone.now(), removed_by=user)
    forget_cache(business)
    return n


def apply_now(business, router=None):
    """Put new lanes on the routers now instead of waiting for the next live-sync pass."""
    try:
        from .traffic_speed import apply_business
        return apply_business(business, force=True)
    except Exception:
        return []
