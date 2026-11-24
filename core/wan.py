"""Internet Lines designer — single path, failover, load balancing (PCC / ECMP) and split-by-network.

The flow is: detect() what the router has → build_plan(cfg) turns the owner's choices into a list of
RouterOS operations → the same plan is rendered as a .rsc script (render_script) or executed over the
API (apply_plan). Everything TapTap creates carries a comment starting with TAG so it can be found,
replaced on the next apply, and removed by undo — without touching anything the owner built by hand.

Safety: before applying, an on-router scheduler is installed that undoes the whole setup after a few
minutes unless TapTap confirms. If a change cuts TapTap off from the router, the router heals itself.
"""
import ipaddress
import re
import secrets
from math import gcd

from .routeros_analysis import parse_version

TAG = 'TapTap WAN'
UNDO_SCHEDULER = 'TapTap-WAN-undo'
LINK_OFF = ' ~tt-wan-off'          # marks a hand-made default route a TapTap Link apply switched off
CHECK_HOSTS = [('8.8.8.8', 'Google DNS'), ('1.1.1.1', 'Cloudflare'), ('9.9.9.9', 'Quad9'),
               ('208.67.222.222', 'OpenDNS'), ('8.8.4.4', 'Google DNS 2'), ('1.0.0.1', 'Cloudflare 2')]
PRIVATE_NETS = ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10']
# DNS the router is given with two or more lines. A health-check address is pinned to ONE line by its /32
# route, so a router asking 8.8.8.8 for names lost every name when that line's ISP failed behind a live
# modem — customers saw "no internet" although the other line worked. These resolvers are taken in order,
# skipping any address used as a health check; none is an Internet Doctor probe (wan_doctor.PROBE_POOL).
DNS_SERVERS = ['1.0.0.1', '8.8.4.4', '9.9.9.9', '208.67.222.222', '1.1.1.1', '8.8.8.8']
STRATEGIES = {
    'single': 'One line', 'failover': 'Backup line', 'balance': 'Share the load',
    'ecmp': 'Combine lines', 'split': 'Split by network',
}


class WanError(Exception):
    pass


def _s(v):
    return str(v if v is not None else '').strip()


def _is_ip(v):
    try:
        ipaddress.ip_address(_s(v)); return True
    except ValueError:
        return False


def gw_on_port(l):
    """The line's gateway tied to its own port, e.g. 192.168.1.1%ether1.

    Two Starlinks (or any two modems of the same brand) usually hand out the same gateway, 192.168.1.1.
    A bare 192.168.1.1 then resolves through whichever port RouterOS picks first, so both lines' health
    checks and routes silently use the same modem. %port keeps every line on its own port.
    PPPoE lines are already named by their interface."""
    if l.get('type') == 'pppoe':
        return l['gateway']
    return f"{l['gateway']}%{l['interface']}"


def split_servers(value):
    """'8.8.8.8,1.1.1.1' or ['8.8.8.8'] or '8.8.8.8;1.1.1.1' → ['8.8.8.8', '1.1.1.1']."""
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = re.split(r'[,;\s]+', _s(value))
    return [x for x in (_s(i) for i in items) if x]


def dns_plan(links, current):
    """DNS servers for a multi-line setup: two resolvers no line pins, then the owner's own servers that are
    not pinned either. Returns the new list, or None when the current one is already safe."""
    pinned = {l['check'] for l in links if l.get('check')}
    current = [x for x in (current or []) if _is_ip(x)]
    safe_now = [x for x in current if x not in pinned]
    if safe_now and len(safe_now) == len(current):
        return None                                   # nothing in it depends on a single line
    fresh = [h for h in DNS_SERVERS if h not in pinned][:2]
    out = list(dict.fromkeys(fresh + safe_now))
    return out if out != current else None


# DHCP states in which the client still holds its address and the line works. RouterOS renews a lease half-way
# through ("renewing..."), then asks any server ("rebinding..."): many modems never answer the renewal, so a line can
# sit in those states for hours while it carries traffic normally. Only "bound" counted, so such a line showed
# "Not connected", was left out of "Share the load" and customers could not be moved to it.
DHCP_HOLDING = ('bound', 'renewing', 'rebinding')
DHCP_WITHOUT = ('searching', 'requesting', 'stopped', 'error', 'invalid')


def dhcp_status(client, explicit=None):
    """The client's status: its own row, else TapTap Link's one-by-one reading (/ip/dhcp-client/gateways)."""
    return (_s((client or {}).get('status')) or _s((explicit or {}).get('status'))).lower()


def dhcp_has_lease(client, explicit=None):
    """Does this DHCP client hold an address from its modem right now (bound, renewing or rebinding)?
    With no status at all (some RouterOS builds leave it out of `print as-value`) an address counts."""
    st = dhcp_status(client, explicit)
    if st.startswith(DHCP_HOLDING):
        return True
    if st.startswith(DHCP_WITHOUT):
        return False
    return not st and bool(_s((client or {}).get('address')) or _s((explicit or {}).get('gateway')))


def route_live_on(routes, iface):
    """Is a route through ``iface`` active? RouterOS makes a gateway route active only while its port is up,
    so this shows a working line even when a synced copy of /interface lags behind."""
    for r in routes or ():
        if _s(r.get('disabled')).lower() in {'true', 'yes'} or _s(r.get('active')).lower() not in {'true', 'yes'}:
            continue
        for v in (r.get('immediate-gw'), r.get('gateway')):
            v = _s(v)
            if v == iface or ('%' in v and v.split('%')[-1] == iface):
                return True
    return False


def dhcp_gateway(client, explicit=None, routes=()):
    """The gateway a bound DHCP client got. Some RouterOS builds leave it out of `print as-value`, so it is
    looked for, in order: the client row, TapTap Link's explicit reading, the client's own dynamic default
    route, a TapTap route written for this port (…%ether1 / gw=ether1;), and the DHCP server address."""
    iface = _s(client.get('interface'))
    for v in (client.get('gateway'), (explicit or {}).get('gateway')):
        v = _s(v).split('%')[0].split('@')[0]
        if _is_ip(v):
            return v
    for r in routes or ():
        if _s(r.get('dst-address')) != '0.0.0.0/0':
            continue
        gw, on = _s(r.get('gateway')), _s(r.get('vrf-interface') or r.get('immediate-gw')).split('%')[-1]
        if _s(r.get('dhcp')).lower() in {'true', 'yes'} and on == iface and _is_ip(gw.split('%')[0]):
            return gw.split('%')[0]
    for r in routes or ():
        gw, c = _s(r.get('gateway')), _s(r.get('comment'))
        if c.startswith(TAG) and (gw.endswith('%' + iface) or f'gw={iface};' in c) and _is_ip(gw.split('%')[0]):
            return gw.split('%')[0]
    for v in (client.get('dhcp-server'), (explicit or {}).get('server')):
        if _is_ip(_s(v)):
            return _s(v)
    return ''


# ═══════════════════════════ hand-made steering ═══════════════════════════
# A router that had its own load balancing before TapTap (marks such as "starlink2", PCC rules, routing rules into its
# own tables) keeps steering customers itself: those rules come first and mark every new connection, and TapTap's
# sharing only shares connections nobody has marked yet — so "Share the load" changed nothing and the second line
# stayed on standby. When TapTap runs two or more lines, these rules are switched off (marked LINK_OFF, so every
# undo puts them back). Only rules that steer internet traffic are touched: a rule for one destination (a VPN, an
# office network) and every TapTap rule stay as they are.
STEER_CHAINS = {'prerouting', 'output'}


def _row_on(row):
    return _s(row.get('disabled')).lower() not in {'true', 'yes'} and _s(row.get('dynamic')).lower() not in {'true', 'yes'}


def _theirs(row):
    return 'taptap' not in _s(row.get('comment')).lower()


def _internet_bound(row):
    dst, lst = _s(row.get('dst-address')), _s(row.get('dst-address-list'))
    return (not dst or dst == '0.0.0.0/0') and (not lst or lst.startswith('!'))


def steering(mangle, rules):
    """Hand-made rules that steer customers to a line: [(menu, row)] with menu 'mangle' or 'rule'."""
    mangle = [dict(r) for r in mangle or []]
    hand = [r for r in mangle if _row_on(r) and _theirs(r) and _s(r.get('chain')) in STEER_CHAINS]
    marks = {_s(r.get('connection-mark')) for r in hand if _s(r.get('action')) == 'mark-routing' and _internet_bound(r)}
    marks -= {'', 'no-mark'}
    out = []
    for r in hand:
        action = _s(r.get('action'))
        if action == 'mark-routing' and _internet_bound(r):
            out.append(('mangle', r))
        elif action == 'mark-connection' and (_s(r.get('per-connection-classifier')) or _s(r.get('new-connection-mark')) in marks):
            out.append(('mangle', r))
    for r in rules or []:
        r = dict(r)
        table = _s(r.get('table'))
        if _row_on(r) and _theirs(r) and table and table != 'main' and _internet_bound(r) and not _s(r.get('dst-address')):
            out.append(('rule', r))
    return out


def steering_text(found):
    """One sentence for the preview."""
    if not found:
        return ''
    names = sorted({_s(r.get('new-routing-mark') or r.get('new-connection-mark') or r.get('table')) for _m, r in found} - {''})
    return (f'{len(found)} hand-made rule(s) on the router steer customers themselves' + (f' ({", ".join(names[:4])})' if names else '')
            + ' — they come before TapTap’s sharing, so it would change nothing. They are switched off; Undo puts them back.')


IFACE_RE = re.compile(r'[A-Za-z0-9_.\-<>@ ]{1,60}')


def is_orphan(iface, interfaces=None):
    """A DHCP / PPPoE client whose port no longer exists. RouterOS then shows the port as "*2" (its old id).
    Such a client can never carry traffic; Internet lines offers to delete it instead of failing with
    "“*2” is not a valid interface name"."""
    iface = _s(iface)
    if re.fullmatch(r'\*[0-9A-Fa-f]{1,8}', iface):
        return True
    return bool(interfaces) and iface not in interfaces


def safe_name(iface):
    return re.sub(r'[^A-Za-z0-9_-]', '-', _s(iface))[:40] or 'wan'


# ═══════════════════════════ detection ═══════════════════════════
def detect(svc):
    """What the router has today: candidate internet lines, LAN networks, version, current method."""
    ver = svc.ros_version()
    dhcp = [dict(r) for r in svc.dhcp_clients()]
    pppoe = [dict(r) for r in svc.safe_get('/interface/pppoe-client')]
    lte = {_s(r.get('name')) for r in svc.safe_get('/interface/lte')}
    addresses = [dict(r) for r in svc.addresses()]
    interfaces = {_s(r.get('name')): dict(r) for r in svc.interfaces()}
    analysis = svc.analyze_load_balancing()
    roles = {x.interface_name: x for x in svc.router.interface_roles.all()} if getattr(svc.router, 'pk', None) else {}

    links, seen = [], set()

    def add(iface, kind, gateway, status='', address='', client=None):
        if not iface or iface in seen:
            return
        missing = kind in {'dhcp', 'pppoe'} and is_orphan(iface, interfaces)
        if not missing and (not IFACE_RE.fullmatch(iface) or (kind == 'static' and interfaces and iface not in interfaces)):
            return                       # not a port (e.g. a route gateway that could not be resolved to a port)
        seen.add(iface)
        client = client or {}
        row = interfaces.get(iface, {})
        role = roles.get(iface)
        links.append({
            'interface': iface, 'type': kind, 'gateway': gateway, 'status': 'port missing' if missing else status, 'address': address,
            'missing': missing,
            'running': False if missing else _s(row.get('running', 'true')).lower() in {'true', 'yes'},
            'label': (role.label if role and role.label else '') or _s(row.get('comment')) or ('4G / LTE' if iface in lte else ''),
            'lte': iface in lte,
            # current client settings, so an undo script can be produced even without API access later
            'add_default_route': _s(client.get('add-default-route')) or ('yes' if kind in {'dhcp', 'pppoe'} else ''),
            'client_script': '' if TAG in _s(client.get('script')) else _s(client.get('script')),
        })

    routes = [dict(r) for r in svc.safe_get('/ip/route')]
    explicit = {_s(r.get('interface')): r for r in svc.safe_get('/ip/dhcp-client/gateways') if r.get('interface')}
    for r in dhcp:
        if _s(r.get('disabled')).lower() in {'true', 'yes'}:
            continue
        iface = _s(r.get('interface'))
        add(iface, 'dhcp', dhcp_gateway(r, explicit.get(iface), routes), dhcp_status(r, explicit.get(iface)), _s(r.get('address')), r)
    for r in pppoe:
        if _s(r.get('disabled')).lower() in {'true', 'yes'}:
            continue
        add(_s(r.get('name')), 'pppoe', _s(r.get('name')), 'connected' if _s(r.get('running')).lower() in {'true', 'yes'} else 'disconnected', '', r)
    for l in analysis.get('wan_links', []):
        gw = l.get('gateway', '')
        if l.get('interface') and TAG not in _s(l.get('label')):
            add(l['interface'], 'static', gw if _is_ip(gw) else '', l.get('state', ''))
    for name, role in roles.items():
        if role.role == 'wan':
            add(name, 'static', '', 'no route yet')

    wan_names = {l['interface'] for l in links}
    lans = []
    for a in addresses:
        iface = _s(a.get('actual-interface') or a.get('interface'))
        if not iface or iface in wan_names or _s(a.get('disabled')).lower() in {'true', 'yes'}:
            continue
        try:
            net = ipaddress.ip_interface(_s(a.get('address'))).network
        except ValueError:
            continue
        if net.is_private and net.prefixlen < 31:
            lans.append({'interface': iface, 'network': str(net), 'label': (roles[iface].label if iface in roles and roles[iface].label else iface)})
    hotspot = [dict(r) for r in svc.hotspot_servers()]
    for lan in lans:
        if any(_s(h.get('interface')) == lan['interface'] for h in hotspot):
            lan['hotspot'] = True
            lan['label'] = lan['label'] + ' (HotSpot)'
    dns_rows = svc.safe_get('/ip/dns')
    dns_servers = split_servers(dns_rows[0].get('servers')) if dns_rows else []
    try:
        steer = steering(svc.safe_get('/ip/firewall/mangle'), svc.safe_get('/routing/rule' if ver[0] >= 7 else '/ip/route/rule'))
    except Exception:
        steer = []
    return {'version': '.'.join(map(str, ver)), 'v7': ver[0] >= 7, 'links': links, 'lans': lans,
            'steering': [{'menu': m, 'comment': _s(r.get('comment'))[:60], 'action': _s(r.get('action') or 'lookup'),
                          'mark': _s(r.get('new-routing-mark') or r.get('new-connection-mark') or r.get('table'))} for m, r in steer][:20],
            'hotspot': bool(hotspot), 'analysis': analysis, 'dns_servers': dns_servers,
            'check_hosts': [{'ip': ip, 'label': lab} for ip, lab in CHECK_HOSTS]}


# ═══════════════════════════ weights ═══════════════════════════
def speed_weights(speeds, max_slots=12):
    """Best small-integer ratio for line speeds, e.g. [50, 20] -> [5, 2]."""
    speeds = [max(0.1, float(s or 1)) for s in speeds]
    if len(speeds) < 2:
        return [1] * len(speeds)
    total = sum(speeds); best = None
    for n in range(len(speeds), max_slots + 1):
        w = [max(1, round(s / total * n)) for s in speeds]
        err = sum(abs(wi / sum(w) - s / total) for wi, s in zip(w, speeds))
        if best is None or err < best[0] - 1e-9:
            best = (err, w)
    w = best[1]; g = 0
    for x in w: g = gcd(g, x)
    return [x // g for x in w]


def interleave(weights):
    """Smooth weighted round-robin: [3,2] -> [0,1,0,1,0] so buckets spread evenly."""
    cur = [0] * len(weights); total = sum(weights); out = []
    for _ in range(total):
        for i, w in enumerate(weights): cur[i] += w
        i = max(range(len(weights)), key=lambda k: cur[k]); cur[i] -= total; out.append(i)
    return out


# ═══════════════════════════ validation ═══════════════════════════
def normalize(cfg, facts):
    """Clean the browser's config and collect problems. Returns (cfg, errors, warnings)."""
    errors, warnings = [], []
    strategy = cfg.get('strategy') if cfg.get('strategy') in STRATEGIES else 'failover'
    links = []
    used_hosts = set()
    for raw in cfg.get('links') or []:
        if not raw.get('enabled', True):
            continue
        iface = _s(raw.get('interface'))
        if iface and is_orphan(iface):
            errors.append(f'“{iface}” is a DHCP client whose port no longer exists — untick it, or delete it from the line card.'); continue
        if not iface or not IFACE_RE.fullmatch(iface):
            errors.append(f'“{iface or "?"}” is not a valid interface name.'); continue
        kind = raw.get('type') if raw.get('type') in {'dhcp', 'static', 'pppoe'} else 'static'
        gw = _s(raw.get('gateway'))
        if kind == 'pppoe':
            gw = gw or iface
        elif not _is_ip(gw):
            errors.append(f'{raw.get("label") or iface}: ' + ('it has no gateway yet — connect the line (DHCP must be bound) or type the gateway IP.' if kind == 'dhcp' else 'type the gateway IP address.'))
        host = _s(raw.get('check')) or next((h for h, _ in CHECK_HOSTS if h not in used_hosts), '')
        if not _is_ip(host):
            errors.append(f'{iface}: the health-check address must be an IP address.')
        elif host in used_hosts:
            errors.append(f'Each line needs its own health-check address ({host} is used twice).')
        used_hosts.add(host)
        try: speed = max(0.1, float(raw.get('speed') or 10))
        except (TypeError, ValueError): speed = 10
        links.append({'interface': iface, 'type': kind, 'gateway': gw, 'check': host, 'speed': speed,
                      'label': _s(raw.get('label'))[:40] or iface, 'id': safe_name(iface)})
    if not links:
        errors.append('Choose at least one internet line.')
    if strategy != 'single' and len(links) < 2:
        errors.append(f'“{STRATEGIES[strategy]}” needs two or more lines. Add another line or pick “One line”.')
    if strategy == 'single':
        primary = _s(cfg.get('primary')) or (links[0]['interface'] if links else '')
        links = [l for l in links if l['interface'] == primary][:1] or links[:1]
    weights = cfg.get('weights') or {}
    if strategy == 'balance':
        auto = speed_weights([l['speed'] for l in links])
        for l, w in zip(links, auto):
            try: l['weight'] = max(1, min(12, int(weights.get(l['interface']) or w)))
            except (TypeError, ValueError): l['weight'] = w
        if sum(l['weight'] for l in links) > 24:
            errors.append('Traffic shares are too fine-grained — keep the total under 24 parts.')
    splits = []
    if strategy == 'split':
        names = {l['interface'] for l in links}
        for sp in cfg.get('splits') or []:
            net, line = _s(sp.get('network')), _s(sp.get('interface'))
            if not net or not line:
                continue
            try:
                net = str(ipaddress.ip_network(net, strict=False))
            except ValueError:
                errors.append(f'“{net}” is not a valid network (use the form 10.5.50.0/24).'); continue
            if line not in names:
                errors.append(f'{net} is sent to a line that is not selected.'); continue
            splits.append({'network': net, 'interface': line, 'label': _s(sp.get('label'))[:40]})
        if not splits:
            errors.append('Send at least one network to a specific line.')
    sticky = 'src-address' if cfg.get('sticky', 'customer') == 'customer' else 'both-addresses-and-ports'
    health = bool(cfg.get('health', True))
    if not health and strategy != 'single':
        warnings.append('Without health checks a line is only dropped when its modem stops answering — an ISP outage behind a working modem will not be noticed.')
    gws = {}
    for l in links:
        if l['type'] != 'pppoe' and l.get('gateway'):
            gws.setdefault(l['gateway'], []).append(l['label'])
    for gw, labels in gws.items():
        if len(labels) > 1:
            warnings.append(f"{' and '.join(labels)} both use the modem address {gw}. That works: TapTap ties every route "
                            f"to its port ({gw}%port), so each line keeps its own modem.")
    if strategy == 'ecmp':
        warnings.append('Combining sends each connection down any line, so a customer can appear from two IP addresses. Banking sites and some logins dislike that — “Share the load” keeps each customer on one line.')
    if strategy in {'balance', 'split', 'ecmp'} and facts.get('hotspot'):
        warnings.append('HotSpot is running: login pages and walled-garden traffic stay on the router, so they are not affected. Customers keep their line for the whole session with “Each customer stays on one line”.')
    return {'strategy': strategy, 'links': links, 'splits': splits, 'sticky': sticky, 'health': health,
            'lans': facts.get('lans', [])}, errors, warnings


# ═══════════════════════════ plan ═══════════════════════════
def build_plan(cfg, facts, run=None):
    """Turn a normalized config into ordered operations. Each op renders as RouterOS script
    and executes over the API. Order: clear old routing → lists → tables → routes → marks →
    rules → NAT → clean leftovers → hand clients over."""
    v7 = bool(facts.get('v7', True))
    run = run or secrets.token_hex(3)
    c = lambda text: f'{TAG} #{run} · {text}'
    links, strategy, health = cfg['links'], cfg['strategy'], cfg['health']
    multi = len(links) > 1 and strategy != 'single'
    B = {k: [] for k in ('lists', 'tables', 'routes', 'mangle', 'filter', 'rules', 'nat', 'clients')}
    table_of = lambda l: f'TT-{l["id"]}'
    # Every line's own table carries the other lines as fallbacks (not only with "Share the load"): a customer moved
    # to a line by hand (core/line_steer.py, Line Hub) is routed through that line's table, and must keep the internet
    # through another line when theirs fails instead of losing it until someone moves them back.
    use_tables = strategy in {'balance', 'split', 'failover', 'ecmp'}
    gwm = lambda l: l['interface'] if l['type'] == 'dhcp' else None

    def table(name, note):
        if v7 and not any(o['key']['name'] == name for o in B['tables']):
            B['tables'].append({'op': 'ensure', 'path': '/routing/table', 'key': {'name': name}, 'fields': {'name': name, 'fib': '', 'comment': c(note)}, 'group': 'tables'})

    def route(dst, gw, *, tbl=None, distance=None, scope=None, target=None, check=False, note='', gw_marker=None):
        f = {'dst-address': dst, 'gateway': gw}
        if distance: f['distance'] = str(distance)
        if scope: f['scope'] = str(scope)
        if target: f['target-scope'] = str(target)
        if check: f['check-gateway'] = 'ping'
        if tbl: f['routing-table' if v7 else 'routing-mark'] = tbl
        f['comment'] = c(note + (f' gw={gw_marker};' if gw_marker else ''))
        B['routes'].append({'op': 'add', 'path': '/ip/route', 'fields': f, 'group': 'routes'})

    def default_via(l, distance, tbl=None, note=''):
        # With health checks each line reaches the internet "through" its own check host (recursive route):
        # if that host stops answering through this line, the route goes inactive and traffic moves on.
        if health:
            route(f'{l["check"]}/32', gw_on_port(l), tbl=tbl, scope=10, note=f'check {l["check"]} via {l["label"]}', gw_marker=gwm(l))
            route('0.0.0.0/0', l['check'], tbl=tbl, distance=distance, target=11, check=True, note=note)
        else:
            route('0.0.0.0/0', gw_on_port(l), tbl=tbl, distance=distance, check=l['type'] != 'pppoe', note=note, gw_marker=gwm(l))

    mangle = lambda f, note: B['mangle'].append({'op': 'add', 'path': '/ip/firewall/mangle', 'fields': {**f, 'comment': c(note)}, 'group': 'mangle'})

    # ── lists ──
    if multi:
        B['lists'].append({'op': 'ensure', 'path': '/interface/list', 'key': {'name': 'TT-WAN'}, 'fields': {'name': 'TT-WAN', 'comment': c('internet ports')}, 'group': 'lists'})
        for l in links:
            B['lists'].append({'op': 'ensure', 'path': '/interface/list/member', 'key': {'list': 'TT-WAN', 'interface': l['interface']},
                               'fields': {'list': 'TT-WAN', 'interface': l['interface'], 'comment': c(l['label'])}, 'group': 'lists'})
        nets = list(dict.fromkeys(PRIVATE_NETS + [x['network'] for x in cfg.get('lans', [])]))
        for net in nets:
            B['lists'].append({'op': 'ensure', 'path': '/ip/firewall/address-list', 'key': {'list': 'TT-local', 'address': net},
                               'fields': {'list': 'TT-local', 'address': net, 'comment': c('local network — never balanced')}, 'group': 'lists'})

    # ── main table ──
    if strategy == 'single':
        default_via(links[0], 1, note=f'only line: {links[0]["label"]}')
    elif strategy == 'ecmp':
        if v7:   # v7: equal-distance routes form an ECMP group
            for l in links:
                default_via(l, 1, note=f'combined: {l["label"]}')
        else:    # v6: one route listing every gateway
            for l in links:
                if health:
                    route(f'{l["check"]}/32', gw_on_port(l), scope=10, note=f'check {l["check"]} via {l["label"]}', gw_marker=gwm(l))
            route('0.0.0.0/0', ','.join(l['check'] if health else gw_on_port(l) for l in links), distance=1,
                  target=11 if health else None, check=True, note='combined lines')
    else:        # failover chain — also the safety net for balance / split
        for i, l in enumerate(links):
            default_via(l, i + 1, note=f'{"main" if i == 0 else "backup " + str(i)} line: {l["label"]}')

    # ── per-line tables ──
    if multi:
        for l in links:
            t = table_of(l)
            table(t, f'via {l["label"]}')
            if use_tables:     # own line first, the others as fallbacks
                for i, x in enumerate([l] + [o for o in links if o is not l]):
                    default_via(x, i + 1, tbl=t, note=f'{t}: {"own line" if i == 0 else "fallback " + str(i)} {x["label"]}')
            else:              # failover / ecmp: table only carries replies for connections that arrived on this line
                route('0.0.0.0/0', gw_on_port(l), tbl=t, distance=1, check=l['type'] != 'pppoe', note=f'{t}: replies via {l["label"]}', gw_marker=gwm(l))

    # ── marks ──
    if multi:
        mangle({'chain': 'prerouting', 'dst-address-list': 'TT-local', 'action': 'accept'}, 'leave local traffic alone')
        for l in links:
            # connection-state=new: only connections that START on this line (port forwards, remote WinBox) are tied to it.
            # Without it, the first reply to one of the router's OWN connections (TapTap Tunnel, TapTap Link check-in,
            # DNS) marked that connection too, so the router's traffic stayed pinned to a line after it failed and
            # TapTap lost the router while customers were still online through the other line.
            mangle({'chain': 'prerouting', 'in-interface': l['interface'], 'connection-state': 'new', 'connection-mark': 'no-mark',
                    'action': 'mark-connection', 'new-connection-mark': f'TT-{l["id"]}', 'passthrough': 'yes'}, f'arrived on {l["label"]}')
        if strategy == 'balance':
            slots = interleave([l['weight'] for l in links]); n = len(slots)
            for k, idx in enumerate(slots):
                l = links[idx]
                mangle({'chain': 'prerouting', 'in-interface-list': '!TT-WAN', 'connection-mark': 'no-mark', 'dst-address-type': '!local',
                        'per-connection-classifier': f'{cfg["sticky"]}:{n}/{k}', 'action': 'mark-connection',
                        'new-connection-mark': f'TT-{l["id"]}', 'passthrough': 'yes'}, f'share {k + 1} of {n} → {l["label"]}')
        for l in links:
            mangle({'chain': 'prerouting', 'connection-mark': f'TT-{l["id"]}', 'in-interface-list': '!TT-WAN', 'action': 'mark-routing',
                    'new-routing-mark': table_of(l), 'passthrough': 'no'}, f'route via {l["label"]}')
            mangle({'chain': 'output', 'connection-mark': f'TT-{l["id"]}', 'action': 'mark-routing',
                    'new-routing-mark': table_of(l), 'passthrough': 'no'}, f'router replies via {l["label"]}')

        # ── keep marked connections out of FastTrack ──
        # FastTrack skips the whole mangle table for every packet after the first few of a connection. The
        # connection mark survives, but the ROUTING mark is per packet, so a FastTracked packet loses it and is
        # routed by the main table — where the first line has distance 1. Result: “Share the load” set to 50/50
        # still sent almost every byte down the first line while the second sat idle. An accept for this
        # line's established traffic, placed just above the router's fasttrack rule, keeps it on the slow path
        # where the routing mark is applied. Only established/related traffic is accepted here, so new
        # connections still meet every drop rule (e.g. “drop all from WAN not DSTNATed”) as before.
        if strategy == 'balance':
            for l in links:
                B['filter'].append({'op': 'fasttrack_guard', 'path': '/ip/firewall/filter',
                                    'fields': {'chain': 'forward', 'action': 'accept', 'connection-state': 'established,related',
                                               'connection-mark': f'TT-{l["id"]}',
                                               'comment': c(f'no fasttrack: keep {l["label"]} share on its line')},
                                    'group': 'filter'})

    # ── split rules ──
    if strategy == 'split':
        rule_path = '/routing/rule' if v7 else '/ip/route/rule'
        for net in PRIVATE_NETS:
            B['rules'].append({'op': 'add', 'path': rule_path, 'fields': {'dst-address': net, 'action': 'lookup-only-in-table', 'table': 'main', 'comment': c('local stays local')}, 'group': 'rules'})
        by_id = {l['interface']: l for l in links}
        for sp in cfg['splits']:
            l = by_id[sp['interface']]
            B['rules'].append({'op': 'add', 'path': rule_path, 'fields': {'src-address': sp['network'], 'action': 'lookup', 'table': table_of(l),
                               'comment': c(f'{sp.get("label") or sp["network"]} → {l["label"]}')}, 'group': 'rules'})

    # ── NAT ──
    for l in links:
        B['nat'].append({'op': 'ensure_nat', 'path': '/ip/firewall/nat', 'interface': l['interface'],
                         'fields': {'chain': 'srcnat', 'out-interface': l['interface'], 'action': 'masquerade', 'comment': c(f'hide customers behind {l["label"]}')}, 'group': 'nat'})

    # ── hand DHCP / PPPoE default routes over to TapTap ──
    selected = {l['interface'] for l in links}
    for l in links:
        if l['type'] == 'dhcp':
            B['clients'].append({'op': 'client', 'path': '/ip/dhcp-client', 'interface': l['interface'], 'fields': {'add-default-route': 'no'}, 'script': dhcp_script(l['interface']), 'group': 'clients'})
        elif l['type'] == 'pppoe':
            B['clients'].append({'op': 'client', 'path': '/interface/pppoe-client', 'interface': l['interface'], 'fields': {'add-default-route': 'no'}, 'group': 'clients'})
    for other in facts.get('links', []):
        if other['interface'] not in selected and other['type'] in {'dhcp', 'pppoe'}:
            path = '/ip/dhcp-client' if other['type'] == 'dhcp' else '/interface/pppoe-client'
            B['clients'].append({'op': 'client', 'path': path, 'interface': other['interface'], 'fields': {'add-default-route': 'no'}, 'group': 'clients',
                                 'note': 'not selected — stays connected but carries no customer traffic'})
    if multi:
        B['clients'].append({'op': 'disable_steering', 'group': 'clients',
                             'note': 'hand-made marks / routing rules would send customers past TapTap’s lines'})
    B['clients'].append({'op': 'disable_static_defaults', 'group': 'clients'})
    if health:
        # A hand-made 8.8.8.8/32 next to TapTap's would make RouterOS send the health checks out through either
        # modem, so a dead line could look healthy (and both lines could ride one modem). Switched off, undoable.
        B['clients'].append({'op': 'disable_conflicting_hosts', 'hosts': [l['check'] for l in links], 'group': 'clients'})

    if multi and health:
        servers = dns_plan(links, facts.get('dns_servers'))
        if servers:
            B['dns'] = [{'op': 'dns', 'path': '/ip/dns', 'servers': servers, 'group': 'dns',
                         'note': 'names keep working when any one line fails'}]

    ops = [{'op': 'purge', 'phase': 'early', 'run': run, 'group': 'cleanup'}]
    for k in ('lists', 'tables', 'routes', 'mangle', 'filter', 'rules', 'nat', 'dns'):
        ops += B.get(k, [])
    ops.append({'op': 'purge', 'phase': 'late', 'run': run, 'group': 'cleanup'})
    ops += B['clients']
    return {'run': run, 'ops': ops, 'v7': v7}


def dhcp_script(iface):
    return (f':if ($bound=1) do={{/ip route set [find where comment~"^{TAG}" and comment~"gw={iface};"] '
            f'gateway=($"gateway-address" . "%{iface}")}} # {TAG}')


# ═══════════════════════════ script rendering ═══════════════════════════
def q(v):
    """Quote a value for RouterOS CLI."""
    v = str(v)
    if v == '':
        return ''
    if re.fullmatch(r'[A-Za-z0-9_.:/!\-,]+', v):
        return v
    return '"' + v.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$') + '"'


def cli_path(path):
    return '/' + ' '.join(path.strip('/').split('/'))


def kv(fields):
    return ' '.join(k if v == '' else f'{k}={q(v)}' for k, v in fields.items())


def render_script(plan, cfg, facts, business_name=''):
    v7 = plan['v7']
    lines = [f'# TapTap Internet Lines — {STRATEGIES[cfg["strategy"]]}',
             f'# {business_name} · RouterOS {"v7" if v7 else "v6"} · run #{plan["run"]}',
             '# Tip: press Ctrl+X in the terminal first (Safe Mode) — if you lose the connection, the router undoes everything.',
             '', '# 1. Remove any earlier TapTap setup']
    lines += purge_lines(v7)
    group_titles = {'lists': '2. Lists', 'tables': '3. Routing tables', 'routes': '4. Routes', 'mangle': '5. Traffic marking',
                    'filter': '5b. Keep shared traffic out of FastTrack (otherwise one line takes it all)',
                    'rules': '6. Network rules', 'nat': '7. NAT', 'dns': '8. DNS that never depends on one line',
                    'clients': '9. Hand default routes (and any hand-made steering rules) over to TapTap'}
    last = None
    for op in plan['ops']:
        g = op.get('group')
        if g != last and g in group_titles:
            lines += ['', f'# {group_titles[g]}']; last = g
        lines += op_lines(op, v7)
    lines += ['', '# Done. Check: /ip route print where dst-address=0.0.0.0/0']
    return '\n'.join(lines) + '\n'


def op_lines(op, v7, run=None):
    """RouterOS CLI lines for one plan operation.

    With ``run`` (TapTap Link apply: the new setup goes in BEFORE the old one is removed, so the router is
    never without a route between check-ins) two things change: a row that already exists is re-tagged with
    this run instead of being left alone — otherwise the final clean-up of older runs deleted the WAN interface
    list, the local-network list and the routing tables — and TapTap's own NAT of an older run does not count
    as "already masqueraded", or the clean-up removed the only NAT of the line and customers had no internet.
    """
    p = cli_path(op.get('path', ''))
    kind = op['op']
    if kind == 'add':
        return [f'{p} add {kv(op["fields"])}']
    if kind == 'ensure':
        key = ' and '.join(f'{k}={q(v)}' for k, v in op['key'].items())
        if run:
            return [f':do {{ :local x [{p} find where {key}]; :if ([:len $x]=0) do={{{p} add {kv(op["fields"])}}} '
                    f'else={{{p} set $x comment={q(op["fields"]["comment"])}}} }}']
        return [f':if ([:len [{p} find where {key}]]=0) do={{{p} add {kv(op["fields"])}}}']
    if kind == 'ensure_nat':
        i = q(op['interface'])
        mine = f' and !(comment~"^{TAG}")' if run else ''
        return [f':if ([:len [{p} find where chain=srcnat and out-interface={i} and action=masquerade and disabled=no{mine}]]=0) '
                f'do={{{p} add {kv(op["fields"])}}}']
    if kind == 'client':
        extra = dict(op['fields'])
        if op.get('script'):
            extra['script'] = op['script']
        if op['path'] == '/ip/dhcp-client':
            return [f'{p} set [find where interface={q(op["interface"])}] {kv(extra)}']
        return [f'{p} set [find where name={q(op["interface"])}] {kv(extra)}']
    if kind == 'dns':
        return [f'/ip dns set servers={",".join(op["servers"])}']
    if kind == 'fasttrack_guard':
        # Only when the router FastTracks at all; placed just above the first active fasttrack rule.
        return [f':do {{ :local ft [{p} find where action=fasttrack-connection disabled=no]; '
                f':if ([:len $ft] > 0) do={{ {p} add {kv(op["fields"])} place-before=[:pick $ft 0] }} }} on-error={{}}']
    if kind == 'disable_conflicting_hosts':
        out = []
        for h in op['hosts']:
            where = f'dst-address={h}/32 and static and disabled=no and !(comment~"^TapTap")'
            if run:
                out.append(f':foreach r in=[/ip route find where {where}] do={{ /ip route set $r disabled=yes '
                           f'comment=([:tostr [/ip route get $r comment]] . "{LINK_OFF}") }}')
            else:
                out.append(f'/ip route disable [find where {where}]')
        return out
    if kind == 'disable_steering':
        m, rule = '/ip firewall mangle', ('/routing rule' if v7 else '/ip route rule')
        tt = '"[Tt][Aa][Pp][Tt][Aa][Pp]"'
        # $b = 1 when the rule is for internet traffic (no destination, or only "not these destinations")
        look = (f':local c [:tostr [{m} get $r comment]]; :local d [:tostr [{m} get $r dst-address]]; '
                f':local l [:tostr [{m} get $r dst-address-list]]; :local b 0; '
                ':if (([:len $d] = 0 || $d = "0.0.0.0/0") && ([:len $l] = 0 || [:pick $l 0 1] = "!")) do={ :set b 1 }; ')
        return ['; '.join([      # one line: TapTap Link cuts a setup into parts at line ends, and $ttcm must reach the next loop
            ':local ttcm [:toarray ""]',
            f':foreach r in=[{m} find where action=mark-routing and disabled=no and dynamic=no] do={{ {look}'
            f':if (!($c ~ {tt}) && $b = 1) do={{ :local k [:tostr [{m} get $r connection-mark]]; '
            ':if ([:len $k] > 0 && $k != "no-mark") do={ :set ttcm ($ttcm, $k) } } }',
            f':foreach r in=[{m} find where disabled=no and dynamic=no and (chain=prerouting or chain=output)] do={{ {look}'
            f':local a [:tostr [{m} get $r action]]; :local h 0; :if (!($c ~ {tt})) do={{ '
            ':if ($a = "mark-routing" && $b = 1) do={ :set h 1 }; '
            f':if ($a = "mark-connection") do={{ :if ([:len [:tostr [{m} get $r per-connection-classifier]]] > 0) do={{ :set h 1 }}; '
            f':if ([:typeof [:find $ttcm [:tostr [{m} get $r new-connection-mark]]]] = "num") do={{ :set h 1 }} }} }}; '
            f':if ($h = 1) do={{ {m} set $r disabled=yes comment=($c . "{LINK_OFF}") }} }}',
            f':foreach r in=[{rule} find where disabled=no] do={{ :local c [:tostr [{rule} get $r comment]]; '
            f':local t [:tostr [{rule} get $r table]]; :if (!($c ~ {tt}) && [:len $t] > 0 && $t != "main" && '
            f'[:len [:tostr [{rule} get $r dst-address]]] = 0) do={{ {rule} set $r disabled=yes comment=($c . "{LINK_OFF}") }} }}',
        ])]
    if kind == 'disable_static_defaults':
        table = 'routing-table=main' if v7 else '!routing-mark'
        if run:   # marked, so the Link undo can put exactly these back
            return [f':foreach r in=[/ip route find where dst-address=0.0.0.0/0 and static and disabled=no and '
                    f'!(comment~"^{TAG}") and {table}] do={{ /ip route set $r disabled=yes '
                    f'comment=([:tostr [/ip route get $r comment]] . "{LINK_OFF}") }}']
        return [f'/ip route disable [find where dst-address=0.0.0.0/0 and static and !(comment~"^{TAG}") and {table}]']
    return []


def purge_lines(v7, keep_run=None):
    cond = f'comment~"^{TAG}"' + (f' and !(comment~"#{keep_run} ")' if keep_run else '')
    paths = ['/ip firewall mangle', '/ip firewall filter', '/routing rule' if v7 else '/ip route rule', '/ip route', '/ip firewall nat',
             '/ip firewall address-list', '/interface list member', '/interface list'] + (['/routing table'] if v7 else [])
    return [f'{p} remove [find where {cond}]' for p in paths]


def render_undo(original, v7, guarded=True):
    """RouterOS script that restores the router to how it was before TapTap touched its WAN setup.
    Every command is guarded so one failure can't stop the restore; the timer removes itself last,
    so if anything did fail the whole undo simply runs again at the next interval."""
    cmds = purge_lines(v7)
    for cl in (original or {}).get('clients', []):
        path = '/ip dhcp-client' if cl['path'] == '/ip/dhcp-client' else '/interface pppoe-client'
        key = 'interface' if cl['path'] == '/ip/dhcp-client' else 'name'
        f = {'add-default-route': cl.get('add-default-route') or 'yes'}
        if cl['path'] == '/ip/dhcp-client':
            f['script'] = cl.get('script', '')
        cmds.append(f'{path} set [find where {key}={q(cl["interface"])}] ' + ' '.join(f'{k}={q(v) if v != "" else chr(34) * 2}' for k, v in f.items()))
    for rid in (original or {}).get('disabled_routes', []):
        cmds.append(f'/ip route enable [find where .id={rid}]')
    mark = LINK_OFF.strip()        # a rule that had no comment carries only the mark (no space before it)
    for p in ('/ip firewall mangle', '/routing rule' if v7 else '/ip route rule'):
        cmds.append(f':foreach r in=[{p} find where comment~"{mark}"] do={{ :local c [:tostr [{p} get $r comment]]; '
                    f':local i [:find $c "{LINK_OFF}"]; :if ([:typeof $i] != "num") do={{ :set i [:find $c "{mark}"] }}; '
                    f'{p} set $r disabled=no comment=[:pick $c 0 $i] }}')
    if (original or {}).get('dns_servers') is not None:
        cmds.append(f'/ip dns set servers={q(",".join(original["dns_servers"])) or chr(34) * 2}')
    if guarded:
        cmds = [f':do {{ {c} }} on-error={{}}' for c in cmds]
        cmds.append(f'/system scheduler remove [find where name="{UNDO_SCHEDULER}"]')
    return '\n'.join(cmds)


# ═══════════════════════════ API execution ═══════════════════════════
def _api_fields(fields):
    return {str(k).replace('-', '_'): v for k, v in fields.items()}


def _find(res, **where):
    out = []
    for row in res.get():
        if all(_s(row.get(k)) == _s(v) for k, v in where.items()):
            out.append(row)
    return out


def _tagged(res, keep_run=None):
    rows = []
    for row in res.get():
        cm = _s(row.get('comment'))
        if cm.startswith(TAG) and not (keep_run and f'#{keep_run} ' in cm):
            rows.append(row)
    return rows


def original_from_facts(facts):
    """Best-effort 'before' state from detection (used for the downloadable undo script)."""
    clients = []
    for l in facts.get('links', []):
        if l.get('type') == 'dhcp':
            clients.append({'path': '/ip/dhcp-client', 'interface': l['interface'], 'add-default-route': l.get('add_default_route') or 'yes', 'script': l.get('client_script', '')})
        elif l.get('type') == 'pppoe':
            clients.append({'path': '/interface/pppoe-client', 'interface': l['interface'], 'add-default-route': l.get('add_default_route') or 'yes'})
    return {'clients': clients, 'disabled_routes': [], 'dns_servers': list(facts.get('dns_servers') or [])}


def capture_original(svc, cfg, facts):
    """Remember client settings and hand-made default routes so undo can put them back."""
    clients = []
    names = {l['interface'] for l in facts.get('links', [])}
    for r in svc.safe_get('/ip/dhcp-client'):
        if _s(r.get('interface')) in names:
            clients.append({'path': '/ip/dhcp-client', 'interface': _s(r.get('interface')), 'add-default-route': _s(r.get('add-default-route')) or 'yes',
                            'script': _s(r.get('script')).split(f' # {TAG}')[0] if TAG not in _s(r.get('script')) else ''})
    for r in svc.safe_get('/interface/pppoe-client'):
        if _s(r.get('name')) in names:
            clients.append({'path': '/interface/pppoe-client', 'interface': _s(r.get('name')), 'add-default-route': _s(r.get('add-default-route')) or 'yes'})
    dns_rows = svc.safe_get('/ip/dns')
    return {'clients': clients, 'disabled_routes': [],
            'dns_servers': split_servers(dns_rows[0].get('servers')) if dns_rows else list(facts.get('dns_servers') or [])}


def apply_plan(svc, plan, facts, original, undo_minutes=5, log=None):
    """Execute the plan over the API. Installs the on-router undo timer first (if undo_minutes)."""
    log = log or (lambda *_: None)
    v7 = plan['v7']; run = plan['run']; done = []
    sched = svc.resource('/system/scheduler')
    for row in _find(sched, name=UNDO_SCHEDULER):
        sched.remove(id=row['id'])
    if undo_minutes:
        sched.add(name=UNDO_SCHEDULER, interval=f'{int(undo_minutes)}m', on_event=render_undo(original, v7),
                  comment=f'{TAG} #{run} · automatic undo unless confirmed in TapTap')
        done.append(f'Safety timer: automatic undo in {undo_minutes} minutes unless you confirm')
    counts = {}
    for op in plan['ops']:
        kind = op['op']
        try:
            if kind == 'add':
                svc.resource(op['path']).add(**_api_fields(op['fields']))
            elif kind == 'ensure':
                res = svc.resource(op['path']); rows = _find(res, **op['key'])
                if rows:
                    res.set(id=rows[0]['id'], comment=op['fields']['comment'])
                else:
                    try:
                        res.add(**_api_fields(op['fields']))
                    except Exception:
                        if 'fib' not in op['fields']:
                            raise
                        res.add(**_api_fields({**op['fields'], 'fib': 'yes'}))
            elif kind == 'ensure_nat':
                res = svc.resource(op['path'])
                have = [r for r in res.get(chain='srcnat') if _s(r.get('out-interface')) == op['interface'] and _s(r.get('action')) == 'masquerade'
                        and _s(r.get('disabled')).lower() not in {'true', 'yes'} and not _s(r.get('comment')).startswith(TAG)]
                if not have:
                    res.add(**_api_fields(op['fields']))
            elif kind == 'purge':
                # early: old routing/marks go first (a second without routes is fine); late: unused lists/tables.
                paths = (['/ip/firewall/mangle', '/ip/firewall/filter', '/routing/rule' if v7 else '/ip/route/rule', '/ip/route', '/ip/firewall/nat'] if op['phase'] == 'early'
                         else ['/ip/firewall/address-list', '/interface/list/member', '/interface/list'] + (['/routing/table'] if v7 else []))
                for path in paths:
                    try: res = svc.resource(path); rows = _tagged(res, keep_run=run)
                    except Exception: continue
                    for row in rows:
                        try: res.remove(id=row['id'])
                        except Exception: pass
            elif kind == 'client':
                res = svc.resource(op['path'])
                key = 'interface' if op['path'] == '/ip/dhcp-client' else 'name'
                for row in _find(res, **{key: op['interface']}):
                    f = dict(op['fields'])
                    if op.get('script'):
                        prev = _s(row.get('script'))
                        prev = '' if TAG in prev else prev
                        f['script'] = op['script'] + ('\n' + prev if prev else '')
                    res.set(id=row['id'], **_api_fields(f))
            elif kind == 'fasttrack_guard':
                res = svc.resource(op['path'])
                ft = next((r for r in res.get() if _s(r.get('action')) == 'fasttrack-connection'
                           and _s(r.get('disabled')).lower() not in {'true', 'yes'}), None)
                if ft:     # no FastTrack on this router → nothing can skip the marks, nothing to add
                    res.add(**_api_fields(op['fields']), place_before=ft['id'])
            elif kind == 'dns':
                res = svc.resource('/ip/dns')
                res.call('set', {'servers': ','.join(op['servers'])})
            elif kind == 'disable_conflicting_hosts':
                res = svc.resource('/ip/route')
                wanted = {f'{h}/32' for h in op['hosts']}
                for row in res.get():
                    if _s(row.get('dst-address')) not in wanted or _s(row.get('comment')).startswith('TapTap'):
                        continue
                    if _s(row.get('dynamic')).lower() in {'true', 'yes'} or _s(row.get('disabled')).lower() in {'true', 'yes'}:
                        continue
                    res.set(id=row['id'], disabled='yes')
                    original.setdefault('disabled_routes', []).append(row['id'])
                if original.get('disabled_routes') and undo_minutes:
                    for row in _find(sched, name=UNDO_SCHEDULER):
                        sched.set(id=row['id'], on_event=render_undo(original, v7))
            elif kind == 'disable_steering':
                paths = {'mangle': '/ip/firewall/mangle', 'rule': '/routing/rule' if v7 else '/ip/route/rule'}
                found = steering(svc.resource(paths['mangle']).get(), _safe_rows(svc, paths['rule']))
                for menu, row in found:
                    svc.resource(paths[menu]).set(id=row['id'], disabled='yes', comment=_s(row.get('comment')) + LINK_OFF)
                if found:
                    done.append(steering_text(found))
            elif kind == 'disable_static_defaults':
                res = svc.resource('/ip/route')
                for row in res.get():
                    if _s(row.get('dst-address')) not in {'0.0.0.0/0'} or _s(row.get('comment')).startswith(TAG):
                        continue
                    if _s(row.get('dynamic')).lower() in {'true', 'yes'} or _s(row.get('disabled')).lower() in {'true', 'yes'}:
                        continue
                    table = _s(row.get('routing-table') or row.get('routing-mark') or 'main')
                    if table != 'main':
                        continue
                    res.set(id=row['id'], disabled='yes')
                    original.setdefault('disabled_routes', []).append(row['id'])
                if original.get('disabled_routes') and undo_minutes:
                    for row in _find(sched, name=UNDO_SCHEDULER):   # refresh undo script with the routes we disabled
                        sched.set(id=row['id'], on_event=render_undo(original, v7))
            counts[op.get('group', 'other')] = counts.get(op.get('group', 'other'), 0) + 1
        except Exception as exc:
            raise WanError(f'Stopped while applying {op.get("path", kind)} ({op.get("fields", {}).get("comment", "")}): {exc}. '
                           + ('The safety timer will undo the partial change automatically.' if undo_minutes else 'Use “Undo” to remove the partial change.')) from exc
    return {'counts': counts, 'notes': done}


def _safe_rows(svc, path):
    try:
        return svc.resource(path).get()
    except Exception:
        return []


def restore_steering(svc, v7):
    """Undo over the API: the hand-made steering rules this setup switched off are switched on again."""
    n = 0
    for path in ('/ip/firewall/mangle', '/routing/rule' if v7 else '/ip/route/rule'):
        try:
            res = svc.resource(path)
            rows = res.get()
        except Exception:
            continue
        mark = LINK_OFF.strip()
        for row in rows:
            c = str(row.get('comment') or '')
            if mark in c:
                try:
                    res.set(id=row['id'], disabled='no', comment=c.split(LINK_OFF)[0] if LINK_OFF in c else c.split(mark)[0]); n += 1
                except Exception:
                    pass
    return n


def confirm(svc):
    sched = svc.resource('/system/scheduler')
    rows = _find(sched, name=UNDO_SCHEDULER)
    for row in rows:
        sched.remove(id=row['id'])
    return bool(rows)


def undo(svc, original, v7):
    """Remove every TapTap WAN item and restore the original client settings / routes."""
    removed = 0
    confirm(svc)
    for path in ['/ip/firewall/mangle', '/ip/firewall/filter', '/routing/rule' if v7 else '/ip/route/rule', '/ip/route', '/ip/firewall/nat',
                 '/ip/firewall/address-list', '/interface/list/member', '/interface/list'] + (['/routing/table'] if v7 else []):
        try:
            res = svc.resource(path)
        except Exception:
            continue
        for row in _tagged(res):
            try: res.remove(id=row['id']); removed += 1
            except Exception: pass
    for cl in (original or {}).get('clients', []):
        res = svc.resource(cl['path']); key = 'interface' if cl['path'] == '/ip/dhcp-client' else 'name'
        for row in _find(res, **{key: cl['interface']}):
            f = {'add_default_route': cl.get('add-default-route') or 'yes'}
            if cl['path'] == '/ip/dhcp-client':
                f['script'] = cl.get('script', '')
            res.set(id=row['id'], **f)
    routes = svc.resource('/ip/route')
    for rid in (original or {}).get('disabled_routes', []):
        try: routes.set(id=rid, disabled='no')
        except Exception: pass
    restore_steering(svc, v7)
    return removed


def undo_pending(svc):
    return bool(_find(svc.resource('/system/scheduler'), name=UNDO_SCHEDULER))


# ═══════════════════════════ health ═══════════════════════════
def ping(svc, address, count=3, **extra):
    try:
        params = {'address': address, 'count': str(count), **{k.replace('_', '-'): str(v) for k, v in extra.items()}}
        rows = svc.api.get_binary_resource('/').call('ping', {k: v.encode() for k, v in params.items()})
        rows = [{(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v) for k, v in dict(r).items()} for r in rows]
        last = rows[-1] if rows else {}
        sent, recv = int(last.get('sent', count) or count), int(last.get('received', 0) or 0)
        rtt = _s(last.get('avg-rtt', '')).replace('ms', ' ms')
        return {'ok': recv > 0, 'loss': round((sent - recv) * 100 / sent) if sent else 100, 'rtt': rtt}
    except Exception as exc:
        return {'ok': None, 'error': str(exc)[:120]}


def health(svc, cfg):
    """Per line: is its TapTap route active, and does its check host answer?"""
    routes = [dict(r) for r in svc.routes()]
    out = []
    for l in cfg.get('links', []):
        mine = [r for r in routes if _s(r.get('comment')).startswith(TAG) and _s(r.get('dst-address')) == '0.0.0.0/0'
                and (_s(r.get('gateway')) in {l['check'], l['gateway'], gw_on_port(l)} or l['check'] in _s(r.get('gateway')).split(','))
                and _s(r.get('routing-table') or r.get('routing-mark') or 'main') == 'main']
        active = any(_s(r.get('active')).lower() in {'true', 'yes'} for r in mine)
        p = ping(svc, l['check'])
        out.append({'interface': l['interface'], 'label': l['label'], 'route_active': active, 'has_route': bool(mine), 'ping': p})
    return out
