"""TP-Link & other routers TapTap does not manage: detection, linking, map and the Detail tab.

Run:  DB_ENGINE=sqlite python manage.py test core.test_site_routers
"""
import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import site_routers as sr
from .models import Business, Router, RouterDevice, RouterInterface, SiteRouter
from .models_team import TeamMember
from .net_vendors import brand_of, is_random_mac
from .netgraph import build_graph

HTML = {'HTTP_ACCEPT': 'text/html'}
TPLINK = '14:CC:20'          # TP-Link prefix in the IEEE registry


def mac(prefix, n):
    return f'{prefix}:{n // 65536 % 256:02X}:{n // 256 % 256:02X}:{n % 256:02X}'


@override_settings(AUTH_EMAIL_OTP=False)
class SiteRouterTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('t@example.com', 't@example.com', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='B', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=7))
        self.r = Router.objects.create(business=self.biz, name='hAP', ip_address='10.0.0.1', username='u', password='p', status='Online')
        RouterInterface.objects.create(router=self.r, name='ether3', mac_address='00:0C:42:00:00:03')
        self.client.force_login(self.owner)
        self.n = 0

    def dev(self, m=None, port='ether3', ip='', host='', kind='wired', online=True, dhcp_class=''):
        self.n += 1
        m = m or mac('AA:BB:CC', self.n)
        raw = {'bridge': {'on-interface': port, 'mac-address': m}}
        if dhcp_class:
            raw['dhcp'] = {'class-id': dhcp_class}
        return RouterDevice.objects.create(router=self.r, device_key=m, mac_address=m, ip_address=ip, hostname=host,
                                           interface_name='bridge', connection_type=kind, sources='bridge', is_online=online, raw_data=raw)

    def customers(self, n, port='ether3'):
        for _ in range(n):
            self.dev(mac('00:1A:2B', 1000 + self.n), port=port)

    # ── signatures ──
    def test_vendor_registry(self):
        self.assertEqual(brand_of(f'{TPLINK}:11:22:33'), 'TP-Link')
        self.assertEqual(brand_of('00:0C:42:11:22:33'), 'MikroTik')
        self.assertTrue(is_random_mac('DA:A1:19:00:00:01'))
        self.assertEqual(brand_of('DA:A1:19:00:00:01'), '')

    def test_physical_port_comes_from_the_bridge(self):
        d = self.dev(port='ether5')
        self.assertEqual(d.interface_name, 'bridge')
        self.assertEqual(sr.physical_port(d), 'ether5')

    def test_tplink_with_customers_behind_is_likely(self):
        d = self.dev(mac(TPLINK, 1), host='TL-WR840N', dhcp_class='udhcp 1.19.4')
        pts, why, brand, mode = sr.score(d, peers_on_port=6)
        self.assertGreaterEqual(pts, sr.LIKELY)
        self.assertEqual((brand, mode), ('TP-Link', 'ap'))
        self.assertTrue(any('TP-Link' in t for _, t in why))

    def test_tapo_camera_and_phones_are_not_routers(self):
        cam = self.dev(mac(TPLINK, 2), host='Tapo_C200', kind='wifi')
        self.assertLess(sr.score(cam)[0], sr.POSSIBLE)
        phone = self.dev('DA:A1:19:00:00:05', host='android-5f2c', dhcp_class='android-dhcp-13')
        self.assertLess(sr.score(phone)[0], 0)

    def test_gateway_address_counts(self):
        d = self.dev(mac(TPLINK, 3), ip='192.168.0.1')
        self.assertIn('Uses a router address (192.168.0.1)', [t for _, t in sr.score(d)[1]])

    # ── collect ──
    def test_suggestion_confirm_and_ignore(self):
        tp = self.dev(mac(TPLINK, 10), ip='192.168.0.1', host='Archer_C6')
        self.customers(4)
        e = [x for x in sr.collect(self.biz) if x['mac'] == tp.mac_address][0]
        self.assertEqual((e['status'], e['confidence'], e['port'], e['clients'], e['brand']), ('suggested', 'likely', 'ether3', 4, 'TP-Link'))
        SiteRouter.objects.create(business=self.biz, mac_address=tp.mac_address, status='ignored')
        self.assertFalse([x for x in sr.collect(self.biz) if x['mac'] == tp.mac_address])

    def test_managed_mikrotik_is_never_suggested(self):
        self.dev('00:0C:42:00:00:03', host='router', ip='10.0.0.1')
        self.assertFalse(sr.collect(self.biz))

    def test_ip_only_router_binds_to_the_one_device(self):
        tp = self.dev(mac(TPLINK, 20), ip='192.168.0.1')
        s = SiteRouter.objects.create(business=self.biz, ip_address='192.168.0.1', source='ip')
        e = [x for x in sr.collect(self.biz) if x['id'] == s.pk][0]
        self.assertEqual((e['mac'], e['port'], e['online']), (tp.mac_address, 'ether3', True))
        s.refresh_from_db(); self.assertEqual(s.mac_address, tp.mac_address)

    def test_ip_shared_by_several_tplinks_asks_which(self):
        self.dev(mac(TPLINK, 30), ip='192.168.0.1', port='ether2'); self.dev(mac(TPLINK, 31), ip='192.168.0.1', port='ether4')
        s = SiteRouter.objects.create(business=self.biz, ip_address='192.168.0.1', source='ip')
        e = [x for x in sr.collect(self.biz) if x['id'] == s.pk][0]
        self.assertEqual(len(e['candidates']), 2)
        rows = sr.find_by_ip(self.biz, '192.168.0.1')
        self.assertEqual({r['port'] for r in rows}, {'ether2', 'ether4'})      # each MAC is its own router

    def test_parse_ips(self):
        self.assertEqual(len(sr.parse_ips('192.168.0.1-10')), 10)
        self.assertEqual(sr.parse_ips('192.168.0.1, 192.168.1.1'), {'192.168.0.1', '192.168.1.1'})
        self.assertEqual(len(sr.parse_ips('192.168.0.0/30')), 2)
        self.assertEqual(sr.parse_ips('hello 999.1.1.1'), set())

    # ── map ──
    def test_map_places_tplink_under_its_port_with_customers_below(self):
        tp = self.dev(mac(TPLINK, 40), host='TL-WR840N', ip='192.168.0.1')
        self.customers(3)
        g = build_graph(self.biz)
        node = [n for n in g['nodes'] if n['type'] == 'siterouter'][0]
        self.assertEqual((node['mac'], node['port'], node['suggested']), (tp.mac_address, 'ether3', True))
        edge = [e for e in g['edges'] if e['target'] == node['id']][0]
        self.assertEqual((edge['source'], edge['iface'], edge['router_id']), (f'router:{self.r.id}', 'ether3', self.r.id))   # animates with ether3
        clients = [e for e in g['edges'] if e['source'] == node['id'] and e['target'].startswith('clients:')]
        self.assertEqual(len(clients), 1)
        self.assertEqual([n for n in g['nodes'] if n['id'] == clients[0]['target']][0]['count'], 3)
        self.assertIn('ether3', g['live_interfaces'][str(self.r.id)])
        self.assertEqual(g['stats']['site_routers'], 1)

    def test_two_routers_on_one_port_share_a_cable_unless_chained(self):
        a = self.dev(mac(TPLINK, 50), host='Archer-A'); b = self.dev(mac(TPLINK, 51), host='Archer-B')
        sa = SiteRouter.objects.create(business=self.biz, mac_address=a.mac_address)
        sb = SiteRouter.objects.create(business=self.biz, mac_address=b.mac_address)
        g = build_graph(self.biz)
        self.assertTrue(any(n['id'] == f'shared:{self.r.id}:ether3' for n in g['nodes']))
        sb.parent = sa; sb.save()
        g = build_graph(self.biz)
        self.assertFalse(any(n['id'].startswith('shared:') for n in g['nodes']))
        self.assertTrue(any(e['source'] == f'site:sr:{sa.pk}' and e['target'] == f'site:sr:{sb.pk}' for e in g['edges']))

    # ── Detail tab ──
    def post(self, **data):
        return self.client.post(reverse('topology_router_action'), json.dumps(data), content_type='application/json')

    def test_page_has_map_and_detail(self):
        r = self.client.get(reverse('topology'), **HTML)
        self.assertContains(r, 'id="tabDetail"'); self.assertContains(r, 'topology-detail')

    def test_actions(self):
        tp = self.dev(mac(TPLINK, 60), host='TL-WR840N', ip='192.168.0.1')
        other = self.dev(mac(TPLINK, 61), host='Archer', ip='192.168.0.1', port='ether4')
        r = self.post(action='confirm', mac=tp.mac_address, name='Garage AP', model='TL-WR840N', mode='ap')
        self.assertTrue(r.json()['success'])
        s = SiteRouter.objects.get(mac_address=tp.mac_address)
        self.assertEqual((s.name, s.brand, s.status, s.mode), ('Garage AP', 'TP-Link', 'confirmed', 'ap'))
        r = self.post(action='add', items=[{'mac': other.mac_address, 'ip': '192.168.0.1'}])
        s2 = SiteRouter.objects.get(mac_address=other.mac_address)
        self.assertEqual(s2.source, 'ip')
        self.assertTrue(self.post(action='update', id=s2.pk, parent_id=s.pk).json()['success'])
        bad = self.post(action='update', id=s.pk, parent_id=s2.pk).json()     # loop
        self.assertFalse(bad['success']); self.assertIn('itself', bad['message'])
        self.assertFalse(self.post(action='update', id=s.pk, port='bad port;').json()['success'])
        self.assertTrue(self.post(action='ignore', mac=mac(TPLINK, 99)).json()['success'])
        self.assertTrue(self.post(action='remove', id=s2.pk).json()['success'])
        self.assertFalse(SiteRouter.objects.filter(pk=s2.pk).exists())
        self.assertFalse(self.post(action='add', items=[]).json()['success'])

    def test_add_unseen_ip_and_find(self):
        self.assertTrue(self.post(action='add', items=[{'ip': '192.168.0.1'}], router_id=self.r.id).json()['success'])
        s = SiteRouter.objects.get(ip_address='192.168.0.1')
        self.assertEqual((s.mac_address, s.router_id), ('', self.r.id))
        r = self.client.get(reverse('topology_router_find') + '?ip=192.168.0.1-5')
        self.assertTrue(r.json()['success'])
        self.assertEqual(self.client.get(reverse('topology_router_find') + '?ip=nope').status_code, 400)

    def test_only_network_managers(self):
        u = User.objects.create_user('vc@x.com', 'vc@x.com', 'pw')
        TeamMember.objects.create(business=self.biz, user=u, role='voucher_creator')
        self.client.force_login(u)
        self.assertNotEqual(self.post(action='ignore', mac=mac(TPLINK, 5)).status_code, 200)
        self.assertFalse(SiteRouter.objects.exists())
