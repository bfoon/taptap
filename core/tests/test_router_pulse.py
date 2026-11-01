"""The connection to every router is checked every few seconds, so the header never shows a stale "Live · 1m ago"
or a lingering "1 router offline" for a router that is fine."""
import socket
import threading
import time
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from core import live, router_pulse as rp
from core.models import Business, Router, RouterAgent, RouterSyncJob


def listening():
    """A local TCP port that accepts connections (stands in for a router's API)."""
    s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(8)
    return s, s.getsockname()[1]


def closed_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]; s.close()
    return port


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('owner', password='x')
        self.biz = Business.objects.create(user=self.user, business_name='B', owner_name='O', phone='1', live_sync=True,
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.srv, self.port = listening()

    def tearDown(self):
        self.srv.close()

    def router(self, name='Shop', port=None, status='Online', **kw):
        return Router.objects.create(business=self.biz, name=name, ip_address='127.0.0.1', api_port=port or self.port, status=status, **kw)


class Knock(Base):
    def test_target_reads_host_and_port(self):
        r = Router(ip_address='10.0.0.1', api_port=8728)
        self.assertEqual(rp.target(r), ('10.0.0.1', 8728))
        self.assertEqual(rp.target(Router(ip_address='shop.example.com:9000', api_port=8728)), ('shop.example.com', 9000))
        self.assertIsNone(rp.target(Router(ip_address='', connection_mode='agent')), 'Link routers: the check-ins are the pulse')

    def test_knock_opens_a_connection_without_logging_in(self):
        self.assertTrue(rp.knock('127.0.0.1', self.port, 2))
        with self.assertRaises(OSError):
            rp.knock('127.0.0.1', closed_port(), 2)


class Pulse(Base):
    def test_every_router_checked_in_one_go(self):
        ok, bad = self.router('Up'), self.router('Down', port=closed_port())
        res = rp.pulse()
        self.assertEqual(res, {ok.pk: True, bad.pk: False})
        self.assertTrue(cache.get(f'tt:pulse:ok:{ok.pk}'))
        self.assertEqual(cache.get(f'tt:watch:fails:{bad.pk}')['n'], 1, 'a failed knock counts toward offline…')
        bad.refresh_from_db()
        self.assertEqual(bad.status, 'Online', '…but one is never enough')

    def test_failed_knocks_mark_offline_after_40_seconds(self):
        bad = self.router('Down', port=closed_port())
        start = timezone.now()
        for i in range(5):
            with mock.patch('django.utils.timezone.now', return_value=start + timedelta(seconds=10 * i)):
                rp.pulse()
        bad.refresh_from_db()
        self.assertEqual(bad.status, 'Offline')

    def test_offline_router_that_answers_is_rechecked_at_once(self):
        r = self.router(status='Offline')
        cache.set(f'tt:watch:retry:{r.pk}', 1, 60)              # would otherwise wait up to a minute
        with mock.patch('core.tasks.watch_router_task.delay') as recheck:
            rp.pulse()
            rp.pulse()
        recheck.assert_called_once_with(r.pk)
        self.assertIsNone(cache.get(f'tt:watch:retry:{r.pk}'))

    def test_many_routers_checked_in_parallel(self):
        for i in range(12):
            self.router(f'R{i}', port=closed_port() if i % 3 == 0 else self.port)
        with mock.patch('core.router_pulse.knock', side_effect=lambda h, p: time.sleep(0.5) or True):
            t0 = time.monotonic(); rp.pulse(); took = time.monotonic() - t0
        self.assertLess(took, 2.5, f'12 routers × 0.5 s took {took:.1f} s: they must be checked side by side')

    def test_live_sync_off_or_no_access_not_pulsed(self):
        self.router()
        Business.objects.filter(pk=self.biz.pk).update(live_sync=False)
        self.assertEqual(rp.pulse(), {})


class Pill(Base):
    def status(self):
        from core.views_live import _status
        return _status(Business.objects.get(pk=self.biz.pk))

    def test_pill_shows_when_the_router_last_answered(self):
        r = self.router()
        Router.objects.filter(pk=r.pk).update(last_watch_at=timezone.now() - timedelta(minutes=2))   # full sync slow
        cache.set(f'tt:pulse:ok:{r.pk}', timezone.now().isoformat(), 60)                          # but it answers
        st = self.status()
        age = (timezone.now() - timezone.datetime.fromisoformat(st['last_watch_at'])).total_seconds()
        self.assertLess(age, 5, 'the pill used to say "Live · 2m ago" here')
        self.assertTrue(st['data_at'], 'the full sync time is still reported (live panel)')

    def test_link_router_check_ins_count(self):
        r = Router.objects.create(business=self.biz, name='Link', ip_address='', connection_mode='agent', status='Online')
        RouterAgent.objects.create(router=r, token_hash='t' * 64, last_seen_at=timezone.now() - timedelta(seconds=4))
        st = self.status()
        self.assertTrue(st['routers'][0]['seen'])
        self.assertLess((timezone.now() - timezone.datetime.fromisoformat(st['last_watch_at'])).total_seconds(), 10)

    def test_page_pulses_when_no_scheduler_runs(self):
        self.router()
        self.client.force_login(self.user)
        with mock.patch('core.views_live.beat_alive', return_value=False), mock.patch('core.views_live.watch_business', return_value=[]), \
                mock.patch('core.router_pulse.pulse') as p:
            self.client.get('/live/tick/')
            self.client.get('/live/tick/')
        self.assertEqual(p.call_count, 1, 'once per pulse period, not on every page tick')


class LivePass(Base):
    def test_a_slow_router_does_not_hold_up_the_others(self):
        fast, slow = self.router('Fast'), self.router('Slow')
        def check(rid, force):
            if rid == slow.pk:
                time.sleep(3)
            return {'router_id': rid}
        with mock.patch.object(live, 'connection', mock.MagicMock(vendor='postgresql')), mock.patch.object(live, '_watch_in_thread', side_effect=check):
            t0 = time.monotonic(); out = live._watch_parallel([fast.pk, slow.pk], deadline=1); took = time.monotonic() - t0
        self.assertLess(took, 2, 'the pass waited for the slow router')
        self.assertEqual(out, [{'router_id': fast.pk}])

    def test_housekeeping_runs_apart_and_never_twice_at_once(self):
        cache.set('tt:live:house', 1, 60)
        self.assertEqual(live.housekeeping(), 'previous housekeeping still running')

    def test_stuck_direct_sync_is_cleared(self):
        r = self.router()
        link = Router.objects.create(business=self.biz, name='L', ip_address='', connection_mode='agent')
        old = RouterSyncJob.objects.create(business=self.biz, router=r, status='running')
        fresh = RouterSyncJob.objects.create(business=self.biz, router=r, status='running')
        linkjob = RouterSyncJob.objects.create(business=self.biz, router=link, status='running')
        RouterSyncJob.objects.filter(pk__in=[old.pk, linkjob.pk]).update(updated_at=timezone.now() - timedelta(minutes=45))
        self.assertEqual(live.settle_stuck_syncs(), 1)
        self.assertEqual(RouterSyncJob.objects.get(pk=old.pk).status, 'failed')
        self.assertEqual(RouterSyncJob.objects.get(pk=fresh.pk).status, 'running')
        self.assertEqual(RouterSyncJob.objects.get(pk=linkjob.pk).status, 'running', 'Link syncs have their own clean-up')

    def test_schedule(self):
        from django.conf import settings
        self.assertEqual(settings.CELERY_BEAT_SCHEDULE['taptap-router-pulse']['task'], 'core.tasks.router_pulse_task')
        self.assertLessEqual(settings.CELERY_BEAT_SCHEDULE['taptap-router-pulse']['schedule'], 15)
        self.assertIn('taptap-live-housekeeping', settings.CELERY_BEAT_SCHEDULE)
        self.assertEqual(settings.CELERY_TASK_ROUTES['core.tasks.router_pulse_task'], {'queue': 'live'})
