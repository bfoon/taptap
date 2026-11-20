"""Fit missions (core/fit_missions.py): build from Fit's card, the clock (at / timetable / quiet moment / condition),
rights checked again at run time, telling people (bell, email, chat), following a restart, the page and the app API."""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core import fit_actions, fit_missions as fm, fit_tools
from core.models import AgentCommand, Business, Router, RouterAgent, Voucher
from core.models_chat import ChatMessage
from core.models_events import EventAlert
from core.models_fit import FitAction, FitMission, FitSettings
from core.models_router_schedule import RouterScheduleRun
from core.models_team import TeamMember


@override_settings(AUTH_EMAIL_OTP=False,
                   CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'fitmission'}})
class FitMissionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456', first_name='Awa')
        self.biz = Business.objects.create(user=self.owner, business_name='Kunda Net', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.lamin = User.objects.create_user('l@x.gm', 'l@x.gm', 'pw-123456', first_name='Lamin', last_name='Jallow')
        self.tm = TeamMember.objects.create(business=self.biz, user=self.lamin, role='voucher_support',
                                            extra_permissions=['network.manage'])
        self.r = Router.objects.create(business=self.biz, name='Mast', connection_mode='agent', status='Online')
        self.agent = RouterAgent.objects.create(router=self.r, token_hash='f' * 64, enrolled_at=timezone.now(),
                                                last_seen_at=timezone.now(), uptime='5d03:00:00')
        self.settings = FitSettings.objects.create(business=self.biz, allowed=['missions', 'router', 'vouchers'])
        self.perms = fm.perms_for(self.owner, self.biz)[0]

    def online(self, n):
        cache.set(f'tt:linkactive:{self.r.pk}', [{'user': f'U{i}'} for i in range(n)], 600)

    def make(self, user=None, **a):
        user = user or self.owner
        perms = fm.perms_for(user, self.biz)[0]
        _, fields, *_ = fm.build(self.biz, user, perms, a)
        return fm.create(self.biz, user, fields)

    # ── reading what the person asked ──
    def test_build_times_and_words(self):
        now = timezone.now()
        clean, fields, title, facts, warnings, danger, confirm = fm.build(
            self.biz, self.owner, self.perms, {'in_minutes': 45, 'mission_task': 'notify', 'message': 'Check the mast', 'email_to': 'Lamin'})
        self.assertEqual(fields['trigger'], 'at')
        self.assertAlmostEqual((fields['next_at'] - now).total_seconds(), 45 * 60, delta=61)
        self.assertIn('at', clean)                                           # relative time turned into a real one
        self.assertEqual(fields['tell']['emails'][0]['user'], self.lamin.pk)
        self.assertTrue(any(f[0] == 'Tells' and 'Lamin' in f[1] for f in facts))
        # the clean args give the same mission again (approval re-checks them)
        _, again, *_ = fm.build(self.biz, self.owner, self.perms, clean)
        self.assertEqual(again['next_at'], fields['next_at'])
        with self.assertRaises(fm.MissionError):
            fm.build(self.biz, self.owner, self.perms, {'at': '2020-01-01 10:00', 'mission_task': 'notify', 'message': 'x'})
        with self.assertRaises(fm.MissionError):                            # a shutdown must be understood
            fm.build(self.biz, self.owner, self.perms, {'at': '23:00', 'mission_task': 'shutdown_router', 'router': 'Mast'})
        with self.assertRaises(fm.MissionError):
            fm.build(self.biz, self.owner, self.perms, {'trigger': 'when', 'condition': 'nope', 'mission_task': 'notify', 'message': 'x'})

    def test_outside_email_only_owner_or_admin(self):
        with self.assertRaises(fm.MissionError):
            self.make(user=self.lamin, at='23:00', mission_task='notify', message='hi', email_to='friend@gmail.com')
        m = self.make(at='23:00', mission_task='notify', message='hi', email_to='friend@gmail.com')
        self.assertEqual(m.tell['emails'], [{'email': 'friend@gmail.com', 'name': 'friend@gmail.com'}])

    def test_restart_needs_network_right(self):
        TeamMember.objects.filter(pk=self.tm.pk).update(role='viewer', extra_permissions=[])
        with self.assertRaises(fm.MissionError):
            self.make(user=self.lamin, at='23:00', mission_task='restart_router', router='Mast')

    # ── at a time ──
    def test_at_time_heads_up_then_notify_by_email_and_chat(self):
        m = self.make(at='23:00', mission_task='notify', message='Collect the cash from Tower', email_to='Lamin',
                      chat_team=True, before_minutes=10)
        due = m.next_at
        self.assertIsNone(fm.process(m, due - timedelta(minutes=30)))
        fm.process(m, due - timedelta(minutes=9))
        self.assertTrue(EventAlert.objects.filter(kind='fit_mission', title__startswith='⏰ In 10 min').exists())
        fm.process(m, due - timedelta(minutes=8))                           # one heads-up only
        self.assertEqual(EventAlert.objects.filter(kind='fit_mission').count(), 1)
        self.assertEqual(fm.process(m, due), 'ran')
        m.refresh_from_db()
        self.assertEqual(m.status, 'done')
        self.assertEqual(m.runs, 1)
        self.assertTrue(any('Collect the cash' in e.body for e in mail.outbox))
        self.assertEqual(mail.outbox[-1].to, ['l@x.gm'])
        self.assertTrue(ChatMessage.objects.filter(system=True, body__contains='Collect the cash').exists())

    def test_missed_when_server_was_down(self):
        m = self.make(at='23:00', mission_task='notify', message='x')
        self.assertEqual(fm.process(m, m.next_at + timedelta(hours=2)), 'missed')
        m.refresh_from_db()
        self.assertEqual(m.status, 'missed')

    def test_timetable_goes_on(self):
        m = self.make(trigger='repeat', repeat='daily', time='08:00', mission_task='report', report='overview')
        first = m.next_at
        self.assertEqual(fm.process(m, first + timedelta(seconds=20)), 'ran')
        m.refresh_from_db()
        self.assertEqual(m.status, 'waiting')
        self.assertEqual(m.next_at, first + timedelta(days=1))
        self.assertIn('Routers', m.last_result)

    # ── the quiet moment: restart when few people are online, tell Lamin when it is back ──
    def test_quiet_restart_and_back_online(self):
        m = self.make(trigger='quiet', window_from='01:00', window_to='05:00', router='Mast', max_online=2,
                      mission_task='restart_router', email_to='Lamin')
        start = m.next_at
        self.online(9)
        self.assertEqual(fm.process(m, start + timedelta(minutes=5)), 'waiting')
        self.assertFalse(AgentCommand.objects.filter(kind='reboot').exists())
        self.online(1)
        self.agent.last_seen_at = timezone.now()
        self.agent.save(update_fields=['last_seen_at'])
        self.assertEqual(fm.process(m, start + timedelta(minutes=40)), 'ran')
        self.assertTrue(AgentCommand.objects.filter(kind='reboot', router=self.r).exists())
        run = RouterScheduleRun.objects.get(router=self.r)
        self.assertEqual(run.online_at_run, 1)
        m.refresh_from_db()
        self.assertEqual(m.status, 'done')
        self.assertEqual(m.state.get('follow'), run.pk)
        self.assertTrue(any('1 person was online' in e.body or 'people (you asked for 2 or fewer)' in e.body or 'online' in e.body for e in mail.outbox))
        # the router comes back: the mission tells its people
        RouterScheduleRun.objects.filter(pk=run.pk).update(status='back', down_seconds=95)
        n = len(mail.outbox)
        self.assertEqual(fm.follow(m), 'followed')
        self.assertGreater(len(mail.outbox), n)
        self.assertIn('back online', mail.outbox[-1].subject)
        m.refresh_from_db()
        self.assertNotIn('follow', m.state)

    def test_quiet_skip_when_never_that_quiet(self):
        m = self.make(trigger='quiet', window_from='01:00', window_to='02:00', router='Mast', max_online=0, otherwise='skip',
                      mission_task='restart_router')
        start = m.next_at
        self.online(4)
        self.assertEqual(fm.process(m, start + timedelta(minutes=10)), 'waiting')
        self.assertEqual(fm.process(m, start + timedelta(minutes=60)), 'skipped')
        self.assertFalse(AgentCommand.objects.filter(kind='reboot').exists())
        m.refresh_from_db()
        self.assertEqual(m.status, 'done')

    def test_quietest_moment_without_number(self):
        m = self.make(trigger='quiet', window_from='01:00', window_to='04:00', router='Mast', mission_task='restart_router', daily=True)
        start = m.next_at
        for minute, n in ((10, 30), (60, 12), (100, 8)):
            self.online(n)
            self.assertEqual(fm.process(m, start + timedelta(minutes=minute)), 'waiting')
        self.online(9)                                                       # after 2/3, but not the quietest
        self.assertEqual(fm.process(m, start + timedelta(minutes=125)), 'waiting')
        self.online(7)
        self.agent.last_seen_at = timezone.now()
        self.agent.save(update_fields=['last_seen_at'])
        self.assertEqual(fm.process(m, start + timedelta(minutes=130)), 'ran')
        m.refresh_from_db()
        self.assertEqual(m.status, 'waiting')                                # every day
        self.assertEqual(m.next_at, start + timedelta(days=1))

    # ── when something happens ──
    def test_condition_router_offline_once_and_every_time(self):
        m = self.make(trigger='when', condition='router_offline', router='Mast', for_minutes=5, mission_task='notify',
                      message='Mast is down — send someone', tell_team=True, every_time=True, cooldown_minutes=1)
        now = timezone.now()
        RouterAgent.objects.filter(pk=self.agent.pk).update(last_seen_at=now - timedelta(hours=1))
        m = FitMission.objects.select_related('business', 'created_by').get(pk=m.pk)
        with mock.patch('django.utils.timezone.now', return_value=now):
            self.assertEqual(fm.process(m, now), 'waiting')                  # offline, but not for 5 minutes yet
        later = now + timedelta(minutes=6)
        with mock.patch('django.utils.timezone.now', return_value=later):
            self.assertEqual(fm.process(m, later), 'ran')
            self.assertEqual(fm.process(m, later + timedelta(minutes=3)), 'waiting')   # still the same outage: not again
        self.assertEqual(EventAlert.objects.filter(kind='fit_mission', user__isnull=True).count(), 1)
        # back online, then down again: told again
        RouterAgent.objects.filter(pk=self.agent.pk).update(last_seen_at=later + timedelta(minutes=10))
        t2 = later + timedelta(minutes=10)
        with mock.patch('django.utils.timezone.now', return_value=t2):
            self.assertIsNone(fm.process(m, t2))
        RouterAgent.objects.filter(pk=self.agent.pk).update(last_seen_at=t2 - timedelta(minutes=30))
        t3 = t2 + timedelta(minutes=20)
        with mock.patch('django.utils.timezone.now', return_value=t3):
            fm.process(m, t3)
            self.assertEqual(fm.process(m, t3 + timedelta(minutes=6)), 'ran')
        m.refresh_from_db()
        self.assertEqual(m.runs, 2)
        self.assertEqual(m.status, 'waiting')

    def test_condition_stock_and_sales(self):
        from core.models import VoucherPlan
        VoucherPlan.objects.create(business=self.biz, name='Daily', price=10, duration_minutes=1440)
        m = self.make(trigger='when', condition='stock_below', plan='daily', value=3, mission_task='notify', message='Print Daily vouchers')
        self.assertEqual(m.when['plan'], 'Daily')
        for i in range(4):
            Voucher.objects.create(business=self.biz, code=f'D{i}', plan_name='Daily', status='active')
        self.assertIsNone(fm.process(m, timezone.now()))
        Voucher.objects.filter(code='D0').update(status='disabled')
        Voucher.objects.filter(code='D1').update(status='disabled')
        self.assertEqual(fm.process(m, timezone.now()), 'ran')
        m.refresh_from_db()
        self.assertEqual(m.status, 'done')

    # ── rights checked again when it runs ──
    def test_rights_checked_at_run_time(self):
        m = self.make(user=self.lamin, at='23:00', mission_task='restart_router', router='Mast')
        TeamMember.objects.filter(pk=self.tm.pk).update(is_active=False)        # left the business
        self.assertEqual(fm.process(m, m.next_at), 'ran')
        m.refresh_from_db()
        self.assertEqual(m.status, 'failed')
        self.assertIn('no longer in the business', m.last_result)
        self.assertFalse(AgentCommand.objects.filter(kind='reboot').exists())
        m2 = self.make(at='23:30', mission_task='restart_router', router='Mast')
        FitSettings.objects.filter(pk=self.settings.pk).update(allowed=['router'])  # missions switched off
        fm.process(m2, m2.next_at)
        m2.refresh_from_db()
        self.assertEqual(m2.status, 'failed')
        self.assertIn('no longer allows Fit missions', m2.last_result)

    def test_action_mission_runs_a_fit_change(self):
        v = Voucher.objects.create(business=self.biz, code='ZX99', plan_name='Daily', status='active')
        m = self.make(at='23:00', mission_task='action', action_kind='freeze_voucher', action_args={'code': 'ZX99', 'reason': 'night check'})
        do = mock.Mock(return_value='Frozen.')
        spec = fit_actions.KINDS['freeze_voucher']
        with mock.patch.dict(fit_actions.KINDS, {'freeze_voucher': spec[:3] + (do,) + spec[4:]}):
            fm.process(m, m.next_at)
        self.assertTrue(do.called)
        m.refresh_from_db()
        self.assertEqual(m.status, 'done', m.last_result)
        self.assertTrue(FitAction.objects.filter(kind='freeze_voucher', status='done', title__contains='(mission)').exists())
        # not allowed group at run time
        FitSettings.objects.filter(pk=self.settings.pk).update(allowed=['missions'])
        m2 = self.make(at='23:30', mission_task='action', action_kind='freeze_voucher', action_args={'code': v.code})
        fm.process(m2, m2.next_at)
        m2.refresh_from_db()
        self.assertEqual(m2.status, 'failed')
        self.assertIn('no longer allows', m2.last_result)

    # ── through Fit's card ──
    def test_propose_and_approve_through_fit(self):
        ctx = fit_tools.FitContext(business=self.biz, user=self.owner, perms=self.perms, settings=self.settings)
        res = json.loads(fit_tools.call(ctx, 'propose_action', {
            'kind': 'create_mission', 'trigger': 'quiet', 'window_from': '01:00', 'window_to': '05:00', 'router': 'Mast',
            'max_online': 3, 'mission_task': 'restart_router', 'email_to': 'Lamin', 'before_minutes': 15}))
        self.assertTrue(res.get('proposed'), res)
        self.assertTrue(any('Everyone online' in w for w in res['warnings']))
        a = FitAction.objects.get(pk=res['action_id'])
        self.assertFalse(FitMission.objects.exists())                        # nothing until approved
        ok, msg = fit_actions.run(a, self.owner, self.perms, self.settings)
        self.assertTrue(ok, msg)
        m = FitMission.objects.get()
        self.assertEqual(m.task, 'restart_router')
        self.assertEqual(m.when['max_online'], 3)
        self.assertEqual(m.tell['before_minutes'], 15)
        listed = json.loads(fit_tools.call(ctx, 'missions', {}))
        self.assertEqual(listed['missions'][0]['id'], m.pk)
        # a read-only mission run of Fit cannot propose anything
        ro = fit_tools.FitContext(business=self.biz, user=self.owner, perms=self.perms, settings=self.settings, read_only=True)
        self.assertNotIn('propose_action', [t['function']['name'] for t in fit_tools.specs(ro)])
        self.assertIn('error', json.loads(fit_tools.call(ro, 'propose_action', {'kind': 'freeze_voucher', 'code': 'X'})))

    def test_tick_runs_due_and_skips_future(self):
        m1 = self.make(in_minutes=1, mission_task='notify', message='soon')
        m2 = self.make(at=(timezone.localtime() + timedelta(days=3)).strftime('%Y-%m-%d %H:%M'), mission_task='notify', message='later')
        out = fm.tick(timezone.now() + timedelta(minutes=2))
        self.assertEqual(out['ran'], 1)
        self.assertEqual(FitMission.objects.get(pk=m1.pk).status, 'done')
        self.assertEqual(FitMission.objects.get(pk=m2.pk).status, 'waiting')

    # ── page and app ──
    def test_page_and_app_ops(self):
        self.client.force_login(self.owner)
        r = self.client.post('/fit/missions/', {'trigger': 'at', 'at': (timezone.localtime() + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M'),
                                                'mission_task': 'notify', 'message': 'Hello team', 'tell_me': 'on'})
        self.assertEqual(r.status_code, 302)
        m = FitMission.objects.get()
        self.assertContains(self.client.get('/fit/missions/'), 'Hello team')
        d = self.client.get('/fit/missions/data/').json()
        self.assertEqual(d['missions'][0]['id'], m.pk)
        r = self.client.post(f'/fit/missions/{m.pk}/', json.dumps({'op': 'pause'}), content_type='application/json')
        self.assertEqual(r.json()['mission']['status'], 'paused')
        r = self.client.post(f'/fit/missions/{m.pk}/', json.dumps({'op': 'resume'}), content_type='application/json')
        self.assertEqual(r.json()['mission']['status'], 'waiting')
        # someone else cannot change the owner's mission
        self.client.force_login(self.lamin)
        r = self.client.post(f'/fit/missions/{m.pk}/', json.dumps({'op': 'cancel'}), content_type='application/json')
        self.assertEqual(r.status_code, 400)
        self.client.force_login(self.owner)
        r = self.client.post(f'/fit/missions/{m.pk}/', json.dumps({'op': 'run'}), content_type='application/json')
        self.assertEqual(r.json()['mission']['status'], 'done')
