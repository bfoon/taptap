import json
import re
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.utils import timezone

from . import geomap
from .models import Business, Router, RouterDevice


class AutoPickTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('o', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1', trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.k = Router.objects.create(business=self.b, name='TapTap K', ip_address='41.223.10.10', username='a', password='b')
        self.o = Router.objects.create(business=self.b, name='Brusubi', ip_address='41.223.10.20', username='a', password='b')
        self.c = Client(); self.c.force_login(self.owner)

    def test_rules(self):
        items = [{'key': f'mt:{self.k.pk}', 'kind': 'mikrotik', 'name': 'TapTap K', 'site_id': self.k.pk, 'geo': None},
                 {'key': 'sr:5', 'kind': 'router', 'name': 'Bar TP-Link', 'site_id': self.k.pk, 'geo': None, 'online': True},
                 {'key': f'mt:{self.o.pk}', 'kind': 'mikrotik', 'name': 'Brusubi', 'site_id': self.o.pk, 'geo': None}]
        self.assertEqual(geomap.auto_pick(items, self.k)[0], f'mt:{self.k.pk}')          # the MikroTik you are connected through
        items[0]['geo'] = {'lat': 1, 'lng': 1}
        key, why = geomap.auto_pick(items, self.k)
        self.assertEqual(key, 'sr:5'); self.assertIn('next router behind it', why)        # already mapped → next one behind it
        self.assertEqual(geomap.auto_pick(items, None), ('', ''))                          # not on one of your sites

    def test_map_a_router_page_preselects(self):
        html = self.c.get('/topology/field/', REMOTE_ADDR='41.223.10.10').content.decode()
        data = json.loads(re.search(r'<script id="fieldData" type="application/json">(.*?)</script>', html).group(1)) if 'id="fieldData"' in html else None
        if data is None:
            m = re.search(r'json_script|<script id="([^"]+)" type="application/json">(\{"items".*?)</script>', html)
            data = json.loads(m.group(2))
        self.assertEqual(data['chosen'], f'mt:{self.k.pk}'); self.assertTrue(data['auto'])
        self.assertIn('Picked automatically', html)
        data2 = self.c.get(f'/topology/field/?r=mt:{self.o.pk}', REMOTE_ADDR='41.223.10.10').content.decode()
        self.assertIn(f'"chosen": "mt:{self.o.pk}"', data2)                                 # your own choice wins

    def test_other_routers_form_gets_the_hints(self):
        RouterDevice.objects.create(router=self.o, device_key='k', mac_address='B0:95:8E:84:55:53', ip_address='172.16.3.40')
        html = self.c.get('/network/devices/').content.decode()
        hints = json.loads(re.search(r'<script id="ndHints" type="application/json">(.*?)</script>', html).group(1))
        self.assertEqual(hints['mac']['B0:95:8E:84:55:53'], self.o.pk); self.assertEqual(hints['ip']['172.16.3.40'], self.o.pk)


class GatewayTests(TestCase):
    def setUp(self):
        AutoPickTests.setUp(self)
        from .models import RouterAgent, RouterConfigSnapshot
        RouterConfigSnapshot.objects.create(router=self.k, sections={'IP addresses': {'rows': [{'address': '172.16.0.1/16'}, {'address': '192.168.1.10/24'}]}})
        RouterConfigSnapshot.objects.create(router=self.o, sections={'IP addresses': {'rows': [{'address': '10.10.0.1/22'}, {'address': '192.168.1.11/24'}]}})
        # both behind the same ISP modem: same internet address
        Router.objects.filter(pk__in=[self.k.pk, self.o.pk]).update(ip_address='', connection_mode='agent')
        for r in (self.k, self.o):
            RouterAgent.objects.create(router=r, last_ip='179.64.95.8', token_hash=f'h{r.pk}')

    def chosen(self, url):
        html = self.c.get(url, REMOTE_ADDR='179.64.95.8').content.decode()
        m = re.search(r'"chosen": "([^"]*)"', html)
        return (m.group(1) if m else ''), html

    def test_gateway_from_wifi_details_finds_the_mikrotik(self):
        self.assertEqual(geomap.site_from_gateway(self.b, '172.16.0.1'), self.k)       # the MikroTik's own address
        self.assertEqual(geomap.site_from_gateway(self.b, '10.10.1.7'), self.o)        # inside its network
        self.assertIsNone(geomap.site_from_gateway(self.b, '192.168.1.99'))            # both have that network: unsure
        key, html = self.chosen('/topology/field/')
        self.assertEqual(key, ''); self.assertIn('Which MikroTik is your Wi-Fi on?', html)   # two share 179.64.95.8: ask
        key, _ = self.chosen('/topology/field/?gw=172.16.0.1')
        self.assertEqual(key, f'mt:{self.k.pk}')
        key, _ = self.chosen('/topology/field/')
        self.assertEqual(key, f'mt:{self.k.pk}')                                          # remembered on this phone
        _, html = self.chosen('/topology/field/?gw=8.8.8.8')
        self.assertIn('is not an address of any of your MikroTiks', html)
