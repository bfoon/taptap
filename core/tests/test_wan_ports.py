"""Which port does a check host (8.8.8.8 / 1.1.1.1) really leave through? Never guess."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from core import wan
from core.routeros_analysis import analyze_wan, port_for_ip
from core.wan_analysis import dhcp_rows

IFS = [{'name': 'ether1', 'running': 'true'}, {'name': 'ether3', 'running': 'true'}]
SAME_SUBNET = [{'address': '192.168.1.50/24', 'interface': 'ether1'},     # Starlink router: 192.168.1.1
               {'address': '192.168.1.77/24', 'interface': 'ether3'}]     # QCell 4G router: also 192.168.1.1


def routes(gw8='192.168.1.1', gw1='192.168.1.1', c8='', c1=''):
    return [{'dst-address': '0.0.0.0/0', 'gateway': '8.8.8.8', 'distance': '1', 'active': 'true'},
            {'dst-address': '0.0.0.0/0', 'gateway': '1.1.1.1', 'distance': '1', 'active': 'true'},
            {'dst-address': '8.8.8.8/32', 'gateway': gw8, 'scope': '10', 'comment': c8},
            {'dst-address': '1.1.1.1/32', 'gateway': gw1, 'scope': '10', 'comment': c1}]


def ports(lb):
    return {l['gateway']: l['interface'] for l in lb['wan_links'] if l['gateway']}


class TwoModemsOnTheSameAddress(SimpleTestCase):
    def test_no_longer_guesses_the_first_port(self):
        lb = analyze_wan(routes=routes(), addresses=SAME_SUBNET, interfaces=IFS)
        self.assertEqual(ports(lb), {'8.8.8.8': '', '1.1.1.1': ''})       # before: both said ether1
        text = ' '.join(lb['warnings'])
        self.assertIn('192.168.1.1, which is the modem on ether1 and on ether3', text)
        self.assertIn('e.g. 192.168.1.1%ether1', text)

    def test_saved_internet_lines_setup_decides(self):
        lb = analyze_wan(routes=routes(), addresses=SAME_SUBNET, interfaces=IFS, declared_wans=['ether1', 'ether3'],
                         hints={'8.8.8.8': 'ether1', '1.1.1.1': 'ether3'})
        self.assertEqual(ports(lb), {'8.8.8.8': 'ether1', '1.1.1.1': 'ether3'})
        self.assertFalse(any(l['state'] == 'no-route' for l in lb['wan_links']))
        text = ' '.join(lb['warnings'])
        self.assertIn('ether1 and ether3 are both on 192.168.1.0/24', text)
        self.assertIn('8.8.8.8 is shown on ether1 because your Internet lines setup says so', text)

    def test_hint_never_overrides_what_the_router_says(self):
        lb = analyze_wan(routes=routes(gw8='192.168.1.1%ether3'), addresses=SAME_SUBNET, interfaces=IFS,
                         hints={'8.8.8.8': 'ether1'})
        self.assertEqual(ports(lb)['8.8.8.8'], 'ether3')

    def test_taptap_port_marker_in_the_comment(self):
        lb = analyze_wan(routes=routes(c8='TapTap WAN #a1 · check 8.8.8.8 via QCell gw=ether3;'),
                         addresses=SAME_SUBNET, interfaces=IFS)
        self.assertEqual(ports(lb)['8.8.8.8'], 'ether3')

    def test_two_dhcp_clients_with_the_same_gateway_are_not_mixed_up(self):
        lb = analyze_wan(routes=routes(), addresses=SAME_SUBNET, interfaces=IFS,
                         dhcp_clients=[{'interface': 'ether1', 'gateway': '192.168.1.1'},
                                       {'interface': 'ether3', 'gateway': '192.168.1.1'}])
        self.assertEqual(ports(lb), {'8.8.8.8': '', '1.1.1.1': ''})


class OtherWaysAPortIsFound(SimpleTestCase):
    def test_lte_passthrough_gateway_outside_the_subnet_is_found_by_arp(self):
        addrs = [{'address': '192.168.1.50/24', 'interface': 'ether1'}, {'address': '100.71.4.10/32', 'interface': 'ether3'}]
        lb = analyze_wan(routes=routes(gw8='100.71.4.9'), addresses=addrs, interfaces=IFS,
                         arp=[{'address': '100.71.4.9', 'interface': 'ether3', 'mac-address': 'AA:BB:CC:00:00:03'}])
        self.assertEqual(ports(lb), {'8.8.8.8': 'ether3', '1.1.1.1': 'ether1'})

    def test_missing_host_route_says_so(self):
        r = [x for x in routes() if x['dst-address'] != '8.8.8.8/32']
        lb = analyze_wan(routes=r, addresses=[{'address': '192.168.1.50/24', 'interface': 'ether1'}], interfaces=IFS)
        self.assertTrue(any('there is no route for 8.8.8.8 itself' in w for w in lb['warnings']), lb['warnings'])

    def test_gateway_on_no_port_says_so(self):
        lb = analyze_wan(routes=routes(gw8='10.9.9.1'), addresses=[{'address': '192.168.1.50/24', 'interface': 'ether1'}],
                         interfaces=IFS)
        self.assertTrue(any('10.9.9.1, which is not on any port' in w for w in lb['warnings']), lb['warnings'])

    def test_port_for_ip_unique_subnet(self):
        self.assertEqual(port_for_ip('192.168.8.1', [(__import__('ipaddress').ip_network('192.168.8.0/24'), 'ether3')]),
                         ('ether3', []))


class DesignerFindsTheDhcpGateway(SimpleTestCase):
    def test_from_the_client_row(self):
        self.assertEqual(wan.dhcp_gateway({'interface': 'ether1', 'gateway': '192.168.1.1'}), '192.168.1.1')

    def test_from_taptap_links_explicit_reading(self):
        self.assertEqual(wan.dhcp_gateway({'interface': 'ether1', 'status': 'bound'}, {'gateway': '192.168.1.1'}), '192.168.1.1')

    def test_from_its_dynamic_default_route(self):
        r = [{'dst-address': '0.0.0.0/0', 'gateway': '192.168.8.1', 'dhcp': 'true', 'immediate-gw': '192.168.8.1%ether3'}]
        self.assertEqual(wan.dhcp_gateway({'interface': 'ether3'}, None, r), '192.168.8.1')

    def test_from_a_taptap_route_for_the_port(self):
        r = [{'dst-address': '1.1.1.1/32', 'gateway': '192.168.8.1%ether3', 'comment': 'TapTap WAN #a · check 1.1.1.1 via QCell gw=ether3;'}]
        self.assertEqual(wan.dhcp_gateway({'interface': 'ether3'}, None, r), '192.168.8.1')

    def test_from_the_dhcp_server(self):
        self.assertEqual(wan.dhcp_gateway({'interface': 'ether3', 'dhcp-server': '192.168.8.1'}), '192.168.8.1')

    def test_same_modem_address_on_both_lines_is_explained_not_an_error(self):
        cfg, errors, warnings = wan.normalize({'strategy': 'ecmp', 'links': [
            {'interface': 'ether1', 'type': 'dhcp', 'gateway': '192.168.1.1', 'check': '8.8.8.8', 'label': 'Starlink'},
            {'interface': 'ether3', 'type': 'dhcp', 'gateway': '192.168.1.1', 'check': '1.1.1.1', 'label': 'QCell'}]},
            {'v7': True, 'links': []})
        self.assertEqual(errors, [])
        self.assertTrue(any('Starlink and QCell both use the modem address 192.168.1.1' in w for w in warnings))
        plan = wan.build_plan(cfg, {'v7': True, 'links': []}, run='r1')
        gws = {op['fields']['dst-address']: op['fields']['gateway'] for op in plan['ops']
               if op['op'] == 'add' and op['path'] == '/ip/route' and op['fields']['dst-address'].endswith('/32')}
        self.assertEqual(gws, {'8.8.8.8/32': '192.168.1.1%ether1', '1.1.1.1/32': '192.168.1.1%ether3'})


class LinkRouter(TestCase):
    """The real path for a TapTap Link router: the designer and the page read the synced configuration."""
    def setUp(self):
        from core.models import Business, Router, RouterAgent, RouterConfigSnapshot, WanSetup
        u = User.objects.create_user('o', password='x')
        biz = Business.objects.create(user=u, business_name='B', owner_name='O', phone='1',
                                      trial_ends_at=timezone.now() + timedelta(days=7))
        self.router = Router.objects.create(business=biz, name='TapTap K', ip_address='10.0.0.1', username='a',
                                            password='p', connection_mode='agent')
        RouterAgent.objects.create(router=self.router, token_hash='h' * 64, last_seen_at=timezone.now(),
                                   enrolled_at=timezone.now(), ros_version='7.16.2')
        WanSetup.objects.create(router=self.router, config={'strategy': 'ecmp', 'links': [
            {'interface': 'ether1', 'check': '8.8.8.8'}, {'interface': 'ether3', 'check': '1.1.1.1'}]})
        sec = lambda path, rows: {'path': path, 'rows': rows}
        RouterConfigSnapshot.objects.create(router=self.router, sections={
            # what `print as-value` sent: bound, but no gateway
            'DHCP clients': sec('/ip/dhcp-client', [{'interface': 'ether1', 'status': 'bound', 'address': '192.168.1.50/24'},
                                                    {'interface': 'ether3', 'status': 'bound', 'address': '192.168.1.77/24'}]),
            'DHCP client gateways': sec('/ip/dhcp-client/gateways', [{'interface': 'ether1', 'gateway': '192.168.1.1', 'server': '192.168.1.1', 'status': 'bound'},
                                                                     {'interface': 'ether3', 'gateway': '192.168.1.1', 'server': '192.168.1.1', 'status': 'bound'}]),
            'Routes': sec('/ip/route', routes()),
            'IP addresses': sec('/ip/address', SAME_SUBNET),
            'Interfaces': sec('/interface', IFS),
        })

    def test_designer_now_sees_both_gateways(self):
        from core.linkops import SnapshotService
        facts = wan.detect(SnapshotService(self.router))
        gw = {l['interface']: l['gateway'] for l in facts['links'] if l['type'] == 'dhcp'}
        self.assertEqual(gw, {'ether1': '192.168.1.1', 'ether3': '192.168.1.1'})

    def test_page_shows_each_check_host_on_its_own_port(self):
        from core.wan_analysis import for_router
        self.assertEqual(ports(for_router(self.router)), {'8.8.8.8': 'ether1', '1.1.1.1': 'ether3'})

    def test_merge_of_explicit_gateways(self):
        from core.models import RouterConfigSnapshot
        rows = dhcp_rows(RouterConfigSnapshot.objects.get(router=self.router).sections)
        self.assertEqual({r['interface']: r['gateway'] for r in rows}, {'ether1': '192.168.1.1', 'ether3': '192.168.1.1'})

    def test_link_sync_asks_for_the_gateway_explicitly(self):
        from core.agent_inventory import SOURCES, inventory_piece_script

        class C:
            pk, params = 1, {'kind': 'dhcp_gateways'}
        script = inventory_piece_script(C, 'https://t', 'yes', 'n')
        self.assertIn('dhcp-client get \\$i gateway', script)
        self.assertIn('dhcp_gateways', SOURCES)
