"""Security Fix dialogs: plan time for never-expiring router users, voucher sharing (with MAC changes),
and the default admin account.

Run:  DB_ENGINE=sqlite python manage.py test core.test_security_fixes
"""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import device_lock, plan_limits, security_admin as sa
from .mikrotik import MikroTikService
from .models import (AgentCommand, Business, Router, RouterConfigSnapshot, RouterDevice, RouterHotspotUser, Voucher,
                     VoucherDeviceBinding, VoucherEvent, VoucherPlan)


class FakeRes:
    def __init__(self, rows): self.rows = rows
    def get(self, **f): return [r for r in self.rows if all(str(r.get(k)) == str(v) for k, v in f.items())]
    def set(self, id, **kw):
        for r in self.rows:
            if r['id'] == id:
                r.update({k.replace('_', '-'): v for k, v in kw.items()})
    def add(self, **kw):
        kw = {k.replace('_', '-'): v for k, v in kw.items()}; kw['id'] = f'*{len(self.rows) + 1}'; self.rows.append(kw); return kw['id']


class FakeMT(MikroTikService):
    """The real MikroTikService methods on an in-memory router."""
    def __init__(self, router, timeout=None, store=None):
        super().__init__(router, timeout)
        self.store = store
    def connect(self): return self
    def close(self): pass
    def resource(self, path): return FakeRes(self.store.setdefault(path, []))
    def configuration_snapshot(self):
        return {'sections': {'Users': {'rows': [dict(r) for r in self.store.get('/user', [])], 'count': 0}}, 'load_balancing': {}, 'captured_at': timezone.now().isoformat()}


@override_settings(AUTH_EMAIL_OTP=False)
class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('s@x.com', 's@x.com', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='B', owner_name='O', phone='1', trial_ends_at=timezone.now() + timedelta(days=7))
        self.r = Router.objects.create(business=self.biz, name='hAP', ip_address='10.0.0.1', username='taptap', password='old', status='Online')
        self.store = {}
        self.client.force_login(self.owner)

    def mt(self):
        store = self.store
        return mock.patch('core.mikrotik.MikroTikService', side_effect=lambda router, timeout=None: FakeMT(router, timeout, store))


class PlanLimitTests(Base):
    def setUp(self):
        super().setUp()
        VoucherPlan.objects.create(business=self.biz, name='24 Hours', mikrotik_profile_name='daily', price=25, duration_minutes=1440)
        VoucherPlan.objects.create(business=self.biz, name='Staff', mikrotik_profile_name='staff', price=0, duration_minutes=0)
        now = timezone.now()
        for name, prof, up in (('K1', 'daily', '2h'), ('K2', 'daily', '1d3h'), ('S1', 'staff', '9d'), ('X1', 'mystery', '1h')):
            RouterHotspotUser.objects.create(business=self.biz, router=self.r, username=name, profile=prof, uptime=up, limit_uptime='',
                                             is_present=True, disabled=False, source='mikrotik', last_seen_at=now)
            self.store.setdefault('/ip/hotspot/user', []).append({'id': f'*{name}', 'name': name, 'profile': prof, 'uptime': up})
        self.k1 = Voucher.objects.create(business=self.biz, router=self.r, code='K1', plan_name='daily', source='mikrotik',
                                         duration_minutes=1440, used_at=now - timedelta(hours=3))      # as imported: no end yet

    def test_preview(self):
        g = {x['profile']: x for x in plan_limits.preview(self.r)}
        self.assertEqual((g['daily']['minutes'], g['daily']['limit'], g['daily']['users'], g['daily']['over']), (1440, '1d', ['K1', 'K2'], 1))
        self.assertEqual((g['staff']['minutes'], g['staff']['why']), (0, 'Its plan is unlimited'))
        self.assertEqual(g['mystery']['why'], 'No TapTap plan uses this profile')

    def test_apply_on_router_and_in_taptap(self):
        with self.mt(), mock.patch('core.linkops.uses_link', return_value=False):
            res = plan_limits.apply(self.r, user=self.owner, overrides={'mystery': 180})
        users = {u['name']: u for u in self.store['/ip/hotspot/user']}
        self.assertEqual((users['K1'].get('limit-uptime'), users['K2'].get('limit-uptime'), users['X1'].get('limit-uptime')), ('1d', '1d', '3h'))
        self.assertNotIn('limit-uptime', users['S1'])                                   # unlimited plan on purpose: left alone
        self.assertEqual((res['set'], res['skipped']), (3, 1))
        self.assertEqual(RouterHotspotUser.objects.get(username='K1').limit_uptime, '1d')
        self.k1.refresh_from_db()
        self.assertEqual((self.k1.duration_minutes, self.k1.expires_at), (1440, self.k1.used_at + timedelta(minutes=1440)))
        self.assertFalse(plan_limits.unlimited_users(self.r).exclude(profile='staff').exists())   # finding clears

    def test_link_queues_a_limit_only_command(self):
        from .agent import command_body
        with mock.patch('core.linkops.uses_link', return_value=True), mock.patch('core.linkops.ensure_online'):
            plan_limits.apply(self.r)
        cmd = AgentCommand.objects.get(kind='hotspot_users_limit')
        body = command_body(cmd)
        self.assertIn('/ip hotspot user set [find name="K1"] limit-uptime="1d"', body)
        self.assertNotIn(' add ', body)                                               # never re-creates a user
        from .agent import queue
        with self.assertRaises(ValueError):
            queue(self.r, 'hotspot_users_limit', {'users': [{'n': 'K1', 'lim': '1d; /system reset'}]})

    def test_automatic_mode(self):
        with self.mt(), mock.patch('core.linkops.uses_link', return_value=False):
            self.assertIsNone(plan_limits.auto(self.r))                               # off by default
            Business.objects.filter(pk=self.biz.pk).update(auto_plan_limits=True); self.r.business.refresh_from_db()
            self.assertEqual(plan_limits.auto(self.r)['set'], 2)
            self.assertIsNone(plan_limits.auto(self.r))                               # at most once a minute

    def test_endpoint_and_finding(self):
        from .security import audit_business
        RouterConfigSnapshot.objects.create(router=self.r, sections={'Users': {'rows': []}, 'IP services': {'rows': [{'name': 'api', 'disabled': 'false', 'port': '8728'}]}})
        f = [x for x in audit_business(self.biz) if x['key'] == f'r{self.r.id}:unlimited-users'][0]
        self.assertEqual(f['action'], 'plan-limits')
        url = reverse('security_plan_limits', args=[self.r.pk])
        self.assertEqual(len(self.client.get(url).json()['groups']), 3)
        with self.mt(), mock.patch('core.linkops.uses_link', return_value=False):
            d = self.client.post(url, json.dumps({'auto': True, 'overrides': {'mystery': 60}}), content_type='application/json').json()
        self.assertTrue(d['success']); self.assertTrue(d['auto'])
        self.assertTrue(Business.objects.get(pk=self.biz.pk).auto_plan_limits)


class SharingTests(Base):
    def setUp(self):
        super().setUp()
        self.v = Voucher.objects.create(business=self.biz, router=self.r, code='ONE00001', plan_name='Day', duration_minutes=1440, max_devices=1)
        VoucherDeviceBinding.objects.create(business=self.biz, voucher=self.v, slot_no=1, current_mac='AA:BB:CC:00:00:01', locked_by='router')

    def host(self, mac, name):
        RouterDevice.objects.create(router=self.r, device_key=mac, mac_address=mac, hostname=name, is_online=True)

    def claim(self, mac, online=()):
        with mock.patch('core.device_lock._router_mac'):
            return device_lock.claim(self.v, mac=mac, source='router', hints={'online_macs': set(online), 'router_id': self.r.pk})

    def test_phone_with_a_new_private_mac_keeps_its_slot(self):
        out = self.claim('DA:11:22:33:44:55')                          # private (randomised) MAC, old one offline
        self.assertEqual(out.status, 'moved')
        b = VoucherDeviceBinding.objects.get(voucher=self.v)
        self.assertEqual((b.current_mac, b.previous_mac), ('DA:11:22:33:44:55', 'AA:BB:CC:00:00:01'))
        self.assertTrue(VoucherEvent.objects.filter(voucher=self.v, event='device', detail__kind='mac_swap').exists())

    def test_a_second_phone_online_at_the_same_time_is_refused(self):
        self.assertEqual(self.claim('DA:11:22:33:44:55', online={'AA:BB:CC:00:00:01'}).status, 'denied')

    def test_same_wifi_name_even_with_a_normal_mac(self):
        self.host('AA:BB:CC:00:00:01', 'Fatou-Galaxy-A14'); self.host('10:20:30:40:50:60', 'Fatou-Galaxy-A14')
        self.assertEqual(self.claim('10:20:30:40:50:60').status, 'moved')

    def test_different_phone_with_a_normal_mac_is_refused(self):
        self.host('AA:BB:CC:00:00:01', 'Fatou-Galaxy-A14'); self.host('10:20:30:40:50:61', 'Lamin-iPhone')
        self.assertEqual(self.claim('10:20:30:40:50:61').status, 'denied')

    def test_generic_names_do_not_count(self):
        self.host('AA:BB:CC:00:00:01', 'android'); self.host('10:20:30:40:50:62', 'android')
        self.assertEqual(self.claim('10:20:30:40:50:62').status, 'denied')

    def test_daily_allowance_for_private_macs(self):
        for i in range(device_lock.SWAP_PER_DAY):
            self.assertEqual(self.claim(f'DA:00:00:00:00:0{i}').status, 'moved')
        self.assertEqual(self.claim('DA:00:00:00:00:09').status, 'denied')       # passing the code around all day: refused

    def test_two_phones_cannot_take_turns_on_one_slot(self):
        self.assertEqual(self.claim('DA:11:22:33:44:55').status, 'moved')                                  # phone B took the slot (A offline)
        out = self.claim('AA:BB:CC:00:00:01', online={'DA:11:22:33:44:55'})                                # A comes back while B is online
        self.assertEqual(out.status, 'denied')

    def test_live_pass_kicks_the_second_phone_only(self):
        # Both phones moving data at the same time: the second one is disconnected on the second reading in a row.
        def rows(b):
            return [(self.v, {'mac-address': 'AA:BB:CC:00:00:01', 'id': '*1', 'bytes-in': b}), (self.v, {'mac-address': 'DA:99:99:99:99:99', 'id': '*2', 'bytes-in': b})]
        with mock.patch('core.device_lock.kick', return_value=True) as k, mock.patch('core.device_lock._router_mac'):
            self.assertEqual(device_lock.enforce_sessions(self.r, rows(10_000)), 0)
            self.assertEqual(device_lock.enforce_sessions(self.r, rows(600_000)), 1)
        self.assertEqual(k.call_args.args[2], 'DA:99:99:99:99:99')
        self.assertEqual(VoucherDeviceBinding.objects.get(voucher=self.v).current_mac, 'AA:BB:CC:00:00:01')

    def test_refused_phones_are_not_reported_as_sharing(self):
        from .models import DeviceSignature
        from .security import audit_business
        b = VoucherDeviceBinding.objects.get(voucher=self.v); b.device_token_hash = 'fp-owner'; b.save()
        for fp in ('fp-owner', 'fp-friend'):
            DeviceSignature.objects.create(business=self.biz, fingerprint=fp, vouchers=['ONE00001'])
        keys = {f['key'] for f in audit_business(self.biz)}
        self.assertNotIn('sig:voucher-sharing', keys); self.assertIn('sig:sharing-blocked', keys)
        Business.objects.filter(pk=self.biz.pk).update(device_lock=False); self.biz.refresh_from_db()
        sharing = [f for f in audit_business(self.biz) if f['key'] == 'sig:voucher-sharing']
        self.assertEqual(sharing[0]['action'], 'sharing')

    def test_stop_it_now_turns_the_lock_on(self):
        Business.objects.filter(pk=self.biz.pk).update(device_lock=False)
        with mock.patch('core.live.watch_business', return_value=[{'router_id': self.r.pk, 'locked_out': 2}]):
            d = self.client.post(reverse('security_sharing')).json()
        self.assertTrue(d['success']); self.assertEqual(d['kicked'], 2)
        self.assertTrue(Business.objects.get(pk=self.biz.pk).device_lock)


class AdminAccountTests(Base):
    def snapshot(self, users):
        RouterConfigSnapshot.objects.update_or_create(router=self.r, defaults={'sections': {'Users': {'rows': users}}})
        self.store['/user'] = [dict(u, id=f'*{i}') for i, u in enumerate(users)]

    def test_password_rules(self):
        for bad, why in (('short1A!', '12'), ('alllowercaseletters', 'three'), ('Admin2026!!xyz', 'admin'), ('Good-Pass-2026', None)):
            if why:
                with self.assertRaises(sa.AdminFixError, msg=bad):
                    sa.check_password(bad, bad)
            else:
                self.assertEqual(sa.check_password(bad, bad), bad)
        with self.assertRaises(sa.AdminFixError):
            sa.check_password('Good-Pass-2026', 'Good-Pass-2027')

    def test_state(self):
        self.snapshot([{'name': 'admin', 'group': 'full', 'disabled': 'false'}, {'name': 'musa', 'group': 'full', 'disabled': 'false'}])
        with mock.patch('core.linkops.uses_link', return_value=False):
            st = sa.state(self.r)
        self.assertEqual((st['can_disable'], st['other_full_users'], st['needs_new_user']), (True, ['musa'], False))
        Router.objects.filter(pk=self.r.pk).update(username='admin'); self.r.refresh_from_db()
        with mock.patch('core.linkops.uses_link', return_value=False):
            self.assertFalse(sa.state(self.r)['can_disable'])                      # TapTap would cut itself off
        Router.objects.filter(pk=self.r.pk).update(username='taptap'); self.r.refresh_from_db()
        with mock.patch('core.linkops.uses_link', return_value=True):
            self.assertIn('TapTap Link', sa.state(self.r)['why_not_disable'])

    def test_new_password_when_taptap_logs_in_as_admin(self):
        Router.objects.filter(pk=self.r.pk).update(username='admin'); self.r.refresh_from_db()
        self.snapshot([{'name': 'admin', 'group': 'full', 'disabled': 'false'}])
        with self.mt(), mock.patch('core.linkops.uses_link', return_value=False):
            msg = sa.set_password(self.r, 'Strong-Pass-2026', 'Strong-Pass-2026', user=self.owner)
        self.assertEqual(self.store['/user'][0]['password'], 'Strong-Pass-2026')
        self.assertEqual(Router.objects.get(pk=self.r.pk).password, 'Strong-Pass-2026')       # TapTap keeps working
        self.assertIn('TapTap now logs in with the new password', msg)

    def test_disable_creates_your_account_first(self):
        self.snapshot([{'name': 'admin', 'group': 'full', 'disabled': 'false'}])
        with self.mt(), mock.patch('core.linkops.uses_link', return_value=False):
            with self.assertRaises(sa.AdminFixError):
                sa.disable_admin(self.r)                                          # no other full user and no name given
            self.assertEqual(self.store['/user'][0].get('disabled'), 'false')
            msg = sa.disable_admin(self.r, new_name='musa', new_password='Strong-Pass-2026', new_again='Strong-Pass-2026')
        users = {u['name']: u for u in self.store['/user']}
        self.assertEqual((users['musa']['group'], users['admin']['disabled']), ('full', 'yes'))
        self.assertIn('musa', msg)

    def test_link_password_is_wiped_after_the_router_confirms(self):
        from .agent import command_body, handle_ack, nonce
        Router.objects.filter(pk=self.r.pk).update(username='admin'); self.r.refresh_from_db()
        with mock.patch('core.linkops.uses_link', return_value=True), mock.patch('core.linkops.ensure_online'):
            sa.set_password(self.r, 'Strong-Pass-2026', 'Strong-Pass-2026')
        cmd = AgentCommand.objects.get(kind='admin_password')
        self.assertEqual(command_body(cmd), '/user set [find name="admin"] password="Strong-Pass-2026"')
        handle_ack(cmd.pk, nonce(cmd), 'ok')
        cmd.refresh_from_db()
        self.assertNotIn('Strong-Pass-2026', json.dumps(cmd.params))
        self.assertEqual(Router.objects.get(pk=self.r.pk).password, 'Strong-Pass-2026')

    def test_endpoint(self):
        self.snapshot([{'name': 'admin', 'group': 'full', 'disabled': 'false'}])
        url = reverse('security_admin_account', args=[self.r.pk])
        with mock.patch('core.linkops.uses_link', return_value=False):
            self.assertTrue(self.client.get(url).json()['needs_new_user'])
            bad = self.client.post(url, json.dumps({'action': 'password', 'password': 'weak', 'password2': 'weak'}), content_type='application/json')
        self.assertEqual(bad.status_code, 400)
