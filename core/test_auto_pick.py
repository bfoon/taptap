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
