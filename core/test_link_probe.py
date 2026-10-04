"""Reaching Wi-Fi routers (TP-Link…) behind a MikroTik through TapTap Link and the TapTap Tunnel.

Run:  DB_ENGINE=sqlite python manage.py test core.test_link_probe
"""
import json
from datetime import timedelta
from unittest import mock

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from . import router_probe as rp
from .models import AgentCommand, RouterConfigSnapshot, SiteRouter
from .test_link_install import routeros_balanced
from .test_site_routers import TPLINK, SiteBase, mac

PAGE = b'p=3\nw=ok\n<html><head><title>TL-WR840N</title></head><body>TP-Link Corporation</body></html>'


@override_settings(AUTH_EMAIL_OTP=False, SITE_URL='https://taptap.example')
class LinkProbeTests(SiteBase):
    def setUp(self):
        super().setUp()
        RouterConfigSnapshot.objects.create(router=self.r, sections={'IP addresses': {'rows': [{'address': '192.168.0.254/24'}]}})
        self.tp = self.dev(mac(TPLINK, 40), ip='192.168.0.1', host='TL-WR840N')
        self.saved = SiteRouter.objects.create(business=self.biz, mac_address=self.tp.mac_address)

    def probe_link(self, key):
        with mock.patch('core.voucher_history.channel', return_value='TapTap Link'), mock.patch('core.linkops.ensure_online'):
            return rp.probe(self.biz, key, user=self.owner)

    def upload(self, cmd, body=PAGE, n=None):
        from .agent import nonce
        url = reverse('agent_probe') + f'?c={cmd.pk}&n={n if n is not None else nonce(cmd)}'
        return self.client.post(url, body, content_type='text/plain')

    def test_link_probe_round_trip(self):
        res = self.probe_link(f'sr:{self.saved.pk}')
        self.assertTrue(res['pending'])
        cmd = AgentCommand.objects.get(kind='site_probe')
        self.assertEqual(cmd.params, {'ip': '192.168.0.1', 'key': f'sr:{self.saved.pk}'})
        self.saved.refresh_from_db(); self.assertTrue(self.saved.probe['pending'])              # shows "checking…"
        r = self.upload(cmd)
        self.assertEqual(r.status_code, 200)
        self.saved.refresh_from_db()
        p = self.saved.probe
        self.assertEqual((p['pending'], p['reachable'], p['maker'], p['model'], p['via']), (False, True, 'TP-Link', 'TL-WR840N', 'TapTap Link'))
        self.assertEqual((self.saved.model, self.saved.brand), ('TL-WR840N', 'TP-Link'))         # filled in
        self.assertTrue(any('3 of 3 replies' in n for n in p['notes']))
        self.assertFalse(any('answer appears here' in n for n in p['notes']))

    def test_suggested_router_result_is_kept_and_listed(self):
        key = f'auto:{mac(TPLINK, 90)}'
        self.dev(mac(TPLINK, 90), ip='192.168.0.2', host='Archer', port='ether4')
        self.probe_link(key)
        self.upload(AgentCommand.objects.get(kind='site_probe'), b'p=0\nw=error\n')
        self.assertFalse(rp.load(self.biz, key)['reachable'])
        d = self.client.get(reverse('topology_routers')).json()
        e = [x for x in d['entries'] if x['key'] == key][0]
        self.assertIn('Does not answer ping', ' '.join(e['probe']['notes']))

    def test_the_router_script(self):
        from .agent import wrap
        self.probe_link(f'sr:{self.saved.pk}')
        cmd = AgentCommand.objects.get(kind='site_probe')
        body = wrap(cmd, 'https://taptap.example', 'yes-without-crl')
        self.assertIn('/ping "192.168.0.1" count=3', body)
        self.assertIn('url=("http://" . "192.168.0.1" . "/") output=user as-value', body)
        self.assertIn(f'/api/agent/v1/probe?c={cmd.pk}&n=', body)
        self.assertTrue(routeros_balanced(body))

    def test_only_local_addresses_and_known_keys(self):
        from .agent import queue
        for ip in ('8.8.8.8', '127.0.0.1', '169.254.1.1', 'x'):
            with self.assertRaises(ValueError, msg=ip):
                queue(self.r, 'site_probe', {'ip': ip, 'key': 'sr:1'})
        with self.assertRaises(ValueError):
            queue(self.r, 'site_probe', {'ip': '192.168.0.1', 'key': 'sr:1; /system reset'})

    def test_upload_is_protected(self):
        self.probe_link(f'sr:{self.saved.pk}')
        cmd = AgentCommand.objects.get(kind='site_probe')
        self.assertEqual(self.upload(cmd, n='wrong').status_code, 403)
        self.assertEqual(self.upload(cmd, body=b'x' * 20_000).status_code, 413)
        other = AgentCommand.objects.create(router=self.r, kind='ping', params={}, expires_at=timezone.now() + timedelta(minutes=5))
        self.assertEqual(self.upload(other).status_code, 404)
        AgentCommand.objects.filter(pk=cmd.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        cmd.refresh_from_db()
        self.assertEqual(self.upload(cmd).status_code, 410)

    def test_link_offline_is_explained(self):
        with mock.patch('core.voucher_history.channel', return_value='TapTap Link'), \
                mock.patch('core.linkops.ensure_online', side_effect=ValueError('TapTap Link has not checked in for 20 minutes')):
            res = rp.probe(self.biz, f'sr:{self.saved.pk}')
        self.assertFalse(res.get('pending'))
        self.assertIn('cannot take commands right now', ' '.join(res['notes']))

    def test_tunnel_routers_go_through_the_tunnel(self):
        from .test_site_routers import FakeProbeSvc
        svc = FakeProbeSvc('<title>TL-WR840N</title> TP-Link')
        with mock.patch('core.voucher_history.channel', return_value='TapTap Tunnel'), mock.patch('core.mikrotik.MikroTikService', return_value=svc):
            res = rp.probe(self.biz, f'sr:{self.saved.pk}')
        self.assertEqual((res['via'], res['model']), ('TapTap Tunnel', 'TL-WR840N'))
        self.assertIn('through the TapTap Tunnel', ' '.join(res['notes']))
        self.assertFalse(AgentCommand.objects.filter(kind='site_probe').exists())
