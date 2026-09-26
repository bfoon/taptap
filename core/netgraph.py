"""Build one network graph for a business from the persisted discovery tables.

The graph is database-only (no RouterOS calls) so the topology page opens
instantly anywhere TapTap is hosted; live discovery refreshes the tables in
short per-router requests and the browser re-reads this graph.

Node types: internet, isp, wan, router, switch, wifi, network, clients
Edge kinds: internet, wan, uplink, peer, lan, wireless, clients
"""
from collections import defaultdict

from django.utils import timezone

from .routeros_analysis import g

WIFI_IFACE_PREFIXES = ('wlan', 'wifi', 'cap', 'wl-')


def _mac(value):
    return str(value or '').strip().upper().replace('-', ':')


def _is_wifi_iface(name):
    return str(name or '').lower().startswith(WIFI_IFACE_PREFIXES)


def _snapshot(router):
    try:
        return router.config_snapshot
    except Exception:
        return None


def _router_ips(router, snap):
    ips = {str(router.ip_address or '').split(':')[0].strip()}
    if snap and snap.sections:
        for row in (snap.sections.get('IP addresses') or {}).get('rows', []):
            addr = str(g(row, 'address')).split('/')[0]
            if addr:
                ips.add(addr)
    ips.discard('')
    return ips


def _identity(router, snap):
    if snap and snap.sections:
        rows = (snap.sections.get('_identity') or {}).get('rows') or (snap.sections.get('System identity') or {}).get('rows') or []
        if rows and rows[0].get('name'):
            return str(rows[0]['name']), str(rows[0].get('model', ''))
    return router.name, ''


def build_graph(business, include_clients=True, client_sample=40):
    routers = list(business.routers.all().order_by('name'))
    nodes, edges = {}, {}
    edge_seq = [0]  # monotonic: edges can be replaced, so len(edges) is not a safe id
    ip_index, mac_index, ident_index = {}, {}, {}
    meta = {}

    for r in routers:
        snap = _snapshot(r)
        identity, model = _identity(r, snap)
        lb = (snap.load_balancing if snap else None) or {}
        meta[r.id] = {'snap': snap, 'lb': lb, 'identity': identity}
        for ip in _router_ips(r, snap):
            ip_index[ip] = r
        for mac in r.interfaces.exclude(mac_address='').values_list('mac_address', flat=True):
            mac_index[_mac(mac)] = r
        ident_index[identity.lower()] = r
        wan_ifaces = [l.get('interface') for l in lb.get('wan_links', []) if l.get('interface')]
        meta[r.id]['wan_ifaces'] = set(wan_ifaces)
        nodes[f'router:{r.id}'] = {
            'id': f'router:{r.id}', 'type': 'router', 'label': identity or r.name, 'sub': r.ip_address,
            'router_id': r.id, 'status': 'online' if r.status == 'Online' else 'offline', 'model': model,
            'lb_method': lb.get('method', ''), 'error': r.last_error[:240] if r.last_error else '',
            'last_seen': r.last_tested_at.isoformat() if r.last_tested_at else None,
            'ports': r.interfaces.filter(is_present=True).count(),
        }

    def add_edge(src, dst, kind, router_id=None, iface='', label='', online=True, direction='down'):
        key = tuple(sorted((src, dst))) if kind == 'peer' else (src, dst)
        if key in edges:
            return edges[key]
        edge_seq[0] += 1
        edge = {'id': f'e{edge_seq[0]}', 'source': src, 'target': dst, 'kind': kind, 'router_id': router_id,
                'iface': iface, 'label': label or iface, 'online': bool(online), 'direction': direction}
        edges[key] = edge
        return edge

    has_upstream = set()
    upstream_ifaces = defaultdict(set)  # router id -> WAN ports fed by another managed router

    for r in routers:
        m = meta[r.id]
        rid = f'router:{r.id}'
        # -------- managed router links & unmanaged neighbors --------
        for n in r.neighbors.all().order_by('-is_online', 'identity'):
            peer = None
            if n.mac_address and _mac(n.mac_address) in mac_index:
                peer = mac_index[_mac(n.mac_address)]
            elif n.address and n.address in ip_index:
                peer = ip_index[n.address]
            elif n.identity and n.identity.lower() in ident_index:
                peer = ident_index[n.identity.lower()]
            if peer and peer.id != r.id:
                on_wan = n.interface_name in m['wan_ifaces']
                src, dst = (f'router:{peer.id}', rid) if on_wan else (rid, f'router:{peer.id}')
                if on_wan:
                    has_upstream.add(r.id)
                    upstream_ifaces[r.id].add(n.interface_name)
                add_edge(src, dst, 'peer', r.id, n.interface_name, n.interface_name, n.is_online)
                continue
            if peer and peer.id == r.id:
                continue
            nid = f'nb:{_mac(n.mac_address)}' if n.mac_address else f'nb:{r.id}:{n.neighbor_key}'
            is_isp = n.interface_name in m['wan_ifaces']
            if nid not in nodes:
                nodes[nid] = {
                    'id': nid, 'type': 'isp' if is_isp else (n.device_kind if n.device_kind in {'wifi', 'switch', 'router'} else 'network'),
                    'label': n.identity or n.board or n.address or n.mac_address or 'Device',
                    'sub': n.address or n.mac_address, 'mac': n.mac_address, 'ip': n.address, 'board': n.board,
                    'platform': n.platform, 'version': n.version, 'discovered_by': n.discovered_by,
                    'status': 'online' if n.is_online else 'offline', 'router_id': r.id, 'port': n.interface_name,
                    'last_seen': n.last_seen_at.isoformat() if n.last_seen_at else None,
                }
            elif n.is_online:
                nodes[nid]['status'] = 'online'
            if is_isp:
                add_edge(nid, rid, 'wan', r.id, n.interface_name, n.interface_name, n.is_online, 'up')
            else:
                kind = 'wireless' if _is_wifi_iface(n.interface_name) or n.discovered_by == 'capsman' else 'lan'
                add_edge(rid, nid, kind, r.id, n.interface_name, n.interface_name, n.is_online)

    # -------- WAN / Internet --------
    nodes['internet'] = {'id': 'internet', 'type': 'internet', 'label': 'Internet', 'sub': 'Public network', 'status': 'online'}
    for r in routers:
        m = meta[r.id]
        rid = f'router:{r.id}'
        links = m['lb'].get('wan_links', [])
        isp_by_iface = {e['iface']: e['source'] for e in edges.values() if e['kind'] == 'wan' and e['target'] == rid}
        if not links and r.id not in has_upstream:
            add_edge('internet', rid, 'internet', r.id, '', 'path unknown', r.status == 'Online')
            continue
        for link in links:
            iface = link.get('interface') or ''
            if iface and iface in upstream_ifaces[r.id]:
                # This WAN is fed by another managed router: the peer edge already shows the path.
                for e in edges.values():
                    if e['kind'] == 'peer' and e['target'] == rid and e['iface'] == iface:
                        e['label'] = f'{iface} (WAN)'; e['direction'] = 'up'
                continue
            wid = f'wan:{r.id}:{link.get("id") or iface}'
            nodes[wid] = {
                'id': wid, 'type': 'wan', 'label': link.get('label') or iface or 'WAN',
                'sub': link.get('gateway') or link.get('source', ''), 'router_id': r.id, 'iface': iface,
                'state': link.get('state', ''), 'role': link.get('role', ''), 'share': link.get('expected_share'),
                'source': link.get('source', ''), 'status': 'offline' if link.get('state') in {'down', 'no-route'} else 'online',
                'tables': link.get('tables', []), 'distance': link.get('distance', ''),
            }
            online = link.get('state') not in {'down', 'no-route'}
            add_edge(wid, rid, 'wan', r.id, iface, iface, online, 'up')
            if iface in isp_by_iface:
                isp = isp_by_iface[iface]
                edges.pop((isp, rid), None)
                add_edge(isp, wid, 'wan', r.id, iface, 'modem', online, 'up')
                add_edge('internet', isp, 'internet', r.id, iface, '', online, 'up')
            else:
                add_edge('internet', wid, 'internet', r.id, iface, '', online, 'up')

    # -------- client clusters --------
    if include_clients:
        neighbor_macs = {n.get('mac', '').upper() for n in nodes.values() if n.get('mac')}
        router_macs = set(mac_index.keys())
        for r in routers:
            rid = f'router:{r.id}'
            m = meta[r.id]
            port_neighbor = {}
            for e in edges.values():
                if e['source'] == rid and e['kind'] in {'lan', 'wireless'} and e['iface'] and e['iface'] not in port_neighbor:
                    port_neighbor[e['iface']] = e['target']
            groups = defaultdict(list)
            for d in r.devices.filter(is_online=True).order_by('interface_name', 'hostname'):
                mac = _mac(d.mac_address)
                if mac and (mac in neighbor_macs or mac in router_macs):
                    continue
                if d.interface_name in m['wan_ifaces']:
                    continue  # upstream ISP equipment, not customers
                groups[d.interface_name or '?'].append(d)
            for port, devs in groups.items():
                cid = f'clients:{r.id}:{port}'
                wifi = sum(1 for d in devs if d.connection_type == 'wifi' or _is_wifi_iface(d.interface_name))
                hotspot = sum(1 for d in devs if 'hotspot-active' in (d.sources or ''))
                parent = port_neighbor.get(port, rid)
                nodes[cid] = {
                    'id': cid, 'type': 'clients', 'label': f'{len(devs)} device{"s" if len(devs) != 1 else ""}',
                    'sub': f'on {port}' if port != '?' else 'port unknown', 'router_id': r.id, 'port': port,
                    'count': len(devs), 'wifi': wifi, 'hotspot': hotspot, 'status': 'online',
                    'devices': [{'name': d.hostname, 'mac': d.mac_address, 'ip': d.ip_address, 'kind': d.connection_type,
                                 'via': d.parent_identity, 'sources': d.sources} for d in devs[:client_sample]],
                }
                kind = 'wireless' if wifi > len(devs) / 2 else 'clients'
                add_edge(parent, cid, kind, r.id, port, '', True)

    # Traffic lookup: which router interfaces does the browser need live counters for?
    live_ifaces = defaultdict(set)
    for e in edges.values():
        if e.get('router_id') and e.get('iface') and e['kind'] != 'internet':
            live_ifaces[e['router_id']].add(e['iface'])

    stats = {
        'routers': len(routers), 'routers_online': sum(1 for r in routers if r.status == 'Online'),
        'network_devices': sum(1 for n in nodes.values() if n['type'] in {'switch', 'wifi', 'network', 'router', 'isp'} and n['id'].startswith('nb:')),
        'clients': sum(n.get('count', 0) for n in nodes.values() if n['type'] == 'clients'),
        'wan_links': sum(1 for n in nodes.values() if n['type'] == 'wan'),
        'wan_down': sum(1 for n in nodes.values() if n['type'] == 'wan' and n['status'] == 'offline'),
    }
    return {
        'nodes': list(nodes.values()), 'edges': list(edges.values()), 'stats': stats,
        'live_interfaces': {str(k): sorted(v) for k, v in live_ifaces.items()},
        'generated_at': timezone.now().isoformat(),
    }
