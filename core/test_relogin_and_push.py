import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, Client
from django.utils import timezone

from . import device_lock as dl
from .models import Business, PortalPage, Router, RouterHotspotProfile, RouterSyncJob, Voucher, VoucherPlan
from .utils import voucher_profile


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('owner', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.plan = VoucherPlan.objects.create(business=self.b, name='1 Day', price=25, duration_minutes=1440, mikrotik_profile_name='daily')
        self.api = Router.objects.create(business=self.b, name='API', ip_address='1.1.1.1', username='a', password='b')
        self.link = Router.objects.create(business=self.b, name='Link', ip_address='', username='', password='', connection_mode='agent')
        self.c = Client(); self.c.force_login(self.owner)


class TargetedPushTests(Base):
    def test_batch_sends_only_its_vouchers_no_full_sync(self):
        with mock.patch('core.tasks.push_vouchers_task.delay') as task:
            r = self.c.post('/vouchers/generate/', {'plan': self.plan.pk, 'quantity': 3, 'router': self.api.pk})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(RouterSyncJob.objects.exists())                        # no full router sync
        router_id, ids = task.call_args[0]
        self.assertEqual((router_id, len(ids)), (self.api.pk, 3))

    def test_link_router_uses_voucher_queue(self):
        with mock.patch('core.agent.push_pending_vouchers') as pp:
            self.c.post('/vouchers/generate/', {'plan': self.plan.pk, 'quantity': 2, 'router': self.link.pk})
        self.assertTrue(pp.called); self.assertFalse(RouterSyncJob.objects.exists())

    def test_push_now_one_connection(self):
        v = Voucher.objects.create(business=self.b, router=self.api, code='ABCD2345', plan_name='1 Day', duration_minutes=1440)
        calls = []
        class Svc:
            def __init__(s, r): pass
            def connect(s): calls.append('connect'); return s
            def close(s): pass
            def ensure_hotspot_profile(s, *a): calls.append(('profile', a[0]))
            def upsert_voucher(s, code, profile, **k): calls.append(('user', code, profile)); return True, '*9'
        from .voucher_push import push_now
        with mock.patch('core.mikrotik.MikroTikService', Svc):
            self.assertEqual(push_now(self.api, [v.pk]), (1, 0))
        self.assertIn(('user', 'ABCD2345', 'daily'), calls)
        v.refresh_from_db(); self.assertEqual(v.mikrotik_sync_status, 'Synced')


class ReloginTests(Base):
    def test_router_is_not_locked_to_a_mac(self):
        v = Voucher.objects.create(business=self.b, router=self.link, code='ONE00001', plan_name='1 Day', max_devices=1)
        with mock.patch('core.linkops.send') as send:
            self.assertEqual(dl.claim(v, mac='AA:00:00:00:00:01', fp='fp1').status, 'new')
            self.assertEqual(dl.claim(v, mac='AA:00:00:00:00:02', fp='fp1').status, 'moved')
        self.assertFalse(any(c.args[1] == 'hotspot_user_mac' for c in send.call_args_list))

    def test_old_session_is_cleared_before_login(self):
        v = Voucher.objects.create(business=self.b, router=self.link, code='ONE00002', plan_name='1 Day', max_devices=1)
        dl.claim(v, mac='AA:00:00:00:00:01', fp='fp1')
        cache.set(f'tt:tr:users:{self.link.pk}', {'users': {'ONE00002': [{'mac': 'AA:00:00:00:00:01', 'ip': '10.5.50.9'}]}}, 600)
        dl.claim(v, mac='AA:00:00:00:00:07', fp='fp1')            # same phone, new MAC
        with mock.patch('core.linkops.send') as send:
            wait = dl.release_stale(v, 'AA:00:00:00:00:07')
        self.assertEqual(wait, 15)
        self.assertEqual(send.call_args.args[1:3], ('hotspot_kick', {'user': 'ONE00002', 'mac': 'AA:00:00:00:00:01'}))

    def test_portal_tells_page_to_wait(self):
        from .studio_presets import portal_template
        PortalPage.objects.create(business=self.b, name='L', slug='kl-1', kind='login', config=portal_template('', 'login'), is_published=True)
        v = Voucher.objects.create(business=self.b, router=self.link, code='ONE00003', plan_name='1 Day', max_devices=1)
        dl.claim(v, mac='AA:00:00:00:00:01', fp='fp1')
        cache.set(f'tt:tr:users:{self.link.pk}', {'users': {'ONE00003': [{'mac': 'AA:00:00:00:00:01'}]}}, 600)
        with mock.patch('core.linkops.send'):
            r = self.c.post('/p/kl-1/state/', json.dumps({'code': 'ONE00003', 'mac': 'AA:00:00:00:00:05', 'fp': 'fp1'}), content_type='text/plain')
        self.assertEqual(r.json().get('wait'), 15)

    def test_old_router_locks_removed_once(self):
        with mock.patch('core.linkops.send') as send:
            self.assertTrue(dl.unlock_router(self.link)); self.assertFalse(dl.unlock_router(self.link))
        self.assertEqual(send.call_args.args[1], 'hotspot_mac_unlock_all')
        from types import SimpleNamespace as S
        from .agent import command_body
        self.assertIn('mac-address=00:00:00:00:00:00', command_body(S(kind='hotspot_mac_unlock_all', params={})))


class ProfileChangeTests(Base):
    def test_choose_router_profile_or_plan(self):
        v = Voucher.objects.create(business=self.b, router=self.api, code='PRF00001', plan_name='Ghost', duration_minutes=60)
        RouterHotspotProfile.objects.create(business=self.b, router=self.api, name='mikhmon-1h', shared_users=1)
        page = self.c.get(f'/vouchers/{v.pk}/')
        self.assertContains(page, 'mikhmon-1h')
        with mock.patch('core.tasks.push_vouchers_task.delay') as task:
            self.c.post(f'/vouchers/{v.pk}/profile/', {'profile': 'router:mikhmon-1h'})
            v.refresh_from_db()
            self.assertEqual(voucher_profile(v)[0], 'mikhmon-1h'); self.assertTrue(task.called)
            self.c.post(f'/vouchers/{v.pk}/profile/', {'profile': f'plan:{self.plan.pk}'})
        v.refresh_from_db()
        self.assertEqual((v.plan_name, v.router_profile, voucher_profile(v, self.plan)[0]), ('1 Day', '', 'daily'))
