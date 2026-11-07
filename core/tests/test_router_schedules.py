"""Scheduled router restarts / shutdowns (core/router_schedules.py): timetable, heads-up, quiet moment, follow-up."""
import json
from datetime import date, datetime, time, timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core import mobile_auth, router_schedules as rs
from core.agent import wrap
from core.models import AgentCommand, Business, Router, RouterAgent
from core.models_events import EventAlert
from core.models_router_schedule import RouterSchedule, RouterScheduleRun
from core.models_team import TeamMember


def at(d, h, m=0):
    return timezone.make_aware(datetime.combine(d, time(h, m)), timezone.get_current_timezone())


@override_settings(AUTH_EMAIL_OTP=False,
                   CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'rsched'}})
class RouterScheduleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456', first_name='Awa')
        self.biz = Business.objects.create(user=self.owner, business_name='Kunda Net', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.r = Router.objects.create(business=self.biz, name='Mast', connection_mode='agent', status='Online')
        self.agent = RouterAgent.objects.create(router=self.r, token_hash='e' * 64, enrolled_at=timezone.now(),
                                                last_seen_at=timezone.now(), uptime='5d03:00:00')

    def sched(self, **kw):
        data = {'action': 'restart', 'repeat': 'daily', 'at_time': '03:00', 'window_minutes': 30, 'warn_minutes': 10, 'email': True}
        data.update(kw)
        return rs.save(self.r, data, self.owner)

    def online(self, n):
        cache.set(f'tt:linkactive:{self.r.pk}', [{'user': f'U{i}'} for i in range(n)], 600)

    def seen(self, when):
        RouterAgent.objects.filter(pk=self.agent.pk).update(last_seen_at=when)

    # ── timetable ──
    def test_next_times(self):
        s = RouterSchedule(router=self.r, repeat='daily', at_time=time(3, 0))
        d = date(2026, 11, 5)
        self.assertEqual(rs.next_after(s, at(d, 2, 59)), at(d, 3))
        self.assertEqual(rs.next_after(s, at(d, 3)), at(d + timedelta(days=1), 3))
        s.repeat, s.weekdays = 'weekly', [6]                                 # Sundays
        self.assertEqual(rs.next_after(s, at(d, 12)), at(date(2026, 11, 8), 3))
        s.repeat, s.month_day = 'monthly', 31
        self.assertEqual(rs.next_after(s, at(date(2026, 11, 5), 12)), at(date(2026, 11, 30), 3))   # short month: last day
        s.repeat, s.run_on = 'once', d
        self.assertIsNone(rs.next_after(s, at(d, 4)))
        self.assertEqual(rs.describe(RouterSchedule(repeat='weekly', weekdays=[0, 1, 2, 3, 4], at_time=time(3, 0))), 'Weekdays at 03:00')

    def test_save_checks(self):
        with self.assertRaises(rs.ScheduleError):
            self.sched(at_time='25:00')
        with self.assertRaises(rs.ScheduleError):
            self.sched(repeat='weekly', weekdays=[])
        with self.assertRaises(rs.ScheduleError):
            self.sched(action='shutdown')                                    # must say they understand
        s = self.sched(action='shutdown', understood='1')
        self.assertEqual(s.action, 'shutdown')
        with self.assertRaises(rs.ScheduleError):
            self.sched(repeat='once', run_on='2020-01-01')

    def test_uptime(self):
        self.assertEqual(rs.uptime_seconds('1w2d03:04:05'), 7 * 86400 + 2 * 86400 + 3 * 3600 + 4 * 60 + 5)
        self.assertEqual(rs.uptime_seconds('3m20s'), 200)
        self.assertEqual(rs.uptime_seconds('00:05:12'), 312)
        self.assertIsNone(rs.uptime_seconds(''))

    # ── a run, start to end ──
    def test_heads_up_quiet_moment_restart_and_back(self):
        s = self.sched()
        due = s.next_at
        rs.process(s, due - timedelta(minutes=9))
        run = RouterScheduleRun.objects.get(schedule=s)
        self.assertEqual(run.status, 'warned')
        self.assertTrue(EventAlert.objects.filter(kind='router_schedule', title__contains='restarts at 03:00').exists())
        rs.process(s, due - timedelta(minutes=8))                            # no second heads-up
        self.assertEqual(EventAlert.objects.filter(kind='router_schedule').count(), 1)
        self.online(3)
        s.refresh_from_db()
        self.assertEqual(rs.process(s, due + timedelta(minutes=1)), 'waiting')
        self.assertFalse(AgentCommand.objects.filter(kind='reboot').exists())
        self.online(0)                                                       # nobody online: it goes now
        self.seen(timezone.now())
        self.assertEqual(rs.process(s, due + timedelta(minutes=5)), 'sent')
        cmd = AgentCommand.objects.get(kind='reboot', router=self.r)
        run.refresh_from_db()
        self.assertEqual(run.status, 'sent')
        self.assertTrue(rs.expected_down(self.r.pk))
        s.refresh_from_db()
        self.assertEqual(s.next_at, due + timedelta(days=1))                 # the next night
        # the router confirms, goes down, checks in again with a fresh uptime
        ran = run.ran_at
        AgentCommand.objects.filter(pk=cmd.pk).update(status='done', done_at=ran + timedelta(seconds=5))
        RouterAgent.objects.filter(pk=self.agent.pk).update(last_seen_at=ran + timedelta(seconds=95), uptime='00:01:10')
        now = ran + timedelta(seconds=100)
        from unittest import mock
        with mock.patch('django.utils.timezone.now', return_value=now):
            self.assertEqual(rs.follow(run, now), 'back')
        run.refresh_from_db()
        self.assertEqual(run.status, 'back')
        self.assertFalse(rs.expected_down(self.r.pk))
        self.assertTrue(EventAlert.objects.filter(title__contains='back online').exists())

    def test_window_end_goes_anyway(self):
        s = self.sched(window_minutes=20, warn_minutes=0)
        self.online(5)
        self.assertEqual(rs.process(s, s.next_at + timedelta(minutes=2)), 'waiting')
        s.refresh_from_db()
        self.assertEqual(rs.process(s, s.next_at + timedelta(minutes=20)), 'sent')
        self.assertEqual(RouterScheduleRun.objects.get(schedule=s).online_at_run, 5)

    def test_skip_next_only(self):
        s = self.sched(warn_minutes=0, window_minutes=0)
        due = s.next_at
        rs.skip_next(s, self.owner)
        s.refresh_from_db()
        self.assertEqual(rs.process(s, due + timedelta(seconds=30)), 'skipped')
        self.assertFalse(AgentCommand.objects.filter(kind='reboot').exists())
        s.refresh_from_db()
        self.assertEqual(s.next_at, due + timedelta(days=1))
        self.assertIsNone(s.skip_at)

    def test_offline_router_fails_and_tells(self):
        s = self.sched(warn_minutes=0, window_minutes=0)
        self.seen(timezone.now() - timedelta(hours=2))
        s = RouterSchedule.objects.select_related('router').get(pk=s.pk)
        self.assertEqual(rs.process(s, s.next_at), 'failed')
        self.assertTrue(EventAlert.objects.filter(level='danger', title__contains='did not happen').exists())

    def test_missed_when_taptap_was_down(self):
        s = self.sched(warn_minutes=0, window_minutes=10)
        self.assertEqual(rs.process(s, s.next_at + timedelta(minutes=30)), 'missed')
        s.refresh_from_db()
        self.assertTrue(s.next_at > timezone.now())

    def test_once_turns_itself_off(self):
        d = (timezone.localtime() + timedelta(days=1)).date()
        s = self.sched(repeat='once', run_on=d.isoformat(), window_minutes=0, warn_minutes=0)
        rs.process(s, s.next_at)
        s.refresh_from_db()
        self.assertFalse(s.active)
        self.assertIsNone(s.next_at)

    def test_shutdown_link_command_and_off_notice(self):
        s = self.sched(action='shutdown', understood='1', window_minutes=0, warn_minutes=0)
        RouterSchedule.objects.filter(pk=s.pk).update(next_at=timezone.now())       # due now (real clock)
        s = RouterSchedule.objects.select_related('router').get(pk=s.pk)
        self.assertEqual(rs.process(s, s.next_at), 'sent')
        cmd = AgentCommand.objects.get(kind='shutdown')
        self.assertIn('/system shutdown', wrap(cmd, 'https://x/ack', 'k'))
        run = RouterScheduleRun.objects.get(schedule=s)
        AgentCommand.objects.filter(pk=cmd.pk).update(status='done', done_at=run.ran_at)
        self.seen(run.ran_at - timedelta(minutes=3))                          # gone quiet (past the Link grace)
        self.assertEqual(rs.follow(run, run.ran_at + timedelta(minutes=2)), 'off')
        self.assertTrue(rs.expected_down(self.r.pk))

    def test_offline_alerts_stay_quiet_while_expected(self):
        from core import uptime_guard
        rs._mark_down(self.r, 'shutdown')
        cache.set(uptime_guard._k(self.r.pk, 'down'), {'since': (timezone.now() - timedelta(hours=1)).isoformat(), 'why': '', 'alerted': False}, 600)
        self.assertFalse(uptime_guard.still_down(self.r))

    def test_tick_runs_due_ones(self):
        s = self.sched(warn_minutes=0, window_minutes=0)
        RouterSchedule.objects.filter(pk=s.pk).update(next_at=timezone.now() - timedelta(seconds=10))
        out = rs.tick()
        self.assertEqual(out['due'], 1)
        self.assertTrue(AgentCommand.objects.filter(kind='reboot').exists())

    # ── website and app ──
    def test_website_page_and_save(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get('/routers/schedules/').status_code, 200)
        r = self.client.post('/routers/schedules/save/', {'router': self.r.pk, 'what': 'restart', 'repeat': 'weekly',
                                                          'weekdays': ['0', '3'], 'at_time': '02:30', 'window_minutes': '20',
                                                          'warn_minutes': '15', 'email': 'on'})
        self.assertEqual(r.status_code, 302)
        s = RouterSchedule.objects.get()
        self.assertEqual((s.weekdays, s.at_time, s.window_minutes), ([0, 3], time(2, 30), 20))
        page = self.client.get(f'/routers/schedules/?router={self.r.pk}')
        self.assertContains(page, 'Mon, Thu at 02:30')
        self.client.post('/routers/schedules/action/', {'id': s.pk, 'action': 'pause'})
        s.refresh_from_db()
        self.assertFalse(s.active)

    def test_viewer_cannot_open_website_page(self):
        u = User.objects.create_user('v@x.gm', 'v@x.gm', 'pw-123456')
        TeamMember.objects.create(business=self.biz, user=u, is_active=True, role='viewer')
        self.client.force_login(u)
        self.assertNotEqual(self.client.get('/routers/schedules/').status_code, 200)

    def test_app_api(self):
        _dev, token = mobile_auth.create_device(self.owner, name='Pixel', platform='android')
        h = {'HTTP_AUTHORIZATION': f'Bearer {token}'}
        r = self.client.post('/api/mobile/v1/schedules/', json.dumps({'action': 'save', 'router': self.r.pk, 'what': 'restart',
                                                                       'repeat': 'daily', 'at_time': '03:15'}),
                             content_type='application/json', **h)
        d = json.loads(r.content)
        self.assertTrue(d['ok'], d)
        sid = d['schedule']['id']
        got = json.loads(self.client.get('/api/mobile/v1/schedules/', **h).content)
        self.assertEqual(got['schedules'][0]['text'], 'Every day at 03:15')
        self.assertTrue(got['can_manage'])
        d = json.loads(self.client.post('/api/mobile/v1/schedules/', json.dumps({'action': 'skip', 'id': sid}),
                                        content_type='application/json', **h).content)
        self.assertTrue(d['ok'])
        self.assertTrue(d['schedule']['skipped'])
        bad = self.client.post('/api/mobile/v1/schedules/', json.dumps({'action': 'run_now', 'router': self.r.pk, 'what': 'restart',
                                                                         'confirm': 'nope'}), content_type='application/json', **h)
        self.assertEqual(bad.status_code, 400)
        ok = json.loads(self.client.post('/api/mobile/v1/schedules/', json.dumps({'action': 'run_now', 'router': self.r.pk,
                                                                                    'what': 'restart', 'confirm': 'mast'}),
                                         content_type='application/json', **h).content)
        self.assertTrue(ok['ok'], ok)
        self.assertTrue(AgentCommand.objects.filter(kind='reboot').exists())
