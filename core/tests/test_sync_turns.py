"""Syncs must get through even while vouchers keep flowing; a refused code must not be re-sent for ever; phones'
connection tests always need a login; a phone logged back in by its MAC cookie is not left in a sign-in window."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from core import agent as link, probe_guard
from core.models import AgentCommand, Business, PortalPage, Router, RouterAgent, RouterSyncJob, Voucher

URL = 'https://t.example'


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('owner', password='x')
        self.biz = Business.objects.create(user=self.user, business_name='B', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.router = Router.objects.create(business=self.biz, name='TapTap K', ip_address='', connection_mode='agent', status='Online')
        self.agent = RouterAgent.objects.create(router=self.router, token_hash='k' * 64, last_seen_at=timezone.now(),
                                                enrolled_at=timezone.now(), script_version=9, ros_version='7.15')

    def vouchers(self, n, status='Pending'):
        return [Voucher.objects.create(business=self.biz, router=self.router, code=f'V{i:04}', plan_name='Day', mikrotik_sync_status=status)
                for i in range(n)]

    def checkin(self):
        """One router check-in: queue the next voucher batch, build the reply, the router answers every command."""
        link.push_pending_vouchers(self.router)
        script = link.build_response(self.router, URL)
        sent = list(AgentCommand.objects.filter(router=self.router, status='sent'))
        for c in sent:
            link.handle_ack(c.pk, link.nonce(c), 'ok')
        return script, {c.kind for c in sent}


class FairTurns(Base):
    def test_other_work_gets_through_during_a_voucher_backlog(self):
        self.vouchers(200)                                         # a big backlog: 8 batches of 25
        other = link.queue(self.router, 'ping', {}, label='waiting job')
        AgentCommand.objects.filter(pk=other.pk).update(created_at=timezone.now() - timedelta(minutes=1))
        kinds = [self.checkin()[1] for _ in range(4)]
        self.assertIn('hotspot_users', kinds[0], 'vouchers still go first')
        self.assertIn('ping', kinds[1], 'the next check-in is the other work’s turn — it used to wait for the whole backlog')
        self.assertIn('hotspot_users', kinds[2], 'and vouchers continue right after')

    def test_sync_tables_are_delivered_while_vouchers_flow(self):
        from core.agent_inventory import start_agent_inventory_sync
        self.vouchers(200)
        job = RouterSyncJob.objects.create(business=self.biz, router=self.router, status='running')
        start_agent_inventory_sync(job)
        AgentCommand.objects.filter(router=self.router, kind='inventory_piece').update(created_at=timezone.now() - timedelta(minutes=1))
        delivered = set()
        for _ in range(6):
            delivered |= self.checkin()[1]
        self.assertIn('inventory_piece', delivered, 'this is “the sync commands never reached the router”')

    def test_vouchers_alone_are_not_slowed(self):
        self.vouchers(60)
        kinds = [self.checkin()[1] for _ in range(3)]
        self.assertTrue(all('hotspot_users' in k for k in kinds), 'with nothing else waiting, every check-in carries vouchers')

    def test_fresh_other_work_does_not_take_the_turn_at_once(self):
        self.vouchers(60)
        link.queue(self.router, 'ping', {})                          # queued just now: vouchers keep going this time
        self.checkin()
        self.assertIn('hotspot_users', self.checkin()[1])


class RefusedCodes(Base):
    def refuse(self, v):
        link.push_pending_vouchers(self.router)
        link.build_response(self.router, URL)
        cmd = AgentCommand.objects.filter(router=self.router, kind='hotspot_users', status='sent').latest('pk')
        link.handle_ack(cmd.pk, link.nonce(cmd), 'part', ',' + v.code)

    def test_a_code_refused_three_times_stops_being_resent(self):
        v = self.vouchers(1)[0]
        for _ in range(3):
            self.refuse(v)
        v.refresh_from_db()
        self.assertTrue(v.mikrotik_sync_error.startswith(link.REFUSED_MARK), v.mikrotik_sync_error)
        self.assertEqual(link.push_pending_vouchers(self.router), 0, 'it was re-sent at every check-in, starving every sync')

    def test_sync_asked_by_the_owner_tries_once_more(self):
        v = self.vouchers(1)[0]
        for _ in range(3):
            self.refuse(v)
        self.assertEqual(link.clear_refused(self.router), 1)
        v.refresh_from_db()
        self.assertEqual(v.mikrotik_sync_status, 'Pending')
        self.assertEqual(link.push_pending_vouchers(self.router), 1)

    def test_owner_sync_clears_refused_codes(self):
        from core.agent_inventory import start_agent_inventory_sync
        job = RouterSyncJob.objects.create(business=self.biz, router=self.router, status='running', requested_by=self.user)
        with mock.patch('core.agent.clear_refused') as clear:
            start_agent_inventory_sync(job)
        clear.assert_called_once_with(self.router)

    def test_good_answer_resets_strikes(self):
        v = self.vouchers(1)[0]
        self.refuse(v); self.refuse(v)
        Voucher.objects.filter(pk=v.pk).update(mikrotik_sync_status='Pending')
        self.checkin()
        self.assertIsNone(cache.get(f'tt:link:strikes:{v.pk}'))

    def test_a_silent_router_never_marks_codes_refused(self):
        v = self.vouchers(1)[0]
        for _ in range(5):                                         # batches that are never answered (router offline)
            link.push_pending_vouchers(self.router); link.build_response(self.router, URL)
            cmds = list(AgentCommand.objects.filter(router=self.router, kind='hotspot_users', status='sent'))
            link._release_voucher_batches(cmds, 'no answer', unanswered=True)
            AgentCommand.objects.filter(pk__in=[c.pk for c in cmds]).update(status='expired')
        v.refresh_from_db()
        self.assertFalse(v.mikrotik_sync_error.startswith(link.REFUSED_MARK), 'an outage must never stop vouchers for good')


class SyncFailureReason(Base):
    def test_says_whether_the_router_was_silent_or_busy(self):
        from core.agent_inventory import _no_data_reason
        job = RouterSyncJob.objects.create(business=self.biz, router=self.router, status='running')
        RouterAgent.objects.filter(pk=self.agent.pk).update(last_seen_at=job.created_at - timedelta(minutes=5))
        self.router.refresh_from_db()
        self.assertIn('has not checked in since', _no_data_reason([], self.router, job))
        RouterAgent.objects.filter(pk=self.agent.pk).update(last_seen_at=job.created_at + timedelta(minutes=3))
        self.router.refresh_from_db()
        self.assertIn('busy sending it other commands', _no_data_reason([], self.router, job))


class ProbeGuard(Base):
    def test_rules_cover_every_phone_test_and_go_on_top(self):
        s = probe_guard.script()
        for host, _ in probe_guard.PROBES:
            self.assertIn(host, s)
        self.assertIn('action=deny', s)
        self.assertIn('place-before=$f', s)
        self.assertEqual(link._quote_aware_depth(s, 0), 0)

    def test_portal_install_and_link_command_carry_it(self):
        from core.portal_deploy import install_script
        self.assertIn(probe_guard.COMMENT, install_script(URL, 'T', ['login'], 't.example', 'yes', ''))
        cmd = link.queue(self.router, 'probe_guard', {'v': 1})
        self.assertIn('walled-garden', link.command_body(cmd))

    def test_free_sites_trigger_it(self):
        from core import free_access
        with mock.patch('core.free_access._apply_router', return_value=(True, 'ok')), mock.patch('core.probe_guard.apply') as guard:
            free_access.apply_router(self.router)
        guard.assert_called_once()

    def test_each_router_once(self):
        with mock.patch('core.probe_guard.apply', return_value=(True, 'ok')) as apply:
            probe_guard.ensure_all(); probe_guard.ensure_all()
        self.assertEqual(apply.call_count, 1)

    def test_which_free_sites_hold_a_test(self):
        self.assertTrue(probe_guard.covers_test('google.com'))
        self.assertTrue(probe_guard.covers_test('apple.com'))
        self.assertFalse(probe_guard.covers_test('facebook.com'))
        self.assertFalse(probe_guard.covers_test('maps.google.com'))


class AfterLoginPage(Base):
    def test_redirect_and_status_pages_know_the_first_address(self):
        from core.views_studio import _export_html
        for kind in ('redirect', 'status'):
            page = PortalPage.objects.create(business=self.biz, name=kind, slug=f'b-{kind}', kind=kind, is_published=True)
            html = _export_html(page, base=URL, router=self.router)
            self.assertIn('<div id="tp-orig" hidden>$(link-orig)</div>', html, kind)
            self.assertIn('ctx.mt.linkOrig=o.textContent', html)
