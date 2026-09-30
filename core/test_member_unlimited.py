from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.utils import timezone

from . import members as mem
from .agent import command_body
from .models import Business, Router, RouterHotspotProfile, RouterHotspotUser, VoucherPlan
from .utils import voucher_profile


class UnlimitedMemberTests(TestCase):
    """A member that is unlimited in TapTap must have no time limit on the MikroTik either."""
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=self.b, name='Kotu', ip_address='1.1.1.1', username='a', password='b')
        # a plan imported from a Mikhmon profile that says 1 day, set to Unlimited + free in TapTap
        self.plan = VoucherPlan.objects.create(business=self.b, name='VIP', price=0, is_free=True, duration_minutes=0,
                                               duration_unit='unlimited', max_devices=2, speed_limit='5M/5M',
                                               source='mikrotik', mikrotik_profile_name='vip-1d')
        self.c = Client(); self.c.force_login(self.owner)

    def test_unlimited_member_gets_unlimited_profile(self):
        v = mem.create_member(self.b, username='awa', same=True, plan_value='free', router=self.r)
        self.assertEqual(voucher_profile(v, None), ('taptap-unlimited-1-device', 1, ''))
        v2 = mem.create_member(self.b, username='musa', same=True, plan_value=str(self.plan.pk), router=self.r)
        prof, shared, rate = voucher_profile(v2, self.plan)
        self.assertEqual((prof, shared, rate), ('taptap-unlimited-2-devices-5M-5M', 2, '5M/5M'))   # not the Mikhmon 'vip-1d' profile

    def test_router_scripts_leave_no_time_limit(self):
        from types import SimpleNamespace as S
        body = command_body(S(kind='hotspot_users', params={
            'profiles': [{'name': 'taptap-unlimited-1-device', 'shared': 1, 'rate': ''}],
            'users': [{'n': 'awa', 'prof': 'taptap-unlimited-1-device', 'lim': '0s', 'dis': False, 'pw': 'secret'}]}))
        self.assertIn('session-timeout=0s on-login=""', body)
        self.assertIn('limit-uptime="0s"', body.replace('limit-uptime=0s', 'limit-uptime="0s"'))
        added = {}
        class Res:
            def get(s, **k): return []
            def add(s, **k): added.update(k); return '*1'
        class Svc:
            router = self.r
            def resource(s, p): return Res()
        from .mikrotik import MikroTikService
        MikroTikService.ensure_hotspot_profile(Svc(), 'taptap-unlimited-1-device', 1, '')
        self.assertEqual((added['session_timeout'], added['on_login']), ('0s', ''))

    def test_members_page_flags_router_that_disagrees(self):
        v = mem.create_member(self.b, username='awa', same=True, plan_value='free', router=self.r)
        RouterHotspotProfile.objects.create(business=self.b, router=self.r, name='old-1d', session_timeout='1d',
                                            raw_data={'on-login': ':put (",rem,0,1d,0,,Disable,");'})
        RouterHotspotUser.objects.create(business=self.b, router=self.r, username='awa', profile='old-1d', is_present=True)
        r = self.c.get('/members/')
        self.assertContains(r, 'Fix on router')
        self.assertContains(r, 'per session (profile old-1d)')
        with mock.patch('core.views_agents.push_one', return_value=True) as m:
            self.c.post(f'/members/{v.pk}/push/', {'next': '/members/'})
            self.assertTrue(m.called)

    def test_limited_voucher_keeps_its_plan_profile(self):
        day = VoucherPlan.objects.create(business=self.b, name='1 Day', price=25, duration_minutes=1440, mikrotik_profile_name='daily')
        from .models import Voucher
        v = Voucher.objects.create(business=self.b, router=self.r, code='ABCD2345', plan_name='1 Day', duration_minutes=1440)
        self.assertEqual(voucher_profile(v, day)[0], 'daily')
        v.plan_name = ''; v.save()
        self.assertEqual(voucher_profile(v, None)[0], 'taptap-1-device')
