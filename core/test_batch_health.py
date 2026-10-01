"""Batch page: profile & stock health — swap the profile of unused vouchers, align the router, unlock them.

Run:  DB_ENGINE=sqlite python manage.py test core.test_batch_health
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import batch_health as bh
from .models import (AgentCommand, Business, Router, RouterHotspotUser, Voucher, VoucherBatch, VoucherDeviceBinding, VoucherEvent,
                     VoucherPlan)


class FakeRes:
    def __init__(self, rows): self.rows = rows
    def get(self, **f): return [r for r in self.rows if all(str(r.get(k)) == str(v) for k, v in f.items())]
    def set(self, id, **kw):
        for r in self.rows:
            if r['id'] == id:
                r.update({k.replace('_', '-'): v for k, v in kw.items()})
    def remove(self, id): self.rows[:] = [r for r in self.rows if r['id'] != id]


class FakeSvc:
    def __init__(self, store): self.store = store; self.profiles = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def resource(self, path): return FakeRes(self.store.setdefault(path, []))
    def ensure_hotspot_profile(self, name, max_devices=1, rate_limit=''): self.profiles.append(name)


@override_settings(AUTH_EMAIL_OTP=False)
class BatchHealthTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('h@x.com', 'h@x.com', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='B', owner_name='O', phone='1', trial_ends_at=timezone.now() + timedelta(days=7))
        self.r = Router.objects.create(business=self.biz, name='hAP', ip_address='10.0.0.1', username='u', password='p', status='Online')
        self.plan = VoucherPlan.objects.create(business=self.biz, name='Daily', price=25, duration_minutes=1440, max_devices=1, mikrotik_profile_name='daily')
        self.weekly = VoucherPlan.objects.create(business=self.biz, name='Weekly', price=150, duration_minutes=10080, max_devices=2, mikrotik_profile_name='weekly')
        self.batch = VoucherBatch.objects.create(business=self.biz, name='VC-009', plan=self.plan, quantity=6)
        mk = lambda code, **kw: Voucher.objects.create(**{**dict(business=self.biz, router=self.r, batch=self.batch, code=code, plan_name='Daily',
                                                                 duration_minutes=1440, price=25, source='taptap'), **kw})
        self.ok, self.wrong, self.locked, self.gone = mk('OK000001'), mk('WR000001'), mk('LK000001'), mk('GO000001')
        self.used = mk('US000001', used_at=timezone.now() - timedelta(hours=1))
        self.frozen = mk('FZ000001', frozen_at=timezone.now())
        self.sneaky = mk('SN000001')                                      # TapTap thinks unused; the router shows uptime
        rows = [('OK000001', 'daily', 'false', '', '1d', '0s'), ('WR000001', '*1', 'false', '', '1d', '0s'),
                ('LK000001', 'daily', 'true', 'AA:BB:CC:00:00:01', '', '0s'), ('US000001', 'daily', 'false', '', '1d', '3h'),
                ('FZ000001', 'daily', 'true', '', '1d', '0s'), ('SN000001', 'daily', 'false', '', '1d', '20m')]
        self.store = {'/ip/hotspot/user': [], '/ip/hotspot/cookie': [{'id': '*c', 'user': 'LK000001', 'mac-address': 'AA:BB:CC:00:00:01'}]}
        for i, (n, prof, dis, mac, lim, up) in enumerate(rows):
            RouterHotspotUser.objects.create(business=self.biz, router=self.r, username=n, profile=prof, disabled=dis == 'true', mac_address=mac,
                                             limit_uptime=lim, uptime=up, source='taptap', last_seen_at=timezone.now())
            self.store['/ip/hotspot/user'].append({'id': f'*{i}', 'name': n, 'profile': prof, 'disabled': dis, 'mac-address': mac or '00:00:00:00:00:00',
                                                   'limit-uptime': lim, 'uptime': up, 'password': 'secret-' + n})
        VoucherDeviceBinding.objects.create(business=self.biz, voucher=self.wrong, slot_no=1, current_mac='DA:00:00:00:00:01')
        self.client.force_login(self.owner)

    def api(self):
        svc = FakeSvc(self.store)
        return svc, mock.patch('core.mikrotik.MikroTikService', return_value=svc), mock.patch('core.voucher_history.channel', return_value='Direct API')

    def test_check(self):
        h = bh.check(self.batch)
        self.assertEqual(h['total'], 5)                                   # used and frozen are not "unused stock"
        by = {x['v'].code: set(x['problems']) for x in h['vouchers']}
        self.assertEqual(by['OK000001'], set())
        self.assertEqual(by['WR000001'], {'profile', 'device_lock'})
        self.assertEqual(by['LK000001'], {'disabled', 'mac_lock', 'limit'})
        self.assertEqual(by['GO000001'], {'missing'})
        self.assertEqual(by['SN000001'], {'used_on_router'})
        self.assertEqual((h['ok'], h['profile']), (1, 'daily'))

    def test_repair_aligns_the_router_and_unlocks(self):
        svc, p1, p2 = self.api()
        with p1, p2, mock.patch('core.voucher_push.push_vouchers') as push:
            msg = bh.repair(self.batch, self.owner)
        users = {u['name']: u for u in self.store['/ip/hotspot/user']}
        self.assertEqual(users['WR000001']['profile'], 'daily')                       # the router follows TapTap
        lk = users['LK000001']
        self.assertEqual((lk['disabled'], lk['mac-address'], lk['limit-uptime']), ('no', '00:00:00:00:00:00', '1d'))
        self.assertEqual(self.store['/ip/hotspot/cookie'], [])
        self.assertEqual(lk['password'], 'secret-LK000001')                          # never touched
        self.assertEqual(users['US000001']['profile'], 'daily'); self.assertEqual(users['FZ000001']['disabled'], 'true')   # used/frozen untouched
        self.assertEqual(users['SN000001'].get('disabled'), 'false')                   # really in use: left alone
        self.assertIn('daily', svc.profiles)                                          # profile created if missing
        self.assertEqual([v.code for v in push.call_args.args[0]], ['GO000001'])       # missing TapTap voucher sent again
        self.assertFalse(VoucherDeviceBinding.objects.filter(voucher=self.wrong).exists())
        self.assertTrue(VoucherEvent.objects.filter(voucher=self.locked, event='note').exists())
        self.assertIn('fixed on the router', msg)

    def test_swap_to_a_router_profile(self):
        svc, p1, p2 = self.api()
        with p1, p2, mock.patch('core.voucher_push.push_vouchers'):
            bh.swap(self.batch, 'router:vip-fast', self.owner)
        users = {u['name']: u for u in self.store['/ip/hotspot/user']}
        self.assertEqual({users[c]['profile'] for c in ('OK000001', 'WR000001', 'LK000001')}, {'vip-fast'})
        self.assertEqual(users['US000001']['profile'], 'daily')                        # used: unchanged
        self.assertEqual(Voucher.objects.get(code='OK000001').router_profile, 'vip-fast')
        self.assertEqual(Voucher.objects.get(code='US000001').router_profile, '')
        self.assertIn('vip-fast', svc.profiles)
        self.assertEqual(Voucher.objects.get(code='OK000001').plan_name, 'Daily')       # keeps its plan and price
        with self.assertRaises(ValueError):
            bh.swap(self.batch, 'router:bad"; /system reset', self.owner)

    def test_swap_to_another_plans_profile_and_back(self):
        svc, p1, p2 = self.api()
        with p1, p2, mock.patch('core.voucher_push.push_vouchers'):
            bh.swap(self.batch, f'plan:{self.weekly.pk}', self.owner)
            self.assertEqual(Voucher.objects.get(code='OK000001').router_profile, 'weekly')
            bh.swap(self.batch, '', self.owner)                                     # back to the plan's own profile
        self.assertEqual(Voucher.objects.get(code='OK000001').router_profile, '')
        self.assertEqual({u['profile'] for u in self.store['/ip/hotspot/user'] if u['name'] in ('OK000001', 'WR000001')}, {'daily'})

    def test_link_heal_command(self):
        from .agent import command_body, queue
        with mock.patch('core.voucher_history.channel', return_value='TapTap Link'), mock.patch('core.linkops.ensure_online'), \
                mock.patch('core.voucher_push.push_vouchers'):
            msg = bh.repair(self.batch, self.owner)
        cmd = AgentCommand.objects.get(kind='hotspot_users_heal')
        body = command_body(cmd)
        self.assertIn('/ip hotspot user set [find name="WR000001"] profile="daily" disabled=no mac-address=00:00:00:00:00:00 limit-uptime="1d"', body)
        self.assertIn('/ip hotspot cookie remove [find user="LK000001"]', body)
        self.assertNotIn('password', body)
        self.assertIn('queued for TapTap Link', msg)
        with self.assertRaises(ValueError):
            queue(self.r, 'hotspot_users_heal', {'profiles': [], 'users': [{'n': 'A1', 'prof': 'x"; /system reset', 'lim': ''}]})

    def test_page(self):
        r = self.client.get(reverse('batch_detail', args=[self.batch.pk]))
        self.assertContains(r, 'Profile &amp; stock health')
        self.assertContains(r, '1 of 5'); self.assertContains(r, 'Fix them all'); self.assertContains(r, 'Swap profile')
        svc, p1, p2 = self.api()
        with p1, p2, mock.patch('core.voucher_push.push_vouchers'):
            r = self.client.post(reverse('batch_health_action', args=[self.batch.pk]), {'action': 'swap', 'target': 'router:vip-fast'})
        self.assertRedirects(r, reverse('batch_detail', args=[self.batch.pk]), fetch_redirect_response=False)
        self.assertEqual(Voucher.objects.get(code='OK000001').router_profile, 'vip-fast')

    def test_cashier_cannot_swap(self):
        from .models_team import TeamMember
        u = User.objects.create_user('c@x.com', 'c@x.com', 'pw')
        TeamMember.objects.create(business=self.biz, user=u, role='voucher_creator')
        self.client.force_login(u)
        self.client.post(reverse('batch_health_action', args=[self.batch.pk]), {'action': 'swap', 'target': 'router:vip-fast'})
        self.assertEqual(Voucher.objects.get(code='OK000001').router_profile, '')
