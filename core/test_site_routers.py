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
class SiteBase(TestCase):
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


@override_settings(AUTH_EMAIL_OTP=False)
class SiteRouterTests(SiteBase):
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
        self.assertContains(r, 'id="tdBig"')        # Open big (full screen) with zoom controls
        self.assertContains(r, 'data-zoom="fit"')

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

    # ── LAN-to-LAN TP-Links ──
    def test_idle_tplink_is_not_guessed_as_nat(self):
        d = self.dev(mac(TPLINK, 200), host='TL-WR840N')
        self.assertEqual(sr.score(d, peers_on_port=0)[3], '')      # idle, not "NAT router"

    def test_tplink_still_running_dhcp_is_flagged(self):
        from .models import RouterConfigSnapshot
        RouterConfigSnapshot.objects.create(router=self.r, sections={'IP addresses': {'rows': [{'address': '10.5.50.1/24'}]}})
        self.dev(mac(TPLINK, 210), ip='192.168.0.1', host='TL-WR840N', port='ether3')
        for i in range(3):                                           # phones that got 192.168.0.x from the TP-Link
            self.dev(mac('00:1A:2B', 500 + i), ip=f'192.168.0.{100 + i}', port='ether3')
        self.dev(mac(TPLINK, 220), ip='10.5.50.200', host='Archer', port='ether2')
        for i in range(3):                                           # phones that got hotspot addresses: fine
            self.dev(mac('00:1A:2B', 600 + i), ip=f'10.5.50.{20 + i}', port='ether2')
        self.dev(mac('00:1A:2B', 700), ip='169.254.3.4', port='ether2')   # link-local: ignored
        by_port = {e['port']: e for e in sr.collect(self.biz)}
        self.assertEqual(by_port['ether3']['foreign_dhcp'], 3)
        self.assertEqual(by_port['ether3']['foreign_sample'], ['192.168.0.100', '192.168.0.101', '192.168.0.102'])
        self.assertEqual(by_port['ether2']['foreign_dhcp'], 0)

    def test_one_odd_device_is_not_enough(self):
        from .models import RouterConfigSnapshot
        RouterConfigSnapshot.objects.create(router=self.r, sections={'IP addresses': {'rows': [{'address': '10.5.50.1/24'}]}})
        self.dev(mac(TPLINK, 230), ip='10.5.50.9', host='TL-WR840N')
        self.dev(mac('00:1A:2B', 800), ip='192.168.8.20')
        self.assertEqual(sr.collect(self.biz)[0]['foreign_dhcp'], 0)


class FakeProbeSvc:
    """Answers the two calls the probe makes: /ping and /tool/fetch."""
    def __init__(self, page='', ping=3, fail_fetch=None):
        self.page, self.ping, self.fail_fetch, self.calls = page, ping, fail_fetch, []

    def connect(self): return self
    def close(self): pass

    def resource(self, path):
        svc = self

        class R:
            def call(self, cmd, args):
                svc.calls.append((path, cmd, dict(args)))
                if cmd == 'ping':
                    return [{'seq': '0'}, {'sent': '3', 'received': str(svc.ping), 'avg-rtt': '2ms450us'}]
                if svc.fail_fetch:
                    raise Exception(svc.fail_fetch)
                return [{'status': 'connecting'}, {'status': 'finished', 'data': svc.page}]
        return R()


@override_settings(AUTH_EMAIL_OTP=False)
class ProbeTests(SiteBase):
    PAGE = '<html><head><title>TL-WR840N</title></head><body>TP-Link Corporation Limited. tplinkwifi.net</body></html>'

    def setUp(self):
        super().setUp()
        from .models import RouterConfigSnapshot
        RouterConfigSnapshot.objects.create(router=self.r, sections={'IP addresses': {'rows': [{'address': '192.168.0.254/24'}]}})

    def run_probe(self, key, svc):
        from unittest import mock
        from . import router_probe
        with mock.patch('core.mikrotik.MikroTikService', return_value=svc), \
                mock.patch('core.voucher_history.channel', return_value='Direct API'):
            return router_probe.probe(self.biz, key)

    def test_read_page(self):
        from .router_probe import read_page
        self.assertEqual(read_page(self.PAGE), ('TL-WR840N', 'TP-Link', 'TL-WR840N'))
        self.assertEqual(read_page('<title>Opening...</title> Archer_C6 v3 tplinkwifi')[1:], ('TP-Link', 'Archer C6'))
        self.assertEqual(read_page('<title>Login</title>'), ('Login', '', ''))

    def test_probe_saves_and_fills_model(self):
        tp = self.dev(mac(TPLINK, 70), ip='192.168.0.1')
        s = SiteRouter.objects.create(business=self.biz, mac_address=tp.mac_address)
        svc = FakeProbeSvc(self.PAGE)
        res = self.run_probe(f'sr:{s.pk}', svc)
        self.assertEqual((res['reachable'], res['rtt_ms'], res['maker'], res['model'], res['web']), (True, 2.5, 'TP-Link', 'TL-WR840N', 'ok'))
        self.assertIn(('/tool', 'fetch', {'url': 'http://192.168.0.1/', 'output': 'user', 'duration': '6s', 'idle-timeout': '4s'}), svc.calls)
        s.refresh_from_db()
        self.assertEqual((s.model, s.brand), ('TL-WR840N', 'TP-Link'))
        self.assertTrue(s.probed_at and s.probe['model'] == 'TL-WR840N')

    def test_probe_explains_problems(self):
        tp = self.dev(mac(TPLINK, 80), ip='10.9.9.1')                      # not in the MikroTik's networks
        res = self.run_probe(f'auto:{tp.mac_address}', FakeProbeSvc(ping=0, fail_fetch='connection timeout'))
        self.assertFalse(res['reachable'])
        text = ' '.join(res['notes'])
        self.assertIn('has no address in 10.9.9.1', text); self.assertIn('did not answer', text)

    def test_ip_conflict_and_same_box(self):
        a = self.dev(mac(TPLINK, 0x10), ip='192.168.0.1', host='TL-WR840N')
        self.dev(mac(TPLINK, 0x11))                                          # its Wi-Fi MAC on the same port
        self.dev(mac(TPLINK, 0x90), ip='192.168.0.1', port='ether4')        # another TP-Link, same IP
        e = [x for x in sr.collect(self.biz) if x['mac'] == a.mac_address][0]
        self.assertEqual((e['siblings'], e['ip_conflict']), ([mac(TPLINK, 0x11)], 1))
        self.assertFalse([x for x in sr.collect(self.biz) if x['mac'] == mac(TPLINK, 0x11)])   # not a second router
        res = self.run_probe(e['key'], FakeProbeSvc(self.PAGE))
        self.assertEqual(res['same_unit'], [mac(TPLINK, 0x11)])
        self.assertEqual([c['mac'] for c in res['ip_conflict']], [mac(TPLINK, 0x90)])
        self.assertIn('IP conflict', ' '.join(res['notes']))

    def test_probe_explains_dhcp_left_on(self):
        from .models import RouterConfigSnapshot
        RouterConfigSnapshot.objects.filter(router=self.r).update(sections={'IP addresses': {'rows': [{'address': '10.5.50.1/24'}]}})
        tp = self.dev(mac(TPLINK, 240), ip='10.5.50.50')
        for i in range(2):
            self.dev(mac('00:1A:2B', 900 + i), ip=f'192.168.0.{110 + i}')
        res = self.run_probe(f'auto:{tp.mac_address}', FakeProbeSvc(self.PAGE))
        self.assertEqual(res['foreign_dhcp'], 2)
        self.assertIn('still runs its own DHCP server', ' '.join(res['notes']))

    def test_link_router_gets_table_checks_only(self):
        from unittest import mock
        from . import router_probe
        tp = self.dev(mac(TPLINK, 95), ip='192.168.0.1')
        with mock.patch('core.voucher_history.channel', return_value='TapTap Link'), \
                mock.patch('core.mikrotik.MikroTikService') as svc:
            res = router_probe.probe(self.biz, f'auto:{tp.mac_address}')
        svc.assert_not_called()
        self.assertFalse(res['live']); self.assertIn('TapTap Link', ' '.join(res['notes']))

    def test_probe_endpoint(self):
        from unittest import mock
        tp = self.dev(mac(TPLINK, 99), ip='192.168.0.1')
        with mock.patch('core.mikrotik.MikroTikService', return_value=FakeProbeSvc(self.PAGE)), \
                mock.patch('core.voucher_history.channel', return_value='Direct API'):
            r = self.client.post(reverse('topology_router_probe'), json.dumps({'key': f'auto:{tp.mac_address}'}), content_type='application/json')
        self.assertEqual(r.json()['result']['model'], 'TL-WR840N')
        bad = self.client.post(reverse('topology_router_probe'), json.dumps({'key': 'x; drop'}), content_type='application/json')
        self.assertEqual(bad.status_code, 400)
