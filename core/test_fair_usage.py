"""Fair usage: speed steps by data used, per day / week / voucher.

Run:  DB_ENGINE=sqlite python manage.py test core.test_fair_usage
"""
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import fair_usage as fu
from .models import Business, Router, UsageRecord, Voucher, VoucherPlan
from .models_fup import FairUsagePolicy, FairUsageState
from .models_team import TeamMember

GB = 1024 ** 3
HTML = {'HTTP_ACCEPT': 'text/html'}
CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


class FakeQueues:
    def __init__(self):
        self.rows, self.n = [], 0

    def get(self, name=None):
        return [r for r in self.rows if name is None or r['name'] == name]

    def add(self, **f):
        self.n += 1
        row = {'id': f'*{self.n}', **{k: v for k, v in f.items() if k != 'place_before'}}
        if 'place_before' in f:
            self.rows.insert(0, row)
        else:
            self.rows.append(row)

    def set(self, id, **f):
        next(r for r in self.rows if r['id'] == id).update(f)

    def remove(self, id):
        self.rows[:] = [r for r in self.rows if r['id'] != id]


class FakeSvc:
    def __init__(self):
        self.q = FakeQueues()

    def resource(self, path):
        return self.q


@override_settings(AUTH_EMAIL_OTP=False, CACHES=CACHE)
class FairUsageTests(TestCase):
    def setUp(self):
        cache.clear()
        # a fixed midday, so "an hour ago" is always today whatever time the tests run
        self.now = timezone.make_aware(datetime.combine(timezone.localdate(), time(13, 30)))
        self.owner = User.objects.create_user('fu@example.com', 'fu@example.com', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='B', owner_name='O', phone='1', trial_ends_at=self.now + timedelta(days=7))
        self.router = Router.objects.create(business=self.biz, name='R', ip_address='10.0.0.1', username='u', password='p')
        self.day = VoucherPlan.objects.create(business=self.biz, name='Day', price=40, duration_minutes=1440)
        self.week = VoucherPlan.objects.create(business=self.biz, name='Week', price=200, duration_minutes=10080)
        self.v = Voucher.objects.create(business=self.biz, router=self.router, code='FU1', plan_name='Day', duration_minutes=1440,
                                        used_at=self.now - timedelta(hours=2))
        self.pol = FairUsagePolicy.objects.create(business=self.biz, name='Daily', period='day', counts='total',
                                                  tiers=[{'gb': 2, 'down': 5, 'up': 2}, {'gb': 5, 'down': 1, 'up': 0.5}])

    def use(self, gb, code='FU1', hours_ago=0, up=0):
        hour = (self.now - timedelta(hours=hours_ago)).replace(minute=0, second=0, microsecond=0)
        UsageRecord.objects.create(business=self.biz, router=self.router, username=code, hour=hour, download=int(gb * GB), upload=int(up * GB))

    def active(self, code='FU1', ip='10.5.50.20'):
        return [{'user': code, 'address': ip, 'mac-address': 'AA:BB:CC:DD:EE:FF'}]

    def test_tiers(self):
        tiers = fu.clean_tiers([{'gb': 5, 'down': 1, 'up': .5}, {'gb': 2, 'down': 5}, {'gb': 0, 'down': 3}, {'gb': 'x'}])
        self.assertEqual(tiers, [{'gb': 2.0, 'down': 5.0, 'up': 5.0}, {'gb': 5.0, 'down': 1.0, 'up': 0.5}])
        self.assertEqual([fu.tier_for(x * GB, tiers) for x in (1, 2, 4.9, 5, 99)], [0, 1, 1, 2, 2])
        self.assertEqual((fu.kbps(5), fu.kbps(0.01), fu.speed_text(0.5)), (5000, 32, '500 kb/s'))

    def test_steps_down_and_caps_on_router(self):
        svc = FakeSvc()
        self.use(1)
        self.assertEqual(fu.enforce(self.router, self.active(), self.now, svc=svc)['capped'], 0)
        self.assertEqual(svc.q.rows, [])
        self.use(1.5, hours_ago=1)                        # 2.5 GB today → step 1
        fu.enforce(self.router, self.active(), self.now, svc=svc)
        self.assertEqual(len(svc.q.rows), 1)
        q = svc.q.rows[0]
        self.assertEqual((q['name'], q['target'], q['max_limit']), ('TTFUP-FU1-10.5.50.20', '10.5.50.20/32', '2000k/5000k'))
        self.use(3, hours_ago=2)                          # 5.5 GB → step 2
        fu.enforce(self.router, self.active(), self.now, svc=svc)
        self.assertEqual(svc.q.rows[0]['max_limit'], '500k/1000k')
        self.assertEqual(FairUsageState.objects.get(voucher=self.v).tier, 2)
        self.assertEqual(self.v.events.filter(event='fup_slowed').count(), 2)
        fu.enforce(self.router, [], self.now, svc=svc)    # logged off → cap removed (IP may go to someone else)
        self.assertEqual(svc.q.rows, [])

    def test_yesterday_does_not_count_for_a_daily_policy(self):
        start = timezone.make_aware(datetime.combine(timezone.localtime(self.now).date(), time.min))
        UsageRecord.objects.create(business=self.biz, router=self.router, username='FU1', hour=start - timedelta(hours=2), download=9 * GB)
        self.assertLess(fu.used_bytes(self.v, self.pol, self.now), 1 * GB)
        self.pol.period = 'voucher'
        self.v.used_at = start - timedelta(hours=3)
        self.assertGreaterEqual(fu.used_bytes(self.v, self.pol, self.now), 9 * GB)   # whole voucher counts it

    def test_download_only_and_free_hours(self):
        self.use(1, up=3)
        self.assertEqual(fu.used_bytes(self.v, self.pol, self.now), 4 * GB)
        self.pol.counts = 'download'
        self.assertEqual(fu.used_bytes(self.v, self.pol, self.now), 1 * GB)
        h = timezone.localtime(self.now).hour
        self.pol.free_from, self.pol.free_to = h, (h + 1) % 24
        self.assertEqual(fu.used_bytes(self.v, self.pol, self.now), 0)          # this hour is free
        self.assertTrue(FairUsagePolicy(free_from=23, free_to=6).is_free_hour(2))
        self.assertFalse(FairUsagePolicy(free_from=23, free_to=6).is_free_hour(12))

    def test_plan_policy_wins_over_all_plans(self):
        weekly = FairUsagePolicy.objects.create(business=self.biz, name='Weekly', period='week', tiers=[{'gb': 10, 'down': 2, 'up': 1}])
        weekly.plans.add(self.week)
        wv = Voucher.objects.create(business=self.biz, code='WK1', plan_name='Week', duration_minutes=10080)
        pols = fu.policies(self.biz)
        self.assertEqual(fu.policy_for(wv, pols), weekly)
        self.assertEqual(fu.policy_for(self.v, pols), self.pol)
        self.pol.plans.add(self.week)      # "Daily" now names Week too, but Day is no longer covered
        self.assertIsNone(fu.policy_for(self.v, fu.policies(self.biz)))

    def test_lift_gives_full_speed_until_reset(self):
        svc = FakeSvc()
        self.use(3)
        fu.enforce(self.router, self.active(), self.now, svc=svc)
        self.assertEqual(len(svc.q.rows), 1)
        until = fu.lift(self.v, self.owner, self.now)
        self.assertGreater(until, self.now)
        fu.enforce(self.router, self.active(), self.now, svc=svc)
        self.assertEqual(svc.q.rows, [])
        self.assertEqual(fu.status(self.v, self.now)['lifted'], True)
        fu.unlift(self.v, self.owner)
        fu.enforce(self.router, self.active(), self.now, svc=svc)
        self.assertEqual(len(svc.q.rows), 1)

    def test_policy_off_removes_caps(self):
        svc = FakeSvc()
        self.use(3)
        fu.enforce(self.router, self.active(), self.now, svc=svc)
        self.pol.active = False
        self.pol.save()
        fu.enforce(self.router, self.active(), self.now, svc=svc)
        self.assertEqual(svc.q.rows, [])

    def test_link_router_gets_a_command(self):
        self.use(3)
        with mock.patch('core.linkops.send') as send:
            fu.enforce(self.router, self.active(), self.now)
        kind, params = send.call_args[0][1], send.call_args[0][2]
        self.assertEqual(kind, 'fup_queues')
        self.assertEqual(params['set'], [['TTFUP-FU1-10.5.50.20', '10.5.50.20', '2000k/5000k']])
        fu.validate_link_params(params)
        script = fu.link_script(params)
        self.assertIn('max-limit="2000k/5000k"', script)
        self.assertIn('place-before=0', script)
        with self.assertRaises(ValueError):
            fu.validate_link_params({'set': [['TTFUP-X', '10.0.0.1; /system reboot', '1k/1k']]})

    def test_pages(self):
        self.client.force_login(self.owner)
        r = self.client.post(reverse('fup_new'), {'name': 'Strict', 'active': '1', 'period': 'week', 'counts': 'download',
                                                  'plans': [self.week.pk], 'free_hours': '1', 'free_from': '23', 'free_to': '6',
                                                  'tiers_json': '[{"gb":10,"down":2,"up":1},{"gb":4,"down":4,"up":2}]'}, **HTML)
        self.assertEqual(r.status_code, 302)
        p = FairUsagePolicy.objects.get(name='Strict')
        self.assertEqual((p.period, p.counts, p.free_from, p.free_to, [t['gb'] for t in p.tiers]), ('week', 'download', 23, 6, [4.0, 10.0]))
        self.assertContains(self.client.get(reverse('security'), **HTML), 'Strict')
        self.assertEqual(self.client.get(reverse('fup_edit', args=[p.pk]), **HTML).status_code, 200)
        self.use(3)
        r = self.client.get(reverse('voucher_detail', args=[self.v.pk]), **HTML)
        self.assertContains(r, 'Fair usage')
        self.assertContains(r, 'Full speed until the period resets')

    def test_roles(self):
        for role, code in [('admin', 200), ('voucher_support', 403), ('viewer', 403)]:
            u = User.objects.create_user(f'{role}@fu.com', f'{role}@fu.com', 'pw')
            TeamMember.objects.create(business=self.biz, user=u, role=role)
            self.client.force_login(u)
            self.assertEqual(self.client.get(reverse('fup_new'), **HTML).status_code, code, role)
        self.client.force_login(User.objects.get(username='voucher_support@fu.com'))
        self.use(3)
        self.client.post(reverse('fup_lift', args=[self.v.pk]), {'action': 'lift'}, **HTML)
        self.assertIsNotNone(FairUsageState.objects.get(voucher=self.v).lifted_until)   # support may lift for a customer


@override_settings(AUTH_EMAIL_OTP=False, CACHES=CACHE)
class SlowedDetailsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.now = timezone.make_aware(datetime.combine(timezone.localdate(), time(13, 30)))
        self.owner = User.objects.create_user('sl@example.com', 'sl@example.com', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='B', owner_name='O', phone='1', trial_ends_at=self.now + timedelta(days=7))
        self.router = Router.objects.create(business=self.biz, name='Main', ip_address='10.0.0.1', username='u', password='p')
        VoucherPlan.objects.create(business=self.biz, name='Day', price=40, duration_minutes=1440)
        self.pol = FairUsagePolicy.objects.create(business=self.biz, name='Daily', period='day',
                                                  tiers=[{'gb': 2, 'down': 5, 'up': 2}, {'gb': 5, 'down': 1, 'up': 0.5}])
        self.v = Voucher.objects.create(business=self.biz, router=self.router, code='SLOW1', plan_name='Day', duration_minutes=1440,
                                        used_at=self.now - timedelta(hours=1))
        hour = self.now.replace(minute=0, second=0, microsecond=0)
        UsageRecord.objects.create(business=self.biz, router=self.router, username='SLOW1', hour=hour, download=6 * GB, peak_bps=8_000_000)
        fu.enforce(self.router, [{'user': 'SLOW1', 'address': '10.5.50.9'}], timezone.now(), svc=FakeSvc())   # step 2
        FairUsageState.objects.filter(voucher=self.v).update(used_bytes=6 * GB)
        self.client.force_login(self.owner)

    def online(self, down_bps=800_000, up_bps=100_000):
        cache.set(f'tt:tr:users:{self.router.pk}', {'at': timezone.now().isoformat(), 'users': {'SLOW1': [
            {'ip': '10.5.50.9', 'mac': 'AA:BB:CC:00:11:22', 'down_bps': down_bps, 'up_bps': up_bps, 'session_down': 0, 'session_up': 0, 'uptime': '1h2m'}]}}, 600)

    def test_details_of_a_slowed_voucher(self):
        self.online()
        rows = fu.slowed(self.biz)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r['tier'], r['steps'], r['cap'], r['online']), (2, 2, {'down': '1 Mb/s', 'up': '500 kb/s'}, True))
        self.assertEqual((r['down_now'], r['up_now'], r['down_pct'], r['peak'], r['next']), ('800 kb/s', '100 kb/s', 80, '8.0 Mb/s', None))
        self.assertEqual(r['sessions'][0]['ip'], '10.5.50.9')

    def test_offline_and_lifted(self):
        r = fu.slowed(self.biz)[0]
        self.assertEqual((r['online'], r['down_pct']), (False, 0))
        fu.lift(self.v, self.owner)
        self.assertEqual(fu.slowed(self.biz), [])

    def test_pages(self):
        self.online(down_bps=990_000)
        r = self.client.get(reverse('fup_slowed'), **HTML)
        self.assertEqual(r.status_code, 200)
        for text in ('SLOW1', 'Step 2 of 2', '1 Mb/s', '990 kb/s', '99% of the cap', '10.5.50.9', 'AA:BB:CC:00:11:22'):
            self.assertContains(r, text)
        self.assertContains(self.client.get(reverse('fup_slowed'), {'policy': self.pol.pk + 99}, **HTML), 'Nobody is slowed')
        d = self.client.get(reverse('voucher_detail', args=[self.v.pk]), **HTML)
        self.assertContains(d, 'Speed cap')
        self.assertContains(d, '99% of the cap')
        self.assertContains(self.client.get(reverse('security'), **HTML), reverse('fup_slowed'))

    def test_who_can_see(self):
        for role, code in [('voucher_support', 200), ('admin', 200), ('finance', 403), ('viewer', 403)]:
            u = User.objects.create_user(f'{role}@sl.com', f'{role}@sl.com', 'pw')
            TeamMember.objects.create(business=self.biz, user=u, role=role)
            self.client.force_login(u)
            self.assertEqual(self.client.get(reverse('fup_slowed'), **HTML).status_code, code, role)


class SharedVoucherPerDeviceTests(TestCase):
    """A voucher for several devices: only the device that used the data is slowed."""
    def setUp(self):
        cache.clear()
        self.now = timezone.make_aware(datetime.combine(timezone.localdate(), time(13, 30)))
        owner = User.objects.create_user('pd@example.com', 'pd@example.com', 'pw')
        self.biz = Business.objects.create(user=owner, business_name='B', owner_name='O', phone='1', trial_ends_at=self.now + timedelta(days=7))
        self.router = Router.objects.create(business=self.biz, name='R', ip_address='10.0.0.1', username='u', password='p')
        VoucherPlan.objects.create(business=self.biz, name='Family', price=100, duration_minutes=1440, max_devices=3)
        self.v = Voucher.objects.create(business=self.biz, router=self.router, code='FAM1', plan_name='Family', duration_minutes=1440,
                                        max_devices=3, used_at=self.now - timedelta(hours=2))
        self.pol = FairUsagePolicy.objects.create(business=self.biz, name='Daily', period='day', counts='total',
                                                  tiers=[{'gb': 2, 'down': 5, 'up': 2}, {'gb': 5, 'down': 1, 'up': 0.5}])
        self.hour = self.now.replace(minute=0, second=0, microsecond=0)

    def use(self, mac, gb):
        UsageRecord.objects.create(business=self.biz, router=self.router, username='FAM1', mac_address=mac, hour=self.hour, download=int(gb * GB))

    def test_only_the_heavy_device_is_capped(self):
        self.use('AA:00:00:00:00:01', 3.0)      # heavy downloader
        self.use('AA:00:00:00:00:02', 0.4)
        self.use('AA:00:00:00:00:03', 0.1)
        active = [{'user': 'FAM1', 'address': '10.5.50.11', 'mac-address': 'AA:00:00:00:00:01'},
                  {'user': 'FAM1', 'address': '10.5.50.12', 'mac-address': 'AA:00:00:00:00:02'},
                  {'user': 'FAM1', 'address': '10.5.50.13', 'mac-address': 'AA:00:00:00:00:03'}]
        caps = fu.desired_caps(self.router, active, self.now)
        self.assertEqual([c[0] for c in caps.values()], ['10.5.50.11'])        # only the heavy device's IP
        self.assertEqual(list(caps.values())[0][1], '2000k/5000k')
        st = fu.status(self.v, self.now)
        self.assertTrue(st['per_device'])
        tiers = {d['label']: d['tier'] for d in st['devices']}
        self.assertEqual(tiers['AA:00:00:00:00:01'], 1)
        self.assertEqual(tiers['AA:00:00:00:00:02'], 0)

    def test_device_followed_across_mac_change(self):
        from .models import VoucherDeviceBinding
        VoucherDeviceBinding.objects.create(business=self.biz, voucher=self.v, slot_no=1, device_token_hash='fp1',
                                            current_mac='AA:00:00:00:00:09', previous_mac='AA:00:00:00:00:01', label='Pixel 8')
        self.use('AA:00:00:00:00:01', 1.5)      # before the phone changed its MAC
        self.use('AA:00:00:00:00:09', 1.0)      # after: same device, 2.5 GB in total
        caps = fu.desired_caps(self.router, [{'user': 'FAM1', 'address': '10.5.50.20', 'mac-address': 'AA:00:00:00:00:09'}], self.now)
        self.assertEqual(len(caps), 1)
        self.assertEqual(fu.status(self.v, self.now)['devices'][0]['label'], 'Pixel 8')
