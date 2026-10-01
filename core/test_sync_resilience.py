"""Full sync must not fail because one table could not be read, and vouchers go first.

Run:  DB_ENGINE=sqlite python manage.py test core.test_sync_resilience
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import (AgentCommand, Business, Router, RouterHotspotProfile, RouterHotspotUser, RouterSyncJob, SyncedIPBinding, Voucher,
                     VoucherPlan)
from .test_members import FakeSvc


@override_settings(AUTH_EMAIL_OTP=False)
class SyncResilienceTests(TestCase):
    def setUp(self):
        u = User.objects.create_user('r@x.com', 'r@x.com', 'pw')
        self.biz = Business.objects.create(user=u, business_name='B', owner_name='O', phone='1', trial_ends_at=timezone.now() + timedelta(days=7))
        self.r = Router.objects.create(business=self.biz, name='hAP', ip_address='10.0.0.1', username='u', password='p', status='Online')
        VoucherPlan.objects.create(business=self.biz, name='Daily', price=25, duration_minutes=1440, mikrotik_profile_name='daily')
        Voucher.objects.create(business=self.biz, router=self.r, code='TT000001', plan_name='Daily', duration_minutes=1440, source='taptap')
        SyncedIPBinding.objects.create(business=self.biz, router=self.r, mac_address='AA:BB:CC:00:00:01', binding_type='bypassed', source='taptap')
        RouterHotspotProfile.objects.create(business=self.biz, router=self.r, name='daily', mikrotik_id='*A')

    def svc(self, broken=None, fail_once=None):
        rows = [{'id': '*1', 'name': 'MK1', 'password': 'MK1', 'profile': 'daily', 'limit-uptime': '1d', 'uptime': '0s'}]
        s = FakeSvc(rows, [{'id': '*A', 'name': 'daily', 'shared-users': '1'}])
        s.upsert_binding = lambda b: ('created', '*b1')
        if broken:
            def boom(*a, **k): raise Exception(f'{broken} timed out')
            setattr(s, broken, boom)
        if fail_once:
            real, calls = getattr(s, fail_once), {'n': 0}
            def flaky(*a, **k):
                calls['n'] += 1
                if calls['n'] == 1:
                    raise Exception('timed out')
                return real(*a, **k)
            setattr(s, fail_once, flaky)
        return s

    def sync(self, svc):
        from .sync import sync_router
        with mock.patch('core.sync.MikroTikService', return_value=svc), mock.patch('time.sleep'):
            return sync_router(self.r)

    def test_unreadable_tables_are_warnings_and_vouchers_still_go_out(self):
        for broken, says in (('hotspot_profiles', 'HotSpot profiles could not be read'), ('bindings', 'IP bindings could not be read'),
                             ('topology_data', 'Network topology could not be read')):
            svc = self.svc(broken)
            res = self.sync(svc)
            self.assertTrue(any(says in e for e in res['errors']), broken)
            self.assertEqual(svc.pushed[-1]['name'], 'TT000001', broken)              # the TapTap voucher still went out
            self.r.refresh_from_db(); self.assertEqual(self.r.status, 'Online', broken)

    def test_a_failed_read_does_not_make_everything_look_deleted(self):
        self.sync(self.svc('hotspot_profiles'))
        self.assertTrue(RouterHotspotProfile.objects.get(router=self.r, name='daily').is_present)
        self.sync(self.svc('bindings'))
        self.assertTrue(SyncedIPBinding.objects.get(mac_address='AA:BB:CC:00:00:01').is_present)

    def test_users_table_gets_one_retry(self):
        res = self.sync(self.svc(fail_once='hotspot_users'))
        self.assertEqual(res['pulled_users'], 1)
        RouterHotspotUser.objects.filter(router=self.r).update(is_present=True)
        with self.assertRaises(Exception):
            self.sync(self.svc('hotspot_users'))                                    # really unreachable: the sync fails…
        self.assertTrue(RouterHotspotUser.objects.filter(router=self.r, is_present=True).exists())   # …without wiping TapTap's copy

    # ── vouchers first ──
    def test_link_delivers_voucher_commands_before_a_full_sync(self):
        from .agent import build_response, queue
        from .models import RouterAgent
        Router.objects.filter(pk=self.r.pk).update(connection_mode='agent'); self.r.refresh_from_db()
        RouterAgent.objects.create(router=self.r)
        for i in range(10):
            queue(self.r, 'inventory_piece', {'job_id': 1, 'kind': 'interfaces'}, label=f'piece {i}')
        queue(self.r, 'hotspot_user_set', {'name': 'TT000001', 'disabled': True}, label='Disable voucher')
        queue(self.r, 'hotspot_users', {'profiles': [], 'users': [{'n': 'NEW00001', 'prof': 'daily', 'lim': '1d', 'dis': False}], 'ids': []}, label='New voucher')
        with mock.patch('core.agent.wrap', side_effect=lambda cmd, url, check: f'# {cmd.kind} {cmd.pk}'), \
                mock.patch('core.agent_inventory.send_function', return_value='# helper'):
            script = build_response(self.r, 'https://taptap.test')
        order = [line.split()[1] for line in script.splitlines() if line.startswith('# ') and line != '# helper']
        self.assertEqual(order[:2], ['hotspot_user_set', 'hotspot_users'])            # vouchers first in what the router runs
        self.assertEqual(set(order[2:]), {'inventory_piece'})

    def test_link_sync_asks_for_voucher_tables_first(self):
        from .agent_inventory import start_agent_inventory_sync
        from .models import RouterAgent
        Router.objects.filter(pk=self.r.pk).update(connection_mode='agent'); self.r.refresh_from_db()
        RouterAgent.objects.create(router=self.r)
        job = RouterSyncJob.objects.create(business=self.biz, router=self.r, status='queued')
        start_agent_inventory_sync(job)
        kinds = [c.params['kind'] for c in AgentCommand.objects.filter(kind='inventory_piece').order_by('created_at', 'id')]
        self.assertEqual(kinds[:4], ['hotspot_user_profiles', 'hotspot_users', 'active_users', 'hotspot_ip_bindings'])

    def test_voucher_pushes_use_the_fast_worker(self):
        from django.conf import settings
        from .tasks import push_vouchers_task
        self.assertEqual(settings.CELERY_TASK_ROUTES['core.tasks.push_vouchers_task'], {'queue': 'live'})
        self.assertEqual(push_vouchers_task.queue, 'live')
