"""Voucher rollback: back to full time while less than 50% is used — Owner and Admin only.

Run:  DB_ENGINE=sqlite python manage.py test core.test_voucher_rollback
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import voucher_rollback as vr
from .models import Business, Voucher, VoucherEvent
from .models_team import TeamMember

HTML = {'HTTP_ACCEPT': 'text/html'}
T0 = timezone.now().replace(microsecond=0) - timedelta(days=1)


def at(when):
    return mock.patch('django.utils.timezone.now', return_value=when)


@override_settings(AUTH_EMAIL_OTP=False)
class RollbackTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('rb@example.com', 'rb@example.com', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='B', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        # 24 hours from first login at T0
        self.v = Voucher.objects.create(business=self.biz, code='RB1', plan_name='Day', duration_minutes=1440,
                                        used_at=T0, expires_at=T0 + timedelta(hours=24))

    def member(self, role, extra=()):
        u = User.objects.create_user(f'{role}@rb.com', f'{role}@rb.com', 'pw')
        TeamMember.objects.create(business=self.biz, user=u, role=role, extra_permissions=list(extra))
        return u

    def post(self, reason='Outage on the router'):
        return self.client.post(reverse('voucher_rollback', args=[self.v.pk]), {'reason': reason}, **HTML)

    def fresh(self):
        return Voucher.objects.get(pk=self.v.pk)

    # ── the 50% rule ──
    def test_rule_less_than_half(self):
        self.assertTrue(vr.check(self.v, T0 + timedelta(hours=6))['allowed'])            # 25%
        self.assertTrue(vr.check(self.v, T0 + timedelta(hours=11, minutes=59, seconds=59))['allowed'])
        self.assertFalse(vr.check(self.v, T0 + timedelta(hours=12))['allowed'])           # exactly 50%
        self.assertFalse(vr.check(self.v, T0 + timedelta(hours=18))['allowed'])           # 75%
        self.assertEqual(vr.check(self.v, T0)['deadline'], T0 + timedelta(hours=12))

    def test_not_possible_when_not_started_frozen_expired_member_or_disabled(self):
        now = T0 + timedelta(hours=1)
        cases = [dict(used_at=None, expires_at=None), dict(frozen_at=now, frozen_left=3600),
                 dict(status='expired'), dict(login_type='member'), dict(status='disabled'),
                 dict(duration_minutes=0, expires_at=None)]
        for change in cases:
            v = Voucher.objects.get(pk=self.v.pk)
            for k, val in change.items():
                setattr(v, k, val)
            self.assertFalse(vr.check(v, now)['allowed'], change)

    # ── the action ──
    def test_owner_rolls_back_to_full_time(self):
        now = T0 + timedelta(hours=6)
        self.client.force_login(self.owner)
        with at(now):
            self.post()
        v = self.fresh()
        self.assertEqual(v.rolled_back_at, now)
        self.assertEqual(v.expires_at, now + timedelta(hours=24))
        self.assertEqual(v.used_at, T0)                                   # first login is never changed
        ev = VoucherEvent.objects.get(voucher=v, event='rolled_back')
        self.assertEqual((ev.user, ev.reason, ev.detail['used_percent']), (self.owner, 'Outage on the router', 25))
        # the new period starts at the rollback: 0% used, rollback open again until half of it
        self.assertEqual(vr.check(v, now)['pct'], 0)
        self.assertEqual(vr.check(v, now)['deadline'], now + timedelta(hours=12))

    def test_admin_can_roll_back(self):
        self.client.force_login(self.member('admin'))
        with at(T0 + timedelta(hours=2)):
            self.post()
        self.assertIsNotNone(self.fresh().rolled_back_at)

    def test_other_roles_cannot_even_as_extra(self):
        for u in (self.member('voucher_support'), self.member('voucher_creator', extra=['vouchers.rollback']),
                  self.member('finance')):
            self.client.force_login(u)
            with at(T0 + timedelta(hours=2)):
                self.post()
            self.assertIsNone(self.fresh().rolled_back_at, u.username)

    def test_refused_at_half_or_more(self):
        self.client.force_login(self.owner)
        with at(T0 + timedelta(hours=12)):
            r = self.client.post(reverse('voucher_rollback', args=[self.v.pk]), {'reason': 'x'}, follow=True, **HTML)
        self.assertIsNone(self.fresh().rolled_back_at)
        self.assertContains(r, 'cannot be rolled back')

    def test_reason_required(self):
        self.client.force_login(self.owner)
        with at(T0 + timedelta(hours=1)):
            self.post(reason='  ')
        self.assertIsNone(self.fresh().rolled_back_at)

    def test_get_not_allowed(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse('voucher_rollback', args=[self.v.pk])).status_code, 405)

    def test_other_business_voucher_not_found(self):
        other = User.objects.create_user('x@example.com', 'x@example.com', 'pw')
        Business.objects.create(user=other, business_name='X', owner_name='X', phone='2',
                                trial_ends_at=timezone.now() + timedelta(days=30))
        self.client.force_login(other)
        with at(T0 + timedelta(hours=1)):
            self.post()
        self.assertIsNone(self.fresh().rolled_back_at)

    def test_router_gets_full_period_on_top_of_used_uptime(self):
        with at(T0 + timedelta(hours=3)), \
                mock.patch('core.voucher_history._router_apply', return_value=('Direct API', 'Applied', True)) as ra:
            vr.rollback(self.v, self.owner, 'outage')
        _, kwargs = ra.call_args
        self.assertEqual((ra.call_args.args[1], kwargs['minutes'], kwargs['total']), ('enable', 1440, False))

    # ── the page ──
    def test_detail_page_shows_rollback_to_owner_only(self):
        url = reverse('voucher_detail', args=[self.v.pk])
        self.client.force_login(self.owner)
        with at(T0 + timedelta(hours=6)):
            self.assertContains(self.client.get(url, **HTML), 'id="rollbackModal"')
        with at(T0 + timedelta(hours=13)):
            r = self.client.get(url, **HTML)
        self.assertNotContains(r, 'id="rollbackModal"')
        self.assertContains(r, 'Rollback is no longer possible')
        self.client.force_login(self.member('voucher_support'))
        with at(T0 + timedelta(hours=6)):
            r = self.client.get(url, **HTML)
        self.assertNotContains(r, 'rollbackModal')
        self.assertNotContains(r, 'Roll back to full time')
