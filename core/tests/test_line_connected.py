"""A line that is plugged in and carrying traffic must never show "Not connected".

What people saw: ether5 had its address (192.168.2.140) and gateway, the Internet paths view showed traffic on it,
but the Gateways tab said "Not connected" — and because of that "Share the load" quietly left it out (Balance:
Single path, 0 % of traffic) and customers could not be moved to it.

Why: only a DHCP status of exactly "bound" counted. RouterOS renews a lease half-way through ("renewing..."), then
asks any server ("rebinding..."); many modems ignore the renewal, so a working line sits in those states for hours.
TapTap Link's copy of /interface or of the client status could also be missing or older than the routes.
"""
from unittest import mock

from django.test import SimpleTestCase

from core import line_hub as hub, wan
from core.tests.test_line_hub import Base, CONFIG, DHCP_CLIENTS, IFACES, ROUTES


def clients(**ether5):
    return [dict(c, **ether5) if c['interface'] == 'ether5' else c for c in DHCP_CLIENTS]


def quiet_routes():
    """ROUTES with nothing active through ether5 (as when its cable is really out)."""
    return [dict(r, active='false') if 'ether5' in r['gateway'] else r for r in ROUTES]


def ifaces(**ether5):
    return [dict(i, **ether5) if i['name'] == 'ether5' else i for i in IFACES]


class LeaseStates(SimpleTestCase):
    def test_a_line_renewing_or_rebinding_its_lease_still_has_its_address(self):
        for st in ('bound', 'renewing...', 'rebinding...', 'Bound'):
            self.assertTrue(wan.dhcp_has_lease({'status': st, 'address': '192.168.2.140/24'}), st)
        for st in ('searching...', 'requesting...', 'stopped', 'error'):
            self.assertFalse(wan.dhcp_has_lease({'status': st, 'address': '192.168.2.140/24'}), st)

    def test_no_status_in_the_row_reads_the_one_by_one_answer_or_the_address(self):
        self.assertTrue(wan.dhcp_has_lease({'address': '192.168.2.140/24'}))
        self.assertTrue(wan.dhcp_has_lease({}, {'status': 'bound', 'gateway': '192.168.2.1'}))
        self.assertFalse(wan.dhcp_has_lease({}, {'status': 'searching...'}))
        self.assertFalse(wan.dhcp_has_lease({}))

    def test_an_active_route_through_the_port_shows_it_is_up(self):
        self.assertTrue(wan.route_live_on([{'gateway': '192.168.2.1', 'immediate-gw': '192.168.2.1%ether5', 'active': 'true'}], 'ether5'))
        self.assertTrue(wan.route_live_on([{'gateway': '192.168.2.1%ether5', 'active': 'true'}], 'ether5'))
        self.assertFalse(wan.route_live_on([{'gateway': '192.168.2.1%ether5', 'active': 'false'}], 'ether5'))
        self.assertFalse(wan.route_live_on([{'gateway': '192.168.2.1%ether5', 'active': 'true', 'disabled': 'true'}], 'ether5'))
        self.assertFalse(wan.route_live_on([{'gateway': '192.168.1.1%ether1', 'active': 'true'}], 'ether5'))


class GatewaysTab(Base):
    def ether5(self):
        rows = hub.open_rows(self.router, live=False)
        try:
            return next(l for l in hub.lines_now(self.router, rows) if l['interface'] == 'ether5')
        finally:
            rows.close()

    def test_renewing_line_is_connected(self):
        self.snapshot(**{'/ip/dhcp-client': clients(status='renewing...')})
        l = self.ether5()
        self.assertTrue(l['up'])
        self.assertEqual(l['status'], 'renewing...')

    def test_row_without_status_uses_the_explicit_reading(self):
        self.snapshot(**{'/ip/dhcp-client': clients(status=''),
                         '/ip/dhcp-client/gateways': [{'interface': 'ether5', 'gateway': '192.168.2.1', 'status': 'bound'}]})
        l = self.ether5()
        self.assertTrue(l['up'])
        self.assertEqual(l['status'], 'bound')

    def test_old_interface_copy_is_overruled_by_an_active_route(self):
        routes = ROUTES + [{'id': '*R9', 'dst-address': '0.0.0.0/0', 'gateway': '192.168.2.1%ether5', 'routing-table': 'TT-ether5',
                            'comment': 'TapTap WAN #abc · QCell', 'active': 'true'}]
        self.snapshot(**{'/interface': ifaces(running='false'), '/ip/route': routes})
        self.assertTrue(self.ether5()['up'])

    def test_traffic_coming_in_proves_the_line_is_up(self):
        self.snapshot(**{'/interface': ifaces(running='false'), '/ip/route': quiet_routes()})
        with mock.patch.object(hub, 'live_rates', return_value={'ether5': {'rx_bps': 273, 'tx_bps': 221}}):
            l = self.ether5()
        self.assertTrue(l['up'])
        self.assertTrue(l['proved_by_traffic'])

    def test_a_line_really_without_an_address_still_says_so(self):
        self.snapshot(**{'/ip/dhcp-client': clients(status='searching...', address='')})
        with mock.patch.object(hub, 'live_rates', return_value={'ether5': {'rx_bps': 900, 'tx_bps': 0}}):
            l = self.ether5()
        self.assertFalse(l['up'])                  # broadcast noise on the cable is not a connection

    def test_unplugged_line_without_traffic_or_routes_stays_down(self):
        self.snapshot(**{'/interface': ifaces(running='false'), '/ip/route': quiet_routes()})
        self.assertFalse(self.ether5()['up'])

    def test_share_the_load_now_includes_the_renewing_line(self):
        self.snapshot(**{'/ip/dhcp-client': clients(status='renewing...')})
        rows = hub.open_rows(self.router, live=False)
        try:
            line_list = hub.lines_now(self.router, rows)
        finally:
            rows.close()
        facts = {'v7': True, 'links': [
            {'interface': 'ether1', 'type': 'dhcp', 'gateway': '192.168.1.1'},
            {'interface': 'ether5', 'type': 'dhcp', 'gateway': '192.168.2.1'}]}
        self.setup.config = dict(CONFIG)
        out = hub.share_config(self.setup, facts, 'ether5', line_list)
        cfg = out[0] if isinstance(out, tuple) else out
        self.assertEqual(sorted(l['interface'] for l in cfg['links'] if l['enabled']), ['ether1', 'ether5'])
