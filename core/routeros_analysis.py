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
STATUS_RE = re.compile(r'(\S+)\s+(reachable|unreachable)(?:\s+via\s+(\S+))?', re.I)
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


def resolve_gateways(route, networks=None, dhcp_gateways=None, interfaces=None):
    """Return [{'gateway','interface','reachable'}] for one route row."""
    networks = networks or []
    dhcp_gateways = dhcp_gateways or {}
    interfaces = interfaces or {}
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
            status_map[match.group(1)] = (match.group(3) or '', match.group(2).lower() == 'reachable')

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
        if not iface and ip:
            iface = dhcp_gateways.get(ip, '')
        if not iface and ip:
            try:
                addr = ipaddress.ip_address(ip)
                best = None
                for net, net_iface in networks:
                    if addr in net and (best is None or net.prefixlen > best[0].prefixlen):
                        best = (net, net_iface)
                if best:
                    iface = best[1]
            except ValueError:
                pass
        if iface and iface in interfaces and not truthy(g(interfaces[iface], 'running', default='yes')):
            reachable = False
        results.append({'gateway': ip, 'interface': iface, 'reachable': bool(reachable)})
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
                addresses=None, dhcp_clients=None, interfaces=None, declared_wans=None, hotspot_servers=None):
    """Full multi-WAN / load-balancing picture. Always returns a complete dict."""
    routes = [dict(r) for r in routes or []]
    mangle = [dict(r) for r in mangle or [] if not truthy(g(r, 'disabled', default='no'))]
    bonding = [dict(r) for r in bonding or [] if not truthy(g(r, 'disabled', default='no'))]
    routing_tables = [dict(r) for r in routing_tables or []]
    routing_rules = [dict(r) for r in routing_rules or [] if not truthy(g(r, 'disabled', default='no'))]
    iface_rows = interface_index(interfaces)
    networks = address_networks(addresses)
    dhcp_gateways = {}
    for row in dhcp_clients or []:
        gw = _strip(g(row, 'gateway'))
        if gw and g(row, 'interface'):
            dhcp_gateways[gw] = _strip(g(row, 'interface'))

    defaults = [r for r in routes
                if str(g(r, 'dst-address')) in DEFAULT_DST
                and not truthy(g(r, 'disabled', default='no'))
                and str(g(r, 'type', default='unicast')).lower() not in {'blackhole', 'unreachable', 'prohibit'}
                and not truthy(g(r, 'blackhole', default='no'))]

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
                'declared': False,
            }
            order.append(key)
        return wans[key]

    for route in defaults:
        table = _route_table(route)
        distance = int(re.sub(r'\D', '', str(g(route, 'distance', default='1'))) or 1)
        active = truthy(g(route, 'active', default='true'))
        for hop in resolve_gateways(route, networks, dhcp_gateways, iface_rows):
            wan = wan_for(hop['interface'], hop['gateway'])
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

    # Interfaces the operator dragged into the WAN role but that have no route yet.
    for iface in declared_wans or []:
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
        if wan['declared'] and not wan['route_count']:
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
    down = [l for l in links if l['state'] == 'down']
    if down:
        warnings.append('Down: ' + ', '.join(l['label'] for l in down))

    return {
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
