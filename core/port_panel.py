"""What the Control Center front panel draws: physical ports on the panel, everything else in RouterOS.

Physical = Ethernet / SFP jacks and real Wi-Fi radios. Bridges, loopback, WireGuard, VLANs, tunnels,
PPPoE, bonds and virtual Wi-Fi access points are RouterOS constructs, so they are listed inside the
RouterOS block. A physical port that is a member of a bridge shows what it gets from that bridge
(its IP address, DHCP server, HotSpot…) as "via bridge1", so what you configure on bridge1 is visible
on every port that carries it.
"""
import re

PHYSICAL_TYPES = {'ether', 'wlan', 'wifi', 'wifiwave2', 'wireless', 'sfp'}
WIFI_TYPES = {'wlan', 'wifi', 'wifiwave2', 'wireless'}
PHYSICAL_NAME = re.compile(r'^(ether|sfp|combo|qsfp|wlan|wifi)', re.I)

# RouterOS interface type → (label, Bootstrap icon) for the chips in the RouterOS block.
VIRTUAL_KINDS = {
    'bridge': ('Bridge', 'bi-diagram-2'), 'loopback': ('Loopback', 'bi-arrow-repeat'), 'wg': ('WireGuard', 'bi-shield-lock'),
    'vlan': ('VLAN', 'bi-tags'), 'pppoe-out': ('PPPoE client', 'bi-telephone-outbound'), 'pppoe-in': ('PPPoE server', 'bi-telephone-inbound'),
    'bond': ('Bond', 'bi-link-45deg'), 'lte': ('LTE modem', 'bi-reception-4'), 'veth': ('Container', 'bi-box'),
    'gre-tunnel': ('GRE tunnel', 'bi-signpost-split'), 'eoip': ('EoIP tunnel', 'bi-signpost-split'), 'ipip-tunnel': ('IPIP tunnel', 'bi-signpost-split'),
    'l2tp-out': ('L2TP', 'bi-signpost-split'), 'sstp-out': ('SSTP', 'bi-signpost-split'), 'ovpn-out': ('OpenVPN', 'bi-signpost-split'),
    'vrrp': ('VRRP', 'bi-hdd-stack'), 'macvlan': ('MACVLAN', 'bi-tags'), 'wlan-virtual': ('Virtual Wi-Fi', 'bi-wifi'),
}


def _natural(name):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r'(\d+)', name or '')]


def _raw(iface):
    return iface.raw_data if isinstance(iface.raw_data, dict) else {}


def kind_of(iface):
    """'eth' | 'wifi' for physical ports, else the RouterOS virtual type ('bridge', 'wg', …)."""
    t = (iface.interface_type or str(_raw(iface).get('type', '')) or '').strip().lower()
    raw = _raw(iface)
    if t in WIFI_TYPES or (not t and re.match(r'^(wlan|wifi)', iface.name or '', re.I)):
        # A virtual access point (second SSID) has a master radio: it is not a radio of its own.
        return 'wlan-virtual' if raw.get('master-interface') or raw.get('master') else 'wifi'
    if t in PHYSICAL_TYPES or (not t and PHYSICAL_NAME.match(iface.name or '')):
        return 'eth'
    return t or 'virtual'


def bridge_of(iface):
    bp = _raw(iface).get('bridge_port') or {}
    return str(bp.get('bridge') or '') if isinstance(bp, dict) else ''


def _rows(snap, title):
    try:
        return list(((snap.sections or {}).get(title) or {}).get('rows') or []) if snap else []
    except Exception:
        return []


def config_tags(snap):
    """{interface name: [(icon, short label), …]} — what runs on each interface, from the snapshot."""
    tags = {}
    add = lambda name, icon, label: name and tags.setdefault(str(name), []).append((icon, label))
    for r in _rows(snap, 'IP addresses'):
        if str(r.get('disabled', '')).lower() not in ('true', 'yes'):
            add(r.get('interface') or r.get('actual-interface'), 'bi-hash', str(r.get('address', 'IP address')))
    for title, icon, label in (('DHCP servers', 'bi-diagram-3', 'DHCP server'), ('DHCP clients', 'bi-cloud-download', 'DHCP client'),
                               ('HotSpot servers', 'bi-wifi', 'HotSpot'), ('PPPoE clients', 'bi-telephone-outbound', 'PPPoE')):
        for r in _rows(snap, title):
            if str(r.get('disabled', '')).lower() not in ('true', 'yes'):
                add(r.get('interface'), icon, label)
    return tags


def panel(interfaces, snap=None):
    """Split interfaces for the front panel. Adds, on each object:
        ui_kind      'eth' | 'wifi' | virtual type
        ui_bridge    bridge it belongs to (physical ports)
        ui_tags      [(icon, label)] configured directly on it
        ui_via       [(icon, label)] it gets from its bridge
        ui_members   names of the ports in it (bridges)
        ui_kind_label / ui_icon   for the RouterOS chips
    Returns (ports, virtual) — physical ports in natural order, then the RouterOS-only interfaces."""
    tags = config_tags(snap)
    by_bridge = {}
    for i in interfaces:
        i.ui_kind = kind_of(i)
        i.ui_bridge = bridge_of(i)
        i.ui_tags = tags.get(i.name, [])
        if i.ui_bridge:
            by_bridge.setdefault(i.ui_bridge, []).append(i.name)
    ports, virtual = [], []
    for i in interfaces:
        i.ui_via = tags.get(i.ui_bridge, []) if i.ui_bridge else []
        i.ui_members = sorted(by_bridge.get(i.name, []), key=_natural)
        label, icon = VIRTUAL_KINDS.get(i.ui_kind, (i.ui_kind.replace('-', ' ').title() if i.ui_kind not in ('eth', 'wifi') else '', 'bi-hdd-network'))
        i.ui_kind_label, i.ui_icon = label, icon
        (ports if i.ui_kind in ('eth', 'wifi') else virtual).append(i)
    ports.sort(key=lambda i: (i.ui_kind == 'wifi', _natural(i.name)))
    # Bridges first (they carry the configuration), then the rest, each in natural order.
    virtual.sort(key=lambda i: (i.ui_kind != 'bridge', i.ui_kind == 'loopback', _natural(i.name)))
    return ports, virtual
