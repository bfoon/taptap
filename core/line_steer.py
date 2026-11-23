"""Which internet line a customer uses on a load-balanced router — and moving them to another one.

"Share the load" (PCC) spreads customers over the lines with mangle rules: every new connection is given a
connection mark (``TT-ether1`` …) by a ``per-connection-classifier`` rule, and the mark chooses the routing table.

* **Seeing it** costs nothing: the connection sample TapTap already takes once a minute for the Traffic page carries
  each connection's mark, so TapTap knows which line every customer is on (``note_marks`` → ``where``). "Check now"
  counts the customer's connections on the router at that moment.
* **Switching** adds ONE mangle rule above the sharing rules: a copy of the chosen line's own sharing rule with the
  classifier replaced by this device (its MAC, or its IP when the MAC is unknown). The device's open connections are
  then cleared so it moves at once instead of connection by connection. The rule's comment starts with
  ``TapTap line-pin <key>``; "Back to automatic" removes it.

Nothing here touches routes, tables, NAT or the sharing rules themselves, so a switch can never take a line down.
Only load sharing with marks can be steered: with a backup line (failover) everyone is on the main line by design.
"""
from __future__ import annotations

import ipaddress
import re

from django.core.cache import cache
from django.utils import timezone

PIN_TAG = 'TapTap line-pin'
MARK_RE = re.compile(r'^[\w.@:+-]{1,60}$')
MAC_RE = re.compile(r'^[0-9A-F]{2}(:[0-9A-F]{2}){5}$')
# What may be copied from a sharing rule into a pin rule (matchers and the action; never counters or ids).
COPY = ('chain', 'action', 'new-connection-mark', 'new-routing-mark', 'passthrough', 'connection-mark', 'connection-state',
        'in-interface', 'in-interface-list', 'dst-address-type', 'dst-address-list', 'src-address-list', 'dst-address',
        'protocol', 'routing-mark', 'connection-type', 'hotspot')
FLUSH_MAX = 3000
MIGRATE_HINT = ('This TapTap server has the new code but has not run its database step yet, so customers cannot be switched '
                'to another line. On the server run:  docker compose run --rm web python manage.py migrate core')
_ready = {'ok': False, 'at': 0.0}


def pins_ready():
    """True when the LinePin table exists (migration 0122_flow_lab). Everything that only *looks* works without it."""
    import time
    if _ready['ok']:
        return True
    now = time.monotonic()
    if now - _ready['at'] < 30:
        return False
    _ready['at'] = now
    try:
        from django.db import connection
        from .models_flow import LinePin
        _ready['ok'] = LinePin._meta.db_table in connection.introspection.table_names()
    except Exception:
        _ready['ok'] = False
    return _ready['ok']


def _s(v):
    return str(v if v is not None else '').strip()


def _yes(v):
    return _s(v).lower() in ('true', 'yes')


def clean_ip(v):
    ip = ipaddress.ip_address(_s(v))
    if ip.version != 4:
        raise ValueError('Only IPv4 devices can be moved.')
    return str(ip)


def clean_mac(v):
    m = _s(v).upper().replace('-', ':')
    return m if MAC_RE.match(m) else ''


def pin_key(mac, ip):
    """How a pin is named on the router: the MAC (it survives a new IP), else the IP."""
    mac = clean_mac(mac)
    return mac or ('ip-' + clean_ip(ip).replace('.', '-'))


def _plain(text, n=40):
    """RouterOS comments travel inside scripts: keep them to plain characters."""
    return re.sub(r'[^A-Za-z0-9 ._+@-]', '', _s(text))[:n].strip()


def pin_comment(key, code, label):
    return f'{PIN_TAG} {key} {_plain(code, 40) or "device"} to {_plain(label) or "line"}'


# ─────────────────────────────── the lines of a router ───────────────────────────────

def mangle_rows(router):
    from .linkops import snapshot_rows
    rows, _label, at = snapshot_rows(router, '/ip/firewall/mangle')
    return rows or [], at


def _labels(router):
    """TT-<id> → the name the owner gave the line in Internet lines."""
    out = {}
    try:
        for l in (router.wan_setup.config or {}).get('links') or []:
            from .wan import safe_name
            if l.get('interface'):
                out[f'TT-{safe_name(l["interface"])}'] = _s(l.get('label')) or l['interface']
    except Exception:
        pass
    return out


def lanes(router, rows=None):
    """The lines customers are shared over, from the router's sharing rules.

    → {'lanes': [{mark, label, parts, share, fields}], 'total', 'classifier', 'sticky', 'why', 'as_of'}"""
    at = None
    if rows is None:
        rows, at = mangle_rows(router)
    names = _labels(router)
    found, total, classifier = {}, 0, ''
    for r in rows:
        pcc = _s(r.get('per-connection-classifier'))
        if not pcc or _yes(r.get('disabled')) or _s(r.get('chain')) != 'prerouting':
            continue
        if _s(r.get('comment')).startswith(PIN_TAG):
            continue
        mark = _s(r.get('new-connection-mark')) or _s(r.get('new-routing-mark'))
        if not mark or not MARK_RE.match(mark):
            continue
        classifier = classifier or pcc.split(':')[0]
        total += 1
        lane = found.get(mark)
        if lane is None:
            cm = _s(r.get('comment'))
            label = names.get(mark) or (cm.rsplit('→', 1)[1].strip() if '→' in cm else '') or mark
            lane = found[mark] = {'mark': mark, 'label': label[:60], 'parts': 0, 'observable': bool(_s(r.get('new-connection-mark'))),
                                  'fields': {k: _s(r.get(k)) for k in COPY if _s(r.get(k))}}
        lane['parts'] += 1
    out = list(found.values())
    for l in out:
        l['share'] = round(l['parts'] * 100 / total) if total else 0
    mode = 'pcc' if len(out) >= 2 else ''
    if len(out) < 2:
        marked = mark_lanes(router, rows, names)
        if len(marked) >= 2:
            out, mode, classifier = marked, 'marks', ''
    why = ''
    if len(out) < 2:
        why = ('This router does not share customers over several lines with marks (it has one line, a backup line, or '
               'combined lines), so there is no line to switch a single customer to.' if rows else
               'TapTap has not read this router\'s firewall yet — run a sync on the Routers page first.')
    return {'lanes': out, 'total': total, 'classifier': classifier,
            'sticky': mode == 'marks' or ('port' not in classifier and bool(classifier)),
            'mode': mode, 'why': why, 'as_of': at.isoformat() if at else None}


def _strategy(router):
    try:
        return (router.wan_setup.config or {}).get('strategy') or ''
    except Exception:
        return ''


def mark_lanes(router, rows, names=None):
    """Lines of a TapTap "Backup line" or "Combine lines" setup: no sharing rules, but every line has its own mark and
    routing table ("route via <line>"). A customer is moved by giving their new connections that line's mark — the
    table then carries them, with the other lines as fallbacks (core/wan.py). "Split by network" is left out: its
    routing rules choose the line per network before any mark is looked at."""
    strategy = _strategy(router)
    if strategy == 'split':
        return []
    names = names if names is not None else _labels(router)
    found = {}
    for r in rows or []:
        if _s(r.get('chain')) != 'prerouting' or _s(r.get('action')) != 'mark-routing' or _yes(r.get('disabled')):
            continue
        mark, cm = _s(r.get('connection-mark')), _s(r.get('comment'))
        if not mark.startswith('TT-') or not MARK_RE.match(mark) or not cm.startswith('TapTap WAN') or mark in found:
            continue
        label = names.get(mark) or (cm.rsplit('route via', 1)[1].strip() if 'route via' in cm else '') or mark
        found[mark] = {'mark': mark, 'label': label[:60], 'parts': 1, 'observable': True,
                       'fields': {'chain': 'prerouting', 'action': 'mark-connection', 'new-connection-mark': mark, 'passthrough': 'yes',
                                  'connection-mark': 'no-mark', 'in-interface-list': '!TT-WAN', 'dst-address-type': '!local'}}
    out = list(found.values())
    order = [f'TT-{_safe(l.get("interface"))}' for l in _cfg_links(router)]
    out.sort(key=lambda l: order.index(l['mark']) if l['mark'] in order else 99)
    for i, l in enumerate(out):
        l['share'] = (100 if i == 0 else 0) if strategy in ('failover', 'single', '') else round(100 / len(out))
    return out


def _safe(iface):
    from .wan import safe_name
    return safe_name(iface)


def _cfg_links(router):
    try:
        return [l for l in (router.wan_setup.config or {}).get('links') or [] if l.get('interface') and l.get('enabled', True)]
    except Exception:
        return []


def is_anchor(r):
    """The rule a switch goes above: the first sharing (PCC) rule, or — without sharing — TapTap's first "route via"
    rule. Both sit below "leave local traffic alone", so a switch never catches traffic between customers."""
    if _s(r.get('chain')) != 'prerouting' or _yes(r.get('disabled')) or _s(r.get('comment')).startswith(PIN_TAG):
        return False
    if _s(r.get('per-connection-classifier')):
        return True
    return _s(r.get('action')) == 'mark-routing' and _s(r.get('connection-mark')).startswith('TT-')


def lane_by_mark(info, mark):
    return next((l for l in info['lanes'] if l['mark'] == mark), None)


def next_lane(info, current):
    """The line after the current one (so one click on a 50/50 pair goes to the other line)."""
    ls = info['lanes']
    if len(ls) < 2:
        return None
    idx = next((i for i, l in enumerate(ls) if l['mark'] == current), -1)
    return ls[(idx + 1) % len(ls)]


# ─────────────────────────────── seeing it (free: from the traffic sample) ───────────────────────────────

def note_marks(router, by_ip, now=None):
    """Called by core.traffic.ingest_connections with {client ip: {mark: connections}} once a minute."""
    data = {ip: dict(m) for ip, m in by_ip.items() if m}
    cache.set(f'tt:tr:lane:{router.pk}', {'at': (now or timezone.now()).isoformat(), 'ips': data}, 900)


def seen(router):
    return cache.get(f'tt:tr:lane:{router.pk}') or {}


def describe(info, counts, total=None, pin_mark=''):
    """Counts per mark → where this customer's traffic goes, in the page's shape."""
    known = {l['mark'] for l in info['lanes']}
    hits = {m: n for m, n in (counts or {}).items() if m in known and n}
    n = sum(hits.values())
    rows = [{'mark': l['mark'], 'label': l['label'], 'share': l['share'], 'count': hits.get(l['mark'], 0),
             'pct': round(hits.get(l['mark'], 0) * 100 / n) if n else 0} for l in info['lanes']]
    top = max(rows, key=lambda r: r['count'], default=None)
    if not n:
        state, lane = 'none', ''
    elif top['pct'] >= 85:
        state, lane = 'one', top['mark']
    else:
        state, lane = 'mixed', top['mark']
    for r in rows:
        r['current'] = bool(n) and r['mark'] == lane
        r['pinned'] = r['mark'] == pin_mark
    return {'state': state, 'lane': lane, 'label': next((r['label'] for r in rows if r['mark'] == lane), ''),
            'lanes': rows, 'connections': n, 'all': total if total is not None else n, 'pin': pin_mark}


def where(router, ip, info=None):
    """Passive answer for one device ({} when the traffic sample has not seen it)."""
    info = info or lanes(router)
    if len(info['lanes']) < 2:
        return {}
    snap = seen(router)
    counts = (snap.get('ips') or {}).get(_s(ip))
    if not counts:
        return {}
    pin = active_pin(router, ip=ip)
    out = describe(info, counts, pin_mark=pin.mark if pin else '')
    out['at'] = snap.get('at')
    return out


def spread(router, sessions, info=None):
    """How the customers online now are spread over the lines: per line → customers, speed, share planned / real."""
    info = info or lanes(router)
    ips = (seen(router).get('ips') or {})
    rows = {l['mark']: {'mark': l['mark'], 'label': l['label'], 'share': l['share'], 'customers': 0, 'down_bps': 0, 'up_bps': 0, 'people': []}
            for l in info['lanes']}
    unknown = 0
    for s in sessions:
        d = describe(info, ips.get(s.get('ip'), {}))
        if d['state'] == 'none':
            unknown += 1
            continue
        row = rows[d['lane']]
        row['customers'] += 1
        row['down_bps'] += s.get('down_bps', 0)
        row['up_bps'] += s.get('up_bps', 0)
        row['people'].append({**s, 'mixed': d['state'] == 'mixed'})
    placed = sum(r['customers'] for r in rows.values())
    for r in rows.values():
        r['real'] = round(r['customers'] * 100 / placed) if placed else 0
        r['people'].sort(key=lambda x: -(x.get('down_bps', 0) + x.get('up_bps', 0)))
        r['people'] = r['people'][:40]
    return {'lines': list(rows.values()), 'placed': placed, 'unknown': unknown}


# ─────────────────────────────── TapTap's record of pins ───────────────────────────────

def active_pin(router, mac='', ip='', code=''):
    if not pins_ready():
        return None
    from .models_flow import LinePin
    qs = LinePin.objects.filter(router=router, removed_at__isnull=True)
    mac = clean_mac(mac)
    if mac:
        hit = qs.filter(key=mac).first()
        if hit:
            return hit
    if ip:
        hit = qs.filter(ip=_s(ip)).first() or qs.filter(key='ip-' + _s(ip).replace('.', '-')).first()
        if hit:
            return hit
    return qs.filter(code__iexact=code).first() if code else None


def record_pin(router, key, mac, ip, code, lane, from_label, user):
    if not pins_ready():
        raise ValueError(MIGRATE_HINT)
    from .models_flow import LinePin
    now = timezone.now()
    LinePin.objects.filter(router=router, key=key, removed_at__isnull=True).update(removed_at=now, removed_by=user)
    return LinePin.objects.create(business=router.business, router=router, key=key, mac=clean_mac(mac), ip=_s(ip), code=_s(code)[:120],
                                  mark=lane['mark'], label=lane['label'][:80], from_label=_s(from_label)[:80], created_by=user)


def release_pin(router, key, user=None):
    if not pins_ready():
        return 0
    from .models_flow import LinePin
    return LinePin.objects.filter(router=router, key=key, removed_at__isnull=True).update(removed_at=timezone.now(), removed_by=user)


def pins_of(router):
    """Switches in force on a router ([] before the database step has run)."""
    if not pins_ready():
        return []
    return list(router.line_pins.filter(removed_at__isnull=True).select_related('created_by')[:200])


def pin_fields(lane, key, mac, ip, code):
    """The mangle rule that keeps one device on one line: the line's own sharing rule, for this device only."""
    f = dict(lane['fields'])
    mac = clean_mac(mac)
    if mac:
        f['src-mac-address'] = mac
    else:
        f['src-address'] = clean_ip(ip)
    f['comment'] = pin_comment(key, code, lane['label'])
    return f


# ─────────────────────────────── Direct API / TapTap Tunnel ───────────────────────────────

def _api_fields(fields):
    return {str(k).replace('-', '_'): v for k, v in fields.items()}


def _conn_rows(svc, props):
    res = svc.resource('/ip/firewall/connection')
    try:
        rows = res.call('print', {'.proplist': props})
        if rows is not None:
            return rows
    except Exception:
        pass
    return res.get() or []


def _src_ip(row):
    v = _s(row.get('src-address'))
    return v.rsplit(':', 1)[0] if ':' in v else v


def api_look(svc, p):
    counts, total = {}, 0
    for row in _conn_rows(svc, 'src-address,connection-mark'):
        if _src_ip(row) != p['ip']:
            continue
        total += 1
        m = _s(row.get('connection-mark'))
        if m:
            counts[m] = counts.get(m, 0) + 1
    pin = ''
    try:
        head = f'{PIN_TAG} {p["key"]} '
        pin = next((_s(r.get('comment')) for r in svc.resource('/ip/firewall/mangle').get() if _s(r.get('comment')).startswith(head)), '')
    except Exception:
        pass
    return {'counts': counts, 'total': total, 'pin': pin}


def _remove_pins(mangle, rows, key):
    head = f'{PIN_TAG} {key} '
    n = 0
    for r in rows:
        if _s(r.get('comment')).startswith(head):
            mangle.remove(id=r['id'])
            n += 1
    return n


def _flush(svc, ip):
    """Close the device's open connections so it takes the new line at once (apps reconnect by themselves)."""
    res = svc.resource('/ip/firewall/connection')
    n = 0
    for row in _conn_rows(svc, '.id,src-address'):
        if _src_ip(row) == ip and n < FLUSH_MAX:
            try:
                res.remove(id=row.get('id') or row.get('.id'))
                n += 1
            except Exception:
                pass
    return n


def api_pin(svc, p):
    import time
    mangle = svc.resource('/ip/firewall/mangle')
    rows = mangle.get()
    anchor = next((r for r in rows if is_anchor(r)), None)
    if not anchor:
        return {'error': 'nopcc', **api_look(svc, p)}
    _remove_pins(mangle, rows, p['key'])
    mangle.add(**_api_fields(p['fields']), place_before=anchor['id'])
    moved = _flush(svc, p['ip'])
    time.sleep(3)
    return {'done': True, 'moved': moved, **api_look(svc, p)}


def api_unpin(svc, p):
    import time
    mangle = svc.resource('/ip/firewall/mangle')
    removed = _remove_pins(mangle, mangle.get(), p['key'])
    moved = _flush(svc, p['ip']) if removed else 0
    if removed:
        time.sleep(2)
    return {'done': True, 'removed': removed, 'moved': moved, **api_look(svc, p)}


# ─────────────────────────────── TapTap Link (router-side script) ───────────────────────────────

def _look_snippet(p):
    marks = [m for m in p.get('marks') or [] if MARK_RE.match(m)][:6]
    ip = p['ip']
    cnt = ''.join(f':local c{i} 0; ' for i in range(len(marks)))
    tests = ''.join(f':if ($k = "{m}") do={{ :set c{i} ($c{i} + 1) }}; ' for i, m in enumerate(marks))
    rep = ''.join(f':set out ($out . "lm={m}|" . $c{i} . "\\n"); ' for i, m in enumerate(marks))
    return (':local ct 0; ' + cnt +
            f':foreach c in=[/ip firewall connection find where src-address~"^{ip}:"] do={{ :do {{ '
            ':local k [:tostr [/ip firewall connection get $c connection-mark]]; :set ct ($ct + 1); ' + tests + '} on-error={} }; '
            ':set out ($out . "lt=" . $ct . "\\n"); ' + rep +
            f':do {{ :set out ($out . "pin=" . [/ip firewall mangle get ([/ip firewall mangle find where comment~"^{PIN_TAG} {p["key"]} "]->0) comment] . "\\n") }} on-error={{}}; ')


def _flush_snippet(ip):
    return (f':foreach c in=[/ip firewall connection find where src-address~"^{ip}:"] do={{ :do {{ /ip firewall connection remove $c; '
            ':set mv ($mv + 1) } on-error={} }; ')


# RouterOS test for is_anchor() inside a loop over /ip firewall mangle (variable $r); sets $an / $hv.
ANCHOR_TEST = (':if (([:len [:tostr [/ip firewall mangle get $r per-connection-classifier]]] > 0) || '
               '(([:tostr [/ip firewall mangle get $r action]] = "mark-routing") && '
               '([:tostr [/ip firewall mangle get $r connection-mark]] ~ "^TT-"))) do={ '
               ':if ([:tostr [/ip firewall mangle get $r comment]] ~ "^' + PIN_TAG + '") do={} else={ :set an $r; :set hv 1 } } ')


def link_body(kind, p):
    """RouterOS for 'lane' (look), 'lanepin' (switch) and 'laneoff' (back to automatic). Appends lines to $out."""
    from .wan import kv
    look = _look_snippet(p)
    if kind == 'lane':
        return look
    rm = f'/ip firewall mangle remove [find where comment~"^{PIN_TAG} {p["key"]} "]; '
    if kind == 'laneoff':
        return (':local mv 0; :do { :local n [:len [/ip firewall mangle find where comment~"^' + f'{PIN_TAG} {p["key"]} ' + '"]]; ' + rm +
                ':if ($n > 0) do={ ' + _flush_snippet(p['ip']) + ':delay 2s }; '
                ':set out ($out . "ok=1\\n" . "rm=" . $n . "\\n" . "mv=" . $mv . "\\n") } on-error={ :set out ($out . "err=remove\\n") }; ' + look)
    # the first sharing rule: the pin goes just above it (below "leave local traffic alone")
    return (':local an ""; :local hv 0; :local mv 0; '
            ':foreach r in=[/ip firewall mangle find where chain=prerouting disabled=no] do={ :if ($hv = 0) do={ :do { '
            + ANCHOR_TEST +
            '} on-error={} } }; '
            ':if ($hv = 0) do={ :set out ($out . "err=nopcc\\n") } else={ :do { ' + rm +
            f'/ip firewall mangle add {kv(p["fields"])} place-before=$an; ' + _flush_snippet(p['ip']) +
            ':set out ($out . "ok=1\\n" . "mv=" . $mv . "\\n"); :delay 3s } on-error={ :set out ($out . "err=add\\n") } }; ' + look)


def parse_link(lines):
    get = lambda k: next((v for kk, v in lines if kk == k), '')
    counts = {}
    for k, v in lines:
        if k == 'lm':
            m, _, n = v.partition('|')
            try:
                counts[m] = int(n)
            except ValueError:
                pass
    num = lambda k: int(get(k)) if get(k).isdigit() else 0
    out = {'counts': {m: n for m, n in counts.items() if n}, 'total': num('lt'), 'pin': get('pin')}
    if get('err'):
        out['error'] = get('err')
    if get('ok'):
        out.update(done=True, moved=num('mv'), removed=num('rm'))
    return out


# ─────────────────────────────── the answer, in plain words ───────────────────────────────

ERRORS = {
    'nopcc': 'The router has no TapTap line rules to place the switch above (its setup changed) — open Internet lines and apply it again.',
    'add': 'The router refused the switch rule. Run a sync so TapTap reads its firewall again, then try once more.',
    'remove': 'The router could not remove the switch rule.',
}


def result(router, kind, p, raw):
    """Raw counts from the router → what the page shows (and TapTap's record brought in line with the router)."""
    info = lanes(router)
    pin_text = _s(raw.get('pin'))
    pinned = next((l['mark'] for l in info['lanes'] if pin_text.endswith(f' to {_plain(l["label"]) or "line"}')), '') if pin_text else ''
    rec = active_pin(router, mac=p.get('mac', ''), ip=p['ip'])
    if pin_text and rec and not pinned:
        pinned = rec.mark
    if rec and not pin_text and (kind != 'lanepin' or raw.get('error')):
        release_pin(router, rec.key)                # gone on the router (removed in WinBox, the setup was re-applied, or refused)
    elif rec and pin_text and not rec.confirmed:
        rec.confirmed = True
        rec.save(update_fields=['confirmed'])
    out = describe(info, raw.get('counts'), raw.get('total'), pinned)
    out.update(kind=kind, ip=p['ip'], sticky=info['sticky'], moved=raw.get('moved', 0))
    err = raw.get('error')
    if err:
        out.update(failed_step=True, title='Not switched', note=ERRORS.get(err, 'The router reported an error.'))
        return out
    want = p.get('mark', '')
    want_label = next((l['label'] for l in info['lanes'] if l['mark'] == want), want)
    if kind == 'lanepin':
        on = out['lane'] == want and out['state'] != 'none'
        out['title'] = f'Now on {want_label}' if on else f'Switched to {want_label}'
        out['note'] = (f'{out["connections"]} of its connections already go through {want_label}.' if on else
                       'The rule is in place. Its old connections were closed; new ones take the new line as the phone reconnects '
                       '(a few seconds — press Check now).') + (f' {raw.get("moved")} open connections were moved.' if raw.get('moved') else '')
    elif kind == 'laneoff':
        out['title'] = 'Back to automatic sharing'
        out['note'] = ('The router chooses its line again.' if raw.get('removed') else 'It was not pinned on the router — nothing to remove.')
    elif out['state'] == 'none':
        out['title'] = 'No traffic to place right now'
        out['note'] = (f'The device has {out["all"]} open connections but none carries a line mark yet.' if out['all'] else
                       'The device has no open connections at this moment (asleep or not browsing), so there is no line to show.')
    elif out['state'] == 'one':
        out['title'] = f'Going through {out["label"]}'
        out['note'] = f'{out["connections"]} connections, all on this line' + (' (kept here by a switch).' if pinned == out['lane'] else '.')
    else:
        out['title'] = f'Mostly {out["label"]}'
        out['note'] = ('Its connections are spread over the lines' +
                       (' — this router shares connection by connection, so a customer uses every line.' if not info['sticky'] else
                        ' — older connections are still on their previous line and move as they close.'))
    return out
