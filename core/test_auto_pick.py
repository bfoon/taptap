import json
import re
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from . import geomap
from .models import Business, Router, RouterDevice


def field_data(html):
    m = re.search(
        r'<script id="field-data" type="application/json">(.*?)</script>',
        html,
        re.S,
    )
    if not m:
        raise AssertionError('field-data JSON was not rendered')
    return json.loads(m.group(1))


class AutoPickTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            'o',
            'o@x.com',
            'pw12345678',
        )
        self.b = Business.objects.create(
            user=self.owner,
            business_name='K',
            owner_name='A',
            phone='1',
            trial_ends_at=timezone.now() + timedelta(days=9),
            is_unlimited=True,
        )
        self.k = Router.objects.create(
            business=self.b,
            name='TapTap K',
            ip_address='41.223.10.10',
            username='a',
            password='b',
        )
        self.o = Router.objects.create(
            business=self.b,
            name='Brusubi',
            ip_address='41.223.10.20',
            username='a',
            password='b',
        )
        self.c = Client()
        self.c.force_login(self.owner)

    def test_rules(self):
        items = [
            {
                'key': f'mt:{self.k.pk}',
                'kind': 'mikrotik',
                'name': 'TapTap K',
                'site_id': self.k.pk,
                'geo': None,
            },
            {
                'key': 'sr:5',
                'kind': 'router',
                'name': 'Bar TP-Link',
                'site_id': self.k.pk,
                'geo': None,
                'online': True,
            },
            {
                'key': f'mt:{self.o.pk}',
                'kind': 'mikrotik',
                'name': 'Brusubi',
                'site_id': self.o.pk,
                'geo': None,
            },
        ]

        self.assertEqual(
            geomap.auto_pick(items, self.k)[0],
            f'mt:{self.k.pk}',
        )

        items[0]['geo'] = {
            'lat': 1,
            'lng': 1,
        }

        key, why = geomap.auto_pick(items, self.k)
        self.assertEqual(key, 'sr:5')
        self.assertIn('next unmapped router', why)
        self.assertEqual(
            geomap.auto_pick(items, None),
            ('', ''),
        )

    def test_map_a_router_page_preselects_site_fallback(self):
        html = self.c.get(
            '/topology/field/',
            REMOTE_ADDR='41.223.10.10',
        ).content.decode()

        data = field_data(html)

        self.assertEqual(
            data['chosen'],
            f'mt:{self.k.pk}',
        )
        self.assertTrue(data['auto'])
        self.assertFalse(data['auto_exact_phone'])
        self.assertIn('Picked automatically', html)

    def test_manual_or_qr_choice_wins(self):
        html = self.c.get(
            f'/topology/field/?r=mt:{self.o.pk}',
            REMOTE_ADDR='41.223.10.10',
        ).content.decode()

        data = field_data(html)

        self.assertEqual(
            data['chosen'],
            f'mt:{self.o.pk}',
        )
        self.assertTrue(data['has_manual_choice'])

    def test_other_routers_form_gets_the_hints(self):
        RouterDevice.objects.create(
            router=self.o,
            device_key='k',
            mac_address='B0:95:8E:84:55:53',
            ip_address='172.16.3.40',
        )

        html = self.c.get('/network/devices/').content.decode()

        hints = json.loads(
            re.search(
                r'<script id="ndHints" type="application/json">(.*?)</script>',
                html,
            ).group(1)
        )

        self.assertEqual(
            hints['mac']['B0:95:8E:84:55:53'],
            self.o.pk,
        )
        self.assertEqual(
            hints['ip']['172.16.3.40'],
            self.o.pk,
        )


class PhonePathTests(AutoPickTests):
    def make_phone(self, ip='172.16.0.55', port='ether2', wifi=False):
        return RouterDevice.objects.create(
            router=self.k,
            device_key='phone',
            mac_address='02:11:22:33:44:55',
            ip_address=ip,
            hostname='android-phone',
            interface_name=port,
            connection_type='wifi' if wifi else 'wired',
            is_online=True,
            raw_data={
                'wifi': {'interface': port}
                if wifi
                else {},
                'bridge': {'on-interface': port},
            },
        )

    def make_tp_link(self, port='ether2'):
        # Hostname (+40) plus another device on the same physical port (+20)
        # makes this a "likely" automatic router suggestion without depending
        # on the vendor/OUI database in the test.
        return RouterDevice.objects.create(
            router=self.k,
            device_key='tp',
            mac_address='10:20:30:40:50:60',
            ip_address='192.168.0.1',
            hostname='TL-WR840N',
            interface_name=port,
            connection_type='wired',
            is_online=True,
            raw_data={
                'bridge': {'on-interface': port},
            },
        )

    def test_phone_behind_one_ap_selects_that_ap(self):
        self.make_phone()
        self.make_tp_link()

        found = geomap.phone_target(
            self.b,
            '172.16.0.55',
            site=self.k,
        )

        self.assertEqual(
            found['key'],
            'auto:10:20:30:40:50:60',
        )
        self.assertIs(found['site'], self.k)
        self.assertTrue(found['auto_add'])
        self.assertEqual(found['port'], 'ether2')
        self.assertIn('TL-WR840N', found['why'])

    def test_page_preselects_phone_ap_and_exposes_auto_candidate(self):
        self.make_phone()
        self.make_tp_link()

        html = self.c.get(
            '/topology/field/?lip=172.16.0.55',
            REMOTE_ADDR='41.223.10.10',
        ).content.decode()

        data = field_data(html)

        self.assertEqual(
            data['chosen'],
            'auto:10:20:30:40:50:60',
        )
        self.assertTrue(data['auto'])
        self.assertTrue(data['auto_exact_phone'])
        self.assertTrue(data['auto_add'])
        self.assertEqual(
            data['phone_ip'],
            '172.16.0.55',
        )

        keys = {x['key'] for x in data['items']}
        self.assertIn(
            'auto:10:20:30:40:50:60',
            keys,
        )

    def test_direct_mikrotik_wifi_selects_managed_mikrotik(self):
        self.make_phone(
            port='wlan1',
            wifi=True,
        )

        found = geomap.phone_target(
            self.b,
            '172.16.0.55',
            site=self.k,
        )

        self.assertEqual(
            found['key'],
            f'mt:{self.k.pk}',
        )
        self.assertFalse(found['auto_add'])
        self.assertIn('directly on', found['why'])

    def test_two_router_candidates_on_same_port_do_not_guess(self):
        self.make_phone()
        self.make_tp_link()

        RouterDevice.objects.create(
            router=self.k,
            device_key='tp2',
            mac_address='10:20:30:40:50:61',
            ip_address='192.168.1.1',
            hostname='Archer_C6',
            interface_name='ether2',
            connection_type='wired',
            is_online=True,
            raw_data={
                'bridge': {'on-interface': 'ether2'},
            },
        )

        found = geomap.phone_target(
            self.b,
            '172.16.0.55',
            site=self.k,
        )

        self.assertEqual(found['key'], '')
        self.assertTrue(found['ambiguous'])

    def test_invalid_or_public_lip_is_ignored(self):
        self.assertEqual(
            geomap.phone_target(
                self.b,
                '8.8.8.8',
                site=self.k,
            )['key'],
            '',
        )
        self.assertEqual(
            geomap.phone_target(
                self.b,
                'not-an-ip',
                site=self.k,
            )['key'],
            '',
        )


class GatewayTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            'gw',
            'gw@x.com',
            'pw12345678',
        )
        self.b = Business.objects.create(
            user=self.owner,
            business_name='K',
            owner_name='A',
            phone='1',
            trial_ends_at=timezone.now() + timedelta(days=9),
            is_unlimited=True,
        )
        self.k = Router.objects.create(
            business=self.b,
            name='TapTap K',
            ip_address='',
            username='a',
            password='b',
            connection_mode='agent',
        )
        self.o = Router.objects.create(
            business=self.b,
            name='Brusubi',
            ip_address='',
            username='a',
            password='b',
            connection_mode='agent',
        )

        from .models import RouterAgent, RouterConfigSnapshot

        RouterConfigSnapshot.objects.create(
            router=self.k,
            sections={
                'IP addresses': {
                    'rows': [
                        {'address': '172.16.0.1/16'},
                        {'address': '192.168.1.10/24'},
                    ]
                }
            },
        )
        RouterConfigSnapshot.objects.create(
            router=self.o,
            sections={
                'IP addresses': {
                    'rows': [
                        {'address': '10.10.0.1/22'},
                        {'address': '192.168.1.11/24'},
                    ]
                }
            },
        )

        for r in (self.k, self.o):
            RouterAgent.objects.create(
                router=r,
                last_ip='179.64.95.8',
                token_hash=f'h{r.pk}',
            )

        self.c = Client()
        self.c.force_login(self.owner)

    def chosen(self, url):
        html = self.c.get(
            url,
            REMOTE_ADDR='179.64.95.8',
        ).content.decode()
        data = field_data(html)
        return data['chosen'], html

    def test_gateway_from_wifi_details_finds_the_mikrotik(self):
        self.assertEqual(
            geomap.site_from_gateway(
                self.b,
                '172.16.0.1',
            ),
            self.k,
        )

        self.assertEqual(
            geomap.site_from_gateway(
                self.b,
                '10.10.1.7',
            ),
            self.o,
        )

        self.assertIsNone(
            geomap.site_from_gateway(
                self.b,
                '192.168.1.99',
            )
        )

        key, html = self.chosen('/topology/field/')
        self.assertEqual(key, '')
        self.assertIn(
            'Which MikroTik is your Wi-Fi on?',
            html,
        )

        key, _ = self.chosen(
            '/topology/field/?gw=172.16.0.1'
        )
        self.assertEqual(
            key,
            f'mt:{self.k.pk}',
        )

        key, _ = self.chosen('/topology/field/')
        self.assertEqual(
            key,
            f'mt:{self.k.pk}',
        )

        _, html = self.chosen(
            '/topology/field/?gw=8.8.8.8'
        )
        self.assertIn(
            'is not an address of any of your MikroTiks',
            html,
        )
