"""Bonanza › Free Wi-Fi: free codes, happy hours, lucky draws and gift time — no wheel."""
from datetime import time, timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core import free_drop as fd
from core.models import Business, Router, Voucher, VoucherDeviceBinding
from core.models_free_drop import FreeDrop, FreeDropClaim


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.now = timezone.now()
        self.owner = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456')
        self.b = Business.objects.create(user=self.owner, business_name='Kerr Wi-Fi', owner_name='O', phone='7000001',
                                         trial_ends_at=self.now + timedelta(days=30), feature_tier='pro')
        self.r = Router.objects.create(business=self.b, name='Hap', ip_address='10.0.0.1', username='u', password='p')
        self.paid = Voucher.objects.create(business=self.b, router=self.r, code='PAID0001', plan_name='Day', price=50,
                                           duration_minutes=1440, sold_at=self.now - timedelta(days=1))

    def drop(self, **kw):
        d = dict(business=self.b, name='Free hour', slug=kw.pop('slug', 'freeone1'), status='live', minutes=60)
        d.update(kw)
        return FreeDrop.objects.create(**d)


class Claims(Base):
    def test_first_come_first_served(self):
        d = self.drop(limit=2)
        a = fd.claim(d, device='dev-a')
        self.assertEqual(a.status, 'won')
        v = a.voucher
        self.assertEqual((v.price, v.duration_minutes, v.router_id, v.source), (0, 60, self.r.pk, 'taptap'))
        self.assertEqual(fd.claim(d, device='dev-a').pk, a.pk)          # same phone: same code, not a second one
        fd.claim(d, device='dev-b')
        self.assertEqual(fd.left(d), 0)
        with self.assertRaises(fd.FreeDropError):
            fd.claim(d, device='dev-c')
        self.assertEqual(Voucher.objects.filter(price=0).count(), 2)

    def test_closed_draft_and_phone(self):
        d = self.drop(status='draft')
        with self.assertRaises(fd.FreeDropError):
            fd.claim(d, device='x')
        d = self.drop(slug='freeone2', ask_phone=True)
        with self.assertRaises(fd.FreeDropError):
            fd.claim(d, device='x')
        c = fd.claim(d, device='x', phone='+220 312 3456')
        self.assertEqual(c.phone, '+2203123456')
        with self.assertRaises(fd.FreeDropError):
            fd.claim(self.drop(slug='freeone3', ends_at=self.now - timedelta(hours=1)), device='x')

    def test_only_customers(self):
        d = self.drop(audience='customers')
        with self.assertRaises(fd.FreeDropError):
            fd.claim(d, device='x')
        unpaid = Voucher.objects.create(business=self.b, router=self.r, code='SHELF001', plan_name='Day', price=50, duration_minutes=60)
        with self.assertRaises(fd.FreeDropError):
            fd.claim(d, device='x', code=unpaid.code)
        c = fd.claim(d, device='x', code='paid0001')
        self.assertEqual(c.proof_id, self.paid.pk)
        with self.assertRaises(fd.FreeDropError):
            fd.claim(d, device='y', code='PAID0001')                   # one gift per bought voucher
        other = Voucher.objects.create(business=self.b, router=self.r, code='PAID0002', plan_name='Day', price=50, duration_minutes=60,
                                       used_at=self.now)
        VoucherDeviceBinding.objects.create(business=self.b, voucher=other, current_mac='AA:BB:CC:DD:EE:01')
        c2 = fd.claim(d, device='z', mac='aa-bb-cc-dd-ee-01')          # recognised by the phone
        self.assertEqual(c2.proof_id, other.pk)

    def test_happy_hour_window_limit_and_cut(self):
        local = timezone.localtime(self.now)
        start = (local - timedelta(minutes=50)).time().replace(second=0, microsecond=0)
        end = (local + timedelta(minutes=10)).time().replace(second=0, microsecond=0)
        d = self.drop(daily_from=start, daily_to=end, limit=1, end_with_window=True)
        self.assertTrue(fd.is_open(d))
        c = fd.claim(d, device='a')
        self.assertLessEqual(c.minutes, 11)                             # stops with the happy hour
        with self.assertRaises(fd.FreeDropError):
            fd.claim(d, device='b')
        # tomorrow is a new happy hour: the limit and the device count start again
        tomorrow = self.now + timedelta(days=1)
        self.assertTrue(fd.is_open(d, tomorrow))
        self.assertEqual(fd.left(d, tomorrow), 1)
        d2 = self.drop(slug='closed01', daily_from=time(3, 0), daily_to=time(3, 1),
                       days=str((local.weekday() + 3) % 7))
        self.assertFalse(fd.is_open(d2) and local.time() > time(3, 1))
        self.assertIn(fd.state(d2)[0], ('soon', 'open'))


class Draw(Base):
    def test_lucky_draw(self):
        d = self.drop(pick='draw', limit=2, draw_at=self.now + timedelta(hours=1))
        for k in 'abcde':
            self.assertEqual(fd.claim(d, device=k).status, 'entered')
        self.assertEqual(Voucher.objects.filter(price=0).count(), 0)
        fd.tick(self.now)                                              # not due yet
        self.assertEqual(d.claims.filter(status='won').count(), 0)
        fd.tick(self.now + timedelta(hours=2))
        self.assertEqual(d.claims.filter(status='won').count(), 2)
        self.assertEqual(d.claims.filter(status='lost').count(), 3)
        self.assertEqual(fd.draw(d), 0)                                # once only
        d.refresh_from_db()
        self.assertEqual(fd.state(d)[0], 'draw_done')


class Gifts(Base):
    def setUp(self):
        super().setUp()
        self.inuse = []
        for i in range(4):
            self.inuse.append(Voucher.objects.create(business=self.b, router=self.r, code=f'USE{i:05d}', plan_name='Day', price=50,
                                                     duration_minutes=1440, used_at=self.now - timedelta(hours=1),
                                                     expires_at=self.now + timedelta(hours=23)))
        Voucher.objects.create(business=self.b, router=self.r, code='MEMBER01', plan_name='M', price=500, duration_minutes=1440,
                               used_at=self.now, expires_at=self.now + timedelta(days=5), login_type='member')

    def test_random_gift_adds_time_once(self):
        d = self.drop(kind='gift', target='active', random_only=True, limit=2, minutes=90)
        self.assertEqual(len(fd.gift_targets(d)), 4)                   # members left out, unused shelf stock too
        with mock.patch('core.voucher_history.enable', return_value=(True, '')) as en:
            self.assertEqual(fd.gift(d), 2)
            self.assertEqual(fd.gift(d), 0)                            # given once
        self.assertEqual(en.call_args.kwargs['add_minutes'], 90)
        self.assertEqual(d.claims.filter(status='gifted').count(), 2)

    def test_repeating_gift_each_happy_hour(self):
        local = timezone.localtime(self.now)
        d = self.drop(kind='gift', target='active', daily_from=(local - timedelta(minutes=5)).time(), daily_to=(local + timedelta(minutes=30)).time())
        with mock.patch('core.voucher_history.enable', return_value=(True, '')) as en:
            fd.tick(self.now)
            fd.tick(self.now + timedelta(minutes=2))
        self.assertEqual(en.call_count, 4)
        d.refresh_from_db()
        self.assertEqual(len(d.gift_runs), 1)

    def test_online_now(self):
        d = self.drop(kind='gift', target='online')
        with mock.patch('core.fit_bulk.online_codes', return_value={'USE00001'}):
            self.assertEqual([v.code for v in fd.gift_targets(d)], ['USE00001'])

    def test_housekeeping_runs_the_tick(self):
        from core import live
        self.assertTrue(getattr(live, '_free_drop_installed', False))


class Pages(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_owner_pages(self):
        r = self.client.get(reverse('free_drops'))
        self.assertContains(r, 'Free hour — first 50')
        r = self.client.get(reverse('free_drop_new') + '?recipe=happy')
        self.assertContains(r, 'value="18:00"')
        r = self.client.post(reverse('free_drop_new'), {'name': 'Party', 'kind': 'claim', 'hours': '2', 'mins': '30', 'pick': 'first', 'audience': 'everyone',
                                                       'limit': '100', 'when': 'once', 'status': 'live', 'one_per_device': 'on', 'headline': 'Party night'})
        d = FreeDrop.objects.get(name='Party')
        self.assertRedirects(r, reverse('free_drop_edit', args=[d.pk]))
        self.assertEqual((d.minutes, d.limit, d.status), (150, 100, 'live'))
        self.assertContains(self.client.get(reverse('free_drop_edit', args=[d.pk])), 'QR poster')
        self.assertContains(self.client.get(reverse('free_drop_poster', args=[d.pk])), 'qrcode(0')
        self.assertContains(self.client.get(reverse('bonanza_list')), 'Free Wi-Fi')
        r = self.client.post(reverse('free_drop_action', args=[d.pk]), {'action': 'paused'})
        d.refresh_from_db()
        self.assertEqual(d.status, 'paused')

    def test_public_claim_flow(self):
        d = self.drop(limit=5, headline='Free hour!')
        self.client.logout()
        r = self.client.get(f'/f/{d.slug}/?mac=AA:BB:CC:DD:EE:02&login=http://10.5.50.1/login')
        self.assertContains(r, 'Free hour!')
        self.assertIn('ttfd', r.cookies)
        r = self.client.post(reverse('free_drop_claim', args=[d.slug]), {'mac': 'AA:BB:CC:DD:EE:02'}, content_type='application/json')
        j = r.json()
        self.assertTrue(j['ok'])
        self.assertEqual(j['left'], 4)
        code = j['result']['code']
        again = self.client.post(reverse('free_drop_claim', args=[d.slug]), {}, content_type='application/json').json()
        self.assertEqual(again['result']['code'], code)                  # the same phone keeps its code
        self.assertEqual(self.client.get(reverse('free_drop_mine', args=[d.slug])).json()['result']['code'], code)
        self.assertContains(self.client.get(f'/f/{d.slug}/'), code)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('free_drop_people', args=[d.pk])), code)

    def test_draft_is_private(self):
        d = self.drop(status='draft')
        self.client.logout()
        self.assertEqual(self.client.get(f'/f/{d.slug}/').status_code, 404)
        gift = self.drop(slug='giftonly', kind='gift')
        self.assertEqual(self.client.get(f'/f/{gift.slug}/').status_code, 404)

    def test_other_business_cannot_edit(self):
        d = self.drop()
        stranger = User.objects.create_user('s@x.gm', 's@x.gm', 'pw-123456')
        Business.objects.create(user=stranger, business_name='S', owner_name='S', phone='2', trial_ends_at=self.now + timedelta(days=3), feature_tier='pro')
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(reverse('free_drop_edit', args=[d.pk])).status_code, 404)
        self.assertEqual(FreeDropClaim.objects.count(), 0)
