"""Config Studio (core/config_studio*.py): tiles → RouterOS rows, the virtual router, backup-first deploy, undo."""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from core import agent, config_studio as cs, config_studio_ai as ai, config_studio_sim as sim, config_studio_tiles as tiles
from core.models import AgentCommand, Business, Router, RouterAgent, RouterBackup, RouterConfigSnapshot
from core.models_config_studio import RouterStudioBoard, RouterStudioDeploy
from core.models_team import TeamMember

CTX = {'ros_major': 7, 'trusted': []}


def board(*items):
    return tiles.clean_board([{'id': f't{i}', 'type': kind, 'params': params} for i, (kind, params) in enumerate(items)])


def sections(state):
    return {path: {'path': path, 'rows': rows, 'count': len(rows)} for path, rows in state.items()}


class FakeResource:
    def __init__(self, svc, path):
        self.svc, self.path = svc, path

    @property
    def rows(self):
        return self.svc.state.setdefault(self.path, [])

    @staticmethod
    def _hy(d):
        return {k.replace('_', '-'): v for k, v in d.items()}

    def get(self, **filters):
        want = self._hy(filters)
        return [dict(r) for r in self.rows if all(sim.same(r.get(k, ''), v) for k, v in want.items())]

    def add(self, **fields):
        fields = self._hy(fields)
        self.svc.calls.append(('add', self.path, dict(fields)))
        if self.svc.fail_on and self.svc.fail_on(self.path, fields):
            raise RuntimeError('failure: bad value')
        before = fields.pop('place-before', None)
        self.svc.n += 1
        row = {'id': f'*F{self.svc.n}', **fields}
        at = next((i for i, r in enumerate(self.rows) if r.get('id') == before), len(self.rows))
        self.rows.insert(at, row)

    def set(self, **fields):
        fields = self._hy(fields)
        rid = fields.pop('id', None)
        self.svc.calls.append(('set', self.path, dict(fields)))
        if not self.rows:
            self.rows.append({})
        row = next((r for r in self.rows if r.get('id') == rid), self.rows[0])
        row.update(fields)

    def remove(self, **fields):
        self.svc.calls.append(('remove', self.path, fields.get('id')))
        self.svc.state[self.path] = [r for r in self.rows if r.get('id') != fields.get('id')]


class FakeService:
    """Just enough of MikroTikService for the deploy engine: menus are lists of rows."""

    def __init__(self, state, fail_on=None):
        self.state, self.calls, self.fail_on, self.n, self.closed = state, [], fail_on, 0, 0

    def resource(self, path):
        return FakeResource(self, path)

    def close(self):
        self.closed += 1


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456', first_name='Awa')
        self.biz = Business.objects.create(user=self.owner, business_name='Kunda Net', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.r = Router.objects.create(business=self.biz, name='Mast', ip_address='192.168.88.1', username='a', password='p', status='Online')
        self.state = sim.lab_state('default')
        RouterConfigSnapshot.objects.create(router=self.r, sections=sections(self.state))
        self.c = Client()
        self.c.force_login(self.owner)

    def post(self, name, data=None, *args):
        from django.urls import reverse
        return self.c.post(reverse(name, args=[self.r.pk, *args]), json.dumps(data or {}), content_type='application/json')

    def deploy_for(self, tiles_, minutes=5):
        ops, problems = tiles.compile_board(tiles_, CTX)
        self.assertEqual(problems, [])
        return RouterStudioDeploy.objects.create(business=self.biz, router=self.r, actor=self.owner, tiles=tiles_, plan=ops, undo_minutes=minutes)


class TileTests(TestCase):
    def test_a_tile_becomes_rows_and_readable_code(self):
        ops, problems = tiles.compile_board(board(('lan', {'interface': 'ether2', 'network': '192.168.50.1/24'})), CTX)
        self.assertEqual(problems, [])
        self.assertEqual([o['resource'] for o in ops],
                         ['/interface/bridge/port', '/ip/address', '/ip/pool', '/ip/dhcp-server/network', '/ip/dhcp-server'])
        self.assertEqual(ops[2]['values']['ranges'], '192.168.50.10-192.168.50.254')
        text = '\n'.join(line['text'] for line in tiles.readable(ops))
        self.assertIn('/ip address add address=192.168.50.1/24 interface=ether2', text)
        self.assertIn('/ip dhcp-server add name=dhcp-ether2', text)

    def test_wrong_values_are_named_and_the_rest_still_compiles(self):
        ops, problems = tiles.compile_board(board(('lan', {'interface': 'ether2', 'network': '192.168.50.0/24'}),
                                                  ('speed_limit', {'target': '192.168.88.50', 'down': 'fast'}),
                                                  ('identity', {'name': 'Shop'})), CTX)
        self.assertEqual([p['tile'] for p in problems], ['t0', 't1'])
        self.assertIn('first or last address', problems[0]['message'])
        self.assertEqual([o['resource'] for o in ops], ['/system/identity'])

    def test_nothing_typed_into_a_tile_can_become_a_command(self):
        for value in ('x"; /system reset-configuration; :put "', 'a\n/system reboot', 'ether1 [/system reboot]', '$(evil)'):
            _ops, problems = tiles.compile_board(board(('identity', {'name': value})), CTX)
            self.assertTrue(problems, value)
        # Free text (a device name) is kept, but always as one quoted string.
        ops, _ = tiles.compile_board(board(('static_lease', {'name': 'TV "$x"; /system reboot', 'mac': 'aa-bb-cc-11-22-33', 'ip': '192.168.88.9'})), CTX)
        line = tiles.safe_line(ops[0])
        self.assertIn(r'comment="TapTap-Studio:lease TV \"\$x\"; /system reboot"', line)
        self.assertEqual(ops[0]['find'], {'mac-address': 'AA:BB:CC:11:22:33'})

    def test_expert_tile_only_reaches_allowed_menus(self):
        _ops, problems = tiles.compile_board(board(('custom', {'menu': '/system/script', 'match': 'name=x', 'set': 'source=y'})), CTX)
        self.assertIn('cannot be changed from the studio', problems[0]['message'])
        _ops, problems = tiles.compile_board(board(('custom', {'menu': '/user', 'match': 'name=admin', 'set': 'group=full'})), CTX)
        self.assertTrue(problems)
        ops, problems = tiles.compile_board(board(('custom', {'menu': '/ip firewall filter', 'match': 'comment="my rule"', 'set': 'chain=forward action=accept'})), CTX)
        self.assertEqual(problems, [])
        self.assertEqual((ops[0]['resource'], ops[0]['find'], ops[0]['values']),
                         ('/ip/firewall/filter', {'comment': 'my rule'}, {'chain': 'forward', 'action': 'accept'}))

    def test_script_runs_twice_without_duplicates_and_fits_link_pieces(self):
        ops, _ = tiles.compile_board(board(*[(t['type'], t['params']) for t in cs.KITS[1]['tiles']]), CTX)
        text = tiles.script(ops, 'Mast')
        self.assertIn(':if ([:len $x] = 0) do={ /ip address add', text)
        self.assertIn('place-before=[:pick $f 0]', text)          # the guest wall goes above the other forward rules
        parts = tiles.chunks(ops)
        self.assertTrue(all(len(p) <= 2600 for p in parts))
        self.assertEqual(sum(p.count('find where') for p in parts), text.count('find where'))

    def test_speeds_and_passwords(self):
        self.assertEqual([tiles.v_speed(x) for x in ('5M', '512k', '1.5m', '2', '1G')], ['5M', '512k', '1500k', '2M', '1G'])
        ops, _ = tiles.compile_board(board(('wan_pppoe', {'port': 'ether1', 'user': 'shop@isp', 'password': 's3cret'})), CTX)
        self.assertNotIn('s3cret', json.dumps(tiles.readable(ops)))
        self.assertIn('s3cret', tiles.script(ops))
        self.assertNotIn('s3cret', tiles.describe(board(('wan_pppoe', {'port': 'ether1', 'user': 'u', 'password': 's3cret'}))[0]))


class InternetLineTests(TestCase):
    """The Internet line tiles on a router whose routes RouterOS 7 writes as "192.168.1.1@starlink2" (Nov 2026)."""
    STATE = {
        '/interface': [{'name': n, 'type': 'ether'} for n in ('ether1', 'ether2', 'ether5', 'bridge')],
        '/ip/address': [{'address': '172.16.0.1/24', 'interface': 'bridge'}],
        '/ip/dhcp-client': [{'interface': 'ether1', 'address': '192.168.1.20/24', 'add-default-route': 'no', 'status': 'bound'},
                            {'interface': 'ether5', 'address': '192.168.2.30/24', 'add-default-route': 'no', 'status': 'bound'}],
        '/ip/route': [{'dst-address': '1.1.1.1/32', 'gateway': '192.168.1.1%ether1'},
                      {'dst-address': '0.0.0.0/0', 'gateway': '1.1.1.1', 'distance': '1', 'active': 'true'},
                      {'dst-address': '0.0.0.0/0', 'gateway': '192.168.1.1@starlink2', 'distance': '1', 'routing-table': 'starlink2'},
                      {'dst-address': '0.0.0.0/0', 'gateway': '192.168.1.1@starlink2', 'distance': '3'},
                      {'dst-address': '8.8.8.8/32', 'gateway': '192.168.2.1%ether5'},
                      {'dst-address': '0.0.0.0/0', 'gateway': '8.8.8.8', 'distance': '2', 'active': 'false'}],
        '/ip/firewall/nat': [{'chain': 'srcnat', 'action': 'masquerade', 'out-interface': 'ether1'}],
    }

    def test_table_gateways_and_check_hosts_lead_to_real_ports(self):
        self.assertEqual(sim.VirtualRouter(self.STATE).wan_interfaces(), ['ether1', 'ether5'])

    def test_sharing_a_line_is_not_reported_as_without_nat(self):
        b = board(('wan_dhcp', {'port': 'ether1', 'share': 'yes'}), ('wan_dhcp', {'port': 'ether5', 'share': 'yes'}))
        ops, problems = tiles.compile_board(b, CTX)
        res = sim.examine(self.STATE, ops, b)
        self.assertEqual([(t['title'][-6:], t['pass']) for t in res['tests']], [('ether1', True), ('ether5', True)])
        self.assertFalse([f for f in res['findings'] if 'without NAT' in f['text']])

    def test_a_line_run_by_internet_lines_keeps_its_taptap_routes(self):
        ctx = {**CTX, 'wan_managed': ['ether1']}
        ops, _ = tiles.compile_board(board(('wan_dhcp', {'port': 'ether1'}), ('wan_dhcp', {'port': 'ether5'})), ctx)
        clients = {o['find']['interface']: o['values'] for o in ops if o['resource'] == '/ip/dhcp-client'}
        self.assertEqual(clients['ether1']['add-default-route'], 'no')     # no second default route next to TapTap's
        self.assertEqual(clients['ether5']['add-default-route'], 'yes')

    def test_taptap_links_own_lists_are_not_menus(self):
        ops, problems = tiles.compile_board(board(('custom', {'menu': '/ip/dhcp-client/gateways', 'match': 'interface="*2"',
                                                              'set': 'chain=forward'})), CTX)
        self.assertIn('read-only list', problems[0]['message'])


class VirtualRouterTests(TestCase):
    def run_board(self, b, base='default', **kw):
        ops, problems = tiles.compile_board(b, CTX)
        self.assertEqual(problems, [])
        return sim.examine(sim.lab_state(base), ops, b, **kw)

    def test_changes_are_marked_and_nothing_is_changed_in_the_original(self):
        state = sim.lab_state('default')
        res = self.run_board(board(('dns', {'servers': '1.1.1.1'}), ('identity', {'name': 'Practice-Router'})))
        self.assertEqual(res['counts']['changed'], 1)
        self.assertEqual(res['counts']['same'], 1)                   # the name is already that
        self.assertEqual(state, sim.lab_state('default'))

    def test_guest_network_reaches_the_internet_but_not_my_network(self):
        res = self.run_board(board(('guest', {'interface': 'ether5', 'network': '10.10.10.1/24', 'down': '5M'})))
        by = {t['title'].split(' (')[0]: t for t in res['tests']}
        self.assertTrue(all(t['pass'] for t in res['tests']), res['tests'])
        self.assertEqual(by['A guest tries your own network']['got'], 'blocked')
        self.assertTrue(any('leaves the group bridge' in f['text'] for f in res['findings']))
        tr = res['after'].trace(src_ip='10.10.10.50', in_if='ether5')
        self.assertEqual(tr['verdict'], 'ok')
        self.assertTrue(any(s['stage'] == 'speed' and '5M down' in s['text'] for s in tr['steps']))

    def test_blocked_site_and_blocked_device(self):
        res = self.run_board(board(('block_site', {'sites': 'tiktok.com'}), ('block_device', {'mac': 'AA:BB:CC:11:22:33', 'name': 'Phone'})))
        self.assertEqual([(t['want'], t['pass']) for t in res['tests']],
                         [('blocked', True), ('reaches', True), ('blocked', True), ('reaches', True)])
        self.assertEqual(res['after'].trace(src_ip='192.168.88.50', in_if='bridge', host='www.tiktok.com')['verdict'], 'blocked')

    def test_open_port_that_the_firewall_still_blocks_is_reported(self):
        res = self.run_board(board(('port_forward', {'port': '8080', 'to_ip': '192.168.88.20'})))
        self.assertTrue(res['tests'][0]['pass'], res['tests'][0])    # the default firewall lets dst-natted traffic in
        res = self.run_board(board(('port_forward', {'port': '8080', 'to_ip': '172.20.0.9'})))
        self.assertTrue(any('no local network contains' in f['text'] for f in res['findings']))

    def test_overlap_missing_port_and_address_on_a_member_port(self):
        res = self.run_board(board(('lan', {'interface': 'ether9', 'network': '192.168.88.1/24'})))
        text = ' | '.join(f['text'] for f in res['findings'] if f['level'] == 'bad')
        self.assertIn('no port or group called “ether9”', text)
        self.assertIn('overlaps', text)
        self.assertFalse(res['safe'])

    def test_a_change_that_would_lock_taptap_out_is_caught(self):
        router = Router(name='Mast', ip_address='192.168.88.1', api_port=8728, connection_mode='api')
        res = self.run_board(board(('services', {'off': 'api, telnet'})), router=router)
        self.assertFalse(res['safe'])
        self.assertTrue(any('TapTap could no longer reach the router' in f['text'] for f in res['findings']))
        lifeline = [t for t in res['tests'] if t.get('lifeline')][0]
        self.assertFalse(lifeline['pass'])
        ok = self.run_board(board(('services', {'off': 'telnet, ftp'})), router=router)
        self.assertTrue(ok['safe'])
        self.assertTrue([t for t in ok['tests'] if t.get('lifeline')][0]['pass'])

    def test_shield_keeps_strangers_out_and_taptap_in(self):
        router = Router(name='Mast', ip_address='41.1.1.1', api_port=8728, connection_mode='api')
        state = sim.lab_state('empty')                                # a router that is already online with a public address
        state['/ip/address'] = [{'id': '*1', 'address': '41.1.1.1/30', 'interface': 'ether1'}, {'id': '*2', 'address': '192.168.88.1/24', 'interface': 'ether2'}]
        state['/ip/route'] = [{'id': '*3', 'dst-address': '0.0.0.0/0', 'gateway': '41.1.1.2'}]
        b = board(('fw_basic', {'wan': 'ether1'}))
        ops, _ = tiles.compile_board(b, {'ros_major': 7, 'trusted': ['102.5.5.5']})
        res = sim.examine(state, ops, b, router=router, trusted=['102.5.5.5'])
        self.assertTrue(res['safe'], res['findings'])
        titles = {t['title']: t['pass'] for t in res['tests']}
        self.assertTrue(titles['A stranger on the internet tries WinBox through ether1'])
        self.assertTrue(titles['You, on ether2, open WinBox'])
        self.assertTrue(titles['TapTap (102.5.5.5) → the router, API port 8728'])
        # Without TapTap's address on the trusted list the same shield would cut TapTap off: reported, not deployed blind.
        ops, _ = tiles.compile_board(b, CTX)
        res = sim.examine(state, ops, b, router=router, trusted=['102.5.5.5'])
        self.assertFalse(res['safe'])
        self.assertTrue(any('TapTap could no longer reach the router' in f['text'] for f in res['findings']))

    def test_rules_the_virtual_router_cannot_judge_are_skipped_not_guessed(self):
        state = sim.lab_state('default')
        state['/ip/firewall/filter'].insert(0, {'id': '*L7', 'chain': 'forward', 'action': 'drop', 'layer7-protocol': 'games'})
        tr = sim.VirtualRouter(state).trace(src_ip='192.168.88.50', in_if='bridge')
        self.assertEqual(tr['verdict'], 'ok')
        self.assertTrue(any('layer7-protocol' in s['text'] and s['level'] == 'info' for s in tr['steps']))

    def test_hotspot_login_bypass_and_free_sites(self):
        b = board(('hotspot', {'interface': 'ether5', 'network': '10.5.50.1/24'}), ('walled_garden', {'sites': 'wave.com'}),
                  ('bypass', {'mac': 'AA:BB:CC:00:00:01', 'name': 'TV'}))
        ops, _ = tiles.compile_board(b, CTX)
        vr = sim.VirtualRouter(sim.lab_state('default')).apply(ops)
        self.assertEqual(vr.trace(src_ip='10.5.50.50', in_if='ether5')['verdict'], 'login')
        self.assertEqual(vr.trace(src_ip='10.5.50.50', in_if='ether5', host='pay.wave.com')['verdict'], 'ok')
        self.assertEqual(vr.trace(src_ip='10.5.50.50', in_if='ether5', mac='AA:BB:CC:00:00:01')['verdict'], 'ok')


class DeployTests(Base):
    BOARD = [('guest', {'interface': 'ether5', 'network': '10.10.10.1/24', 'down': '5M'}), ('dns', {'servers': '1.1.1.1'})]

    def test_timer_first_then_rows_then_keep(self):
        svc = FakeService(self.state)
        d = cs.deploy_direct(self.deploy_for(board(*self.BOARD)), connect=lambda r: svc)
        self.assertEqual(d.status, 'applied')
        first_write = next(c for c in svc.calls if c[0] in ('add', 'set'))
        self.assertEqual((first_write[1], first_write[2]['name']), ('/system/scheduler', cs.UNDO_SCHEDULER))   # the safety net goes in first
        self.assertIn('/ip dns set servers=""', first_write[2]['on-event'])                                    # …and holds the old values
        self.assertTrue(any(r['address'] == '10.10.10.1/24' and r['interface'] == 'ether5' for r in self.state['/ip/address']))
        self.assertEqual(self.state['/ip/dns'][0]['servers'], '1.1.1.1')
        wall = self.state['/ip/firewall/filter'][4]                 # above the first forward rule, below the input rules
        self.assertEqual((wall['chain'], wall['action'], wall['src-address']), ('forward', 'drop', '10.10.10.0/24'))
        self.assertFalse(any(r.get('interface') == 'ether5' for r in self.state['/interface/bridge/port']))
        cs.keep(d, connect=lambda r: svc)
        d.refresh_from_db()
        self.assertEqual(d.status, 'kept')
        self.assertEqual(self.state.get('/system/scheduler'), [])

    def test_undo_puts_everything_back(self):
        original = json.loads(json.dumps(self.state))
        svc = FakeService(self.state)
        d = cs.deploy_direct(self.deploy_for(board(*self.BOARD)), connect=lambda r: svc)
        cs.undo(d, connect=lambda r: svc)
        d.refresh_from_db()
        self.assertEqual((d.status, d.undone_by, d.error), ('undone', 'user', ''))
        strip = lambda rows: sorted(json.dumps({k: v for k, v in r.items() if k not in ('id', 'comment')}, sort_keys=True) for r in rows)
        for path in ('/ip/address', '/ip/pool', '/ip/dhcp-server', '/ip/dhcp-server/network', '/ip/firewall/filter', '/ip/firewall/address-list',
                     '/queue/simple', '/ip/dns', '/interface/bridge/port'):
            self.assertEqual(strip(self.state.get(path, [])), strip(original.get(path, [])), path)

    def test_a_refused_row_takes_back_the_rows_before_it(self):
        original = json.loads(json.dumps(self.state))
        svc = FakeService(self.state, fail_on=lambda path, fields: path == '/queue/simple')
        d = cs.deploy_direct(self.deploy_for(board(*self.BOARD)), connect=lambda r: svc)
        self.assertEqual(d.status, 'failed')
        self.assertIn('All guests together', d.error)
        self.assertIn('the router is as it was', d.error)
        self.assertEqual(self.state['/ip/address'], original['/ip/address'])
        self.assertEqual(len(self.state['/ip/firewall/filter']), len(original['/ip/firewall/filter']))
        self.assertEqual(self.state.get('/system/scheduler'), [])
        self.assertEqual(self.state['/ip/dns'][0]['servers'], '')    # the DNS tile was never reached

    def test_nobody_pressed_keep(self):
        svc = FakeService(self.state)
        d = cs.deploy_direct(self.deploy_for(board(*self.BOARD)), connect=lambda r: svc)
        RouterStudioDeploy.objects.filter(pk=d.pk).update(undo_until=timezone.now() - timedelta(minutes=1))
        d.refresh_from_db()
        self.assertEqual(cs.deploy_json(d)['status'], 'undone')
        self.assertEqual(d.undone_by, 'router')
        with self.assertRaises(cs.StudioError):
            cs.keep(d, connect=lambda r: svc)

    def test_keep_after_the_router_already_undid_it(self):
        svc = FakeService(self.state)
        d = cs.deploy_direct(self.deploy_for(board(*self.BOARD)), connect=lambda r: svc)
        self.state['/system/scheduler'] = []                         # the timer ran on the router a moment ago
        cs.keep(d, connect=lambda r: svc)
        d.refresh_from_db()
        self.assertEqual((d.status, d.undone_by), ('undone', 'router'))

    def test_passwords_are_never_put_into_the_undo(self):
        self.state['/interface/pppoe-client'] = [{'id': '*9', 'name': 'pppoe-ether1', 'interface': 'ether1', 'user': 'old', 'password': 'oldpw'}]
        svc = FakeService(self.state)
        d = cs.deploy_direct(self.deploy_for(board(('wan_pppoe', {'port': 'ether1', 'user': 'new', 'password': 'newpw'}))), connect=lambda r: svc)
        self.assertNotIn('oldpw', json.dumps(d.rollback_plan))
        self.assertIn('"user": "old"', json.dumps(d.rollback_plan))


class LinkDeployTests(Base):
    def setUp(self):
        super().setUp()
        Router.objects.filter(pk=self.r.pk).update(connection_mode='agent')
        self.r.refresh_from_db()
        RouterAgent.objects.create(router=self.r, token_hash='h' * 64, last_seen_at=timezone.now(), enrolled_at=timezone.now(), ros_version='7.16.2')

    def cmds(self):
        return list(AgentCommand.objects.filter(router=self.r, kind=cs.LINK_KIND).order_by('pk'))

    def answer(self, status='ok', result=''):
        cmd = [c for c in self.cmds() if c.status in ('queued', 'sent')][-1]
        self.assertTrue(agent.handle_ack(cmd.pk, agent.nonce(cmd), status, result))
        return cmd

    def start(self):
        d = self.deploy_for(board(*[(t['type'], t['params']) for t in cs.KITS[1]['tiles']]))
        cs.deploy_link(d, self.state)
        return d

    def answer_step(self, step, status='ok', result=''):
        cmd = [c for c in self.cmds() if c.params.get('step') == step][-1]
        self.assertTrue(agent.handle_ack(cmd.pk, agent.nonce(cmd), status, result), step)
        return cmd

    def answer_all(self, d):
        for step in d.link_parts['order']:
            self.answer_step(step)

    def lose(self, *steps):
        """The router ran these (or not) but no answer reached TapTap: the Link queue gave up on them."""
        AgentCommand.objects.filter(router=self.r, kind=cs.LINK_KIND, params__step__in=list(steps)).update(
            status='failed', result='No answer from the router', done_at=timezone.now())

    def test_the_whole_chain_goes_at_once_timer_first_each_step_guarded(self):
        d = self.start()
        order = d.link_parts['order']
        self.assertEqual([c.params['step'] for c in self.cmds()], order)            # all queued at once — no waiting
        self.assertTrue(order[0].startswith('u'))
        self.assertLess(order.index('arm'), order.index('p0'))                      # the undo timer before any change
        arm = agent.command_body([c for c in self.cmds() if c.params['step'] == 'arm'][0])
        self.assertIn(f'/system scheduler add name={cs.UNDO_SCHEDULER} interval=8m', arm)     # at least 8 min on Link
        self.assertIn(':if ($s >= ', arm)                                           # skipped when already done
        self.assertIn('an earlier step did not run', arm)                           # refused when out of order
        wrapped = agent.wrap(self.cmds()[-1], 'https://t.example', '')
        self.assertIn(':for tti from=1 to=5', wrapped)                              # the answer is retried
        for step in order:
            d.refresh_from_db()
            self.assertEqual(d.status, 'sending')
            self.answer_step(step)
        d.refresh_from_db()
        self.assertEqual(d.status, 'applied')
        self.assertIsNotNone(d.undo_until)
        cs.keep(d)
        self.assertIn('already undone', agent.command_body(self.cmds()[-1]))
        self.answer_step('keep')
        d.refresh_from_db()
        self.assertEqual(d.status, 'kept')

    def test_the_browser_cannot_choose_what_a_link_command_runs(self):
        d = self.start()
        cmd = self.cmds()[0]
        for bad in ({'deploy': d.pk, 'step': 'order'}, {'deploy': d.pk, 'step': 'steps'}, {'deploy': d.pk + 99, 'step': 'p0'}):
            cmd.params = bad
            with self.assertRaises(ValueError):
                cs.link_body(cmd)
        with self.assertRaises(ValueError):                            # a board cannot smuggle a script kind through the queue
            agent.queue(self.r, 'script', {'source': '/system reboot'})

    def test_a_refused_piece_stops_and_undoes(self):
        d = self.start()
        order = d.link_parts['order']
        for step in order[:order.index('p0') + 1]:
            self.answer_step(step)
        self.answer_step('p1', 'fail', 'no such item')
        d.refresh_from_db()
        self.assertEqual(d.status, 'failed')
        self.assertIn('no such item', d.error)
        later = [c for c in self.cmds() if c.params['step'] in order[order.index('p1') + 1:]]
        self.assertTrue(all(c.status == 'cancelled' for c in later))                # the rest never runs
        self.assertEqual(self.cmds()[-1].params['step'], 'undo')
        self.assertIn(f'/system script run {cs.UNDO_SCRIPT}', agent.command_body(self.cmds()[-1]))
        for step in d.link_parts['undo_order']:
            self.answer_step(step)
        d.refresh_from_db()
        self.assertEqual(d.status, 'failed')
        self.assertIn('taken back', d.error)

    def test_lost_answers_are_resolved_by_asking_the_router(self):
        d = self.start()
        order = d.link_parts['order']
        for step in order[:-2]:
            self.answer_step(step)
        self.lose(*order[-2:])                         # the internet blinked: two answers never arrived
        cs.progress(RouterStudioDeploy.objects.get(pk=d.pk))
        d.refresh_from_db()
        self.assertEqual((d.status, self.cmds()[-1].params['step']), ('sending', 'check'))
        self.assertIn(f'!= {len(order) - 1})', agent.command_body(self.cmds()[-1]))
        self.answer_step('check')
        d.refresh_from_db()
        self.assertEqual(d.status, 'applied')

    def test_a_broken_chain_is_sent_once_more_then_taken_back(self):
        d = self.start()
        self.lose(*d.link_parts['order'])
        cs.progress(RouterStudioDeploy.objects.get(pk=d.pk))
        self.answer_step('check', 'fail', 'stopped at step 2')
        d.refresh_from_db()
        self.assertEqual((d.status, d.link_parts['round']), ('sending', 2))          # a fresh round with a fresh marker
        self.assertIn('ttS%dr2' % d.pk, agent.command_body(self.cmds()[-1]))
        self.lose(*d.link_parts['order'])
        cs.progress(RouterStudioDeploy.objects.get(pk=d.pk))
        self.answer_step('check', 'fail')
        d.refresh_from_db()
        self.assertEqual(d.status, 'failed')
        self.assertIn('2 times', d.error)
        self.assertEqual(self.cmds()[-1].params['step'], 'undo')

    def test_it_never_loops_for_ever(self):
        d = self.start()
        d.link_parts['started_at'] = (timezone.now() - timedelta(minutes=cs.DEADLINE + 1)).isoformat()
        d.save()
        cs.progress(d)
        d.refresh_from_db()
        self.assertEqual(d.status, 'failed')
        self.assertIn('did not get the change through', d.error)
        self.assertFalse(AgentCommand.objects.filter(router=self.r, kind=cs.LINK_KIND, status='queued',
                                                     params__step__startswith='p').exists())

    def test_a_change_stuck_with_the_old_version_stops_blocking(self):
        d = self.deploy_for(board(('dns', {'servers': '1.1.1.1'})))
        RouterStudioDeploy.objects.filter(pk=d.pk).update(status='sending', transport='TapTap Link', link_parts={'order': ['arm', 'p0']},
                                                          created_at=timezone.now() - timedelta(hours=2))
        d.refresh_from_db()
        cs.progress(d)
        d.refresh_from_db()
        self.assertEqual(d.status, 'failed')
        self.assertIn('never finished', d.error)

    def test_keep_by_itself_once_the_router_stays_online(self):
        d = self.deploy_for(board(*[(t['type'], t['params']) for t in cs.KITS[1]['tiles']]))
        cs.deploy_link(d, self.state, auto_keep=True)
        self.answer_all(d)
        d.refresh_from_db()
        self.assertEqual(d.status, 'applied')
        cs.progress(d)
        self.assertFalse(any(c.params['step'] == 'keep' for c in self.cmds()))     # not before the router proved itself
        RouterAgent.objects.filter(router=self.r).update(last_seen_at=d.applied_at + timedelta(seconds=cs.KEEP_AFTER + 5))
        cs.link_progress(self.r)                                                     # what every check-in runs
        self.answer_step('keep')
        d.refresh_from_db()
        self.assertEqual(d.status, 'kept')

    def test_a_lost_keep_answer_is_checked_not_assumed_undone(self):
        d = self.start()
        self.answer_all(d)
        d.refresh_from_db()
        cs.keep(d)
        self.lose('keep')
        RouterStudioDeploy.objects.filter(pk=d.pk).update(undo_until=timezone.now() - timedelta(minutes=1))
        d.refresh_from_db()
        cs.settle(d)
        self.assertEqual(self.cmds()[-1].params['step'], 'kcheck')
        d.refresh_from_db()
        self.assertEqual(d.status, 'applied')                                        # not "undone" on a guess
        self.answer_step('kcheck')
        d.refresh_from_db()
        self.assertEqual(d.status, 'kept')

    def test_keep_too_late_is_reported_as_undone(self):
        d = self.start()
        self.answer_all(d)
        d.refresh_from_db()
        cs.keep(d)
        self.answer_step('keep', 'fail')
        d.refresh_from_db()
        self.assertEqual((d.status, d.undone_by), ('undone', 'router'))

    def test_stop_while_on_its_way(self):
        d = self.start()
        out = self.post('config_studio_action', {'action': 'stop'}, d.pk).json()
        self.assertTrue(out['ok'], out)
        self.assertEqual(out['deploy']['status'], 'failed')
        self.assertIn('Nothing had reached the router', out['deploy']['error'])
        self.assertFalse(AgentCommand.objects.filter(router=self.r, kind=cs.LINK_KIND, status='queued').exists())

    def test_the_page_shows_every_step(self):
        d = self.start()
        self.answer_step(d.link_parts['order'][0])
        tl = cs.deploy_json(RouterStudioDeploy.objects.get(pk=d.pk))['timeline']
        self.assertEqual(tl['steps'][0], {'key': 'undo', 'label': 'Undo plan onto the router', 'state': 'wait'})
        self.assertTrue(any(x['key'] == 'arm' for x in tl['steps']))
        self.assertIn('checks in', tl['note'])

    def test_deploy_through_link_makes_its_own_backup_first(self):
        self.post('config_studio_check', {'tiles': [{'id': 'a', 'type': 'dns', 'params': {'servers': '1.1.1.1'}}]})
        out = self.post('config_studio_deploy', {'undo_minutes': 5}).json()
        self.assertTrue(out['ok'], out)
        d = RouterStudioDeploy.objects.get()
        self.assertEqual(d.link_parts['order'][0], 'backup')
        self.assertIn('/system backup save name="taptap-studio-', d.link_parts['steps']['backup'])
        self.assertEqual(d.backup.name, d.link_parts['backup_name'])


@override_settings(AUTH_EMAIL_OTP=False)
class ViewTests(Base):
    def test_page_and_live_check(self):
        from django.urls import reverse
        page = self.c.get(reverse('config_studio', args=[self.r.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'csBoot')
        self.assertContains(page, 'Shield the router')
        out = self.post('config_studio_check', {'tiles': [{'id': 'a', 'type': 'guest', 'params': {'interface': 'ether5'}}], 'base': 'router'}).json()
        self.assertTrue(out['ok'])
        self.assertTrue(out['safe'])
        self.assertEqual(RouterStudioBoard.objects.get(router=self.r).tiles[0]['type'], 'guest')
        self.assertTrue(any(p['name'] == 'ether5' and p['role'] == 'guest' for p in out['picture']['ports']))
        self.assertTrue(any(s['path'] == '/ip/firewall/filter' and s['touched'] for s in out['sections']))
        rows = self.post('config_studio_section', {'path': '/ip/firewall/filter', 'view': 'after'}).json()['rows']
        self.assertEqual(sum(1 for r in rows if r['mark'] == 'new'), 1)
        self.assertEqual(self.post('config_studio_section', {'path': '/ip/firewall/filter', 'view': 'now'}).json()['total'], 8)
        tr = self.post('config_studio_trace', {'ip': '10.10.10.50', 'iface': 'ether5', 'to': '192.168.88.20', 'port': 80}).json()['trace']
        self.assertEqual(tr['verdict'], 'blocked')

    def test_practice_router_needs_no_real_one(self):
        RouterConfigSnapshot.objects.all().delete()
        out = self.post('config_studio_check', {'tiles': [], 'base': 'router'}).json()
        self.assertTrue(out['needs_read'])
        out = self.post('config_studio_check', {'tiles': [{'id': 'a', 'type': 'wan_dhcp', 'params': {}}], 'base': 'lab-empty'}).json()
        self.assertTrue(out['info']['lab'])
        self.assertEqual(out['picture']['wans'], ['ether1'])

    def test_deploy_needs_a_backup_a_clean_board_and_a_real_change(self):
        self.assertEqual(self.post('config_studio_deploy').status_code, 400)                       # empty board
        self.post('config_studio_check', {'tiles': [{'id': 'a', 'type': 'identity', 'params': {'name': 'Practice-Router'}}]})
        self.assertIn('already has everything', self.post('config_studio_deploy').json()['message'])
        self.post('config_studio_check', {'tiles': [{'id': 'a', 'type': 'services', 'params': {'off': 'api'}}]})
        r = self.post('config_studio_deploy')
        self.assertEqual((r.status_code, r.json()['needs']), (409, 'DEPLOY ANYWAY'))               # would lock TapTap out
        self.post('config_studio_check', {'tiles': [{'id': 'a', 'type': 'dns', 'params': {'servers': '1.1.1.1'}}]})
        self.assertEqual(RouterStudioDeploy.objects.count(), 0)
        svc = FakeService(self.state)
        made = RouterBackup.objects.create(router=self.r, name='auto', backup_file='auto.backup')
        made.created_at = timezone.now() - timedelta(hours=3)        # an old backup does not count …
        made.save()
        with mock.patch('core.mikrotik.MikroTikService.connect', return_value=svc), mock.patch('core.config_studio._reread'), \
                mock.patch('core.config_studio.start_backup', side_effect=lambda *a, **k: RouterBackup.objects.create(router=self.r, name='b1', backup_file='b1.backup')) as sb:
            out = self.post('config_studio_deploy', {'undo_minutes': 3, 'auto_keep': False}).json()
        self.assertTrue(sb.called)                                    # … so a fresh one is made by itself first
        self.assertTrue(out['ok'], out)
        self.assertEqual((out['deploy']['status'], out['deploy']['undo_minutes']), ('applied', 3))
        self.assertGreater(out['deploy']['seconds_left'], 150)
        self.assertEqual(RouterStudioDeploy.objects.get().backup.name, 'b1')
        self.assertEqual(self.post('config_studio_deploy').status_code, 409)                       # decide Keep / Undo first
        with mock.patch('core.mikrotik.MikroTikService.connect', return_value=svc), mock.patch('core.config_studio._reread'):
            kept = self.post('config_studio_action', {'action': 'keep'}, out['deploy']['id']).json()
        self.assertEqual(kept['deploy']['status'], 'kept')

    def test_an_unreachable_router_is_a_failed_deploy_not_a_crash(self):
        from core.mikrotik import MikroTikError
        RouterBackup.objects.create(router=self.r, name='b1', backup_file='b1.backup')
        self.post('config_studio_check', {'tiles': [{'id': 'a', 'type': 'dns', 'params': {'servers': '1.1.1.1'}}]})
        with mock.patch('core.mikrotik.MikroTikService.connect', side_effect=MikroTikError('timed out')):
            out = self.post('config_studio_deploy').json()
        self.assertFalse(out['ok'])
        self.assertEqual(out['deploy']['status'], 'failed')
        self.assertIn('timed out', out['message'])

    def test_other_businesses_and_staff_without_network_rights_stay_out(self):
        from django.urls import reverse
        other = User.objects.create_user('p@x.gm', 'p@x.gm', 'pw-123456')
        Business.objects.create(user=other, business_name='Other', owner_name='P', phone='2', trial_ends_at=timezone.now() + timedelta(days=30))
        c = Client()
        c.force_login(other)
        self.assertEqual(c.get(reverse('config_studio', args=[self.r.pk])).status_code, 404)
        self.assertEqual(c.post(reverse('config_studio_deploy', args=[self.r.pk]), '{}', content_type='application/json').status_code, 404)
        staff = User.objects.create_user('s@x.gm', 's@x.gm', 'pw-123456')
        TeamMember.objects.create(business=self.biz, user=staff, role='voucher_support')
        c = Client()
        c.force_login(staff)
        for name in ('config_studio', 'config_studio_state', 'config_studio_export'):
            self.assertNotEqual(c.get(reverse(name, args=[self.r.pk])).status_code, 200, name)
        self.assertNotEqual(c.post(reverse('config_studio_deploy', args=[self.r.pk]), '{}', content_type='application/json').status_code, 200)

    def test_export_is_a_script_file(self):
        from django.urls import reverse
        self.post('config_studio_check', {'tiles': [{'id': 'a', 'type': 'block_site', 'params': {'sites': 'tiktok.com'}}]})
        r = self.c.get(reverse('config_studio_export', args=[self.r.pk]))
        self.assertIn('attachment; filename="Mast-studio.rsc"', r['Content-Disposition'])
        self.assertIn('/ip firewall address-list add list="tt-studio-blocked-sites" address="tiktok.com"', r.content.decode())


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'studio-ai'}})
class HelperTests(Base):
    def vr(self):
        return sim.VirtualRouter(self.state)

    def test_phrase_reader_understands_everyday_requests(self):
        kinds = lambda text: [(a['type'], a['params']) for a in ai.phrase_reader(text, self.vr())['add']]
        self.assertEqual(kinds('guest wifi on ether5 at 5 mbps and block tiktok.com'),
                         [('guest', {'interface': 'ether5', 'down': '5M'}), ('block_site', {'sites': 'tiktok.com'})])
        self.assertEqual(kinds('open port 8080 to 192.168.88.20'), [('port_forward', {'protocol': 'tcp', 'port': '8080', 'to_ip': '192.168.88.20'})])
        self.assertEqual(kinds('limit 192.168.88.50 to 2 Mbps'), [('speed_limit', {'target': '192.168.88.50', 'down': '2M', 'up': '2M'})])
        self.assertEqual(kinds('protect the router from hackers'), [('fw_basic', {'wan': 'ether1'})])
        self.assertEqual(ai.phrase_reader('what is the weather', self.vr())['add'], [])

    def test_without_an_ai_key_the_box_still_builds(self):
        with mock.patch.dict('os.environ', {'GEMINI_API_KEY': '', 'GROQ_API_KEY': ''}):
            out = self.post('config_studio_ai', {'text': 'block bet365.com', 'tiles': []}).json()
        self.assertTrue(out['ok'])
        self.assertEqual(out['engine'], 'Phrase reader')
        self.assertEqual([t['type'] for t in out['tiles']], ['block_site'])
        self.assertEqual(out['touched'], [out['tiles'][0]['id']])

    def test_ai_answers_become_checked_tiles_only(self):
        reply = {'say': 'Guests are on ether5.', 'add': [{'type': 'guest', 'params': {'interface': 'ether5', 'network': '10.9.9.1/24', 'evil': 'x'}},
                                                         {'type': 'run_script', 'params': {'source': '/system reboot'}},
                                                         {'type': 'speed_limit', 'params': {'target': 'everyone', 'down': '5M'}}],
                 'change': [{'id': 'keep', 'params': {'servers': '9.9.9.9'}}], 'remove': ['gone']}
        seen = {}

        def fake_post(model, payload, provider='gemini'):
            seen['prompt'] = payload['messages'][0]['content']
            return {'choices': [{'message': {'content': '```json\n' + json.dumps(reply) + '\n```'}}]}

        start = [{'id': 'keep', 'type': 'dns', 'params': {'servers': '1.1.1.1'}}, {'id': 'gone', 'type': 'identity', 'params': {'name': 'X'}},
                 {'id': 'pw', 'type': 'wan_pppoe', 'params': {'port': 'ether1', 'user': 'u', 'password': 'hunter2'}}]
        with mock.patch.dict('os.environ', {'GEMINI_API_KEY': 'k'}), mock.patch('core.fit_ai._post', fake_post):
            out = self.post('config_studio_ai', {'text': 'give guests their own network', 'tiles': start}).json()
        self.assertTrue(out['ok'], out)
        got = {t['id']: t for t in out['tiles']}
        self.assertEqual(sorted(t['type'] for t in out['tiles']), ['dns', 'guest', 'speed_limit', 'wan_pppoe'])
        self.assertEqual(got['keep']['params']['servers'], '9.9.9.9')
        guest = [t for t in out['tiles'] if t['type'] == 'guest'][0]
        self.assertNotIn('evil', guest['params'])
        self.assertIn('Still to fill in', out['say'])                 # the speed limit it proposed has a wrong target: said, not hidden
        self.assertNotIn('hunter2', seen['prompt'])                   # passwords never go to the AI
        self.assertIn('never write RouterOS commands', seen['prompt'])
        self.assertEqual(RouterStudioDeploy.objects.count(), 0)       # the helper only changes the board

    def test_a_broken_ai_answer_falls_back(self):
        with mock.patch.dict('os.environ', {'GEMINI_API_KEY': 'k'}), \
                mock.patch('core.fit_ai._post', return_value={'choices': [{'message': {'content': 'Sure! I reset your router.'}}]}):
            out = self.post('config_studio_ai', {'text': 'block tiktok.com', 'tiles': []}).json()
        self.assertEqual([t['type'] for t in out['tiles']], ['block_site'])
        self.assertIn('built-in reader', out['say'])
