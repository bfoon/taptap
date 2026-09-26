from django.utils import timezone
import routeros_api


class MikroTikError(Exception):
    pass


def ros_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'yes', 'true', '1', 'on', 'running', 'enabled'}


class MikroTikService:
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
        data = {
            'name': code,
            'password': code,
            'profile': profile,
            'comment': comment,
            'disabled': 'yes' if disabled else 'no',
        }
        if limit_uptime:
            data['limit_uptime'] = limit_uptime
        return self.resource('/ip/hotspot/user').add(**data)

    def upsert_voucher(self, code, profile, limit_uptime=None, comment='TapTap voucher', disabled=False):
        users = self.resource('/ip/hotspot/user')
        existing = users.get(name=code)
        data = {
            'password': code,
            'profile': profile,
            'comment': comment,
            'disabled': 'yes' if disabled else 'no',
        }
        if limit_uptime:
            data['limit_uptime'] = limit_uptime
        if existing:
            users.set(id=existing[0]['id'], **data)
            return 'updated', existing[0]['id']
        item_id = users.add(name=code, **data)
        return 'created', item_id

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
        data = {
            'mac_address': mac,
            'type': kind,
            'comment': comment,
            'disabled': 'yes' if disabled else 'no',
        }
        if address:
            data['address'] = address
        if server:
            data['server'] = server
        return self.resource('/ip/hotspot/ip-binding').add(**data)

    def upsert_binding(self, binding):
        resource = self.resource('/ip/hotspot/ip-binding')
        existing = []
        if binding.mikrotik_id:
            try:
                existing = resource.get(id=binding.mikrotik_id)
            except Exception:
                existing = []
        if not existing and binding.mac_address:
            try:
                existing = resource.get(mac_address=binding.mac_address)
            except Exception:
                existing = []
        data = {
            'mac_address': binding.mac_address,
            'type': binding.binding_type,
            'comment': binding.comment or 'TapTap',
            'disabled': 'yes' if binding.disabled else 'no',
        }
        if binding.address:
            data['address'] = binding.address
        if binding.server:
            data['server'] = binding.server
        if existing:
            item_id = existing[0]['id']
            resource.set(id=item_id, **data)
            return 'updated', item_id
        return 'created', resource.add(**data)

    def toggle_binding(self, item_id, disabled):
        return self.resource('/ip/hotspot/ip-binding').set(id=item_id, disabled='yes' if disabled else 'no')

    def delete_binding(self, item_id):
        return self.resource('/ip/hotspot/ip-binding').remove(id=item_id)

    # ------------------------------ Topology -----------------------------
    def interfaces(self):
        return self.safe_get('/interface')

    def ethernet_interfaces(self):
        return self.safe_get('/interface/ethernet')

    def bridge_ports(self):
        return self.safe_get('/interface/bridge/port')

    def bridge_hosts(self):
        return self.safe_get('/interface/bridge/host')

    def neighbors(self):
        return self.safe_get('/ip/neighbor')

    def dhcp_leases(self):
        return self.safe_get('/ip/dhcp-server/lease')

    def arp(self):
        return self.safe_get('/ip/arp')

    def wifi_registrations(self):
        rows = self.safe_get('/interface/wifi/registration-table')
        if rows:
            return rows
        return self.safe_get('/interface/wireless/registration-table')

    def remote_caps(self):
        rows = self.safe_get('/interface/wifi/capsman/remote-cap')
        if rows:
            return rows
        return self.safe_get('/caps-man/remote-cap')

    def topology_data(self):
        return {
            'identity': self.identity(),
            'routerboard': self.routerboard(),
            'interfaces': self.interfaces(),
            'ethernet': self.ethernet_interfaces(),
            'bridge_ports': self.bridge_ports(),
            'bridge_hosts': self.bridge_hosts(),
            'neighbors': self.neighbors(),
            'dhcp_leases': self.dhcp_leases(),
            'arp': self.arp(),
            'wifi_registrations': self.wifi_registrations(),
            'remote_caps': self.remote_caps(),
            'captured_at': timezone.now(),
        }
