"""Lighter syncs that leave the router free for vouchers:

* voucher time is corrected at 50 % and 75 % of a voucher, not at every pass — at once only when the router would
  cut a customer early, a voucher is not used yet, or has no end (core/router_time.py);
* those checkpoint corrections travel in the background lane on TapTap Link, behind every voucher change;
* a Direct API live pass reads the router's whole user table only when it is useful (core/router_load.py)."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core import agent, router_load, router_time as rt
from core.models import AgentCommand, Business, Router, RouterAgent, RouterHotspotUser, Voucher

DAY = 86400


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'synclight'}})
class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.u = User.objects.create_user('o', 'o@x.gm', 'pw')
        self.biz = Business.objects.create(user=self.u, business_name='Kunda', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.router = Router.objects.create(business=self.biz, name='Shop', ip_address='10.0.0.1', username='a', password='p',
                                            status='Online')
        rt.note_sessions(self.router.pk, [])          # no sessions running (the router said so)
        self.now = timezone.now()

    def voucher(self, code, used_ago=None, minutes=1440, **kw):
        used = self.now - timedelta(seconds=used_ago) if used_ago is not None else None
        return Voucher.objects.create(business=self.biz, router=self.router, code=code, plan_name='Day', price=10,
                                      duration_minutes=minutes, max_devices=1, status='active', source='taptap',
                                      used_at=used, expires_at=used + timedelta(minutes=minutes) if used and minutes else None, **kw)


class Checkpoints(Base):
    """A 24 h, 1-device voucher. TapTap's headroom = time left; the router's = limit − uptime."""

    def test_more_time_on_the_router_waits_until_50_percent(self):
        v = self.voucher('AVA1', used_ago=6 * 3600)                       # 25 %
        self.assertEqual(rt.time_check(v, '3d', '6h', now=self.now), 'wait')
        self.assertTrue(rt.acceptable(v, '3d', '6h', now=self.now))       # every sync path: leave it alone
        self.assertFalse(rt.strictly_acceptable(v, '3d', '6h', now=self.now))

    def test_no_limit_on_the_router_waits_too(self):
        v = self.voucher('AVA2', used_ago=6 * 3600)
        self.assertEqual(rt.time_check(v, '', '6h', now=self.now), 'wait')

    def test_corrected_at_50_then_75_percent_once_each(self):
        v = self.voucher('AVA3', used_ago=6 * 3600)
        at50 = v.used_at + timedelta(hours=13)                            # 54 %
        self.assertEqual(rt.stage(v, at50), 50)
        self.assertEqual(rt.time_check(v, '3d', '13h', now=at50), 'checkpoint')
        rt.mark_checked(v, at50)
        self.assertEqual(rt.time_check(v, '3d', '14h', now=at50 + timedelta(hours=1)), 'wait')     # handled: no more
        at75 = v.used_at + timedelta(hours=19)                            # 79 %
        self.assertEqual(rt.time_check(v, '3d', '19h', now=at75), 'checkpoint')
        rt.mark_checked(v, at75)
        self.assertEqual(rt.time_check(v, '3d', '20h', now=at75 + timedelta(hours=1)), 'wait')

    def test_a_late_check_skips_straight_to_75(self):
        v = self.voucher('AVA4', used_ago=6 * 3600)
        late = v.used_at + timedelta(hours=20)
        self.assertEqual(rt.stage(v, late), 75)
        rt.mark_checked(v, late)
        self.assertEqual(rt.checkpoint_open(v, late), 0)

    def test_the_router_would_cut_early_is_fixed_at_once(self):
        v = self.voucher('AVA5', used_ago=6 * 3600)                       # 18 h left, the router allows 14 h more
        self.assertEqual(rt.time_check(v, '20h', '6h', now=self.now), 'urgent')
        self.assertFalse(rt.acceptable(v, '20h', '6h', now=self.now))

    def test_before_usage_kept_right_as_before(self):
        v = self.voucher('AVA6')                                          # not used yet
        self.assertEqual(rt.time_check(v, '', '0s', now=self.now), 'urgent')
        self.assertEqual(rt.time_check(v, '1d', '0s', now=self.now), 'ok')

    def test_no_end_with_a_limit_on_the_router_is_urgent(self):
        v = self.voucher('AVA7', used_ago=3600, minutes=0)
        self.assertEqual(rt.time_check(v, '5h', '1h', now=self.now), 'urgent')

    def test_agreeing_router_is_ok(self):
        v = self.voucher('AVA8', used_ago=6 * 3600)
        self.assertEqual(rt.time_check(v, '1d', '6h', now=self.now), 'ok')

    def test_a_renewal_starts_new_checkpoints(self):
        v = self.voucher('MEM1', used_ago=20 * DAY, minutes=30 * 1440, login_type='member')
        self.assertEqual(rt.stage(v, self.now), 50)
        rt.mark_checked(v, self.now)
        self.assertEqual(rt.checkpoint_open(v, self.now), 0)
        v.expires_at = v.expires_at + timedelta(days=30)                  # renewed: 20 of 60 days
        self.assertEqual(rt.stage(v, self.now), 0)
        v.expires_at = v.expires_at + timedelta(days=60)                  # … and later 20 of 120 days
        self.assertEqual(rt.checkpoint_open(v, self.now + timedelta(days=45)), 50)   # a fresh 50 % checkpoint


class FakeUsers:
    def __init__(self):
        self.sets = []

    def set(self, **kw):
        self.sets.append(kw)


class ApiRepairs(Base):
    def test_only_urgent_and_checkpoint_are_written_urgent_first_with_a_budget(self):
        rows = []
        for i in range(30):                                               # 30 that would be cut early
            self.voucher(f'U{i:02d}', used_ago=6 * 3600)
            rows.append({'id': f'*{i}', 'name': f'U{i:02d}', 'limit-uptime': '20h', 'uptime': '6h'})
        self.voucher('W1', used_ago=6 * 3600)                             # more time on the router, 25 %: waits
        rows.append({'id': '*w', 'name': 'W1', 'limit-uptime': '3d', 'uptime': '6h'})
        svc = mock.MagicMock()
        res = FakeUsers()
        svc.resource.return_value = res
        self.assertEqual(rt.repair_api(self.router, svc, rows), rt.REPAIRS_PER_PASS)
        self.assertNotIn('*w', [s['id'] for s in res.sets])
        self.assertEqual(rt.tally(self.router.pk)['urgent'], rt.REPAIRS_PER_PASS)
        self.assertEqual(rt.tally(self.router.pk)['wait'], 1)

    def test_nothing_to_do_does_not_even_open_the_user_menu(self):
        self.voucher('W2', used_ago=6 * 3600)
        svc = mock.MagicMock()
        rt.repair_api(self.router, svc, [{'id': '*1', 'name': 'W2', 'limit-uptime': '3d', 'uptime': '6h'}])
        svc.resource.assert_not_called()


class LinkLanes(Base):
    def setUp(self):
        super().setUp()
        self.router.connection_mode = 'agent'
        self.router.save(update_fields=['connection_mode'])
        RouterAgent.objects.create(router=self.router, token_hash='e' * 64, last_seen_at=timezone.now(),
                                   enrolled_at=timezone.now(), script_version=9, ros_version='7.15')

    def mirror(self, code, limit, uptime):
        return RouterHotspotUser.objects.create(business=self.biz, router=self.router, username=code, limit_uptime=limit,
                                                uptime=uptime, is_present=True)

    def test_urgent_in_the_voucher_lane_checkpoint_in_the_background_lane(self):
        self.voucher('URG', used_ago=6 * 3600); self.mirror('URG', '20h', '6h')
        self.voucher('CHK', used_ago=13 * 3600); self.mirror('CHK', '3d', '13h')
        self.voucher('WAI', used_ago=6 * 3600); self.mirror('WAI', '3d', '6h')
        self.assertEqual(rt.repair_link(self.router), 2)
        cmds = {c.kind: [u['n'] for u in c.params['users']] for c in AgentCommand.objects.filter(router=self.router)}
        self.assertEqual(cmds, {'hotspot_users_limit': ['URG'], 'hotspot_users_time': ['CHK']})
        self.assertIn('hotspot_users_limit', agent.VOUCHER_KINDS)
        self.assertNotIn('hotspot_users_time', agent.VOUCHER_KINDS)      # never takes a voucher's place

    def test_a_handled_checkpoint_is_not_corrected_again(self):
        v = self.voucher('CHK2', used_ago=13 * 3600); m = self.mirror('CHK2', '3d', '13h')
        self.assertEqual(rt.repair_link(self.router), 1)
        AgentCommand.objects.filter(router=self.router).update(status='done')
        RouterHotspotUser.objects.filter(pk=m.pk).update(limit_uptime='3d')   # the router drifted again since
        cache.delete(f'tt:rt:fix:{self.router.pk}:{v.code}')                   # (10 minutes later)
        self.assertEqual(rt.repair_link(self.router), 0)

    def test_background_command_is_the_same_verified_write(self):
        params = {'users': [{'n': 'CHK', 'lim': '1d2h'}], 'profiles': [], 'router_time': True}
        agent.queue(self.router, 'hotspot_users_limit', params)
        agent.queue(self.router, 'hotspot_users_time', params)            # allowed and validated like the urgent one
        a = AgentCommand.objects.get(router=self.router, kind='hotspot_users_limit')
        b = AgentCommand.objects.get(router=self.router, kind='hotspot_users_time')
        self.assertEqual(agent.command_body(a), agent.command_body(b))
        self.assertIn('limit-uptime=1d2h', agent.command_body(b))
        with self.assertRaises(ValueError):
            agent.queue(self.router, 'hotspot_users_time', {'users': [{'n': 'X; /system reset', 'lim': '1d'}]})

    def test_background_goes_after_vouchers(self):
        agent.queue(self.router, 'hotspot_users_time', {'users': [{'n': 'CHK', 'lim': '1d'}], 'profiles': [], 'router_time': True})
        agent.queue(self.router, 'hotspot_users_limit', {'users': [{'n': 'URG', 'lim': '1d'}], 'profiles': [], 'router_time': True})
        order = list(AgentCommand.objects.filter(router=self.router).order_by(agent.delivery_order(), 'created_at')
                     .values_list('kind', flat=True))
        self.assertEqual(order, ['hotspot_users_limit', 'hotspot_users_time'])


class LightPasses(Base):
    def test_small_router_full_read_every_pass(self):
        router_load.note_full_read(self.router, 0.2, 120)
        self.assertTrue(router_load.full_due(self.router))

    def test_big_router_reads_its_table_every_two_minutes(self):
        router_load.note_full_read(self.router, 0.9, 2400)
        self.assertEqual(router_load.full_every(self.router), 120)
        self.assertFalse(router_load.full_due(self.router))
        self.assertTrue(router_load.full_due(self.router, force=True))
        self.assertTrue(router_load.full_due(self.router, unknown_codes=True))

    def test_a_slow_router_is_read_less(self):
        router_load.note_full_read(self.router, 4.0, 2400)
        self.assertEqual(router_load.full_every(self.router), 240)
        router_load.note_full_read(self.router, 9.0, 2400)
        self.assertEqual(router_load.full_every(self.router), router_load.MAX_EVERY)
        router_load.note_full_read(self.router, 4.0, 50)                 # small table, still slow: a busy router
        self.assertEqual(router_load.full_every(self.router), 60)

    def test_host_list_only_with_bypassed_bindings(self):
        self.assertFalse(router_load.needs_hosts([{'type': 'regular'}, {'type': 'blocked'}]))
        self.assertFalse(router_load.needs_hosts([{'type': 'bypassed', 'disabled': 'true'}]))
        self.assertTrue(router_load.needs_hosts([{'type': 'bypassed', 'disabled': 'false'}]))

    def svc(self, users, active, bindings=()):
        s = mock.MagicMock()
        s.hotspot_users.return_value = users
        s.active_users.return_value = active
        s.bindings.return_value = list(bindings)
        s.hotspot_hosts.return_value = []
        return s

    def watch(self, s, force=False):
        from core import live
        with mock.patch('core.live.MikroTikService') as cls, mock.patch('core.self_trust.ensure'):
            cls.return_value.connect.return_value = s
            return live.watch_router(Router.objects.get(pk=self.router.pk), force=force)

    def test_a_light_pass_skips_the_table_and_still_starts_and_ends_vouchers(self):
        new = self.voucher('NEW1')                                         # sold, about to log in
        old = self.voucher('OLD1', used_ago=2 * DAY)                       # its time ran out a day ago
        users = [{'.id': '*1', 'id': '*1', 'name': 'NEW1', 'uptime': '0s', 'limit-uptime': '1d', 'disabled': 'false', 'profile': 'Day'},
                 {'.id': '*2', 'id': '*2', 'name': 'OLD1', 'uptime': '1d', 'limit-uptime': '1d', 'disabled': 'false', 'profile': 'Day'}]
        first = self.svc(users, [])
        self.assertTrue(self.watch(first, force=True)['full_read'])
        first.hotspot_users.assert_called_once()
        router_load.note_full_read(self.router, 1.0, 2400)                # a big router: next full read in 2 minutes
        session = [{'user': 'NEW1', 'uptime': '30s', 'mac-address': 'AA:BB:CC:00:00:01', 'id': '*a'}]
        light = self.svc(users, session)
        out = self.watch(light)
        self.assertFalse(out['full_read'])
        light.hotspot_users.assert_not_called()                           # the whole table: not read
        light.hotspot_hosts.assert_not_called()                           # no bypassed bindings: not read
        new.refresh_from_db(); old.refresh_from_db()
        self.assertIsNotNone(new.used_at)                                 # started from its session
        self.assertEqual(old.status, 'expired')                           # ended on TapTap's clock
        self.assertFalse(RouterHotspotUser.objects.get(router=self.router, username='NEW1').is_present is False)

    def test_an_unknown_code_online_forces_a_full_read(self):
        router_load.note_full_read(self.router, 1.0, 2400)
        s = self.svc([], [{'user': 'MADE-ON-ROUTER', 'uptime': '1m', 'mac-address': 'AA:BB:CC:00:00:02', 'id': '*b'}])
        self.assertTrue(self.watch(s)['full_read'])
        s.hotspot_users.assert_called_once()
        router_load.note_full_read(self.router, 1.0, 2400)                # (the fake table was empty)
        s2 = self.svc([], [{'user': 'MADE-ON-ROUTER', 'uptime': '2m', 'mac-address': 'AA:BB:CC:00:00:02', 'id': '*b'}])
        self.assertFalse(self.watch(s2)['full_read'])                      # once per code, not every pass

    def test_routers_page_shows_the_load(self):
        router_load.note_full_read(self.router, 1.2, 2400)
        self.client.force_login(self.u)
        r = self.client.get('/routers/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Voucher table read every')
        self.assertContains(r, 'voucher time checked at 50')
