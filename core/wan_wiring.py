"""Internet line wiring — finds and repairs a line that has internet but carries no customers.

Why this exists
===============
On 22 Oct 2026 a hAP on two Starlinks used only ether1. ether3 had an address and its Starlink
worked, but:
  • its default route (0.0.0.0/0 via the check host 8.8.8.8, recursive) was INACTIVE with an empty
    immediate-gw: the 8.8.8.8/32 route that should send the check host out of ether3 was missing,
    so RouterOS could not resolve the route, and
  • nothing masqueraded out of ether3 (the only srcnat rules were out-interface-list=WAN — which held
    ether1 only — and a Quick WAN rule on ether1), so customer traffic leaving ether3 would have kept
    its 172.16.x source address and been dropped by the modem.
The Internet Doctor tested the line with its own temporary probe routes, so the line looked
"working" and the page offered only “Put in use” — which turns on the DHCP default route and still
leaves the line without NAT.

What this does
==============
For each line it checks the wiring that customers need, not the modem:
  1. health-check route: the /32 route that pins the line's check host to the line's own port
     exists, is enabled and goes out of THAT port (not the other modem);
  2. NAT: some enabled srcnat rule hides customers behind this port;
  3. WAN list: when the router's firewall uses an interface list named WAN and another line is in
     it, this line is too (so the default "drop from WAN" protection and WAN NAT cover it).
One click (“Repair wiring”) puts all three right, over the API or through TapTap Link. It never
touches the other line's routes, never removes anything (a conflicting hand-made /32 is disabled,
not deleted), and never forces a route active: the default route keeps check-gateway=ping, so a line
whose internet really is down stays out of use.
"""
from __future__ import annotations

import ipaddress
import re

TAG = 'TapTap rewire'                    # comment on things this module adds (WAN list member)
NAT_COMMENT = 'TapTap Quick WAN'         # same comment as MikroTikService.configure_wan_dhcp_nat
OFF_SUFFIX = ' ~rewire-off'              # appended to a conflicting route this module disabled
WAN_LIST = 'WAN'
_HOST = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')
_IFACE = re.compile(r'^[A-Za-z0-9._:-]{1,40}$')
_SCRIPT_COMMENT = re.compile(r'comment\s*=\s*"([^"]{1,120})"')
_SCRIPT_MARK = re.compile(r'gw=([A-Za-z0-9._:-]{1,40});')
_NOT_CUSTOMER_TYPES = {'wg', 'wireguard', 'ovpn-out', 'ovpn-in', 'l2tp-out', 'l2tp-in', 'pppoe-out', 'pptp-out',
                       'sstp-out', 'sstp-in', 'loopback', 'lte', 'gre-tunnel', 'eoip-tunnel', 'ipip-tunnel'}
HELD_MARKS = (' ~doctor-off:', ' ~owner-off:')   # a line deliberately out of use — not a wiring fault


class WiringError(Exception):
    pass


def _s(v):
    return str(v if v is not None else '').strip()


def _yes(v):
    return _s(v).lower() in {'true', 'yes'}


def _is_host(v):
    if not _HOST.match(_s(v)):
        return False
    try:
        ipaddress.IPv4Address(_s(v))
        return True
    except ValueError:
        return False


def _net(v):
    try:
        return ipaddress.ip_network(_s(v), strict=False)
    except ValueError:
        return None


def _rows(svc, path):
    try:
        return [dict(r) for r in (svc.resource(path).get() or [])]
    except Exception:
        try:
            return [dict(r) for r in (svc.safe_get(path) or [])]
        except Exception:
            return []


def _doctor(row):
    return _s(row.get('comment')).startswith('TapTap Doctor')


def _held(row):
    c = _s(row.get('comment'))
    return any(m in c for m in HELD_MARKS)


def _client(svc, iface):
    for r in _rows(svc, '/ip/dhcp-client'):
        if _s(r.get('interface')) == iface:
            return 'dhcp', r
    for r in _rows(svc, '/interface/pppoe-client'):
        if _s(r.get('name')) == iface:
            return 'pppoe', r
    return None, None


def _label_in(label, text):
    label = _s(label)
    if not label or len(label) < 3:
        return False
    return re.search(r'(?<![A-Za-z0-9])' + re.escape(label) + r'(?![A-Za-z0-9])', _s(text), re.I) is not None


def _label_from_check_comment(comment):
    """'TapTap check Starlink 2' → 'Starlink 2' (the older Internet Lines naming)."""
    m = re.match(r'^TapTap check (.+)$', _s(comment))
    return m.group(1).strip() if m else ''


# ═══════════════════════════ reading the router ═══════════════════════════
def _context(svc):
    routes = _rows(svc, '/ip/route')
    static = [r for r in routes if not _yes(r.get('dynamic'))]
    defaults = [r for r in static if _s(r.get('dst-address')) == '0.0.0.0/0'
                and _s(r.get('comment')).startswith('TapTap') and not _doctor(r)]
    recursive = [r for r in defaults if _is_host(_s(r.get('gateway')))]
    hosts = list(dict.fromkeys(_s(r.get('gateway')) for r in recursive))
    host_routes = {h: [r for r in static if _s(r.get('dst-address')) == f'{h}/32' and not _doctor(r)] for h in hosts}
    return {'routes': routes, 'defaults': defaults, 'recursive': recursive, 'hosts': hosts, 'host_routes': host_routes}


def _script_hint(client_row):
    """What the line's DHCP client script updates: (route comment, gw= marker interface)."""
    script = _s((client_row or {}).get('script'))
    m = _SCRIPT_COMMENT.search(script)
    k = _SCRIPT_MARK.search(script)
    return (m.group(1) if m else ''), (k.group(1) if k else ''), script


def _host_for(ctx, line, lines, script_comment):
    """The check host that belongs to this line, and why. '' when the line has none."""
    iface, label = line['interface'], line.get('label') or line['interface']
    # a) a check route already goes out of this port
    for h in ctx['hosts']:
        if any(_s(r.get('gateway')).endswith('%' + iface) for r in ctx['host_routes'][h]):
            return h, 'route'
    # b) the route the DHCP script keeps up to date
    if script_comment:
        for r in ctx['routes']:
            if _s(r.get('comment')) == script_comment and _s(r.get('dst-address')).endswith('/32'):
                h = _s(r.get('dst-address')).split('/')[0]
                if h in ctx['hosts']:
                    return h, 'script'
    # c) the default route is named after this line ('TapTap Starlink 2 pinned', 'combined: Starlink 2'),
    #    and not after another line — even when its /32 wrongly goes out of the other port
    ifaces = {l['interface'] for l in lines}
    names = [n for n in (label, _label_from_check_comment(script_comment)) if n]
    others = [_s(l.get('label')) for l in lines if l['interface'] != iface and _s(l.get('label'))]
    named = set()
    for r in ctx['recursive']:
        c = _s(r.get('comment'))
        if any(_label_in(n, c) for n in names) and not any(
                _label_in(o, c) and not any(_label_in(o, n) for n in names) for o in others):
            named.add(_s(r.get('gateway')))
    if len(named) == 1:
        return named.pop(), 'name'
    good_elsewhere = {h for h in ctx['hosts'] for r in ctx['host_routes'][h]
                      if not _yes(r.get('disabled')) and '%' in _s(r.get('gateway'))
                      and _s(r.get('gateway')).split('%')[-1] in ifaces - {iface}}
    # d) exactly one check host has no working route on any line, and this is the only line without a host
    orphans = [h for h in ctx['hosts'] if h not in good_elsewhere and not any(
        not _yes(r.get('disabled')) and _s(r.get('gateway')).endswith('%' + iface) for r in ctx['host_routes'][h])]
    with_host = {_s(r.get('gateway')).split('%')[-1] for h in ctx['hosts'] for r in ctx['host_routes'][h]
                 if '%' in _s(r.get('gateway')) and not _yes(r.get('disabled'))}
    without = [x for x in ifaces if x not in with_host]
    if len(orphans) == 1 and without == [iface]:
        return orphans[0], 'only-orphan'
    return '', ''


def _customer_nets(svc, line_ifaces):
    kinds = {_s(r.get('name')): _s(r.get('type')) for r in _rows(svc, '/interface')}
    hs = {_s(r.get('interface')) for r in _rows(svc, '/ip/hotspot') if not _yes(r.get('disabled'))}
    nets = []
    for a in _rows(svc, '/ip/address'):
        iface = _s(a.get('interface'))
        if iface in line_ifaces or _yes(a.get('disabled')) or kinds.get(iface, '') in _NOT_CUSTOMER_TYPES:
            continue
        if hs and iface not in hs and not kinds.get(iface, '').startswith('bridge'):
            continue
        n = _net(a.get('address'))
        if n:
            nets.append(n)
    return nets


def _nat_covers(svc, iface, line_ifaces):
    """True / False / None (no NAT table known). A rule covers the port when it masquerades or src-nats
    customers out of it: out-interface=port, an out-interface-list holding the port, or no out filter at all
    for a source that includes the customers."""
    rules = _rows(svc, '/ip/firewall/nat')
    if not rules:
        return None
    members = _rows(svc, '/interface/list/member')
    in_list = lambda name: (not members) or any(_s(m.get('list')) == name and _s(m.get('interface')) == iface
                                                and not _yes(m.get('disabled')) for m in members)
    customers = None
    for r in rules:
        if _s(r.get('chain')) != 'srcnat' or _yes(r.get('disabled')) or _s(r.get('action')) not in {'masquerade', 'src-nat'}:
            continue
        oi, ol = _s(r.get('out-interface')), _s(r.get('out-interface-list'))
        if oi.startswith('!') or ol.startswith('!'):
            continue
        if oi:
            if oi == iface:
                return True
            continue
        if ol:
            if in_list(ol):
                return True
            continue
        src = _s(r.get('src-address'))
        if not src or _s(r.get('src-address-list')):
            return True
        if src.startswith('!'):
            continue
        if customers is None:
            customers = _customer_nets(svc, line_ifaces)
        sn = _net(src)
        if sn and any(sn.overlaps(c) for c in customers):
            return True
    return False


def _wan_list_missing(svc, iface, line_ifaces):
    lists = {_s(r.get('name')) for r in _rows(svc, '/interface/list')}
    members = _rows(svc, '/interface/list/member')
    if WAN_LIST not in lists and not any(_s(m.get('list')) == WAN_LIST for m in members):
        return False
    wan = {_s(m.get('interface')) for m in members if _s(m.get('list')) == WAN_LIST and not _yes(m.get('disabled'))}
    return bool(wan & (set(line_ifaces) - {iface})) and iface not in wan


# ═══════════════════════════ the check ═══════════════════════════
def line_wiring(svc, line, lines):
    """What is wrong with how customers reach the internet through this line.

    Returns {'problems': [{'code', 'text'}], 'host', 'comment', 'nat', 'wan_list', 'script', 'fixable', 'held'}.
    Read only — works on a live connection and on the TapTap Link snapshot alike."""
    iface, label = line['interface'], line.get('label') or line['interface']
    line_ifaces = [l['interface'] for l in lines]
    kind, client = _client(svc, iface)
    out = {'problems': [], 'host': '', 'comment': '', 'nat': False, 'wan_list': False, 'script': False,
           'fixable': False, 'held': False}
    if client is not None and _yes(client.get('disabled')):
        return out
    ctx = _context(svc)
    script_comment, script_mark, script = _script_hint(client)
    host, why = _host_for(ctx, line, lines, script_comment)
    if host:
        defaults = [r for r in ctx['recursive'] if _s(r.get('gateway')) == host]
        rows = ctx['host_routes'][host]
        if any(_held(r) for r in defaults + rows):
            out['held'] = True                 # taken out of use on purpose (owner or Doctor) — leave it
        else:
            good = [r for r in rows if not _yes(r.get('disabled')) and _s(r.get('gateway')).endswith('%' + iface)]
            wrong = [r for r in rows if not _yes(r.get('disabled')) and not _s(r.get('gateway')).endswith('%' + iface)]
            if not good:
                if not rows:
                    text = (f'The route that sends the health check {host} out of {iface} is missing, so {label}’s '
                            f'internet route cannot work (it shows as inactive).')
                elif wrong:
                    port = _s(wrong[0].get('gateway')).split('%')[-1] if '%' in _s(wrong[0].get('gateway')) else 'no port'
                    text = (f'The health check {host} for {label} goes out of {port} instead of {iface}, so {label} is '
                            f'judged by the other modem and its route cannot work.')
                else:
                    text = f'The route that sends the health check {host} out of {iface} is switched off, so {label}’s internet route cannot work.'
                out['problems'].append({'code': 'check_route', 'text': text})
            elif wrong:
                out['problems'].append({'code': 'check_conflict', 'text': f'A second route also sends {host} out of '
                                        + (_s(wrong[0].get('gateway')).split('%')[-1] or 'another port')
                                        + f' — the health check of {label} can ride the wrong modem.'})
            out['host'] = host
            existing = next((_s(r.get('comment')) for r in rows if _s(r.get('comment')).startswith('TapTap')
                             and not _held(r)), '')
            if script_comment and not script_mark:
                out['comment'] = script_comment                    # the DHCP script already keeps this route up to date
            elif script_mark == iface and f'gw={iface};' in existing:
                out['comment'] = existing
            elif script_mark == iface:
                out['comment'] = f'TapTap check {host} via {label} gw={iface};'
            else:
                out['comment'] = existing or f'TapTap check {label}'
            if kind == 'dhcp':
                updates = (script_comment and script_comment == out['comment']) or (script_mark == iface)
                if not updates and (not script or 'TapTap' in script):
                    out['script'] = True
    nat = _nat_covers(svc, iface, line_ifaces)
    if nat is False:
        out['nat'] = True
        out['problems'].append({'code': 'nat', 'text': f'Nothing hides customers behind {iface} (no masquerade), so their '
                                                      f'traffic would leave {label} with private addresses and be dropped.'})
    if _wan_list_missing(svc, iface, line_ifaces):
        out['wan_list'] = True
        out['problems'].append({'code': 'wan_list', 'text': f'{iface} is not in the router’s WAN interface list like the other '
                                                           f'line, so the WAN firewall protection and WAN NAT do not cover it.'})
    out['fixable'] = bool(out['problems']) and not out['held']
    return out


def summary(wiring, label):
    n = len(wiring.get('problems') or [])
    if not n:
        return ''
    return (f' {label} is not wired for customers ({n} problem{"s" if n > 1 else ""}) — press “Repair wiring”.')


# ═══════════════════════════ the repair over the API ═══════════════════════════
def _gateway(svc, line, kind, client):
    iface = line['interface']
    if kind == 'pppoe':
        return iface
    gw = _s((client or {}).get('gateway')) if kind == 'dhcp' else _s(line.get('gateway'))
    if not gw:
        raise WiringError(f'{line.get("label") or iface} has no gateway from its modem yet — wait for it to get an address, then try again.')
    return f'{gw}%{iface}'


def dhcp_script(comment, iface):
    return f':if ($bound=1) do={{/ip route set [find comment="{comment}"] gateway=($"gateway-address" . "%{iface}")}}'


def repair(svc, line, lines):
    """Put the line's wiring right. Returns a plain-language result."""
    iface, label = line['interface'], line.get('label') or line['interface']
    w = line_wiring(svc, line, lines)
    if w['held']:
        raise WiringError(f'{label} was taken out of use on purpose. Press “Put in use” instead.')
    if not w['fixable']:
        return f'{label} is already wired for customers — nothing to repair.'
    kind, client = _client(svc, iface)
    done = []
    if w['host']:
        gwp = _gateway(svc, line, kind, client)
        routes = svc.resource('/ip/route')
        rows = [r for r in routes.get() if _s(r.get('dst-address')) == f'{w["host"]}/32'
                and not _yes(r.get('dynamic')) and not _doctor(r)]
        keep = next((r for r in rows if _s(r.get('comment')) == w['comment']), None) \
            or next((r for r in rows if _s(r.get('gateway')).endswith('%' + iface)), None) \
            or next((r for r in rows if _s(r.get('comment')).startswith('TapTap')), None)
        if keep is None:
            routes.add(dst_address=f'{w["host"]}/32', gateway=gwp, scope='10', comment=w['comment'])
            done.append(f'added the route that sends {w["host"]} out of {iface}')
        else:
            routes.set(id=keep['id'], gateway=gwp, scope='10', disabled='no', comment=w['comment'])
            done.append(f'pointed {w["host"]} at {iface}')
        for r in rows:
            if (keep is None or r['id'] != keep['id']) and not _yes(r.get('disabled')) and not _s(r.get('gateway')).endswith('%' + iface):
                routes.set(id=r['id'], disabled='yes', comment=(_s(r.get('comment')) + OFF_SUFFIX)[:255])
                done.append(f'switched off a second {w["host"]} route that used another port')
        if w['script'] and kind == 'dhcp':
            res = svc.resource('/ip/dhcp-client')
            res.set(id=client['id'], script=dhcp_script(w['comment'], iface))
            done.append(f'the {iface} DHCP client now keeps that route on the right modem after a restart')
    if w['nat']:
        nat = svc.resource('/ip/firewall/nat')
        mine = [r for r in nat.get() if _s(r.get('chain')) == 'srcnat' and _s(r.get('action')) == 'masquerade'
                and _s(r.get('out-interface')) == iface]
        if mine:
            nat.set(id=mine[0]['id'], disabled='no')
        else:
            nat.add(chain='srcnat', action='masquerade', out_interface=iface, comment=NAT_COMMENT)
        done.append(f'customers are now hidden behind {iface} (masquerade)')
    if w['wan_list']:
        svc.resource('/interface/list/member').add(list=WAN_LIST, interface=iface, comment=TAG)
        done.append(f'{iface} joined the WAN list')
    tail = (' Its route comes up by itself once the health check answers through it (a few seconds).'
            if w['host'] else ' Press “Put in use” if customers should use it.')
    return f'{label}: ' + '; '.join(done) + '.' + tail


# ═══════════════════════════ the repair through TapTap Link ═══════════════════════════
def _q(iface):
    if not _IFACE.match(_s(iface)):
        raise ValueError('Invalid interface.')
    return '"' + _s(iface) + '"'


def _lit(text):
    return '"' + str(text).replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('\r', '').replace('\n', '\\n') + '"'


def _clean_comment(text):
    c = re.sub(r'[\x00-\x1f"\\$]', '', _s(text))[:120]
    if not c:
        raise ValueError('Invalid comment.')
    return c


def link_params(line, wiring):
    """Validated parameters for a queued Link command (the RouterOS text is rebuilt on delivery)."""
    p = {'interface': line['interface'], 'type': line.get('type', 'dhcp'), 'label': _s(line.get('label'))[:40],
         'nat': bool(wiring.get('nat')), 'wan_list': bool(wiring.get('wan_list')), 'script': bool(wiring.get('script'))}
    if wiring.get('host'):
        p['host'] = wiring['host']
        p['comment'] = wiring['comment']
    if p['type'] == 'static':
        p['gateway'] = _s(line.get('gateway'))
    return p


def link_body(p):
    """RouterOS commands for one queued rewire (validated, never trusted as text). Stays well under agent.MAX_SCRIPT."""
    iface = _s(p.get('interface'))
    q = _q(iface)
    kind = _s(p.get('type')) if _s(p.get('type')) in {'dhcp', 'pppoe', 'static'} else 'dhcp'
    out = []
    host = _s(p.get('host'))
    if host:
        if not _is_host(host):
            raise ValueError('Invalid host.')
        c = _lit(_clean_comment(p.get('comment')))
        dst = f'"{host}/32"'
        if kind == 'pppoe':
            out.append(f':local gwp {q}')
        else:
            if kind == 'static':
                gw = _s(p.get('gateway'))
                if not _is_host(gw):
                    raise ValueError('Invalid gateway.')
                out.append(f':local gw "{gw}"')
            else:
                out.append(f':local gw ""; :do {{ :set gw [:tostr [/ip dhcp-client get [find where interface={q}] gateway]] }} on-error={{}}')
            out.append(f':if ([:len $gw] = 0) do={{ :error "TapTap rewire: {iface} has no gateway" }}')
            out.append(f':local gwp ($gw . "%{iface}")')
        mine = f'[/ip route find where dst-address={dst} and dynamic=no and !(comment~"^TapTap Doctor")]'
        out.append(f':local keep [/ip route find where dst-address={dst} and dynamic=no and comment={c}]')
        out.append(f':if ([:len $keep] = 0) do={{ :set keep [/ip route find where dst-address={dst} and dynamic=no and gateway~"%{iface}\\$"] }}')
        out.append(f':if ([:len $keep] = 0) do={{ :set keep [/ip route find where dst-address={dst} and dynamic=no and comment~"^TapTap" and !(comment~"^TapTap Doctor")] }}')
        out.append(f':if ([:len $keep] = 0) do={{ /ip route add dst-address={dst} gateway=$gwp scope=10 comment={c}; :set keep [/ip route find where dst-address={dst} and comment={c}] }} '
                   f'else={{ /ip route set ($keep->0) gateway=$gwp scope=10 disabled=no comment={c} }}')
        out.append(f':foreach r in={mine} do={{ :if (($r != ($keep->0)) && ([/ip route get $r disabled] = false) && '
                   f'!([:tostr [/ip route get $r gateway]] ~ "%{iface}\\$")) do={{ '
                   f'/ip route set $r disabled=yes comment=([:tostr [/ip route get $r comment]] . "{OFF_SUFFIX}") }} }}')
        if p.get('script') and kind == 'dhcp':
            out.append(f'/ip dhcp-client set [find where interface={q}] script={_lit(dhcp_script(_clean_comment(p.get("comment")), iface))}')
    if p.get('nat'):
        find = f'[/ip firewall nat find where chain=srcnat and action=masquerade and out-interface={q}]'
        out.append(f':if ([:len {find}] = 0) do={{ /ip firewall nat add chain=srcnat action=masquerade out-interface={q} comment="{NAT_COMMENT}" }} '
                   f'else={{ /ip firewall nat set {find} disabled=no }}')
    if p.get('wan_list'):
        out.append(f':if (([:len [/interface list find where name="{WAN_LIST}"]] > 0) && ([:len [/interface list member find where list="{WAN_LIST}" and interface={q}]] = 0)) '
                   f'do={{ /interface list member add list={WAN_LIST} interface={q} comment="{TAG}" }}')
    if not out:
        raise ValueError('Nothing to repair.')
    out.append(f':log warning "TapTap: wiring of {iface} repaired through TapTap Link"')
    return '; '.join(out)
