import re
from django.utils import timezone
import routeros_api


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
        ('Neighbors', '/ip/neighbor'),
    ]

    WRITE_PREFIXES = (
        '/interface', '/ip/address', '/ip/route', '/routing/table', '/routing/rule',
        '/ip/dns', '/ip/dhcp-client', '/ip/dhcp-server', '/ip/pool', '/ip/hotspot',
        '/ip/firewall', '/queue', '/ppp/profile', '/ppp/secret', '/ip/service', '/snmp',
    )

    def __init__(self, router):
        self.router = router
        self.pool = None
        self.api = None

    def connect(self):
        try:
            self.pool = routeros_api.RouterOsApiPool(
                self.router.ip_address,
                username=self.router.username,
                password=self.router.password,
                port=self.router.api_port,
                use_ssl=self.router.use_ssl,
                plaintext_login=True,
            )
            self.api = self.pool.get_api()
            return self
        except Exception as exc:
            raise MikroTikError(str(exc)) from exc

    def close(self):
        if self.pool:
            self.pool.disconnect()

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
        return self.resource('/system/resource').get()[0]

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
        if existing:
            profiles.set(id=existing[0]['id'], **values)
            return existing[0]['id']
        return profiles.add(name=profile_name, **values)

    def add_voucher(self, code, profile, limit_uptime=None, comment='TapTap voucher', disabled=False):
        data = {'name': code, 'password': code, 'profile': profile, 'comment': comment, 'disabled': 'yes' if disabled else 'no'}
        if limit_uptime:
            data['limit_uptime'] = limit_uptime
        return self.resource('/ip/hotspot/user').add(**data)

    def upsert_voucher(self, code, profile, limit_uptime=None, comment='TapTap voucher', disabled=False):
        users = self.resource('/ip/hotspot/user')
        existing = users.get(name=code)
        data = {'password': code, 'profile': profile, 'comment': comment, 'disabled': 'yes' if disabled else 'no'}
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
        immediate = str(row.get('immediate-gw', row.get('immediate_gw', '')) or '')
        match = re.search(r'%([^,\s]+)', immediate)
        if match:
            return match.group(1)
        gateway = str(row.get('gateway', '') or '')
        # Gateway may itself be an interface name.
        if gateway and not re.match(r'^\d{1,3}(?:\.\d{1,3}){3}', gateway) and ':' not in gateway:
            return gateway.split(',')[0].strip()
        return ''

    def analyze_load_balancing(self, routes=None, mangle=None, bonding=None, routing_tables=None, routing_rules=None):
        routes = [dict(x) for x in (routes if routes is not None else self.routes())]
        mangle = [dict(x) for x in (mangle if mangle is not None else self.mangle())]
        bonding = [dict(x) for x in (bonding if bonding is not None else self.bonding())]
        routing_tables = [dict(x) for x in (routing_tables if routing_tables is not None else self.routing_tables())]
        routing_rules = [dict(x) for x in (routing_rules if routing_rules is not None else self.routing_rules())]
        default_routes = [r for r in routes if str(r.get('dst-address', r.get('dst_address', ''))) in {'0.0.0.0/0', '::/0'}]
        active_defaults = [r for r in default_routes if not ros_bool(r.get('disabled', False)) and (ros_bool(r.get('active', True)) or 'active' not in r)]
        pcc_rules = [r for r in mangle if str(r.get('per-connection-classifier', r.get('per_connection_classifier', ''))).strip()]
        marked_rules = [r for r in mangle if r.get('new-routing-mark') or r.get('new_routing_mark') or r.get('new-connection-mark') or r.get('new_connection_mark')]
        bonds = [b for b in bonding if not ros_bool(b.get('disabled', False))]
        distances = sorted({str(r.get('distance', '1')) for r in default_routes})
        same_distance = len({str(r.get('distance', '1')) for r in active_defaults}) <= 1 if active_defaults else False
        if pcc_rules:
            method = 'PCC'
            description = 'Per-connection-classifier rules detected in firewall mangle.'
        elif any(',' in str(r.get('gateway', '')) for r in active_defaults) or (len(active_defaults) > 1 and same_distance):
            method = 'ECMP'
            description = 'Multiple equal-cost default paths detected.'
        elif bonds:
            method = 'Bonding'
            description = 'One or more active bonding interfaces detected.'
        elif len(default_routes) > 1 and len(distances) > 1:
            method = 'Failover'
            description = 'Multiple default routes with different distances detected.'
        else:
            method = 'Single WAN'
            description = 'No multi-WAN load-balancing policy was detected.'

        wans = []
        seen = set()
        for row in default_routes:
            iface = self._route_interface(row)
            gateway = str(row.get('gateway', '') or '')
            table = str(row.get('routing-table', row.get('routing_table', 'main')) or 'main')
            key = (iface, gateway, table)
            if key in seen: continue
            seen.add(key)
            wans.append({
                'interface': iface,
                'gateway': gateway,
                'distance': str(row.get('distance', '1')),
                'table': table,
                'active': ros_bool(row.get('active', True)) and not ros_bool(row.get('disabled', False)),
                'check_gateway': str(row.get('check-gateway', row.get('check_gateway', '')) or ''),
            })
        for bond in bonds:
            name = str(bond.get('name', ''))
            if name and not any(x['interface'] == name for x in wans):
                wans.append({'interface': name, 'gateway': '', 'distance': '', 'table': 'bonding', 'active': ros_bool(bond.get('running', True)), 'check_gateway': ''})

        return redact({
            'method': method,
            'description': description,
            'configured': method != 'Single WAN',
            'wan_links': wans,
            'pcc_rule_count': len(pcc_rules),
            'marked_rule_count': len(marked_rules),
            'routing_table_count': len(routing_tables),
            'routing_rule_count': len(routing_rules),
            'bond_count': len(bonds),
        })

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
        )
        return {'sections': sections, 'load_balancing': lb, 'captured_at': timezone.now()}

    def telemetry(self):
        interfaces = []
        for raw in self.interfaces():
            row = dict(raw)
            interfaces.append({
                'name': str(row.get('name', '')),
                'running': ros_bool(row.get('running', False)),
                'disabled': ros_bool(row.get('disabled', False)),
                'rx_byte': int(row.get('rx-byte', row.get('rx_byte', 0)) or 0),
                'tx_byte': int(row.get('tx-byte', row.get('tx_byte', 0)) or 0),
            })
        return {'interfaces': interfaces, 'load_balancing': self.analyze_load_balancing(), 'captured_at': timezone.now().isoformat()}

    def topology_data(self):
        return {
            'identity': self.identity(), 'routerboard': self.routerboard(), 'interfaces': self.interfaces(),
            'ethernet': self.ethernet_interfaces(), 'bridge_ports': self.bridge_ports(), 'bridge_hosts': self.bridge_hosts(),
            'neighbors': self.neighbors(), 'dhcp_leases': self.dhcp_leases(), 'dhcp_clients': self.dhcp_clients(),
            'arp': self.arp(), 'hotspot_hosts': self.hotspot_hosts(), 'active_users': self.safe_get('/ip/hotspot/active'),
            'wifi_registrations': self.wifi_registrations(), 'remote_caps': self.remote_caps(), 'routes': self.routes(),
            'mangle': self.mangle(), 'routing_tables': self.routing_tables(), 'routing_rules': self.routing_rules(),
            'bonding': self.bonding(), 'captured_at': timezone.now(),
        }
