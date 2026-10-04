from datetime import timedelta
from types import SimpleNamespace as S
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, Client
from django.utils import timezone

from . import device_lock as dl
from . import sharing as sh
from .models import Business, DeviceAppUsage, Router, Voucher, VoucherPlan
from .models_events import EventAlert
from .models_sharing import SharingCase, SharingTrust

TPLINK = '00:0A:EB:11:22:33'      # a TP-Link MAC
PHONE = 'AA:BB:CC:00:00:01'


class ScoreTests(TestCase):
    def test_scores(self):
        self.assertEqual(sh.score_client(TPLINK, '10.5.50.9', [63], 0, None)[0], 80)          # TTL 63 + router maker
        s, why = sh.score_client(PHONE, '10.5.50.9', [127], 0, None)
        self.assertEqual(s, 40); self.assertIn('TTL 127', why[0])                                 # one signal: never blocks at 80
        self.assertEqual(sh.score_client(PHONE, '10.5.50.9', [64, 128], 0, None)[0], 30)        # two kinds of systems
        self.assertEqual(sh.score_client(PHONE, '', [], 0, {'os': {'Apple', 'Android'}, 'apps': set(range(20))})[0], 45)
        self.assertEqual(sh.score_client(PHONE, '', [], 0, None, hostname='OpenWrt')[0], 40)


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1', trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=self.b, name='R', ip_address='1.1.1.1', username='a', password='b')
        VoucherPlan.objects.create(business=self.b, name='1 Day', price=25, duration_minutes=1440, max_devices=1)
        self.v = Voucher.objects.create(business=self.b, router=self.r, code='9SHUDE68', plan_name='1 Day', max_devices=1, duration_minutes=1440,
                                        used_at=timezone.now() - timedelta(minutes=30))
        self.p = sh.policy(self.b); self.p.enabled = True; self.p.save()
        cache.set(f'tt:tr:users:{self.r.pk}', {'users': {'9SHUDE68': [{'mac': TPLINK, 'ip': '10.5.50.9'}]}}, 600)
        cache.set(f'tt:share:ttl:{self.r.pk}', {'10.5.50.9': [63]}, 600)


class EvaluateTests(Base):
    def test_monitor_only_tells_you(self):
        sh.evaluate_router(self.r)
        c = SharingCase.objects.get()
        self.assertEqual((c.score, c.status, c.code), (80, 'suspected', '9SHUDE68'))
        self.assertTrue(EventAlert.objects.filter(kind='sharing', level='danger').exists())     # "likely" alert at threshold
        self.v.refresh_from_db(); self.assertIsNone(self.v.frozen_at)                            # nothing done to the customer

    def test_block_disconnects_and_refuses_on_login_page(self):
        self.p.action = 'block'; self.p.save()
        with mock.patch('core.sharing._disconnect') as dis:
            sh.evaluate_router(self.r)
        c = SharingCase.objects.get()
        self.assertEqual(c.status, 'blocked'); self.assertTrue(dis.called)
        out = dl.claim(self.v, mac=TPLINK, fp='x')
        self.assertFalse(out.allowed); self.assertIn('one device only', out.message)
        self.assertTrue(dl.claim(self.v, mac=PHONE, fp='y').allowed)                             # other devices unaffected

    def test_escalate_warns_first_then_blocks(self):
        self.p.action = 'escalate'; self.p.save()
        with mock.patch('core.voucher_freeze._apply_on_routers', return_value=[]), mock.patch('core.sharing._disconnect'):
            sh.evaluate_router(self.r)
            c = SharingCase.objects.get(); self.assertEqual(c.status, 'warned')
            self.v.refresh_from_db(); self.assertEqual(self.v.freeze_kind, 'warning')
            sh.evaluate_router(self.r)
            c.refresh_from_db(); self.assertEqual(c.status, 'blocked')

    def test_family_plans_trusted_devices_and_below_suspect(self):
        Voucher.objects.filter(pk=self.v.pk).update(max_devices=5)
        self.assertEqual(sh.evaluate_router(self.r), 0)                                           # multi-device voucher: left alone
        Voucher.objects.filter(pk=self.v.pk).update(max_devices=1)
        SharingTrust.objects.create(business=self.b, mac=TPLINK)
        self.assertEqual(sh.evaluate_router(self.r), 0)                                           # trusted
        SharingTrust.objects.all().delete()
        cache.set(f'tt:share:ttl:{self.r.pk}', {'10.5.50.9': [64]}, 600)
        cache.set(f'tt:tr:users:{self.r.pk}', {'users': {'9SHUDE68': [{'mac': PHONE, 'ip': '10.5.50.9'}]}}, 600)
        self.assertEqual(sh.evaluate_router(self.r), 0)                                           # normal phone

    def test_os_signal_from_traffic(self):
        h = timezone.now().replace(minute=0, second=0, microsecond=0)
        for app in ('Apple updates & iCloud', 'Google Play & Android', 'Windows Update'):
            DeviceAppUsage.objects.create(business=self.b, router=self.r, hour=h, mac=TPLINK, ip='10.5.50.9', app=app, category='x', domain='d', download=1)
        sh.evaluate_router(self.r)
        self.assertEqual(SharingCase.objects.get().score, 100)


class LinkAndPageTests(Base):
    def test_link_rules_and_lists_upload(self):
        from .agent import command_body
        body = command_body(S(kind='share_rules', params={'enable': True}))
        self.assertIn('ttl="equal:63"', body); self.assertIn('hotspot="auth"', body); self.assertIn('address-list="tt-share-ttl127"', body)
        self.assertNotIn('mangle add', command_body(S(kind='share_rules', params={'enable': False})))
        cache.delete(f'tt:share:ttl:{self.r.pk}')
        sh.ingest_lists(self.r, [{'list': 'tt-share-ttl63', 'address': '10.5.50.9'}, {'list': 'other', 'address': '1.2.3.4'}])
        self.assertEqual(cache.get(f'tt:share:ttl:{self.r.pk}'), {'10.5.50.9': [63]})

    def test_security_page_and_actions(self):
        c = Client(); c.force_login(self.owner)
        self.assertContains(c.get('/security/'), 'Internet sharing protection')
        with mock.patch('core.sharing.apply_all', return_value=[(True, 'R: on')]) as ap:
            c.post('/security/internet-sharing/', {'enabled': 'on', 'action': 'block', 'threshold': '85', 'suspect_at': '40', 'block_minutes': '45', 'notify': 'app_email',
                                          'message': 'One device only, please.', 'single_device_only': 'on', 'apply': '1'})
        self.p.refresh_from_db()
        self.assertEqual((self.p.action, self.p.threshold, self.p.block_minutes, self.p.notify), ('block', 85, 45, 'app_email')); self.assertTrue(ap.called)
        sh.evaluate_router(self.r)                                                                # 80 < 85: listed, not blocked
        case = SharingCase.objects.get(); self.assertEqual(case.status, 'suspected')
        self.assertContains(c.get('/security/'), '9SHUDE68')
        with mock.patch('core.sharing._disconnect'):
            c.post(f'/security/internet-sharing/{case.pk}/', {'action': 'block'})
        case.refresh_from_db(); self.assertEqual(case.status, 'blocked')
        c.post(f'/security/internet-sharing/{case.pk}/', {'action': 'trust'})
        self.assertTrue(SharingTrust.objects.filter(mac=TPLINK).exists())
        self.assertIsNone(sh.blocked_message(self.b, TPLINK))                                     # trust lifts the block
