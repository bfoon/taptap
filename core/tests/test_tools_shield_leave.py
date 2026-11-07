"""Network tools on a busy TapTap Link router, the member shield, and leaving a business you were added to."""
import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core import agent, mobile_auth
from core.models import AgentCommand, Business, Router, RouterAgent, Voucher
from core.models_events import EventAlert
from core.models_nettools import NetTest
from core.models_team import TeamMember

CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'tsl'}}


@override_settings(CACHES=CACHE, AUTH_EMAIL_OTP=False)
class LinkGraceAndNetTools(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456', first_name='Awa')
        self.biz = Business.objects.create(user=self.owner, business_name='Kunda Net', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.r = Router.objects.create(business=self.biz, name='Mast', connection_mode='agent', status='Online')
        self.a = RouterAgent.objects.create(router=self.r, token_hash='e' * 64, enrolled_at=timezone.now(), last_seen_at=timezone.now())

    def seen(self, secs_ago):
        RouterAgent.objects.filter(pk=self.a.pk).update(last_seen_at=timezone.now() - timedelta(seconds=secs_ago))
        self.a.refresh_from_db()

    def test_one_rule_for_online(self):
        self.seen(70)                                   # a busy router: late, but within 6 check-ins / 90 s
        self.assertTrue(self.a.online)
        from core.linklive import link_state
        self.assertTrue(link_state(self.r)[0])
        self.seen(200)
        self.assertFalse(self.a.online)
        self.assertEqual(self.a.grace_seconds, 90)

    def test_network_tools_waits_for_a_late_router(self):
        self.client.force_login(self.owner)
        self.seen(240)                                  # quiet for 4 minutes: tools still send the test
        r = self.client.post('/network-tools/run/', json.dumps({'router': self.r.pk, 'kind': 'ping', 'target': '8.8.8.8'}),
                             content_type='application/json')
        d = json.loads(r.content)
        self.assertTrue(d['ok'], d)
        self.assertEqual(d['test']['status'], 'waiting', d)
        self.assertIn('next check-in', d['test']['result'].get('note', ''))
        self.assertTrue(AgentCommand.objects.filter(kind='nettest', router=self.r).exists())

    def test_network_tools_long_gone_router_says_so(self):
        self.client.force_login(self.owner)
        self.seen(3600)
        d = json.loads(self.client.post('/network-tools/run/', json.dumps({'router': self.r.pk, 'kind': 'ping', 'target': '8.8.8.8'}),
                                        content_type='application/json').content)
        self.assertEqual(d['test']['status'], 'failed')


@override_settings(CACHES=CACHE)
class MemberShield(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o2@x.gm', 'o2@x.gm', 'pw-123456')
        self.biz = Business.objects.create(user=self.owner, business_name='Kunda Net', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.r = Router.objects.create(business=self.biz, name='Mast', connection_mode='agent', status='Online')
        RouterAgent.objects.create(router=self.r, token_hash='f' * 64, enrolled_at=timezone.now(), last_seen_at=timezone.now())
        self.m = Voucher.objects.create(business=self.biz, router=self.r, code='MEMBER01', login_type='member',
                                        plan_name='Month', status='active', expires_at=timezone.now() + timedelta(days=10))
        self.v = Voucher.objects.create(business=self.biz, router=self.r, code='VOUCH001', plan_name='Day')

    def test_installed(self):
        self.assertTrue(getattr(agent.queue, '_taptap_member_shield', False))

    def test_automatic_disconnect_of_a_member_with_time_is_held_back(self):
        cmd = agent.queue(self.r, 'disconnect', {'user': 'member01'}, label='Disconnect MEMBER01')
        self.assertEqual(cmd.status, 'cancelled')
        self.assertIn('Member shield', cmd.result)
        cmd = agent.queue(self.r, 'hotspot_kick', {'user': 'MEMBER01', 'mac': 'AA:BB:CC:00:11:22'}, label='Remove extra device')
        self.assertEqual(cmd.status, 'cancelled')
        from core.member_shield import shield_stats
        self.assertEqual(shield_stats(self.biz, [self.m.pk])[self.m.pk]['count'], 2)

    def test_what_still_disconnects(self):
        self.assertEqual(agent.queue(self.r, 'disconnect', {'user': 'MEMBER01'}, label='x', user=self.owner).status, 'queued')   # a person
        self.assertEqual(agent.queue(self.r, 'disconnect', {'user': 'VOUCH001'}, label='x').status, 'queued')                  # a voucher
        Voucher.objects.filter(pk=self.m.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(agent.queue(self.r, 'disconnect', {'user': 'MEMBER01'}, label='x').status, 'queued')                  # time over
        Voucher.objects.filter(pk=self.m.pk).update(expires_at=timezone.now() + timedelta(days=1), status='disabled')
        self.assertEqual(agent.queue(self.r, 'disconnect', {'user': 'MEMBER01'}, label='x').status, 'queued')                  # switched off
        Voucher.objects.filter(pk=self.m.pk).update(status='active')
        self.assertEqual(agent.queue(self.r, 'disconnect', {'user': 'MEMBER01', 'force': True}, label='x').status, 'queued')   # explicit


@override_settings(CACHES=CACHE, AUTH_EMAIL_OTP=False)
class LeaveBusiness(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('boss@x.gm', 'boss@x.gm', 'pw-123456', first_name='Boss')
        self.biz = Business.objects.create(user=self.owner, business_name='Kunda  Net', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.other_owner = User.objects.create_user('b2@x.gm', 'b2@x.gm', 'pw-123456')
        self.biz2 = Business.objects.create(user=self.other_owner, business_name='Brikama Wi-Fi', owner_name='O', phone='2',
                                            trial_ends_at=timezone.now() + timedelta(days=30))
        self.musa = User.objects.create_user('musa@x.gm', 'musa@x.gm', 'pw-123456', first_name='Musa')
        self.tm = TeamMember.objects.create(business=self.biz, user=self.musa, is_active=True, role='viewer')

    def test_page_and_leave_only_business_signs_out(self):
        self.client.force_login(self.musa)
        page = self.client.get(f'/team/leave/{self.biz.pk}/')
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Leave Kunda')
        bad = self.client.post(f'/team/leave/{self.biz.pk}/', {'confirm_name': 'nope'})
        self.assertEqual(bad.status_code, 302)
        self.tm.refresh_from_db()
        self.assertTrue(self.tm.is_active)
        r = self.client.post(f'/team/leave/{self.biz.pk}/', {'confirm_name': 'kunda net'})
        self.assertRedirects(r, '/login/', fetch_redirect_response=False)
        self.tm.refresh_from_db()
        self.assertFalse(self.tm.is_active)                     # switched off, not deleted: records stay
        self.assertTrue(EventAlert.objects.filter(business=self.biz, user=self.owner, kind='team').exists())
        from core.models_chat import ChatMessage
        self.assertTrue(ChatMessage.objects.filter(body__contains='Musa left the team').exists())

    def test_staff_is_not_shown_as_owner_and_sees_leave(self):
        self.client.force_login(self.musa)
        page = self.client.get('/sales/today/')
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, f'/team/leave/{self.biz.pk}/')
        self.assertContains(page, 'View only')
        from core.team import attach_business, owned_business
        attach_business(self.musa, self.biz)
        self.assertIsNone(owned_business(self.musa))          # working in it is not owning it
        self.assertEqual(owned_business(self.owner), self.biz)

    def test_leave_one_of_two_moves_to_the_other(self):
        TeamMember.objects.create(business=self.biz2, user=self.musa, is_active=True, role='viewer')
        self.client.force_login(self.musa)
        r = self.client.post(f'/team/leave/{self.biz.pk}/', {'confirm_name': 'Kunda Net'})
        self.assertEqual(r.status_code, 302)
        self.assertNotEqual(r['Location'], '/login/')
        self.assertEqual(self.client.session.get('tt_business'), self.biz2.pk)

    def test_owner_cannot_leave_own_business(self):
        from core import team_leave as tl
        TeamMember.objects.create(business=self.biz, user=self.owner, is_active=True, role='admin')
        with self.assertRaises(tl.LeaveError):
            tl.leave(self.owner, self.biz.pk, 'Kunda Net')

    def test_app_leave(self):
        _dev, token = mobile_auth.create_device(self.musa, name='Pixel', platform='android')
        h = {'HTTP_AUTHORIZATION': f'Bearer {token}'}
        d = json.loads(self.client.post('/api/mobile/v1/business/leave/', json.dumps({'id': self.biz.pk, 'confirm': 'Kunda Net'}),
                                        content_type='application/json', **h).content)
        self.assertTrue(d['ok'], d)
        self.assertTrue(d['signed_out'])
        self.assertEqual(self.client.get('/api/mobile/v1/me/', **h).status_code, 401)


class WhyPeopleDrop(TestCase):
    def test_reasons_from_the_router_log(self):
        from core import nettools as nt
        body = (b't=5\n'
                b'l=12:01:02|MEMBER01 (10.5.50.12): logged out: keepalive timeout\n'
                b'l=12:05:00|->: ABC123 (10.5.50.9): logged out: lost dhcp lease\n'
                b'l=12:09:00|MEMBER01 (10.5.50.12): logged out: keepalive timeout\n'
                b'l=12:10:00|ZZZ999 (10.5.50.20): logged out: user request\n'
                b'l=12:11:00|user admin logged out from 1.2.3.4 via winbox\n')
        r = nt.parse_link('drops', {}, body)
        self.assertEqual(r['total'], 4)
        self.assertEqual(r['in_log'], 5)
        self.assertEqual(r['reasons'][0]['key'], 'keepalive')
        self.assertEqual(r['people'][0]['user'], 'MEMBER01')
        self.assertEqual(r['level'], 'warn')
        self.assertEqual(nt.clean_params('drops', {}), {'target': 'hotspot sign-outs'})

    def test_link_script_is_built(self):
        from types import SimpleNamespace
        from core import nettools as nt
        s = nt.link_body(SimpleNamespace(pk=7, params={'kind': 'drops', 'target': 'hotspot sign-outs'}), 'https://x', 'no', 'abc')
        self.assertIn('logged out', s)
        self.assertIn('/log find', s)
