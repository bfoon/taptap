import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, Client
from django.utils import timezone

from . import device_lock as dl, sticky
from .models import Business, PortalPage, Router, Voucher, VoucherPlan
from .models_team import TeamMember


class DeviceLockTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('owner', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='Kairaba Net', owner_name='A', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.one = Voucher.objects.create(business=self.b, code='ONEDEV01', plan_name='x', max_devices=1)
        self.three = Voucher.objects.create(business=self.b, code='THREE001', plan_name='x', max_devices=3)

    def test_single_device_locks_and_follows_mac_change(self):
        self.assertEqual(dl.claim(self.one, mac='aa-bb-cc-dd-ee-01', fp='fpA').status, 'new')
        self.assertEqual(dl.claim(self.one, mac='AA:BB:CC:DD:EE:01').status, 'known')          # same MAC
        out = dl.claim(self.one, mac='AA:BB:CC:DD:EE:99', fp='fpA')                              # phone changed MAC
        self.assertEqual(out.status, 'moved')
        self.assertEqual(out.binding.previous_mac, 'AA:BB:CC:DD:EE:01')
        self.assertFalse(dl.claim(self.one, mac='11:22:33:44:55:66', fp='fpB').allowed)          # another phone
        self.assertEqual(self.one.device_bindings.count(), 1)

    def test_shared_voucher_counts_down_then_locks(self):
        for i in range(3):
            self.assertEqual(dl.claim(self.three, mac=f'AA:BB:CC:00:00:0{i}').status, 'new')
            self.assertEqual(dl.summary(self.three)['free'], 2 - i)
        self.assertFalse(dl.claim(self.three, mac='AA:BB:CC:00:00:09').allowed)
        self.assertTrue(dl.claim(self.three, mac='AA:BB:CC:00:00:01').allowed)                  # locked device comes back

    def test_only_reset_frees_slots(self):
        from . import voucher_history as vh
        dl.claim(self.one, mac='AA:BB:CC:DD:EE:01')
        with mock.patch('core.voucher_history._router_apply', return_value=('TapTap', 'ok', True)):
            vh.reset_devices(self.one, self.owner, 'new phone')
        self.assertEqual(dl.claim(self.one, mac='11:22:33:44:55:66').status, 'new')
        # a voucher creator (no voucher-support right) cannot reset
        u = User.objects.create_user('sales', 's@x.com', 'pw12345678')
        TeamMember.objects.create(business=self.b, user=u, role='voucher_creator')
        c = Client(); c.force_login(u)
        r = c.post(f'/vouchers/{self.one.pk}/reset-mac/', {'reason': 'x'})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.one.device_bindings.count(), 1)
        s = User.objects.create_user('support', 'v@x.com', 'pw12345678')
        TeamMember.objects.create(business=self.b, user=s, role='voucher_support')
        c2 = Client(); c2.force_login(s)
        with mock.patch('core.voucher_history._router_apply', return_value=('TapTap', 'ok', True)):
            c2.post(f'/vouchers/{self.one.pk}/reset-mac/', {'reason': 'customer changed phone'})
        self.assertEqual(self.one.device_bindings.count(), 0)

    def test_portal_refuses_other_devices(self):
        from .studio_presets import portal_template
        PortalPage.objects.create(business=self.b, name='L', slug='kl-1', kind='login', config=portal_template('', 'login'), is_published=True)
        c = Client()
        r = c.post('/p/kl-1/check/', json.dumps({'code': 'ONEDEV01', 'mac': 'AA:BB:CC:DD:EE:01'}), content_type='application/json')
        self.assertEqual(r.status_code, 200)
        cache.clear()
        r = c.post('/p/kl-1/check/', json.dumps({'code': 'ONEDEV01', 'mac': 'AA:BB:CC:DD:EE:02'}), content_type='application/json')
        self.assertEqual(r.status_code, 403); self.assertEqual(r.json()['kind'], 'locked')
        r = c.post('/p/kl-1/state/', json.dumps({'code': 'ONEDEV01', 'mac': 'AA:BB:CC:DD:EE:02'}), content_type='text/plain')
        self.assertEqual(r.json().get('kind'), 'locked')
        self.b.device_lock = False; self.b.save()
        cache.clear()
        r = c.post('/p/kl-1/check/', json.dumps({'code': 'ONEDEV01', 'mac': 'AA:BB:CC:DD:EE:02'}), content_type='application/json')
        self.assertEqual(r.status_code, 200)

    def test_live_sync_kicks_foreign_device(self):
        r = Router.objects.create(business=self.b, name='R1', ip_address='1.1.1.1', username='a', password='b')
        dl.claim(self.one, mac='AA:BB:CC:DD:EE:01')
        removed = []
        class Res:
            def get(s, **k): return [{'id': '*7', 'user': 'ONEDEV01', 'mac-address': 'AA:BB:CC:DD:EE:02'}]
            def remove(s, id): removed.append(id)
        class Svc:
            def resource(s, p): return Res()
        # Both devices in use at the same time (data moving): kicked on the second reading in a row, not the first.
        def rows(b):
            return [(self.one, {'id': '*7', 'mac-address': 'AA:BB:CC:DD:EE:02', 'bytes-in': b, 'bytes-out': 0}),
                    (self.one, {'id': '*8', 'mac-address': 'AA:BB:CC:DD:EE:01', 'bytes-in': b, 'bytes-out': 0})]
        self.assertEqual(dl.enforce_sessions(r, rows(10_000), svc=Svc()), 0)
        n = dl.enforce_sessions(r, rows(500_000), svc=Svc())
        self.assertEqual(n, 1); self.assertIn('*7', removed)

    def test_sticky_router_settings(self):
        self.assertEqual(sticky.profile_values(self.b)['add-mac-cookie'], 'yes')
        self.assertEqual(sticky.profile_values(self.b)['idle-timeout'], 'none')
        self.assertIn('mac-cookie', sticky.script(self.b))
        self.b.sticky_sessions = False
        self.assertEqual(sticky.profile_values(self.b)['add-mac-cookie'], 'no')
