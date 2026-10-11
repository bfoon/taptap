"""Member plans put their price on the router profile, and their profile rows on the Plans page show it.

Run:  DB_ENGINE=sqlite python manage.py test core.test_member_plan_prices
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .member_plan_prices import plan_id, router_comment, sync_business
from .models import Business, Router, Voucher, VoucherPlan
from .models_member_plans import MemberPlan, MemberPlanAssignment
from .sync import _profile_to_plan, price_from_text, profile_price


@override_settings(AUTH_EMAIL_OTP=False)
class MemberPlanPriceTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('o', 'o@x.gm', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='TapTap K', owner_name='B', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=self.b, name='TapTap K', ip_address='10.0.0.1', username='u', password='p')
        self.monthly = MemberPlan.objects.create(business=self.b, name='Member Monthly 7', price=Decimal('1000'),
                                                 duration_minutes=43200, duration_unit='months', max_devices=7, speed_limit='2M/2M')
        self.free = MemberPlan.objects.create(business=self.b, name='Unlimited', price=0, duration_minutes=0,
                                              duration_unit='unlimited', max_devices=11, speed_limit='10M/10M')
        self.client.force_login(self.owner)

    def summary(self):
        return {'duplicate_plans_skipped': 0, 'pulled_plans': 0}

    def test_comment_round_trips_through_the_importer(self):
        c = router_comment(self.monthly)
        self.assertEqual(c, 'TapTap member plan · Member Monthly 7 · price=1000')
        self.assertEqual(price_from_text(c, 'D'), 1000)                       # TapTap's own reader finds it
        self.assertEqual(profile_price({'name': 'taptap-member-plan-1', 'comment': c}, 'D')[0], 1000)
        self.assertEqual(router_comment(self.free), 'TapTap member plan · Unlimited · free')
        odd = MemberPlan(name='1 Device D400 plan', price=Decimal('400.50'))   # a number in the name must not win
        self.assertEqual(price_from_text(router_comment(odd), 'D'), Decimal('400.5'))
        self.assertEqual(plan_id('taptap-unlimited-member-plan-12'), 12); self.assertIsNone(plan_id('taptap-1-device'))

    def test_import_takes_price_free_and_validity_from_the_member_plan(self):
        s = self.summary()
        p1 = _profile_to_plan(self.r, {'name': self.monthly.router_profile_name, 'shared-users': '7', 'rate-limit': '2M/2M'}, s, timezone.now())
        p2 = _profile_to_plan(self.r, {'name': self.free.router_profile_name, 'shared-users': '11', 'session-timeout': '0s'}, s, timezone.now())
        self.assertEqual((p1.price, p1.is_free, p1.price_source, p1.duration_minutes, p1.duration_unit), (Decimal('1000'), False, 'member', 43200, 'months'))
        self.assertEqual((p2.price, p2.is_free, p2.duration_unit), (Decimal('0'), True, 'unlimited'))
        self.assertNotIn(p1.name, s.get('plans_without_price', []))
        # The rows from before the fix ("No price · 1 day") are corrected at the next sync…
        VoucherPlan.objects.filter(pk=p1.pk).update(price=0, price_source='', duration_minutes=1440, duration_unit='days')
        _profile_to_plan(self.r, {'name': self.monthly.router_profile_name, 'shared-users': '7'}, self.summary(), timezone.now())
        p1.refresh_from_db(); self.assertEqual((p1.price, p1.duration_minutes), (Decimal('1000'), 43200))
        # …but a price you typed yourself on the Plans page is kept.
        VoucherPlan.objects.filter(pk=p1.pk).update(price=1500, price_source='manual')
        _profile_to_plan(self.r, {'name': self.monthly.router_profile_name}, self.summary(), timezone.now())
        p1.refresh_from_db(); self.assertEqual(p1.price, Decimal('1500'))

    def test_plans_page_fixes_existing_rows_and_follows_edits(self):
        row = VoucherPlan.objects.create(business=self.b, name=self.monthly.router_profile_name, price=0, source='mikrotik',
                                         duration_minutes=1440, duration_unit='days', mikrotik_profile_name=self.monthly.router_profile_name)
        html = self.client.get(reverse('plans')).content.decode()
        row.refresh_from_db()
        self.assertEqual(row.price, Decimal('1000'))
        self.assertIn('Member plan', html); self.assertIn('Member Monthly 7', html); self.assertIn('from the member plan', html)
        # Editing the member plan updates its row straight away.
        from .members import save_member_plan
        save_member_plan(self.b, {'name': 'Member Monthly 7', 'price': '1250', 'max_devices': 7, 'speed_limit': '2M/2M',
                                  'duration_unit': 'months', 'duration_value': 1}, plan=self.monthly)
        row.refresh_from_db(); self.assertEqual(row.price, Decimal('1250'))
        self.assertEqual(sync_business(self.b), 0)          # nothing left to fix

    def test_router_gets_the_price_on_the_profile(self):
        member = Voucher.objects.create(business=self.b, router=self.r, code='anna', plan_name=self.monthly.name, price=1000,
                                        login_type='member', router_profile=self.monthly.router_profile_name,
                                        duration_minutes=43200, max_devices=7)
        MemberPlanAssignment.objects.create(member=member, plan=self.monthly)
        from .member_router_alignment import desired
        wanted = desired(member)
        self.assertEqual(wanted['comment'], 'TapTap member plan · Member Monthly 7 · price=1000')
        # Direct API: the comment is written with the profile.
        from .mikrotik import MikroTikService
        svc = MikroTikService.__new__(MikroTikService); svc.router = self.r
        prof = mock.MagicMock(); prof.get.return_value = [{'id': '*9'}]
        svc.resource = mock.MagicMock(return_value=prof)
        svc.ensure_hotspot_profile(wanted['profile'], 7, '2M/2M', comment=wanted['comment'])
        self.assertEqual(prof.set.call_args.kwargs['comment'], 'TapTap member plan · Member Monthly 7 · price=1000')
        # TapTap Link: both scripts that create the profile also set the comment.
        from .agent import command_body
        from .models import AgentCommand
        for kind in ('hotspot_users', 'hotspot_users_limit'):
            cmd = AgentCommand(router=self.r, kind=kind, params={
                'users': [{'n': 'anna', 'prof': wanted['profile'], 'lim': '30d'}],
                'profiles': [{'name': wanted['profile'], 'shared': 7, 'rate': '2M/2M', 'comment': wanted['comment']}]})
            self.assertIn('comment="TapTap member plan · Member Monthly 7 · price=1000"', command_body(cmd), kind)
        # A voucher plan's profile is untouched (no comment key → no comment line).
        cmd = AgentCommand(router=self.r, kind='hotspot_users', params={'users': [], 'profiles': [{'name': '1-day', 'shared': 1}]})
        self.assertNotIn('comment=', command_body(cmd))
