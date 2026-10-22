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
        self.assertTrue(any('not tied to any Internet line' in w for w in lb['warnings']))


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


class CheckHostWithoutItsOwnRoute(SimpleTestCase):
    """hAP on Starlink (ether1) + QCell (ether3), 22 Oct 2026: 0.0.0.0/0 via 8.8.8.8 had no 8.8.8.8/32, so the page drew a
    third Internet line called “8.8.8.8” (port not found) and QCell as “No route yet”."""
    ROUTES = [
        {'dst-address': '1.1.1.1/32', 'gateway': '192.168.1.1%ether1', 'scope': '10', 'active': 'true', 'comment': 'TapTap check Starlink 1'},
        {'dst-address': '0.0.0.0/0', 'gateway': '1.1.1.1', 'immediate-gw': '192.168.1.1%ether1', 'distance': '1',
         'check-gateway': 'ping', 'active': 'true', 'comment': 'TapTap Starlink 1 pinned'},
        {'dst-address': '0.0.0.0/0', 'gateway': '8.8.8.8', 'immediate-gw': '', 'distance': '1', 'check-gateway': 'ping',
         'active': 'false', 'comment': 'TapTap Starlink 2 pinned'},
    ]

    def test_no_ghost_line_the_route_belongs_to_the_free_wan_port(self):
        lb = run(self.ROUTES)
        by = {l['interface']: l for l in lb['wan_links']}
        self.assertEqual(sorted(by), ['ether1', 'ether3'])          # two lines, never a third one called 8.8.8.8
        self.assertEqual(by['ether1']['state'], 'active')
        self.assertEqual(by['ether3']['state'], 'broken')
        self.assertEqual(by['ether3']['broken_via'], '8.8.8.8')
        self.assertTrue(by['ether3']['guessed_port'])
        text = ' '.join(lb['warnings'])
        self.assertIn('no route that sends 8.8.8.8 out of ether3', text)
        self.assertIn('Repair wiring', text)
        self.assertNotIn('marked as WAN but no default route', text)
        self.assertNotIn('Multiple WAN interfaces exist', text)

    def test_ports_with_dhcp_clients_count_when_nothing_is_dragged_into_wan(self):
        lb = run(self.ROUTES, declared=(), dhcp_clients=[{'interface': 'ether1', 'gateway': '192.168.1.1'},
                                                          {'interface': 'ether3', 'gateway': '192.168.8.1'}])
        self.assertEqual(sorted(l['interface'] for l in lb['wan_links']), ['ether1', 'ether3'])

    def test_unclear_owner_stays_a_broken_route_not_a_line(self):
        lb = run(self.ROUTES, declared=('ether1', 'ether3', 'ether5'))     # two free WAN ports: no guessing
        ghost = next(l for l in lb['wan_links'] if not l['interface'])
        self.assertEqual(ghost['state'], 'broken')
        self.assertEqual(ghost['label'], 'Route via 8.8.8.8')
        self.assertTrue(any('not tied to any Internet line' in w for w in lb['warnings']))

    def test_once_the_host_route_exists_both_lines_work(self):
        routes = self.ROUTES + [{'dst-address': '8.8.8.8/32', 'gateway': '192.168.8.1%ether3', 'scope': '10', 'active': 'true'}]
        routes[2] = {**routes[2], 'active': 'true', 'immediate-gw': '192.168.8.1%ether3'}
        lb = run(routes)
        self.assertEqual(lb['method'], 'ECMP')
        self.assertEqual({l['interface']: l['state'] for l in lb['wan_links']}, {'ether1': 'active', 'ether3': 'active'})


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


from datetime import timedelta  # noqa: E402

from django.contrib.auth.models import User  # noqa: E402
from django.test import TestCase  # noqa: E402
from django.utils import timezone  # noqa: E402


def link_rows():
    """Rows exactly as the Link inventory uploads them (RouterOS 7 `print as-value`, every value a string)."""
    return {
        'Routes': {'rows': [
            {'.id': '*1', 'dst-address': '0.0.0.0/0', 'gateway': '1.1.1.1', 'distance': '1', 'scope': '30',
             'target-scope': '11', 'check-gateway': 'ping', 'active': 'true', 'disabled': 'false', 'static': 'true'},
            {'.id': '*2', 'dst-address': '0.0.0.0/0', 'gateway': '8.8.8.8', 'distance': '1', 'scope': '30',
             'target-scope': '11', 'check-gateway': 'ping', 'active': 'true', 'disabled': 'false', 'static': 'true'},
            {'.id': '*3', 'dst-address': '1.1.1.1/32', 'gateway': '192.168.8.1', 'distance': '1', 'scope': '10',
             'active': 'true', 'disabled': 'false'},
            {'.id': '*4', 'dst-address': '8.8.8.8/32', 'gateway': '192.168.1.1', 'distance': '1', 'scope': '10',
             'active': 'true', 'disabled': 'false'},
            {'.id': '*5', 'dst-address': '192.168.8.0/24', 'gateway': 'ether3', 'dynamic': 'true', 'connect': 'true',
             'active': 'true'},
        ]},
        'IP addresses': {'rows': [{'address': '192.168.8.100/24', 'interface': 'ether3', 'actual-interface': 'ether3',
                                   'disabled': 'false', 'dynamic': 'true'},
                                  {'address': '192.168.1.50/24', 'interface': 'ether1', 'actual-interface': 'ether1',
                                   'disabled': 'false', 'dynamic': 'true'}]},
        'Interfaces': {'rows': [{'name': 'ether1', 'running': 'true'}, {'name': 'ether3', 'running': 'true'}]},
    }


# What the old code saved for this router: two ghost lines and two "No route yet" lines (the screenshot).
OLD_SAVED = {'method': 'ECMP', 'wan_links': [
    {'id': 'gw_1_1_1_1', 'interface': '', 'gateway': '1.1.1.1', 'state': 'active'},
    {'id': 'gw_8_8_8_8', 'interface': '', 'gateway': '8.8.8.8', 'state': 'active'},
    {'id': 'ether3', 'interface': 'ether3', 'gateway': '', 'state': 'no-route'},
    {'id': 'ether1', 'interface': 'ether1', 'gateway': '', 'state': 'no-route'}]}


class SavedAnalysisFromOlderCode(TestCase):
    def setUp(self):
        from core.models import Business, Router, RouterAgent, RouterConfigSnapshot, RouterInterfaceRole
        u = User.objects.create_user('o', password='x')
        biz = Business.objects.create(user=u, business_name='B', owner_name='O', phone='1',
                                      trial_ends_at=timezone.now() + timedelta(days=7))
        self.router = Router.objects.create(business=biz, name='TapTap K', ip_address='10.0.0.1', username='a',
                                            password='p', connection_mode='agent')
        RouterAgent.objects.create(router=self.router, token_hash='h' * 64, last_seen_at=timezone.now(),
                                   enrolled_at=timezone.now())
        for iface, isp in (('ether3', 'QCell'), ('ether1', 'Starlink')):
            RouterInterfaceRole.objects.create(router=self.router, interface_name=iface, role='wan', isp_name=isp)
        self.snap = RouterConfigSnapshot.objects.create(router=self.router, sections=link_rows(),
                                                        load_balancing=OLD_SAVED)

    def assert_two_real_lines(self, lb):
        self.assertEqual(sorted(l['interface'] for l in lb['wan_links']), ['ether1', 'ether3'])
        self.assertFalse(any(l['state'] == 'no-route' for l in lb['wan_links']))
        self.assertTrue(all(l['state'] == 'active' for l in lb['wan_links']))

    def test_live_view_of_a_link_router_is_redone_without_waiting_for_a_sync(self):
        from core.linklive import telemetry
        self.assert_two_real_lines(telemetry(self.router)['load_balancing'])

    def test_upgrade_is_saved_once(self):
        from core.models import RouterConfigSnapshot
        from core.routeros_analysis import ANALYSIS_VERSION
        from core.wan_analysis import for_router
        self.assert_two_real_lines(for_router(self.router))
        saved = RouterConfigSnapshot.objects.get(pk=self.snap.pk).load_balancing
        self.assertEqual(saved['version'], ANALYSIS_VERSION)
        with mock_analyze() as again:
            for_router(self.router)
        again.assert_not_called()                                  # current analysis is not redone on every read

    def test_names_still_applied(self):
        from core.linklive import telemetry
        from core.wan_names import named
        lb = named(self.router, telemetry(self.router)['load_balancing'])
        self.assertEqual(sorted(l['label'] for l in lb['wan_links']), ['QCell', 'Starlink'])

    def test_traffic_report_records_the_real_ports(self):
        from core.traffic import wan_interfaces
        self.assertEqual(sorted(wan_interfaces(self.router)), ['ether1', 'ether3'])

    def test_router_page_and_telemetry_endpoint(self):
        from django.test import Client
        c = Client()
        c.force_login(self.router.business.user)
        data = c.get(f'/routers/{self.router.pk}/telemetry/').json()
        self.assertTrue(data['success'], data)
        self.assertEqual(sorted(l['label'] for l in data['load_balancing']['wan_links']), ['QCell', 'Starlink'])


def mock_analyze():
    from unittest import mock
    return mock.patch('core.wan_analysis.analyze_sections')
