"""Regression checks for two independent TapTap MikroTik sites."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from core import agent
from core.agent_inventory import _users
from core.models import Business, Router, RouterAgent, RouterHotspotUser, TrafficSample, Voucher
from core.traffic_report import _samples


class TwoSiteIsolationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('site-owner', password='dummy')
        self.business = Business.objects.create(
            user=self.user, business_name='Two Sites', owner_name='Owner', phone='1',
            trial_ends_at=timezone.now() + timedelta(days=30),
        )
        self.first = Router.objects.create(
            business=self.business, name='Site A', ip_address='', connection_mode='agent',
        )
        self.second = Router.objects.create(
            business=self.business, name='Site B', ip_address='', connection_mode='agent',
        )
        RouterAgent.objects.create(
            router=self.first, token_hash='a' * 64, last_seen_at=timezone.now(),
            enrolled_at=timezone.now(), script_version=9, ros_version='7.15',
        )
        RouterAgent.objects.create(
            router=self.second, token_hash='b' * 64, last_seen_at=timezone.now(),
            enrolled_at=timezone.now(), script_version=9, ros_version='7.15',
        )
        self.voucher = Voucher.objects.create(
            business=self.business, router=self.first, code='SITEA001',
            plan_name='Day', source='mikrotik', mikrotik_sync_status='Pending',
        )

    def test_import_on_other_site_only_updates_its_mirror(self):
        summary = _users(self.second, [{'name': 'SITEA001', 'profile': 'default'}], timezone.now())
        self.voucher.refresh_from_db()
        self.assertEqual(self.voucher.router_id, self.first.pk)
        self.assertEqual(self.voucher.mikrotik_sync_status, 'Pending')
        self.assertTrue(RouterHotspotUser.objects.filter(
            router=self.second, username='SITEA001', is_present=True).exists())
        self.assertTrue(any('assigned to router' in x for x in summary['errors']))

    def test_old_router_ack_does_not_mark_reassigned_voucher_synced(self):
        cmd = agent.queue(self.first, 'hotspot_users', {
            'users': [{'n': self.voucher.code, 'prof': 'Day', 'lim': '1d', 'dis': False}],
            'profiles': [], 'ids': [self.voucher.pk],
        })
        Voucher.objects.filter(pk=self.voucher.pk).update(router=self.second)
        agent.handle_ack(cmd.pk, agent.nonce(cmd), 'ok')
        self.voucher.refresh_from_db()
        self.assertEqual(self.voucher.mikrotik_sync_status, 'Pending')

    def test_second_site_session_cannot_activate_first_site_voucher(self):
        agent.ingest_sessions(self.second, [{
            'user': 'SITEA001', 'mac-address': '02:00:00:00:00:02',
            'address': '192.168.80.10', 'uptime': '2m',
            'bytes-in': '1024', 'bytes-out': '2048', 'id': '*1',
        }], timezone.now())
        self.voucher.refresh_from_db()
        self.assertIsNone(self.voucher.used_at)

    def test_zero_wan_counters_do_not_hide_hotspot_usage(self):
        when = timezone.now()
        TrafficSample.objects.create(router=self.second, interface='ether1', bucket=when,
                                     rx_bytes=0, tx_bytes=0)
        TrafficSample.objects.create(router=self.second, interface='*users', bucket=when,
                                     rx_bytes=2048, tx_bytes=1024)
        rows, wan_routers = _samples(self.business, when - timedelta(hours=1),
                                    when + timedelta(hours=1), self.second.pk)
        self.assertNotIn(self.second.pk, wan_routers)
        self.assertEqual(list(rows.values_list('interface', flat=True)), ['*users'])
