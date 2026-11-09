"""'Connection unstable' a few hours after each restart, and scheduled restarts that never ran.

* a busy Link router is late, not offline: its own rhythm, and answers / uploads, keep it online
* apps & sites samples follow the router's load and are capped on the router
* a restart / shutdown goes alone at the next check-in, ahead of voucher changes, and waits for a late router
* schedules keep trying when the router does not answer; a Direct API router that drops the connection while it
  restarts is a success, not "did not answer"
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core import agent as link, link_rhythm as lr, router_schedules as rs
from core.models import AgentCommand, Business, Router, RouterAgent

CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'rhythm'}}
URL = 'https://t.example'


@override_settings(CACHES=CACHE, AUTH_EMAIL_OTP=False)
class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456')
        self.biz = Business.objects.create(user=self.owner, business_name='Kerr Sering', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.r = Router.objects.create(business=self.biz, name='KerrSering', connection_mode='agent', status='Online')
        self.a = RouterAgent.objects.create(router=self.r, token_hash='r' * 64, enrolled_at=timezone.now(),
                                            last_seen_at=timezone.now(), script_version=9)

    def seen(self, secs_ago):
        RouterAgent.objects.filter(pk=self.a.pk).update(last_seen_at=timezone.now() - timedelta(seconds=secs_ago))
        self.a.refresh_from_db()


class Rhythm(Base):
    def test_a_router_that_is_always_slow_learns_a_longer_allowance(self):
        self.assertEqual(self.a.grace_seconds, 90)
        for _ in range(20):
            lr.note_checkin(self.a, previous=timezone.now() - timedelta(seconds=55))
        self.assertEqual(self.a.grace_seconds, int(55 * 2.5))
        self.seen(120)
        self.assertTrue(self.a.online, 'late for its own rhythm is not offline')
        for _ in range(60):
            lr.note_checkin(self.a, previous=timezone.now() - timedelta(seconds=600))
        self.assertEqual(self.a.grace_seconds, lr.MAX_GRACE, 'never more than 5 minutes')
        lr.note_checkin(self.a, previous=timezone.now() - timedelta(seconds=2))      # turbo: ignored
        self.assertEqual(len(lr.gaps(self.a)), lr.KEEP)

    def test_an_answer_or_upload_keeps_it_online(self):
        self.seen(200)
        self.assertFalse(self.a.online)
        cmd = link.queue(self.r, 'ping', {}, label='x')
        AgentCommand.objects.filter(pk=cmd.pk).update(status='sent', sent_at=timezone.now())
        cmd.refresh_from_db()
        self.assertTrue(link.handle_ack(cmd.pk, link.nonce(cmd), 'ok'))
        self.a.refresh_from_db()
        self.assertTrue(self.a.online)
        link.check_offline_agents()
        self.r.refresh_from_db()
        self.assertEqual(self.r.status, 'Online')

    def test_a_restart_answer_does_not_count(self):
        self.seen(200)
        cmd = link.queue(self.r, 'reboot', {}, label='x')
        AgentCommand.objects.filter(pk=cmd.pk).update(status='sent', sent_at=timezone.now())
        cmd.refresh_from_db()
        link.handle_ack(cmd.pk, link.nonce(cmd), 'ok')
        self.a.refresh_from_db()
        self.assertFalse(self.a.online)

    def test_rhythm_on_the_link_page(self):
        for _ in range(12):
            lr.note_checkin(self.a, previous=timezone.now() - timedelta(seconds=11))
        self.client.force_login(self.owner)
        page = self.client.get(f'/routers/{self.r.pk}/link/')
        self.assertContains(page, 'Check-ins usually every')


class Samples(Base):
    def agent(self, cpu=10, users=20):
        RouterAgent.objects.filter(pk=self.a.pk).update(cpu_load=cpu, active_sessions=users)
        self.a.refresh_from_db()
        return self.a

    def test_samples_follow_the_load(self):
        self.assertEqual(lr.sample_interval(self.agent())[0], lr.SAMPLE_FAST)
        self.assertEqual(lr.sample_interval(self.agent(cpu=65))[0], lr.SAMPLE_BUSY)
        self.assertEqual(lr.sample_interval(self.agent(users=400))[0], lr.SAMPLE_BUSY)
        self.assertEqual(lr.sample_interval(self.agent(cpu=92))[0], lr.SAMPLE_SLOW)
        lr.note_sample(self.r.pk, 120)
        self.assertEqual(lr.sample_interval(self.agent())[0], lr.SAMPLE_SLOW)

    def test_sample_throttle_used_by_the_check_in(self):
        self.agent(cpu=92)
        with mock.patch('core.linkops.uses_link', return_value=True), mock.patch('core.link_rhythm.sample_interval', return_value=(900, 'x')) as si:
            link.link_traffic_samples(self.r)
        si.assert_called_once()
        self.assertTrue(AgentCommand.objects.filter(kind='inventory_piece').exists())
        self.assertIsNotNone(cache.ttl(f'tt:link:apps:{self.r.pk}') if hasattr(cache, 'ttl') else True)

    def test_samples_are_capped_on_the_router(self):
        from core.agent_inventory import CONN_SAMPLE, DNS_SAMPLE, inventory_piece_script
        for kind, n in (('traffic_conns', CONN_SAMPLE), ('traffic_dns', DNS_SAMPLE)):
            body = inventory_piece_script(SimpleNamespace(pk=1, params={'kind': kind}), URL, 'no', 'n')
            self.assertIn(f'- {n})', body)
            self.assertIn('get \\$x', body)
            self.assertNotIn('print as-value', body)
        self.assertIn('repl-bytes>20000', inventory_piece_script(SimpleNamespace(pk=1, params={'kind': 'traffic_conns'}), URL, 'no', 'n'))


class RestartGoesFirst(Base):
    def test_restart_travels_alone_ahead_of_vouchers(self):
        for i in range(5):
            link.queue(self.r, 'disconnect', {'user': f'V{i}'}, label='voucher change')
        reboot = link.queue(self.r, 'reboot', {}, label='Restart (schedule)', minutes=15)
        script = link.build_response(self.r, URL)
        sent = list(AgentCommand.objects.filter(router=self.r, status='sent'))
        self.assertEqual([c.pk for c in sent], [reboot.pk])
        self.assertIn('/system reboot', script)

    def test_late_link_router_still_gets_the_restart(self):
        self.seen(240)                        # busy: 4 min late, heard recently enough
        ok, msg = rs.execute(self.r, 'restart')
        self.assertTrue(ok, msg)
        self.assertIn('late', msg)
        cmd = AgentCommand.objects.get(router=self.r, kind='reboot')
        self.assertGreaterEqual((cmd.expires_at - timezone.now()).total_seconds(), 14 * 60)
        self.seen(3600)
        ok, msg = rs.execute(self.r, 'restart')
        self.assertFalse(ok)
        self.assertIn('offline', msg)


@override_settings(CACHES=CACHE, AUTH_EMAIL_OTP=False)
class DirectApiRestart(TestCase):
    def setUp(self):
        cache.clear()
        owner = User.objects.create_user('d@x.gm', 'd@x.gm', 'pw-123456')
        biz = Business.objects.create(user=owner, business_name='B', owner_name='O', phone='1', trial_ends_at=timezone.now() + timedelta(days=30))
        self.r = Router.objects.create(business=biz, name='R', ip_address='10.0.0.1', username='a', password='p')

    def svc(self, err):
        class Svc:
            def __init__(s, *a, **k):
                pass

            def connect(s):
                return s

            def resource(s, path):
                class R:
                    def call(r, *a, **k):
                        raise err
                return R()

            def close(s):
                raise OSError('Bad file descriptor')
        return Svc

    def test_connection_dropped_by_the_restart_is_success(self):
        for err in (ConnectionResetError('[Errno 104] Connection reset by peer'), BrokenPipeError('[Errno 32] Broken pipe'),
                    TimeoutError('timed out')):
            with mock.patch('core.mikrotik.MikroTikService', self.svc(err)):
                ok, msg = rs.execute(self.r, 'restart')
            self.assertTrue(ok, (err, msg))

    def test_refused_and_unreachable_are_failures(self):
        with mock.patch('core.mikrotik.MikroTikService', self.svc(ValueError('not enough permissions (9)'))):
            self.assertFalse(rs.execute(self.r, 'restart')[0])

        class Down:
            def __init__(s, *a, **k):
                pass

            def connect(s):
                raise OSError('No route to host')
        with mock.patch('core.mikrotik.MikroTikService', Down):
            ok, msg = rs.execute(self.r, 'restart')
        self.assertFalse(ok)
        self.assertIn('did not answer', msg)


@override_settings(CACHES=CACHE, AUTH_EMAIL_OTP=False)
class ScheduleKeepsTrying(Base):
    def test_retries_then_reports(self):
        s = rs.save(self.r, {'action': 'restart', 'repeat': 'daily', 'at_time': '03:00', 'window_minutes': 0, 'warn_minutes': 0,
                             'email': False}, self.owner)
        due = s.next_at
        with mock.patch('core.router_schedules.execute', return_value=(False, 'KerrSering did not answer: timed out')):
            self.assertEqual(rs.process(s, due + timedelta(seconds=30)), 'retry')
            s.refresh_from_db()
            self.assertEqual(s.next_at, due, 'not given up yet')
            self.assertEqual(rs.process(s, due + timedelta(minutes=1)), 'retry')
            run = s.runs.get(planned_at=due)
            self.assertIn('attempt 2', run.result)
        with mock.patch('core.router_schedules.execute', return_value=(True, 'Restart sent.')):
            self.assertEqual(rs.process(s, due + timedelta(minutes=2)), 'sent')
        s.refresh_from_db()
        self.assertGreater(s.next_at, due)

    def test_gives_up_after_the_retry_time(self):
        s = rs.save(self.r, {'action': 'restart', 'repeat': 'daily', 'at_time': '03:00', 'window_minutes': 0, 'warn_minutes': 0,
                             'email': False}, self.owner)
        due = s.next_at
        with mock.patch('core.router_schedules.execute', return_value=(False, 'KerrSering did not answer: timed out')):
            rs.process(s, due + timedelta(seconds=30))
            self.assertEqual(rs.process(s, due + rs.RETRY_FOR + timedelta(seconds=10)), 'failed')
        self.assertEqual(s.runs.get(planned_at=due).status, 'failed')
