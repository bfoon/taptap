import io
from datetime import timedelta
from types import SimpleNamespace as S
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, Client
from django.utils import timezone

from . import netdev, omada as om
from .models import Business, Router, RouterDevice
from .models_netdev import NetDevice, OmadaController, RemoteSession


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1', trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.api = Router.objects.create(business=self.b, name='Main', ip_address='41.223.10.10', username='a', password='b')
        self.link = Router.objects.create(business=self.b, name='Brusubi', ip_address='', username='', password='', connection_mode='agent')
        self.d = NetDevice.objects.create(business=self.b, router=self.api, name='Bar TP-Link', brand='TP-Link', ip='192.168.88.20', username='admin')
        self.d.set_password('s3cret!'); self.d.save()
        self.c = Client(); self.c.force_login(self.owner)


class ReachAndRulesTests(Base):
    def test_password_is_encrypted(self):
        self.assertNotIn('s3cret', self.d.password_enc); self.assertEqual(self.d.get_password(), 's3cret!')

    def test_direct_mode_only_for_your_address(self):
        self.assertEqual(netdev.reach(self.api), ('direct', '41.223.10.10'))
        applied = []
        with mock.patch('core.netdev._apply', side_effect=lambda r, rules, add=True, comment='': applied.append((add, rules, comment))):
            s = netdev.open_session(self.d, self.owner, '102.1.2.3')
        dst = applied[0][1][0]
        self.assertEqual((dst['src-address'], dst['dst-address-type'], dst['to-addresses'], dst['to-ports']), ('102.1.2.3', 'local', '192.168.88.20', '80'))
        self.assertEqual(netdev.direct_url(s), f'http://41.223.10.10:{s.port}/')
        with mock.patch('core.netdev._apply', side_effect=lambda r, rules, add=True, comment='': applied.append((add, rules, comment))):
            netdev.close_session(s)
        self.assertEqual(applied[-1], (False, [], s.comment))

    def test_tunnel_router_uses_the_proxy_and_link_commands(self):
        self.d.router = self.link; self.d.save()
        self.assertIsNone(netdev.reach(self.link)[0])                         # no public IP, no tunnel
        with mock.patch('core.tunnel.tunnel_ready', return_value=True), mock.patch('core.tunnel.get_tunnel', return_value=S(tunnel_ip='10.77.0.9')), \
             mock.patch('core.netdev._channel', return_value='TapTap Link'), mock.patch('core.linkops.send', return_value=S(pk=77)) as send:
            s = netdev.open_session(self.d, self.owner, '')
        self.assertEqual((s.mode, s.target_host), ('proxy', '10.77.0.9'))
        kind, params = send.call_args.args[1], send.call_args.args[2]
        self.assertEqual(kind, 'remote_nat'); self.assertEqual(params['rules'][0]['dst-address'], '10.77.0.9')
        from .agent import _validate_remote, command_body
        _validate_remote('remote_nat', params)
        self.assertIn('/ip firewall nat add', command_body(S(kind='remote_nat', params=params)))
        with self.assertRaises(ValueError):
            _validate_remote('remote_nat', {'rules': [{'chain': 'dstnat', 'action': 'accept; /system reset'}]})


class ProxyTests(Base):
    def _session(self, **kw):
        return RemoteSession.objects.create(device=self.d, user=self.owner, token='tok' * 6, mode='proxy', port=41001, target_host='10.77.0.9',
                                            expires_at=timezone.now() + timedelta(minutes=30), **kw)

    def test_page_is_shown_through_taptap_with_paths_fixed(self):
        s = self._session()
        html = b'<html><head></head><body><a href="/userRpm/Index.htm">x</a><img src="/img/logo.png"><form action="/login"></form></body></html>'
        fake = S(status=200, headers=mock.MagicMock(), read=lambda: html)
        fake.headers.get.side_effect = lambda k, d=None: 'text/html' if k == 'Content-Type' else d
        fake.headers.items.return_value = [('Content-Type', 'text/html')]
        fake.headers.get_all.return_value = ['SESSION=abc; Path=/; HttpOnly']
        with mock.patch('urllib.request.OpenerDirector.open', return_value=fake):
            r = self.c.get(f'/remote/{s.token}/index.htm')
        body = r.content.decode()
        self.assertIn(f'href="/remote/{s.token}/userRpm/Index.htm"', body); self.assertIn(f'src="/remote/{s.token}/img/logo.png"', body)
        self.assertIn('XMLHttpRequest.prototype.open', body)
        self.assertEqual(r.cookies['SESSION']['path'], f'/remote/{s.token}/')

    def test_ended_or_someone_elses_session(self):
        s = self._session(closed_at=timezone.now())
        self.assertEqual(self.c.get(f'/remote/{s.token}/').status_code, 410)
        other = User.objects.create_user('x', 'x@x.com', 'pw12345678')
        s2 = RemoteSession.objects.create(device=self.d, user=other, token='abc' * 6, mode='proxy', port=41002, target_host='10.77.0.9', expires_at=timezone.now() + timedelta(minutes=5))
        self.assertEqual(self.c.get(f'/remote/{s2.token}/').status_code, 410)


class PageAndOmadaTests(Base):
    def test_page_suggestions_and_reveal(self):
        RouterDevice.objects.create(router=self.api, device_key='k1', mac_address='00:0A:EB:11:22:33', ip_address='192.168.88.30', hostname='TL-WR840N')
        page = self.c.get('/network/devices/')
        self.assertContains(page, 'Bar TP-Link'); self.assertContains(page, 'TL-WR840N'); self.assertContains(page, 'direct, your IP only')
        d = self.c.post(f'/network/devices/{self.d.pk}/', {'action': 'reveal'}).json()
        self.assertEqual(d, {'username': 'admin', 'password': 's3cret!'})

    def test_omada_connect_list_block_and_blacklist(self):
        calls = []
        def fake_call(c, method, path, body=None, token=None):
            calls.append((method, path))
            if 'authorize/token' in path: return {'accessToken': 'T', 'expiresIn': 7200}
            if path.endswith('/sites?page=1&pageSize=100'): return {'data': [{'siteId': 'S1', 'name': 'Default'}]}
            if '/devices?' in path: return {'data': [{'name': 'Bar EAP', 'mac': 'AA-BB-CC-00-00-01', 'model': 'EAP225', 'status': 14, 'clientNum': 9}]}
            if '/clients?' in path: return {'data': [{'name': 'phone', 'mac': '06-11-22-33-44-55', 'ip': '192.168.88.77', 'apName': 'Bar EAP'}]}
            return {}
        with mock.patch('core.omada._call', side_effect=fake_call):
            self.c.post('/network/omada/', {'base_url': 'https://192.168.88.10:8043', 'omadac_id': 'OID', 'client_id': 'CID', 'client_secret': 'SEC'})
            c = OmadaController.objects.get(); self.assertEqual((c.site_id, c.site_name), ('S1', 'Default')); self.assertNotIn('SEC', c.client_secret_enc)
            page = self.c.get('/network/devices/')
            self.assertContains(page, 'Bar EAP'); self.assertContains(page, '192.168.88.77')
            self.c.post('/network/omada/do/', {'action': 'block', 'mac': '06:11:22:33:44:55'})
            self.assertIn(('POST', '/openapi/v1/OID/sites/S1/clients/06-11-22-33-44-55/block'), calls)
            self.assertEqual(om.blacklist_hook(self.b, ['AA:BB:CC:DD:EE:FF']), 1)


class BehindNatAndStatusTests(Base):
    def test_router_behind_isp_nat_is_not_offered_a_direct_path(self):
        from .models import RouterConfigSnapshot
        RouterConfigSnapshot.objects.create(router=self.api, sections={'IP addresses': {'rows': [{'address': '192.168.1.2/24'}, {'address': '192.168.88.1/24'}]}})
        mode, why = netdev.reach(self.api)
        self.assertIsNone(mode); self.assertIn('is not on the MikroTik', why); self.assertIn('TapTap Tunnel', why)
        RouterConfigSnapshot.objects.filter(router=self.api).update(sections={'IP addresses': {'rows': [{'address': '41.223.10.10/30'}]}})
        self.assertEqual(netdev.reach(self.api)[0], 'direct')

    def test_waiting_ready_failed_and_moving_to_the_browser_address(self):
        from .models import AgentCommand
        self.d.router = self.link; self.d.save()
        s = RemoteSession.objects.create(device=self.d, user=self.owner, token='zz' * 9, mode='direct', port=41005, target_host='179.64.95.8',
                                         client_ip='2a02::1', expires_at=timezone.now() + timedelta(minutes=30))
        cmd = AgentCommand.objects.create(router=self.link, kind='remote_nat', params={}, status='queued', expires_at=timezone.now() + timedelta(minutes=10))
        s.command_id = cmd.pk; s.save()
        self.assertEqual(netdev.state(s)[0], 'waiting')
        AgentCommand.objects.filter(pk=cmd.pk).update(status='done'); self.assertEqual(netdev.state(s)[0], 'ready')
        AgentCommand.objects.filter(pk=cmd.pk).update(status='failed', result='bad'); self.assertEqual(netdev.state(s)[0], 'failed')
        applied = []
        with mock.patch('core.netdev._apply', side_effect=lambda r, rules, add=True, comment='': applied.append((add, rules)) or None):
            r = self.c.post(f'/network/remote/{s.token}/', {'action': 'ip', 'ip': '102.1.2.3'}).json()
        self.assertTrue(r['moved']); self.assertEqual(r['client_ip'], '102.1.2.3')
        self.assertEqual(applied[0], (False, [])); self.assertEqual(applied[1][1][0]['src-address'], '102.1.2.3')
        self.assertEqual(self.c.get(f'/network/remote/{s.token}/?status=1').json()['client_ip'], '102.1.2.3')


class CardHintsTests(Base):
    def test_address_problems_and_one_banner_per_router(self):
        from .models import RouterConfigSnapshot
        RouterConfigSnapshot.objects.create(router=self.api, sections={'IP addresses': {'rows': [{'address': '172.16.0.1/16'}, {'address': '192.168.1.2/24'}]}})
        a = NetDevice.objects.create(business=self.b, router=self.api, name='TP A', ip='192.168.0.1')
        NetDevice.objects.create(business=self.b, router=self.api, name='TP B', ip='192.168.0.1')
        g = NetDevice.objects.create(business=self.b, router=self.api, name='Grandstream', ip='172.16.3.112')
        items = list(NetDevice.objects.filter(business=self.b).select_related('router'))
        self.assertIn('also uses 192.168.0.1', netdev.device_problem(a, items))
        self.assertIsNone(netdev.device_problem(g, items))
        self.assertIn('not in any network', netdev.device_problem(self.d, items))        # 192.168.88.20 is not on this MikroTik
        page = self.c.get('/network/devices/').content.decode()
        self.assertEqual(page.count('admin pages can’t be opened yet'), 1)                # once per router, not per card
        self.assertIn('TAPTAP_TUNNEL_ENABLED', page)
