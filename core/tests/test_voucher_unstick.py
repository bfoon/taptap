"""Vouchers cut off / refused by the router although TapTap has time left: limits from the router's own counter,
"Can't connect?" findings, Fix it with a router read-back, and the automatic fix from the login page."""
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core import agent, mobile_auth, router_time as rt, voucher_fastlane as fl, voucher_ready as vr, voucher_unstick as vu
from core.models import (AgentCommand, Business, PortalPage, Router, RouterAgent, RouterHotspotUser, Voucher,
                         VoucherDeviceBinding, VoucherEvent)
from core.models_team import TeamMember

CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'unstick'}}
URL = 'https://taptap.example'


@override_settings(CACHES=CACHE, AUTH_EMAIL_OTP=False)
class Base(TestCase):
    link = True

    def setUp(self):
        cache.clear()
        fl._local.pending = set()
        self.owner = User.objects.create_user('owner@x.gm', 'owner@x.gm', 'pw-123456')
        self.biz = Business.objects.create(user=self.owner, business_name='Kerr Sering', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        if self.link:
            self.router = Router.objects.create(business=self.biz, name='KerrSering', connection_mode='agent', status='Online')
            self.agent = RouterAgent.objects.create(router=self.router, token_hash='u' * 64, last_seen_at=timezone.now(),
                                                    enrolled_at=timezone.now(), script_version=9, ros_version='7.15.3 (stable)')
        else:
            self.router = Router.objects.create(business=self.biz, name='KerrSering', ip_address='10.0.0.1', username='a', password='p')
        vr.RUN_INLINE = True
        vu.RUN_INLINE = True

    def tearDown(self):
        vr.RUN_INLINE = False
        vu.RUN_INLINE = False

    def voucher(self, code='KS1234', used=True, **kw):
        now = timezone.now()
        data = dict(business=self.biz, router=self.router, code=code, plan_name='Day', duration_minutes=1440, max_devices=1,
                    mikrotik_sync_status='Synced', status='active')
        if used:
            data.update(used_at=now - timedelta(hours=4), expires_at=now + timedelta(hours=20))
        data.update(kw)
        v = Voucher.objects.create(**data)
        AgentCommand.objects.filter(router=self.router).delete()
        fl.unclaim([v.pk])
        return v

    def cmds(self, kind):
        return AgentCommand.objects.filter(router=self.router, kind=kind, status='queued')


class LimitFromTheRoutersOwnCounter(Base):
    def test_used_voucher_gets_add_and_router_side_limit(self):
        v = self.voucher()
        row, _prof = fl.user_row(v)
        self.assertAlmostEqual(row['add'], 20 * 3600, delta=120)          # time left × 1 device slot
        cmd = SimpleNamespace(kind='hotspot_users', params={'users': [row], 'profiles': []}, router=self.router, pk=1)
        body = agent._catalogue_body(cmd)
        self.assertTrue(body.startswith(agent.REL_FN))                    # defined once per batch
        self.assertIn('[/ip hotspot user get $i uptime]', agent.REL_FN)    # the router's own counter
        self.assertIn('[/ip hotspot active get $x uptime]', agent.REL_FN)  # + sessions still running
        self.assertIn(f'$ttRel n="KS1234" s="{row["add"]}s"', body)
        self.assertIn(f'limit-uptime="{row["lim"]}"', body)               # absolute value stays as the fallback
        unused = SimpleNamespace(kind='hotspot_users', params={'users': [fl.user_row(self.voucher('NEW002', used=False))[0]],
                                                               'profiles': []}, router=self.router, pk=2)
        self.assertNotIn('ttRel', agent._catalogue_body(unused))

    def test_unused_disabled_and_imported_keep_absolute_limits(self):
        self.assertNotIn('add', fl.user_row(self.voucher('NEW001', used=False))[0])
        self.assertNotIn('add', fl.user_row(self.voucher('OFF001', status='disabled'))[0])
        self.assertNotIn('add', fl.user_row(self.voucher('OLD001', source='router'))[0])
        self.assertIsNone(rt.add_seconds(self.voucher('UNL001', used=True, duration_minutes=0, expires_at=None)))
        late = self.voucher('LATE01', expires_at=timezone.now() - timedelta(minutes=1))
        self.assertIsNone(rt.add_seconds(late))

    def test_reset_devices_writes_the_limit_from_the_router(self):
        from core import voucher_history as vh
        v = self.voucher()
        vh.reset_devices(v, self.owner, 'new phone')
        heal = self.cmds('hotspot_users_heal').first()
        self.assertIsNotNone(heal)
        self.assertTrue(heal.params['users'][0].get('add'))
        self.assertIn('$ttRel n="KS1234"', agent._catalogue_body(heal))

    def test_router_time_correction_uses_the_counter_and_fits_a_check_in(self):
        for i in range(6):
            v = self.voucher(f'RT{i:04d}')
            RouterHotspotUser.objects.create(business=self.biz, router=self.router, username=v.code, uptime='4h', limit_uptime='4h10m', is_present=True)
        n = rt.repair_link(self.router)
        self.assertEqual(n, 6)
        cmds = list(self.cmds('hotspot_users_limit'))
        self.assertEqual(len(cmds), 2)                                   # 4 + 2
        for c in cmds:
            self.assertTrue(all(u.get('add') for u in c.params['users']))
            script = agent.wrap(c, URL, 'no')
            self.assertLessEqual(len(script), agent.MAX_SCRIPT, 'must travel isolated in a normal check-in')


class FindingsAndFix(Base):
    def test_findings_name_the_real_reasons(self):
        v = self.voucher()
        RouterHotspotUser.objects.create(business=self.biz, router=self.router, username=v.code, uptime='9h', limit_uptime='8h', is_present=True,
                                         mac_address='AA:BB:CC:00:00:07')
        VoucherDeviceBinding.objects.create(business=self.biz, voucher=v, slot_no=1, current_mac='AA:BB:CC:00:00:01')
        agent.queue(self.router, 'hotspot_user_set', {'name': v.code, 'disabled': True}, label='old disable')
        vu.note_refusal(v, 'user KS1234 has reached uptime limit', 'AA:BB:CC:00:00:01')
        keys = {f['key']: f for f in vu.findings(v)}
        self.assertEqual(keys['router_time']['level'], 'bad')
        self.assertIn('pinned', keys)
        self.assertIn('slots', keys)
        self.assertIn('pending', keys)
        self.assertIn('uptime limit', keys['refused']['text'])
        self.assertEqual(vu.blocking(v), '')

    def test_switched_off_vouchers_are_never_fixed(self):
        v = self.voucher(status='disabled')
        self.assertIn('Enable', vu.blocking(v))
        out = vu.unstick(v, self.owner)
        self.assertFalse(out['ok'])
        self.assertFalse(AgentCommand.objects.filter(router=self.router, kind='voucher_unstick').exists())
        late = self.voucher('LATE02', expires_at=timezone.now() - timedelta(minutes=5))
        self.assertIn('time', vu.blocking(late))

    def test_fix_on_link_queues_check_and_reads_the_router_back(self):
        v = self.voucher()
        VoucherDeviceBinding.objects.create(business=self.biz, voucher=v, slot_no=1, current_mac='AA:BB:CC:00:00:01')
        self.client.force_login(self.owner)
        d = self.client.post(f'/vouchers/{v.pk}/fix/', json.dumps({'reason': 'cannot connect'}), content_type='application/json').json()
        self.assertTrue(d['waiting'], d)
        cmd = AgentCommand.objects.get(pk=d['cmd'])
        self.assertEqual(cmd.kind, 'voucher_unstick')
        self.assertTrue(cmd.params['free'] and cmd.params['any'])
        self.assertIn('AA:BB:CC:00:00:01', cmd.params['macs'])
        self.assertFalse(VoucherDeviceBinding.objects.filter(voucher=v).exists(), 'Reset devices ran')
        script = agent.wrap(cmd, URL, 'no')
        self.assertLessEqual(len(script), agent.MAX_SCRIPT)
        self.assertIn('limit-uptime=($ttLu +', vu.link_body(cmd, URL, 'no', 'n'))
        self.assertIn('/api/agent/v1/ack?c=', vu.link_body(cmd, URL, 'no', 'n'))
        # the router has not answered yet
        st = self.client.get(f'/vouchers/{v.pk}/fix/?cmd={cmd.pk}').json()
        self.assertEqual(st['status'], 'waiting')
        # the router's read-back arrives with the acknowledgement
        self.assertTrue(agent.handle_ack(cmd.pk, agent.nonce(cmd), 'ok',
                                         'u=1~up=4h0m5s~lim=1d0h0m~dis=false~mac=00:00:00:00:00:00~act=0~fr=1~bl=0~sh=1'))
        st = self.client.get(f'/vouchers/{v.pk}/fix/?cmd={cmd.pk}').json()
        self.assertEqual(st['status'], 'done')
        self.assertTrue(st['ok'], st)
        self.assertTrue(any('Any phone' in x for x in st['lines']))
        self.assertTrue(VoucherEvent.objects.filter(voucher=v, reason='Voucher fix').exists())

    def test_read_back_that_is_still_wrong_says_so(self):
        v = self.voucher()
        ok, lines = vu.verdict(v, vu.read_state('u=1~up=9h~lim=8h~dis=true~mac=AA:BB:CC:00:00:07~act=0~fr=0~bl=1~sh=3'))
        self.assertFalse(ok)
        text = ' '.join(lines)
        self.assertIn('Still switched off', text)
        self.assertIn('used up', text)
        self.assertIn('IP Bindings', text)
        self.assertEqual(vu.verdict(v, vu.read_state('u=0'))[0], False)

    def test_a_newer_disable_cancels_a_waiting_fix(self):
        v = self.voucher()
        cmd = agent.queue(self.router, 'voucher_unstick', vu.params_for(v), label='fix')
        agent.queue(self.router, 'hotspot_user_set', {'name': v.code, 'disabled': True}, label='Disable', user=self.owner)
        cmd.refresh_from_db()
        self.assertEqual(cmd.status, 'cancelled')

    def test_only_voucher_support_may_fix(self):
        v = self.voucher()
        viewer = User.objects.create_user('view@x.gm', 'view@x.gm', 'pw-123456')
        TeamMember.objects.create(business=self.biz, user=viewer, is_active=True, role='viewer')
        self.client.force_login(viewer)
        r = self.client.post(f'/vouchers/{v.pk}/fix/', '{}', content_type='application/json')
        self.assertIn(r.status_code, (302, 403))
        self.assertFalse(AgentCommand.objects.filter(kind='voucher_unstick').exists())

    def test_network_tools_finds_a_code(self):
        v = self.voucher()
        self.client.force_login(self.owner)
        d = self.client.get('/vouchers/fix/find/?code=ks1234').json()
        self.assertTrue(d['ok'])
        self.assertEqual(d['voucher']['id'], v.pk)
        self.assertEqual(self.client.get('/vouchers/fix/find/?code=NOPE').status_code, 404)
        page = self.client.get(f'/network-tools/?router={self.router.pk}')
        self.assertContains(page, 'A voucher can')
        page = self.client.get(f'/vouchers/{v.pk}/')
        self.assertContains(page, "Can't connect?")


class AutomaticFromTheLoginPage(Base):
    def setUp(self):
        super().setUp()
        self.page = PortalPage.objects.create(business=self.biz, name='Login', slug='ks-login', kind='login', is_published=True)

    def heal(self, v, error):
        return self.client.post(f'/p/{self.page.slug}/heal/', json.dumps({'t': vr.token_for(v), 'error': error, 'mac': 'AA:BB:CC:00:00:01'}),
                                content_type='text/plain').json()

    def test_second_refusal_runs_the_router_fix_once(self):
        v = self.voucher()
        self.heal(v, 'invalid username or password')
        self.assertFalse(AgentCommand.objects.filter(kind='voucher_unstick').exists(), 'one refusal: the normal re-send only')
        self.heal(v, 'invalid username or password')
        fixes = AgentCommand.objects.filter(kind='voucher_unstick')
        self.assertEqual(fixes.count(), 1)
        p = fixes.first().params
        self.assertFalse(p['free'], 'nobody is signed out by the automatic fix')
        self.assertFalse(p['any'])
        self.heal(v, 'invalid username or password')
        self.assertEqual(AgentCommand.objects.filter(kind='voucher_unstick').count(), 1, 'at most once per 10 minutes')

    def test_used_up_router_counter_is_fixed_at_once(self):
        v = self.voucher()
        self.heal(v, 'user KS1234 has reached uptime limit')
        self.assertEqual(AgentCommand.objects.filter(kind='voucher_unstick').count(), 1)


class DirectApi(Base):
    link = False

    def test_api_fix_reads_the_counter_and_sets_the_limit_above_it(self):
        v = self.voucher()
        state = {'user': {'id': '*1', 'name': v.code, 'uptime': '5h', 'limit-uptime': '4h', 'disabled': 'true',
                          'mac-address': 'AA:BB:CC:00:00:07', 'profile': 'taptap-day'}}
        sets = []

        class Res:
            def __init__(self, path):
                self.path = path

            def get(self, **kw):
                if self.path == '/ip/hotspot/user':
                    return [dict(state['user'])]
                return []

            def set(self, **kw):
                sets.append((self.path, kw))
                if 'limit_uptime' in kw:
                    state['user']['limit-uptime'] = kw['limit_uptime']
                if 'disabled' in kw:
                    state['user']['disabled'] = 'false'
                if 'mac_address' in kw:
                    state['user']['mac-address'] = kw['mac_address']

            def remove(self, **kw):
                pass

            def add(self, **kw):
                return '*2'

        class Svc:
            def __init__(self, *a, **k):
                pass

            def connect(self):
                return self

            def close(self):
                pass

            def resource(self, path):
                return Res(path)

            def safe_get(self, path, **kw):
                if path == '/ip/hotspot/active':
                    return [{'user': v.code, 'uptime': '30m', 'mac-address': 'AA:BB:CC:00:00:01'}]
                return []

            def release_devices(self, code, macs=()):
                return 0

            def ensure_hotspot_profile(self, *a):
                pass

            def upsert_voucher(self, *a, **k):
                return 'updated', '*1'

        with mock.patch('core.mikrotik.MikroTikService', Svc):
            s = vu._api(v, free=True, any_mac=True)
        lim = [kw['limit_uptime'] for _p, kw in sets if 'limit_uptime' in kw][-1]
        self.assertAlmostEqual(rt.secs(lim), 5 * 3600 + 30 * 60 + 20 * 3600, delta=180)   # counter + session + time left
        ok, lines = vu.verdict(v, s)
        self.assertTrue(ok, lines)


class MobileApp(Base):
    def test_check_and_fix_from_the_app(self):
        v = self.voucher()
        _dev, token = mobile_auth.create_device(self.owner, name='Pixel', platform='android')
        h = {'HTTP_AUTHORIZATION': f'Bearer {token}'}
        d = self.client.get(f'/api/mobile/v1/vouchers/{v.pk}/', **h).json()
        self.assertTrue(d['voucher']['can']['fix'])
        d = self.client.post(f'/api/mobile/v1/vouchers/{v.pk}/', json.dumps({'action': 'check'}), content_type='application/json', **h).json()
        self.assertTrue(d['ok'] and d['fixable'], d)
        d = self.client.post(f'/api/mobile/v1/vouchers/{v.pk}/', json.dumps({'action': 'fix'}), content_type='application/json', **h).json()
        self.assertTrue(d['ok'] and d['waiting'], d)
        st = self.client.get(f'/api/mobile/v1/vouchers/{v.pk}/?fix={d["cmd"]}', **h).json()
        self.assertEqual(st['status'], 'waiting')
