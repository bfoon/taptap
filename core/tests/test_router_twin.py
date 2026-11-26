"""Router twins (standby with everything, failover) and router-to-router migration."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core import agent
from core import router_migrate as mig
from core import router_twin as tw
from core.agent_inventory import _users
from core.models import (AgentCommand, AgentProgram, Business, Router, RouterAgent, RouterBackup, RouterHotspotProfile,
                         RouterHotspotUser, SyncedIPBinding, Voucher, VoucherPlan)
from core.models_router_twin import RouterMigration, RouterTwin, RouterTwinEvent, RouterTwinItem


class FakeResource:
    def __init__(self, store):
        self.rows = store

    def get(self, **kw):
        out = []
        for r in self.rows:
            if all(str(r.get(k.replace('_', '-'), r.get(k))) == str(v) for k, v in kw.items()):
                out.append(r)
        return out

    def add(self, **kw):
        row = {k.replace('_', '-'): v for k, v in kw.items()}
        row['id'] = f'*{len(self.rows) + 1}'
        self.rows.append(row)
        return row['id']

    def set(self, id, **kw):  # noqa: A002
        for r in self.rows:
            if r['id'] == id:
                r.update({k.replace('_', '-'): v for k, v in kw.items()})

    def remove(self, id):  # noqa: A002
        self.rows[:] = [r for r in self.rows if r['id'] != id]


class FakeSvc:
    def __init__(self):
        self.tables = {}

    def resource(self, path):
        return FakeResource(self.tables.setdefault(path, []))

    def connect(self):
        return self

    def close(self):
        pass

    def ensure_hotspot_profile(self, name, shared=1, rate=''):
        res = self.resource('/ip/hotspot/user/profile')
        rows = res.get(name=name)
        if rows:
            res.set(rows[0]['id'], shared_users=str(shared))
        else:
            res.add(name=name, shared_users=str(shared), rate_limit=rate)

    def reset_active_by_name(self, code):
        pass

    def disable_voucher(self, code):
        res = self.resource('/ip/hotspot/user')
        for r in res.get(name=code):
            res.set(r['id'], disabled='yes')

    def users(self):
        return {r['name']: r for r in self.tables.get('/ip/hotspot/user', [])}


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.now = timezone.now()
        self.owner = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456')
        self.b = Business.objects.create(user=self.owner, business_name='Kerr Wi-Fi', owner_name='O', phone='7000001',
                                         trial_ends_at=self.now + timedelta(days=30), feature_tier='pro')
        self.main = Router.objects.create(business=self.b, name='Main', ip_address='', connection_mode='agent', status='Online')
        self.spare = Router.objects.create(business=self.b, name='Spare', ip_address='', connection_mode='agent', status='Online')
        for r, h in ((self.main, 'a'), (self.spare, 'b')):
            RouterAgent.objects.create(router=r, token_hash=h * 64, last_seen_at=self.now, enrolled_at=self.now, script_version=9, ros_version='7.15')
        self.day = VoucherPlan.objects.create(business=self.b, name='Day', price=50, duration_minutes=1440)
        self.v1 = Voucher.objects.create(business=self.b, router=self.main, code='TWIN0001', plan_name='Day', duration_minutes=1440,
                                         mikrotik_sync_status='Synced')
        self.v2 = Voucher.objects.create(business=self.b, router=self.main, code='TWIN0002', plan_name='Day', duration_minutes=1440,
                                         mikrotik_sync_status='Synced', used_at=self.now - timedelta(hours=2),
                                         expires_at=self.now + timedelta(hours=22))
        self.old = Voucher.objects.create(business=self.b, router=self.main, code='TWIN0003', plan_name='Day', duration_minutes=1440,
                                          status='expired', used_at=self.now - timedelta(days=2), expires_at=self.now - timedelta(days=1))

    def pair(self, **kw):
        return tw.pair(self.b, self.main, self.spare, user=self.owner, **kw)

    def twin_cmds(self, router=None):
        return list(AgentCommand.objects.filter(router=router or self.spare, kind=tw.KIND).order_by('pk'))


class Pairing(Base):
    def test_pair_rules(self):
        with self.assertRaises(tw.TwinError):
            tw.pair(self.b, self.main, self.main)
        twin = self.pair()
        self.assertEqual(twin.events.first().kind, 'paired')
        third = Router.objects.create(business=self.b, name='Third', ip_address='1.1.1.1')
        with self.assertRaises(tw.TwinError):
            tw.pair(self.b, third, self.spare)          # already a standby
        other = Business.objects.create(user=User.objects.create_user('z@x.gm'), business_name='Other', owner_name='Z', phone='1',
                                        trial_ends_at=self.now + timedelta(days=3))
        foreign = Router.objects.create(business=other, name='Foreign', ip_address='2.2.2.2')
        with self.assertRaises(tw.TwinError):
            tw.pair(self.b, third, foreign)
        self.assertTrue(tw.mirrors(self.main.pk, self.spare.pk))
        self.assertFalse(tw.mirrors(self.spare.pk, self.main.pk))
        self.assertFalse(tw.mirrors(third.pk, self.spare.pk))

    def test_desired_rows(self):
        RouterHotspotUser.objects.create(business=self.b, router=self.main, username='mik1', profile='3h', limit_uptime='3h', uptime='1h',
                                         raw_data={'password': 'pw1'})
        RouterHotspotUser.objects.create(business=self.b, router=self.main, username='mik2', profile='3h', raw_data={})
        RouterHotspotProfile.objects.create(business=self.b, router=self.main, name='3h', shared_users=2, rate_limit='2M/2M')
        SyncedIPBinding.objects.create(business=self.b, router=self.main, mac_address='aa:bb:cc:dd:ee:01', binding_type='bypassed', comment='Boss')
        twin = self.pair()
        want = tw.desired(twin)
        self.assertEqual(set(want['user']), {'TWIN0001', 'TWIN0002', 'mik1'})        # expired one is not kept
        self.assertEqual({n for n, _ in want['skipped']}, {'mik2'})                   # password unknown
        self.assertIn('Day', want['profile'])
        self.assertIn('3h', want['profile'])
        self.assertIn('AA:BB:CC:DD:EE:01', want['binding'])
        self.assertEqual(want['user']['mik1'][1]['lim'], '2h')                         # what is left of its limit
        twin.copy_router_users = twin.copy_vouchers = False
        twin.save()
        self.assertEqual(set(tw.desired(twin)['user']), set())

    def test_shadow_never_touches_the_voucher(self):
        s = tw.shadow(self.v2, self.spare)
        self.assertEqual(s.router_id, self.spare.pk)
        self.v2.refresh_from_db()
        self.assertEqual(self.v2.router_id, self.main.pk)


class LinkMirror(Base):
    def test_mirror_queues_ack_and_only_changes_travel(self):
        twin = self.pair()
        res = tw.mirror(twin, now_button=True)
        self.assertEqual(res['users'], 2)
        cmds = self.twin_cmds()
        self.assertEqual(len(cmds), 1)
        p = cmds[0].params
        self.assertNotIn('ids', p)                                   # never marks anything Synced
        self.assertEqual(p['twin_id'], twin.pk)
        self.assertEqual({u['n'] for u in p['users']}, {'TWIN0001', 'TWIN0002'})
        body = agent.command_body(cmds[0])
        self.assertIn('/ip hotspot user add name="TWIN0001"', body.replace("'", '"'))
        self.assertIn('profile add name="Day"', body.replace("'", '"'))
        self.assertTrue(all(i.status == 'queued' for i in twin.items.filter(kind='user')))
        cmds[0].status = 'sent'
        cmds[0].save()
        agent.handle_ack(cmds[0].pk, agent.nonce(cmds[0]), 'ok')
        self.assertEqual(twin.items.filter(kind='user', status='sent').count(), 2)
        self.assertEqual(twin.items.filter(kind='profile', status='sent').count(), 1)
        # the primary's vouchers are not touched by the standby's answer
        self.v1.refresh_from_db()
        self.assertEqual(self.v1.mikrotik_sync_status, 'Synced')
        res = tw.mirror(twin, now_button=True)
        self.assertEqual(res['users'], 0)
        self.assertEqual(len(self.twin_cmds()), 1)
        self.assertEqual(tw.readiness(twin)['percent'], 100)
        # a change travels: switched off in TapTap → switched off on the standby
        Voucher.objects.filter(pk=self.v1.pk).update(status='disabled')
        tw.mirror(twin, now_button=True)
        last = self.twin_cmds()[-1].params['users']
        self.assertEqual([(u['n'], u['dis']) for u in last], [('TWIN0001', True)])

    def test_gone_codes_are_taken_off(self):
        twin = self.pair()
        tw.mirror(twin, now_button=True)
        Voucher.objects.filter(pk=self.v1.pk).update(status='expired')
        tw.mirror(twin, now_button=True)
        rm = AgentCommand.objects.filter(router=self.spare, kind='hotspot_users_remove').first()
        self.assertEqual(rm.params['names'], ['TWIN0001'])
        self.assertNotIn('ids', rm.params)
        self.assertFalse(twin.items.filter(key='TWIN0001').exists())

    def test_router_made_user_removed_by_its_exact_name(self):
        m = RouterHotspotUser.objects.create(business=self.b, router=self.main, username='mik1', profile='default', raw_data={'password': 'p'})
        twin = self.pair()
        tw.mirror(twin, now_button=True)
        self.assertIn('mik1', [u['n'] for c in self.twin_cmds() for u in c.params['users']])
        RouterHotspotUser.objects.filter(pk=m.pk).update(is_present=False)
        tw.mirror(twin, now_button=True)
        rm = AgentCommand.objects.get(router=self.spare, kind='hotspot_users_remove')
        self.assertEqual(rm.params['names'], ['mik1'])

    def test_codes_moved_to_the_standby_are_never_taken_off_it(self):
        twin = self.pair()
        tw.mirror(twin, now_button=True)
        mig.move_vouchers(self.main, self.spare, members=True, vouchers=True)
        tw.mirror(twin, now_button=True)
        self.assertFalse(AgentCommand.objects.filter(router=self.spare, kind='hotspot_users_remove').exists())
        self.assertFalse(twin.items.filter(kind='user').exists())
        self.assertIn('Nothing to take off', tw.clean_standby(twin))

    def test_normal_rhythm_is_throttled_and_paused(self):
        twin = self.pair()
        tw.mirror(twin)
        self.assertTrue(tw.mirror(twin).get('later'))
        tw.set_paused(twin, True)
        twin.refresh_from_db()
        self.assertTrue(tw.mirror(twin, now_button=True).get('paused'))

    def test_link_checkin_hook_mirrors_the_standby_only(self):
        twin = self.pair()
        agent.push_pending_vouchers(self.main)
        self.assertEqual(self.twin_cmds(self.main), [])
        agent.push_pending_vouchers(self.spare)
        self.assertEqual(len(self.twin_cmds()), 1)
        self.assertEqual(twin.items.filter(kind='user').count(), 2)

    def test_standby_own_user_is_never_overwritten(self):
        RouterHotspotUser.objects.create(business=self.b, router=self.main, username='shared', profile='default', raw_data={'password': 'x'})
        RouterHotspotUser.objects.create(business=self.b, router=self.spare, username='shared', profile='default', raw_data={'password': 'y'})
        twin = self.pair()
        res = tw.mirror(twin, now_button=True)
        self.assertEqual(res['conflicts'], 1)
        self.assertEqual(twin.items.get(key='shared').status, 'conflict')
        self.assertNotIn('shared', [u['n'] for c in self.twin_cmds() for u in c.params['users']])


class StandbyInUse(Base):
    def test_first_login_on_standby_starts_the_clock(self):
        twin = self.pair()
        agent.ingest_sessions(self.spare, [{'user': 'TWIN0001', 'mac-address': 'AA:BB:CC:00:00:01', 'address': '10.0.0.9',
                                            'uptime': '5m', 'bytes-in': '1', 'bytes-out': '1'}], self.now)
        self.v1.refresh_from_db()
        self.assertIsNotNone(self.v1.used_at)
        self.assertIsNotNone(self.v1.expires_at)
        self.assertEqual(self.v1.router_id, self.main.pk)
        self.assertTrue(twin.pk)

    def test_without_a_twin_the_other_site_still_cannot_start_it(self):
        agent.ingest_sessions(self.spare, [{'user': 'TWIN0001', 'mac-address': 'AA:BB:CC:00:00:01', 'address': '10.0.0.9',
                                            'uptime': '5m', 'bytes-in': '1', 'bytes-out': '1'}], self.now)
        self.v1.refresh_from_db()
        self.assertIsNone(self.v1.used_at)

    def test_standby_inventory_is_expected_not_an_error(self):
        self.pair()
        summary = _users(self.spare, [{'name': 'TWIN0001', 'profile': 'Day', 'uptime': '10m'}], self.now)
        self.assertFalse(any('assigned to router' in x for x in summary['errors']))
        self.v1.refresh_from_db()
        self.assertIsNotNone(self.v1.used_at)          # time counted on the standby = its first use
        self.assertIsNotNone(self.v1.expires_at)
        self.assertEqual(self.v1.router_id, self.main.pk)

    @mock.patch('core.notify.notify')
    def test_failover_event_and_handover(self, notify):
        twin = self.pair()
        tw.on_down(self.main)
        ev = twin.events.get(kind='failover')
        self.assertIsNone(ev.ended_at)
        twin.refresh_from_db()
        self.assertIsNotNone(twin.failover_since)
        self.assertIn('standing in', notify.call_args[0][2])
        tw.on_down(self.main)                                   # once per outage
        self.assertEqual(twin.events.filter(kind='failover').count(), 1)
        agent.ingest_sessions(self.spare, [{'user': 'TWIN0001', 'mac-address': 'AA:BB:CC:00:00:01', 'address': '10.0.0.9',
                                            'uptime': '1m', 'bytes-in': '1', 'bytes-out': '1'}], self.now)
        tw.on_back(self.main)
        ev.refresh_from_db()
        self.assertIsNotNone(ev.ended_at)
        self.assertEqual(ev.activated, 1)
        twin.refresh_from_db()
        self.assertIsNone(twin.failover_since)
        tw.on_down(self.spare)
        self.assertTrue(twin.events.filter(kind='standby_down', ended_at__isnull=True).exists())

    def test_login_page_does_not_wait_for_a_dead_main_router(self):
        from core import voucher_ready
        twin = self.pair()
        Voucher.objects.filter(pk=self.v1.pk).update(mikrotik_sync_status='Pending')
        self.v1.refresh_from_db()
        Router.objects.filter(pk=self.main.pk).update(status='Offline')
        self.v1 = Voucher.objects.select_related('router').get(pk=self.v1.pk)
        r = voucher_ready.make_ready(self.v1)
        self.assertEqual((r['ready'], r['via']), (True, 'Router twin'))
        Router.objects.filter(pk=self.main.pk).update(status='Online')
        self.v1 = Voucher.objects.select_related('router').get(pk=self.v1.pk)
        with mock.patch('core.voucher_fastlane.sync_now'):
            self.assertNotEqual(voucher_ready.make_ready(self.v1).get('via'), 'Router twin')
        self.assertTrue(twin.pk)

    def test_uptime_guard_is_wrapped(self):
        from core import uptime_guard
        self.assertTrue(getattr(uptime_guard, '_router_twin_installed', False))
        self.assertTrue(hasattr(uptime_guard.still_down, '__wrapped__'))

    def test_promote_swaps_roles_and_moves_vouchers(self):
        twin = self.pair()
        tw.mirror(twin, now_button=True)
        n = tw.promote(twin)
        self.assertEqual(n, 3)
        twin.refresh_from_db()
        self.assertEqual((twin.primary_id, twin.standby_id), (self.spare.pk, self.main.pk))
        self.v1.refresh_from_db()
        self.assertEqual(self.v1.router_id, self.spare.pk)
        self.assertEqual(twin.items.count(), 0)
        self.assertTrue(twin.events.filter(kind='promoted').exists())


class ApiMirror(Base):
    def setUp(self):
        super().setUp()
        Router.objects.filter(pk=self.spare.pk).update(connection_mode='api', ip_address='10.0.0.2')
        self.spare.refresh_from_db()
        RouterHotspotUser.objects.create(business=self.b, router=self.main, username='blank', profile='default', raw_data={'password': ''})
        SyncedIPBinding.objects.create(business=self.b, router=self.main, address='10.0.0.50', binding_type='bypassed', comment='Printer')

    def test_api_writes_users_profiles_bindings(self):
        twin = self.pair()
        svc = FakeSvc()
        res = tw.mirror(twin, svc=svc, now_button=True)
        users = svc.users()
        self.assertEqual(set(users), {'TWIN0001', 'TWIN0002', 'blank'})
        self.assertEqual(users['TWIN0001']['password'], 'TWIN0001')
        self.assertNotIn('password', users['blank'])          # an empty password stays empty
        self.assertTrue(users['TWIN0002']['limit-uptime'])
        self.assertEqual(svc.tables['/ip/hotspot/ip-binding'][0]['address'], '10.0.0.50')
        self.assertEqual(svc.tables['/ip/hotspot/ip-binding'][0]['server'], 'all')
        self.assertEqual(res['errors'], 0)
        self.assertEqual(twin.items.filter(status='sent').count(), twin.items.count())

    def test_live_pass_hook(self):
        twin = self.pair()
        svc = FakeSvc()
        from core import traffic
        with mock.patch('core.router_twin.ingest') as ing:
            traffic.collect(self.spare, svc, [], self.now)
        ing.assert_called_once()
        self.assertIn('TWIN0001', svc.users())
        self.assertTrue(twin.items.exists())


class Migration(Base):
    def test_preview_and_move(self):
        RouterHotspotUser.objects.create(business=self.b, router=self.main, username='mik1', profile='3h', limit_uptime='3h', raw_data={'password': 'p'})
        Voucher.objects.create(business=self.b, router=self.main, code='mik1', plan_name='3h', source='mikrotik')
        SyncedIPBinding.objects.create(business=self.b, router=self.main, mac_address='AA:BB:CC:DD:EE:02', binding_type='blocked')
        prog = AgentProgram.objects.create(business=self.b, router=self.main)
        p = mig.preview(self.b, self.main.pk, self.spare.pk)
        self.assertEqual(p['counts']['vouchers'], 3)
        self.assertEqual(p['counts']['vouchers_live'], 2)
        self.assertEqual(p['counts']['router_users'], 1)
        self.assertEqual(p['counts']['bindings'], 1)
        self.assertEqual(p['counts']['sales'], 1)
        m = mig.start(self.b, self.main.pk, self.spare.pk, ['vouchers', 'router_users', 'bindings', 'sales'], clean_source=True, user=self.owner)
        m.refresh_from_db()
        self.assertIn(m.status, ('done', 'partly'))
        self.v1.refresh_from_db()
        self.old.refresh_from_db()
        self.assertEqual((self.v1.router_id, self.old.router_id), (self.spare.pk, self.spare.pk))
        self.assertEqual(Voucher.objects.get(code='mik1').router_id, self.spare.pk)
        self.assertTrue(AgentCommand.objects.filter(router=self.spare, kind=tw.KIND, params__migration_id=m.pk).exists())
        self.assertTrue(AgentCommand.objects.filter(router=self.spare, kind='binding_upsert').exists())
        rm = AgentCommand.objects.get(router=self.main, kind='hotspot_users_remove')
        self.assertEqual(set(rm.params['names']), {'TWIN0001', 'TWIN0002'})
        prog.refresh_from_db()
        self.assertEqual(prog.router_id, self.spare.pk)
        self.assertEqual(m.counts['vouchers'], 3)

    def test_rules(self):
        with self.assertRaises(mig.MigrateError):
            mig.preview(self.b, self.main.pk, self.main.pk)
        with self.assertRaises(mig.MigrateError):
            mig.start(self.b, self.main.pk, self.spare.pk, [])

    def test_unreachable_target_still_moves_vouchers(self):
        Router.objects.filter(pk=self.spare.pk).update(connection_mode='api', ip_address='10.0.0.2')
        with mock.patch('core.mikrotik.MikroTikService') as svc:
            svc.return_value.connect.side_effect = OSError('timed out')
            m = mig.start(self.b, self.main.pk, self.spare.pk, ['profiles', 'vouchers'])
        m.refresh_from_db()
        self.assertEqual(m.status, 'partly')
        self.v1.refresh_from_db()
        self.assertEqual(self.v1.router_id, self.spare.pk)


EXPORT = """# 2026-11-20 by RouterOS 7.15
# model = RB951Ui-2HnD
/interface bridge
add name=bridge-hotspot
/ip pool
add name=hs-pool ranges=10.5.50.10-10.5.50.250
/ip hotspot user profile
set [ find default=yes ] shared-users=2
add name=1day rate-limit=2M/2M \\
    shared-users=1
/ip address
add address=10.5.50.1/24 interface=bridge-hotspot
/system identity
set name=Main
"""


class Sections(Base):
    def test_sections_and_script(self):
        rows = {r['path']: r for r in mig.section_list(EXPORT)}
        self.assertTrue(rows['/interface bridge']['risky'])
        self.assertTrue(rows['/ip address']['risky'])
        self.assertFalse(rows['/ip pool']['risky'])
        self.assertTrue(rows['/ip hotspot user profile']['suggest'])
        self.assertEqual(rows['/ip hotspot user profile']['count'], 2)
        script, n = mig.sections_script(EXPORT, ['/ip pool', '/ip hotspot user profile'], 'Main', 'Spare')
        self.assertEqual(n, 3)
        self.assertIn(':do { /ip hotspot user profile add name=1day rate-limit=2M/2M shared-users=1 }', script)
        self.assertNotIn('/ip address', script)
        with self.assertRaises(mig.MigrateError):
            mig.sections_script(EXPORT, ['/nope'], 'Main')

    def test_vrrp(self):
        twin = self.pair()
        out = tw.vrrp_scripts(twin, 'bridge', '10.5.50.254', 42)
        self.assertIn('vrid=42 priority=200', out['primary'])
        self.assertIn('priority=100', out['standby'])
        self.assertIn('/interface vrrp remove [find comment="TapTap twin"]', out['undo'])
        for bad in (('bri dge', '10.5.50.254'), ('bridge', 'nope'), ('bridge', '0.0.0.0')):
            with self.assertRaises(tw.TwinError):
                tw.vrrp_scripts(twin, *bad)


class Pages(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_pages_and_json(self):
        r = self.client.get(reverse('router_twins') + '?tab=migrate')
        self.assertContains(r, 'Move to a new MikroTik')
        r = self.client.get(reverse('router_twins') + '?tab=twins')
        self.assertContains(r, 'Pair two routers')
        r = self.client.post(reverse('router_twin_pair'), {'primary': self.main.pk, 'standby': self.spare.pk, 'copy_vouchers': 'on',
                                                          'copy_members': 'on', 'switch_over': 'vrrp'})
        twin = RouterTwin.objects.get()
        self.assertRedirects(r, reverse('router_twin_detail', args=[twin.pk]))
        self.assertFalse(twin.copy_bindings)
        r = self.client.get(reverse('router_twin_detail', args=[twin.pk]))
        self.assertContains(r, 'Shared gateway address')
        self.assertContains(r, 'Make Spare the main router')
        r = self.client.get(reverse('router_twin_state', args=[twin.pk]))
        self.assertTrue(r.json()['ok'])
        r = self.client.get(reverse('routers'))
        self.assertContains(r, 'Standby: Spare')
        self.assertContains(r, 'Migrate &amp; twins')
        r = self.client.post(reverse('router_twin_vrrp', args=[twin.pk]), {'interface': 'bridge', 'address': '10.1.1.254'}, content_type='application/json')
        self.assertIn('10.1.1.254/32', r.json()['primary'])
        r = self.client.post(reverse('router_twin_action', args=[twin.pk]), {'action': 'unpair', 'confirm': 'wrong'})
        self.assertTrue(RouterTwin.objects.exists())
        r = self.client.post(reverse('router_twin_action', args=[twin.pk]), {'action': 'unpair', 'confirm': 'main'})
        self.assertFalse(RouterTwin.objects.exists())

    def test_migrate_json(self):
        r = self.client.get(reverse('router_migrate_preview'), {'source': self.main.pk, 'target': self.spare.pk})
        self.assertEqual(r.json()['counts']['vouchers'], 3)
        url = reverse('router_migrate_start')
        r = self.client.post(url, {'source': self.main.pk, 'target': self.spare.pk, 'parts': ['vouchers'], 'confirm': 'nope'}, content_type='application/json')
        self.assertEqual(r.status_code, 400)
        r = self.client.post(url, {'source': self.main.pk, 'target': self.spare.pk, 'parts': ['vouchers'], 'confirm': 'spare'}, content_type='application/json')
        self.assertTrue(r.json()['ok'])
        mid = r.json()['migration']['id']
        self.assertEqual(self.client.get(reverse('router_migrate_status', args=[mid])).json()['migration']['status'], 'done')
        r = self.client.get(reverse('router_migrate_config'), {'source': self.main.pk, 'target': self.spare.pk})
        self.assertEqual(r.json()['backups'], [])
        b = RouterBackup.objects.create(router=self.main, name='nightly', content=EXPORT)
        self.assertEqual(self.client.get(reverse('router_migrate_sections'), {'backup': b.pk}).status_code, 200)
        r = self.client.post(reverse('router_migrate_sections'), {'backup': b.pk, 'target': self.spare.pk, 'sections': ['/ip pool']})
        self.assertIn('attachment', r['Content-Disposition'])
        self.assertIn(b'/ip pool add name=hs-pool', r.content)

    def test_other_business_cannot_see_twins(self):
        twin = self.pair()
        stranger = User.objects.create_user('s@x.gm', 's@x.gm', 'pw-123456')
        Business.objects.create(user=stranger, business_name='S', owner_name='S', phone='2', trial_ends_at=self.now + timedelta(days=3))
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(reverse('router_twin_detail', args=[twin.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('router_migrate_status', args=[RouterMigration.objects.create(business=self.b).pk])).status_code, 404)


class ModelsAreRegistered(TestCase):
    def test_imports(self):
        self.assertTrue(RouterTwinItem and RouterTwinEvent)
