"""Member page: scheduled pause / unpause, password reset, access & security controls.

Run:  DB_ENGINE=sqlite python manage.py test core.test_member_support
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import member_support as ms
from .models import Business, Voucher, VoucherDeviceBinding, VoucherEvent
from .models_member_plans import MemberNotificationSettings, MemberPlan, MemberPlanAssignment
from .models_member_support import MemberSchedule

DAY = 1440


def at(when):
    """Move TapTap's clock (every timezone.now()) to ``when``."""
    return mock.patch('django.utils.timezone.now', return_value=when)


def run_at(when):
    with at(when), mock.patch('core.voucher_freeze._apply_on_routers', return_value=[(True, 'No router')]):
        return ms.run_due(when)


@override_settings(AUTH_EMAIL_OTP=False)
class Base(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@x.gm', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='Kairaba Net', owner_name='A', phone='2200000',
                                         trial_ends_at=timezone.now() + timedelta(days=30), is_unlimited=True)
        self.plan = MemberPlan.objects.create(business=self.b, name='Monthly', price=Decimal('1000'), duration_minutes=30 * DAY,
                                              duration_unit='months', max_devices=2)
        self.plan2 = MemberPlan.objects.create(business=self.b, name='Quarterly', price=Decimal('2700'), duration_minutes=90 * DAY,
                                               duration_unit='months', max_devices=3)
        now = timezone.now()
        # 30-day member on day 20: 10 days left
        self.m = Voucher.objects.create(business=self.b, code='jimmy', login_type='member', plan_name='Monthly', price=Decimal('1000'),
                                        duration_minutes=30 * DAY, max_devices=2, status='active', customer_name='Jimmy Sowe',
                                        customer_phone='+220 777 1234', used_at=now - timedelta(days=20), expires_at=now + timedelta(days=10))
        MemberPlanAssignment.objects.create(member=self.m, plan=self.plan, assigned_by=self.owner)
        MemberNotificationSettings.objects.create(member=self.m, email='jimmy@example.com')
        VoucherDeviceBinding.objects.create(business=self.b, voucher=self.m, slot_no=1, current_mac='AA:BB:CC:DD:EE:FF', label='Jimmy phone')
        self.client.force_login(self.owner)

    def post(self, action, **data):
        return self.client.post(reverse('member_support', args=[self.m.pk]), {'action': action, **data})

    def fresh(self):
        return Voucher.all_objects.get(pk=self.m.pk)


class ScheduleTests(Base):
    def test_pause_when_ten_days_left_keeps_ten_days_until_unpaused(self):
        now = timezone.now()
        pause, resume = ms.schedule_pause(self.m, days_left=3, reason='Travelling', user=self.owner)
        self.assertIsNone(resume)
        self.assertAlmostEqual((pause.run_at - (now + timedelta(days=7))).total_seconds(), 0, delta=5)
        self.assertEqual(run_at(now + timedelta(days=6)), 0)                          # not yet
        self.assertEqual(run_at(now + timedelta(days=7, minutes=1)), 1)
        m = self.fresh()
        self.assertIsNotNone(m.frozen_at); self.assertEqual(m.status, 'disabled')
        self.assertAlmostEqual(m.frozen_left, 3 * 86400, delta=120)               # three days kept
        pause.refresh_from_db(); self.assertEqual(pause.status, 'done')
        ev = VoucherEvent.objects.filter(voucher=self.m, event='frozen').first()
        self.assertEqual((ev.source, ev.reason), ('auto', 'Scheduled pause: Travelling'))
        # stays paused until someone unpauses, then the three days come back from that moment
        later = now + timedelta(days=60)
        self.assertEqual(run_at(later), 0)
        with at(later), mock.patch('core.voucher_freeze._apply_on_routers', return_value=[(True, 'No router')]):
            ms.unpause_now(self.fresh(), user=self.owner)
        m = self.fresh()
        self.assertIsNone(m.frozen_at); self.assertEqual(m.status, 'active')
        self.assertAlmostEqual((m.expires_at - later).total_seconds(), 3 * 86400, delta=180)   # 3 days from the unpause

    def test_days_left_pause_follows_a_renewal(self):
        pause, _ = ms.schedule_pause(self.m, days_left=3, reason='Keep days', user=self.owner)
        Voucher.objects.filter(pk=self.m.pk).update(expires_at=self.m.expires_at + timedelta(days=30))   # renewed
        ms.run_due()
        pause.refresh_from_db()
        self.assertAlmostEqual((pause.run_at - (self.m.expires_at + timedelta(days=27))).total_seconds(), 0, delta=5)
        self.assertEqual(pause.status, 'pending')

    def test_pause_with_unpause_date(self):
        now = timezone.now()
        pause, resume = ms.schedule_pause(self.m, when=now + timedelta(days=1), resume_at=now + timedelta(days=4), reason='Holiday')
        run_at(now + timedelta(days=1, minutes=1))
        self.assertIsNotNone(self.fresh().frozen_at)
        run_at(now + timedelta(days=4, minutes=1))
        m = self.fresh()
        self.assertIsNone(m.frozen_at)
        resume.refresh_from_db(); self.assertEqual(resume.status, 'done')

    def test_checks(self):
        now = timezone.now()
        for kwargs, msg in (({'days_left': 3}, 'reason'), ({'days_left': 12, 'reason': 'x'}, 'pause now instead'),
                            ({'when': now + timedelta(days=11), 'reason': 'x'}, 'before that pause'),
                            ({'when': now - timedelta(hours=1), 'reason': 'x'}, 'future'),
                            ({'when': now + timedelta(days=2), 'resume_at': now + timedelta(days=1), 'reason': 'x'}, 'after the pause'),
                            ({'days_left': 500, 'reason': 'x'}, 'between 1 and 365')):
            with self.assertRaisesRegex(ms.SupportError, msg):
                ms.schedule_pause(self.m, **kwargs)
        fresh_member = Voucher.objects.create(business=self.b, code='newbie', login_type='member', plan_name='Monthly', duration_minutes=30 * DAY)
        with self.assertRaisesRegex(ms.SupportError, 'has not started'):
            ms.schedule_pause(fresh_member, days_left=5, reason='x')

    def test_new_schedule_replaces_old_and_cancel(self):
        now = timezone.now()
        a, _ = ms.schedule_pause(self.m, when=now + timedelta(days=1), reason='first')
        b, _ = ms.schedule_pause(self.m, when=now + timedelta(days=2), reason='second')
        a.refresh_from_db(); self.assertEqual(a.status, 'cancelled')
        ms.cancel_schedule(self.m, b.pk, user=self.owner)
        b.refresh_from_db(); self.assertEqual(b.status, 'cancelled')
        self.assertEqual(run_at(now + timedelta(days=3)), 0)

    def test_skipped_when_time_already_ran_out(self):
        now = timezone.now()
        pause, resume = ms.schedule_pause(self.m, when=now + timedelta(days=1), resume_at=now + timedelta(days=2), reason='x')
        Voucher.objects.filter(pk=self.m.pk).update(expires_at=now + timedelta(hours=1))      # time was cut short
        run_at(now + timedelta(days=1, minutes=1))
        pause.refresh_from_db(); resume.refresh_from_db()
        self.assertEqual(pause.status, 'skipped'); self.assertIn('run out', pause.result)
        self.assertEqual(resume.status, 'cancelled')

    def test_two_workers_never_run_one_schedule_twice(self):
        now = timezone.now()
        pause, _ = ms.schedule_pause(self.m, when=now + timedelta(days=1), reason='x')
        MemberSchedule.objects.filter(pk=pause.pk).update(status='running')            # another worker took it
        self.assertEqual(run_at(now + timedelta(days=2)), 0)
        self.assertIsNone(self.fresh().frozen_at)

    def test_beat_task_runs_schedules(self):
        from .tasks import deliver_notifications
        with mock.patch('core.member_support.run_due') as rd, mock.patch('core.notify.deliver'), \
                mock.patch('core.member_notifications.send_due_member_reminders'):
            deliver_notifications()
        rd.assert_called_once()


class PageTests(Base):
    def test_page_shows_everything(self):
        html = self.client.get(reverse('member_detail', args=[self.m.pk])).content.decode()
        for text in ('Jimmy Sowe', 'jimmy', 'Monthly', 'Schedule pause', 'When days are left', 'Reset password', 'Send portal access link',
                     'Sign out of portal everywhere', 'Jimmy phone', 'Free all slots', 'Block account', 'Support notes', 'History',
                     'Billing &amp; payments', 'Renew 1 month &amp; receipt', 'Advanced'):
            self.assertIn(text, html, text)
        self.assertIn(reverse('member_detail', args=[self.m.pk]), self.client.get(reverse('members')).content.decode())

    def test_schedule_through_the_page(self):
        r = self.post('schedule', when_mode='days', days_left='4', reason='Travelling')
        self.assertRedirects(r, reverse('member_detail', args=[self.m.pk]) + '#pause', fetch_redirect_response=False)
        s = MemberSchedule.objects.get(member=self.m, status='pending')
        self.assertEqual((s.action, s.days_left), ('pause', 4))
        html = self.client.get(reverse('member_detail', args=[self.m.pk])).content.decode()
        self.assertIn('When 4 days are left', html)
        pause_at = timezone.localtime(timezone.now() + timedelta(days=2)).strftime('%Y-%m-%dT%H:%M')
        resume_at = timezone.localtime(timezone.now() + timedelta(days=5)).strftime('%Y-%m-%dT%H:%M')
        self.post('schedule', when_mode='date', pause_at=pause_at, resume_at=resume_at, reason='Holiday')
        self.assertEqual(MemberSchedule.objects.filter(member=self.m, status='pending').count(), 2)

    def test_pause_now_and_unpause_through_the_page(self):
        with mock.patch('core.voucher_freeze._apply_on_routers', return_value=[(True, 'ok')]):
            self.post('pause_now', reason='Asked to pause')
            self.assertIsNotNone(self.fresh().frozen_at)
            html = self.client.get(reverse('member_detail', args=[self.m.pk])).content.decode()
            self.assertIn('Paused since', html); self.assertIn('Schedule unpause', html)
            self.post('unpause')
        self.assertIsNone(self.fresh().frozen_at)

    @mock.patch('core.members._push_password', return_value=('TapTap only', 'No router assigned — changed in TapTap only', True))
    def test_password_reset_shown_once_and_emailed(self, _push):
        with mock.patch('core.member_notifications._send', return_value=1) as send:
            self.post('password', mode='random', email='1', reason='forgot')
        pw = self.fresh().password
        self.assertEqual(len(pw), 8); self.assertTrue(set(pw) <= set(ms.PASSWORD_ALPHABET))
        self.assertEqual(send.call_args[0][0], 'jimmy@example.com'); self.assertEqual(send.call_args[0][4]['password'], pw)
        first = self.client.get(reverse('member_detail', args=[self.m.pk])).content.decode()
        self.assertIn(pw, first); self.assertIn('shown once', first); self.assertIn('wa.me', first)
        self.assertNotIn(pw, self.client.get(reverse('member_detail', args=[self.m.pk])).content.decode())     # only once
        self.post('password', mode='custom', password='bad pw')
        self.assertEqual(self.fresh().password, pw)                                     # rejected: spaces
        self.post('password', mode='same')
        self.assertEqual(self.fresh().password, '')

    def test_access_controls(self):
        from .member_self_service import profile
        with mock.patch('core.member_notifications._send', return_value=1) as send:
            self.post('portal_link')
        self.assertEqual(send.call_args[0][0], 'jimmy@example.com')
        before = profile(self.m).auth_version
        self.post('portal_signout')
        self.assertEqual(profile(self.m).auth_version, before + 1)
        self.assertFalse(self.m.member_portal_links.filter(expires_at__gt=timezone.now(), used_at__isnull=True).exists())
        self.post('wifi_signout')
        self.assertTrue(VoucherEvent.objects.filter(voucher=self.m, event='note', reason='Signed out of the Wi-Fi').exists())
        with mock.patch('core.device_lock.release_all', return_value=['AA:BB:CC:DD:EE:FF']), mock.patch('core.device_lock.unlock_on_router'):
            self.post('devices', reason='new phone')
        self.assertTrue(VoucherEvent.objects.filter(voucher=self.m, event='mac_reset').exists())

    def test_block_unblock_details_note_plan(self):
        self.post('block')                                                               # needs a reason
        self.assertEqual(self.fresh().status, 'active')
        self.post('block', reason='Account shared')
        self.assertEqual(self.fresh().status, 'disabled')
        self.post('unblock')
        self.assertEqual(self.fresh().status, 'active')
        self.post('details', customer_name='Jimmy B. Sowe', customer_phone='+220 999', customer_email='new@example.com', note='VIP',
                  reminders_enabled='1', remind_2_days='1')
        m = self.fresh()
        self.assertEqual((m.customer_name, m.customer_phone, m.note), ('Jimmy B. Sowe', '+220 999', 'VIP'))
        pref = MemberNotificationSettings.objects.get(member=self.m)
        self.assertEqual((pref.email, pref.reminders_enabled, pref.remind_2_days, pref.remind_7_days), ('new@example.com', True, True, False))
        self.post('note', text='Called — slow at night')
        self.assertContains(self.client.get(reverse('member_detail', args=[self.m.pk])), 'Called — slow at night')
        self.post('plan', plan=str(self.plan2.pk))
        self.assertEqual(MemberPlanAssignment.objects.get(member=self.m).plan, self.plan2)
        self.assertEqual(self.fresh().max_devices, 3)

    def test_permissions_and_other_business(self):
        from .models_team import TeamMember
        viewer = User.objects.create_user('view', 'v@x.gm', 'pw12345678')
        TeamMember.objects.create(business=self.b, user=viewer, role='voucher_creator')
        self.client.force_login(viewer)
        r = self.post('pause_now', reason='x')
        self.assertNotEqual(r.status_code, 302 if self.fresh().frozen_at else 0)
        self.assertIsNone(self.fresh().frozen_at)
        other = User.objects.create_user('x', 'x@x.gm', 'pw12345678')
        Business.objects.create(user=other, business_name='X', owner_name='X', phone='1', trial_ends_at=timezone.now() + timedelta(days=9))
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse('member_detail', args=[self.m.pk])).status_code, 404)
        self.assertEqual(self.post('pause_now', reason='x').status_code, 404)
        voucher = Voucher.objects.create(business=self.b, code='VCH123', plan_name='Day')
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse('member_detail', args=[voucher.pk])).status_code, 404)   # vouchers have no member page
