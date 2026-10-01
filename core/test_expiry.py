"""Strict expiry: a voucher whose time runs out is switched off at once — TapTap-made or router-made.

Run:  DB_ENGINE=sqlite python manage.py test core.test_expiry
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from . import expiry
from .models import AgentCommand, Business, Router, RouterHotspotUser, SessionIncident, Voucher, VoucherEvent


class FakeSvc:
    """Just enough RouterOS for the live pass and the sweep."""
    def __init__(self, users=None, active=None):
        self.users = users if users is not None else []
        self.active = active if active is not None else []
        self.disabled, self.kicked = [], []

    def connect(self): return self
    def close(self): pass
    def hotspot_users(self): return [dict(u) for u in self.users]
    def active_users(self): return [dict(a) for a in self.active]
    def bindings(self): return []
    def hotspot_hosts(self): return []

    def disable_voucher(self, code):
        self.disabled.append(code)
        for u in self.users:
            if u['name'] == code:
                u['disabled'] = 'true'

    def reset_active_by_name(self, code):
        self.kicked.append(code)
        self.active = [a for a in self.active if a['user'] != code]


@override_settings(AUTH_EMAIL_OTP=False)
class ExpiryTests(TestCase):
    def setUp(self):
        cache.clear()
        owner = User.objects.create_user('e@x.com', 'e@x.com', 'pw')
        self.biz = Business.objects.create(user=owner, business_name='B', owner_name='O', phone='1', trial_ends_at=timezone.now() + timedelta(days=7),
                                           auto_enforce=False, enforce_grace_minutes=30, live_sync=False)   # strict must not depend on these
        self.r = Router.objects.create(business=self.biz, name='hAP', ip_address='10.0.0.1', username='u', password='p', status='Online')
        self.now = timezone.now()

    def v(self, code, **kw):
        base = dict(business=self.biz, router=self.r, code=code, plan_name='Day', duration_minutes=1440, source='taptap', status='active')
        base.update(kw)
        return Voucher.objects.create(**base)

    def sweep(self, svc=None, link=False):
        svc = svc or FakeSvc()
        with mock.patch('core.mikrotik.MikroTikService', return_value=svc), mock.patch('core.linkops.uses_link', return_value=link):
            return expiry.sweep(recheck=True), svc

    # ── the sweep: every business, live sync off, no session needed ──
    def test_time_up_is_switched_off_at_once(self):
        gone = self.v('GONE0001', used_at=self.now - timedelta(days=2), expires_at=self.now - timedelta(days=1))
        summary, svc = self.sweep()
        gone.refresh_from_db()
        self.assertEqual(gone.status, 'expired')
        self.assertEqual(svc.disabled, ['GONE0001']); self.assertEqual(svc.kicked, ['GONE0001'])
        ev = VoucherEvent.objects.get(voucher=gone, event='time_up')
        self.assertEqual((ev.source, ev.status_before, ev.status_after), ('auto', 'active', 'expired'))
        self.assertEqual(summary['expired'], 1)

    def test_missing_end_is_computed(self):
        v = self.v('NOEND001', used_at=self.now - timedelta(hours=30), duration_minutes=1440)      # no expires_at stored
        self.sweep()
        v.refresh_from_db()
        self.assertEqual(v.status, 'expired')
        self.assertEqual(v.expires_at, v.used_at + timedelta(minutes=1440))

    def test_what_must_not_expire(self):
        running = self.v('RUN00001', used_at=self.now - timedelta(hours=1), expires_at=self.now + timedelta(hours=23))
        unused = self.v('NEW00001')
        unlimited = self.v('UNL00001', duration_minutes=0, used_at=self.now - timedelta(days=90))
        frozen = self.v('FRZ00001', used_at=self.now - timedelta(days=2), expires_at=self.now - timedelta(days=1), frozen_at=self.now)
        _, svc = self.sweep()
        for v in (running, unused, unlimited, frozen):
            v.refresh_from_db()
        self.assertEqual([running.status, unused.status, unlimited.status, frozen.status], ['active'] * 4)
        self.assertEqual(svc.disabled, [])

    def test_router_made_and_no_router(self):
        rm = self.v('RTR00001', source='mikrotik', used_at=self.now - timedelta(days=3), expires_at=self.now - timedelta(days=2))
        loose = self.v('LOOSE001', router=None, used_at=self.now - timedelta(days=3), expires_at=self.now - timedelta(days=2))
        _, svc = self.sweep()
        rm.refresh_from_db(); loose.refresh_from_db()
        self.assertEqual((rm.status, loose.status), ('expired', 'expired'))
        self.assertEqual(svc.disabled, ['RTR00001'])        # no router: marked expired in TapTap only

    def test_nothing_escapes_offline_router_is_retried(self):
        v = self.v('OFF00001', used_at=self.now - timedelta(days=2), expires_at=self.now - timedelta(days=1))
        with mock.patch('core.mikrotik.MikroTikService', side_effect=OSError('timed out')), mock.patch('core.linkops.uses_link', return_value=False):
            expiry.sweep(recheck=True)
        v.refresh_from_db(); self.assertEqual(v.status, 'expired')          # expired in TapTap even while offline
        cache.delete(f'tt:exp:api:{self.r.pk}')                               # a minute later…
        _, svc = self.sweep()
        self.assertEqual(svc.disabled, ['OFF00001'])                          # …the router gets it

    def test_re_enabled_on_the_router_is_switched_off_again(self):
        v = self.v('BACK0001', status='expired', used_at=self.now - timedelta(days=2), expires_at=self.now - timedelta(days=1))
        RouterHotspotUser.objects.create(business=self.biz, router=self.r, username='BACK0001', disabled=False, is_present=True, last_seen_at=self.now)
        _, svc = self.sweep()
        self.assertEqual(svc.disabled, ['BACK0001'])
        cache.delete(f'tt:exp:api:{self.r.pk}')
        _, svc = self.sweep()                                                 # confirmed disabled now: left alone
        self.assertEqual(svc.disabled, [])

    # ── TapTap Link ──
    def test_link_batch_confirm_and_requeue(self):
        from .agent import handle_ack, nonce
        for i in range(3):
            self.v(f'LNK0000{i}', used_at=self.now - timedelta(days=2), expires_at=self.now - timedelta(days=1))
        with mock.patch('core.linkops.ensure_online'):
            self.sweep(link=True)
            cmd = AgentCommand.objects.get(kind='hotspot_users_disable')
            self.assertEqual((sorted(cmd.params['names']), cmd.params['disabled'], cmd.params['reason']), (['LNK00000', 'LNK00001', 'LNK00002'], True, 'expired'))
            self.sweep(link=True)
            self.assertEqual(AgentCommand.objects.filter(kind='hotspot_users_disable').count(), 1)   # not flooded
            handle_ack(cmd.pk, nonce(cmd), 'failed')                          # router refused: try again at once
            self.sweep(link=True)
            cmd2 = AgentCommand.objects.filter(kind='hotspot_users_disable').exclude(pk=cmd.pk).get()
            handle_ack(cmd2.pk, nonce(cmd2), 'ok')
            self.sweep(link=True)
            self.assertEqual(AgentCommand.objects.filter(kind='hotspot_users_disable').count(), 2)   # confirmed: done

    def test_link_session_of_expired_voucher_is_disconnected_at_once(self):
        from .agent import ingest_sessions
        v = self.v('LSES0001', used_at=self.now - timedelta(days=2), expires_at=self.now - timedelta(days=1))
        with mock.patch('core.agent.queue') as q:
            ingest_sessions(self.r, [{'user': 'LSES0001', 'mac-address': 'AA:BB:CC:00:00:01', 'uptime': '5m'}], self.now)
        self.assertIn('disconnect', [c.args[1] for c in q.call_args_list])    # auto-enforce off, 30 min grace: still at once
        v.refresh_from_db(); self.assertEqual(v.status, 'expired')

    # ── the live pass (API routers, every 15 s) ──
    def watch(self, svc):
        from .live import watch_router
        with mock.patch('core.live.MikroTikService', return_value=svc):
            return watch_router(self.r, force=True)

    def test_live_pass_switches_off_and_kicks(self):
        self.v('LIVE0001', used_at=self.now - timedelta(days=2), expires_at=self.now - timedelta(days=1))
        svc = FakeSvc(users=[{'name': 'LIVE0001', 'profile': 'Day', 'disabled': 'false', 'uptime': '2h', 'limit-uptime': '1d'}],
                      active=[{'user': 'LIVE0001', 'mac-address': 'AA:BB:CC:00:00:02', 'uptime': '10m', 'address': '10.5.50.2'}])
        res = self.watch(svc)
        self.assertEqual(res['time_up'], 1)
        self.assertEqual((svc.disabled, svc.kicked), (['LIVE0001'], ['LIVE0001']))
        self.assertEqual(Voucher.objects.get(code='LIVE0001').status, 'expired')
        self.assertFalse(SessionIncident.objects.filter(status='open').exists())   # handled, not left as an incident

    def test_uptime_used_up_on_the_router(self):
        v = self.v('UPTM0001', source='mikrotik', used_at=self.now - timedelta(hours=2), expires_at=self.now + timedelta(hours=20))
        svc = FakeSvc(users=[{'name': 'UPTM0001', 'profile': 'Day', 'disabled': 'false', 'uptime': '1d', 'limit-uptime': '1d'}])
        self.watch(svc)
        v.refresh_from_db()
        self.assertEqual(v.status, 'expired'); self.assertEqual(svc.disabled, ['UPTM0001'])
        self.assertIn('uptime limit', VoucherEvent.objects.get(voucher=v, event='time_up').reason)

    def test_router_cannot_bring_an_expired_voucher_back(self):
        v = self.v('STCK0001', source='mikrotik', status='expired', used_at=self.now - timedelta(days=2), expires_at=self.now - timedelta(days=1))
        svc = FakeSvc(users=[{'name': 'STCK0001', 'profile': 'Day', 'disabled': 'false', 'uptime': '3h', 'limit-uptime': '1d'}])   # re-enabled in WinBox
        self.watch(svc)
        v.refresh_from_db()
        self.assertEqual(v.status, 'expired'); self.assertEqual(svc.disabled, ['STCK0001'])

    def test_add_time_reopens_it(self):
        from . import voucher_history as vh
        v = self.v('ADDT0001', status='expired', used_at=self.now - timedelta(days=2), expires_at=self.now - timedelta(days=1))
        with mock.patch('core.voucher_history._router_apply', return_value=('Direct API', 'ok', True)):
            vh.enable(v, add_minutes=1440)
        v.refresh_from_db(); self.assertEqual(v.status, 'active')
        _, svc = self.sweep()
        v.refresh_from_db(); self.assertEqual(v.status, 'active'); self.assertEqual(svc.disabled, [])

    def test_recheck_of_old_ones_runs_once_a_minute_new_ones_at_once(self):
        old = self.v('OLD00001', status='expired', used_at=self.now - timedelta(days=5), expires_at=self.now - timedelta(days=4))
        RouterHotspotUser.objects.create(business=self.biz, router=self.r, username='OLD00001', disabled=False, is_present=True, last_seen_at=self.now)
        svc = FakeSvc()
        with mock.patch('core.mikrotik.MikroTikService', return_value=svc), mock.patch('core.linkops.uses_link', return_value=False):
            expiry.sweep()                                            # first pass of the minute: full re-check
            self.assertEqual(svc.disabled, ['OLD00001'])
            RouterHotspotUser.objects.filter(username='OLD00001').update(disabled=False)   # re-enabled again
            cache.delete(f'tt:exp:api:{self.r.pk}')
            self.v('NEW00009', used_at=self.now - timedelta(days=2), expires_at=self.now - timedelta(seconds=5))
            expiry.sweep()                                            # same minute: only the newly expired one
            self.assertEqual(svc.disabled, ['OLD00001', 'NEW00009'])
            cache.delete('tt:exp:recheck:all'); cache.delete(f'tt:exp:api:{self.r.pk}')
            expiry.sweep()                                            # next minute: the old one again
            self.assertEqual(svc.disabled[-1], 'OLD00001')



class FreeDevicesTests(TestCase):
    """When time runs out the phone must see the login page again (iPhones only look for it when they join Wi-Fi)."""
    def setUp(self):
        cache.clear()
        owner = User.objects.create_user('f@x.com', 'f@x.com', 'pw')
        self.biz = Business.objects.create(user=owner, business_name='B', owner_name='O', phone='1', trial_ends_at=timezone.now() + timedelta(days=7))
        self.r = Router.objects.create(business=self.biz, name='hAP', ip_address='10.0.0.1', username='u', password='p', status='Online')
        self.now = timezone.now()

    def test_release_devices_clears_session_cookie_host_and_wifi(self):
        from .mikrotik import MikroTikService
        store = {
            '/ip/hotspot/active': [{'id': '*a', 'user': 'IPH00001', 'mac-address': 'aa:bb:cc:00:00:01'}],
            '/ip/hotspot/cookie': [{'id': '*c', 'user': 'IPH00001', 'mac-address': 'AA:BB:CC:00:00:01'},
                                   {'id': '*d', 'user': 'OTHER001', 'mac-address': 'AA:BB:CC:00:00:09'}],
            '/ip/hotspot/host': [{'id': '*h1', 'mac-address': 'AA:BB:CC:00:00:01'}, {'id': '*h2', 'mac-address': 'AA:BB:CC:00:00:09'}],
            '/interface/wireless/registration-table': [{'id': '*w', 'mac-address': 'AA:BB:CC:00:00:01'}, {'id': '*x', 'mac-address': '11:22:33:44:55:66'}],
        }

        class Res:
            def __init__(self, path):
                if path not in store and 'registration-table' in path:
                    raise Exception('no such command prefix')     # radio package not installed
                self.rows = store.setdefault(path, [])
            def get(self, **f): return [r for r in self.rows if all(r.get(k) == v for k, v in f.items())]
            def remove(self, id): self.rows[:] = [r for r in self.rows if r['id'] != id]

        class Svc(MikroTikService):
            def resource(self, path): return Res(path)

        dropped = Svc(self.r).release_devices('IPH00001')
        self.assertEqual(dropped, 1)
        self.assertEqual(store['/ip/hotspot/active'], [])
        self.assertEqual([c['user'] for c in store['/ip/hotspot/cookie']], ['OTHER001'])       # others untouched
        self.assertEqual([h['id'] for h in store['/ip/hotspot/host']], ['*h2'])
        self.assertEqual([w['id'] for w in store['/interface/wireless/registration-table']], ['*x'])

    def test_expiry_frees_the_phone(self):
        from .models import VoucherDeviceBinding
        v = Voucher.objects.create(business=self.biz, router=self.r, code='IPH00002', plan_name='Day', duration_minutes=1440,
                                   used_at=self.now - timedelta(days=2), expires_at=self.now - timedelta(days=1))
        VoucherDeviceBinding.objects.create(business=self.biz, voucher=v, slot_no=1, current_mac='DA:00:00:00:00:02')
        freed = []

        class Svc(FakeSvc):
            def release_devices(s, code, macs=()):
                freed.append((code, set(macs)))
                return 1

            def resource(s, path):
                class R:
                    def get(self_, **k): return []
                return R()

        svc = Svc(users=[{'name': 'IPH00002', 'disabled': 'false', 'uptime': '2h', 'limit-uptime': '1d'}],
                  active=[{'user': 'IPH00002', 'mac-address': 'AA:BB:CC:00:00:02'}])
        expiry.enforce_on_router(self.r, svc, svc.users, svc.active, self.now)
        self.assertEqual(freed, [('IPH00002', {'AA:BB:CC:00:00:02', 'DA:00:00:00:00:02'})])

    def test_already_expired_phone_with_a_cookie_left_is_freed(self):
        Voucher.objects.create(business=self.biz, router=self.r, code='OLD00002', plan_name='Day', status='expired',
                               used_at=self.now - timedelta(days=5), expires_at=self.now - timedelta(days=4))
        freed = []

        class Svc(FakeSvc):
            def release_devices(s, code, macs=()):
                freed.append(code); return 0

            def resource(s, path):
                class R:
                    def get(self_, **k): return [{'user': 'OLD00002', 'mac-address': 'AA:BB:CC:00:00:03'}] if path == '/ip/hotspot/cookie' else []
                return R()

        svc = Svc(users=[{'name': 'OLD00002', 'disabled': 'true'}])
        expiry.enforce_on_router(self.r, svc, svc.users, [], self.now)
        self.assertEqual(freed, ['OLD00002'])

    def test_link_script_frees_devices_and_runs_on_any_routeros(self):
        from types import SimpleNamespace
        from . import agent
        command_body = agent.command_body          # as installed at start-up (core.hotspot_recovery wraps it)
        body = command_body(SimpleNamespace(kind='hotspot_users_disable', pk=1, params={'names': ['IPH00001'], 'disabled': True, 'reason': 'expired'}))
        self.assertIn('/ip hotspot cookie remove $c', body)
        self.assertIn('/ip hotspot host remove [find mac-address=$m]', body)
        self.assertIn(':parse ($t . " remove [find mac-address=" . $m . "]")', body)
        outside = body.replace('"/interface wifi registration-table"', '').replace('"/interface wifiwave2 registration-table"', '')
        self.assertNotIn('/interface wifi', outside)            # never a bare v7-only menu: v6 routers would reject the whole script
        freeze = command_body(SimpleNamespace(kind='hotspot_users_disable', pk=2, params={'names': ['IPH00001'], 'disabled': True}))
        self.assertNotIn('$drop', freeze)                       # freezing keeps the existing behaviour
        one = command_body(SimpleNamespace(kind='hotspot_user_set', pk=3, params={'name': 'IPH00001', 'disabled': True}))
        self.assertIn('/ip hotspot user set [find name="IPH00001"] disabled=yes', one)
        self.assertIn('$drop m=$m', one)                          # a disabled voucher frees its phone too
