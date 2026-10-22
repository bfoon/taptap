"""Topology map: each Internet line is one box under the Internet, and ISP equipment is never a site router.
Built from the hAP on Starlink (ether1) + QCell (ether3), 22 Oct 2026: the QCell modem (found on ether3) sat alone in
the row under the Internet and Starlink's cable ran through it; the QCell modem on 192.168.0.1 was also drawn as a
TP-Link-style router hanging from the hAP."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from core import netgraph, site_routers
from core.models import Business, Router, RouterConfigSnapshot, RouterDevice, RouterNeighbor
from core.routeros_analysis import ANALYSIS_VERSION

MODEM = '50:04:4F:2F:AB:D6'


class Base(TestCase):
    def setUp(self):
        cache.clear()
        u = User.objects.create_user('owner', password='x')
        self.biz = Business.objects.create(user=u, business_name='B', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=7))
        self.r = Router.objects.create(business=self.biz, name='TapTap K', ip_address='10.0.0.1', username='a', password='p',
                                       status='Online')
        RouterConfigSnapshot.objects.create(router=self.r, sections={
            'DHCP clients': {'rows': [{'interface': 'ether1', 'gateway': '192.168.1.1'},
                                      {'interface': 'ether3', 'gateway': '192.168.0.1'},
                                      {'interface': 'bridge1', 'gateway': '10.9.9.1'}]},
        }, load_balancing={'version': ANALYSIS_VERSION, 'method': 'Single WAN', 'wan_links': [
            {'id': 'ether1', 'interface': 'ether1', 'label': 'ether1', 'gateway': '1.1.1.1', 'state': 'active', 'expected_share': 100},
            {'id': 'ether3', 'interface': 'ether3', 'label': 'ether3', 'gateway': '8.8.8.8', 'state': 'broken', 'expected_share': 0},
        ], 'warnings': []})
        RouterNeighbor.objects.create(router=self.r, neighbor_key=MODEM, mac_address=MODEM, address='192.168.0.1',
                                      interface_name='ether3', identity='', board='B315')

    def device(self, mac, port, ip='192.168.0.1', host='TL-WR840N'):
        return RouterDevice.objects.create(router=self.r, device_key=mac, mac_address=mac, ip_address=ip, hostname=host,
                                           interface_name=port, is_online=True)


class OneBoxPerLine(Base):
    def test_lines_hang_straight_from_the_internet(self):
        g = netgraph.build_graph(self.biz)
        ids = {n['id']: n for n in g['nodes']}
        self.assertNotIn(f'nb:{MODEM}', ids)                                # no modem box in front of the lines
        wans = [n for n in g['nodes'] if n['type'] == 'wan']
        self.assertEqual(sorted(n['iface'] for n in wans), ['ether1', 'ether3'])
        for w in wans:
            self.assertTrue(any(e['source'] == 'internet' and e['target'] == w['id'] for e in g['edges']))
        qcell = next(w for w in wans if w['iface'] == 'ether3')
        self.assertEqual(qcell['modem']['mac'], MODEM)
        self.assertEqual(qcell['modem']['ip'], '192.168.0.1')
        self.assertEqual(qcell['status'], 'offline')                       # broken line drawn as not carrying traffic

    def test_modem_is_not_a_customer(self):
        self.device(MODEM, 'ether3', host='')
        g = netgraph.build_graph(self.biz)
        self.assertFalse(any(n['type'] == 'clients' and n.get('port') == 'ether3' for n in g['nodes']))


class IspGearIsNotASiteRouter(Base):
    def test_router_on_a_wan_port_is_not_suggested(self):
        self.device('E8:48:B8:00:00:01', 'ether3')                         # TP-Link-looking box on the QCell port
        self.device('E8:48:B8:00:00:02', 'ether2')                         # a real site router on the LAN
        macs = {e['mac'] for e in site_routers.collect(self.biz)}
        self.assertNotIn('E8:48:B8:00:00:01', macs)
        self.assertIn('E8:48:B8:00:00:02', macs)

    def test_wan_ports(self):
        self.assertEqual(site_routers.wan_ports(self.r), {'ether1', 'ether3'})   # bridge1's DHCP client is not a WAN
