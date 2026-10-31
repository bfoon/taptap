"""Ready to browse: a valid code is on the router before the login, refused logins heal, nothing waits for a sync."""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core import agent, voucher_fastlane as fl, voucher_ready as vr
from core.models import (AgentCommand, Business, PortalDeployment, PortalPage, Router, RouterAgent, RouterHotspotUser,
                         Voucher, VoucherDeviceBinding, VoucherEvent, VoucherPlan)

URL = 'https://taptap.example'


class Base(TestCase):
    link = True

    def setUp(self):
        cache.clear()
        fl._local.pending = set()
        self.user = User.objects.create_user('owner', password='x')
        self.biz = Business.objects.create(user=self.user, business_name='B', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=7))
        if self.link:
            self.router = Router.objects.create(business=self.biz, name='R1', ip_address='', connection_mode='agent')
            self.agent = RouterAgent.objects.create(router=self.router, token_hash='h' * 64, last_seen_at=timezone.now(),
                                                    enrolled_at=timezone.now(), script_version=8, ros_version='7.15.3 (stable)')
        else:
            self.router = Router.objects.create(business=self.biz, name='R1', ip_address='10.0.0.1', username='a', password='p')
        self.page = PortalPage.objects.create(business=self.biz, name='Login', slug='b-login', kind='login', is_published=True)
        vr.RUN_INLINE = True

    def tearDown(self):
        vr.RUN_INLINE = False

    def voucher(self, code='ABC123', **kw):
        data = dict(business=self.biz, router=self.router, code=code, plan_name='Day', duration_minutes=1440,
                    max_devices=1, mikrotik_sync_status='Synced')
        data.update(kw)
        v = Voucher.objects.create(**data)      # no on-commit callbacks run here: nothing is sent by the fast lane
        AgentCommand.objects.filter(router=self.router).delete()
        fl.unclaim([v.pk])
        return v

    def state(self, code, **extra):
        body = {'code': code, 'mac': 'AA:BB:CC:00:00:01', **extra}
        r = self.client.post(f'/p/{self.page.slug}/state/', json.dumps(body), content_type='text/plain')
        return r.status_code, r.json()

    def check(self, code, **extra):
        r = self.client.post(f'/p/{self.page.slug}/check/', json.dumps({'code': code, 'mac': 'AA:BB:CC:00:00:01', **extra}),
                             content_type='application/json')
        return r.status_code, r.json()

    def post(self, path, data):
        return self.client.post(f'/p/{self.page.slug}/{path}/', json.dumps(data), content_type='text/plain').json()

    def queued_users(self):
        return [u['n'] for c in AgentCommand.objects.filter(router=self.router, kind='hotspot_users', status='queued')
                for u in (c.params or {}).get('users', [])]


# ─────────────────────────── login guarantee: TapTap Link ───────────────────────────

class LoginGuaranteeLink(Base):
    def test_voucher_not_on_router_is_sent_first_and_page_waits(self):
        v = self.voucher(mikrotik_sync_status='Pending')
        status, d = self.state('abc123')
        self.assertEqual(status, 200)
        self.assertTrue(d['ok'])
        self.assertEqual(d['code'], 'ABC123')
        self.assertIs(d['ready'], False)
        self.assertGreaterEqual(d['wait'], 10, 'old router pages must wait instead of logging in at once')
        self.assertTrue(d['ready_token'])
        self.assertIn('ABC123', self.queued_users(), 'the voucher must go to the router before the login')
        v.refresh_from_db()
        self.assertEqual(v.mikrotik_sync_status, 'Queued')
        self.assertTrue(cache.get(f'tt:link:urgent:{self.router.pk}'), 'the router should check in faster')

    def test_voucher_already_on_router_logs_in_at_once(self):
        self.voucher()
        _, d = self.state('ABC123')
        self.assertTrue(d['ok'])
        self.assertNotIn('ready', d)
        self.assertFalse(d.get('wait'))
        self.assertTrue(d['ready_token'])
        self.assertEqual(self.queued_users(), [])

    def test_page_poll_turns_ready_when_router_confirms(self):
        self.voucher(mikrotik_sync_status='Pending')
        _, d = self.state('ABC123')
        self.assertFalse(self.post('ready', {'t': d['ready_token']})['ready'])
        cmd = AgentCommand.objects.get(router=self.router, kind='hotspot_users', status='queued')
        self.assertTrue(agent.handle_ack(cmd.pk, agent.nonce(cmd), 'ok'))
        self.assertTrue(self.post('ready', {'t': d['ready_token']})['ready'])

    def test_stale_link_inventory_does_not_hold_a_confirmed_voucher(self):
        v = self.voucher()
        RouterHotspotUser.objects.create(business=self.biz, router=self.router, username=v.code, disabled=True,
                                         last_seen_at=timezone.now())
        vr.confirm([v.pk])          # the router acknowledged the enable after that inventory
        cache.set(f'tt:ready:ok:{v.pk}', timezone.now().timestamp() + 5, 60)
        _, d = self.state('ABC123')
        self.assertNotIn('ready', d)

    def test_router_copy_switched_off_is_fixed_before_login(self):
        v = self.voucher()
        RouterHotspotUser.objects.create(business=self.biz, router=self.router, username=v.code, disabled=True,
                                         last_seen_at=timezone.now())
        _, d = self.state('ABC123')
        self.assertIs(d['ready'], False)
        self.assertIn('ABC123', self.queued_users())

    def test_refusals_are_untouched(self):
        self.voucher(frozen_at=timezone.now(), freeze_kind='freeze', mikrotik_sync_status='Pending')
        _, d = self.state('ABC123')
        self.assertTrue(d.get('blocked'))
        self.assertNotIn('ready_token', d)
        self.assertEqual(self.queued_users(), [])

    def test_forged_or_foreign_token_reveals_nothing(self):
        self.voucher(mikrotik_sync_status='Pending')
        self.assertEqual(self.post('ready', {'t': 'nonsense'}), {'ready': True, 'wait': 0})
        self.assertEqual(self.post('heal', {'t': 'nonsense', 'error': 'invalid username or password'}), {'retry': False})
        self.assertEqual(self.queued_users(), [])

    def test_offline_link_router_does_not_make_customer_wait(self):
        RouterAgent.objects.filter(pk=self.agent.pk).update(last_seen_at=timezone.now() - timedelta(hours=1))
        self.voucher(mikrotik_sync_status='Pending')
        _, d = self.state('ABC123')
        self.assertNotIn('ready', d)

    def test_wait_reply_carries_the_real_code(self):
        self.voucher(code='abcLower')
        with mock.patch('core.device_lock.release_stale', return_value=15):
            _, d = self.state('ABCLOWER')
        self.assertEqual(d['wait'], 15)
        self.assertEqual(d['code'], 'abcLower', 'after the countdown the page must log in with the real code')

    def test_login_check_is_noted_in_voucher_history(self):
        v = self.voucher(mikrotik_sync_status='Pending')
        self.state('ABC123')
        self.assertTrue(VoucherEvent.objects.filter(voucher=v, reason='Ready to browse').exists())


# ─────────────────────────── login guarantee: Direct API / Tunnel ───────────────────────────

class LoginGuaranteeApi(Base):
    link = False

    def test_pending_voucher_is_pushed_during_the_check(self):
        self.voucher(mikrotik_sync_status='Pending')
        with mock.patch('core.voucher_ready.push_exact', return_value=True) as push:
            _, d = self.check('ABC123')
        self.assertTrue(d['success'])
        push.assert_called_once()
        self.assertNotIn('ready', d)
        self.assertFalse(d.get('wait'))

    def test_router_copy_missing_is_pushed(self):
        v = self.voucher()
        RouterHotspotUser.objects.create(business=self.biz, router=self.router, username=v.code, is_present=False,
                                         last_seen_at=timezone.now())
        with mock.patch('core.voucher_ready.push_exact', return_value=True) as push:
            self.check('ABC123')
        push.assert_called_once()

    def test_used_up_router_time_is_pushed(self):
        v = self.voucher()
        RouterHotspotUser.objects.create(business=self.biz, router=self.router, username=v.code, uptime='1d',
                                         limit_uptime='1d', last_seen_at=timezone.now())
        self.assertEqual(vr.needs_send(v)[2], 'limit')

    def test_synced_voucher_costs_no_router_call(self):
        self.voucher()
        with mock.patch('core.voucher_ready.push_exact') as push:
            _, d = self.check('ABC123')
        push.assert_not_called()
        self.assertTrue(d['success'])

    def test_unreachable_router_never_blocks_the_login(self):
        self.voucher(mikrotik_sync_status='Pending')
        with mock.patch('core.voucher_ready.push_exact', return_value=False):
            _, d = self.check('ABC123')
        self.assertTrue(d['success'])
        self.assertNotIn('ready', d)

    def test_push_exact_uses_plan_whatever_its_letter_case(self):
        VoucherPlan.objects.create(business=self.biz, name='1day', price=10, duration_minutes=1440, max_devices=2,
                                   mikrotik_profile_name='plan-1day')
        v = self.voucher(plan_name='1Day', mikrotik_sync_status='Pending')
        svc = mock.MagicMock()
        svc.connect.return_value = svc
        svc.upsert_voucher.return_value = ('created', '*1')
        with mock.patch('core.mikrotik.MikroTikService', return_value=svc):
            self.assertTrue(vr.push_exact(v.pk))
        self.assertEqual(svc.upsert_voucher.call_args[0][1], 'plan-1day')
        svc.ensure_hotspot_profile.assert_called_with('plan-1day', 2, '')
        v.refresh_from_db()
        self.assertEqual(v.mikrotik_sync_status, 'Synced')
        self.assertTrue(vr._confirmed_at(v.pk))

    def test_push_now_sends_time_up_voucher_switched_off(self):
        v = self.voucher(used_at=timezone.now() - timedelta(days=2), expires_at=timezone.now() - timedelta(days=1),
                         mikrotik_sync_status='Pending')
        svc = mock.MagicMock()
        svc.connect.return_value = svc
        svc.upsert_voucher.return_value = ('updated', '*1')
        from core.voucher_push import push_now
        with mock.patch('core.mikrotik.MikroTikService', return_value=svc):
            push_now(self.router, [v.pk])
        self.assertTrue(svc.upsert_voucher.call_args.kwargs['disabled'])

    def test_failed_push_is_released_for_the_sweeper(self):
        v = self.voucher(mikrotik_sync_status='Pending')
        fl.claim(v)
        svc = mock.MagicMock()
        svc.connect.side_effect = OSError('no route')
        from core.voucher_push import push_now
        with mock.patch('core.mikrotik.MikroTikService', return_value=svc):
            self.assertEqual(push_now(self.router, [v.pk]), (0, 1))
        self.assertIsNone(cache.get(f'tt:vfast:{v.pk}'), 'the sweeper must be able to retry it at once')


# ─────────────────────────── self-healing login ───────────────────────────

class HealLink(Base):
    def token(self, v):
        return vr.token_for(v)

    def test_invalid_user_resends_even_a_synced_voucher(self):
        v = self.voucher()
        fl.claim(v)                    # sent a moment ago: the duplicate guard must not stop the fix
        d = self.post('heal', {'t': self.token(v), 'error': 'invalid username or password'})
        self.assertTrue(d['retry'])
        self.assertEqual(d['code'], 'ABC123')
        self.assertGreaterEqual(d['wait'], 1)
        self.assertIn('ABC123', self.queued_users())
        self.assertTrue(VoucherEvent.objects.filter(voucher=v, reason='Ready to browse').exists())

    def test_no_more_sessions_clears_the_old_session(self):
        v = self.voucher()
        with mock.patch('core.device_lock.release_stale', return_value=15) as rel:
            d = self.post('heal', {'t': self.token(v), 'error': 'no more sessions are allowed for user ABC123',
                                   'mac': 'AA:BB:CC:00:00:09'})
        rel.assert_called_once()
        self.assertEqual(rel.call_args[0][1], 'AA:BB:CC:00:00:09')
        self.assertTrue(d['retry'])
        self.assertGreaterEqual(d['wait'], 15)

    def test_invalid_mac_unpins_on_the_router(self):
        v = self.voucher()
        d = self.post('heal', {'t': self.token(v), 'error': 'invalid mac address'})
        self.assertTrue(d['retry'])
        self.assertTrue(AgentCommand.objects.filter(router=self.router, kind='hotspot_user_mac',
                                                    params__mac='00:00:00:00:00:00').exists())

    def test_permanent_device_pin_is_respected(self):
        from core.models_permanent_device import PermanentVoucherDevice
        v = self.voucher()
        b = VoucherDeviceBinding.objects.create(business=self.biz, voucher=v, slot_no=1, current_mac='AA:BB:CC:00:00:01')
        PermanentVoucherDevice.objects.create(binding=b)
        d = self.post('heal', {'t': self.token(v), 'error': 'invalid mac address'})
        self.assertFalse(d['retry'])
        self.assertFalse(AgentCommand.objects.filter(router=self.router, kind='hotspot_user_mac').exists())

    def test_radius_and_blocked_vouchers_are_not_retried(self):
        v = self.voucher()
        self.assertFalse(self.post('heal', {'t': self.token(v), 'error': 'RADIUS server is not responding'})['retry'])
        Voucher.objects.filter(pk=v.pk).update(status='disabled')
        self.assertFalse(self.post('heal', {'t': self.token(v), 'error': 'invalid username or password'})['retry'])
        self.assertEqual(self.queued_users(), [])

    def test_router_busy_just_retries(self):
        v = self.voucher()
        d = self.post('heal', {'t': self.token(v), 'error': 'already authorizing, retry later'})
        self.assertTrue(d['retry'])
        self.assertEqual(self.queued_users(), [])

    def test_heal_is_rate_limited_per_voucher(self):
        v = self.voucher()
        answers = [self.post('heal', {'t': self.token(v), 'error': 'already authorizing'})['retry'] for _ in range(6)]
        self.assertEqual(answers[:4], [True] * 4)
        self.assertFalse(answers[-1])


class HealApi(Base):
    link = False

    def test_unreachable_router_says_so_instead_of_looping(self):
        v = self.voucher()
        with mock.patch('core.voucher_ready.push_exact', return_value=False):
            d = self.post('heal', {'t': vr.token_for(v), 'error': 'invalid username or password'})
        self.assertFalse(d['retry'])
        self.assertIn('could not reach', d['message'])

    def test_invalid_mac_unpins_with_the_push(self):
        v = self.voucher()
        with mock.patch('core.voucher_ready.push_exact', return_value=True) as push:
            d = self.post('heal', {'t': vr.token_for(v), 'error': 'invalid mac address'})
        self.assertTrue(d['retry'])
        self.assertTrue(push.call_args.kwargs.get('unpin'))


# ─────────────────────────── background retries ───────────────────────────

class Sweeper(Base):
    link = False

    def test_failed_pushes_are_retried_per_router(self):
        a = self.voucher('AAA111', mikrotik_sync_status='Pending')
        b = self.voucher('BBB222', mikrotik_sync_status='Error')
        c = self.voucher('CCC333', mikrotik_sync_status='Pending')
        self.voucher('DDD444')                          # synced: nothing to do
        fl.claim(c)                                     # just sent by the fast lane: not twice
        with mock.patch('core.tasks.push_vouchers_task.delay') as delay:
            self.assertEqual(vr.sweep(), 2)
        router_id, ids, swept = delay.call_args[0]
        self.assertEqual((router_id, sorted(ids), swept), (self.router.pk, sorted([a.pk, b.pk]), True))

    def test_link_routers_are_left_to_their_check_in(self):
        link_router = Router.objects.create(business=self.biz, name='L', ip_address='', connection_mode='agent')
        RouterAgent.objects.create(router=link_router, token_hash='k' * 64, last_seen_at=timezone.now())
        self.voucher('LLL111', router=link_router, mikrotik_sync_status='Pending')
        with mock.patch('core.tasks.push_vouchers_task.delay') as delay:
            vr.sweep()
        delay.assert_not_called()

    def test_router_that_failed_backs_off(self):
        v = self.voucher('AAA111', mikrotik_sync_status='Pending')
        vr.after_sweep_push(self.router.pk, [v.pk], sent=0, failed=1)
        first = cache.get(f'tt:vsweep:back:{self.router.pk}')
        vr.after_sweep_push(self.router.pk, [v.pk], sent=0, failed=1)
        self.assertEqual(cache.get(f'tt:vsweep:back:{self.router.pk}'), first * 2)
        cache.delete('tt:vsweep:pass')
        with mock.patch('core.tasks.push_vouchers_task.delay') as delay:
            vr.sweep()
        delay.assert_not_called()
        vr.after_sweep_push(self.router.pk, [v.pk], sent=1, failed=0)
        self.assertIsNone(cache.get(f'tt:vsweep:wait:{self.router.pk}'))

    def test_task_sends_members_with_the_member_sender(self):
        m = self.voucher('member1', login_type='member', password='pw', mikrotik_sync_status='Error')
        v = self.voucher('AAA111', mikrotik_sync_status='Pending')
        from core import tasks
        with mock.patch('core.voucher_push.push_now', return_value=(1, 0)) as plain, \
                mock.patch('core.member_sync.push_members', return_value=(1, 0)) as members:
            self.assertEqual(tasks.push_vouchers_task(self.router.pk, [m.pk, v.pk], True), (2, 0))
        self.assertEqual(plain.call_args[0][1], [v.pk])
        self.assertEqual(set(members.call_args[0][0]), {m.pk})


# ─────────────────────────── members: no stuck thread after a rollback ───────────────────────────

class MemberSyncAfterRollback(Base):
    def test_member_change_is_flushed_even_with_leftover_pending_ids(self):
        from core import member_sync
        fl._local.pending = {987654}                # left behind by a rolled-back transaction
        with self.captureOnCommitCallbacks() as callbacks:
            member_sync._schedule_many([1])
        self.assertTrue(any(cb is fl._flush for cb in callbacks), 'member changes would wait for the next full sync')


# ─────────────────────────── TapTap Link: honest per-voucher results ───────────────────────────

class LinkPartialResults(Base):
    def batch(self):
        a = self.voucher('AAA111', mikrotik_sync_status='Pending')
        b = self.voucher('BBB222', mikrotik_sync_status='Pending')
        fl.sync_now([a.pk, b.pk])
        return a, b, AgentCommand.objects.get(router=self.router, kind='hotspot_users', status='queued')

    def test_batch_reports_refused_codes_by_name(self):
        a, b, cmd = self.batch()
        script = agent.build_response(self.router, URL)
        self.assertIn(':local ttBad', script)                     # inside [:parse "…"], so quotes are escaped
        self.assertIn(':set ttBad', script)
        self.assertIn(f'/api/agent/v1/ack?c={cmd.pk}&n={agent.nonce(cmd)}&s=part&r=', script)
        self.assertLess(script.index('s=part&r='), script.index('&s=ok'), 'refusals must arrive before the batch ok')

    def test_partial_ack_marks_only_refused_codes(self):
        a, b, cmd = self.batch()
        agent.build_response(self.router, URL)
        self.assertTrue(agent.handle_ack(cmd.pk, agent.nonce(cmd), 'part', ',BBB222'))
        a.refresh_from_db(); b.refresh_from_db(); cmd.refresh_from_db()
        self.assertEqual((a.mikrotik_sync_status, b.mikrotik_sync_status), ('Synced', 'Error'))
        self.assertEqual(cmd.status, 'done')
        self.assertIn('BBB222', cmd.result)
        self.assertTrue(vr._confirmed_at(a.pk))
        self.assertFalse(vr._confirmed_at(b.pk))
        self.assertFalse(agent.handle_ack(cmd.pk, agent.nonce(cmd), 'ok'), 'the late ok must not overwrite it')

    def test_body_without_report_is_unchanged(self):
        a, b, cmd = self.batch()
        self.assertNotIn('ttBad', agent.command_body(cmd))


# ─────────────────────────── TapTap Link: turbo check-in and batch size ───────────────────────────

class LinkTurboAndChunks(Base):
    def test_turbo_while_customer_waits(self):
        v = self.voucher(mikrotik_sync_status='Pending')
        self.state(v.code)
        self.assertTrue(agent.build_response(self.router, URL).endswith(agent.TURBO_LINE))

    def test_no_turbo_without_voucher_work(self):
        self.voucher()
        agent.queue(self.router, 'ping', {})
        self.assertNotIn(agent.TURBO_LINE, agent.build_response(self.router, URL))

    def test_no_turbo_on_old_heartbeat(self):
        RouterAgent.objects.filter(pk=self.agent.pk).update(script_version=3)
        self.router.refresh_from_db()
        v = self.voucher(mikrotik_sync_status='Pending')
        agent.mark_urgent(self.router)
        fl.sync_now([v.pk])
        self.assertNotIn(agent.TURBO_LINE, agent.build_response(self.router, URL))

    def test_turbo_is_capped_per_minute(self):
        agent.mark_urgent(self.router)
        got = [agent._wants_turbo(self.router) for _ in range(agent.TURBO_PER_MINUTE + 3)]
        self.assertEqual(got.count(True), agent.TURBO_PER_MINUTE)

    def test_batch_size_follows_routeros_version(self):
        self.assertEqual(agent.voucher_chunk(self.router), agent.LINK_CHUNK_V7)
        RouterAgent.objects.filter(pk=self.agent.pk).update(ros_version='6.49.10 (long-term)')
        self.router.refresh_from_db()
        self.assertEqual(agent.voucher_chunk(self.router), agent.LINK_CHUNK_V6)

    def test_batch_shrinks_when_unanswered_and_grows_back(self):
        users = [{'n': f'V{i:03}'} for i in range(20)]
        cmd = AgentCommand.objects.create(router=self.router, kind='hotspot_users', params={'users': users, 'ids': []},
                                          expires_at=timezone.now() + timedelta(hours=1))
        agent._release_voucher_batches([cmd], 'no answer', unanswered=True)
        self.assertEqual(agent.voucher_chunk(self.router), 10)
        agent._learn_chunk(self.router, 10, answered=True)
        self.assertEqual(agent.voucher_chunk(self.router), 12)

    def test_fast_lane_respects_router_batch_size(self):
        RouterAgent.objects.filter(pk=self.agent.pk).update(ros_version='6.48.6')
        self.router.refresh_from_db()
        vs = [self.voucher(f'C{i:04}', mikrotik_sync_status='Pending') for i in range(12)]
        fl.sync_now([v.pk for v in vs])
        self.assertEqual(len(self.queued_users()), agent.LINK_CHUNK_V6)
        self.assertEqual(Voucher.objects.filter(mikrotik_sync_status='Pending').count(), 12 - agent.LINK_CHUNK_V6)


# ─────────────────────────── pages and portal refresh ───────────────────────────

class PagesAndRefresh(Base):
    link = False

    def test_router_page_knows_ready_and_heal_addresses_and_the_username(self):
        from core.views_studio import _export_html
        html = _export_html(self.page, base='https://t.example', router=self.router)
        self.assertIn('https://t.example/p/b-login/ready/', html)
        self.assertIn('https://t.example/p/b-login/heal/', html)
        self.assertIn('<div id="tp-user" hidden>$(username)</div>', html)
        self.assertIn('ctx.mt.username=u.textContent', html)

    def test_hosted_page_knows_ready_and_heal_addresses(self):
        r = self.client.get('/p/b-login/')
        self.assertContains(r, '/p/b-login/ready/')
        self.assertContains(r, '/p/b-login/heal/')

    @override_settings(SITE_URL='https://t.example')
    def test_outdated_router_pages_are_refreshed_once(self):
        PortalDeployment.objects.create(business=self.biz, router=self.router, status='installed', version='old')
        with mock.patch('core.portal_deploy.deploy', return_value=(True, 'ok')) as deploy:
            self.assertEqual(vr.refresh_portals(), 1)
            self.assertEqual(vr.refresh_portals(), 0)
        deploy.assert_called_once_with(self.router)

    @override_settings(SITE_URL='')
    def test_no_refresh_without_site_url(self):
        PortalDeployment.objects.create(business=self.biz, router=self.router, status='installed', version='old')
        with mock.patch('core.portal_deploy.deploy') as deploy:
            self.assertEqual(vr.refresh_portals(), 0)
        deploy.assert_not_called()


# ─────────────────────────── add-on views are really used by their URLs ───────────────────────────

class UrlAlignment(TestCase):
    def test_login_paths_and_security_use_their_add_ons(self):
        from django.urls import resolve
        from core.url_alignment import stale
        self.assertEqual(stale(), [], 'an add-on view wrapper is not reachable through its URL')
        for path in ('/p/x/state/', '/p/x/check/'):
            self.assertTrue(getattr(resolve(path).func, '_taptap_ready', False), path)
        self.assertEqual(resolve('/security/').func.__name__, 'security_with_voucher_entry')

    def test_decorated_urls_are_left_alone(self):
        from django.urls import resolve
        func = resolve('/settings/features/').func
        from core import views_features
        self.assertIn(views_features.business_features, [func, getattr(func, '__wrapped__', None)])

    def test_expired_voucher_gets_its_page_on_the_router_login(self):
        user = User.objects.create_user('o2', password='x')
        biz = Business.objects.create(user=user, business_name='B2', owner_name='O', phone='1',
                                      trial_ends_at=timezone.now() + timedelta(days=7))
        page = PortalPage.objects.create(business=biz, name='L', slug='b2-login', kind='login', is_published=True)
        Voucher.objects.create(business=biz, code='OLD999', plan_name='Day', used_at=timezone.now() - timedelta(days=3),
                               expires_at=timezone.now() - timedelta(days=2))
        r = self.client.post(f'/p/{page.slug}/state/', json.dumps({'code': 'OLD999'}), content_type='text/plain')
        self.assertEqual(r.json().get('kind'), 'expired', 'the expired-voucher page add-on must answer')
