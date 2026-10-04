from datetime import timedelta
from types import SimpleNamespace as S
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, Client
from django.utils import timezone

from . import blacklist as bl
from . import device_lock as dl
from .models import Business, DeviceSignature, Router, Voucher, VoucherEvent
from .models_events import EventAlert

OLD, NEW = 'AA:BB:CC:00:00:01', '06:11:22:33:44:55'


class BlacklistTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1', support_phone='+220 300 1111',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r1 = Router.objects.create(business=self.b, name='Main', ip_address='1.1.1.1', username='a', password='b')
        self.r2 = Router.objects.create(business=self.b, name='Brusubi', ip_address='1.1.1.2', username='a', password='b')
        self.sig = DeviceSignature.objects.create(business=self.b, fingerprint='fp-bad', macs=[OLD], last_mac=OLD, model='SM-A125F')
        self.v = Voucher.objects.create(business=self.b, router=self.r1, code='GOOD1234', plan_name='1 Day', duration_minutes=1440)
        self.c = Client(); self.c.force_login(self.owner)

    def test_blacklist_blocks_on_every_router(self):
        with mock.patch('core.device_block.block', return_value='Blocked on the router') as blk:
            self.c.post(f'/devices/{self.sig.pk}/action/', {'action': 'blacklist', 'reason': 'resold vouchers'})
        self.sig.refresh_from_db()
        self.assertIsNotNone(self.sig.blacklisted_at); self.assertEqual(self.sig.blacklist_reason, 'resold vouchers')
        args, kw = blk.call_args
        self.assertIsNone(args[0]); self.assertEqual(args[1], [OLD]); self.assertEqual(kw['business'], self.b)
        from .device_block import routers_for
        self.assertEqual({r.name for r in routers_for(None, [OLD], self.b)}, {'Main', 'Brusubi'})

    def test_voucher_from_blacklisted_device_is_refused_new_mac_learned_and_team_told(self):
        DeviceSignature.objects.filter(pk=self.sig.pk).update(blacklisted_at=timezone.now(), blacklist_reason='abuse')
        with mock.patch('core.device_block.block', return_value='ok') as blk:
            out = dl.claim(self.v, mac=NEW, fp='fp-bad')            # new random MAC, same phone
        self.assertFalse(out.allowed); self.assertIn('blocked on our Wi-Fi', out.message); self.assertIn('+220 300 1111', out.message)
        self.assertEqual(blk.call_args.args[1], [NEW])                # the new MAC is blocked too
        self.sig.refresh_from_db()
        self.assertEqual(self.sig.blacklist_hits, 1); self.assertIn(NEW, self.sig.macs)
        a = EventAlert.objects.get(kind='blacklist'); self.assertEqual(a.level, 'danger'); self.assertIn('GOOD1234', a.title)
        self.assertTrue(VoucherEvent.objects.filter(voucher=self.v, reason='Blacklisted device').exists())
        with mock.patch('core.device_block.block', return_value='ok'):
            self.assertFalse(dl.claim(self.v, mac=NEW, fp='').allowed)   # known by MAC now, even without device ID

    def test_remove_from_blacklist(self):
        with mock.patch('core.device_block.block', return_value='ok'):
            bl.blacklist(self.sig, self.owner, 'x')
        with mock.patch('core.device_block.unblock', return_value=2) as ub:
            self.c.post(f'/devices/{self.sig.pk}/action/', {'action': 'unblacklist'})
        self.assertTrue(ub.called)
        self.sig.refresh_from_db(); self.assertIsNone(self.sig.blacklisted_at)
        self.assertTrue(dl.claim(self.v, mac=OLD, fp='fp-bad').allowed)

    def test_live_sync_drops_it_anywhere(self):
        DeviceSignature.objects.filter(pk=self.sig.pk).update(blacklisted_at=timezone.now())
        cache.set(f'tt:tr:users:{self.r2.pk}', {'users': {'GOOD1234': [{'mac': OLD, 'ip': '10.5.50.9'}]}}, 600)
        with mock.patch('core.blacklist._kick') as k:
            self.assertEqual(bl.enforce(self.r2), 1)
        self.assertEqual(k.call_args.args[1], OLD)

    def test_flag_and_tell_me_when_it_comes_back(self):
        self.c.post(f'/devices/{self.sig.pk}/action/', {'action': 'flag_set', 'level': 'suspect', 'note': 'shared with 6 phones', 'notify': 'on'})
        self.sig.refresh_from_db(); self.assertEqual((self.sig.flag_level, self.sig.flagged, self.sig.flag_notify), ('suspect', True, True))
        self.assertTrue(dl.claim(self.v, mac=OLD, fp='fp-bad').allowed)                # flagged is allowed…
        self.assertIn('Flagged device', EventAlert.objects.get(kind='blacklist').title)  # …but you are told
        dl.claim(self.v, mac=OLD, fp='fp-bad')
        self.assertEqual(EventAlert.objects.count(), 1)                                   # not every time

    def test_page_and_link_command(self):
        page = self.c.get(f'/devices/{self.sig.pk}/')
        self.assertContains(page, 'Blacklist this device'); self.assertContains(page, 'Flag this device')
        from .agent import command_body
        self.assertIn('/ip hotspot host remove [find mac-address="AA:BB:CC:00:00:01"]', command_body(S(kind='hotspot_kick_mac', params={'mac': OLD})))
