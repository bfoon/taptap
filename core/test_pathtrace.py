"""My connection: find the device, map the Wi-Fi chain to the MikroTik, measure every stop, find the slow link.

Run:  DB_ENGINE=sqlite python manage.py test core.test_pathtrace
"""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import pathtrace as pt
from .models import AgentCommand, Business, Router, RouterDevice, SiteRouter
from .models_nettools import NetTest
from .test_link_install import routeros_balanced
from .test_nettools import FakeSvc, ping_rows


def dev(router, key, mac, ip, host='', port='ether2', wifi=False):
    return RouterDevice.objects.create(router=router, device_key=key, mac_address=mac, ip_address=ip, hostname=host, interface_name='bridge',
                                       connection_type='wifi' if wifi else 'wired', is_online=True,
                                       raw_data={'bridge': {'on-interface': port}, **({'wifi': {'interface': port}} if wifi else {})})


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o', 'o@x.gm', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1', trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=self.b, name='TapTap K', ip_address='10.0.0.1', username='u', password='p', status='Online')
        self.phone = dev(self.r, 'phone', '02:11:22:33:44:55', '172.16.7.66', 'Galaxy-A12')
        self.ap1 = dev(self.r, 'ap1', 'E8:48:B8:00:00:01', '172.16.7.40', 'TL-WA801N')      # TP-Link MACs: router maker
        self.ap2 = dev(self.r, 'ap2', 'E8:48:B8:00:00:09', '172.16.7.41', 'TL-WA850RE')
        self.client.force_login(self.owner)

    def chain(self):
        s1 = SiteRouter.objects.create(business=self.b, router=self.r, port='ether2', mac_address='E8:48:B8:00:00:01', ip_address='172.16.7.40', name='Pole AP', role='ap')
        s2 = SiteRouter.objects.create(business=self.b, router=self.r, mac_address='E8:48:B8:00:00:09', ip_address='172.16.7.41', name='Shop repeater', role='repeater', parent=s1)
        return s1, s2


class CleanTests(TestCase):
    def test_hops_are_checked(self):
        p = pt.clean('hops', {'hops': ['172.16.7.40', '172.16.7.40', '172.16.7.66'], 'port': 'ether2', 'mac': 'e8-48-b8-00-00-01', 'phase': 'load'})
        self.assertEqual(p['hops'], ['172.16.7.40', '172.16.7.66'])
        self.assertEqual((p['mac'], p['phase'], p['rounds']), ('E8:48:B8:00:00:01', 'load', 8))
        for bad in ({'hops': ['google.com']}, {'hops': ['1.1.1.1"; /system reset']}, {'hops': ['10.0.0.%d' % i for i in range(20)]},
                    {'hops': [], 'port': 'ether2"; /x'}, {'hops': [], 'mac': 'zz'}):
            with self.assertRaises(ValueError, msg=bad):
                pt.clean('hops', bad)
        with self.assertRaises(ValueError):
            pt.clean('whoami', {'ips': ['192.168.1.1']})


class AnalyzeTests(TestCase):
    def path(self, guessed=False):
        return {'device': {'kind': 'device', 'name': 'Galaxy', 'ip': '172.16.7.66', 'mac': 'M'}, 'behind_nat': False, 'guessed': guessed, 'port': 'ether2',
                'router': {'name': 'TapTap K'}, 'hops': [{'kind': 'box', 'name': 'Pole AP', 'ip': '10.1.1.1', 'key': 'a'},
                                                          {'kind': 'box', 'name': 'Shop', 'ip': '10.1.1.2', 'key': 'b'},
                                                          {'kind': 'box', 'name': 'Far', 'ip': '10.1.1.3', 'key': 'c'}]}

    def hops(self, values, phase='idle', **extra):
        return {'phase': phase, 'samples': {ip: v for ip, v in values.items()}, 'gateway': '192.168.1.1',
                'gw_samples': [2, 2, 3], 'inet_samples': [30, 31, 30], **extra}

    def test_finds_the_bottleneck_under_load(self):
        idle = self.hops({'10.1.1.1': [2, 2, 3], '10.1.1.2': [4, 5, 4], '10.1.1.3': [7, 6, 7], '172.16.7.66': [12, 15, 11]})
        load = self.hops({'10.1.1.1': [3, 3, 4], '10.1.1.2': [6, 7, 5], '10.1.1.3': [190, 210, 180], '172.16.7.66': [200, 220, 190]}, 'load',
                         inet_samples=[230, 240, 220])
        v = pt.analyze(self.path(), idle, load, {'down': 3.1}, 22.0)
        self.assertEqual(v['level'], 'bad')
        self.assertEqual(v['title'], 'Problem between Shop and Far')
        self.assertIn('bottleneck', v['detail'])
        self.assertEqual([li['state'] for li in v['links']], ['ok', 'ok', 'bad', 'ok'])
        self.assertTrue(any('lost between the router and you' in f['text'] for f in v['findings']))

    def test_packet_loss_from_one_box_on(self):
        idle = self.hops({'10.1.1.1': [2] * 10, '10.1.1.2': [4, None, 5, None, 4, None, 4, 5, None, 4], '10.1.1.3': [6, None, None, 7, 6, None, 7, None, 6, 6],
                          '172.16.7.66': [9, None, 10, None, 9, 9, None, 10, None, 9]})
        v = pt.analyze(self.path(), idle)
        self.assertEqual(v['title'], 'Problem between Pole AP and Shop'); self.assertIn('drops packets', v['detail'])

    def test_silent_box_is_skipped_and_healthy_chain_passes(self):
        idle = self.hops({'10.1.1.1': [2, 2, 2], '10.1.1.2': [None, None, None], '10.1.1.3': [5, 5, 6], '172.16.7.66': [8, 9, 8]})
        v = pt.analyze(self.path(), idle)
        self.assertEqual(v['level'], 'ok')
        self.assertEqual(v['links'][1]['state'], 'unknown'); self.assertTrue(v['stops'][1]['silent'])
        self.assertEqual(v['links'][2]['from'], 'Pole AP')                 # judged against the last box that answered

    def test_order_from_response_times_when_not_placed(self):
        idle = self.hops({'10.1.1.1': [9, 9, 9], '10.1.1.2': [2, 2, 2], '10.1.1.3': [5, 5, 5], '172.16.7.66': [12, 12, 12]})
        v = pt.analyze(self.path(guessed=True), idle)
        self.assertEqual([s['name'] for s in v['stops']], ['Shop', 'Far', 'Pole AP', 'Galaxy'])
        self.assertTrue(v['ordered_by_time'])

    def test_internet_line_is_the_limit(self):
        ok = {'10.1.1.1': [2, 2], '10.1.1.2': [3, 3], '10.1.1.3': [4, 4], '172.16.7.66': [8, 9]}
        load = self.hops({'10.1.1.1': [3, 3], '10.1.1.2': [4, 4], '10.1.1.3': [5, 6], '172.16.7.66': [10, 11]}, 'load', inet_samples=[260, 300, 280])
        v = pt.analyze(self.path(), self.hops(ok), load, {'down': 4.8}, 5.0)
        self.assertEqual(v['title'], 'The Internet line is the limit')

    def test_bad_cable_and_weak_wifi(self):
        ok = {'10.1.1.1': [2, 2], '10.1.1.2': [3, 3], '10.1.1.3': [4, 4], '172.16.7.66': [8, 9]}
        v = pt.analyze(self.path(), self.hops(ok, eth={'rate': '10Mbps', 'full_duplex': 'false', 'status': 'link-ok'}, wifi={'signal': '-84', 'tx': '6Mbps'}))
        self.assertEqual(v['title'], 'Check the cable on ether2')
        texts = ' '.join(f['text'] for f in v['findings'])
        self.assertIn('10Mbps half duplex', texts); self.assertIn('weak (-84 dBm)', texts)


@override_settings(AUTH_EMAIL_OTP=False, SITE_URL='https://taptap.example')
class PathTests(Base):
    def test_saved_chain_is_used_in_order(self):
        self.chain()
        p = pt.build_path(self.r, '172.16.7.66')
        self.assertEqual([h['name'] for h in p['hops']], ['Pole AP', 'Shop repeater'])
        self.assertFalse(p['guessed'])
        self.assertEqual((p['hops'][0]['ip'], p['hops'][0]['mac']), ('172.16.7.40', 'E8:48:B8:00:00:01'))
        self.assertEqual(pt.measured_ips(p), ['172.16.7.40', '172.16.7.41', '172.16.7.66'])

    def test_unplaced_boxes_on_the_port_are_guessed(self):
        p = pt.build_path(self.r, '172.16.7.66')
        self.assertEqual({h['ip'] for h in p['hops']}, {'172.16.7.40', '172.16.7.41'})
        self.assertTrue(p['guessed']); self.assertIn('response times', p['note'])

    def test_direct_wifi_and_nat(self):
        dev(self.r, 'w', '02:AA:00:00:00:01', '172.16.7.90', 'iPhone', port='wlan1', wifi=True)
        p = pt.build_path(self.r, '172.16.7.90')
        self.assertTrue(p['wifi_direct']); self.assertEqual(p['hops'], [])
        s1, _ = self.chain()
        p = pt.build_path(self.r, '172.16.7.40')                         # the "device" TapTap sees is the NAT router itself
        self.assertTrue(p['behind_nat']); self.assertTrue(p['hops'][-1]['nat'])
        self.assertEqual(pt.measured_ips(p), ['172.16.7.41', '172.16.7.40'])       # the NAT box once, last; nothing behind it

    def test_path_endpoint(self):
        self.chain()
        j = self.client.get(reverse('network_tools_path'), {'router': self.r.pk, 'ip': '172.16.7.66'}).json()
        self.assertEqual(j['path']['measure'], ['172.16.7.40', '172.16.7.41', '172.16.7.66'])
        self.assertEqual(self.client.get(reverse('network_tools_path'), {'router': self.r.pk, 'ip': 'x"; y'}).status_code, 400)


@override_settings(AUTH_EMAIL_OTP=False, SITE_URL='https://taptap.example')
class ApiFlowTests(Base):
    def svc(self):
        def ping(a):
            delay = {'172.16.7.40': '3ms', '172.16.7.41': '6ms', '172.16.7.66': '12ms', '192.168.1.1': '2ms', '1.1.1.1': '30ms'}[a['address']]
            return ping_rows([delay] * int(a['count']), a['address'])
        s = FakeSvc({('/', 'ping'): ping, ('/interface/ethernet', 'monitor'): [{'rate': '1Gbps', 'full-duplex': 'true', 'status': 'link-ok'}]},
                    {'/ip/route': [{'dst-address': '0.0.0.0/0', 'gateway': '192.168.1.1', 'active': 'true'}]})
        conns = {'104.21.48.7:443': [{'src-address': '172.16.7.66:51515', 'dst-address': '104.21.48.7:443'}]}
        s.safe_get = lambda path, **f: {
            '/ip/firewall/connection': conns.get(f.get('dst-address'), []),
            '/ip/arp': [{'address': '172.16.7.66', 'mac-address': '02:11:22:33:44:55', 'interface': 'bridge'}],
            '/interface/bridge/host': [{'mac-address': '02:11:22:33:44:55', 'on-interface': 'ether2'}],
            '/ip/dhcp-server/lease': [{'host-name': 'Galaxy-A12'}],
        }.get(path, [])
        return s

    def post(self, data):
        with mock.patch('core.mikrotik.MikroTikService', return_value=self.svc()), \
                mock.patch('core.pathtrace.taptap_ips', return_value=('taptap.example', ['104.21.48.7'])):
            return self.client.post(reverse('network_tools_run'), json.dumps({'router': self.r.pk, **data}), content_type='application/json')

    def test_whole_check(self):
        t = self.post({'kind': 'whoami', 'lip': ''}).json()['test']
        r = t['result']
        self.assertEqual(r['chosen'], '172.16.7.66')
        self.assertEqual(r['candidates'][0], {'ip': '172.16.7.66', 'mac': '02:11:22:33:44:55', 'name': 'Galaxy-A12', 'port': 'ether2', 'conns': 1, 'known': True})
        self.chain()
        hops = ['172.16.7.40', '172.16.7.41', '172.16.7.66']
        idle = self.post({'kind': 'hops', 'hops': hops, 'port': 'ether2', 'phase': 'idle', 'rounds': 3}).json()['test']
        self.assertEqual(idle['result']['samples']['172.16.7.41'], [6, 6, 6])
        self.assertEqual((idle['result']['gateway'], idle['result']['eth']['rate']), ('192.168.1.1', '1Gbps'))
        load = self.post({'kind': 'hops', 'hops': hops, 'port': 'ether2', 'phase': 'load', 'rounds': 3}).json()['test']
        speed = NetTest.objects.create(business=self.b, router=self.r, kind='speed', status='done', result={'ok': True, 'mbps': 20.0})
        body = {'router': self.r.pk, 'ip': '172.16.7.66', 'idle': idle['id'], 'load': load['id'], 'speed': speed.pk, 'device': {'down': 18.5, 'up': 6, 'rtt': 41}}
        prev = self.client.post(reverse('network_tools_verdict'), json.dumps({**body, 'preview': True}), content_type='application/json').json()
        self.assertNotIn('id', prev['test']); self.assertFalse(NetTest.objects.filter(kind='mypath').exists())
        v = self.client.post(reverse('network_tools_verdict'), json.dumps(body), content_type='application/json').json()['test']
        self.assertEqual(v['kind'], 'mypath'); self.assertEqual(v['result']['level'], 'ok')
        self.assertEqual([s['name'] for s in v['result']['stops']], ['Pole AP', 'Shop repeater', 'Galaxy-A12'])
        self.assertIn('close to the router', ' '.join(f['text'] for f in v['result']['findings']))
        self.assertEqual(v['summary'], 'Your connection looks healthy')

    def test_hint_picks_between_several(self):
        r = pt.enrich_whoami(self.r, {'clients': [{'ip': '172.16.7.66', 'conns': 2}, {'ip': '172.16.7.70', 'conns': 1}]}, '172.16.7.70')
        self.assertEqual(r['chosen'], '172.16.7.70')
        r = pt.enrich_whoami(self.r, {'clients': [{'ip': '172.16.7.66', 'conns': 2}, {'ip': '172.16.7.70', 'conns': 1}]}, '')
        self.assertEqual(r['chosen'], ''); self.assertIn('Pick yours', r['note'])
        self.assertIn('not mobile data', pt.enrich_whoami(self.r, {'clients': []})['note'])

    def test_save_chain_then_it_is_used(self):
        j = self.client.get(reverse('network_tools_path'), {'router': self.r.pk, 'ip': '172.16.7.66'}).json()['path']
        keys = sorted([h['key'] for h in j['hops']], reverse=True)               # far box first = wrong order on purpose, then fixed
        keys = [k for k in keys if k.endswith('01')] + [k for k in keys if k.endswith('09')]
        r = self.client.post(reverse('network_tools_save_chain'), json.dumps({'router': self.r.pk, 'keys': keys, 'port': 'ether2'}), content_type='application/json')
        self.assertEqual(r.json()['saved'], 2)
        s1 = SiteRouter.objects.get(mac_address='E8:48:B8:00:00:01'); s2 = SiteRouter.objects.get(mac_address='E8:48:B8:00:00:09')
        self.assertEqual((s1.parent, s1.port, s2.parent, s2.status), (None, 'ether2', s1, 'confirmed'))
        p = pt.build_path(self.r, '172.16.7.66')
        self.assertFalse(p['guessed']); self.assertEqual([h['ip'] for h in p['hops']], ['172.16.7.40', '172.16.7.41'])

    def test_speed_fallback_endpoints(self):
        r = self.client.get(reverse('network_tools_blob'), {'bytes': '300000'})
        self.assertEqual(len(b''.join(r.streaming_content)), 300000)
        self.assertEqual(self.client.post(reverse('network_tools_sink'), b'x' * 5000, content_type='application/octet-stream').json()['bytes'], 5000)
        self.assertEqual(len(b''.join(self.client.get(reverse('network_tools_blob'), {'bytes': '99999999999'}).streaming_content)), 30_000_000)

    def test_devices_search_and_access(self):
        j = self.client.get(reverse('network_tools_devices'), {'router': self.r.pk, 'q': 'galaxy'}).json()
        self.assertEqual([d['ip'] for d in j['devices']], ['172.16.7.66'])
        other = User.objects.create_user('x', 'x@x.gm', 'pw12345678')
        ob = Business.objects.create(user=other, business_name='X', owner_name='X', phone='1', trial_ends_at=timezone.now() + timedelta(days=9))
        orr = Router.objects.create(business=ob, name='theirs', ip_address='10.9.9.9', username='u', password='p')
        self.assertEqual(self.client.get(reverse('network_tools_path'), {'router': orr.pk, 'ip': '1.2.3.4'}).status_code, 404)
        from .models_team import TeamMember
        u = User.objects.create_user('s', 's@x.gm', 'pw12345678')
        TeamMember.objects.create(business=self.b, user=u, role='voucher_creator')
        self.client.force_login(u)
        self.assertNotEqual(self.client.get(reverse('network_tools_path'), {'router': self.r.pk, 'ip': '172.16.7.66'}).status_code, 200)

    def test_page_shows_both_modes(self):
        html = self.client.get(reverse('network_tools')).content.decode()
        self.assertIn('Trace from this device', html); self.assertIn('From the router', html); self.assertIn('mypath.js', html)


@override_settings(AUTH_EMAIL_OTP=False, SITE_URL='https://taptap.example')
class LinkFlowTests(Base):
    def setUp(self):
        super().setUp()
        self.r.connection_mode = 'agent'; self.r.save()

    def run_link(self, data):
        with mock.patch('core.voucher_history.channel', return_value='TapTap Link'), mock.patch('core.linkops.ensure_online'), \
                mock.patch('core.pathtrace.taptap_ips', return_value=('taptap.example', ['104.21.48.7'])):
            return self.client.post(reverse('network_tools_run'), json.dumps({'router': self.r.pk, **data}), content_type='application/json').json()['test']

    def upload(self, cmd, body):
        from .agent import nonce
        return self.client.post(reverse('agent_nettest') + f'?c={cmd.pk}&n={nonce(cmd)}', body, content_type='text/plain')

    def test_whoami_round_trip(self):
        t = self.run_link({'kind': 'whoami', 'lip': '172.16.7.66'})
        cmd = AgentCommand.objects.get(kind='nettest')
        from .agent import wrap
        body = wrap(cmd, 'https://taptap.example', 'no')
        self.assertTrue(routeros_balanced(body))
        self.assertIn(':set ips ($ips , "104.21.48.7")', body); self.assertIn('[:resolve "taptap.example"]', body)
        self.assertIn('/ip firewall connection find where dst-address=($i . ":443")', body)
        self.upload(cmd, b'c=172.16.7.66:51515\nc=172.16.7.66:51516\nc=172.16.7.70:40000\n')
        r = NetTest.objects.get(pk=t['id']).result
        self.assertEqual(r['chosen'], '172.16.7.66')                           # the browser's hint decides between two
        self.assertEqual(r['candidates'][0]['name'], 'Galaxy-A12')

    def test_hops_round_trip(self):
        t = self.run_link({'kind': 'hops', 'hops': ['172.16.7.40', '172.16.7.66'], 'port': 'ether2', 'mac': 'E8:48:B8:00:00:01', 'phase': 'idle', 'rounds': 2})
        cmd = AgentCommand.objects.get(kind='nettest')
        from .agent import wrap
        body = wrap(cmd, 'https://taptap.example', 'no')
        self.assertTrue(routeros_balanced(body))
        self.assertIn(':for r from=1 to=2 do={', body)
        self.assertIn('/interface ethernet monitor [find name="ether2"] once as-value', body)
        self.assertIn('[:parse ":local i [/interface wifi registration-table find mac-address=E8:48:B8:00:00:01]', body)
        self.assertNotRegex(body, r'flood-ping[^\]]*as-value')
        self.upload(cmd, b'gw=192.168.1.1\nh0=1|3\nh1=1|12\nhg=1|2\nhi=1|30\nh0=1|4\nh1=0|\nhg=1|2\nhi=1|31\neth=100Mbps|true|link-ok\nwifi=-71|54Mbps|48Mbps|80|wlan1\n')
        r = NetTest.objects.get(pk=t['id']).result
        self.assertEqual(r['samples'], {'172.16.7.40': [3, 4], '172.16.7.66': [12, None]})
        self.assertEqual((r['gateway'], r['inet_samples'], r['eth']['rate'], r['wifi']['signal']), ('192.168.1.1', [30, 31], '100Mbps', '-71'))

    def test_queue_refuses_tampered_hops(self):
        from .agent import queue
        for p in ({'test_id': 1, 'kind': 'hops', 'hops': ['1.1.1.1"; /system reset'], 'phase': 'idle'},
                  {'test_id': 1, 'kind': 'hops', 'hops': ['1.1.1.1'], 'port': 'x"y', 'phase': 'idle'},
                  {'test_id': 1, 'kind': 'whoami', 'ips': ['10.0.0.1'], 'host': ''}):
            with self.assertRaises(ValueError, msg=p):
                queue(self.r, 'nettest', p)
