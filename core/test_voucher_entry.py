import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, Client
from django.utils import timezone

from .models import Business, PortalPage, Router, RouterHotspotUser, Voucher
from .models_voucher_entry import VoucherEntryDevice, VoucherEntryPolicy


class VoucherEntryProtectionTests(TestCase):
    def setUp(self):
        cache.clear()
        owner = User.objects.create_user('o', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=owner, business_name='K', owner_name='A', phone='1', support_phone='+220 300 1111',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        from .studio_presets import portal_template
        PortalPage.objects.create(business=self.b, name='L', slug='kl-1', kind='login', config=portal_template('', 'login'), is_published=True)
        VoucherEntryPolicy.objects.create(business=self.b, enabled=True, max_attempts=5, warning_remaining=2, window_minutes=10, block_minutes=30)
        Voucher.objects.create(business=self.b, code='GOOD1234', plan_name='1 Day', duration_minutes=1440)
        self.c = Client(REMOTE_ADDR='41.223.1.1')          # every customer reaches TapTap from the router's public IP

    def state(self, code, fp='fp-phone-1', mac='AA:BB:CC:00:00:01', ip='10.5.50.20'):
        r = self.c.post('/p/kl-1/state/', json.dumps({'code': code, 'fp': fp, 'mac': mac, 'ip': ip}), content_type='text/plain')
        return r.json()

    def test_count_warn_then_lock_on_the_router_portal(self):
        r1 = self.state('BAD00001')
        self.assertTrue(r1['invalid']); self.assertEqual(r1['attempts_remaining'], 4)
        self.assertNotIn('wait', r1)                                   # answered on the page, never sent to the router
        self.state('BAD00002')
        r3 = self.state('BAD00003')
        self.assertTrue(r3['security_warning']); self.assertEqual(r3['attempts_remaining'], 2)
        self.state('BAD00004')
        r5 = self.state('BAD00005')
        self.assertTrue(r5['security_block']); self.assertGreater(r5['block_remaining_seconds'], 1700)
        self.assertNotIn('wait', r5)                                   # no automatic login after the countdown
        self.assertTrue(self.state('GOOD1234')['security_block'])      # still locked, even with a right code

    def test_block_follows_the_same_phone(self):
        for i in range(5):
            self.state(f'BAD0000{i}')
        # same phone, browser storage cleared (new device ID) but the same MAC: still locked
        self.assertTrue(self.state('BAD00009', fp='fp-new-browser')['security_block'])

    def test_other_customers_behind_the_router_are_never_blocked(self):
        for i in range(5):
            self.state(f'BAD0000{i}')
        r = self.state('BAD00007', fp='fp-other', mac='AA:BB:CC:00:00:99', ip='10.5.50.77')
        self.assertFalse(r.get('security_block'))
        # no device ID, no MAC, no hotspot address: cannot tell devices apart → not counted at all
        for i in range(8):
            r = self.state(f'XXX0000{i}', fp='', mac='', ip='')
        self.assertFalse(r.get('security_block'))
        self.assertFalse(VoucherEntryDevice.objects.filter(device_key__startswith='ip:').exists())

    def test_right_code_resets_and_router_users_are_not_guessing(self):
        self.state('BAD00001'); self.state('BAD00002')
        self.state('GOOD1234')
        self.assertEqual(VoucherEntryDevice.objects.get().attempts, 0)
        r = Router.objects.create(business=self.b, name='R', ip_address='1.1.1.1', username='a', password='b')
        RouterHotspotUser.objects.create(business=self.b, router=r, username='MIKH0001', profile='default', is_present=True)
        res = self.state('MIKH0001')                                   # made on the router, not in TapTap yet
        self.assertFalse(res.get('invalid')); self.assertEqual(VoucherEntryDevice.objects.get().attempts, 0)

    def test_hosted_portal_counts_and_locks_too(self):
        for i in range(4):
            self.c.post('/p/kl-1/check/', json.dumps({'code': f'BAD0000{i}', 'fp': 'fp-h', 'mac': 'AA:BB:CC:00:00:05'}), content_type='application/json')
        d = self.c.post('/p/kl-1/check/', json.dumps({'code': 'BAD00009', 'fp': 'fp-h', 'mac': 'AA:BB:CC:00:00:05'}), content_type='application/json').json()
        self.assertTrue(d['security_block']); self.assertIn('+220 300 1111', d['message'])
