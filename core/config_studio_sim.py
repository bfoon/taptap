"""Config Studio's virtual router.

A copy of the router's configuration held in memory (from the last snapshot TapTap read, or a factory-fresh
practice router). The studio applies a board's operations to that copy — nothing touches the real router — then:

* ``diff``     shows row by row what would be new, changed or removed;
* ``trace``    walks one imaginary packet through hotspot, port forwarding, routing, the firewall, NAT and the
               speed queues, and says at each step what happened and which rule decided;
* ``examine``  runs a set of such packets automatically (one per tile: "can a guest reach the internet?",
               "can a guest reach my printer?", "can TapTap still reach the router?") plus plain checks
               (overlapping networks, a port that does not exist …).

The virtual firewall understands the matchers that everyday rules use. A rule using anything else (layer-7,
time, limits …) is reported as "cannot be judged here" and skipped, never guessed.
"""
from __future__ import annotations

import ipaddress
from copy import deepcopy

INTERNET_IP = '203.0.113.7'        # a documentation address standing for "somebody on the internet"
PUBLIC_DST = '1.1.1.1'
HIDDEN = ('id', '.id', '_sim', '_was')
IGNORED_MATCH = {'comment', 'action', 'chain', 'disabled', 'dynamic', 'invalid', 'bytes', 'packets', 'log', 'log-prefix',
                 'jump-target', 'reject-with', 'to-addresses', 'to-ports', 'address-list', 'address-list-timeout',
                 'new-packet-mark', 'new-connection-mark', 'new-routing-mark', 'passthrough', 'creation-time', 'hw-offload', '.nextid'}
TERMINAL = {'accept': 'accept', 'drop': 'drop', 'reject': 'drop', 'tarpit': 'drop'}
SERVICE_PORTS = {'winbox': 8291, 'api': 8728, 'api-ssl': 8729, 'ssh': 22, 'telnet': 23, 'ftp': 21, 'www': 80, 'www-ssl': 443}


def yes(value):
    return str(value).strip().lower() in ('true', 'yes', '1', 'on')


def on(row):
    return not yes(row.get('disabled', 'false'))


def same(a, b):
    a, b = str(a).strip(), str(b).strip()
    if a == b:
        return True
    la, lb = a.lower(), b.lower()
    if la in ('yes', 'true', 'no', 'false') and lb in ('yes', 'true', 'no', 'false'):
        return yes(la) == yes(lb)
    return la == lb


def hy(d):
    return {str(k).replace('_', '-'): v for k, v in (d or {}).items()}


# ───────────────────────────── practice routers ─────────────────────────────
def _ports(n):
    return [{'id': f'*{i}', 'name': f'ether{i}', 'type': 'ether', 'running': 'true' if i < 3 else 'false', 'disabled': 'false'}
            for i in range(1, n + 1)]


def lab_state(kind='default'):
    """A practice router: 'empty' (five bare ports) or 'default' (MikroTik's usual home/office start)."""
    state = {'/interface': _ports(5), '/system/identity': [{'name': 'Practice-Router'}],
             '/system/resource': [{'version': '7.16 (stable)', 'board-name': 'Practice hEX'}],
             '/ip/dns': [{'servers': '', 'allow-remote-requests': 'false'}],
             '/ip/service': [{'id': f'*s{i}', 'name': n, 'port': str(p), 'disabled': 'false', 'address': ''}
                             for i, (n, p) in enumerate(SERVICE_PORTS.items())]}
    if kind == 'empty':
        return state
    state['/interface'].append({'id': '*B', 'name': 'bridge', 'type': 'bridge', 'running': 'true', 'disabled': 'false'})
    state.update({
        '/interface/bridge': [{'id': '*B', 'name': 'bridge', 'disabled': 'false', 'comment': 'defconf'}],
        '/interface/bridge/port': [{'id': f'*bp{i}', 'interface': f'ether{i}', 'bridge': 'bridge', 'comment': 'defconf'} for i in range(2, 6)],
        '/interface/list': [{'id': '*L1', 'name': 'WAN'}, {'id': '*L2', 'name': 'LAN'}],
        '/interface/list/member': [{'id': '*M1', 'list': 'WAN', 'interface': 'ether1'}, {'id': '*M2', 'list': 'LAN', 'interface': 'bridge'}],
        '/ip/address': [{'id': '*A1', 'address': '192.168.88.1/24', 'interface': 'bridge', 'network': '192.168.88.0', 'disabled': 'false', 'comment': 'defconf'}],
        '/ip/pool': [{'id': '*P1', 'name': 'default-dhcp', 'ranges': '192.168.88.10-192.168.88.254'}],
        '/ip/dhcp-server': [{'id': '*D1', 'name': 'defconf', 'interface': 'bridge', 'address-pool': 'default-dhcp', 'disabled': 'false'}],
        '/ip/dhcp-server/network': [{'id': '*N1', 'address': '192.168.88.0/24', 'gateway': '192.168.88.1', 'dns-server': '192.168.88.1'}],
        '/ip/dhcp-client': [{'id': '*C1', 'interface': 'ether1', 'disabled': 'false', 'add-default-route': 'yes', 'status': 'bound',
                             'address': '100.64.10.20/24', 'gateway': '100.64.10.1', 'comment': 'defconf'}],
        '/ip/dns': [{'servers': '', 'dynamic-servers': '100.64.10.1', 'allow-remote-requests': 'true'}],
        '/ip/firewall/nat': [{'id': '*F1', 'chain': 'srcnat', 'action': 'masquerade', 'out-interface-list': 'WAN', 'disabled': 'false',
                              'comment': 'defconf: masquerade'}],
        '/ip/firewall/filter': [
            {'id': '*1', 'chain': 'input', 'action': 'accept', 'connection-state': 'established,related,untracked', 'comment': 'defconf: accept established,related,untracked'},
            {'id': '*2', 'chain': 'input', 'action': 'drop', 'connection-state': 'invalid', 'comment': 'defconf: drop invalid'},
            {'id': '*3', 'chain': 'input', 'action': 'accept', 'protocol': 'icmp', 'comment': 'defconf: accept ICMP'},
            {'id': '*4', 'chain': 'input', 'action': 'drop', 'in-interface-list': '!LAN', 'comment': 'defconf: drop all not coming from LAN'},
            {'id': '*5', 'chain': 'forward', 'action': 'fasttrack-connection', 'connection-state': 'established,related', 'comment': 'defconf: fasttrack'},
            {'id': '*6', 'chain': 'forward', 'action': 'accept', 'connection-state': 'established,related,untracked', 'comment': 'defconf: accept established,related, untracked'},
            {'id': '*7', 'chain': 'forward', 'action': 'drop', 'connection-state': 'invalid', 'comment': 'defconf: drop invalid'},
            {'id': '*8', 'chain': 'forward', 'action': 'drop', 'connection-state': 'new', 'connection-nat-state': '!dstnat', 'in-interface-list': 'WAN',
             'comment': 'defconf: drop all from WAN not DSTNATed'},
        ],
    })
    return state


def state_from_sections(sections):
    """{path: rows} from a RouterConfigSnapshot.sections ({label: {path, rows}})."""
    state = {}
    for label, sec in (sections or {}).items():
        if isinstance(sec, dict) and sec.get('path') and not str(label).startswith('_'):
            state[sec['path']] = [hy(r) for r in (sec.get('rows') or []) if isinstance(r, dict)]
    return state


# ───────────────────────────── the virtual router ─────────────────────────────
class VirtualRouter:
    def __init__(self, state):
        self.state = deepcopy(state or {})
        self.changes = []        # [{resource, kind: new|changed|removed|same, row, before, tile, label}]
        self._n = 0

    def rows(self, path):
        return self.state.setdefault(path, [])

    def live(self, path):
        return [r for r in self.rows(path) if on(r) and r.get('_sim') != 'removed']

    def match(self, path, find):
        find = hy(find)
        return [r for r in self.rows(path) if r.get('_sim') != 'removed' and all(same(r.get(k, ''), v) for k, v in find.items())]

    # ── applying a board ──
    def apply(self, ops):
        for o in ops:
            self.apply_one(o)
        return self

    def apply_one(self, o):
        path, find, values = o['resource'], hy(o['find']), hy(o['values'])
        note = {'resource': path, 'tile': o.get('tile', ''), 'label': o.get('label', '')}
        if o['action'] == 'singleton_set':
            rows = self.rows(path)
            if not rows:
                rows.append({})
            self._change(rows[0], values, note)
            return
        found = self.match(path, find)
        if o['action'] == 'remove_matching':
            for row in found:
                row['_sim'] = 'removed'
                self.changes.append({**note, 'kind': 'removed', 'row': _public(row), 'before': _public(row)})
            return
        if o['action'] == 'set':
            if not found:
                self.changes.append({**note, 'kind': 'missing', 'row': dict(find), 'before': {}})
                return
            self._change(found[0], values, note)
            return
        if found:                                  # ensure
            self._change(found[0], values, note)
            return
        self._n += 1
        row = {'id': f'*new{self._n}', **find, **values, '_sim': 'new'}
        rows = self.rows(path)
        if o.get('place') == 'top':
            chain = row.get('chain')
            at = next((i for i, r in enumerate(rows) if r.get('chain') == chain and not yes(r.get('dynamic', 'false'))), len(rows))
            rows.insert(at, row)
        else:
            rows.append(row)
        if path in ('/interface/bridge', '/interface/vlan', '/interface/pppoe-client') and not self.match('/interface', {'name': row.get('name')}):
            self.rows('/interface').append({'id': row['id'], 'name': row.get('name'), 'type': path.rsplit('/', 1)[-1], 'running': 'true',
                                            'disabled': 'false', '_sim': 'new'})
        self.changes.append({**note, 'kind': 'new', 'row': _public(row), 'before': {}})

    def _change(self, row, values, note):
        before = _public(row)
        diff = {k: v for k, v in values.items() if not same(row.get(k, ''), v)}
        if not diff:
            self.changes.append({**note, 'kind': 'same', 'row': before, 'before': before})
            return
        row.setdefault('_was', {}).update({k: row.get(k, '') for k in diff if k not in row.get('_was', {})})
        row.update(diff)
        if row.get('_sim') != 'new':
            row['_sim'] = 'changed'
        self.changes.append({**note, 'kind': 'changed', 'row': _public(row), 'before': before, 'fields': sorted(diff)})

    # ── facts ──
    def interfaces(self):
        names = [r.get('name') for r in self.rows('/interface') if r.get('name') and r.get('_sim') != 'removed']
        for path in ('/interface/bridge', '/interface/vlan', '/interface/pppoe-client'):
            names += [r.get('name') for r in self.rows(path) if r.get('name') and r.get('name') not in names]
        return names

    def bridge_of(self, iface):
        for r in self.live('/interface/bridge/port'):
            if r.get('interface') == iface:
                return r.get('bridge')
        return None

    def addresses(self):
        out = []
        for r in self.live('/ip/address'):
            try:
                out.append((ipaddress.IPv4Interface(str(r.get('address'))), r.get('interface'), r))
            except ValueError:
                continue
        for r in self.live('/ip/dhcp-client'):          # an address the provider gave
            try:
                if r.get('address'):
                    out.append((ipaddress.IPv4Interface(str(r.get('address'))), r.get('interface'), r))
            except ValueError:
                continue
        return out

    def router_ips(self):
        return {str(a.ip) for a, _, _ in self.addresses()}

    def connected(self, ip):
        ip = ipaddress.IPv4Address(ip)
        best = None
        for a, iface, _ in self.addresses():
            if ip in a.network and (best is None or a.network.prefixlen > best[0].network.prefixlen):
                best = (a, iface)
        return best

    def wan_interfaces(self):
        """Ports the default route leaves through."""
        out = []
        for r in self.live('/ip/route'):
            if str(r.get('dst-address', '')) != '0.0.0.0/0' or not r.get('gateway'):
                continue
            gw = str(r.get('immediate-gw') or r.get('gateway'))
            if '%' in gw:
                out.append(gw.split('%', 1)[1])
                continue
            try:
                hit = self.connected(gw.split(',')[0])
                if hit:
                    out.append(hit[1])
            except ValueError:
                out.append(gw)                           # the gateway is an interface name (pppoe-out1)
        for r in self.live('/ip/dhcp-client'):
            if str(r.get('add-default-route', 'yes')).lower() != 'no' and r.get('interface'):
                out.append(r['interface'])
        for r in self.live('/interface/pppoe-client'):
            if yes(r.get('add-default-route', 'no')) and r.get('name'):
                out.append(r['name'])
        return list(dict.fromkeys(out))

    def list_members(self, name):
        if name in ('all', 'static', 'dynamic'):
            return None                                  # everything
        members = {r.get('interface') for r in self.live('/interface/list/member') if r.get('list') == name}
        for r in self.rows('/interface/list'):           # a list that includes another list
            if r.get('name') == name and r.get('include'):
                for inc in str(r['include']).split(','):
                    members |= self.list_members(inc.strip()) or set()
        return members

    def in_address_list(self, name, ip, host=''):
        for r in self.live('/ip/firewall/address-list'):
            if r.get('list') != name:
                continue
            entry = str(r.get('address', ''))
            if host and entry.lower().lstrip('*.') and host.lower().endswith(entry.lower().lstrip('*.')) and not entry[:1].isdigit():
                return True
            if _ip_in(ip, entry):
                return True
        return False

    # ── one packet ──
    def trace(self, src_ip='', in_if='', dst='internet', proto='tcp', port=443, mac='', host='', logged_in=False):
        steps = []

        def say(stage, text, level='ok', rule=None):
            steps.append({'stage': stage, 'text': text, 'level': level, 'rule': _public(rule) if rule else None})

        def done(verdict, summary):
            return {'verdict': verdict, 'summary': summary, 'steps': steps,
                    'packet': {'src': p['src'], 'dst': p['dst'], 'in': p['in'], 'out': p.get('out', ''), 'proto': proto, 'port': port}}

        wans = self.wan_interfaces()
        if not in_if:
            hit = self.connected(src_ip) if src_ip else None
            in_if = hit[1] if hit else (wans[0] if wans else '')
        raw_if = in_if
        in_if = self.bridge_of(in_if) or in_if
        p = {'src': src_ip or INTERNET_IP, 'dst': '', 'in': in_if, 'proto': proto, 'port': int(port or 0), 'mac': mac.upper(),
             'host': host, 'dstnat': False, 'local': False}
        if in_if not in self.interfaces():
            say('arrive', f'The port {in_if or "?"} does not exist on this router.', 'bad')
            return done('blocked', f'{in_if or "The port"} does not exist.')
        iface_row = (self.match('/interface', {'name': raw_if}) or [{}])[0]
        if yes(iface_row.get('disabled', 'false')):
            say('arrive', f'{raw_if} is switched off: nothing gets in.', 'bad')
            return done('blocked', f'{raw_if} is switched off.')
        if dst == 'internet':
            p['dst'] = PUBLIC_DST
        elif dst == 'router':
            mine = [str(a.ip) for a, iface, _ in self.addresses() if iface == in_if]
            p['dst'] = mine[0] if mine else next(iter(sorted(self.router_ips())), '')
            if not p['dst']:
                say('arrive', 'The router has no address yet, so nothing can talk to it.', 'bad')
                return done('blocked', 'The router has no IP address.')
        else:
            p['dst'] = str(dst)
        say('arrive', f'{p["src"]} arrives on {in_if}' + (f' (through {raw_if})' if raw_if != in_if else '')
            + f', asking for {host or p["dst"]} {proto}' + (f' port {p["port"]}' if proto in ('tcp', 'udp') else ''))

        # hotspot
        login = False
        hs = [h for h in self.live('/ip/hotspot') if h.get('interface') == in_if]
        if hs and p['dst'] not in self.router_ips():
            bound = [b for b in self.live('/ip/hotspot/ip-binding') if (p['mac'] and str(b.get('mac-address', '')).upper() == p['mac'])
                     or (b.get('address') and same(b.get('address'), p['src']))]
            if bound and str(bound[0].get('type', 'regular')) == 'blocked':
                say('hotspot', 'This device is blocked on the hotspot.', 'bad', bound[0])
                return done('blocked', 'Blocked by a hotspot IP binding.')
            free = host and any(_host_match(host, w.get('dst-host', '')) and str(w.get('action', 'allow')) == 'allow'
                                for w in self.live('/ip/hotspot/walled-garden'))
            if bound and str(bound[0].get('type')) == 'bypassed':
                say('hotspot', 'Hotspot: this device skips the login page.', 'ok', bound[0])
            elif free:
                say('hotspot', f'Hotspot: {host} is a free site, it opens without signing in.')
            elif logged_in:
                say('hotspot', 'Hotspot: the customer has signed in.')
            else:
                login = True
                say('hotspot', f'Hotspot {hs[0].get("name", "")}: the login page comes first. After signing in:', 'warn')

        # port forwarding
        p['local'] = p['dst'] in self.router_ips()
        for r in self._chain('/ip/firewall/nat', 'dstnat'):
            ok, unknown = self._matches(r, p)
            if unknown:
                say('dstnat', f'A port-forward rule uses “{unknown}”, which the virtual router cannot judge. Skipped.', 'info', r)
                continue
            if ok and str(r.get('action')) in ('dst-nat', 'netmap', 'redirect'):
                if r.get('action') == 'redirect':
                    p['dst'] = next(iter(sorted(self.router_ips())), p['dst'])
                else:
                    p['dst'] = str(r.get('to-addresses') or p['dst']).split('-')[0]
                if r.get('to-ports'):
                    p['port'] = int(str(r['to-ports']).split('-')[0])
                p['dstnat'] = True
                say('dstnat', f'Port forwarding sends it on to {p["dst"]} port {p["port"]}.', 'ok', r)
                break

        # routing
        p['local'] = p['dst'] in self.router_ips()
        if p['local']:
            chain = 'input'
            say('route', f'{p["dst"]} is the router itself.')
        else:
            chain = 'forward'
            try:
                hit = self.connected(p['dst'])
            except ValueError:
                say('route', f'{p["dst"]} is not an address.', 'bad')
                return done('blocked', 'Not a valid destination.')
            if hit:
                p['out'] = hit[1]
                say('route', f'{p["dst"]} is on the network of {hit[1]}.')
            elif wans:
                p['out'] = wans[0]
                say('route', f'Not a local address: it leaves through the internet line {wans[0]}.')
            else:
                say('route', 'The router has no internet line (no default route), so it does not know where to send this.', 'bad')
                return done('noroute', 'No internet line is set up.')
            if p['out'] == p['in'] and hit:
                say('route', 'Same network on both sides: devices talk directly, the router is not in between.', 'info')
                return done('ok', 'Same network — goes directly, the router does not filter it.')

        # firewall
        verdict, rule = self._filter(chain, p, say)
        if verdict == 'drop':
            return done('blocked', f'Blocked by the firewall ({(rule or {}).get("comment") or "rule in " + chain}).')

        if p['local']:
            if proto in ('tcp', 'udp'):
                for s in self.rows('/ip/service'):
                    if str(s.get('port')) == str(p['port']):
                        if yes(s.get('disabled', 'false')):
                            say('service', f'The {s.get("name")} service is switched off, nobody answers on port {p["port"]}.', 'bad', s)
                            return done('blocked', f'The {s.get("name")} service is off.')
                        allowed = str(s.get('address') or '').strip()
                        if allowed and not any(_ip_in(p['src'], a.strip()) for a in allowed.split(',')):
                            say('service', f'{s.get("name")} only answers to {allowed}; {p["src"]} is not in it.', 'bad', s)
                            return done('blocked', f'{s.get("name")} does not answer to this address.')
                        say('service', f'The {s.get("name")} service answers.')
            return done('login' if login else 'ok', 'Reaches the router.')

        # leaving: NAT and speed
        natted = False
        for r in self._chain('/ip/firewall/nat', 'srcnat'):
            ok, unknown = self._matches(r, p)
            if ok and not unknown and str(r.get('action')) in ('masquerade', 'src-nat'):
                natted = True
                say('nat', f'Leaves {p["out"]} wearing the router’s own address (NAT), so the answer finds its way back.', 'ok', r)
                break
        level = 'ok'
        if not natted and p.get('out') in wans and _private(p['src']):
            level = 'warn'
            say('nat', f'No NAT rule covers {p["src"]} leaving {p["out"]}: the internet cannot answer a private address.', 'bad')
            return done('nonat', 'Leaves the router, but without NAT no answer comes back.')
        for qrow in self.live('/queue/simple'):
            targets = [t.strip() for t in str(qrow.get('target', '')).split(',') if t.strip()]
            if any(_ip_in(p['src'], t) or t == p['in'] for t in targets) and qrow.get('max-limit'):
                up, _, down = str(qrow['max-limit']).partition('/')
                say('speed', f'Speed limit “{qrow.get("name", "")}”: {down} down, {up} up.', 'info', qrow)
                break
        where = 'the internet' if p.get('out') in wans and not self.connected(p['dst']) else p['dst']
        return done('login' if login else level, f'Reaches {where}' + (' after the customer signs in.' if login else '.'))

    def _chain(self, path, chain):
        return [r for r in self.live(path) if r.get('chain') == chain]

    def _filter(self, chain, p, say, depth=0):
        for r in self._chain('/ip/firewall/filter', chain):
            ok, unknown = self._matches(r, p)
            if unknown:
                say('firewall', f'A {chain} rule uses “{unknown}”, which the virtual router cannot judge. Skipped.', 'info', r)
                continue
            if not ok:
                continue
            action = str(r.get('action', 'accept'))
            name = r.get('comment') or f'{action} rule in {chain}'
            if action in TERMINAL:
                if TERMINAL[action] == 'accept':
                    say('firewall', f'Firewall lets it through: {name}', 'ok', r)
                    return 'accept', r
                say('firewall', f'Firewall stops it: {name}', 'bad', r)
                return 'drop', r
            if action == 'jump' and r.get('jump-target') and depth < 6:
                verdict, rule = self._filter(str(r['jump-target']), p, say, depth + 1)
                if verdict in ('accept', 'drop'):
                    return verdict, rule
            elif action == 'return':
                return 'return', r
        if depth == 0:
            say('firewall', f'No {chain} rule stops it, so it passes (RouterOS allows what is not forbidden).')
            return 'accept', None
        return 'return', None

    def _matches(self, r, p):
        """(matches, unknown matcher name or '')."""
        for key, raw in r.items():
            if key in IGNORED_MATCH or key in HIDDEN or raw in ('', None):
                continue
            value = str(raw)
            neg = value.startswith('!')
            want = value[1:].strip() if neg else value
            if key in ('src-address', 'dst-address'):
                hit = _ip_in(p['src' if key == 'src-address' else 'dst'], want)
            elif key in ('in-interface', 'out-interface'):
                hit = p.get('in' if key == 'in-interface' else 'out', '') == want
            elif key in ('in-interface-list', 'out-interface-list'):
                members = self.list_members(want)
                hit = True if members is None else p.get('in' if key.startswith('in') else 'out', '') in members
            elif key in ('src-address-list', 'dst-address-list'):
                src = key.startswith('src')
                hit = self.in_address_list(want, p['src' if src else 'dst'], '' if src else p['host'])
            elif key == 'protocol':
                hit = want == p['proto']
            elif key == 'dst-port':
                hit = p['proto'] in ('tcp', 'udp') and _port_in(p['port'], want)
            elif key == 'connection-state':
                hit = 'new' in [x.strip() for x in want.split(',')]
            elif key == 'connection-nat-state':
                hit = ('dstnat' in want) == p['dstnat'] if 'dstnat' in want else False
            elif key == 'src-mac-address':
                hit = bool(p['mac']) and want.upper() == p['mac']
            elif key == 'dst-address-type' and want == 'local':
                hit = p['local']
            elif key == 'hotspot' and want == 'auth':
                hit = True                                # the packet tests assume a signed-in customer
            else:
                return False, key
            if hit == neg:
                return False, ''
        return True, ''


def _public(row):
    return {k: v for k, v in (row or {}).items() if k not in ('_sim', '_was')}


def _ip_in(ip, spec):
    spec = str(spec).strip()
    try:
        addr = ipaddress.IPv4Address(str(ip))
        if '-' in spec:
            a, b = spec.split('-', 1)
            return int(ipaddress.IPv4Address(a.strip())) <= int(addr) <= int(ipaddress.IPv4Address(b.strip()))
        return addr in ipaddress.IPv4Network(spec, strict=False)
    except ValueError:
        return False


def _port_in(port, spec):
    for part in str(spec).split(','):
        a, _, b = part.strip().partition('-')
        try:
            if int(a) <= int(port) <= int(b or a):
                return True
        except ValueError:
            continue
    return False


def _private(ip):
    try:
        return ipaddress.IPv4Address(ip).is_private
    except ValueError:
        return False


def _host_match(host, pattern):
    pattern = str(pattern).lower().strip()
    host = host.lower()
    if not pattern:
        return False
    core = pattern.replace('*', '').strip('.')
    return host == core or host.endswith('.' + core) or (pattern.startswith('*') and host.endswith(core))


def sample_ip(gw):
    """An address a device would get in the network of ``gw`` (an IPv4Interface)."""
    net = gw.network
    for offset in (50, 20, 2, 1):
        candidate = ipaddress.IPv4Address(int(net.network_address) + offset)
        if candidate in net and candidate not in (gw.ip, net.broadcast_address, net.network_address):
            return str(candidate)
    return str(gw.ip)


# ───────────────────────────── the automatic examination ─────────────────────────────
def management_probe(vr, router=None, trusted=()):
    """Can TapTap still reach this router? Returns (description, trace) or None when it cannot be told."""
    mode = getattr(router, 'connection_mode', 'api') if router else 'api'
    port = int(getattr(router, 'api_port', 8728) or 8728) if router else 8728
    if mode == 'agent':
        return None
    host = str(getattr(router, 'ip_address', '') or '').split(':')[0] if router else ''
    try:
        target = str(ipaddress.IPv4Address(host))
    except ValueError:
        target = ''
    src, in_if = '', ''
    for addr in trusted:                     # where TapTap's connections really come from (core/self_trust.py)
        try:
            hit = vr.connected(addr)
        except ValueError:
            continue
        if hit:
            src, in_if = addr, hit[1]
            break
        if not _private(addr) and not src:
            src = addr                       # a public address: it arrives over the internet line
    if not src and target and vr.connected(target):
        src = sample_ip(vr.connected(target)[0])       # TapTap sits on the router's own network
    if not src:
        src = INTERNET_IP
    dst = target if target in vr.router_ips() else 'router'
    return f'TapTap ({src}) → the router, API port {port}', vr.trace(src_ip=src, in_if=in_if, dst=dst, proto='tcp', port=port)


def examine(base_state, ops, board=(), router=None, trusted=()):
    """Everything the studio shows under "Virtual router": changes, findings and the packet tests."""
    from . import config_studio_tiles as tiles

    before, after = VirtualRouter(base_state), VirtualRouter(base_state).apply(ops)
    findings, tests = [], []

    def find(level, text, tile='', fix=''):
        findings.append({'level': level, 'text': text, 'tile': tile, 'fix': fix})

    def test(title, want, tr, tile=''):
        got = 'blocked' if tr['verdict'] in ('blocked', 'noroute', 'nonat') else 'reaches'
        tests.append({'title': title, 'want': want, 'got': got, 'pass': got == want, 'tile': tile, 'summary': tr['summary'],
                      'verdict': tr['verdict'], 'steps': tr['steps']})

    names = set(after.interfaces())
    by_tile = {t['id']: t for t in tiles.clean_board(board)}

    # plain checks on every operation
    for ch in after.changes:
        if ch['kind'] == 'missing':
            find('bad', f'{ch["label"]}: the router has no such row ({", ".join(f"{k}={v}" for k, v in ch["row"].items())}).', ch['tile'],
                 'Check the name on the tile.')
    for o in ops:
        for key in ('interface', 'out-interface', 'in-interface', 'bridge'):
            ref = {**o['find'], **o['values']}.get(key)
            if ref and o['action'] != 'remove_matching' and ref not in names:
                find('bad', f'{o["label"]}: there is no port or group called “{ref}” on this router.', o.get('tile', ''),
                     'Pick an existing port, or add a “Join ports” tile that creates it first.')
        if o['resource'] == '/interface/bridge/port' and o['action'] == 'remove_matching':
            was = before.bridge_of(o['find'].get('interface'))
            if was:
                find('warn', f'{o["find"]["interface"]} leaves the group {was} to stand alone: whatever is plugged into it changes network.', o.get('tile', ''))
        if o['resource'] == '/interface/bridge/port' and o['action'] == 'ensure':
            was = before.bridge_of(o['find'].get('interface'))
            if was and was != o['values'].get('bridge'):
                find('warn', f'{o["find"]["interface"]} moves from {was} to {o["values"]["bridge"]}: devices plugged into it change network.', o.get('tile', ''))

    # overlapping networks, addresses on member ports
    seen = []
    for addr, iface, row in after.addresses():
        slave_of = after.bridge_of(iface)
        if slave_of and row.get('_sim'):
            find('bad', f'{iface} belongs to the group {slave_of}: the address {addr} must go on {slave_of}, not on the port.',
                 _tile_of(after, '/ip/address', row), f'Use {slave_of} on the tile.')
        for other, other_if, other_row in seen:
            if iface != other_if and addr.network.overlaps(other.network) and (row.get('_sim') or other_row.get('_sim')):
                find('bad', f'{addr.network} on {iface} overlaps {other.network} on {other_if}: the router cannot tell the two apart.',
                     _tile_of(after, '/ip/address', row if row.get('_sim') else other_row), 'Choose a different network, for example 10.20.30.1/24.')
        seen.append((addr, iface, row))
    servers = {}
    for s in after.live('/ip/dhcp-server'):
        servers.setdefault(s.get('interface'), []).append(s)
    for iface, group in servers.items():
        if len(group) > 1 and any(s.get('_sim') for s in group):
            find('warn', f'{iface} would have {len(group)} DHCP servers ({", ".join(str(s.get("name")) for s in group)}): devices get random answers.',
                 '', 'Remove the old server or reuse its name.')
    for r in after.live('/ip/firewall/nat'):
        if r.get('_sim') and r.get('action') == 'dst-nat':
            try:
                if not after.connected(str(r.get('to-addresses', '')).split('-')[0]):
                    find('warn', f'Port forward to {r.get("to-addresses")}: no local network contains that address.',
                         _tile_of(after, '/ip/firewall/nat', r), 'Check the device address.')
            except ValueError:
                pass
            if str(r.get('dst-port')) in {str(v) for v in SERVICE_PORTS.values()}:
                find('warn', f'Port {r.get("dst-port")} is also used by the router itself; outside visitors now land on the device instead.',
                     _tile_of(after, '/ip/firewall/nat', r))
    dns = (after.rows('/ip/dns') or [{}])[0]
    if dns.get('_sim') and yes(dns.get('allow-remote-requests', 'false')):
        for wan in after.wan_interfaces():
            tr = after.trace(src_ip=INTERNET_IP, in_if=wan, dst='router', proto='udp', port=53)
            if tr['verdict'] == 'ok':
                find('warn', f'DNS answers everybody, including strangers on the internet side ({wan}). Attackers abuse open resolvers.', '',
                     'Add the “Shield the router” tile.')
                break

    # TapTap must keep its own way in
    mode = getattr(router, 'connection_mode', 'api') if router else 'api'
    if router is not None and mode != 'agent':
        b, a = management_probe(before, router, trusted), management_probe(after, router, trusted)
        if a:
            title, tr = a
            was_ok = bool(b) and b[1]['verdict'] == 'ok'
            if was_ok or tr['verdict'] == 'ok':           # when the picture cannot show TapTap's way in at all, nothing is claimed
                tests.append({'title': title, 'want': 'reaches', 'got': 'reaches' if tr['verdict'] == 'ok' else 'blocked', 'pass': tr['verdict'] == 'ok',
                              'tile': '', 'summary': tr['summary'], 'verdict': tr['verdict'], 'steps': tr['steps'], 'lifeline': True})
            if tr['verdict'] != 'ok' and was_ok:
                find('bad', f'After this change TapTap could no longer reach the router: {tr["summary"]}', '',
                     'Deploying this would cut TapTap off. Change the tile that closes the door, or allow TapTap’s address.')
    elif router is not None and before.wan_interfaces() and not after.wan_interfaces():
        find('bad', 'After this change the router has no internet line, so TapTap Link could not check in any more.')
    if router is not None and mode == 'agent':
        touched = sorted({o['resource'] for o in ops if o['resource'].startswith(('/ip/route', '/ip/dns', '/ip/dhcp-client', '/interface/pppoe-client'))})
        if touched:
            find('warn', 'This router talks to TapTap through TapTap Link. Changing ' + ', '.join(touched)
                 + ' can interrupt that for a moment; the automatic undo brings the old setup back if the Link does not return.')

    # one or two packets per tile
    wans = after.wan_interfaces()
    for tid, tile in by_tile.items():
        if tile['off']:
            continue
        p, kind = tile['params'], tile['type']
        mine = [c for c in after.changes if c['tile'] == tid]
        nets = []
        for c in mine:
            if c['resource'] == '/ip/address':
                try:
                    nets.append((ipaddress.IPv4Interface(c['row']['address']), c['row'].get('interface')))
                except (ValueError, KeyError):
                    pass
        if kind in ('lan', 'guest', 'hotspot') and nets:
            gw, iface = nets[0]
            device = sample_ip(gw)
            who = {'lan': 'A device', 'guest': 'A guest', 'hotspot': 'A hotspot customer'}[kind]
            test(f'{who} on {iface} ({device}) opens a website', 'reaches', after.trace(src_ip=device, in_if=iface, logged_in=True), tid)
            if kind == 'guest' and str(p.get('isolate', 'yes')).lower() != 'no':
                for other, other_if, _ in after.addresses():
                    if other_if != iface and other_if not in wans and other.network.is_private:
                        test(f'A guest tries your own network ({sample_ip(other)} on {other_if})', 'blocked',
                             after.trace(src_ip=device, in_if=iface, dst=sample_ip(other), proto='tcp', port=80), tid)
                        break
        elif kind == 'fw_basic':
            wan = p.get('wan') or (wans[0] if wans else '')
            test(f'A stranger on the internet tries WinBox through {wan}', 'blocked',
                 after.trace(src_ip=INTERNET_IP, in_if=wan, dst='router', proto='tcp', port=8291), tid)
            for addr, iface, _ in after.addresses():
                if iface != wan and iface not in wans and addr.network.is_private:
                    test(f'You, on {iface}, open WinBox', 'reaches',
                         after.trace(src_ip=sample_ip(addr), in_if=iface, dst='router', proto='tcp', port=8291), tid)
                    break
        elif kind == 'block_site':
            src = _any_lan(after, wans)
            first = str(p.get('sites', '')).replace(',', ' ').split()
            if src and first:
                site = first[0].lower().lstrip('*.')
                test(f'A device opens {site}', 'blocked', after.trace(src_ip=src[0], in_if=src[1], host=site, logged_in=True), tid)
                test('The same device opens another site', 'reaches', after.trace(src_ip=src[0], in_if=src[1], host='example.org', logged_in=True), tid)
        elif kind == 'block_device':
            src = _any_lan(after, wans)
            if src:
                mac = ''.join(c for c in str(p.get('mac', '')) if c.isalnum()).upper()
                mac = ':'.join(mac[i:i + 2] for i in range(0, 12, 2))
                test(f'{p.get("name") or "The blocked device"} opens a website', 'blocked',
                     after.trace(src_ip=src[0], in_if=src[1], mac=mac, logged_in=True), tid)
                test('Another device opens a website', 'reaches', after.trace(src_ip=src[0], in_if=src[1], mac='02:00:00:00:00:01', logged_in=True), tid)
        elif kind == 'port_forward' and wans:
            wan = p.get('wan') or wans[0]
            pub = next((str(a.ip) for a, i, _ in after.addresses() if i == wan), '')
            if pub:
                test(f'Somebody outside opens {p.get("protocol", "tcp")} port {p.get("port")}', 'reaches',
                     after.trace(src_ip=INTERNET_IP, in_if=wan, dst=pub, proto=str(p.get('protocol') or 'tcp'), port=int(p.get('port') or 0)), tid)
            else:
                find('info', f'The internet line {wan} has no address in this picture, so the open port cannot be tried here.', tid)
        elif kind in ('wan_dhcp', 'wan_pppoe', 'wan_static'):
            src = _any_lan(after, after.wan_interfaces())
            if src:
                test(f'A local device ({src[0]}) opens a website through the new line', 'reaches',
                     after.trace(src_ip=src[0], in_if=src[1], logged_in=True), tid)
    for t in tests:
        if not t['pass'] and not t.get('lifeline'):
            find('bad' if t['want'] == 'reaches' else 'warn',
                 f'{t["title"]}: expected it to {"get through" if t["want"] == "reaches" else "be stopped"}, but: {t["summary"]}', t['tile'])

    counts = {k: sum(1 for c in after.changes if c['kind'] == k) for k in ('new', 'changed', 'removed', 'same', 'missing')}
    return {'changes': after.changes, 'findings': findings, 'tests': tests, 'counts': counts, 'after': after,
            'safe': not any(f['level'] == 'bad' for f in findings)}


def _tile_of(vr, path, row):
    for c in vr.changes:
        if c['resource'] == path and c['row'].get('id') == row.get('id'):
            return c['tile']
    return ''


def _any_lan(vr, wans):
    for addr, iface, _ in vr.addresses():
        if iface not in wans and addr.network.is_private and addr.network.prefixlen <= 30:
            return sample_ip(addr), iface
    return None
