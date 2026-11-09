"""Full sync heals every sync issue: after the router is read, each TapTap voucher is compared with it and what
differs is sent again (core/sync_doctor.py)."""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core import sync_doctor as sd, voucher_fastlane as fl
from core.durations import router_limit
from core.models import (AgentCommand, Business, Router, RouterAgent, RouterHotspotProfile, RouterHotspotUser,
                         RouterSyncJob, Voucher, VoucherPlan)
from core.utils import voucher_profile

CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'sdoc'}}


@override_settings(CACHES=CACHE, AUTH_EMAIL_OTP=False)
class Base(TestCase):
    link = True

    def setUp(self):
        cache.clear()
        fl._local.pending = set()
        self.owner = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456')
        self.biz = Business.objects.create(user=self.owner, business_name='Kerr Sering', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.plan = VoucherPlan.objects.create(business=self.biz, name='Day', duration_minutes=1440, price=10)
        self.router = Router.objects.create(business=self.biz, name='KerrSering', status='Online',
                                            connection_mode='agent' if self.link else 'api')
        if self.link:
            RouterAgent.objects.create(router=self.router, token_hash='d' * 64, last_seen_at=timezone.now(),
                                       enrolled_at=timezone.now(), script_version=9, ros_version='7.15')
        self.prof = voucher_profile(Voucher(business=self.biz, plan_name='Day', duration_minutes=1440), self.plan)[0]
        RouterHotspotProfile.objects.create(business=self.biz, router=self.router, name=self.prof, is_present=True)
        self.old = timezone.now() - timedelta(hours=1)

    def voucher(self, code, **kw):
        data = dict(business=self.biz, router=self.router, code=code, plan_name='Day', duration_minutes=1440, status='active',
                    mikrotik_sync_status='Synced')
        data.update(kw)
        v = Voucher.objects.create(**data)
        Voucher.objects.filter(pk=v.pk).update(created_at=self.old)
        v.refresh_from_db()
        return v

    def on_router(self, v, **kw):
        """The router has this voucher exactly as TapTap wants it (unless kw says otherwise)."""
        data = dict(business=self.biz, router=self.router, username=v.code, profile=self.prof,
                    limit_uptime=router_limit(v, self.plan) or '', uptime='0s', disabled=False, is_present=True)
        data.update(kw)
        return RouterHotspotUser.objects.create(**data)

    def job(self, users_arrived=True, by=None):
        job = RouterSyncJob.objects.create(business=self.biz, router=self.router, status='success', progress=100,
                                           requested_by=by, started_at=timezone.now() - timedelta(minutes=2))
        if self.link:
            AgentCommand.objects.create(router=self.router, kind='inventory_piece', status='done', expires_at=timezone.now() + timedelta(hours=1), done_at=timezone.now() - timedelta(minutes=1),
                                        params={'job_id': job.pk, 'kind': 'hotspot_users', 'received': users_arrived})
        return job

    def batches(self):
        ids = []
        for p in AgentCommand.objects.filter(router=self.router, kind='hotspot_users').values_list('params', flat=True):
            ids += (p or {}).get('ids') or []
        return ids


class LinkHealing(Base):
    def test_in_sync_router_needs_nothing(self):
        for c in ('AAA1', 'AAA2'):
            self.on_router(self.voucher(c))
        r = sd.run(self.job().pk)
        self.assertEqual((r['checked'], r['in_sync'], r['healed']), (2, 2, 0))
        self.assertEqual(r['score'], 100)
        self.assertEqual(sd.line(r), 'All 2 voucher(s) match the router')
        self.assertEqual(self.batches(), [])

    def test_synced_in_taptap_but_missing_on_the_router_is_sent_again(self):
        """The bug: a router reset / a user deleted in WinBox stayed missing for good, because TapTap said Synced."""
        gone = self.voucher('LOST1')
        self.on_router(self.voucher('HERE1'))
        r = sd.run(self.job().pk)
        self.assertEqual(r['fixed']['missing'], 1)
        self.assertIn(gone.pk, self.batches())
        gone.refresh_from_db()
        self.assertIn(gone.mikrotik_sync_status, ('Queued', 'Pending'))
        self.assertIn(gone.pk, r['ids'])
        self.assertEqual(sd.line(r), 'Healed 1: 1 missing')

    def test_report_is_saved_on_the_job_and_shown_with_live_confirmation(self):
        gone = self.voucher('LOST2')
        job = self.job()
        sd.run(job.pk)
        Voucher.objects.filter(pk=gone.pk).update(mikrotik_sync_status='Synced')       # the router answered
        from core.views import router_sync_status
        from django.test import RequestFactory
        req = RequestFactory().get('/x')
        req.user = self.owner
        with mock.patch('core.views.b', return_value=self.biz):
            d = json.loads(router_sync_status(req).content)
        doc = d['jobs'][0]['summary']['doctor']
        self.assertEqual((doc['confirmed'], doc['waiting']), (1, 0))
        self.assertNotIn('ids', doc)
        self.assertEqual(d['jobs'][0]['summary']['doctor_line'], 'Healed 1: 1 missing')

    def test_right_on_the_router_but_pending_is_marked_synced(self):
        v = self.voucher('PEND1', mikrotik_sync_status='Error', mikrotik_sync_error='timeout')
        self.on_router(v)
        r = sd.run(self.job().pk)
        v.refresh_from_db()
        self.assertEqual((v.mikrotik_sync_status, v.mikrotik_sync_error), ('Synced', ''))
        self.assertEqual(r['fixed']['status'], 1)
        self.assertEqual(self.batches(), [])

    def test_frozen_voucher_still_on_is_switched_off(self):
        v = self.voucher('FRZ1', frozen_at=timezone.now())
        self.on_router(v, disabled=False)
        r = sd.run(self.job().pk)
        self.assertEqual(r['fixed']['onoff'], 1)
        self.assertIn(v.pk, self.batches())

    def test_wrong_profile_is_moved_back(self):
        v = self.voucher('PRF1')
        self.on_router(v, profile='default')
        r = sd.run(self.job().pk)
        self.assertEqual(r['fixed']['profile'], 1)
        self.assertIn(v.pk, self.batches())

    def test_expired_but_still_on_is_switched_off(self):
        v = self.voucher('EXP1', status='expired')
        self.on_router(v, disabled=False)
        r = sd.run(self.job().pk)
        self.assertEqual(r['fixed']['expired_on'], 1)
        cmd = AgentCommand.objects.get(router=self.router, kind='hotspot_users_disable')
        self.assertEqual(cmd.params['names'], ['EXP1'])

    def test_nothing_is_guessed_when_the_user_list_did_not_arrive(self):
        v = self.voucher('LOST3')
        r = sd.run(self.job(users_arrived=False).pk)
        self.assertEqual(r['state'], 'partial')
        self.assertEqual(r['checked'], 0)
        self.assertIn('did not arrive', r['notes'][0])
        self.assertNotIn(v.pk, self.batches())

    def test_vouchers_on_their_way_or_made_after_the_read_are_left_alone(self):
        flying = self.voucher('FLY1', mikrotik_sync_status='Queued')
        AgentCommand.objects.create(router=self.router, kind='hotspot_users', status='sent', expires_at=timezone.now() + timedelta(hours=1), params={'ids': [flying.pk], 'users': []})
        job = self.job()
        fresh = Voucher.objects.create(business=self.biz, router=self.router, code='NEW1', plan_name='Day', duration_minutes=1440,
                                       status='active', mikrotik_sync_status='Synced')
        r = sd.run(job.pk)
        self.assertEqual(r['checked'], 0)
        self.assertEqual(r['healed'], 0)
        self.assertNotIn(fresh.pk, r['ids'])

    def test_router_made_voucher_that_vanished_is_flagged_not_deleted(self):
        v = self.voucher('WINBOX1', source='mikrotik')
        r = sd.run(self.job().pk)
        v.refresh_from_db()
        self.assertEqual(r['fixed']['gone'], 1)
        self.assertEqual((v.status, v.mikrotik_sync_status, v.mikrotik_sync_error), ('active', 'Error', sd.GONE_NOTE))
        self.assertNotIn(v.pk, self.batches())

    def test_router_reset_is_recognised(self):
        for i in range(25):
            self.voucher(f'RST{i:02d}')
        r = sd.run(self.job().pk)
        self.assertTrue(r['reset_suspected'])
        self.assertEqual(r['fixed']['missing'], 25)
        self.assertIn('reset', r['notes'][0])

    def test_refused_codes_only_retried_when_someone_presses_sync(self):
        from core.agent import REFUSED_MARK
        v = self.voucher('BAD1', mikrotik_sync_status='Error', mikrotik_sync_error=f'{REFUSED_MARK} 3 times')
        r = sd.run(self.job().pk)
        self.assertEqual((r['refused'], r['fixed']['missing']), (1, 0))
        self.assertNotIn(v.pk, self.batches())
        r = sd.run(self.job(by=self.owner).pk)
        self.assertEqual(r['fixed']['missing'], 1)
        self.assertIn(v.pk, self.batches())

    def test_renamed_code_is_left_to_the_rename(self):
        from core.models import VoucherCodeAlias
        v = self.voucher('NEWCODE')
        VoucherCodeAlias.objects.create(business=self.biz, voucher=v, code='OLDCODE')
        r = sd.run(self.job().pk)
        self.assertEqual((r.get('renamed'), r['fixed']['missing']), (1, 0))

    def test_missing_plan_profile_is_created(self):
        RouterHotspotProfile.objects.filter(router=self.router).update(is_present=False)
        r = sd.run(self.job().pk)
        self.assertEqual(r['fixed']['profiles'], 1)
        self.assertTrue(AgentCommand.objects.filter(router=self.router, kind='hotspot_users').exists())

    def test_members_use_the_exact_member_alignment(self):
        m = self.voucher('MEM1', login_type='member')
        with mock.patch('core.member_router_alignment.aligned_push_one', return_value=True) as push:
            r = sd.run(self.job().pk)
        push.assert_called_once()
        self.assertEqual(push.call_args[0][0].pk, m.pk)
        self.assertEqual(r['fixed']['missing'], 1)

    def test_runs_once_per_job_after_a_link_sync(self):
        job = self.job()
        with mock.patch.object(sd, 'run') as run:
            self.assertTrue(sd.schedule(job))
            self.assertFalse(sd.schedule(job))
        run.assert_called_once_with(job.pk)


class ApiHealing(Base):
    link = False

    def test_reads_the_router_after_the_sync_and_sends_what_differs(self):
        here, lost = self.voucher('API1'), self.voucher('API2')
        rows = [{'name': 'API1', 'profile': self.prof, 'disabled': 'false', 'limit-uptime': router_limit(here, self.plan) or '', 'uptime': '0s'}]
        svc = mock.MagicMock()
        svc.connect.return_value = svc
        svc.resource.return_value.get.return_value = rows
        svc.hotspot_profiles.return_value = [{'name': self.prof}]
        with mock.patch('core.mikrotik.MikroTikService', return_value=svc), \
                mock.patch('core.voucher_push.push_now', return_value=(1, 0)) as push:
            r = sd.run(self.job().pk)
        self.assertEqual((r['transport'], r['checked'], r['fixed']['missing']), ('RouterOS API', 2, 1))
        push.assert_called_once_with(self.router, [lost.pk])

    def test_unreachable_router_is_reported_not_guessed(self):
        self.voucher('API3')
        with mock.patch('core.mikrotik.MikroTikService', side_effect=OSError('timed out')):
            r = sd.run(self.job().pk)
        self.assertEqual(r['state'], 'partial')
        self.assertIn('timed out', r['notes'][0])

    def test_api_sync_completion_schedules_the_doctor(self):
        from core.tasks import _complete_job
        job = RouterSyncJob.objects.create(business=self.biz, router=self.router, status='running')
        with mock.patch('core.sync_doctor.schedule') as sch, mock.patch('core.member_keepalive.ensure_member_profiles'):
            _complete_job(job, {'errors': []})
        sch.assert_called_once()


class Words(Base):
    def test_phone_card_shows_what_the_last_sync_healed(self):
        from core.views_mobile_routers import _healed
        job = self.job()
        self.voucher('LOSTM')
        sd.run(job.pk)
        job.refresh_from_db()
        self.assertEqual(_healed(job), 'Healed 1: 1 missing')
        job.status = 'running'
        self.assertEqual(_healed(job), '')

    def test_fit_sees_the_last_check(self):
        from core.fit_sync import routers_status
        job = self.job()
        self.on_router(self.voucher('OKF1'))
        sd.run(job.pk)
        self.assertEqual(routers_status(self.biz)['routers'][0]['last_sync_check'], 'All 1 voucher(s) match the router')
