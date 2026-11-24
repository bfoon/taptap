"""Sync control (core/sync_control.py): the owner decides when full syncs run, how hard they push a busy router,
when new voucher batches go, and whether apps & sites samples are taken — and a busy router is late, not offline."""
from datetime import time, timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from core import agent, sync_control as sc
from core.models import AgentCommand, Business, Router, RouterAgent, RouterSyncJob, Voucher, VoucherPlan
from core.models_sync import RouterSyncPolicy

CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'sync-control'}}
URL = 'https://taptap.example'


def at(hh, mm=0):
    """An aware datetime today at hh:mm local time."""
    return timezone.make_aware(timezone.localtime().replace(tzinfo=None, hour=hh, minute=mm, second=0, microsecond=0))


class QuietHours(SimpleTestCase):
    def test_windows_inside_a_day_and_across_midnight(self):
        p = RouterSyncPolicy(quiet_from=time(1), quiet_to=time(5))
        self.assertTrue(sc.in_quiet(p, at(3)))
        self.assertFalse(sc.in_quiet(p, at(5)))
        self.assertFalse(sc.in_quiet(p, at(23)))
        p = RouterSyncPolicy(quiet_from=time(22), quiet_to=time(4))
        self.assertTrue(sc.in_quiet(p, at(23, 30)))
        self.assertTrue(sc.in_quiet(p, at(2)))
        self.assertFalse(sc.in_quiet(p, at(12)))

    def test_the_quietest_four_hours_wrap_midnight(self):
        load = [50] * 24
        for h in (23, 0, 1, 2):
            load[h] = 1
        self.assertEqual(sc.quietest(load), 23)
        self.assertIsNone(sc.quietest([0] * 24))


@override_settings(CACHES=CACHE, AUTH_EMAIL_OTP=False, LINK_SYNC_MINUTES=30)
class Base(TestCase):
    link = True

    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('owner@x.gm', 'owner@x.gm', 'pw-123456')
        self.biz = Business.objects.create(user=self.owner, business_name='Kerr Sering', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        if self.link:
            self.router = Router.objects.create(business=self.biz, name='KerrSering', connection_mode='agent', status='Online')
            self.agent = RouterAgent.objects.create(router=self.router, token_hash='s' * 64, last_seen_at=timezone.now(),
                                                    enrolled_at=timezone.now(), script_version=9, ros_version='7.15.3 (stable)',
                                                    cpu_load=20)
        else:
            self.router = Router.objects.create(business=self.biz, name='KerrSering', ip_address='10.0.0.1', username='a', password='p')
        self.client.force_login(self.owner)

    def synced(self, minutes_ago, status='success', by=None):
        now = timezone.now()
        return RouterSyncJob.objects.create(business=self.biz, router=self.router, status=status, requested_by=by,
                                            finished_at=now - timedelta(minutes=minutes_ago))

    def save(self, **kw):
        data = {'full_mode': 'auto', 'full_every': 30, 'quiet_from': '01:00', 'quiet_to': '05:00', 'busy_guard': 'on',
                'busy_cpu': 75, 'pace': 'normal', 'voucher_mode': 'instant', 'samples_mode': 'auto'}
        data.update(kw)
        return sc.save(self.router, data, self.owner)[0]


class Decisions(Base):
    def test_defaults_keep_the_old_rhythm(self):
        self.assertEqual(sc.policy(self.router).full_mode, 'auto')
        self.synced(10)
        self.assertFalse(sc.decide(self.router).ok)
        RouterSyncJob.objects.all().delete()
        self.synced(31)
        self.assertTrue(sc.decide(self.router).ok)

    def test_manual_never_syncs_by_itself_but_the_first_check_in_does(self):
        self.save(full_mode='manual')
        self.synced(600)
        d = sc.decide(self.router)
        self.assertFalse(d.ok)
        self.assertIn('manual', d.why)
        self.assertTrue(sc.decide(self.router, first=True).ok)

    def test_quiet_hours_only(self):
        self.save(full_mode='quiet', quiet_from='01:00', quiet_to='05:00')
        RouterSyncJob.objects.create(business=self.biz, router=self.router, status='success', finished_at=at(0, 30))
        self.assertFalse(sc.decide(self.router, now=at(14)).ok)
        self.assertTrue(sc.decide(self.router, now=at(2)).ok)

    def test_a_busy_router_waits_and_an_overdue_one_runs_gently(self):
        self.save()
        RouterAgent.objects.filter(pk=self.agent.pk).update(cpu_load=92)
        self.router.refresh_from_db()
        self.synced(45)
        d = sc.decide(self.router)
        self.assertFalse(d.ok)
        self.assertIn('CPU 92', d.why)
        RouterSyncJob.objects.all().delete()
        self.synced(60 * 7)
        d = sc.decide(self.router)
        self.assertTrue(d.ok)
        self.assertTrue(d.gentle)

    def test_guard_off_ignores_the_cpu(self):
        self.save(busy_guard='')
        RouterAgent.objects.filter(pk=self.agent.pk).update(cpu_load=99)
        self.router.refresh_from_db()
        self.synced(45)
        self.assertTrue(sc.decide(self.router).ok)

    def test_a_retry_skips_the_interval_only(self):
        self.save()
        self.synced(5)
        self.synced(1, status='failed')
        self.assertTrue(sc.decide(self.router, retry=True).ok)
        self.save(full_mode='manual')
        self.assertFalse(sc.decide(self.router, retry=True).ok)

    def test_the_check_in_follows_the_policy(self):
        self.save(full_mode='manual')
        self.synced(600)
        with mock.patch('core.tasks.enqueue_router_sync') as enq, mock.patch('core.tasks.settle_link_router'):
            agent.auto_sync(self.router)
        enq.assert_not_called()
        cache.clear()
        self.save(full_mode='auto')
        with mock.patch('core.tasks.enqueue_router_sync', return_value=(None, True)) as enq, mock.patch('core.tasks.settle_link_router'):
            agent.auto_sync(self.router)
        enq.assert_called_once()


class ApiRouters(Base):
    link = False

    def test_default_is_manual_and_a_saved_automatic_policy_is_run_by_housekeeping(self):
        self.assertEqual(sc.policy(self.router).full_mode, 'manual')
        with mock.patch('core.tasks.enqueue_router_sync', return_value=(None, True)) as enq:
            sc.tick()
        enq.assert_not_called()
        self.save(full_mode='auto', full_every=60)
        self.synced(61)
        with mock.patch('core.tasks.enqueue_router_sync', return_value=(None, True)) as enq:
            self.assertEqual(sc.tick()['synced'], 1)
        enq.assert_called_once()


class GentlePace(Base):
    def pieces(self, n=3):
        job = RouterSyncJob.objects.create(business=self.biz, router=self.router, status='running')
        for k in ('hotspot_users', 'interfaces', 'ip_addresses')[:n]:
            agent.queue(self.router, 'inventory_piece', {'job_id': job.pk, 'kind': k}, label=f'Sync {k}', minutes=30)
        return job

    def sent(self):
        return list(AgentCommand.objects.filter(router=self.router, kind='inventory_piece', status='sent'))

    def test_normal_pace_sends_what_fits(self):
        self.pieces()
        agent.build_response(self.router, URL)
        self.assertGreater(len(self.sent()), 1)

    def test_gentle_sends_one_table_then_waits_until_it_arrived(self):
        self.save(pace='gentle')
        self.pieces()
        agent.build_response(self.router, URL)
        first = self.sent()
        self.assertEqual(len(first), 1)
        AgentCommand.objects.filter(router=self.router).update(sent_at=timezone.now() - timedelta(seconds=60))
        agent.build_response(self.router, URL)
        self.assertEqual(len(self.sent()), 1)                 # the router is still printing the first table
        p = dict(first[0].params)
        p['uploaded'] = True
        AgentCommand.objects.filter(pk=first[0].pk).update(params=p)
        agent.build_response(self.router, URL)
        self.assertEqual(len(self.sent()), 2)

    def test_a_busy_router_gets_the_gentle_pace_by_itself(self):
        RouterAgent.objects.filter(pk=self.agent.pk).update(cpu_load=90)
        self.router.refresh_from_db()
        self.pieces()
        agent.build_response(self.router, URL)
        self.assertEqual(len(self.sent()), 1)

    def test_printing_a_table_is_late_not_offline(self):
        before = self.agent.grace_seconds
        self.pieces(1)
        agent.build_response(self.router, URL)
        self.agent.refresh_from_db()
        self.assertGreaterEqual(self.agent.grace_seconds, sc.BUSY_GRACE)
        self.assertGreater(self.agent.grace_seconds, before)


class Samples(Base):
    def test_off_and_quiet_hours(self):
        self.assertTrue(sc.samples_allowed(self.router))
        self.save(samples_mode='off')
        self.assertFalse(sc.samples_allowed(self.router))
        with mock.patch.object(agent, 'queue') as q:
            agent.link_traffic_samples(self.router)
        q.assert_not_called()


class VoucherBatches(Base):
    def setUp(self):
        super().setUp()
        self.plan = VoucherPlan.objects.create(business=self.biz, name='Day', price=25, duration_minutes=1440, active=True)

    def make(self, n=3):
        from core.utils import code_format
        from core.voucher_make import make_batch
        return make_batch(self.biz, self.plan, n, code_format(8, 'mixed', business=self.biz), router=self.router, user=self.owner)

    def hotspot_cmds(self):
        return AgentCommand.objects.filter(router=self.router, kind='hotspot_users')

    def test_instant_is_unchanged(self):
        with self.captureOnCommitCallbacks(execute=True):
            batch, _notes = self.make()
        self.assertFalse(batch.vouchers.filter(mikrotik_sync_status=sc.HELD).exists())
        self.assertTrue(self.hotspot_cmds().exists())

    def test_manual_holds_the_batch_until_send(self):
        self.save(voucher_mode='manual')
        with self.captureOnCommitCallbacks(execute=True):
            batch, notes = self.make()
        self.assertEqual(batch.vouchers.filter(mikrotik_sync_status=sc.HELD).count(), 3)
        self.assertIn('the moment it is typed', ' '.join(t for _l, t in notes))
        agent.push_pending_vouchers(self.router)
        self.assertFalse(self.hotspot_cmds().exists())        # nothing goes by itself
        self.assertEqual(sc.held_count(self.router), 3)
        self.assertEqual(sc.release(self.router), 3)
        self.assertTrue(self.hotspot_cmds().exists())
        self.assertEqual(sc.held_count(self.router), 0)

    def test_quiet_mode_releases_in_quiet_hours(self):
        self.save(voucher_mode='quiet', quiet_from='01:00', quiet_to='05:00')
        with mock.patch('core.sync_control.in_quiet', return_value=False), self.captureOnCommitCallbacks(execute=True):
            self.make(2)
        self.assertEqual(sc.held_count(self.router), 2)
        with mock.patch('core.sync_control.in_quiet', return_value=True):
            self.assertEqual(sc.tick()['released'], 2)
        self.assertEqual(sc.held_count(self.router), 0)

    def test_a_customer_typing_a_held_voucher_still_gets_in(self):
        from core import voucher_fastlane as fl, voucher_ready as vr
        self.save(voucher_mode='manual')
        with self.captureOnCommitCallbacks(execute=True):
            batch, _ = self.make(1)
        v = batch.vouchers.get()
        self.assertEqual(fl.sync_now([v.pk]), {})               # a plain change waits with it
        need, _why, state = vr.needs_send(v)
        self.assertTrue(need)
        self.assertEqual(state, 'missing')
        fl.sync_now([v.pk], force=True)                          # the login guarantee sends it at once
        v.refresh_from_db()
        self.assertEqual(v.mikrotik_sync_status, 'Queued')

    def test_back_to_instant_sets_the_waiting_ones_free(self):
        self.save(voucher_mode='manual')
        with self.captureOnCommitCallbacks(execute=True):
            self.make(2)
        p, notes = sc.save(self.router, {'voucher_mode': 'instant', 'busy_guard': 'on'}, self.owner)
        self.assertEqual(sc.held_count(self.router), 0)
        self.assertIn('2 waiting', ' '.join(notes))

    def test_the_sync_doctor_leaves_held_vouchers_alone(self):
        from core import sync_doctor
        self.save(voucher_mode='manual')
        with self.captureOnCommitCallbacks(execute=True):
            self.make(2)
        Voucher.objects.filter(router=self.router).update(created_at=timezone.now() - timedelta(hours=1))
        report = sync_doctor.heal(self.router, None, mirror={}, read_at=timezone.now(), profiles=set())
        self.assertEqual(report['checked'], 0)
        self.assertEqual(sc.held_count(self.router), 2)


class Page(Base):
    def test_page_save_copy_and_actions(self):
        other = Router.objects.create(business=self.biz, name='Branch', connection_mode='agent', status='Online')
        r = self.client.get(f'/routers/{self.router.pk}/sync-control/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Full sync')
        self.assertContains(r, 'Branch')
        r = self.client.post(f'/routers/{self.router.pk}/sync-control/save/', {
            'full_mode': 'quiet', 'full_every': '125', 'quiet_from': '23:00', 'quiet_to': '04:00', 'busy_guard': 'on',
            'busy_cpu': '200', 'pace': 'gentle', 'voucher_mode': 'manual', 'samples_mode': 'quiet', 'all_routers': '1'})
        self.assertEqual(r.status_code, 302)
        p = RouterSyncPolicy.objects.get(router=self.router)
        self.assertEqual((p.full_mode, p.full_every, p.busy_cpu, p.pace), ('quiet', 120, 95, 'gentle'))
        self.assertEqual(p.quiet_from, time(23))
        self.assertEqual(RouterSyncPolicy.objects.get(router=other).voucher_mode, 'manual')
        r = self.client.get(f'/routers/{self.router.pk}/sync-control/status/')
        self.assertEqual(r.json()['cpu'], 20)
        with mock.patch('core.tasks.enqueue_router_sync', return_value=(mock.Mock(progress=0), True)) as enq:
            r = self.client.post(f'/routers/{self.router.pk}/sync-control/action/', {'action': 'sync'})
        enq.assert_called_once()

    def test_other_businesses_cannot_see_it(self):
        stranger = User.objects.create_user('s@x.gm', 's@x.gm', 'pw-123456')
        Business.objects.create(user=stranger, business_name='Other', owner_name='S', phone='2',
                                trial_ends_at=timezone.now() + timedelta(days=30))
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(f'/routers/{self.router.pk}/sync-control/').status_code, 404)


class GentleSyncStart(Base):
    def test_a_gentle_sync_gives_its_tables_the_whole_sync_time(self):
        from core import agent_inventory as inv
        self.save(pace='gentle')
        job = RouterSyncJob.objects.create(business=self.biz, router=self.router, status='queued')
        inv.start_agent_inventory_sync(job)
        cmd = AgentCommand.objects.filter(router=self.router, kind='inventory_piece').first()
        self.assertGreaterEqual((cmd.expires_at - cmd.created_at).total_seconds(), inv.MAX_SYNC_MINUTES * 60 - 5)


class MikroTikControlPage(Base):
    def test_every_router_card_shows_how_it_syncs_and_links_to_sync_control(self):
        api = Router.objects.create(business=self.biz, name='Branch', ip_address='10.0.0.2', username='a', password='p')
        self.save(full_mode='quiet', voucher_mode='manual')
        Voucher.objects.create(business=self.biz, router=self.router, code='WAIT1', plan_name='Day', duration_minutes=60,
                               source='taptap', mikrotik_sync_status=sc.HELD)
        r = self.client.get('/routers/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, f'/routers/{self.router.pk}/sync-control/')
        self.assertContains(r, f'/routers/{api.pk}/sync-control/')
        self.assertContains(r, 'Sync: Quiet hours · 01:00–05:00')
        self.assertContains(r, '1 waiting')
        self.assertContains(r, 'Sync: Manual')                 # a direct API router without a saved policy
        self.assertContains(r, 'sync-ctl-btn')

    def test_the_page_still_opens_if_sync_control_cannot_answer(self):
        with mock.patch('core.sync_control.card_summaries', side_effect=RuntimeError('boom')):
            r = self.client.get('/routers/')
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'Sync: ')
