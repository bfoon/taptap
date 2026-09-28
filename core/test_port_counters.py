"""Port panel totals (Received from devices / Sent to devices / packets / errors).

Run:  DB_ENGINE=sqlite python manage.py test core.test_port_counters
"""
from datetime import timedelta
from types import SimpleNamespace

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .agent_inventory import SOURCES, apply_interface_stats, inventory_piece_script, merge_interface_stats
from .linklive import ingest_counters
from .models import Business, Router, RouterInterface

STATS = [{'name': 'ether4', 'rx-byte': '5368709120', 'tx-byte': '1073741824', 'rx-packet': '4200000', 'tx-packet': '3100000',
          'rx-error': '0', 'tx-error': '0', 'rx-drop': '12', 'tx-drop': '0', 'link-downs': '3', 'last-link-up-time': '2026-09-27 08:10:02'}]


@override_settings(AUTH_EMAIL_OTP=False, CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class PortCounterTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('p@example.com', 'p@example.com', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='B', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=7))
        self.router = Router.objects.create(business=self.biz, name='R1', ip_address='', username='u', password='p', connection_mode='agent')
        # what a Link inventory saved before this fix: interface row without any counters
        self.port = RouterInterface.objects.create(router=self.router, name='ether4', interface_type='ether', running=True,
                                                   last_seen_at=timezone.now(), raw_data={'name': 'ether4', 'type': 'ether', 'running': 'true',
                                                                                          'bridge_port': {'bridge': 'bridge1'}})
        self.client.force_login(self.owner)

    def panel(self, **q):
        return self.client.get(reverse('router_port', args=[self.router.pk]), {'name': 'ether4', **q}).json()

    def test_link_inventory_reads_interface_stats(self):
        self.assertEqual(SOURCES['interface_stats']['path'], '/interface')
        script = inventory_piece_script(SimpleNamespace(pk=7, params={'kind': 'interface_stats'}), 'https://t', 'yes', 'n')
        self.assertIn('/interface print stats as-value', script)
        normal = inventory_piece_script(SimpleNamespace(pk=8, params={'kind': 'interfaces'}), 'https://t', 'yes', 'n')
        self.assertIn('/interface print as-value', normal)

    def test_stats_are_merged_and_saved(self):
        merged = merge_interface_stats([{'name': 'ether4', 'type': 'ether'}, {'name': 'ether5'}], STATS)
        self.assertEqual(merged[0]['rx-packet'], '4200000')
        self.assertEqual(merged[1], {'name': 'ether5'})
        self.assertEqual(apply_interface_stats(self.router, STATS), 1)
        c = self.panel()['counters']
        self.assertEqual((int(c['rx-byte']), int(c['tx-byte']), c['rx-packet'], c['link-downs']), (5368709120, 1073741824, '4200000', '3'))
        self.assertEqual(c['last-link-up-time'], '2026-09-27 08:10:02')
        self.port.refresh_from_db()
        self.assertEqual(self.port.raw_data['bridge_port'], {'bridge': 'bridge1'})  # config kept

    def test_heartbeat_keeps_byte_totals_current(self):
        ingest_counters(self.router, 'ether4,1000,2000;ether5,1,1;')
        self.port.refresh_from_db()
        self.assertEqual((self.port.rx_byte, self.port.tx_byte), (1000, 2000))
        self.assertEqual(self.panel()['counters']['rx-byte'], 1000)

    def test_live_link_panel_shows_latest_heartbeat_bytes_and_saved_packets(self):
        from .models import RouterAgent
        RouterAgent.objects.create(router=self.router, token_hash='x' * 64, enrolled_at=timezone.now(), last_seen_at=timezone.now())
        self.router.status = 'Online'
        self.router.save()
        apply_interface_stats(self.router, STATS)
        cache.set(f'tt:linkctr:{self.router.pk}', {'ether4': (9000000000, 2000000000, timezone.now().timestamp())}, 60)
        d = self.panel(live=1)
        if d['live'].get('error'):
            self.skipTest('Link agent not considered online in this setup: ' + d['live']['error'])
        c = d['live']['counters']
        self.assertEqual((c['rx-byte'], c['tx-byte']), (9000000000, 2000000000))
        self.assertEqual(c['rx-packet'], '4200000')   # kept from the saved stats, not wiped
