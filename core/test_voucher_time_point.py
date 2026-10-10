"""Voucher time bar: end the time at a clicked point, or freeze the voucher there; device page → latest voucher.

Run:  DB_ENGINE=sqlite python manage.py test core.test_voucher_time_point
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Business, DeviceSignature, Voucher, VoucherCodeAlias, VoucherEvent
from .models_team import TeamMember
from .voucher_schedule import run_due


def ms(t):
    return str(int(t.timestamp() * 1000))


@override_settings(AUTH_EMAIL_OTP=False)
class TimePointTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('o', 'o@x.gm', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='TapTap K', owner_name='B', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        now = timezone.now()
        # 24-hour voucher started 6 hours ago: ends in 18 hours.
        self.v = Voucher.objects.create(business=self.b, code='ABC123', plan_name='24 HOURS', duration_minutes=1440,
                                        used_at=now - timedelta(hours=6), expires_at=now + timedelta(hours=18))
        self.url = reverse('voucher_time_point', args=[self.v.pk])
        self.client.force_login(self.owner)

    def post(self, **data):
        return self.client.post(self.url, data, follow=True)

    def test_page_has_the_clickable_bar(self):
        html = self.client.get(reverse('voucher_detail', args=[self.v.pk])).content.decode()
        self.assertIn('id="tbTrack"', html); self.assertIn('"url": "%s"' % self.url, html)

    def test_end_earlier_later_and_now(self):
        now = timezone.now()
        r = self.post(action='end', at=ms(now + timedelta(hours=2)), reason='closing early')
        self.v.refresh_from_db()
        self.assertAlmostEqual((self.v.expires_at - now).total_seconds(), 7200, delta=120)
        self.assertContains(r, 'switches it off at that moment')
        self.assertTrue(VoucherEvent.objects.filter(voucher=self.v, event='time_shortened').exists())
        # Later than the current end adds time through the normal extend action (router limit raised too).
        self.post(action='end', at=ms(now + timedelta(hours=5)))
        self.v.refresh_from_db()
        self.assertAlmostEqual((self.v.expires_at - now).total_seconds(), 5 * 3600, delta=120)
        self.assertTrue(VoucherEvent.objects.filter(voucher=self.v, event='extended').exists())
        # A point in the past means: end now. The expiry sweep switches it off at once.
        self.post(action='end', at=ms(now - timedelta(hours=1)))
        self.v.refresh_from_db()
        self.assertEqual(self.v.status, 'expired')

    def test_plan_freeze_runs_when_due_and_can_be_cancelled(self):
        now = timezone.now()
        r = self.post(action='freeze', at=ms(now + timedelta(hours=3)), reason='Shop closed tonight')
        self.v.refresh_from_db()
        self.assertIsNotNone(self.v.freeze_planned_at); self.assertIsNone(self.v.frozen_at)
        self.assertContains(r, 'will freeze at')
        html = self.client.get(reverse('voucher_detail', args=[self.v.pk])).content.decode()
        self.assertIn('Freezes at', html); self.assertIn('id="tbPlan"', html)
        self.assertEqual(run_due(now + timedelta(hours=1)), 0)            # not yet
        self.assertEqual(run_due(now + timedelta(hours=3, minutes=1)), 1)  # its moment came
        self.v.refresh_from_db()
        self.assertIsNotNone(self.v.frozen_at); self.assertIsNone(self.v.freeze_planned_at)
        self.assertIn('Shop closed tonight', self.v.freeze_reason)
        # Cancelling (on another voucher).
        w = Voucher.objects.create(business=self.b, code='XYZ9', plan_name='24 HOURS', used_at=now, expires_at=now + timedelta(days=1))
        self.client.post(reverse('voucher_time_point', args=[w.pk]), {'action': 'freeze', 'at': ms(now + timedelta(hours=2)), 'reason': 'x'})
        self.client.post(reverse('voucher_time_point', args=[w.pk]), {'action': 'cancel_freeze'})
        w.refresh_from_db(); self.assertIsNone(w.freeze_planned_at)
        self.assertTrue(VoucherEvent.objects.filter(voucher=w, event='freeze_plan_cancelled').exists())

    def test_rules(self):
        now = timezone.now()
        r = self.post(action='freeze', at=ms(now + timedelta(hours=2)), reason='')      # reason required
        self.assertContains(r, 'Give a reason'); self.v.refresh_from_db(); self.assertIsNone(self.v.freeze_planned_at)
        r = self.post(action='freeze', at=ms(now + timedelta(hours=30)), reason='x')    # after the end
        self.assertContains(r, 'after its time runs out')
        # A freeze planned after a new, earlier end is cancelled automatically.
        self.post(action='freeze', at=ms(now + timedelta(hours=10)), reason='x')
        self.post(action='end', at=ms(now + timedelta(hours=4)))
        self.v.refresh_from_db(); self.assertIsNone(self.v.freeze_planned_at)
        # Staff without voucher support rights cannot use it.
        u = User.objects.create_user('v', 'v@x.gm', 'pw12345678')
        TeamMember.objects.create(business=self.b, user=u, role='viewer')
        self.client.force_login(u)
        self.client.post(self.url, {'action': 'end', 'at': ms(now + timedelta(hours=1))})
        self.v.refresh_from_db()
        self.assertAlmostEqual((self.v.expires_at - now).total_seconds(), 4 * 3600, delta=120)

    def test_device_page_links_latest_voucher(self):
        now = timezone.now()
        old = Voucher.objects.create(business=self.b, code='NEW777', plan_name='1 DAY', used_at=now, expires_at=now + timedelta(hours=3))
        VoucherCodeAlias.objects.create(business=self.b, voucher=old, code='OLD777')     # the device saw the old code
        d = DeviceSignature.objects.create(business=self.b, fingerprint='f1', vouchers=['OLD777', 'ABC123', 'GONE1'])
        html = self.client.get(reverse('device_detail', args=[d.pk])).content.decode()
        self.assertIn('Latest voucher', html)
        self.assertIn(f'href="{reverse("voucher_detail", args=[old.pk])}" aria-label="Open latest voucher NEW777"', html)
        self.assertIn('was OLD777', html)
        self.assertIn(f'href="{reverse("voucher_detail", args=[self.v.pk])}">ABC123</a>', html)
        self.assertIn('<span class="font-monospace">GONE1</span>', html)
