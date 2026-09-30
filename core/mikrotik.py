import re
import time

from django.conf import settings
from django.utils import timezone
import routeros_api

from .routeros_analysis import analyze_wan, parse_version


class MikroTikError(Exception):
    pass


def ros_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'yes', 'true', '1', 'on', 'running', 'enabled'}


SENSITIVE_FRAGMENTS = ('password', 'secret', 'private-key', 'private_key', 'passphrase', 'otp-secret', 'otp_secret')


def redact(value):
    """Remove secrets before configuration is stored or returned to the browser."""
    if isinstance(value, dict):
        out = {}
        for key, val in value.items():
            k = str(key)
            if any(fragment in k.lower() for fragment in SENSITIVE_FRAGMENTS):
                out[k] = '••••••••'
            else:
                out[k] = redact(val)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class MikroTikService:
    CONFIG_CATALOG = [
        ('System identity', '/system/identity'),
        ('System resources', '/system/resource'),
        ('Interfaces', '/interface'),
        ('Ethernet', '/interface/ethernet'),
        ('Interface lists', '/interface/list'),
        ('Interface list members', '/interface/list/member'),
        ('Bridges', '/interface/bridge'),
        ('Bridge ports', '/interface/bridge/port'),
        ('Bridge VLANs', '/interface/bridge/vlan'),
        ('VLAN interfaces', '/interface/vlan'),
        ('Bonding', '/interface/bonding'),
        ('WireGuard', '/interface/wireguard'),
        ('IP addresses', '/ip/address'),
        ('ARP', '/ip/arp'),
        ('Routes', '/ip/route'),
        ('Routing tables', '/routing/table'),
        ('Routing rules', '/routing/rule'),
        ('DNS', '/ip/dns'),
        ('DHCP clients', '/ip/dhcp-client'),
        ('DHCP servers', '/ip/dhcp-server'),
        ('DHCP networks', '/ip/dhcp-server/network'),
        ('DHCP leases', '/ip/dhcp-server/lease'),
        ('IP pools', '/ip/pool'),
        ('HotSpot servers', '/ip/hotspot'),
        ('HotSpot server profiles', '/ip/hotspot/profile'),
        ('HotSpot user profiles', '/ip/hotspot/user/profile'),
        ('HotSpot users', '/ip/hotspot/user'),
        ('HotSpot IP bindings', '/ip/hotspot/ip-binding'),
        ('Firewall filter', '/ip/firewall/filter'),
        ('Firewall NAT', '/ip/firewall/nat'),
        ('Firewall mangle', '/ip/firewall/mangle'),
        ('Firewall raw', '/ip/firewall/raw'),
        ('Firewall address lists', '/ip/firewall/address-list'),
        ('Simple queues', '/queue/simple'),
        ('Queue tree', '/queue/tree'),
        ('PPP profiles', '/ppp/profile'),
        ('PPP secrets', '/ppp/secret'),
        ('IP services', '/ip/service'),
        ('SNMP', '/snmp'),
        ('SNMP communities', '/snmp/community'),
        ('Neighbors', '/ip/neighbor'),
        ('Neighbor discovery settings', '/ip/neighbor/discovery-settings'),
        ('Users', '/user'),
        ('MAC server', '/tool/mac-server'),
        ('MAC Winbox', '/tool/mac-server/mac-winbox'),
        ('Bandwidth server', '/tool/bandwidth-server'),
        ('SOCKS', '/ip/socks'),
        ('Web proxy', '/ip/proxy'),
        ('UPnP', '/ip/upnp'),
        ('Cloud', '/ip/cloud'),
        ('SSH', '/ip/ssh'),
        ('RouterBOARD', '/system/routerboard'),
        ('PPPoE clients', '/interface/pppoe-client'),
    ]

    WRITE_PREFIXES = (
        '/interface', '/ip/address', '/ip/route', '/routing/table', '/routing/rule',
        '/ip/dns', '/ip/dhcp-client', '/ip/dhcp-server', '/ip/pool', '/ip/hotspot',
        '/ip/firewall', '/queue', '/ppp/profile', '/ppp/secret', '/ip/service', '/snmp',
    )

    def __init__(self, router, timeout=None):
        self.router = router
        self.pool = None
        self.api = None
        self.timeout = float(timeout or getattr(settings, 'MIKROTIK_TIMEOUT', 10))
        self.version = (0, 0, 0)

    def _pool(self, plaintext):
        host = str(self.router.ip_address or '').strip()
        # Accept "host:port" typed into the IP field (common with port-forwards / DDNS).
        port = self.router.api_port
        if host.count(':') == 1 and not host.startswith('['):
            host, _, maybe_port = host.partition(':')
            if maybe_port.isdigit():
                port = int(maybe_port)
        pool = routeros_api.RouterOsApiPool(
            host,
            username=self.router.username,
            password=self.router.password,
            port=port,
            use_ssl=self.router.use_ssl,
            # RouterOS api-ssl ships with a self-signed certificate on almost every
            # router; verification is opt-in so api-ssl works out of the box.
            ssl_verify=getattr(settings, 'MIKROTIK_SSL_VERIFY', False),
            ssl_verify_hostname=getattr(settings, 'MIKROTIK_SSL_VERIFY', False),
            plaintext_login=plaintext,
        )
        pool.socket_timeout = self.timeout  # per-instance; library default is 15s
        return pool

    def connect(self):
        """Connect with bounded timeouts; fall back to the legacy MD5 login (< 6.43)."""
        # TapTap Link routers sit behind NAT: reach them only through TapTap Tunnel.
        if getattr(self.router, 'connection_mode', 'api') == 'agent':
            from .tunnel import connect_via_tunnel, tunnel_enabled
            if tunnel_enabled():
                return connect_via_tunnel(self)
        if getattr(self.router, 'connection_mode', 'api') == 'agent' or not str(self.router.ip_address or '').strip():
            raise MikroTikError(f'{self.router.name} is managed through TapTap Link, so TapTap does not connect to it directly. '
                                'Its data comes from the Link check-ins and syncs; changes are sent as Link commands.')
        last = None
        for plaintext in (True, False):
            try:
                self.pool = self._pool(plaintext)
                self.api = self.pool.get_api()
                return self
            except Exception as exc:  # noqa: BLE001 - routeros_api raises many types
                last = exc
                try:
                    if self.pool:
                        self.pool.disconnect()
                except Exception:
                    pass
                text = str(exc).lower()
                # Only retry the legacy login on an authentication-style failure.
                if not any(w in text for w in ('login', 'password', 'cannot log', 'invalid user', 'failure')):
                    break
        raise MikroTikError(self.friendly_error(last)) from last

    def friendly_error(self, exc):
        """Explain *why* a router is unreachable from wherever TapTap is hosted."""
        text = str(exc or 'Unknown error')
        low = text.lower()
        host = str(self.router.ip_address or '')
        private = bool(re.match(r'^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.)', host))
        if any(w in low for w in ('timed out', 'timeout', 'no route', 'unreachable', 'refused', 'errno 111', 'errno 113')):
            hint = (f' {host} is a private address — it is only reachable if the TapTap server is on the same LAN or '
                    'connected by VPN (WireGuard/SSTP/OVPN).') if private else (
                    ' Check that the API service port is open to the TapTap server (IP > Services > api/api-ssl, '
                    'the "Available From" list, and the input firewall).')
            return f'{text}.{hint}'
        if 'ssl' in low or 'certificate' in low or 'wrong version' in low:
            return f'{text}. The port does not match the SSL setting (api = 8728 plain, api-ssl = 8729).'
        if any(w in low for w in ('login', 'password', 'invalid user', 'cannot log')):
            return f'{text}. Check the username/password and that the user group has the "api" policy.'
        return text

    def close(self):
        if self.pool:
            try:
                self.pool.disconnect()
            except Exception:
                pass

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()
        return False

    def resource(self, path):
        return self.api.get_resource(path)

    def safe_get(self, path, **filters):
        try:
            return self.resource(path).get(**filters)
        except Exception:
            return []

    @staticmethod
    def validate_resource_path(path):
        path = (path or '').strip()
        if not path.startswith('/') or '..' in path or not re.fullmatch(r'/[A-Za-z0-9_\-/]+', path):
            raise MikroTikError('Invalid RouterOS resource path.')
        return path.rstrip('/') or '/'

    def browse_resource(self, path, limit=1000):
        path = self.validate_resource_path(path)
        rows = self.safe_get(path)
        return redact([dict(x) for x in rows[:max(1, min(int(limit or 1000), 2000))]])

    def apply_change(self, path, operation, item_id='', fields=None):
        """Generic advanced CRUD for configuration resources, never arbitrary commands."""
        path = self.validate_resource_path(path)
        if not any(path == prefix or path.startswith(prefix + '/') for prefix in self.WRITE_PREFIXES):
            raise MikroTikError('This resource is read-only in TapTap Advanced Explorer.')
        operation = (operation or '').lower().strip()
        if operation not in {'add', 'set', 'remove'}:
            raise MikroTikError('Only add, set and remove operations are supported.')
        fields = fields or {}
        if not isinstance(fields, dict):
            raise MikroTikError('Configuration fields must be a JSON object.')
        # routeros-api maps Python keyword underscores to RouterOS dashes.
        clean_fields = {str(k).replace('-', '_'): v for k, v in fields.items() if v is not None}
        resource = self.resource(path)
        if operation == 'add':
            return resource.add(**clean_fields)
        if not item_id:
            raise MikroTikError('A RouterOS item ID is required for set/remove.')
        if operation == 'set':
            resource.set(id=item_id, **clean_fields)
            return item_id
        resource.remove(id=item_id)
        return item_id

    def test(self):
        row = self.resource('/system/resource').get()[0]
        self.version = parse_version(row.get('version', ''))
        return row

    def ros_version(self):
        if self.version == (0, 0, 0):
            rows = self.safe_get('/system/resource')
            if rows:
                self.version = parse_version(rows[0].get('version', ''))
        return self.version

    def identity(self):
        rows = self.safe_get('/system/identity')
        return rows[0] if rows else {}

    def routerboard(self):
        rows = self.safe_get('/system/routerboard')
        return rows[0] if rows else {}

    # ------------------------------ Hotspot ------------------------------
    def active_users(self):
        return self.resource('/ip/hotspot/active').get()

    def hotspot_users(self):
        return self.resource('/ip/hotspot/user').get()

    def hotspot_profiles(self):
        return self.resource('/ip/hotspot/user/profile').get()

    def hotspot_hosts(self):
        return self.safe_get('/ip/hotspot/host')

    def disconnect(self, item_id):
        return self.resource('/ip/hotspot/active').remove(id=item_id)

    def ensure_hotspot_profile(self, profile_name, max_devices=1, rate_limit=''):
        profiles = self.resource('/ip/hotspot/user/profile')
        existing = profiles.get(name=profile_name)
        values = {'shared_users': str(max(1, int(max_devices or 1)))}
        if rate_limit:
            values['rate_limit'] = rate_limit
        try:   # sticky sessions: devices log back in by themselves, idle devices stay logged in
            from .sticky import api_values
            values.update(api_values(self.router.business))
        except Exception:
            pass
        if existing:
            profiles.set(id=existing[0]['id'], **values)
            return existing[0]['id']
        return profiles.add(name=profile_name, **values)

    def add_voucher(self, code, profile, limit_uptime=None, comment='TapTap voucher', disabled=False, password=None):
        # Vouchers log in with their code as username AND password; members send their own password.
        data = {'name': code, 'password': password or code, 'profile': profile, 'comment': comment, 'disabled': 'yes' if disabled else 'no'}
        if limit_uptime:
            data['limit_uptime'] = limit_uptime
        return self.resource('/ip/hotspot/user').add(**data)

    def upsert_voucher(self, code, profile, limit_uptime=None, comment='TapTap voucher', disabled=False, password=None):
        users = self.resource('/ip/hotspot/user')
        existing = users.get(name=code)
        data = {'password': password or code, 'profile': profile, 'comment': comment, 'disabled': 'yes' if disabled else 'no'}
        if limit_uptime:
            data['limit_uptime'] = limit_uptime
        if existing:
            users.set(id=existing[0]['id'], **data)
            return 'updated', existing[0]['id']
        return 'created', users.add(name=code, **data)

    def disable_voucher(self, code):
        users = self.resource('/ip/hotspot/user').get(name=code)
        if users:
            self.resource('/ip/hotspot/user').set(id=users[0]['id'], disabled='yes')

    def enable_voucher(self, code):
        users = self.resource('/ip/hotspot/user').get(name=code)
        if not users:
            return False
        self.resource('/ip/hotspot/user').set(id=users[0]['id'], disabled='no')
        return True

    def extend_voucher(self, code, hours=None, seconds=None, total=False):
        """Enable and add time on top of the time already used, so the router's own
        limit-uptime does not lock the voucher out again. With ``total`` (a voucher
        not used yet) the new limit is the whole new duration. Returns the new limit
        (or '' when the voucher has no router-side limit)."""
        add = int(seconds) if seconds else int(hours or 0) * 3600
        from .sync import _routeros_seconds
        users = self.resource('/ip/hotspot/user').get(name=code)
        if not users:
            return None
        row = users[0]
        data = {'disabled': 'no'}
        limit = ''
        if _routeros_seconds(row.get('limit-uptime', row.get('limit_uptime', ''))) > 0:
            total = add if total else _routeros_seconds(row.get('uptime', '')) + add
            d, rest = divmod(total, 86400); h, rest = divmod(rest, 3600); m, s = divmod(rest, 60)
            limit = ''.join(f'{v}{u}' for v, u in ((d, 'd'), (h, 'h'), (m, 'm'), (s, 's')) if v) or '1h'
            data['limit_uptime'] = limit
        self.resource('/ip/hotspot/user').set(id=row['id'], **data)
        return limit

    def set_user_password(self, name, password):
        """Change a hotspot user's password and drop its live sessions so the new one is needed.
        Returns False when the user is not on this router."""
        users = self.resource('/ip/hotspot/user')
        rows = users.get(name=name)
        if not rows:
            return False
        users.set(id=rows[0]['id'], password=password)
        try:
            self.reset_active_by_name(name)
        except Exception:
            pass
        return True

    def reset_active_by_name(self, code):
        for row in self.resource('/ip/hotspot/active').get(user=code):
            self.resource('/ip/hotspot/active').remove(id=row['id'])

    # ----------------------------- IP binding ----------------------------
    def bindings(self):
        return self.resource('/ip/hotspot/ip-binding').get()

    def add_binding(self, mac, kind='bypassed', comment='TapTap', address='', server='all', disabled=False):
        data = {'mac_address': mac, 'type': kind, 'comment': comment, 'disabled': 'yes' if disabled else 'no'}
        if address: data['address'] = address
        if server: data['server'] = server
        return self.resource('/ip/hotspot/ip-binding').add(**data)

    def upsert_binding(self, binding):
        resource = self.resource('/ip/hotspot/ip-binding')
        existing = []
        if binding.mikrotik_id:
            try: existing = resource.get(id=binding.mikrotik_id)
            except Exception: existing = []
        if not existing and binding.mac_address:
            try: existing = resource.get(mac_address=binding.mac_address)
            except Exception: existing = []
        data = {'mac_address': binding.mac_address, 'type': binding.binding_type, 'comment': binding.comment or 'TapTap', 'disabled': 'yes' if binding.disabled else 'no'}
        if binding.address: data['address'] = binding.address
        if binding.server: data['server'] = binding.server
        if existing:
            item_id = existing[0]['id']; resource.set(id=item_id, **data); return 'updated', item_id
        return 'created', resource.add(**data)

    def toggle_binding(self, item_id, disabled):
        return self.resource('/ip/hotspot/ip-binding').set(id=item_id, disabled='yes' if disabled else 'no')

    def delete_binding(self, item_id):
        return self.resource('/ip/hotspot/ip-binding').remove(id=item_id)

    def get_binding(self, item_id):
        """Read one binding back from the router (to confirm a change really happened)."""
        for row in self.resource('/ip/hotspot/ip-binding').get():
            if str(row.get('id', '')) == str(item_id):
                return row
        return None

    def set_binding(self, item_id, **fields):
        clean = {k.replace('-', '_'): v for k, v in fields.items()}
        return self.resource('/ip/hotspot/ip-binding').set(id=item_id, **clean)

    # ------------------------------ Topology -----------------------------
    def interfaces(self): return self.safe_get('/interface')
    def ethernet_interfaces(self): return self.safe_get('/interface/ethernet')
    def bridge_ports(self): return self.safe_get('/interface/bridge/port')
    def bridge_hosts(self): return self.safe_get('/interface/bridge/host')
    def neighbors(self): return self.safe_get('/ip/neighbor')
    def dhcp_leases(self): return self.safe_get('/ip/dhcp-server/lease')
    def arp(self): return self.safe_get('/ip/arp')
    def routes(self): return self.safe_get('/ip/route')
    def mangle(self): return self.safe_get('/ip/firewall/mangle')
    def routing_tables(self): return self.safe_get('/routing/table')
    def routing_rules(self): return self.safe_get('/routing/rule')
    def bonding(self): return self.safe_get('/interface/bonding')
    def nat(self): return self.safe_get('/ip/firewall/nat')
    def firewall_filter(self): return self.safe_get('/ip/firewall/filter')
    def dhcp_clients(self): return self.safe_get('/ip/dhcp-client')
    def addresses(self): return self.safe_get('/ip/address')
    def hotspot_servers(self): return self.safe_get('/ip/hotspot')

    def default_routes(self):
        rows = self.safe_get('/ip/route', dst_address='0.0.0.0/0')
        if rows:
            return rows
        # Some builds reject the query form; filter client-side instead.
        return [r for r in self.routes() if str(r.get('dst-address', '')) == '0.0.0.0/0']

    def wifi_registrations(self):
        rows = self.safe_get('/interface/wifi/registration-table')
        return rows or self.safe_get('/interface/wireless/registration-table')

    def remote_caps(self):
        rows = self.safe_get('/interface/wifi/capsman/remote-cap')
        return rows or self.safe_get('/caps-man/remote-cap')

    def set_interface_disabled(self, interface_name, disabled):
        resource = self.resource('/interface')
        rows = resource.get(name=interface_name)
        if not rows:
            raise MikroTikError(f'Interface {interface_name} was not found.')
        resource.set(id=rows[0]['id'], disabled='yes' if disabled else 'no')

    def ensure_bridge_port(self, interface_name, bridge_name):
        ports = self.resource('/interface/bridge/port')
        existing = ports.get(interface=interface_name)
        if existing:
            ports.set(id=existing[0]['id'], bridge=bridge_name, disabled='no')
            return 'updated'
        ports.add(interface=interface_name, bridge=bridge_name, disabled='no')
        return 'created'

    def remove_bridge_port(self, interface_name):
        ports = self.resource('/interface/bridge/port')
        rows = ports.get(interface=interface_name)
        for row in rows:
            ports.remove(id=row['id'])
        return len(rows)

    def configure_wan_dhcp_nat(self, interface_name):
        """Explicit quick recipe: enable port, remove it from bridge, add DHCP client + masquerade."""
        self.set_interface_disabled(interface_name, False)
        removed = self.remove_bridge_port(interface_name)
        dhcp = self.resource('/ip/dhcp-client')
        existing = dhcp.get(interface=interface_name)
        if existing:
            dhcp.set(id=existing[0]['id'], disabled='no')
            dhcp_action = 'updated'
        else:
            dhcp.add(interface=interface_name, disabled='no', add_default_route='yes', use_peer_dns='yes')
            dhcp_action = 'created'
        nat = self.resource('/ip/firewall/nat')
        rules = nat.get(chain='srcnat')
        existing_nat = None
        for row in rules:
            if str(row.get('out-interface', row.get('out_interface', ''))) == interface_name and str(row.get('action', '')) == 'masquerade':
                existing_nat = row; break
        if existing_nat:
            nat.set(id=existing_nat['id'], disabled='no', comment='TapTap Quick WAN')
            nat_action = 'updated'
        else:
            nat.add(chain='srcnat', action='masquerade', out_interface=interface_name, comment='TapTap Quick WAN', disabled='no')
            nat_action = 'created'
        return {'bridge_ports_removed': removed, 'dhcp_client': dhcp_action, 'nat': nat_action}

    @staticmethod
    def _route_interface(row):
        """Kept for backwards compatibility; the analysis module does the real work."""
        from .routeros_analysis import resolve_gateways
        hops = resolve_gateways(row)
        return hops[0]['interface'] if hops else ''

    def analyze_load_balancing(self, routes=None, mangle=None, bonding=None, routing_tables=None,
                               routing_rules=None, addresses=None, dhcp_clients=None, interfaces=None,
                               hotspot_servers=None):
        declared = list(self.router.interface_roles.filter(role='wan').values_list('interface_name', flat=True)) if getattr(self.router, 'pk', None) else []
        return redact(analyze_wan(
            routes=routes if routes is not None else self.routes(),
            mangle=mangle if mangle is not None else self.mangle(),
            bonding=bonding if bonding is not None else self.bonding(),
            routing_tables=routing_tables if routing_tables is not None else self.routing_tables(),
            routing_rules=routing_rules if routing_rules is not None else self.routing_rules(),
            addresses=addresses if addresses is not None else self.addresses(),
            dhcp_clients=dhcp_clients if dhcp_clients is not None else self.dhcp_clients(),
            interfaces=interfaces if interfaces is not None else self.interfaces(),
            hotspot_servers=hotspot_servers if hotspot_servers is not None else self.hotspot_servers(),
            declared_wans=declared,
        ))

    # --------------------------- Live traffic ----------------------------
    def live_traffic(self, names):
        """Real bits-per-second straight from RouterOS (monitor-traffic once).

        Falls back to counter deltas between two quick reads when the monitor
        command is not permitted for this API user.
        """
        names = [n for n in dict.fromkeys(str(x) for x in names if x)][:32]
        if not names:
            return {}
        out = {}
        try:
            rows = self.resource('/interface').call('monitor-traffic', {'interface': ','.join(names), 'once': ''})
            for row in rows:
                name = str(row.get('name', ''))
                if name:
                    out[name] = {
                        'rx_bps': int(row.get('rx-bits-per-second', 0) or 0),
                        'tx_bps': int(row.get('tx-bits-per-second', 0) or 0),
                        'rx_pps': int(row.get('rx-packets-per-second', 0) or 0),
                        'tx_pps': int(row.get('tx-packets-per-second', 0) or 0),
                    }
            if out:
                return out
        except Exception:
            pass
        wanted = set(names)
        def counters():
            return {str(r.get('name')): (int(r.get('rx-byte', 0) or 0), int(r.get('tx-byte', 0) or 0))
                    for r in self.interfaces() if str(r.get('name')) in wanted}
        first, t0 = counters(), time.monotonic()
        time.sleep(1.0)
        second, t1 = counters(), time.monotonic()
        span = max(0.2, t1 - t0)
        for name, (rx, tx) in second.items():
            prx, ptx = first.get(name, (rx, tx))
            out[name] = {'rx_bps': int(max(0, rx - prx) * 8 / span), 'tx_bps': int(max(0, tx - ptx) * 8 / span), 'rx_pps': 0, 'tx_pps': 0}
        return out

    def wan_telemetry(self, stored_sections=None):
        """Cheap poll for the live load-balancing view.

        Only default routes, interfaces and the WAN monitor are read live; the
        slow-changing inputs (mangle, addresses, routing rules) come from the
        stored configuration snapshot when available.
        """
        stored = stored_sections or {}
        def rows(label, loader):
            sec = stored.get(label)
            if sec and isinstance(sec.get('rows'), list):
                return sec['rows']
            return loader()
        interfaces = self.interfaces()
        analysis = self.analyze_load_balancing(
            routes=self.default_routes(),
            mangle=rows('Firewall mangle', self.mangle),
            bonding=rows('Bonding', self.bonding),
            routing_tables=rows('Routing tables', self.routing_tables),
            routing_rules=rows('Routing rules', self.routing_rules),
            addresses=rows('IP addresses', self.addresses),
            dhcp_clients=self.dhcp_clients(),
            interfaces=interfaces,
            hotspot_servers=rows('HotSpot servers', self.hotspot_servers),
        )
        names = [l['interface'] for l in analysis['wan_links'] if l['interface']]
        for bond in analysis.get('bonds', []):
            names.extend(bond['slaves'])
        traffic = self.live_traffic(names)
        compact = [{'name': str(r.get('name', '')), 'running': str(r.get('running', '')).lower() == 'true',
                    'disabled': str(r.get('disabled', '')).lower() == 'true',
                    'rx_byte': int(r.get('rx-byte', 0) or 0), 'tx_byte': int(r.get('tx-byte', 0) or 0)}
                   for r in interfaces if str(r.get('name', '')) in set(names)]
        return {'load_balancing': analysis, 'traffic': traffic, 'interfaces': compact,
                'captured_at': timezone.now().isoformat()}

    # ------------------------- Security remediation ------------------------
    SECURITY_FIXES = {
        # key: (resource path, lookup filter or None for singleton, fields, description)
        'svc-telnet': ('/ip/service', {'name': 'telnet'}, {'disabled': 'yes'}, 'Disable the Telnet service'),
        'svc-ftp': ('/ip/service', {'name': 'ftp'}, {'disabled': 'yes'}, 'Disable the FTP service'),
        'ip-socks': ('/ip/socks', None, {'enabled': 'no'}, 'Disable the SOCKS proxy'),
        'ip-upnp': ('/ip/upnp', None, {'enabled': 'no'}, 'Disable UPnP'),
        'bw-server': ('/tool/bandwidth-server', None, {'enabled': 'no'}, 'Disable the bandwidth-test server'),
        'ip-proxy-open': ('/ip/proxy', None, {'enabled': 'no'}, 'Disable the open web proxy'),
    }

    def apply_security_fix(self, key):
        """Apply one whitelisted fix. None of these can cut off TapTap's API access."""
        if key not in self.SECURITY_FIXES:
            raise MikroTikError('This finding has no automatic fix; follow the manual steps.')
        path, lookup, fields, _label = self.SECURITY_FIXES[key]
        resource = self.resource(path)
        clean = {k.replace('-', '_'): v for k, v in fields.items()}
        if lookup is None:
            resource.call('set', clean)
        else:
            rows = resource.get(**lookup)
            if not rows:
                raise MikroTikError(f'{path} item was not found on this router.')
            item_id = rows[0].get('id') or rows[0].get('.id')
            if not item_id:
                raise MikroTikError(f'RouterOS did not return an item id for {path}.')
            resource.set(id=item_id, **clean)
        return path

    def configuration_snapshot(self):
        sections = {}
        for label, path in self.CONFIG_CATALOG:
            rows = self.safe_get(path)
            sections[label] = {'path': path, 'rows': redact([dict(x) for x in rows[:500]]), 'count': len(rows)}
        lb = self.analyze_load_balancing(
            routes=sections['Routes']['rows'],
            mangle=sections['Firewall mangle']['rows'],
            bonding=sections['Bonding']['rows'],
            routing_tables=sections['Routing tables']['rows'],
            routing_rules=sections['Routing rules']['rows'],
            addresses=sections['IP addresses']['rows'],
            dhcp_clients=sections['DHCP clients']['rows'],
            interfaces=sections['Interfaces']['rows'],
            hotspot_servers=sections['HotSpot servers']['rows'],
        )
        return {'sections': sections, 'load_balancing': lb, 'captured_at': timezone.now()}

    def telemetry(self, stored_sections=None):
        return self.wan_telemetry(stored_sections)

    def topology_data(self):
        return {
            'identity': self.identity(), 'routerboard': self.routerboard(), 'interfaces': self.interfaces(),
            'ethernet': self.ethernet_interfaces(), 'bridge_ports': self.bridge_ports(), 'bridge_hosts': self.bridge_hosts(),
            'neighbors': self.neighbors(), 'dhcp_leases': self.dhcp_leases(), 'dhcp_clients': self.dhcp_clients(),
            'arp': self.arp(), 'hotspot_hosts': self.hotspot_hosts(), 'active_users': self.safe_get('/ip/hotspot/active'),
            'wifi_registrations': self.wifi_registrations(), 'remote_caps': self.remote_caps(), 'routes': self.routes(),
            'mangle': self.mangle(), 'routing_tables': self.routing_tables(), 'routing_rules': self.routing_rules(),
            'bonding': self.bonding(), 'addresses': self.addresses(), 'hotspot_servers': self.hotspot_servers(),
            'captured_at': timezone.now(),
        }
