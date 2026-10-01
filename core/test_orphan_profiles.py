"""Vouchers whose plan is a RouterOS internal ID (*1, *C…): find them, fix them, stop new ones.

Run:  DB_ENGINE=sqlite python manage.py test core.test_orphan_profiles
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import orphan_profiles as op
from .models import AgentCommand, Business, Router, RouterHotspotProfile, RouterHotspotUser, Voucher, VoucherPlan


class FakeRes:
    def __init__(self, rows): self.rows = rows
    def get(self, **f): return [r for r in self.rows if all(str(r.get(k)) == str(v) for k, v in f.items())]
    def set(self, id, **kw):
        for r in self.rows:
            if r['id'] == id:
                r.update(kw)


class FakeSvc:
    def __init__(self, users): self.users = users; self.profiles = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def resource(self, path): return FakeRes(self.users)
    def ensure_hotspot_profile(self, name, max_devices=1, rate_limit=''):
        self.profiles.append((name, max_devices, rate_limit)); return '*P'


@override_settings(AUTH_EMAIL_OTP=False)
class OrphanProfileTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('o@x.com', 'o@x.com', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='B', owner_name='O', phone='1', trial_ends_at=timezone.now() + timedelta(days=7))
        self.r = Router.objects.create(business=self.biz, name='hAP', ip_address='10.0.0.1', username='u', password='p', status='Online')
        self.plan = VoucherPlan.objects.create(business=self.biz, name='24 Hours', price=25, duration_minutes=1440, max_devices=1, mikrotik_profile_name='24hours')
        now = timezone.now()
        for i in range(3):
            Voucher.objects.create(business=self.biz, router=self.r, code=f'ST{i:04d}', plan_name='*1', source='mikrotik', duration_minutes=0, price=0,
                                   used_at=now - timedelta(hours=2) if i == 0 else None)
        Voucher.objects.create(business=self.biz, router=self.r, code='CC0001', plan_name='*C', source='mikrotik')
        Voucher.objects.create(business=self.biz, router=self.r, code='OK0001', plan_name='24 Hours')
        self.client.force_login(self.owner)

    def test_ids_are_recognised(self):
        for good in ('*1', '*10', '*C', '*1A'):
            self.assertTrue(op.is_orphan(good), good)
        for bad in ('1-Device', '24 HOURS', '*', 'default', '*x!'):
            self.assertFalse(op.is_orphan(bad), bad)

    def test_groups(self):
        RouterHotspotProfile.objects.create(business=self.biz, router=self.r, name='24hours', mikrotik_id='*1')
        g = {x['profile']: x for x in op.groups(self.biz)}
        self.assertEqual(set(g), {'*1', '*C'})
        self.assertEqual((g['*1']['total'], g['*1']['used'], g['*1']['unused']), (3, 1, 2))
        self.assertEqual((g['*1']['suggestion'], g['*1']['plan']), ('24hours', self.plan))     # the router still knows *1
        self.assertEqual(g['*C']['suggestion'], '')

    def test_import_turns_a_known_id_into_its_name(self):
        RouterHotspotProfile.objects.create(business=self.biz, router=self.r, name='24hours', mikrotik_id='*1')
        self.assertEqual(op.resolve(self.r, '*1'), '24hours')
        self.assertEqual(op.resolve(self.r, '*9'), '*9')            # gone for good: left for the Fix tab
        self.assertEqual(op.resolve(self.r, 'daily'), 'daily')

    def test_fix_over_the_api_creates_the_profile_and_moves_users(self):
        users = [{'id': f'*u{i}', 'name': f'ST{i:04d}', 'profile': '*1'} for i in range(3)] + [{'id': '*x', 'name': 'OTHER', 'profile': '*1'}]
        RouterHotspotUser.objects.create(business=self.biz, router=self.r, username='ST0001', profile='*1', last_seen_at=timezone.now())
        svc = FakeSvc(users)
        with mock.patch('core.mikrotik.MikroTikService', return_value=svc), mock.patch('core.voucher_history.channel', return_value='Direct API'):
            msg = op.fix(self.biz, self.r.pk, '*1', self.plan, self.owner)
        self.assertEqual(svc.profiles, [('24hours', 1, '')])                 # created if missing
        self.assertEqual([u['profile'] for u in users], ['24hours'] * 3 + ['*1'])   # only these vouchers move
        self.assertIn('3 vouchers now use the plan “24 Hours”', msg)
        used, unused = Voucher.objects.get(code='ST0000'), Voucher.objects.get(code='ST0001')
        self.assertEqual((used.plan_name, used.duration_minutes), ('24 Hours', 0))            # used: keeps its time
        self.assertEqual((unused.plan_name, unused.duration_minutes, unused.price), ('24 Hours', 1440, 25))
        self.assertEqual(RouterHotspotUser.objects.get(username='ST0001').profile, '24hours')
        self.assertEqual([g['profile'] for g in op.groups(self.biz)], ['*C'])

    def test_fix_over_taptap_link(self):
        from .agent import command_body, queue
        with mock.patch('core.voucher_history.channel', return_value='TapTap Link'), mock.patch('core.linkops.ensure_online'):
            op.fix(self.biz, self.r.pk, '*C', self.plan, self.owner)
        cmd = AgentCommand.objects.get(kind='hotspot_users_profile')
        body = command_body(cmd)
        self.assertIn('/ip hotspot user profile add name="24hours" shared-users=1', body)
        self.assertIn('/ip hotspot user set [find name=$n] profile="24hours"', body)
        self.assertIn('"CC0001"', body)
        with self.assertRaises(ValueError):
            queue(self.r, 'hotspot_users_profile', {'profile': {'name': 'x"; /system reset', 'shared': 1}, 'names': ['A1']})

    def test_plans_page_tab_and_fix_with_a_new_plan(self):
        r = self.client.get(reverse('plans'))
        self.assertContains(r, 'Fix unknown profiles'); self.assertContains(r, '<code class="fs-6">*1</code>', html=False)
        with mock.patch('core.mikrotik.MikroTikService', return_value=FakeSvc([])), mock.patch('core.voucher_history.channel', return_value='Direct API'):
            r = self.client.post(reverse('plan_fix_profile'), {'profile': '*C', 'router_id': self.r.pk, 'plan': 'new',
                                                              'new_name': 'Weekly', 'new_price': '150', 'new_minutes': '10080', 'new_devices': '2'})
        self.assertRedirects(r, '/plans/#fix', fetch_redirect_response=False)
        weekly = VoucherPlan.objects.get(business=self.biz, name='Weekly')
        self.assertEqual((weekly.price, weekly.duration_minutes, weekly.max_devices), (150, 10080, 2))
        self.assertEqual(Voucher.objects.get(code='CC0001').plan_name, 'Weekly')
        bad = self.client.post(reverse('plan_fix_profile'), {'profile': '24 Hours', 'router_id': self.r.pk, 'plan': self.plan.pk})
        self.assertEqual(Voucher.objects.get(code='OK0001').plan_name, '24 Hours')          # only *IDs can be "fixed"
        self.assertEqual(bad.status_code, 302)
