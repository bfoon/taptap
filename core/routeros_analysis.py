"""Pure (no network, no Django) analysis of RouterOS configuration rows.

Everything here takes plain dicts as returned by ``routeros_api`` (or stored
in ``RouterConfigSnapshot.sections``) so it can be unit-tested and re-run
cheaply on every telemetry poll.

Designed to survive every common RouterOS layout:

* RouterOS v6 (``gateway-status`` / ``routing-mark``) and v7
  (``immediate-gw`` / ``routing-table`` / ``/routing/rule``)
* gateways given as an IP, an interface (``pppoe-out1``, ``lte1``) or
  ``ip%interface``
* DHCP-client and PPPoE dynamic default routes
* recursive routes (``check-gateway``, target-scope) and ECMP multi-gateway
* PCC, policy routing, ECMP, failover (distance) and bonding
"""
import ipaddress
import re

IPV4_RE = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}$')
# v6 gateway-status: "192.168.8.1 reachable via  ether3", "1.1.1.1 recursive via 192.168.8.1 ether3", "1.1.1.1 unreachable"
STATUS_RE = re.compile(r'(\S+)\s+(reachable|unreachable|recursive)(?:\s+via\s+(\S+)(?:\s+(\S+))?)?', re.I)
MAX_RECURSION = 4
# Bump whenever the analysis changes: saved analyses made by older code are redone (core/wan_analysis.py).
# 2 = recursive gateways (1.1.1.1 / 8.8.8.8 check hosts) resolve to the real ISP port.
ANALYSIS_VERSION = 4
# 4 = no guessing when two ports share a modem subnet (two modems on 192.168.1.1): TapTap's gw=<port>; marker,
#     DHCP gateways, ARP and the saved Internet Lines setup decide; an unresolved check host says why.
MARKER_RE = re.compile(r'\bgw=([^;\s]+);')
# 3 = lines held out of use (Internet Doctor / owner) are shown as such; DNS that depends on one line is reported.
HELD_RE = re.compile(r'~(doctor|owner)-off:(\S+)\s*$')
PCC_RE = re.compile(r':\s*(\d+)\s*/\s*(\d+)')
DEFAULT_DST = {'0.0.0.0/0', '::/0'}


def g(row, *keys, default=''):
    """Read a RouterOS field accepting dash, underscore and dotted variants."""
    for key in keys:
        for variant in (key, key.replace('-', '_'), '.' + key):
            if variant in row and row[variant] not in (None, ''):
                return row[variant]
    return default


def truthy(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'yes', 'true', '1', 'on', 'running', 'enabled'}


def is_ip(value):
    return bool(IPV4_RE.match(str(value or '').strip())) or (':' in str(value or '') and '%' not in str(value or ''))


def _strip(value):
    return str(value or '').strip().strip('<>').strip()


def parse_version(text):
    """'7.15.3 (stable)' -> (7, 15, 3). Unknown -> (0, 0, 0)."""
    nums = re.findall(r'\d+', str(text or ''))[:3]
    nums = [int(n) for n in nums] + [0] * (3 - len(nums))
    return tuple(nums)


def interface_index(interfaces):
    out = {}
    for row in interfaces or []:
        name = _strip(g(row, 'name'))
        if name:
            out[name] = row
    return out


def address_networks(addresses):
    """[(network, interface)] from /ip/address rows, point-to-point aware."""
    nets = []
    for row in addresses or []:
        if truthy(g(row, 'disabled', default='no')) or truthy(g(row, 'invalid', default='no')):
            continue
        iface = _strip(g(row, 'actual-interface', 'interface'))
        addr = _strip(g(row, 'address'))
        if not iface or not addr:
            continue
        try:
            nets.append((ipaddress.ip_interface(addr).network, iface))
        except ValueError:
            pass
        peer = _strip(g(row, 'network'))  # PPP / point-to-point peer address
        if peer:
            try:
                nets.append((ipaddress.ip_network(peer + ('' if '/' in peer else '/32'), strict=False), iface))
            except ValueError:
                pass
    return nets


def port_for_ip(ip, networks=None, dhcp_gateways=None, arp=None):
    """(interface, candidates) for a next-hop IP. Never guesses: when the IP sits in a subnet that two ports
    share (two modems both on 192.168.1.1), interface is '' and candidates lists the ports."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return '', []
    dhcp = set(_as_set((dhcp_gateways or {}).get(ip)))
    if len(dhcp) == 1:
        return next(iter(dhcp)), []
    best, cands = -1, set()
    for net, net_iface in networks or []:
        if addr.version == net.version and addr in net:
            if net.prefixlen > best:
                best, cands = net.prefixlen, {net_iface}
            elif net.prefixlen == best:
                cands.add(net_iface)
    pool = dhcp if len(dhcp) > 1 else cands
    if len(pool) == 1:
        return next(iter(pool)), []
    seen = set(_as_set((arp or {}).get(ip)))
    if pool:
        both = pool & seen
        if len(both) == 1:
            return next(iter(both)), []
    elif len(seen) == 1:
        return next(iter(seen)), []              # gateway outside every subnet (LTE passthrough): ARP knows
    return '', sorted(pool)


def _as_set(v):
    if not v:
        return set()
    return set(v) if isinstance(v, (set, list, tuple)) else {v}


def arp_index(rows):
    """{ip: {interface}} from /ip/arp (complete entries only)."""
    out = {}
    for row in rows or []:
        ip, iface = _strip(g(row, 'address')), _strip(g(row, 'interface'))
        if not ip or not iface or truthy(g(row, 'invalid', default='no')) or truthy(g(row, 'disabled', default='no')):
            continue
        if not g(row, 'mac-address') and not truthy(g(row, 'complete', default='yes')):
            continue
        out.setdefault(ip, set()).add(iface)
    return out


def resolve_gateways(route, networks=None, dhcp_gateways=None, interfaces=None, arp=None):
    """Return [{'gateway','interface','reachable','candidates'}] for one route row."""
    networks = networks or []
    dhcp_gateways = dhcp_gateways or {}
    interfaces = interfaces or {}
    marker = MARKER_RE.search(str(g(route, 'comment')))
    marker = marker.group(1) if marker and (not interfaces or marker.group(1) in interfaces) else ''
    gateway_field = str(g(route, 'gateway'))
    immediate_map = {}
    for piece in str(g(route, 'immediate-gw')).split(','):
        piece = _strip(piece)
        if not piece:
            continue
        if '%' in piece:
            ip, iface = piece.split('%', 1)
            immediate_map[ip.strip()] = iface.strip()
        else:
            immediate_map.setdefault('*', piece)
    status_map = {}
    for chunk in str(g(route, 'gateway-status')).split(','):
        match = STATUS_RE.search(chunk.strip())
        if match:
            via, last = match.group(3) or '', match.group(4) or ''
            iface = last or ('' if is_ip(via) else via)      # "recursive via 192.168.8.1 ether3" → ether3
            status_map[match.group(1)] = (iface, match.group(2).lower() != 'unreachable')

    # ``reachable`` means "not known to be dead". An inactive v7 backup route is
    # normal in failover, so only an explicit 'unreachable' or a down interface
    # marks a gateway dead.
    results = []
    pieces = [p for p in (_strip(x) for x in gateway_field.split(',')) if p]
    if not pieces and immediate_map:
        pieces = list(immediate_map.keys())
    for piece in pieces:
        ip, iface = piece, ''
        if '%' in piece:
            ip, iface = [x.strip() for x in piece.split('%', 1)]
        if ip == '*':
            ip, iface = '', immediate_map['*']
        elif not is_ip(ip):
            iface, ip = ip, ''  # interface used directly as gateway
        reachable = True
        if not iface and ip in immediate_map:
            iface = immediate_map[ip]
        if ip in status_map:
            via, reachable = status_map[ip]
            iface = iface or via
        elif iface in status_map:
            reachable = status_map[iface][1]
        if not iface and len(pieces) == 1 and '*' in immediate_map:
            iface = immediate_map['*']
        if not iface and len(pieces) == 1 and len(immediate_map) == 1:
            # v7 recursive route: gateway=1.1.1.1, immediate-gw=192.168.8.1%ether3 — the next hop decides the port
            iface = next(iter(immediate_map.values()))
        candidates = []
        if not iface and ip and marker and len(pieces) == 1:
            iface = marker                       # TapTap wrote this route for that port ("… gw=ether3;")
        if not iface and ip:
            iface, candidates = port_for_ip(ip, networks, dhcp_gateways, arp)
        if iface and iface in interfaces and not truthy(g(interfaces[iface], 'running', default='yes')):
            reachable = False
        results.append({'gateway': ip, 'interface': iface, 'reachable': bool(reachable), 'candidates': candidates})
    return results


def _route_table(route):
    return str(g(route, 'routing-table', 'routing-mark', default='main') or 'main')


def _route_source(route, iface, interfaces):
    if truthy(g(route, 'dhcp', default='no')) or 'dhcp' in str(g(route, 'comment')).lower():
        return 'dhcp'
    itype = str(g(interfaces.get(iface, {}), 'type')).lower()
    if 'pppoe' in itype or iface.startswith('pppoe'):
        return 'pppoe'
    if 'lte' in itype or iface.startswith('lte'):
        return 'lte'
    if truthy(g(route, 'dynamic', default='no')):
        return 'dynamic'
    return 'static'


def analyze_wan(routes=None, mangle=None, bonding=None, routing_tables=None, routing_rules=None,
                addresses=None, dhcp_clients=None, interfaces=None, declared_wans=None, hotspot_servers=None, dns=None,
                arp=None, hints=None):
    """hints: {check host IP: interface} from the owner's saved Internet Lines setup — used only when the
    router's own data cannot tell which port a check host leaves through."""
    """Full multi-WAN / load-balancing picture. Always returns a complete dict."""
    routes = [dict(r) for r in routes or []]
    # Lines whose default routes are switched off by the Internet Doctor or by the owner ("out of use").
    held = {}
    for r in routes:
        m = HELD_RE.search(str(g(r, 'comment')))
        if m and truthy(g(r, 'disabled', default='no')) and str(g(r, 'dst-address')) in DEFAULT_DST:
            held[m.group(2)] = m.group(1)
    mangle = [dict(r) for r in mangle or [] if not truthy(g(r, 'disabled', default='no'))]
    bonding = [dict(r) for r in bonding or [] if not truthy(g(r, 'disabled', default='no'))]
    routing_tables = [dict(r) for r in routing_tables or []]
    routing_rules = [dict(r) for r in routing_rules or [] if not truthy(g(r, 'disabled', default='no'))]
    iface_rows = interface_index(interfaces)
    networks = address_networks(addresses)
    dhcp_gateways = {}                   # gateway IP → {interfaces}: two modems may hand out the same gateway
    for row in dhcp_clients or []:
        gw = _strip(g(row, 'gateway')).split('%')[0]
        if gw and g(row, 'interface') and is_ip(gw):
            dhcp_gateways.setdefault(gw, set()).add(_strip(g(row, 'interface')))
    arp_map = arp_index(arp)
    hints = {str(k): str(v) for k, v in (hints or {}).items() if k and v}
    notes = {}                           # check host → why its port is unknown / how it was decided

    def usable(r):
        return (not truthy(g(r, 'disabled', default='no'))
                and str(g(r, 'type', default='unicast')).lower() not in {'blackhole', 'unreachable', 'prohibit'}
                and not truthy(g(r, 'blackhole', default='no')))

    defaults = [r for r in routes if str(g(r, 'dst-address')) in DEFAULT_DST and usable(r)]
    specific = []        # (network, route) for every non-default route: what a recursive gateway resolves through
    for r in routes:
        dst = _strip(g(r, 'dst-address'))
        if dst and dst not in DEFAULT_DST and usable(r):
            try:
                specific.append((ipaddress.ip_network(dst, strict=False), r))
            except ValueError:
                pass

    def recurse(ip, depth=0, seen=()):
        iface, hop = _recurse(ip, depth, seen)
        if not iface and depth == 0 and hints.get(ip) and (not iface_rows or hints[ip] in iface_rows):
            cands = notes.get(ip, {}).get('candidates') or []
            if not cands or hints[ip] in cands:
                notes.setdefault(ip, {})['hint'] = hints[ip]
                return hints[ip], hop
        return iface, hop

    def _recurse(ip, depth=0, seen=()):
        """Interface and next hop that a recursive gateway (e.g. 1.1.1.1 in the classic two-ISP setup) really
        leaves through: longest matching host/network route, active and lowest distance first."""
        if depth >= MAX_RECURSION or not ip or ip in seen:
            return '', ''
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return '', ''
        best = None
        for net, r in specific:
            if addr.version == net.version and addr in net:
                rank = (net.prefixlen, truthy(g(r, 'active', default='true')),
                        -int(re.sub(r'\D', '', str(g(r, 'distance', default='1'))) or 1))
                if best is None or rank > best[0]:
                    best = (rank, r)
        if not best:
            if depth == 0:
                notes.setdefault(ip, {})['why'] = f'there is no route for {ip} itself (e.g. {ip}/32 via the modem)'
            return '', ''
        for hop in resolve_gateways(best[1], networks, dhcp_gateways, iface_rows, arp_map):
            if hop['interface']:
                return hop['interface'], hop['gateway']
            if hop.get('candidates'):
                notes.setdefault(seen[0] if seen else ip, {}).update(
                    candidates=hop['candidates'], gateway=hop['gateway'],
                    why=f"its route goes to {hop['gateway']}, which is the modem on {' and on '.join(hop['candidates'])}")
                return '', hop['gateway']
            if hop['gateway']:
                iface, _ = _recurse(hop['gateway'], depth + 1, seen + (ip,))
                if iface:
                    return iface, hop['gateway']
                notes.setdefault(seen[0] if seen else ip, {}).setdefault(
                    'why', f"its route goes to {hop['gateway']}, which is not on any port of this router")
        return '', ''

    wans = {}
    order = []

    def wan_for(iface, gateway):
        key = iface or ('gw:' + gateway if gateway else 'unknown')
        if key not in wans:
            row = iface_rows.get(iface, {})
            wans[key] = {
                'id': re.sub(r'[^A-Za-z0-9_-]', '_', key), 'interface': iface, 'gateways': [], 'tables': [],
                'distances': [], 'active': False, 'reachable': False, 'check_gateway': '', 'source': 'static',
                'running': truthy(g(row, 'running', default='yes')) if row else True,
                'comment': str(g(row, 'comment')), 'ecmp_weight': 0, 'pcc_rules': 0, 'route_count': 0,
                'declared': False, 'next_hops': [], 'recursive': False,
            }
            order.append(key)
        return wans[key]

    for route in defaults:
        table = _route_table(route)
        distance = int(re.sub(r'\D', '', str(g(route, 'distance', default='1'))) or 1)
        active = truthy(g(route, 'active', default='true'))
        for hop in resolve_gateways(route, networks, dhcp_gateways, iface_rows, arp_map):
            next_hop = ''
            if hop['gateway'] and (not hop['interface'] or hop['interface'] not in iface_rows and iface_rows):
                # Recursive route: the gateway (1.1.1.1, 8.8.8.8…) is not on any port; follow its host route.
                iface, next_hop = recurse(hop['gateway'])
                if iface:
                    hop = {**hop, 'interface': iface}
            wan = wan_for(hop['interface'], hop['gateway'])
            if next_hop:
                wan['recursive'] = True
                if next_hop not in wan['next_hops']:
                    wan['next_hops'].append(next_hop)
            if hop['gateway'] and hop['gateway'] not in wan['gateways']:
                wan['gateways'].append(hop['gateway'])
            if table not in wan['tables']:
                wan['tables'].append(table)
            wan['distances'].append((table, distance))
            wan['route_count'] += 1
            wan['active'] = wan['active'] or active
            wan['reachable'] = wan['reachable'] or hop['reachable']
            wan['check_gateway'] = wan['check_gateway'] or str(g(route, 'check-gateway'))
            wan['source'] = _route_source(route, hop['interface'], iface_rows)

    # Interfaces the operator dragged into the WAN role but that have no route yet, and held-off lines.
    for iface in list(declared_wans or []) + list(held):
        if iface and iface not in wans:
            wan = wan_for(iface, '')
            wan['declared'] = True

    # ---------------- PCC / policy routing mapping ----------------
    pcc_rules = [r for r in mangle if str(g(r, 'per-connection-classifier')).strip()]
    conn_to_route_mark = {}
    conn_to_iface = {}
    route_marking_rules = []
    for rule in mangle:
        action = str(g(rule, 'action')).lower()
        new_rmark = str(g(rule, 'new-routing-mark'))
        if action == 'mark-routing' and new_rmark:
            route_marking_rules.append(rule)
            cmark = str(g(rule, 'connection-mark'))
            if cmark:
                conn_to_route_mark[cmark] = new_rmark
        if action == 'mark-connection' and g(rule, 'new-connection-mark') and g(rule, 'in-interface'):
            conn_to_iface.setdefault(str(g(rule, 'new-connection-mark')), _strip(g(rule, 'in-interface')))

    table_to_wans = {}
    for key, wan in wans.items():
        for table in wan['tables']:
            table_to_wans.setdefault(table, []).append(key)

    unmapped_pcc = 0
    denominators = set()
    for rule in pcc_rules:
        m = PCC_RE.search(str(g(rule, 'per-connection-classifier')))
        if m:
            denominators.add(int(m.group(1)))
        rmark = str(g(rule, 'new-routing-mark'))
        cmark = str(g(rule, 'new-connection-mark'))
        target_keys = []
        if not rmark and cmark:
            rmark = conn_to_route_mark.get(cmark, '')
        if rmark and rmark in table_to_wans:
            target_keys = table_to_wans[rmark][:1]
        elif cmark and conn_to_iface.get(cmark) in wans:
            target_keys = [conn_to_iface[cmark]]
        if target_keys:
            wans[target_keys[0]]['pcc_rules'] += 1
        else:
            unmapped_pcc += 1

    policy_tables = set()
    for rule in routing_rules:
        if str(g(rule, 'action', default='lookup')).startswith('lookup') and g(rule, 'table'):
            policy_tables.add(str(g(rule, 'table')))
    for rule in route_marking_rules:
        policy_tables.add(str(g(rule, 'new-routing-mark')))
    policy_tables = {t for t in policy_tables if t in table_to_wans and t != 'main'}

    # ---------------- ECMP / failover in main table ----------------
    main_active = [(key, d) for key, wan in wans.items() for (t, d) in wan['distances'] if t == 'main' and wan['active']]
    min_distance = min((d for _, d in main_active), default=None)
    for key, wan in wans.items():
        wan['ecmp_weight'] = sum(1 for (t, d) in wan['distances'] if t == 'main' and d == min_distance)
    ecmp_members = [k for k, w in wans.items() if w['ecmp_weight'] and w['active']]
    main_distances = sorted({d for w in wans.values() for (t, d) in w['distances'] if t == 'main'})
    main_members = [k for k, w in wans.items() if any(t == 'main' for t, _ in w['distances'])]

    bonds = []
    for bond in bonding:
        slaves = [s.strip() for s in str(g(bond, 'slaves')).split(',') if s.strip()]
        bonds.append({'name': str(g(bond, 'name')), 'slaves': slaves, 'mode': str(g(bond, 'mode')),
                      'running': truthy(g(bond, 'running', default='yes'))})

    if pcc_rules:
        method = 'PCC'
        description = ('Per-Connection Classifier splits new connections between the WAN links. '
                       'Each connection stays on one link, so sessions do not break.')
    elif policy_tables:
        method = 'Policy routing'
        description = 'Traffic is steered into dedicated routing tables by mangle marks or routing rules.'
    elif len(ecmp_members) > 1:
        method = 'ECMP'
        description = 'Several default routes share the same distance, so RouterOS spreads connections across them.'
    elif bonds and any(len(b['slaves']) > 1 for b in bonds):
        method = 'Bonding'
        description = 'Links are aggregated into a bond interface.'
    elif len(main_members) > 1 and len(main_distances) > 1:
        method = 'Failover'
        description = 'The lowest-distance route carries traffic; the others take over automatically when it fails.'
    elif wans:
        method = 'Single WAN'
        description = 'One Internet path is configured. Add a second WAN for redundancy or balancing.'
    else:
        method = 'No default route'
        description = 'No active default route (0.0.0.0/0) was found on this router.'

    # ---------------- roles, expected shares, state ----------------
    total_pcc = sum(w['pcc_rules'] for w in wans.values())
    total_ecmp = sum(wans[k]['ecmp_weight'] for k in ecmp_members) or 0
    primary_key = None
    if main_active:
        best = min(main_active, key=lambda x: x[1])[1]
        primary_key = next((k for k, d in main_active if d == best), None)

    links = []
    for key in order:
        wan = wans[key]
        up = wan['running'] and wan['reachable']
        if method == 'PCC':
            share = round(wan['pcc_rules'] * 100 / total_pcc) if total_pcc else 0
            role = 'balanced' if share else ('backup' if wan['route_count'] else 'idle')
        elif method == 'ECMP':
            share = round(wan['ecmp_weight'] * 100 / total_ecmp) if key in ecmp_members and total_ecmp else 0
            role = 'balanced' if share else 'backup'
        elif method == 'Policy routing':
            share = None
            role = 'policy' if set(wan['tables']) & policy_tables else ('primary' if key == primary_key else 'backup')
        else:
            is_primary = key == primary_key or (primary_key is None and len(order) == 1)
            share = 100 if is_primary else 0
            role = 'primary' if is_primary else 'backup'
        if wan['interface'] in held and not wan['route_count']:
            state, role = 'held', 'held'
        elif wan['declared'] and not wan['route_count']:
            state, role = 'no-route', 'unconfigured'
        elif not wan['running']:
            state = 'down'
        elif not up:
            state = 'down'
        elif role in {'balanced', 'primary', 'policy'} and wan['active']:
            state = 'active'
        else:
            state = 'standby'
        distance = min((d for _, d in wan['distances']), default='')
        links.append({
            'id': wan['id'], 'interface': wan['interface'], 'label': wan['comment'] or wan['interface'] or (wan['gateways'][0] if wan['gateways'] else 'WAN'),
            'gateways': wan['gateways'], 'gateway': wan['gateways'][0] if wan['gateways'] else '',
            'tables': wan['tables'], 'table': wan['tables'][0] if wan['tables'] else '',
            'distance': str(distance), 'active': state == 'active', 'running': wan['running'], 'state': state,
            'role': role, 'expected_share': share, 'check_gateway': wan['check_gateway'], 'source': wan['source'],
            'pcc_rules': wan['pcc_rules'], 'ecmp_weight': wan['ecmp_weight'],
            'next_hop': wan['next_hops'][0] if wan['next_hops'] else '', 'recursive': wan['recursive'],
            'held_by': held.get(wan['interface'], ''),
            'route_count': wan['route_count'],
        })

    warnings = []
    if unmapped_pcc:
        warnings.append(f'{unmapped_pcc} PCC rule(s) mark traffic into a table that has no default route — that share of traffic may fall back to main.')
    if method in {'PCC', 'Policy routing'} and hotspot_servers:
        warnings.append('HotSpot is running on a router with policy routing. HotSpot login traffic uses the main table; keep a working default route in main.')
    if method == 'Failover' and not any(l['check_gateway'] for l in links):
        warnings.append('Failover routes have no check-gateway, so a dead ISP behind a live modem will not trigger failover.')
    if len(links) > 1 and method == 'Single WAN':
        warnings.append('Multiple WAN interfaces exist but only one carries a default route.')
    unresolved = [l for l in links if not l['interface'] and l['gateway']]
    for l in unresolved:
        why = (notes.get(l['gateway']) or {}).get('why') or 'the router did not say'
        warnings.append(f"Could not tell which port {l['gateway']} leaves through: {why}. "
                        + ('Write its gateway with the port, e.g. ' + (notes[l['gateway']].get('gateway') or '192.168.1.1')
                           + '%' + notes[l['gateway']]['candidates'][0] + ', or apply the Internet lines setup.'
                           if (notes.get(l['gateway']) or {}).get('candidates')
                           else f"Add a host route ({l['gateway']}/32 via the ISP modem) or apply the Internet lines setup."))
    shared = {}
    for net, iface in networks:
        if iface in wans and net.prefixlen < 32:
            shared.setdefault(str(net), set()).add(iface)
    for net, ports in sorted(shared.items()):
        if len(ports) > 1:
            names = sorted(ports)
            warnings.append(f"{' and '.join(names)} are both on {net} — two modems with the same address. A route that says "
                            'only the modem address can go out either port, so both lines may really use one modem: '
                            'one line idles and losing the other cuts everyone. Give every route its port '
                            f'(e.g. …%{names[-1]}, the Internet lines page does this) or change one modem to another '
                            'address range.')
    for ip, n in sorted(notes.items()):
        if n.get('hint'):
            warnings.append(f"{ip} is shown on {n['hint']} because your Internet lines setup says so — the router's own "
                            f"routes do not make it clear ({n.get('why') or 'no port in the route'}).")
    for wan in wans.values():
        same = [d for (t, d) in wan['distances'] if t == 'main' and d == min_distance]
        others = [k for k, w in wans.items() if w is not wan and (w['declared'] or w['route_count'])]
        if len(same) > 1 and len(set(wan['gateways'])) > 1 and others and wan['ecmp_weight'] and len(ecmp_members) == 1:
            warnings.append(f"{len(same)} equal-cost default routes (via {', '.join(wan['gateways'])}) all leave through "
                            f"{wan['interface'] or 'one port'}, so the other line carries no customer traffic. "
                            f"Check that each check-host route points at a different ISP gateway.")
    for l in links:
        if l['state'] == 'held':
            others = [x['label'] for x in links if x is not l and x['state'] == 'active']
            why = ('the Internet Doctor took it out of use because its test addresses did not answer through it'
                   if l['held_by'] == 'doctor' else 'it was taken out of use on the Internet lines page')
            warnings.append(f"{l['label']} carries no customers: {why}. "
                            + (f"Everything depends on {', '.join(others)}" if others else 'No other line is in use')
                            + ' — press “Put in use” on the Internet lines page if it works.')
    dns_rows = [dict(r) for r in dns or []]
    if dns_rows and len([l for l in links if l['state'] in {'active', 'standby', 'held'}]) > 1:
        dns_servers = []
        for key in ('servers', 'dynamic-servers'):
            dns_servers += [x for x in re.split(r'[,;\s]+', str(g(dns_rows[0], key))) if is_ip(x)]
        on_line = {}
        for srv in dict.fromkeys(dns_servers):
            pinned_to = ''
            for net, r in specific:              # /32 health-check route (8.8.8.8 via Starlink) pins it
                try:
                    if net.prefixlen == 32 and ipaddress.ip_address(srv) in net:
                        hops = resolve_gateways(r, networks, dhcp_gateways, iface_rows)
                        pinned_to = hops[0]['interface'] if hops else ''
                except ValueError:
                    pass
            if not pinned_to:                    # the modem itself (192.168.1.1 on the Starlink port)
                try:
                    addr = ipaddress.ip_address(srv)
                    pinned_to = next((i for n, i in networks if addr in n and i in wans), '')
                except ValueError:
                    pass
            on_line[srv] = pinned_to
        lines_used = {i for i in on_line.values() if i}
        if on_line and all(on_line.values()) and len(lines_used) == 1:
            port = next(iter(lines_used))
            label = next((l['label'] for l in links if l['interface'] == port), port)
            warnings.append(f"The router looks up names only through {', '.join(on_line)}, which goes out through {label}. "
                            f"If {label} fails, names stop working and customers see no internet even though another "
                            'line works. Apply the Internet lines setup again (it adds DNS that works on every line).')
    idle = [l for l in links if l['state'] == 'no-route']
    if idle:
        warnings.append(', '.join(l['label'] for l in idle) + ' ' + ('is' if len(idle) == 1 else 'are') +
                        ' marked as WAN but no default route uses ' + ('it' if len(idle) == 1 else 'them') +
                        ', so customers never go out through ' + ('it.' if len(idle) == 1 else 'them.'))
    down = [l for l in links if l['state'] == 'down']
    if down:
        warnings.append('Down: ' + ', '.join(l['label'] for l in down))

    return {
        'version': ANALYSIS_VERSION,
        'method': method,
        'description': description,
        'configured': method not in {'Single WAN', 'No default route'},
        'wan_links': links,
        'primary': wans[primary_key]['id'] if primary_key else '',
        'pcc_rule_count': len(pcc_rules),
        'pcc_denominators': sorted(denominators),
        'marked_rule_count': len(route_marking_rules) + len([r for r in mangle if g(r, 'new-connection-mark')]),
        'routing_table_count': len(routing_tables),
        'routing_rule_count': len(routing_rules),
        'policy_tables': sorted(policy_tables),
        'bond_count': len(bonds),
        'bonds': bonds,
        'warnings': warnings,
    }
