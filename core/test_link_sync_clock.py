"""TapTap Link sync robustness + Control Center router clock.

Run:  DB_ENGINE=sqlite CELERY_EAGER=1 python manage.py test core.test_link_sync_clock
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import AgentCommand, Business, Router, RouterAgent, RouterSyncJob
from .test_link_install import link_text, routeros_balanced


def parsed_body(chunk):
    """The command text inside ':local ttRun [:parse "<here>"]' (must be isolated)."""
    assert ':local ttRun [:parse "' in chunk, chunk[:120]
    return link_text(chunk)


@override_settings(SITE_URL='https://taptap.test')
class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.u = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw12345678')
        self.biz = Business.objects.create(user=self.u, business_name='B', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=self.biz, name='hAP', status='Online', connection_mode='agent')
        self.agent = RouterAgent.objects.create(router=self.r, last_seen_at=timezone.now(), ros_version='7.15.3 (stable)')
        self.client.force_login(self.u)


class LinkBatchTests(Base):
    def test_each_command_is_isolated_so_a_syntax_error_cannot_stop_the_batch(self):
        from .agent import queue, wrap
        cmd = queue(self.r, 'hotspot_user_set', {'name': 'TT000001', 'disabled': True}, label='x')
        chunk = wrap(cmd, 'https://taptap.test', 'yes-without-crl')
        self.assertTrue(routeros_balanced(chunk))
        self.assertIn(':local ttRun [:parse ', chunk)
        inner = parsed_body(chunk)
        self.assertTrue(routeros_balanced(inner))
        self.assertIn('/ip hotspot user', inner)
        self.assertIn('/api/agent/v1/ack', chunk.split('$ttRun;', 1)[1])        # ack stays outside the parsed block

    def test_reboot_and_self_update_are_not_wrapped(self):
        from .agent import queue, wrap
        cmd = queue(self.r, 'reboot', {}, label='r')
        self.assertNotIn('[:parse', wrap(cmd, 'https://taptap.test', 'no'))

    def test_inventory_piece_uses_global_helper_and_escapes_filters(self):
        from .agent import queue, wrap
        from .agent_inventory import send_function
        self.assertTrue(send_function().startswith(':global ttSend do='))
        cmd = queue(self.r, 'inventory_piece', {'kind': 'share_lists'}, label='s')
        chunk = wrap(cmd, 'https://taptap.test', 'no')
        self.assertTrue(routeros_balanced(chunk))
        inner = parsed_body(chunk)
        self.assertTrue(inner.startswith(':global ttSend;'))
        self.assertTrue(routeros_balanced(inner))                                 # the quoted filter no longer breaks it
        self.assertIn('list~\\"^tt-share-\\"', inner)

    def test_expired_pieces_do_not_do_bookkeeping_inside_the_check_in(self):
        from .agent import build_response, queue
        job = RouterSyncJob.objects.create(business=self.biz, router=self.r, status='running')
        cmd = queue(self.r, 'inventory_piece', {'job_id': job.pk, 'kind': 'interfaces'}, label='p')
        AgentCommand.objects.filter(pk=cmd.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        with mock.patch('core.tasks.record_inventory_job.delay') as later:
            build_response(self.r, 'https://taptap.test')
        later.assert_called_once()
        self.assertEqual(later.call_args.args[:2], (self.r.pk, job.pk))


class LinkJobTests(Base):
    def job_with(self, n):
        from .agent import queue
        job = RouterSyncJob.objects.create(business=self.biz, router=self.r, status='running')
        cmds = [queue(self.r, 'inventory_piece', {'job_id': job.pk, 'kind': k}, label=k)
                for k in ['interfaces', 'routes', 'dns'][:n]]
        return job, cmds

    def test_pieces_are_found_by_job_without_scanning_old_samples(self):
        from .agent import queue
        from .agent_inventory import job_pieces
        job, cmds = self.job_with(3)
        for _ in range(20):
            queue(self.r, 'inventory_piece', {'kind': 'traffic_dns'}, label='sample', minutes=5)
        self.assertEqual({c.pk for c in job_pieces(self.r, job.pk)}, {c.pk for c in cmds})

    def test_acknowledged_but_unprocessed_upload_is_counted_so_the_job_finishes(self):
        from .agent_inventory import _record_piece
        job, cmds = self.job_with(2)
        a, b = cmds
        params = dict(a.params, received=True)
        AgentCommand.objects.filter(pk=a.pk).update(status='done', params=params, done_at=timezone.now())
        AgentCommand.objects.filter(pk=b.pk).update(status='done', done_at=timezone.now() - timedelta(minutes=10))
        _record_piece(self.r, job.pk, {})
        job.refresh_from_db()
        self.assertEqual(job.status, 'success')
        self.assertEqual(job.summary['inventory_skipped'], 1)

    def test_settle_imports_an_upload_the_worker_missed(self):
        from .agent_inventory import settle_stalled_jobs
        job, (cmd,) = self.job_with(1)
        AgentCommand.objects.filter(pk=cmd.pk).update(status='done', done_at=timezone.now())
        RouterSyncJob.objects.filter(pk=job.pk).update(updated_at=timezone.now() - timedelta(minutes=30))
        cache.set(f'tt:agent-inventory-rows:{cmd.pk}', [{'name': 'ether1', 'type': 'ether'}], 600)
        settle_stalled_jobs(self.r)
        cmd.refresh_from_db(); job.refresh_from_db()
        self.assertTrue(cmd.params.get('received'))
        self.assertEqual(job.status, 'success')

    def test_prune_removes_old_finished_commands_only(self):
        from .agent import queue
        from .agent_inventory import prune_commands
        old = queue(self.r, 'inventory_piece', {'kind': 'traffic_dns'}, label='old', minutes=5)
        live = queue(self.r, 'inventory_piece', {'kind': 'traffic_dns'}, label='live', minutes=5)
        recent = queue(self.r, 'ping', {}, label='recent')
        AgentCommand.objects.filter(pk__in=[old.pk, recent.pk]).update(status='done')
        AgentCommand.objects.filter(pk__in=[old.pk, live.pk]).update(created_at=timezone.now() - timedelta(days=5))
        self.assertEqual(prune_commands(), 1)
        self.assertEqual(set(AgentCommand.objects.values_list('pk', flat=True)), {live.pk, recent.pk})


class ClockMathTests(TestCase):
    def test_parse_router_dates_both_formats(self):
        from .router_clock import parse_router_datetime
        want = datetime(2026, 10, 15, 9, 5, 7)
        self.assertEqual(parse_router_datetime('2026-10-15', '09:05:07'), want)
        self.assertEqual(parse_router_datetime('oct/15/2026', '09:05:07'), want)
        self.assertIsNone(parse_router_datetime('garbage', '09:05:07'))

    def test_offsets(self):
        from .router_clock import parse_offset
        self.assertEqual(parse_offset('+01:00'), timedelta(hours=1))
        self.assertEqual(parse_offset('-05:30'), -timedelta(hours=5, minutes=30))
        self.assertEqual(parse_offset('3600'), timedelta(hours=1))
        self.assertIsNone(parse_offset('manual'))

    def test_drift_and_verdict(self):
        from .router_clock import build_reading
        at = datetime(2026, 10, 15, 12, 0, 0, tzinfo=dt_timezone.utc)
        ok = build_reading({'date': '2026-10-15', 'time': '12:00:20', 'tz': 'Africa/Banjul', 'ntp': 'true'}, 'x', at)
        self.assertEqual((ok['drift_seconds'], ok['verdict'], ok['ntp_enabled']), (20, 'ok', True))
        bad = build_reading({'date': 'jan/02/1970', 'time': '00:10:00', 'tz': 'Africa/Banjul'}, 'x', at)
        self.assertEqual(bad['verdict'], 'wrong')
        self.assertLess(bad['drift_seconds'], 0)
        paris = build_reading({'date': '2026-10-15', 'time': '14:00:00', 'tz': 'Europe/Paris'}, 'x', at)
        self.assertEqual(paris['drift_seconds'], 0)                                 # CEST = UTC+2 in October

    def test_validation(self):
        from .router_clock import validate_params
        for bad in ({'action': 'rm'}, {'action': 'set', 'tz': 'Nowhere/X'}, {'action': 'set', 'tz': '"; /system reset'},
                    {'action': 'set', 'manual': '1999-01-01T00:00'}, {'action': 'set', 'ntp': 'yes'}):
            with self.assertRaises(ValueError, msg=bad):
                validate_params(bad)
        validate_params({'action': 'set', 'tz': 'Africa/Banjul', 'manual': '2026-10-15T10:00', 'ntp': True})


class ClockLinkTests(Base):
    def test_set_command_is_built_fresh_when_sent_and_reads_back(self):
        from .agent import queue, wrap
        cmd = queue(self.r, 'clock', {'action': 'set', 'tz': 'Africa/Banjul', 'manual': '', 'ntp': True}, label='c')
        fixed = datetime(2026, 10, 15, 12, 0, 0, tzinfo=dt_timezone.utc)
        with mock.patch('django.utils.timezone.now', return_value=fixed):
            chunk = wrap(cmd, 'https://taptap.test', 'no')
        self.assertTrue(routeros_balanced(chunk))
        inner = parsed_body(chunk)
        self.assertTrue(routeros_balanced(inner))
        self.assertIn('time-zone-name="Africa/Banjul"', inner)
        self.assertIn('date=2026-10-15 time=12:00:01', inner)                       # ISO first on RouterOS 7.15
        self.assertIn('date=oct/15/2026 time=12:00:01', inner)                      # fallback
        self.assertIn('/api/agent/v1/clock?c=', inner)
        self.assertLess(inner.index('time-zone-name'), inner.index('date=2026'))   # zone before time

    def test_old_routeros_gets_legacy_date_first(self):
        from .agent import queue, wrap
        RouterAgent.objects.filter(pk=self.agent.pk).update(ros_version='6.49.10 (long-term)')
        cmd = queue(self.r, 'clock', {'action': 'set', 'manual': '2026-10-15T10:00:00'}, label='c')
        self.r.refresh_from_db()
        inner = parsed_body(wrap(AgentCommand.objects.select_related('router__agent').get(pk=cmd.pk), 'https://taptap.test', 'no'))
        self.assertLess(inner.index('date=oct/15/2026 time=10:00:00'), inner.index('date=2026-10-15'))

    def test_bad_clock_params_never_queue(self):
        from .agent import queue
        with self.assertRaises(ValueError):
            queue(self.r, 'clock', {'action': 'set', 'tz': 'x" ; /system reset'})

    def test_full_round_trip_through_the_pages(self):
        from . import agent as link
        resp = self.client.post(f'/routers/{self.r.pk}/clock/read/', '{}', content_type='application/json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()['clock']['pending'])
        cmd = AgentCommand.objects.get(kind='clock')
        body = 'date=jan/02/1970\ntime=00:03:10\ntz=Africa/Banjul\noffset=+00:00\nauto=false\nntp=false\nservers=\nstatus='
        r = self.client.post(f'/api/agent/v1/clock?c={cmd.pk}&n=wrong', body, content_type='text/plain')
        self.assertEqual(r.status_code, 403)
        r = self.client.post(f'/api/agent/v1/clock?c={cmd.pk}&n={link.nonce(cmd)}', body, content_type='text/plain')
        self.assertEqual(r.json()['verdict'], 'wrong')
        st = self.client.get(f'/routers/{self.r.pk}/clock/?zones=1').json()
        self.assertFalse(st['clock']['pending'])
        self.assertEqual(st['clock']['verdict'], 'wrong')
        self.assertFalse(st['clock']['ntp_enabled'])
        self.assertIn('Africa/Banjul', st['zones'])
        self.assertIn('/system clock set date=', st['manual_command'])
        resp = self.client.post(f'/routers/{self.r.pk}/clock/set/', '{"ntp": true}', content_type='application/json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(AgentCommand.objects.filter(kind='clock').count(), 2)

    def test_failed_command_shows_an_error(self):
        from .router_clock import read, status
        read(self.r, self.u)
        AgentCommand.objects.filter(kind='clock').update(status='expired', result='')
        self.assertIn('did not answer', status(self.r)['error'])

    def test_other_business_cannot_touch_the_clock(self):
        other = User.objects.create_user('z@x.gm', 'z@x.gm', 'pw12345678')
        Business.objects.create(user=other, business_name='Z', owner_name='Z', phone='2', trial_ends_at=timezone.now() + timedelta(days=9))
        self.client.force_login(other)
        self.assertEqual(self.client.post(f'/routers/{self.r.pk}/clock/set/', '{}', content_type='application/json').status_code, 404)


class ClockApiTests(Base):
    def test_direct_api_read_and_set(self):
        Router.objects.filter(pk=self.r.pk).update(connection_mode='api', ip_address='10.0.0.1', username='u', password='p')
        self.r.refresh_from_db()
        clock = {'date': '2026-10-15', 'time': '08:00:00', 'time-zone-name': 'Africa/Banjul', 'gmt-offset': '+00:00'}
        ntp = {'enabled': 'false', 'servers': ''}

        class Res:
            def __init__(self, row): self.row = row
            def get(self): return [dict(self.row)]
            def set(self, **kw): self.row.update(kw)

        class Svc:
            def __init__(self, router, timeout=None): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def resource(self, path): return Res(clock) if path == '/system/clock' else Res(ntp)
            def safe_get(self, path): return [dict(ntp)]
            def ros_version(self): return (7, 15, 3)

        from . import router_clock as rc
        with mock.patch('core.mikrotik.MikroTikService', Svc):
            first = rc.read(self.r)
            self.assertEqual(first['verdict'], 'wrong')
            fixed = rc.correct(self.r, tz='Africa/Banjul', ntp=True)
        self.assertEqual(ntp['enabled'], 'yes')
        self.assertIn(fixed['verdict'], ('ok',))
        self.assertFalse(fixed['pending'])
