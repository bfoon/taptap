"""Line Hub — the control room of a router's internet lines (Internet lines page, Traffic → Internet lines).

One place to see and run every gateway of a router:

* **Who is on which line.** Every customer online is placed on the line their traffic really leaves through. The
  traffic sample TapTap already takes once a minute (core/traffic.py) carries, per connection, the address the reply
  comes back to — the internet line's own address after NAT — so this works for *any* setup, TapTap's or hand-made.
  A customer moved by hand (core/line_steer.py) is shown on the line they are kept on.
* **Moving customers.** One, several or a whole line at once — by drag and drop or "Move to". Possible when TapTap
  runs the router's lines with marks ("Share the load", "Backup line", "Combine lines"): each moved device gets one
  mangle rule above TapTap's own line rules, and its open connections are closed so it changes line at once.
* **Each gateway.** Switch on / off and in / out of use (Internet Doctor, core/wan_doctor.py), delete it, change its
  settings, add a new line on a free port, NAT on / off, and choose the line that carries TapTap Link / Tunnel.
* **Router-wide.** Firewall presets and rule switches, DHCP servers (lease time, DNS, on / off).

Every change travels the way the router is reached: live over the RouterOS API (direct or TapTap Tunnel), or as one
TapTap Link command applied at the next check-in. Changes are built here as small structured operations, checked
against an allow-list both when they are made and again when TapTap Link turns them into RouterOS text — nothing
the browser sends is ever run as RouterOS text. Everything TapTap adds carries a comment starting with TAG, and only
rows with that comment are ever removed (apart from deleting a line's own DHCP / PPPoE client, which the owner asks
for by name). Changes that could cut TapTap off from a router reached over the API install an undo timer on the
router first; TapTap removes it after reaching the router again on a fresh connection.

No database migration: moves use LinePin (core/models_flow.py), the design lives in WanSetup.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import line_steer as ls
from . import wan

logger = logging.getLogger('taptap.line_hub')

TAG = 'TapTap Line Hub'
LINK_KIND = 'line_hub'
UNDO_SCHED = 'TapTap-Hub-undo'
UNDO_MINUTES = 5
HUB_WAN_LIST = 'TT-HUB-WAN'
MAX_MOVE = 120                 # devices in one move
LINK_BUDGET = 2600             # RouterOS characters per TapTap Link command (the Link batch carries ~3600)
PEOPLE_MAX = 250               # customers listed per line
NAT_KEY = 'tt:lh:nat:{}'
_INSTALLED = False


class HubError(ValueError):
    pass


def _s(v):
    return str(v if v is not None else '').strip()


def _yes(v):
    return _s(v).lower() in ('true', 'yes')


def _rid(row):
    return _s(row.get('id') or row.get('.id'))


def _plain(text, n=40):
    """Comments travel inside RouterOS scripts: plain ASCII only."""
    return re.sub(r'[^A-Za-z0-9 ._+()/-]', '', _s(text))[:n].strip()


def _ipv4(v):
    try:
        ip = ipaddress.ip_address(_s(v))
    except ValueError:
        return None
    return ip if ip.version == 4 else None


def _rx(text):
    """Text as a literal inside a RouterOS (POSIX) regular expression."""
    return re.sub(r'([.^$*+?()\[\]{}|\\])', r'\\\1', str(text if text is not None else ''))   # spaces kept: they end a key


def _ip_of(addr):
    """'192.168.1.23/24' → '192.168.1.23'."""
    return _s(addr).split('/')[0]


# ═══════════════════════════ what the traffic sample tells (free) ═══════════════════════════

def note_nat(router, by_ip, now=None):
    """Called by core.traffic.ingest_connections once a minute: {customer ip: {address it leaves from: connections}}."""
    data = {ip: dict(m) for ip, m in by_ip.items() if m}
    cache.set(NAT_KEY.format(router.pk), {'at': (now or timezone.now()).isoformat(), 'ips': data}, 900)


def nat_seen(router):
    return cache.get(NAT_KEY.format(router.pk)) or {}


# ═══════════════════════════ reading the router ═══════════════════════════

class Rows:
    """Rows of RouterOS menus from one source — a live connection or the latest synced copy — each read once."""

    def __init__(self, svc, live, via):
        self.svc, self.live, self.via = svc, live, via
        self._c = {}

    def __call__(self, path):
        if path not in self._c:
            try:
                self._c[path] = [dict(r) for r in (self.svc.safe_get(path) or [])]
            except Exception:
                self._c[path] = []
        return self._c[path]

    def forget(self):
        self._c = {}

    def close(self):
        try:
            self.svc.close()
        except Exception:
            pass

    # read-only adapter, so the Internet Doctor's helpers work on the cached rows
    def resource(self, path):
        return _CachedRes(self, path)

    def safe_get(self, path, **filters):
        return _CachedRes(self, path).get(**filters)


class DictRows(Rows):
    """Rows remembered from the last full read (the 8-second pulse must not read the whole router every time)."""

    def __init__(self, tables, via):
        super().__init__(None, False, via)
        self._c = {p: [dict(r) for r in rows] for p, rows in tables.items()}

    def __call__(self, path):
        return self._c.get(path, [])

    def close(self):
        pass


VIEW_KEY = 'tt:lh:view:{}'
VIEW_PATHS = ('/interface', '/ip/dhcp-client', '/interface/pppoe-client', '/ip/address', '/ip/route', '/ip/firewall/mangle',
              '/ip/firewall/nat', '/interface/list/member', '/ip/dhcp-client/gateways')


def remember_view(router, rows):
    try:
        cache.set(VIEW_KEY.format(router.pk), {'via': rows.via, 'tables': {p: rows(p) for p in VIEW_PATHS}}, 1800)
    except Exception:
        pass


def quick_rows(router):
    """Cheap rows for the board: what the last full read saw (live routers), else the latest synced copy."""
    if not on_link(router):
        hit = cache.get(VIEW_KEY.format(router.pk))
        if hit:
            return DictRows(hit['tables'], hit['via'])
    return open_rows(router, live=False)


class _CachedRes:
    def __init__(self, rows, path):
        self.rows, self.path = rows, path

    def get(self, **filters):
        out = self.rows(self.path)
        for k, v in filters.items():
            key = k.replace('_', '-')
            out = [r for r in out if _s(r.get(key, r.get(k))) == _s(v)]
        return [dict(r) for r in out]


def channel(router):
    from .voucher_history import channel as ch
    return ch(router)


def on_link(router):
    from .linkops import uses_link
    return uses_link(router)


def open_rows(router, live=True):
    """Rows from the router itself when it can be reached, else from the latest TapTap Link sync."""
    from .linkops import SnapshotService
    if live and not on_link(router):
        from .mikrotik import MikroTikService
        try:
            return Rows(MikroTikService(router).connect(), True, channel(router))
        except Exception as exc:
            if getattr(router, 'connection_mode', 'api') != 'agent':
                raise HubError(f'Could not reach {router.name}: {exc}')
            logger.info('line hub: %s not reachable live, using the synced copy (%s)', router, exc)
    return Rows(SnapshotService(router), False, 'TapTap Link' if on_link(router) else 'last sync')


def _setup(router):
    from .models import WanSetup
    return WanSetup.objects.get_or_create(router=router)[0]


# ═══════════════════════════ the lines ═══════════════════════════

def _cfg(setup):
    """The owner's saved design, normalized when possible (labels, speeds, planned shares)."""
    raw = setup.config or {}
    saved = {_s(l.get('interface')): l for l in raw.get('links') or [] if l.get('interface')}
    order = [_s(l.get('interface')) for l in raw.get('links') or [] if l.get('interface')]
    norm, errors = None, ['no design']
    if raw.get('links'):
        try:
            norm, errors, _w = wan.normalize(raw, setup.facts or {})
        except Exception:
            norm, errors = None, ['bad design']
    return raw, saved, order, (norm if not errors else None)


def planned_shares(norm):
    """{interface: % of customers the design sends there} (None for "Split by network")."""
    if not norm:
        return {}
    L, strat = norm['links'], norm['strategy']
    if strat == 'single':
        return {l['interface']: 100 for l in L[:1]}
    if strat == 'failover':
        return {l['interface']: (100 if i == 0 else 0) for i, l in enumerate(L)}
    if strat == 'balance':
        tot = sum(l.get('weight', 1) for l in L) or 1
        return {l['interface']: round(l.get('weight', 1) * 100 / tot) for l in L}
    if strat == 'ecmp':
        return {l['interface']: round(100 / len(L)) for l in L}
    return {l['interface']: None for l in L}


def _lists_of(rows, iface):
    return {_s(m.get('list')) for m in rows('/interface/list/member') if _s(m.get('interface')) == iface and not _yes(m.get('disabled'))}


def nat_for(rows, iface):
    """Masquerade / src-nat rules that hide customers behind this line."""
    lists = _lists_of(rows, iface)
    out = []
    for r in rows('/ip/firewall/nat'):
        if _s(r.get('chain')) != 'srcnat' or _s(r.get('action')) not in ('masquerade', 'src-nat'):
            continue
        oi, ol = _s(r.get('out-interface')), _s(r.get('out-interface-list'))
        if oi == iface:
            via = 'port'
        elif ol and ol in lists:
            via = f'list {ol}'
        else:
            continue
        out.append({'id': _rid(r), 'disabled': _yes(r.get('disabled')), 'via': via, 'comment': _s(r.get('comment'))[:80],
                    'action': _s(r.get('action')), 'mine': _s(r.get('comment')).startswith(TAG),
                    'taptap': _s(r.get('comment')).lower().startswith('taptap')})
    return {'on': any(not x['disabled'] for x in out), 'rules': out}


def link_line_of(rows):
    """The line chosen to carry TapTap Link / Tunnel ('' = automatic), from TapTap's routes on the router."""
    for r in rows('/ip/route'):
        c = _s(r.get('comment'))
        if c.startswith(f'{TAG}: TapTap via'):
            m = re.search(r'port=([^;\s]+);', c)
            if m:
                return m.group(1), not _yes(r.get('disabled')) and (('active' not in r) or _yes(r.get('active')))
    return '', False


def lines(router, rows, setup=None):
    """Every internet line of the router, with its state. Missing ports (a client left on a deleted port, shown by
    RouterOS as "*2") are included and marked, so they can be deleted."""
    from . import wan_doctor as doc
    setup = setup or _setup(router)
    raw, saved, order, norm = _cfg(setup)
    shares = planned_shares(norm)
    selected = {l['interface'] for l in (norm or {}).get('links', [])}
    ifaces = {_s(r.get('name')): r for r in rows('/interface')}
    roles = {}
    try:
        roles = {x.interface_name: x for x in router.interface_roles.all()}
    except Exception:
        pass
    routes = rows('/ip/route')
    explicit = {_s(r.get('interface')): r for r in rows('/ip/dhcp-client/gateways') if r.get('interface')}
    addrs = {}
    for a in rows('/ip/address'):
        if _yes(a.get('disabled')):
            continue
        addrs.setdefault(_s(a.get('actual-interface') or a.get('interface')), []).append(_s(a.get('address')))
    link_iface, link_ok = link_line_of(rows)
    out, seen = [], set()

    def add(iface, kind, client=None, gateway=''):
        if not iface or iface in seen:
            return
        seen.add(iface)
        client = client or {}
        port = ifaces.get(iface, {})
        missing = kind in ('dhcp', 'pppoe') and wan.is_orphan(iface, ifaces)
        s = saved.get(iface, {})
        role = roles.get(iface)
        label = _s(s.get('label')) or (role.isp_name if role and getattr(role, 'isp_name', '') else '') or \
            (role.label if role and role.label else '') or _s(client.get('comment')) or _s(port.get('comment')) or iface
        if label.startswith(TAG):
            label = label[len(TAG):].lstrip(': ') or iface
        address = _s(client.get('address')) if kind == 'dhcp' else ''
        address = address or next(iter(addrs.get(iface, [])), '')
        if kind == 'dhcp':
            gateway = wan.dhcp_gateway(client, explicit.get(iface), routes)
        elif kind == 'pppoe':
            gateway = iface
        client_off = _yes(client.get('disabled'))
        port_off = _yes(port.get('disabled'))
        running = not missing and not port_off and _yes(port.get('running', 'true'))
        if kind == 'dhcp':
            status = 'port missing' if missing else ('switched off' if client_off else (_s(client.get('status')) or 'unknown'))
            up = running and not client_off and _s(client.get('status')) == 'bound'
        elif kind == 'pppoe':
            status = 'port missing' if missing else ('switched off' if client_off else ('connected' if _yes(client.get('running')) else 'disconnected'))
            up = running and not client_off and _yes(client.get('running'))
        else:
            status = 'static' if gateway else 'no gateway'
            up = running and bool(gateway)
        try:
            use = doc.use_state(rows, iface) if not missing else {'state': 'out', 'has_routes': False, 'routes_off': 0}
            if use['state'] == 'out' and not missing:
                # A backup line waiting its turn: its TapTap routes are switched on but not active (the main line is
                # up). The Doctor calls that "out"; it is in use — customers move to it the moment the main line fails.
                _c, defaults, _p = doc.line_routes(rows, iface)
                if any(not _yes(r.get('disabled')) for r in defaults):
                    use = {**use, 'state': 'standby'}
        except Exception:
            use = {'state': 'unknown', 'has_routes': False, 'routes_off': 0}
        out.append({
            'interface': iface, 'type': kind, 'label': label[:60], 'gateway': gateway, 'address': address,
            'status': status, 'up': up, 'running': running, 'missing': missing,
            'client_off': client_off, 'port_off': port_off, 'switchable': kind in ('dhcp', 'pppoe') and not missing,
            'speed': s.get('speed') or (20 if 'lte' in _s(port.get('type')) else 50), 'check': _s(s.get('check')),
            'use': use['state'], 'routes_off': use.get('routes_off', 0), 'has_routes': use.get('has_routes', False),
            'in_design': iface in saved, 'selected': iface in selected, 'order': order.index(iface) if iface in order else 99,
            'share': shares.get(iface),
            'mark': f'TT-{wan.safe_name(iface)}', 'nat': nat_for(rows, iface) if not missing else {'on': False, 'rules': []},
            'carries_link': iface == link_iface, 'link_route_ok': link_ok if iface == link_iface else None,
            'mac': _s(port.get('mac-address')), 'port_type': _s(port.get('type')),
            'use_peer_dns': _s(client.get('use-peer-dns')), 'user': _s(client.get('user')) if kind == 'pppoe' else '',
            'pppoe_port': _s(client.get('interface')) if kind == 'pppoe' else '', 'comment': _s(client.get('comment'))[:80],
        })

    for r in rows('/ip/dhcp-client'):
        add(_s(r.get('interface')), 'dhcp', r)
    for r in rows('/interface/pppoe-client'):
        add(_s(r.get('name')), 'pppoe', r)
    for iface, l in saved.items():
        if l.get('type') == 'static' and iface in ifaces:
            add(iface, 'static', None, _s(l.get('gateway')))
    for name, role in roles.items():
        if role.role == 'wan' and name in ifaces and name not in seen and addrs.get(name):
            add(name, 'static', None, _s(saved.get(name, {}).get('gateway')))
    out.sort(key=lambda l: (l['missing'], l['order'], l['interface']))
    for i, l in enumerate(out):
        l['color'] = i % 6
    return out


# ═══════════════════════════ customers on lines ═══════════════════════════

def _sessions(router):
    from . import traffic_idle as ti
    users, age = ti._snap(router, timezone.now())
    fresh = age is not None and age <= ti.FRESH
    out = []
    if fresh:
        for code, ss in users.items():
            for s in ss:
                if s.get('ip'):
                    out.append({'code': code, 'ip': _s(s.get('ip')), 'mac': _s(s.get('mac')).upper(),
                                'down_bps': int(s.get('down_bps') or 0), 'up_bps': int(s.get('up_bps') or 0)})
    return out, fresh, age


def _addr_map(rows, line_list):
    """Address an internet line hides customers behind → its interface."""
    m = {}
    wan_names = {l['interface'] for l in line_list}
    for a in rows('/ip/address'):
        iface = _s(a.get('actual-interface') or a.get('interface'))
        if iface in wan_names:
            m[_ip_of(a.get('address'))] = iface
    for l in line_list:
        if l.get('address'):
            m[_ip_of(l['address'])] = l['interface']
    return m


def steering(router, rows, line_list):
    """Which lines customers can be moved to: {interface: lane} (+ the lane info)."""
    info = ls.lanes(router, rows('/ip/firewall/mangle'))
    by_mark = {l['mark']: l for l in info['lanes']}
    out = {}
    for l in line_list:
        lane = by_mark.get(l['mark'])
        if lane is None:            # a hand-made sharing setup: match the line by its name
            lane = next((x for x in info['lanes'] if x['label'] and x['label'] in (l['label'], l['interface'])), None)
        if lane is not None:
            out[l['interface']] = lane
    return out, info


def board(router, rows=None, line_list=None):
    """Customers online now, grouped by the line they use — the data of the drag-and-drop board."""
    own = rows is None
    rows = rows or quick_rows(router)
    try:
        setup = _setup(router)
        line_list = line_list if line_list is not None else lines(router, rows, setup)
        sessions, fresh, age = _sessions(router)
        steer, info = steering(router, rows, line_list)
        mark_to_iface = {lane['mark']: iface for iface, lane in steer.items()}
        addr_iface = _addr_map(rows, line_list)
        nat = nat_seen(router)
        nat_ips = nat.get('ips') or {}
        marks = ls.seen(router).get('ips') or {}
        pins = ls.pins_of(router)
        pin_ip = {p.ip: p for p in pins if p.ip}
        pin_mac = {p.mac: p for p in pins if p.mac}
        strategy = (setup.config or {}).get('strategy') or ''
        tt_active = setup.status in ('active', 'pending')
        default = ''
        if tt_active and strategy in ('failover', 'single'):
            default = next((l['interface'] for l in sorted(line_list, key=lambda x: x['order'])
                            if l['selected'] and l['use'] in ('managed', 'plain') and l['up']), '')   # the active one, not a standby
        cols = {l['interface']: {'interface': l['interface'], 'label': l['label'], 'color': l['color'], 'up': l['up'],
                                 'use': l['use'], 'missing': l['missing'], 'share': l['share'],
                                 'mark': steer[l['interface']]['mark'] if l['interface'] in steer else '',
                                 'users': 0, 'down_bps': 0, 'up_bps': 0, 'people': []} for l in line_list if not l['missing']}
        unplaced = []
        for s in sessions:
            pin = pin_mac.get(s['mac']) if s['mac'] else None
            pin = pin or pin_ip.get(s['ip'])
            iface, how = '', ''
            if pin and mark_to_iface.get(pin.mark):
                iface, how = mark_to_iface[pin.mark], 'kept'
            if not iface:
                counts = nat_ips.get(s['ip']) or {}
                tally = {}
                for addr, n in counts.items():
                    if addr in addr_iface:
                        tally[addr_iface[addr]] = tally.get(addr_iface[addr], 0) + n
                if tally:
                    iface, how = max(tally.items(), key=lambda x: x[1])[0], 'seen'
            if not iface:
                counts = marks.get(s['ip']) or {}
                best = max(((mark_to_iface[m], n) for m, n in counts.items() if m in mark_to_iface), key=lambda x: x[1], default=None)
                if best:
                    iface, how = best[0], 'seen'
            if not iface and default:
                iface, how = default, 'main'
            person = {**s, 'how': how, 'pinned': bool(pin), 'pin_label': pin.label if pin else '',
                      'pin_confirmed': bool(pin and pin.confirmed)}
            col = cols.get(iface)
            if col is None:
                unplaced.append(person)
                continue
            col['users'] += 1
            col['down_bps'] += s['down_bps']
            col['up_bps'] += s['up_bps']
            col['people'].append(person)
        placed = sum(c['users'] for c in cols.values())
        for c in cols.values():
            c['real'] = round(c['users'] * 100 / placed) if placed else 0
            c['people'].sort(key=lambda p: -(p['down_bps'] + p['up_bps']))
            c['more'] = max(0, len(c['people']) - PEOPLE_MAX)
            c['people'] = c['people'][:PEOPLE_MAX]
        unplaced.sort(key=lambda p: -(p['down_bps'] + p['up_bps']))
        can_move, why = movable(router, setup, steer, info)
        return {'router_id': router.pk, 'router': router.name, 'via': channel(router), 'columns': list(cols.values()),
                'unplaced': unplaced[:PEOPLE_MAX], 'online': len(sessions), 'placed': placed, 'fresh': fresh,
                'age': int(age) if age is not None else None, 'nat_at': nat.get('at'), 'mark_at': ls.seen(router).get('at'),
                'can_move': can_move, 'why': why, 'sticky': info.get('sticky'), 'mode': info.get('mode', ''),
                'strategy': strategy, 'strategy_label': wan.STRATEGIES.get(strategy, ''),
                'pins': [{'id': p.pk, 'code': p.code, 'mac': p.mac, 'ip': p.ip, 'mark': p.mark, 'label': p.label, 'from': p.from_label,
                          'confirmed': p.confirmed, 'since': p.created_at.isoformat(),
                          'by': (p.created_by.get_full_name() or p.created_by.username) if p.created_by else ''} for p in pins]}
    finally:
        if own:
            rows.close()


def movable(router, setup, steer, info):
    if not ls.pins_ready():
        return False, ls.MIGRATE_HINT
    strategy = (setup.config or {}).get('strategy') or ''
    if strategy == 'split':
        return False, ('“Split by network” sends whole networks down chosen lines, so single customers are not moved — '
                       'change which line a network uses in step 2 instead.')
    if len(steer) >= 2:
        return True, ''
    if setup.status not in ('active', 'pending'):
        return False, ('Customers can be moved by hand once TapTap runs this router’s lines: choose “Share the load” or '
                       '“Backup line” in step 2 and apply. Until then the board shows who is on which line.')
    return False, info.get('why') or 'This router’s line rules were not found — run a sync, or apply the setup again.'


# ═══════════════════════════ structured router operations ═══════════════════════════
# op = {'v': verb, 'p': path, 'w': {field: value, 'comment^': prefix}, 'id': '*1A', 'f': {field: value}, 'before': {where}}

OPS = {
    '/ip/firewall/nat': ({'add', 'enable', 'disable', 'remove'},
                         {'chain', 'action', 'out-interface', 'out-interface-list', 'comment'}),
    '/ip/firewall/filter': ({'add', 'enable', 'disable', 'remove'},
                            {'chain', 'action', 'protocol', 'dst-port', 'in-interface-list', 'connection-state', 'comment'}),
    '/ip/route': ({'add', 'remove'}, {'dst-address', 'gateway', 'distance', 'check-gateway', 'target-scope', 'scope', 'comment'}),
    '/ip/dhcp-client': ({'add', 'set', 'remove'},
                        {'interface', 'add-default-route', 'default-route-distance', 'use-peer-dns', 'use-peer-ntp', 'comment', 'disabled', 'script'}),
    '/interface/pppoe-client': ({'add', 'set', 'remove'},
                                {'name', 'interface', 'user', 'password', 'add-default-route', 'default-route-distance', 'use-peer-dns',
                                 'comment', 'disabled'}),
    '/ip/address': ({'add', 'remove'}, {'address', 'interface', 'comment'}),
    '/ip/dhcp-server': ({'set', 'enable', 'disable'}, {'lease-time'}),
    '/ip/dhcp-server/network': ({'set'}, {'dns-server'}),
    '/interface/list': ({'ensure'}, {'name', 'comment'}),
    '/interface/list/member': ({'add', 'remove'}, {'list', 'interface', 'comment'}),
}
WHERE_KEYS = {'chain', 'action', 'out-interface', 'interface', 'name', 'address', 'list', 'comment', 'comment^', 'dst-address'}
# remove is only allowed on TapTap Line Hub's own rows — except a line's own DHCP / PPPoE client
REMOVE_ANY = {'/ip/dhcp-client', '/interface/pppoe-client'}
ID_RE = re.compile(r'^\*[0-9A-Fa-f]{1,8}$')
VAL_MAX = 900


def _clean_val(v, key=''):
    v = _s(v) if key != 'script' else str(v if v is not None else '')
    if len(v) > (VAL_MAX if key == 'script' else 160) or any(ord(c) < 32 and c not in '\n\t' for c in v):
        raise HubError('A value is too long or contains control characters.')
    if key != 'script' and any(ord(c) < 32 for c in v):
        raise HubError('A value contains control characters.')
    return v


def check_op(op):
    """Validated copy of one operation. Raises HubError."""
    if not isinstance(op, dict):
        raise HubError('Bad operation.')
    v, p = _s(op.get('v')), _s(op.get('p'))
    if p not in OPS:
        raise HubError(f'{p or "?"} cannot be changed here.')
    verbs, fields = OPS[p]
    if v not in verbs:
        raise HubError(f'{p}: “{v}” is not allowed.')
    out = {'v': v, 'p': p}
    f = {}
    for k, val in (op.get('f') or {}).items():
        k = _s(k)
        if k not in fields:
            raise HubError(f'{p}: “{k}” cannot be set here.')
        f[k] = _clean_val(val, k)
    if v in ('add', 'set', 'ensure') and not f:
        raise HubError('Nothing to set.')
    if f:
        out['f'] = f
    w = {}
    for k, val in (op.get('w') or {}).items():
        k = _s(k)
        if k not in WHERE_KEYS:
            raise HubError(f'“{k}” cannot be used to find rows.')
        w[k] = _clean_val(val)
    rid = _s(op.get('id'))
    if rid:
        if not ID_RE.match(rid):
            raise HubError('Bad row id.')
        out['id'] = rid
    if w:
        out['w'] = w
    if v in ('set', 'remove', 'enable', 'disable') and not (w or rid):
        raise HubError('Say which rows.')
    if v == 'remove' and p not in REMOVE_ANY and not _s(w.get('comment^')).startswith(TAG):
        raise HubError('Only TapTap Line Hub’s own rows can be removed here.')
    if v == 'ensure' and not w:
        raise HubError('Say which row must exist.')
    if op.get('before'):
        b = {}
        for k, val in op['before'].items():
            if _s(k) not in WHERE_KEYS:
                raise HubError('Bad position.')
            b[_s(k)] = _clean_val(val)
        out['before'] = b
    return out


def check_ops(ops):
    if not isinstance(ops, list) or not ops or len(ops) > 60:
        raise HubError('Nothing to do.')
    return [check_op(o) for o in ops]


def _match(row, op_w, rid=''):
    if rid:
        return _rid(row) == rid
    for k, v in op_w.items():
        if k == 'comment^':
            if not _s(row.get('comment')).startswith(v):
                return False
        elif _s(row.get(k)) != v:
            return False
    return True


def _api_kw(fields):
    return {str(k).replace('-', '_'): v for k, v in fields.items()}


def api_run(svc, ops):
    """Carry out checked operations over the API. Returns how many rows were touched."""
    n = 0
    for op in ops:
        res = svc.resource(op['p'])
        w, rid = op.get('w', {}), op.get('id', '')
        if op['v'] in ('add', 'ensure'):
            rows = [dict(r) for r in res.get()] if (op['v'] == 'ensure' or op.get('before')) else []
            if op['v'] == 'ensure' and any(_match(r, w) for r in rows):
                continue
            kw = _api_kw(op['f'])
            if op.get('before'):
                hit = next((r for r in rows if _match(r, op['before'])), None)
                if hit is not None:
                    kw['place_before'] = _rid(hit)
            res.add(**kw)
            n += 1
            continue
        for r in [dict(x) for x in res.get()]:
            if not _match(r, w, rid):
                continue
            if op['v'] == 'remove':
                res.remove(id=_rid(r))
            elif op['v'] == 'set':
                res.set(id=_rid(r), **_api_kw(op['f']))
            else:
                res.set(id=_rid(r), disabled='yes' if op['v'] == 'disable' else 'no')
            n += 1
    return n


def _rs(v):
    from .agent import rs
    return rs(v)


def _cond(w, rid=''):
    if rid:
        return f'.id={rid}'
    parts = []
    for k, v in w.items():
        if k == 'comment^':
            parts.append(f'comment~{_rs("^" + _rx(v))}')
        else:
            parts.append(f'{k}={_rs(v)}')
    return ' and '.join(parts)


def cli(op, n=0):
    """One checked operation as RouterOS text (TapTap Link, undo timers). ``n`` keeps helper variables apart."""
    p = wan.cli_path(op['p'])
    vals = ' '.join(f'{k}={_rs(v)}' for k, v in (op.get('f') or {}).items())
    if op['v'] == 'ensure':
        return f':if ([:len [{p} find where {_cond(op["w"])}]] = 0) do={{ {p} add {vals} }}'
    if op['v'] == 'add':
        if op.get('before'):
            return (f':local b{n} [{p} find where {_cond(op["before"])}]; :if ([:len $b{n}] > 0) do={{ {p} add {vals} '
                    f'place-before=($b{n}->0) }} else={{ {p} add {vals} }}')
        return f'{p} add {vals}'
    find = f'[find where {_cond(op.get("w", {}), op.get("id", ""))}]'
    if op['v'] == 'set':
        return f'{p} set {find} {vals}'
    return f'{p} {op["v"]} {find}'


def cli_block(ops):
    """Several operations as one RouterOS text, in order (a failing step stops the rest and fails the command)."""
    return '; '.join(cli(op, i) for i, op in enumerate(ops))


# ─────────── moving customers (pins) ───────────

PIN_FIELDS = set(ls.COPY) | {'src-mac-address', 'src-address', 'comment'}


def check_pin(p):
    """A pin rule as stored in a Link command: {key, ip, fields}. Raises HubError."""
    key = _s(p.get('key'))
    if not re.fullmatch(r'([0-9A-F]{2}(:[0-9A-F]{2}){5}|ip-\d{1,3}(-\d{1,3}){3})', key):
        raise HubError('Bad device.')
    ip = _s(p.get('ip'))
    if not _ipv4(ip):
        raise HubError('Bad device address.')
    f = p.get('fields') or {}
    if not isinstance(f, dict):
        raise HubError('Bad rule.')
    clean = {}
    for k, v in f.items():
        if k not in PIN_FIELDS:
            raise HubError(f'“{k}” cannot be part of a line switch.')
        clean[k] = _clean_val(v)
    if clean:
        if not clean.get('comment', '').startswith(f'{ls.PIN_TAG} {key} '):
            raise HubError('A line switch must carry its own comment.')
        if not ls.MARK_RE.match(clean.get('new-connection-mark') or clean.get('new-routing-mark') or ''):
            raise HubError('A line switch needs a line mark.')
    return {'key': key, 'ip': ip, 'fields': clean}


def pins_body(pins):
    """RouterOS for a batch of moves ({fields} empty = back to automatic). The new rule goes above the first TapTap
    line rule; the device's open connections are closed so it changes line at once."""
    out = [':local an ""; :local hv 0; '
           ':foreach r in=[/ip firewall mangle find where chain=prerouting disabled=no] do={ :if ($hv = 0) do={ :do { '
           + ls.ANCHOR_TEST + '} on-error={} } }']
    for p in pins:
        p = check_pin(p)
        rm = f'/ip firewall mangle remove [find where comment~{_rs("^" + _rx(ls.PIN_TAG + " " + p["key"] + " "))}]'
        flush = (f':foreach c in=[/ip firewall connection find where src-address~{_rs("^" + _rx(p["ip"]) + ":")}] do={{ '
                 ':do { /ip firewall connection remove $c } on-error={} }')
        if p['fields']:
            add = f'/ip firewall mangle add {wan.kv(p["fields"])} place-before=$an'
            out.append(f':if ($hv = 1) do={{ :do {{ {rm}; {add}; {flush} }} on-error={{ :log warning "TapTap line move failed" }} }}')
        else:
            out.append(f':do {{ {rm}; {flush} }} on-error={{}}')
    out.append(':if ($hv = 0) do={ :log warning "TapTap line move: no TapTap line rules on this router" }')
    return '; '.join(out)


def api_pins(svc, pins):
    """Moves over the API: one read of the firewall, one sweep of the connection table."""
    mangle = svc.resource('/ip/firewall/mangle')
    rows = [dict(r) for r in mangle.get()]
    anchor = next((r for r in rows if ls.is_anchor(r)), None)
    done, ips = 0, set()
    for p in pins:
        p = check_pin(p)
        head = f'{ls.PIN_TAG} {p["key"]} '
        for r in rows:
            if _s(r.get('comment')).startswith(head):
                try:
                    mangle.remove(id=_rid(r))
                except Exception:
                    pass
        if p['fields']:
            if anchor is None:
                raise HubError('The router has no TapTap line rules to place the move above — apply the setup in Internet lines again.')
            mangle.add(**_api_kw(p['fields']), place_before=_rid(anchor))
        done += 1
        ips.add(p['ip'])
    moved = 0
    if ips:
        res = svc.resource('/ip/firewall/connection')
        for row in ls._conn_rows(svc, '.id,src-address'):
            if ls._src_ip(row) in ips and moved < ls.FLUSH_MAX * 2:
                try:
                    res.remove(id=row.get('id') or row.get('.id'))
                    moved += 1
                except Exception:
                    pass
    return done, moved


# ─────────── TapTap Link command ───────────

def link_body(params):
    """RouterOS text of one queued line_hub command — re-checked here, never trusted as text."""
    kind = _s(params.get('kind'))
    if kind == 'ops':
        return cli_block(check_ops(params.get('ops')))
    if kind == 'pins':
        pins = params.get('pins') or []
        if not isinstance(pins, list) or not pins or len(pins) > MAX_MOVE:
            raise HubError('Nothing to move.')
        return pins_body(pins)
    raise HubError('Unknown Line Hub command.')


def link_send(router, params, label, user=None):
    from .linkops import send
    link_body(params)                       # check now, not only at delivery
    return send(router, LINK_KIND, params, label=f'Line Hub: {label}'[:200], user=user, minutes=30)


def link_send_pins(router, pins, label, user=None):
    """Moves through TapTap Link, split so each command stays inside the Link batch budget."""
    batches, cur = [], []
    for p in pins:
        trial = cur + [p]
        if cur and len(pins_body(trial)) > LINK_BUDGET:
            batches.append(cur)
            cur = [p]
        else:
            cur = trial
    if cur:
        batches.append(cur)
    for i, b in enumerate(batches):
        link_send(router, {'kind': 'pins', 'pins': b}, f'{label}' + (f' ({i + 1}/{len(batches)})' if len(batches) > 1 else ''), user)
    return len(batches)


# ─────────── running a change ───────────

def run(router, ops, label, user=None, undo=None):
    """Carry out a change the way the router is reached. ``undo``: operations that put things back — with a live
    connection they are installed as an on-router timer first, and removed once TapTap reaches the router again on a
    fresh connection (so a change that cuts TapTap off heals by itself). Returns (message, via_link)."""
    ops = check_ops(ops)
    undo = check_ops(undo) if undo else None
    if on_link(router):
        link_send(router, {'kind': 'ops', 'ops': ops}, label, user)
        return 'Sent through TapTap Link — the router applies it at its next check-in (a few seconds).', True
    from .mikrotik import MikroTikService
    try:
        svc = MikroTikService(router).connect()
    except Exception as exc:
        if getattr(router, 'connection_mode', 'api') == 'agent':
            link_send(router, {'kind': 'ops', 'ops': ops}, label, user)
            return 'Sent through TapTap Link — the router applies it at its next check-in (a few seconds).', True
        raise HubError(f'Could not reach {router.name}: {exc}')
    try:
        if undo:
            sched = svc.resource('/system/scheduler')
            for r in [dict(x) for x in sched.get()]:
                if _s(r.get('name')) == UNDO_SCHED:
                    sched.remove(id=_rid(r))
            body = '; '.join(f':do {{ {cli(o)} }} on-error={{}}' for o in undo)
            sched.add(name=UNDO_SCHED, interval=f'{UNDO_MINUTES}m', on_event=f'{body}; /system scheduler remove [find where name="{UNDO_SCHED}"]',
                      comment=f'{TAG}: puts the last change back unless TapTap confirms')
        n = api_run(svc, ops)
    except HubError:
        raise
    except Exception as exc:
        raise HubError(f'{router.name} refused the change: {exc}' + (' The undo timer puts back anything that was changed.' if undo else ''))
    finally:
        svc.close()
    if undo:
        try:
            svc2 = MikroTikService(router).connect()
            try:
                sched = svc2.resource('/system/scheduler')
                for r in [dict(x) for x in sched.get()]:
                    if _s(r.get('name')) == UNDO_SCHED:
                        sched.remove(id=_rid(r))
            finally:
                svc2.close()
        except Exception:
            return (f'Done, but TapTap could not reach {router.name} again afterwards — the router undoes the change by itself '
                    f'within {UNDO_MINUTES} minutes.'), False
    return f'Done on {router.name}' + (f' ({n} change{"s" if n != 1 else ""}).' if n else '.'), False


def taptap_addresses():
    """Public IPv4 addresses the router talks to for TapTap Link / Tunnel (the site and the tunnel endpoint)."""
    key = 'tt:lh:ttaddr'
    hit = cache.get(key)
    if hit is not None:
        return hit
    from urllib.parse import urlparse
    from django.conf import settings
    hosts = []
    site = getattr(settings, 'SITE_URL', '') or ''
    if site:
        hosts.append(urlparse(site).hostname or '')
    try:
        from .tunnel import tunnel_enabled, wg_endpoint
        if tunnel_enabled():
            ep = wg_endpoint()
            hosts.append(ep.rsplit(':', 1)[0] if ep.count(':') == 1 else ep)
    except Exception:
        pass
    found = []
    for h in [x.strip('[]') for x in hosts if x]:
        ip = _ipv4(h)
        cands = [h] if ip else []
        if not ip:
            try:
                cands = [ai[4][0] for ai in socket.getaddrinfo(h, None, socket.AF_INET)]
            except Exception:
                cands = []
        for c in cands:
            a = _ipv4(c)
            if a and a.is_global and str(a) not in found:
                found.append(str(a))
    try:
        from .self_trust import learned
        for c in learned():
            a = _ipv4(c)
            if a and a.is_global and str(a) not in found:
                found.append(str(a))
    except Exception:
        pass
    found = found[:6]
    cache.set(key, found, 600)
    return found


# ═══════════════════════════ firewall presets ═══════════════════════════

PRESETS = {
    'dns': ('Block DNS requests from the internet',
            'Strangers can no longer use the router as a public DNS server — a common cause of a slow line and of ISP warnings.',
            [{'chain': 'input', 'protocol': 'udp', 'dst-port': '53'}, {'chain': 'input', 'protocol': 'tcp', 'dst-port': '53'}]),
    'legacy': ('Block Telnet and FTP from the internet',
               'Closes two old, unencrypted ways in that are attacked all day. TapTap never uses them.',
               [{'chain': 'input', 'protocol': 'tcp', 'dst-port': '21,23'}]),
    'invalid': ('Drop broken (invalid) connections',
                'Packets that belong to no real connection are dropped — less junk through the router.',
                [{'chain': 'forward', 'connection-state': 'invalid'}, {'chain': 'input', 'connection-state': 'invalid'}]),
}


def preset_comment(key):
    return f'{TAG}: fw {key} - {_plain(PRESETS[key][0], 60)}'


def presets_state(rows):
    have = {}
    for r in rows('/ip/firewall/filter'):
        c = _s(r.get('comment'))
        for k in PRESETS:
            if c == preset_comment(k):
                have.setdefault(k, []).append(not _yes(r.get('disabled')))
    return [{'key': k, 'title': v[0], 'about': v[1], 'on': bool(have.get(k)) and all(have[k])} for k, v in PRESETS.items()]


def preset_ops(key, on, wan_ifaces):
    c = preset_comment(key)
    ops = [{'v': 'remove', 'p': '/ip/firewall/filter', 'w': {'comment^': c}}]
    if not on:
        return ops
    ops += [{'v': 'ensure', 'p': '/interface/list', 'w': {'name': HUB_WAN_LIST}, 'f': {'name': HUB_WAN_LIST, 'comment': f'{TAG}: internet ports'}},
            {'v': 'remove', 'p': '/interface/list/member', 'w': {'list': HUB_WAN_LIST, 'comment^': TAG}}]
    for iface in wan_ifaces:
        ops.append({'v': 'add', 'p': '/interface/list/member', 'f': {'list': HUB_WAN_LIST, 'interface': iface, 'comment': f'{TAG}: internet port'}})
    for rule in PRESETS[key][2]:
        f = {**rule, 'action': 'drop', 'comment': c}
        if 'connection-state' not in rule:
            f['in-interface-list'] = HUB_WAN_LIST
        ops.append({'v': 'add', 'p': '/ip/firewall/filter', 'f': f, 'before': {'chain': rule['chain']}})
    return ops


def filter_rules(rows, limit=80):
    out = []
    for r in rows('/ip/firewall/filter'):
        if _yes(r.get('dynamic')):
            continue
        c = _s(r.get('comment'))
        out.append({'id': _rid(r), 'chain': _s(r.get('chain')), 'action': _s(r.get('action')), 'comment': c[:90],
                    'disabled': _yes(r.get('disabled')), 'protected': bool(re.search(r'taptap', c, re.I)),
                    'what': ' '.join(f'{k}={_s(r.get(k))}' for k in ('protocol', 'dst-port', 'src-address', 'dst-address', 'in-interface',
                                                                     'in-interface-list', 'connection-state', 'src-address-list')
                                     if _s(r.get(k)))[:120],
                    'bytes': int(_s(r.get('bytes')) or 0) if _s(r.get('bytes')).isdigit() else 0})
        if len(out) >= limit:
            break
    return out


# ═══════════════════════════ DHCP servers ═══════════════════════════

LEASE_RE = re.compile(r'^(\d{1,4}w)?(\d{1,4}d)?(\d{1,4}h)?(\d{1,4}m)?(\d{1,4}s)?$|^\d{1,3}:\d{2}:\d{2}$')


def dhcp_servers(rows):
    nets = []
    for n in rows('/ip/dhcp-server/network'):
        try:
            nets.append((ipaddress.ip_network(_s(n.get('address')), strict=False), n))
        except ValueError:
            pass
    iface_nets = {}
    for a in rows('/ip/address'):
        try:
            iface_nets.setdefault(_s(a.get('actual-interface') or a.get('interface')), []).append(ipaddress.ip_interface(_s(a.get('address'))).network)
        except ValueError:
            pass
    pools = {_s(p.get('name')): _s(p.get('ranges')) for p in rows('/ip/pool')}
    leases = {}
    for l in rows('/ip/dhcp-server/lease'):
        leases[_s(l.get('server'))] = leases.get(_s(l.get('server')), 0) + 1
    hs = {_s(h.get('interface')) for h in rows('/ip/hotspot')}
    out = []
    for s in rows('/ip/dhcp-server'):
        iface = _s(s.get('interface'))
        net = next((n for net, n in nets if any(net == x or net.subnet_of(x) for x in iface_nets.get(iface, []) if x.version == net.version)), None)
        out.append({'name': _s(s.get('name')), 'interface': iface, 'pool': _s(s.get('address-pool')),
                    'ranges': pools.get(_s(s.get('address-pool')), ''), 'lease_time': _s(s.get('lease-time')) or '10m',
                    'disabled': _yes(s.get('disabled')), 'network': _s(net.get('address')) if net else '',
                    'gateway': _s(net.get('gateway')) if net else '', 'dns': _s(net.get('dns-server')) if net else '',
                    'leases': leases.get(_s(s.get('name')), 0), 'hotspot': iface in hs})
    return out


# ═══════════════════════════ free ports for a new line ═══════════════════════════

def free_ports(rows, line_list):
    used = {l['interface'] for l in line_list} | {l.get('pppoe_port') for l in line_list if l.get('pppoe_port')}
    bridged = {_s(b.get('interface')) for b in rows('/interface/bridge/port')}
    hs = {_s(h.get('interface')) for h in rows('/ip/hotspot')}
    with_addr = {_s(a.get('actual-interface') or a.get('interface')) for a in rows('/ip/address') if not _yes(a.get('disabled'))}
    servers = {_s(s.get('interface')) for s in rows('/ip/dhcp-server')}
    out = []
    for r in rows('/interface'):
        name, kind = _s(r.get('name')), _s(r.get('type'))
        if not name or name in used or name in bridged or name in hs or name in servers:
            continue
        if kind not in ('ether', 'lte', 'vlan', 'sfp', 'wlan', 'wifi', 'ppp-out') and not name.startswith(('ether', 'sfp', 'lte', 'combo')):
            continue
        if kind == 'ppp-out':
            continue
        out.append({'name': name, 'type': kind or 'ether', 'running': _yes(r.get('running')), 'has_address': name in with_addr,
                    'comment': _s(r.get('comment'))[:40]})
    return out[:40]


# ═══════════════════════════ the whole picture ═══════════════════════════

def live_rates(router, rows, names):
    try:
        return rows.svc.live_traffic(list(names)) or {}
    except Exception:
        return {}


def state(router, rows):
    setup = _setup(router)
    line_list = lines(router, rows, setup)
    if rows.live:
        remember_view(router, rows)
    rates = live_rates(router, rows, [l['interface'] for l in line_list if not l['missing']])
    for l in line_list:
        r = rates.get(l['interface']) or {}
        l['rx_bps'], l['tx_bps'] = int(r.get('rx_bps') or 0), int(r.get('tx_bps') or 0)
    b = board(router, rows, line_list)
    for l in line_list:
        col = next((c for c in b['columns'] if c['interface'] == l['interface']), None)
        l['users'] = col['users'] if col else 0
        l['real'] = col['real'] if col else 0
        l['movable'] = bool(col and col['mark'])
    link_iface, link_ok = link_line_of(rows)
    raw = setup.config or {}
    return {
        'ok': True, 'router': router.name, 'router_id': router.pk, 'via': rows.via, 'live': rows.live,
        'channel': channel(router), 'at': timezone.now().isoformat(),
        'setup': {'status': setup.status, 'strategy': raw.get('strategy') or '', 'strategy_label': wan.STRATEGIES.get(raw.get('strategy') or '', ''),
                  'applied_at': setup.applied_at.isoformat() if setup.applied_at else None},
        'lines': line_list, 'board': b,
        'link': {'interface': link_iface, 'active': link_ok, 'addresses': taptap_addresses(),
                 'mode': getattr(router, 'connection_mode', 'api'), 'channel': channel(router)},
        'firewall': {'presets': presets_state(rows), 'rules': filter_rules(rows)},
        'dhcp': dhcp_servers(rows), 'ports': free_ports(rows, line_list), 'stale': _stale(router, rows),
    }


def _stale(router, rows):
    if rows.live:
        return []
    try:
        from .wan_doctor import link_freshness
        return link_freshness(router)['stale']
    except Exception:
        return []


# ═══════════════════════════ actions ═══════════════════════════

def _line(line_list, iface):
    l = next((x for x in line_list if x['interface'] == iface), None)
    if l is None:
        raise HubError(f'{iface or "That line"} is not one of this router’s internet lines (any more) — refresh the page.')
    return l


IN_USE = ('managed', 'plain', 'standby')


def _others_in_use(line_list, iface):
    return [l for l in line_list if l['interface'] != iface and not l['missing'] and l['use'] in IN_USE and l['up']]


def _forget_in_design(setup, iface):
    cfg = dict(setup.config or {})
    cfg['links'] = [l for l in cfg.get('links') or [] if _s(l.get('interface')) != iface]
    cfg['splits'] = [s for s in cfg.get('splits') or [] if _s(s.get('interface')) != iface]
    w = dict(cfg.get('weights') or {})
    w.pop(iface, None)
    cfg['weights'] = w
    if _s(cfg.get('primary')) == iface:
        cfg['primary'] = ''
    facts = dict(setup.facts or {})
    facts['links'] = [l for l in facts.get('links') or [] if _s(l.get('interface')) != iface]
    setup.config, setup.facts = cfg, facts
    setup.save(update_fields=['config', 'facts', 'updated_at'])


def _remember_in_design(setup, iface, kind, label, speed=None, gateway='', enabled=False):
    cfg = dict(setup.config or {})
    links = [dict(l) for l in cfg.get('links') or []]
    row = next((l for l in links if _s(l.get('interface')) == iface), None)
    if row is None:
        used = {_s(l.get('check')) for l in links}
        check = next((h for h, _ in wan.CHECK_HOSTS if h not in used), wan.CHECK_HOSTS[0][0])
        row = {'interface': iface, 'type': kind, 'gateway': gateway, 'label': label or iface, 'speed': speed or 50,
               'check': check, 'enabled': enabled}
        links.append(row)
    else:
        if label:
            row['label'] = label
        if speed:
            row['speed'] = speed
        if kind == 'static' and gateway:
            row['gateway'] = gateway
    cfg['links'] = links
    setup.config = cfg
    setup.save(update_fields=['config', 'updated_at'])


def act_delete(router, rows, line_list, data, user):
    iface = _s(data.get('interface'))
    l = _line(line_list, iface)
    setup = _setup(router)
    ops = []
    if l['missing']:
        path, key = ('/ip/dhcp-client', 'interface') if l['type'] == 'dhcp' else ('/interface/pppoe-client', 'name')
        ops.append({'v': 'remove', 'p': path, 'w': {key: iface}})
    else:
        if l['use'] not in ('out',) and not l['client_off']:
            raise HubError(f'Customers can still use {l["label"]}. Take it out of use (or switch it off) first — then delete it. '
                           'That way the internet never drops while it goes.')
        if l['carries_link']:
            raise HubError(f'{l["label"]} carries TapTap Link. Choose another line (or “Automatic”) for TapTap first.')
        if l['type'] == 'dhcp':
            ops.append({'v': 'remove', 'p': '/ip/dhcp-client', 'w': {'interface': iface}})
        elif l['type'] == 'pppoe':
            ops.append({'v': 'remove', 'p': '/interface/pppoe-client', 'w': {'name': iface}})
        ops += [{'v': 'remove', 'p': '/ip/address', 'w': {'interface': iface, 'comment^': TAG}},
                {'v': 'remove', 'p': '/ip/firewall/nat', 'w': {'out-interface': iface, 'comment^': TAG}},
                {'v': 'remove', 'p': '/ip/route', 'w': {'comment^': f'{TAG}: backup via {_plain(iface)};'}}]
    msg, via_link = run(router, ops, f'delete {iface}', user)
    _forget_in_design(setup, iface)
    try:
        router.interface_roles.filter(interface_name=iface, role='wan').update(role='unused')
    except Exception:
        pass
    note = (' Its TapTap routes go away the next time you apply the setup (step 3).' if l.get('has_routes') else '')
    return f'{l["label"]} is deleted. {msg}{note}', via_link


def act_add(router, rows, line_list, data, user):
    port, kind = _s(data.get('port')), _s(data.get('type'))
    if kind not in ('dhcp', 'static', 'pppoe'):
        raise HubError('Choose DHCP, static or PPPoE.')
    free = {p['name']: p for p in free_ports(rows, line_list)}
    if port not in free:
        raise HubError(f'{port or "That port"} cannot become an internet line: it is in use (bridge, LAN, HotSpot or already a line).')
    if not wan.IFACE_RE.fullmatch(port):
        raise HubError('Bad port name.')
    label = _plain(data.get('label'), 40) or port
    try:
        speed = max(1, min(10000, int(float(data.get('speed') or 50))))
    except (TypeError, ValueError):
        speed = 50
    setup = _setup(router)
    active = setup.status in ('active', 'pending')
    cm = f'{TAG}: {label}'
    ops, name, gateway = [], port, ''
    if kind == 'dhcp':
        f = {'interface': port, 'add-default-route': 'no' if active else 'yes', 'use-peer-dns': 'no', 'comment': cm, 'disabled': 'no'}
        if not active:
            f['default-route-distance'] = '10'
        ops.append({'v': 'add', 'p': '/ip/dhcp-client', 'f': f})
    elif kind == 'pppoe':
        user_, pw = _s(data.get('user')), str(data.get('password') or '')
        if not user_ or not pw:
            raise HubError('Type the PPPoE user name and password your provider gave you.')
        if len(user_) > 64 or len(pw) > 64 or any(ord(c) < 32 for c in user_ + pw):
            raise HubError('The PPPoE user name or password is too long or has strange characters.')
        name = f'pppoe-{port}'[:30]
        f = {'name': name, 'interface': port, 'user': user_, 'password': pw, 'add-default-route': 'no' if active else 'yes',
             'use-peer-dns': 'no', 'comment': cm, 'disabled': 'no'}
        if not active:
            f['default-route-distance'] = '10'
        ops.append({'v': 'add', 'p': '/interface/pppoe-client', 'f': f})
    else:
        try:
            ipi = ipaddress.ip_interface(_s(data.get('address')))
        except ValueError:
            raise HubError('Type the address with its size, e.g. 192.168.20.2/24.')
        gw = _ipv4(data.get('gateway'))
        if ipi.version != 4 or ipi.network.prefixlen > 30 or gw is None or gw not in ipi.network or gw == ipi.ip:
            raise HubError('The gateway must be another address in the same network, e.g. 192.168.20.1 for 192.168.20.2/24.')
        for a in rows('/ip/address'):
            try:
                if ipaddress.ip_interface(_s(a.get('address'))).network.overlaps(ipi.network):
                    raise HubError(f'{ipi.network} is already used on {_s(a.get("interface"))}.')
            except ValueError:
                continue
        gateway = str(gw)
        ops.append({'v': 'add', 'p': '/ip/address', 'f': {'address': str(ipi), 'interface': port, 'comment': cm}})
        if not active:
            ops.append({'v': 'add', 'p': '/ip/route', 'f': {'dst-address': '0.0.0.0/0', 'gateway': f'{gateway}%{port}', 'distance': '10',
                                                            'check-gateway': 'ping', 'comment': f'{TAG}: backup via {_plain(port)};'}})
    if not nat_for(rows, name)['on'] and not nat_for(rows, port)['on']:
        ops.append({'v': 'add', 'p': '/ip/firewall/nat', 'f': {'chain': 'srcnat', 'out-interface': name, 'action': 'masquerade',
                                                               'comment': f'{TAG}: NAT for {label}'}})
    msg, via_link = run(router, ops, f'new line {label}', user)
    _remember_in_design(setup, name, kind, label, speed, gateway, enabled=False)
    try:
        from .models import RouterInterfaceRole
        RouterInterfaceRole.objects.update_or_create(router=router, interface_name=name, defaults={'role': 'wan', 'label': label})
    except Exception:
        pass
    tail = (' It is connected but carries no customers yet: tick it in step 1 and apply to share customers over it.' if active else
            ' It works as a backup line now; choose how the lines work together in step 2 and apply.')
    return f'{label} ({name}) is a new internet line. {msg}{tail}', via_link


def act_edit(router, rows, line_list, data, user):
    iface = _s(data.get('interface'))
    l = _line(line_list, iface)
    setup = _setup(router)
    label = _plain(data.get('label'), 40)
    speed = None
    if data.get('speed') not in (None, ''):
        try:
            speed = max(1, min(10000, int(float(data['speed']))))
        except (TypeError, ValueError):
            raise HubError('Speed must be a number of Mbps.')
    gateway = ''
    if l['type'] == 'static' and data.get('gateway'):
        if not _ipv4(data['gateway']):
            raise HubError('The gateway must be an IP address.')
        gateway = _s(data['gateway'])
    ops = []
    if l['type'] == 'dhcp' and data.get('use_peer_dns') in ('yes', 'no'):
        ops.append({'v': 'set', 'p': '/ip/dhcp-client', 'w': {'interface': iface}, 'f': {'use-peer-dns': data['use_peer_dns']}})
    if l['type'] == 'pppoe' and (data.get('user') or data.get('password')):
        f = {}
        if data.get('user'):
            f['user'] = _s(data['user'])[:64]
        if data.get('password'):
            pw = str(data['password'])
            if len(pw) > 64 or any(ord(c) < 32 for c in pw):
                raise HubError('The password is too long or has strange characters.')
            f['password'] = pw
        ops.append({'v': 'set', 'p': '/interface/pppoe-client', 'w': {'name': iface}, 'f': f})
    msg, via_link = ('', False)
    if ops:
        msg, via_link = run(router, ops, f'settings of {iface}', user)
    if label or speed or gateway:
        _remember_in_design(setup, iface, l['type'], label, speed, gateway, enabled=l['selected'])
        if label:
            try:
                from .models import RouterInterfaceRole
                RouterInterfaceRole.objects.update_or_create(router=router, interface_name=iface, defaults={'label': label})
            except Exception:
                pass
    need = ' The new speed / gateway is used the next time you apply (step 3).' if (speed or gateway) and l['selected'] else ''
    return f'{label or l["label"]} saved. {msg}{need}'.strip(), via_link


def act_nat(router, rows, line_list, data, user):
    iface = _s(data.get('interface'))
    l = _line(line_list, iface)
    on = bool(data.get('on'))
    if l['missing']:
        raise HubError('This line’s port no longer exists.')
    if on:
        if l['nat']['on']:
            return f'NAT is already on for {l["label"]}.', False
        if any(r['via'] == 'port' and r['disabled'] for r in l['nat']['rules']):
            ops = [{'v': 'enable', 'p': '/ip/firewall/nat', 'w': {'chain': 'srcnat', 'action': 'masquerade', 'out-interface': iface}}]
        else:
            ops = [{'v': 'add', 'p': '/ip/firewall/nat', 'f': {'chain': 'srcnat', 'out-interface': iface, 'action': 'masquerade',
                                                               'comment': f'{TAG}: NAT for {_plain(l["label"])}'}}]
        msg, via = run(router, ops, f'NAT on for {iface}', user)
        return f'Customers are hidden behind {l["label"]} again (NAT on). {msg}', via
    if l['use'] != 'out' and not l['client_off']:
        raise HubError(f'Customers use {l["label"]} right now — without NAT their traffic would not come back and the internet '
                       'would stop for them. Take the line out of use first, or keep NAT on.')
    if any(r['via'] != 'port' and not r['disabled'] for r in l['nat']['rules']):
        raise HubError('This line is covered by a NAT rule for a whole list of ports — change that rule in WinBox.')
    ops = [{'v': 'disable', 'p': '/ip/firewall/nat', 'w': {'chain': 'srcnat', 'action': 'masquerade', 'out-interface': iface}}]
    msg, via = run(router, ops, f'NAT off for {iface}', user,
                   undo=[{'v': 'enable', 'p': '/ip/firewall/nat', 'w': {'chain': 'srcnat', 'action': 'masquerade', 'out-interface': iface}}])
    return f'NAT is off for {l["label"]}. {msg}', via


def act_firewall(router, rows, line_list, data, user):
    key = _s(data.get('preset'))
    if key not in PRESETS:
        raise HubError('Unknown protection.')
    on = bool(data.get('on'))
    wan_ifaces = [l['interface'] for l in line_list if not l['missing'] and wan.IFACE_RE.fullmatch(l['interface'])]
    if on and not wan_ifaces:
        raise HubError('No internet line is known on this router yet.')
    msg, via = run(router, preset_ops(key, on, wan_ifaces), f'firewall {key} {"on" if on else "off"}', user)
    return f'{PRESETS[key][0]}: {"on" if on else "off"}. {msg}', via


def act_rule(router, rows, line_list, data, user):
    rid, on = _s(data.get('id')), bool(data.get('on'))
    if not ID_RE.match(rid):
        raise HubError('Bad rule.')
    row = next((r for r in rows('/ip/firewall/filter') if _rid(r) == rid), None)
    if row is None:
        raise HubError('That rule is not on the router any more — refresh.')
    if re.search(r'taptap', _s(row.get('comment')), re.I):
        raise HubError('TapTap looks after its own rules — change them from the page that made them.')
    what = _s(row.get('comment')) or f'{_s(row.get("chain"))} {_s(row.get("action"))}'
    undo = None
    if not on and _s(row.get('chain')) == 'input':
        undo = [{'v': 'enable', 'p': '/ip/firewall/filter', 'id': rid}]
    if on and _s(row.get('action')) in ('drop', 'reject') and _s(row.get('chain')) == 'input':
        undo = [{'v': 'disable', 'p': '/ip/firewall/filter', 'id': rid}]
    msg, via = run(router, [{'v': 'enable' if on else 'disable', 'p': '/ip/firewall/filter', 'id': rid}],
                   f'firewall rule {"on" if on else "off"}', user, undo=undo)
    return f'Rule “{what[:60]}” {"switched on" if on else "switched off"}. {msg}', via


def act_dhcp(router, rows, line_list, data, user):
    name = _s(data.get('server'))
    srv = next((s for s in dhcp_servers(rows) if s['name'] == name), None)
    if srv is None:
        raise HubError('That DHCP server is not on the router any more — refresh.')
    ops, said = [], []
    lt = _s(data.get('lease_time'))
    if lt and lt != srv['lease_time']:
        if not LEASE_RE.match(lt) or lt in ('', '0s'):
            raise HubError('Lease time looks like 30m, 1h, 12h or 1d.')
        ops.append({'v': 'set', 'p': '/ip/dhcp-server', 'w': {'name': name}, 'f': {'lease-time': lt}})
        said.append(f'lease time {lt}')
    if 'dns' in data and _s(data.get('dns')) != srv['dns']:
        dns = [x for x in re.split(r'[,;\s]+', _s(data.get('dns'))) if x]
        if len(dns) > 3 or any(not _ipv4(x) for x in dns):
            raise HubError('DNS servers are up to three IP addresses, separated by commas.')
        if not srv['network']:
            raise HubError('This server has no DHCP network to give DNS servers to.')
        ops.append({'v': 'set', 'p': '/ip/dhcp-server/network', 'w': {'address': srv['network']}, 'f': {'dns-server': ','.join(dns)}})
        said.append('DNS ' + (', '.join(dns) or 'from the router'))
    if 'enabled' in data and bool(data['enabled']) == srv['disabled']:
        on = bool(data['enabled'])
        if not on and srv['hotspot'] and not data.get('sure'):
            raise HubError(f'{name} gives addresses to the HotSpot on {srv["interface"]}. Switched off, new customers get no address '
                           'and cannot connect. Press again to confirm.')
        ops.append({'v': 'enable' if on else 'disable', 'p': '/ip/dhcp-server', 'w': {'name': name}})
        said.append('switched ' + ('on' if on else 'off'))
    if not ops:
        return 'Nothing changed.', False
    msg, via = run(router, ops, f'DHCP {name}', user)
    return f'{name}: {", ".join(said)}. {msg}', via


def act_link(router, rows, line_list, data, user):
    iface = _s(data.get('interface'))
    ops = [{'v': 'remove', 'p': '/ip/route', 'w': {'comment^': f'{TAG}: TapTap via'}}]
    if not iface:
        msg, via = run(router, ops, 'TapTap on automatic', user)
        return f'TapTap follows the customers’ lines again (automatic). {msg}', via
    l = _line(line_list, iface)
    if l['missing'] or not l['up']:
        raise HubError(f'{l["label"]} is not connected right now, so TapTap cannot be sent through it.')
    if l['use'] == 'out':
        raise HubError(f'{l["label"]} is out of use. Put it in use first.')
    addrs = taptap_addresses()
    if not addrs:
        raise HubError('TapTap could not find its own public address (SITE_URL is not set on the server).')
    setup = _setup(router)
    mode = getattr(router, 'connection_mode', 'api')
    raw, saved, order, norm = _cfg(setup)
    multi = bool(norm) and norm['strategy'] != 'single' and len(norm['links']) > 1 and setup.status in ('active', 'pending')
    if mode != 'agent' and not multi:
        raise HubError('TapTap reaches this router directly over the internet. Choosing its line is only safe once TapTap runs the lines '
                       '(“Backup line” or “Share the load”, applied in step 3) — otherwise its answers could leave by the wrong line.')
    via_check = ''
    if norm and norm.get('health'):
        cl = next((x for x in norm['links'] if x['interface'] == iface), None)
        if cl and l['has_routes']:
            via_check = cl['check']
    if via_check:
        gw, extra = via_check, {'target-scope': '11'}
    elif l['type'] == 'pppoe':
        gw, extra = iface, {}
    else:
        if not _ipv4(l['gateway']):
            raise HubError(f'{l["label"]} has no gateway yet.')
        gw, extra = f'{l["gateway"]}%{iface}', {}
    for a in addrs:
        ops.append({'v': 'add', 'p': '/ip/route', 'f': {'dst-address': f'{a}/32', 'gateway': gw, 'distance': '1', 'check-gateway': 'ping',
                                                       **extra, 'comment': f'{TAG}: TapTap via {_plain(l["label"], 30)} port={iface};'}})
    undo = [{'v': 'remove', 'p': '/ip/route', 'w': {'comment^': f'{TAG}: TapTap via'}}]
    msg, via = run(router, ops, f'TapTap via {iface}', user, undo=undo)
    return (f'TapTap Link and Tunnel now go through {l["label"]}. If that line stops, they move to the others by themselves. {msg}'), via


ACTIONS = {'delete': act_delete, 'add': act_add, 'edit': act_edit, 'nat': act_nat, 'firewall': act_firewall, 'rule': act_rule,
           'dhcp': act_dhcp, 'link': act_link}


# ─────────── moving customers ───────────

def move(router, data, user):
    """Move devices to a line ({'interface' or 'mark'}) or back to automatic ({'auto': true})."""
    from .conn_lab import device_params
    devices = data.get('devices') or []
    if not isinstance(devices, list) or not devices:
        raise HubError('Choose at least one customer.')
    if len(devices) > MAX_MOVE:
        raise HubError(f'At most {MAX_MOVE} customers at once.')
    if not ls.pins_ready():
        raise HubError(ls.MIGRATE_HINT)
    rows = open_rows(router, live=False)
    try:
        setup = _setup(router)
        line_list = lines(router, rows, setup)
        steer, info = steering(router, rows, line_list)
    finally:
        rows.close()
    auto = bool(data.get('auto'))
    lane = None
    if not auto:
        ok, why = movable(router, setup, steer, info)
        if not ok:
            raise HubError(why)
        iface, mark = _s(data.get('interface')), _s(data.get('mark'))
        lane = steer.get(iface) or next((x for x in steer.values() if x['mark'] == mark), None)
        if lane is None:
            raise HubError('Customers cannot be moved to that line — it is not part of TapTap’s line rules (tick it in step 1 and apply).')
        to_line = next((l for l in line_list if steer.get(l['interface']) is lane), None)
        if to_line and (not to_line['up'] or to_line['use'] == 'out'):
            raise HubError(f'{to_line["label"]} is {"out of use" if to_line["use"] == "out" else "not connected"} — customers moved there '
                           'would be carried by the other lines anyway.')
    mark_label = {x['mark']: x['label'] for x in steer.values()}
    pins, recs = [], []
    for d in devices:
        if not isinstance(d, dict):
            continue
        try:
            p = device_params(d.get('ip'), d.get('mac'), d.get('code'))
        except ValueError as exc:
            raise HubError(f'{d.get("code") or d.get("ip") or "A device"}: {exc}')
        key = ls.pin_key(p.get('mac', ''), p['ip'])
        if auto:
            pins.append({'key': key, 'ip': p['ip'], 'fields': {}})
        else:
            pins.append({'key': key, 'ip': p['ip'], 'fields': ls.pin_fields(lane, key, p.get('mac', ''), p['ip'], p['code'])})
        cur = ls.active_pin(router, mac=p.get('mac', ''), ip=p['ip'])
        recs.append((key, p, mark_label.get(cur.mark, cur.label) if cur else _s(d.get('from'))))
    for p in pins:
        check_pin(p)
    label = f'{len(pins)} customer{"s" if len(pins) != 1 else ""} ' + ('back to automatic' if auto else f'to {lane["label"]}')
    if on_link(router):
        n = link_send_pins(router, pins, label, user)
        via_link, moved = True, 0
        msg = ('Sent through TapTap Link' + (f' in {n} parts' if n > 1 else '') + ' — the router moves them at its next check-in.')
    else:
        from .mikrotik import MikroTikService
        try:
            svc = MikroTikService(router).connect()
        except Exception as exc:
            if getattr(router, 'connection_mode', 'api') != 'agent':
                raise HubError(f'Could not reach {router.name}: {exc}')
            n = link_send_pins(router, pins, label, user)
            svc, via_link, moved, msg = None, True, 0, 'Sent through TapTap Link — the router moves them at its next check-in.'
        if svc is not None:
            try:
                done, moved = api_pins(svc, pins)
            finally:
                svc.close()
            via_link = False
            msg = f'Done on {router.name}: {moved} open connections were closed so they change line at once.'
    for key, p, from_label in recs:
        if auto:
            ls.release_pin(router, key, user)
        else:
            rec = ls.record_pin(router, key, p.get('mac', ''), p['ip'], p['code'], lane, from_label, user)
            if not via_link:
                rec.confirmed = True
                rec.save(update_fields=['confirmed'])
    return {'ok': True, 'via_link': via_link, 'moved': moved, 'count': len(pins),
            'message': (f'{len(pins)} customer{"s are" if len(pins) != 1 else " is"} ' +
                        ('back on automatic sharing. ' if auto else f'now kept on {lane["label"]}. ') + msg)}


# ═══════════════════════════ views ═══════════════════════════

def _json_safe(view):
    @wraps(view)
    def inner(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except Http404:
            raise
        except HubError as exc:
            return JsonResponse({'ok': False, 'message': str(exc)}, status=400)
        except ValueError as exc:            # TapTap Link not usable right now, etc.
            return JsonResponse({'ok': False, 'message': str(exc)}, status=409)
        except Exception as exc:  # noqa: BLE001 — shown to the person, logged in full
            logger.exception('line hub view %s failed', view.__name__)
            return JsonResponse({'ok': False, 'message': f'TapTap hit an error here ({type(exc).__name__}). The details are in the server log.'},
                                status=500)
    return inner


def _router(request, pk):
    r = request.user.business.routers.filter(pk=pk).first() if str(pk).isdigit() else None
    if r is None:
        raise Http404
    return r


def _body(request):
    try:
        return json.loads(request.body or b'{}')
    except ValueError:
        return {}


def _audit(request, router, op, fields, ok=True, error=''):
    from .models import RouterConfigChange
    try:
        RouterConfigChange.objects.create(business=router.business, router=router, actor=request.user, resource_path='/taptap/line-hub',
                                          operation=op[:40], fields=fields, status='success' if ok else 'failed', error=error[:2000])
    except Exception:
        logger.info('line hub audit not saved', exc_info=True)


def _log(router, text):
    from .utils import log
    try:
        log(router.business, 'Internet Line Change', f'{router.name}: {text}', router=router)
    except Exception:
        pass


@login_required
@require_GET
@_json_safe
def hub_state(request, pk):
    router = _router(request, pk)
    rows = open_rows(router, live=True)
    try:
        return JsonResponse(state(router, rows))
    finally:
        rows.close()


@login_required
@require_GET
@_json_safe
def hub_pulse(request, pk):
    """Light: the board from cached samples, line speeds — no full read of the router."""
    router = _router(request, pk)
    rows = quick_rows(router)
    try:
        b = board(router, rows)
        names = [c['interface'] for c in b['columns'] if not c['missing']]
        rates, quiet = {}, f'tt:lh:noapi:{router.pk}'
        if on_link(router):
            rates = live_rates(router, rows, names)
        elif names and not cache.get(quiet):
            from .mikrotik import MikroTikService
            try:
                with MikroTikService(router) as svc:
                    rates = svc.live_traffic(names) or {}
            except Exception:
                cache.set(quiet, 1, 60)       # not reachable: don't make every pulse wait for a timeout
        return JsonResponse({'ok': True, 'board': b, 'at': timezone.now().isoformat(),
                             'rates': {k: {'rx_bps': int(v.get('rx_bps') or 0), 'tx_bps': int(v.get('tx_bps') or 0)} for k, v in rates.items()}})
    finally:
        rows.close()


@login_required
@require_POST
@_json_safe
def hub_action(request, pk):
    router = _router(request, pk)
    data = _body(request)
    action = _s(data.get('action'))
    fn = ACTIONS.get(action)
    if fn is None:
        raise HubError('Unknown action.')
    rows = open_rows(router, live=True)
    try:
        if not rows.live and action in ('delete', 'nat', 'link'):
            from .wan_doctor import link_freshness
            stale = link_freshness(router)['stale']
            if stale:
                raise HubError('TapTap’s copy of this router’s ' + ' and '.join(stale) + ' is out of date, so it cannot tell safely '
                               'which line customers use. Press “Refresh from router” and try again in a minute.')
        line_list = lines(router, rows, _setup(router))
        message, via_link = fn(router, rows, line_list, data, request.user)
    except HubError as exc:
        _audit(request, router, f'hub_{action}', {k: v for k, v in data.items() if k != 'password'}, False, str(exc))
        raise
    finally:
        rows.close()
    safe = {k: v for k, v in data.items() if k != 'password'}
    _audit(request, router, f'hub_{action}', safe)
    _log(router, message.split(' Sent through')[0].split(' Done on')[0][:300])
    if via_link:
        try:
            from .linkops import refresh
            refresh(router, request.user)
        except Exception:
            pass
    return JsonResponse({'ok': True, 'message': message, 'via_link': via_link, 'config': _setup(router).config or {}})


@login_required
@require_POST
@_json_safe
def hub_move(request, pk):
    router = _router(request, pk)
    data = _body(request)
    out = move(router, data, request.user)
    _audit(request, router, 'hub_move', {'to': data.get('interface') or data.get('mark') or 'automatic', 'count': out['count']})
    _log(router, out['message'].split(' Sent through')[0].split(' Done on')[0][:300])
    return JsonResponse(out)


@login_required
@require_GET
@_json_safe
def flow_boards(request):
    """Traffic → Internet lines: the board of every router in scope (cached data only — instant)."""
    from .router_scope import scope
    b = request.user.business
    selected, routers = scope(request, b)
    out = []
    for r in ([selected] if selected else routers):
        try:
            bd = board(r)
        except Exception:
            logger.exception('line hub board for router %s', r.pk)
            bd = {'router_id': r.pk, 'router': r.name, 'columns': [], 'unplaced': [], 'online': 0, 'placed': 0, 'can_move': False,
                  'why': 'TapTap could not read this router’s lines — see the server log.', 'pins': []}
        from django.urls import reverse
        bd['design_url'] = reverse('wan_designer', args=[r.pk])
        out.append(bd)
    return JsonResponse({'ok': True, 'routers': out})


@login_required
@require_POST
@_json_safe
def flow_move(request):
    data = _body(request)
    router = _router(request, data.get('router'))
    out = move(router, data, request.user)
    _audit(request, router, 'hub_move', {'to': data.get('interface') or data.get('mark') or 'automatic', 'count': out['count'], 'from': 'traffic'})
    _log(router, out['message'].split(' Sent through')[0].split(' Done on')[0][:300])
    return JsonResponse(out)


URLS = [
    ('routers/<int:pk>/internet/hub/', hub_state, 'line_hub', 'network.manage'),
    ('routers/<int:pk>/internet/hub/pulse/', hub_pulse, 'line_hub_pulse', 'network.manage'),
    ('routers/<int:pk>/internet/hub/action/', hub_action, 'line_hub_action', 'network.manage'),
    ('routers/<int:pk>/internet/hub/move/', hub_move, 'line_hub_move', ('vouchers.support', 'network.manage')),
    ('flow/lines/boards/', flow_boards, 'flow_line_boards', ('vouchers.support', 'network.manage')),
    ('flow/lines/move/', flow_move, 'flow_line_move', ('vouchers.support', 'network.manage')),
]


def install():
    """Called from CoreConfig.ready(): URLs, permissions and the TapTap Link command kind."""
    global _INSTALLED
    if _INSTALLED:
        return
    from django.urls import path
    from . import permissions, urls
    names = {getattr(x, 'name', None) for x in urls.urlpatterns}
    for route, view, name, perm in URLS:
        permissions.URL_PERMS[name] = perm
        if name not in names:
            urls.urlpatterns.append(path(route, view, name=name))
    try:
        from . import agent
        agent.SAFE_KINDS.add(LINK_KIND)
        agent.DEFAULT_EXPIRY.setdefault(LINK_KIND, 30)
        original = agent.command_body

        @wraps(original)
        def command_body(cmd):
            if cmd.kind == LINK_KIND:
                return link_body(cmd.params or {})
            return original(cmd)
        agent.command_body = command_body
    except Exception as exc:
        logger.warning('Line Hub: TapTap Link commands not available (%s)', exc)
    _INSTALLED = True
