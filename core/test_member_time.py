"""Members: the router's time equals the plan's, and changing a plan keeps the member's clock.

"If someone already used 4 days and their plan changes to 1 month, those 4 days count in the 1 month plan."

Run:  DB_ENGINE=sqlite python manage.py test core.test_member_time
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import member_time as mt
from . import members as mem
from .durations import parse_routeros, router_limit
from .models import Business, Router, RouterHotspotUser, Voucher
from .models_member_plans import MemberPlan

DAY, WEEK, MONTH = 1440, 10080, 43200


@override_settings(AUTH_EMAIL_OTP=False)
class MemberTimeTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.owner = User.objects.create_user('o@x.com', 'o@x.com', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='B', owner_name='O', phone='1', trial_ends_at=self.now + timedelta(days=7))
        self.r = Router.objects.create(business=self.biz, name='hAP', ip_address='10.0.0.1', username='u', password='p', status='Online')
        self.weekly = MemberPlan.objects.create(business=self.biz, name='Weekly', price=300, duration_minutes=WEEK, max_devices=2)
        self.monthly = MemberPlan.objects.create(business=self.biz, name='Member Monthly 7', price=1000, duration_minutes=MONTH, max_devices=7)
        self.client.force_login(self.owner)

    def member(self, code='jimmy', used_days=4, duration=WEEK, expires_days=None, **kw):
        used = self.now - timedelta(days=used_days) if used_days is not None else None
        exp = (used + timedelta(days=expires_days)) if (used and expires_days) else ((used + timedelta(minutes=duration)) if used and duration else None)
        return Voucher.objects.create(business=self.biz, router=self.r, code=code, login_type='member', plan_name='Weekly',
                                      duration_minutes=duration, used_at=used, expires_at=exp, **kw)

    # ── the rule ──
    def test_four_days_used_count_in_the_new_month(self):
        v = self.member()
        u = mt.rebase(v, MONTH)
        self.assertEqual(u['expires_at'], v.used_at + timedelta(minutes=MONTH))     # not now + 1 month
        self.assertAlmostEqual((u['expires_at'] - self.now).days, 26, delta=1)       # 30 − 4 days left

    def test_renewals_already_paid_for_are_kept(self):
        v = self.member(expires_days=14)                                             # weekly, renewed once
        self.assertEqual(mt.rebase(v, MONTH)['expires_at'], v.used_at + timedelta(minutes=MONTH + WEEK))

    def test_not_started_gets_the_whole_plan_from_first_login(self):
        v = self.member(used_days=None)
        self.assertEqual(mt.rebase(v, MONTH), {'duration_minutes': MONTH, 'expires_at': None})

    def test_from_unlimited_the_clock_starts_now(self):
        v = self.member(used_days=120, duration=0)                                   # months on a free plan
        u = mt.rebase(v, MONTH)
        self.assertAlmostEqual((u['expires_at'] - self.now).total_seconds(), MONTH * 60, delta=5)

    def test_to_unlimited_and_shorter_plans(self):
        v = self.member()
        self.assertEqual(mt.rebase(v, 0), {'duration_minutes': 0, 'expires_at': None})
        short = mt.rebase(v, DAY)                                                     # 4 days used, 1-day plan
        self.assertLess(short['expires_at'], self.now)
        self.assertIn('time ended', mt.describe_change(v, short))

    # ── the router has the same time ──
    def test_router_limit_is_the_whole_plan_period(self):
        v = self.member()
        v.__dict__.update(mt.rebase(v, MONTH))
        self.assertEqual(parse_routeros(mt.expected_limit(v)), MONTH)                # 30 days, used time inside it
        self.assertEqual(parse_routeros(router_limit(v)), MONTH)                     # what TapTap actually sends
        self.assertTrue(mt.router_matches(v, '4w2d'))
        self.assertFalse(mt.router_matches(v, ''))                                    # "No time limit" on the router

    # ── where plans change ──
    def test_assigning_a_new_plan_keeps_the_clock(self):
        v = self.member()
        mem.assign_member_plan(v, self.monthly, user=self.owner)
        v.refresh_from_db()
        self.assertEqual((v.duration_minutes, v.expires_at), (MONTH, v.used_at + timedelta(minutes=MONTH)))
        from .models import VoucherEvent
        notes = [str((e.detail or {}).get('text', '')) for e in VoucherEvent.objects.filter(voucher=v, event='note')]
        self.assertTrue(any('time already used counts' in n for n in notes), notes)

    def test_editing_a_plans_length_rebases_its_members_and_updates_routers(self):
        v = self.member()
        mem.assign_member_plan(v, self.weekly, user=self.owner)
        with mock.patch('core.views_agents.push_one', return_value=True) as push, self.captureOnCommitCallbacks(execute=True):
            mem.save_member_plan(self.biz, {'name': 'Weekly', 'price': '300', 'max_devices': 2, 'duration_value': 1, 'duration_unit': 'months'},
                                 plan=self.weekly, user=self.owner)
        v.refresh_from_db()
        self.assertEqual(v.expires_at, v.used_at + timedelta(minutes=MONTH))
        self.assertEqual([c.args[0].pk for c in push.call_args_list], [v.pk])

    # ── the members page ──
    def test_page_flags_a_router_without_the_plan_time_and_fixes_it(self):
        v = self.member()
        mem.assign_member_plan(v, self.monthly, user=self.owner)
        RouterHotspotUser.objects.create(business=self.biz, router=self.r, username='jimmy', limit_uptime='', profile=self.monthly.router_profile_name,
                                         last_seen_at=self.now)
        r = self.client.get(reverse('members'))
        self.assertContains(r, 'No time limit · plan says')
        self.assertContains(r, 'Fix all on routers (1 differs from the plan)')
        with mock.patch('core.views_members.push_one', return_value=True) as push:
            self.client.post(reverse('member_fix_all'), {'next': reverse('members')})
        self.assertEqual([c.args[0].code for c in push.call_args_list], ['jimmy'])

    def test_matching_router_is_not_flagged(self):
        v = self.member()
        mem.assign_member_plan(v, self.monthly, user=self.owner)
        v.refresh_from_db()
        RouterHotspotUser.objects.create(business=self.biz, router=self.r, username='jimmy', limit_uptime=router_limit(v),
                                         profile=self.monthly.router_profile_name, last_seen_at=self.now)
        self.assertNotContains(self.client.get(reverse('members')), 'Fix all on routers')
