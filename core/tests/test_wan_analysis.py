"""Load-balancing analysis: recursive routes (1.1.1.1 / 8.8.8.8 check hosts) must show the real ISP lines."""
from django.test import SimpleTestCase

from core.mikrotik import MikroTikService
from core.routeros_analysis import analyze_wan

ADDRESSES = [{'address': '192.168.8.100/24', 'interface': 'ether3'},     # QCell modem LAN
             {'address': '192.168.1.50/24', 'interface': 'ether1'}]      # Starlink router LAN
INTERFACES = [{'name': 'ether1', 'running': 'true'}, {'name': 'ether3', 'running': 'true'},
              {'name': 'bridge-hotspot', 'running': 'true'}]


def recursive_routes(qcell_gw='192.168.8.1', starlink_gw='192.168.1.1', d1='1', d2='1', **extra):
    """The classic two-ISP recipe: default routes via check hosts, each host pinned to one ISP."""
    return [
        {'dst-address': '0.0.0.0/0', 'gateway': '1.1.1.1', 'distance': d1, 'check-gateway': 'ping', 'active': 'true',
         'target-scope': '11', **extra.get('r1', {})},
        {'dst-address': '0.0.0.0/0', 'gateway': '8.8.8.8', 'distance': d2, 'check-gateway': 'ping',
         'active': 'true' if d1 == d2 else 'false', 'target-scope': '11', **extra.get('r2', {})},
        {'dst-address': '1.1.1.1/32', 'gateway': qcell_gw, 'scope': '10', 'active': 'true'},
        {'dst-address': '8.8.8.8/32', 'gateway': starlink_gw, 'scope': '10', 'active': 'true'},
    ]


def run(routes, declared=('ether3', 'ether1'), **kw):
    return analyze_wan(routes=routes, addresses=ADDRESSES, interfaces=INTERFACES, declared_wans=list(declared), **kw)


class RecursiveEcmp(SimpleTestCase):
    def test_shows_two_isp_lines_not_four(self):
        lb = run(recursive_routes())
        by = {l['interface']: l for l in lb['wan_links']}
        self.assertEqual(lb['method'], 'ECMP')
        self.assertEqual(sorted(by), ['ether1', 'ether3'])               # no '1.1.1.1' / '8.8.8.8' ghost lines
        self.assertEqual(by['ether3']['gateway'], '1.1.1.1')
        self.assertEqual(by['ether3']['next_hop'], '192.168.8.1')
        self.assertEqual(by['ether1']['gateway'], '8.8.8.8')
        for l in by.values():
            self.assertEqual(l['state'], 'active')                       # not "No route yet"
            self.assertEqual(l['expected_share'], 50)
            self.assertTrue(l['recursive'])
        self.assertEqual(lb['warnings'], [])

    def test_v7_immediate_gw_of_the_next_hop_is_used(self):
        routes = recursive_routes(r1={'immediate-gw': '192.168.8.1%ether3'}, r2={'immediate-gw': '192.168.1.1%ether1'})
        routes = routes[:2]                                              # even without the host routes
        lb = run(routes)
        self.assertEqual(sorted(l['interface'] for l in lb['wan_links']), ['ether1', 'ether3'])

    def test_v6_gateway_status_recursive_via(self):
        routes = recursive_routes(r1={'gateway-status': '1.1.1.1 recursive via 192.168.8.1 ether3'},
                                  r2={'gateway-status': '8.8.8.8 recursive via 192.168.1.1 ether1'})[:2]
        lb = run(routes)
        self.assertEqual(sorted(l['interface'] for l in lb['wan_links']), ['ether1', 'ether3'])

    def test_v6_unreachable_check_host_marks_the_line_down(self):
        routes = recursive_routes(r1={'gateway-status': '1.1.1.1 unreachable'})
        lb = run(routes)
        by = {l['interface']: l for l in lb['wan_links']}
        self.assertEqual(by['ether3']['state'], 'down')

    def test_recursive_failover(self):
        lb = run(recursive_routes(d1='1', d2='2'))
        by = {l['interface']: l for l in lb['wan_links']}
        self.assertEqual(lb['method'], 'Failover')
        self.assertEqual(by['ether3']['role'], 'primary')
        self.assertEqual(by['ether1']['role'], 'backup')

    def test_two_level_recursion(self):
        routes = recursive_routes(qcell_gw='9.9.9.9') + [{'dst-address': '9.9.9.9/32', 'gateway': '192.168.8.1'}]
        lb = run(routes)
        self.assertIn('ether3', [l['interface'] for l in lb['wan_links']])

    def test_recursion_loop_does_not_hang(self):
        routes = [{'dst-address': '0.0.0.0/0', 'gateway': '1.1.1.1'},
                  {'dst-address': '1.1.1.1/32', 'gateway': '2.2.2.2'},
                  {'dst-address': '2.2.2.2/32', 'gateway': '1.1.1.1'}]
        lb = run(routes, declared=())
        self.assertTrue(any('Could not tell which port' in w for w in lb['warnings']))


class WhyALineCarriesNothing(SimpleTestCase):
    def test_both_check_hosts_through_the_same_isp_is_reported(self):
        # The usual mistake: 1.1.1.1/32 and 8.8.8.8/32 both point at the Starlink gateway.
        lb = run(recursive_routes(qcell_gw='192.168.1.1'))
        by = {l['interface']: l for l in lb['wan_links']}
        self.assertEqual(by['ether3']['state'], 'no-route')
        text = ' '.join(lb['warnings'])
        self.assertIn('all leave through ether1', text)
        self.assertIn('ether3 is marked as WAN but no default route uses it', text)

    def test_missing_host_route_is_reported(self):
        routes = [r for r in recursive_routes() if r['dst-address'] != '1.1.1.1/32']
        lb = run(routes)
        self.assertTrue(any('1.1.1.1' in w and 'host route' in w for w in lb['warnings']))


class NothingElseChanged(SimpleTestCase):
    def test_plain_dhcp_single_wan(self):
        lb = analyze_wan(routes=[{'dst-address': '0.0.0.0/0', 'gateway': '192.168.1.1', 'dhcp': 'true', 'active': 'true'}],
                         addresses=ADDRESSES, interfaces=INTERFACES)
        self.assertEqual(lb['method'], 'Single WAN')
        self.assertEqual(lb['wan_links'][0]['interface'], 'ether1')
        self.assertFalse(lb['wan_links'][0]['recursive'])

    def test_plain_ecmp_two_gateways(self):
        lb = analyze_wan(routes=[{'dst-address': '0.0.0.0/0', 'gateway': '192.168.1.1,192.168.8.1', 'active': 'true'}],
                         addresses=ADDRESSES, interfaces=INTERFACES)
        self.assertEqual(lb['method'], 'ECMP')
        self.assertEqual(sorted(l['interface'] for l in lb['wan_links']), ['ether1', 'ether3'])

    def test_interface_gateway(self):
        lb = analyze_wan(routes=[{'dst-address': '0.0.0.0/0', 'gateway': 'pppoe-out1', 'active': 'true'}],
                         interfaces=INTERFACES + [{'name': 'pppoe-out1', 'type': 'pppoe-out', 'running': 'true'}])
        self.assertEqual(lb['wan_links'][0]['interface'], 'pppoe-out1')
        self.assertEqual(lb['wan_links'][0]['source'], 'pppoe')


class LivePollReadsHostRoutes(SimpleTestCase):
    def test_recursive_defaults_need_the_host_routes(self):
        self.assertTrue(MikroTikService._needs_recursion(recursive_routes()[:2]))
        self.assertTrue(MikroTikService._needs_recursion([{'gateway': '1.1.1.1', 'gateway-status': '1.1.1.1 recursive via 192.168.8.1 ether3'}]))

    def test_interface_or_resolved_defaults_do_not(self):
        self.assertFalse(MikroTikService._needs_recursion([{'gateway': 'pppoe-out1'}]))
        self.assertFalse(MikroTikService._needs_recursion([{'gateway': '192.168.1.1', 'immediate-gw': '192.168.1.1%ether1'}]))


class ProviderNamesInWarnings(SimpleTestCase):
    def test_warning_uses_the_provider_name(self):
        from core.wan_names import apply_names
        lb = apply_names(run(recursive_routes(qcell_gw='192.168.1.1')), {'ether3': 'QCell', 'ether1': 'Starlink'})
        text = ' '.join(lb['warnings'])
        self.assertIn('QCell (ether3) is marked as WAN', text)
        self.assertIn('Starlink (ether1)', text)

    def test_ether1_does_not_match_inside_ether10(self):
        from core.wan_names import _with_names
        self.assertEqual(_with_names('ether10 down', {'ether1': 'Starlink'}), 'ether10 down')
