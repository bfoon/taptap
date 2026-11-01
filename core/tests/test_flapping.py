"""A router that blips must not look like an internet line going down and up every few seconds, nor email each blip."""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core import agent as link, live, uptime_guard
from core.models import Business, Notification, Router, RouterAgent

T0 = timezone.now().replace(microsecond=0)


class Clock:
    """Move time forward in tests (django.utils.timezone.now everywhere)."""
    def __init__(self, start=T0):
        self.t = start
        self.p = mock.patch('django.utils.timezone.now', side_effect=lambda: self.t)

    def __enter__(self):
        self.p.start(); return self

    def __exit__(self, *a):
        self.p.stop()

    def tick(self, seconds):
        self.t = self.t + timedelta(seconds=seconds)


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('owner', password='x', email='owner@example.com')
        self.biz = Business.objects.create(user=self.user, business_name='B', owner_name='O', phone='1', email='owner@example.com',
                                           trial_ends_at=timezone.now() + timedelta(days=30))

    def notes(self, event=None):
        qs = Notification.objects.filter(business=self.biz)
        return list(qs.filter(event=event)) if event else list(qs)


# ─────────────────────────── Direct API / Tunnel routers (live loop) ───────────────────────────

class DirectRouter(Base):
    def setUp(self):
        super().setUp()
        self.router = Router.objects.create(business=self.biz, name='Shop', ip_address='10.0.0.1', username='a', password='p', status='Online')

    def down(self):
        with mock.patch('core.live.MikroTikService') as svc:
            svc.return_value.connect.side_effect = OSError('timed out')
            self.router.refresh_from_db()
            live.watch_router(self.router, force=True)
        self.router.refresh_from_db()

    def ok(self):
        svc = mock.MagicMock()
        svc.hotspot_users.return_value = []; svc.active_users.return_value = []
        with mock.patch('core.live.MikroTikService') as cls:
            cls.return_value.connect.return_value = svc
            self.router.refresh_from_db()
            live.watch_router(self.router, force=True)
        self.router.refresh_from_db()

    def test_one_slow_answer_is_not_an_outage(self):
        with Clock() as c:
            self.down(); c.tick(15)
            self.assertEqual(self.router.status, 'Online')
            self.ok()
        self.assertEqual(self.router.status, 'Online')
        self.assertEqual(self.notes(), [], 'a single failed check used to email “offline” and then “back online”')

    def test_offline_after_failures_in_a_row_email_only_after_minutes(self):
        with Clock() as c:
            for _ in range(3):
                self.down(); c.tick(15)
            self.assertEqual(self.router.status, 'Online', 'three failures within 30 s: still a blip')
            self.down(); c.tick(15)
            self.assertEqual(self.router.status, 'Offline', 'failing for 45 s: offline')
            self.assertEqual(self.notes('router_offline'), [], 'not emailed yet — it may be a blip')
            for _ in range(4):
                c.tick(60); self.down()
            self.assertEqual(len(self.notes('router_offline')), 1, 'down for 3+ minutes: one email')
            c.tick(60); self.down()
            self.assertEqual(len(self.notes('router_offline')), 1, 'not emailed again while still down')
            self.ok()
        self.assertEqual(self.router.status, 'Online')
        back = self.notes('router_online')
        self.assertEqual(len(back), 1)
        self.assertIn('after', back[0].body)

    def test_short_outage_shows_but_never_emails(self):
        with Clock() as c:
            for _ in range(4):
                self.down(); c.tick(15)
            self.assertEqual(self.router.status, 'Offline')
            self.ok()
        self.assertEqual(self.router.status, 'Online')
        self.assertEqual(self.notes(), [], 'a 1-minute drop is on the dashboard, not in the inbox')

    def test_one_failed_action_does_not_mark_offline(self):
        live.note_failure(self.router, OSError('scan timed out'))
        self.router.refresh_from_db()
        self.assertEqual(self.router.status, 'Online')
        self.assertIn('scan timed out', self.router.last_error)


# ─────────────────────────── flapping → one “unstable” email ───────────────────────────

class Flapping(Base):
    def setUp(self):
        super().setUp()
        self.router = Router.objects.create(business=self.biz, name='Shop', ip_address='10.0.0.1', status='Online')

    def cycle(self, c, down_for):
        uptime_guard.went_down(self.router, since=c.t)
        c.tick(down_for); uptime_guard.still_down(self.router)
        uptime_guard.came_back(self.router, via='TapTap Link')

    def test_three_drops_in_30_minutes_send_one_summary(self):
        with Clock() as c:
            for _ in range(5):
                self.cycle(c, 240); c.tick(60)
        unstable = self.notes('router_unstable')
        self.assertEqual(len(unstable), 1, 'one summary, not a pair of emails per drop')
        self.assertIn('dropped 3 times', unstable[0].body)
        self.assertLessEqual(len(self.notes('router_offline')), 2, 'per-drop emails stop once it is called unstable')
        self.assertTrue(uptime_guard.quiet(self.router.pk))

    def test_history_records_every_change(self):
        with Clock() as c:
            self.cycle(c, 30)
        events = [e['event'] for e in uptime_guard.history(self.router.pk)]
        self.assertEqual(events, ['down', 'up'])


# ─────────────────────────── TapTap Link routers ───────────────────────────

class LinkRouters(Base):
    def make(self, name, seen_ago=5, status='Online'):
        r = Router.objects.create(business=self.biz, name=name, ip_address='', connection_mode='agent', status=status)
        RouterAgent.objects.create(router=r, token_hash=(name * 64)[:64], last_seen_at=timezone.now() - timedelta(seconds=seen_ago),
                                   enrolled_at=timezone.now(), poll_seconds=10, script_version=9)
        return r

    def test_silent_router_offline_email_after_minutes(self):
        quiet = self.make('Quiet')
        [self.make(f'Ok{i}') for i in range(3)]
        RouterAgent.objects.filter(router=quiet).update(last_seen_at=T0 - timedelta(seconds=100))
        with Clock() as c:
            link.check_offline_agents()
            quiet.refresh_from_db()
            self.assertEqual(quiet.status, 'Offline')
            self.assertEqual(self.notes('router_offline'), [], '100 s silent: shown offline, not emailed yet')
            RouterAgent.objects.exclude(router=quiet).update(last_seen_at=T0 + timedelta(seconds=180))
            c.tick(200); link.check_offline_agents()
        self.assertEqual(len(self.notes('router_offline')), 1, 'silent for 5 min: emailed once')

    def test_shared_isp_outage_is_still_reported(self):
        routers = [self.make(f'R{i}') for i in range(4)]
        RouterAgent.objects.filter(router__in=routers[:3]).update(last_seen_at=timezone.now() - timedelta(seconds=300))
        link.check_offline_agents()
        self.assertEqual(Router.objects.filter(status='Offline').count(), 3, '3 of 4 down together is real, not TapTap')

    def test_taptap_side_incident_marks_nobody_offline(self):
        routers = [self.make(f'R{i}') for i in range(4)]
        RouterAgent.objects.update(last_seen_at=timezone.now() - timedelta(seconds=300))     # TapTap was down / restarting
        link.check_offline_agents()
        self.assertEqual(Router.objects.filter(pk__in=[r.pk for r in routers], status='Offline').count(), 0)
        self.assertEqual(self.notes(), [])

    def poll(self, r, **data):
        a = RouterAgent.objects.get(router=r)
        base = {'id': r.name, 'ver': '7.15', 'up': '1d', 'cpu': '5', 'mf': '1', 'mt': '2', 'board': 'hAP', 'n': '0', 'sv': '9'}
        base.update(data)
        with mock.patch.object(link, 'build_response', return_value=''):
            link.handle_poll(a, base, '1.2.3.4', 'https://t.example')

    def test_back_online_email_only_after_an_offline_email(self):
        r = self.make('Shop', status='Offline')
        uptime_guard.went_down(r, since=timezone.now() - timedelta(seconds=120))          # 2 min: never emailed
        self.poll(r)
        r.refresh_from_db()
        self.assertEqual(r.status, 'Online')
        self.assertEqual(self.notes('router_online'), [], 'no “back online” for an outage nobody was told about')

    def test_back_online_after_a_real_outage(self):
        r = self.make('Shop', status='Offline')
        with Clock() as c:
            uptime_guard.went_down(r, since=c.t); c.tick(400); uptime_guard.still_down(r, via='TapTap Link')
            self.poll(r, lf='40', lw='route')
        back = self.notes('router_online')
        self.assertEqual(len(back), 1)
        self.assertIn('internet link that was down', back[0].body, 'the reason travels in the back-online email')
        self.assertEqual(self.notes('link_outage'), [], 'and no separate “could not reach TapTap” email for the same outage')

    def test_short_missed_checkins_are_not_emailed(self):
        r = self.make('Shop')
        self.poll(r, lf='3', lw='route')               # 30 s — what a busy hotspot used to report constantly
        self.assertEqual(self.notes('link_outage'), [])
        self.assertEqual(uptime_guard.history(r.pk)[-1]['event'], 'blip')

    def test_long_outage_noted_in_digest(self):
        r = self.make('Shop')
        self.poll(r, lf='30', lw='dns')                 # 5 min, never marked offline (e.g. it kept partial contact)
        out = self.notes('link_outage')
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].status, 'digest', 'an outage that is already over goes in the digest by default')

    def test_server_error_never_looks_like_an_outage(self):
        r = self.make('Shop', seen_ago=60)
        a = RouterAgent.objects.get(router=r)
        before = a.last_seen_at
        with mock.patch('core.agent.agent_for_token', return_value=a), mock.patch('core.agent.handle_poll', side_effect=RuntimeError('boom')):
            resp = self.client.post('/api/agent/v1/poll', 'id=Shop&sv=9', content_type='application/x-www-form-urlencoded',
                                    HTTP_AUTHORIZATION='Bearer x')
        self.assertEqual(resp.status_code, 200, 'an HTTP 500 is counted by the router as “cannot reach TapTap”')
        self.assertEqual(resp.content, b'')
        a.refresh_from_db()
        self.assertGreater(a.last_seen_at, before, 'the check-in still counts as a check-in')


# ─────────────────────────── the router script (v9) ───────────────────────────

class HeartbeatScript(TestCase):
    def setUp(self):
        self.s = link.agent_script('https://t.example', 'TOKEN', 'yes-without-crl')

    def test_version(self):
        self.assertTrue(self.s.startswith('# TapTap Link v9'))
        self.assertGreaterEqual(link.SCRIPT_VERSION, 9)

    def test_a_customer_logging_out_cannot_fail_the_checkin(self):
        self.assertIn(':do {\n        :local e [/ip hotspot active get $s]', self.s)
        self.assertIn(':do {\n        :local f [/interface get $i]', self.s)

    def test_waits_for_a_slow_answer(self):
        self.assertIn('duration=25s idle-timeout=25s', self.s)
        self.assertNotIn('idle-timeout=10s]', self.s)

    def test_route_watch_needs_a_change_to_hold(self):
        self.assertIn(':if ($wanNow = $ttWanNew and $ttWanCool = 0) do={', self.s)
        self.assertIn(':set ttWanCool 12', self.s)

    def test_script_is_well_formed(self):
        self.assertEqual(link._quote_aware_depth(self.s, 0), 0)
