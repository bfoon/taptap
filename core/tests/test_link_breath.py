"""Breathing check-ins: TapTap Link asks less of the router when nothing needs it (core/link_breath.py)."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core import link_breath as lb
from core.models import AgentCommand, Business, Router, RouterAgent, RouterSyncJob, Voucher

SET = '/system scheduler set [find name="taptap-link"] interval='


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'breath'}})
class BreathTests(TestCase):
    def setUp(self):
        cache.clear()
        u = User.objects.create_user('o', 'o@x.gm', 'pw')
        self.biz = Business.objects.create(user=u, business_name='Kunda Net', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.router = Router.objects.create(business=self.biz, name='R1',
                                            connection_mode='agent')
        self.agent = RouterAgent.objects.create(router=self.router, token_hash='h' * 64, script_version=12,
                                                active_sessions=40, cpu_load=20, last_seen_at=timezone.now(),
                                                enrolled_at=timezone.now())

    def test_calm_with_customers_and_nothing_waiting(self):
        self.assertEqual(lb.decide(self.router, self.agent)[:2], (20, 'calm'))
        self.assertIn(SET + '20s', lb.line(self.router, self.agent))
        self.assertEqual(lb.line(self.router, self.agent), '')            # already told: not said again
        self.assertEqual(lb.pace(self.router.pk), 20)

    def test_rest_at_night_and_step_back_when_the_router_is_busy(self):
        self.agent.active_sessions = 0
        self.assertEqual(lb.decide(self.router, self.agent)[:2], (30, 'rest'))
        self.agent.active_sessions, self.agent.cpu_load = 200, 92
        secs, level, why = lb.decide(self.router, self.agent)
        self.assertEqual((secs, level), (30, 'ease')); self.assertIn('92', why)

    def test_work_brings_it_straight_back_to_fast(self):
        lb.line(self.router, self.agent)                                 # calm: 20 s
        AgentCommand.objects.create(router=self.router, kind='disconnect', params={'user': 'X'}, label='x',
                                    expires_at=timezone.now() + timedelta(minutes=5))
        self.assertIn(SET + '10s', lb.line(self.router, self.agent))
        AgentCommand.objects.all().update(status='done')
        self.assertEqual(lb.decide(self.router, self.agent)[1], 'fast')    # held fast for 2 minutes after the work
        cache.delete(f'tt:link:busy:{self.router.pk}')
        self.assertEqual(lb.decide(self.router, self.agent)[1], 'calm')

    def test_customer_at_login_page_pending_voucher_and_viewer_keep_it_fast(self):
        from core.agent import mark_urgent
        mark_urgent(self.router)
        self.assertEqual(lb.decide(self.router, self.agent)[:2], (10, 'fast'))
        cache.clear()
        Voucher.objects.create(business=self.biz, router=self.router, code='AAA11', plan_name='1 Day', mikrotik_sync_status='Pending')
        self.assertEqual(lb.decide(self.router, self.agent)[1], 'fast')
        Voucher.objects.all().update(mikrotik_sync_status='Synced'); cache.clear()
        lb.watching(self.biz.pk)
        self.assertEqual(lb.decide(self.router, self.agent)[1], 'fast')

    def test_fast_router_never_told_gets_no_line(self):
        lb.hold_fast(self.router.pk)
        self.assertEqual(lb.line(self.router, self.agent), '')

    def test_old_script_and_switch_off(self):
        self.agent.script_version = 3
        self.assertEqual(lb.line(self.router, self.agent), '')
        self.agent.script_version = 12
        lb.line(self.router, self.agent)                                 # now at 20 s
        import os
        os.environ['LINK_BREATHING'] = '0'
        try:
            self.assertIn(SET + '10s', lb.line(self.router, self.agent))  # switched off: put back to its own interval
            self.assertEqual(lb.sync_minutes(self.router)[0], 30)
        finally:
            del os.environ['LINK_BREATHING']

    def test_said_again_after_ten_minutes(self):
        lb.line(self.router, self.agent)
        st = cache.get(f'tt:link:pace:{self.router.pk}'); st['at'] -= lb.REASSERT + 5
        cache.set(f'tt:link:pace:{self.router.pk}', st, 3600)
        self.assertIn(SET + '20s', lb.line(self.router, self.agent))

    def test_offline_allowance_follows_the_pace(self):
        self.assertEqual(self.agent.grace_seconds, 90)
        self.agent.active_sessions = 0
        lb.line(self.router, self.agent)                                 # resting: 30 s
        self.assertEqual(self.agent.grace_seconds, 120)

    def test_answer_carries_the_pace_after_the_commands(self):
        self.assertTrue(lb.with_pace(self.router, self.agent, ':log info "x"').startswith(':log info "x"\n:do {'))
        self.assertEqual(lb.with_pace(self.router, self.agent, ':log info "x"'), ':log info "x"')

    def _job(self, healed, minutes_ago, status='success'):
        j = RouterSyncJob.objects.create(business=self.biz, router=self.router, status=status,
                                         summary={'doctor': {'healed': healed}})
        RouterSyncJob.objects.filter(pk=j.pk).update(finished_at=timezone.now() - timedelta(minutes=minutes_ago))

    def test_full_syncs_spread_out_while_clean(self):
        self.agent.uptime = '3d4h'; self.agent.save()
        self.assertEqual(lb.sync_minutes(self.router)[0], 30)
        self._job(0, 200)
        self.assertEqual(lb.sync_minutes(self.router)[0], 30)
        self._job(0, 150)
        self.assertEqual(lb.sync_minutes(self.router)[0], 60)
        self._job(0, 90)
        self.assertEqual(lb.sync_minutes(self.router)[0], 120)
        self._job(0, 20)
        self.assertEqual(lb.sync_minutes(self.router)[0], 120)            # never beyond the cap
        self._job(3, 5)
        self.assertEqual(lb.sync_minutes(self.router)[0], 30)             # something was healed: close watch again
        self._job(0, 3); self._job(0, 2, status='failed')
        self.assertEqual(lb.sync_minutes(self.router)[0], 30)             # a failed sync: also back

    def test_restart_brings_the_sync_back(self):
        self._job(0, 200); self._job(0, 150); self._job(0, 90)
        self.agent.uptime = '10m5s'; self.agent.save()
        self.router.refresh_from_db()
        minutes, why = lb.sync_minutes(self.router)
        self.assertEqual(minutes, 30); self.assertIn('restarted', why)

    def test_usage_counts_and_savings(self):
        t = timezone.now()
        for i in range(4):                                               # four check-ins 20 s apart on a 10 s router
            lb.note_traffic(self.router.pk, 20000, 100, now=t + timedelta(seconds=20 * i), agent=self.agent)
        u = lb.usage(self.router.pk, now=t)
        self.assertEqual(u['checkins'], 4); self.assertEqual(u['saved'], 3); self.assertEqual(u['saved_pct'], 43)
        s = lb.summary(self.router, self.agent)
        self.assertEqual(s['usage']['checkins'], 4); self.assertTrue(s['on'])
