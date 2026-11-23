"""Line Hub (core/line_hub.py): who is on which line, moving one or many customers, and every gateway switch —
for both channels (TapTap Link and the RouterOS API) — plus the detection fixes behind it (orphan "*2" clients,
"192.168.1.1@table" gateways) and the checks that stop a tampered command from reaching a router."""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core import agent, line_hub as hub, line_steer as ls, wan
from core.models import AgentCommand, Business, Router, RouterAgent, RouterConfigSnapshot, WanSetup
from core.models_flow import LinePin
from core.routeros_analysis import resolve_gateways

CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'hub'}}
URL = 'https://taptap.example'
NOW = lambda: timezone.now().isoformat()

# Two lines in a TapTap "Backup line" setup: no sharing rules, but each line has its mark and table.
MANGLE_FAILOVER = [
    {'id': '*1', 'chain': 'prerouting', 'dst-address-list': 'TT-local', 'action': 'accept', 'comment': 'TapTap WAN #abc · leave local traffic alone'},
    {'id': '*2', 'chain': 'prerouting', 'in-interface': 'ether1', 'connection-state': 'new', 'connection-mark': 'no-mark',
     'action': 'mark-connection', 'new-connection-mark': 'TT-ether1', 'passthrough': 'yes', 'comment': 'TapTap WAN #abc · arrived on Starlink'},
    {'id': '*3', 'chain': 'prerouting', 'in-interface': 'ether5', 'connection-state': 'new', 'connection-mark': 'no-mark',
     'action': 'mark-connection', 'new-connection-mark': 'TT-ether5', 'passthrough': 'yes', 'comment': 'TapTap WAN #abc · arrived on QCell'},
    {'id': '*4', 'chain': 'prerouting', 'connection-mark': 'TT-ether1', 'in-interface-list': '!TT-WAN', 'action': 'mark-routing',
     'new-routing-mark': 'TT-ether1', 'passthrough': 'no', 'comment': 'TapTap WAN #abc · route via Starlink'},
    {'id': '*5', 'chain': 'prerouting', 'connection-mark': 'TT-ether5', 'in-interface-list': '!TT-WAN', 'action': 'mark-routing',
     'new-routing-mark': 'TT-ether5', 'passthrough': 'no', 'comment': 'TapTap WAN #abc · route via QCell'},
]
IFACES = [{'name': 'ether1', 'type': 'ether', 'running': 'true', 'disabled': 'false'},
          {'name': 'ether3', 'type': 'ether', 'running': 'true', 'disabled': 'false'},
          {'name': 'ether5', 'type': 'ether', 'running': 'true', 'disabled': 'false'},
          {'name': 'bridge-hotspot', 'type': 'bridge', 'running': 'true', 'disabled': 'false'}]
DHCP_CLIENTS = [{'id': '*A', 'interface': 'ether1', 'status': 'bound', 'address': '192.168.1.20/24', 'gateway': '192.168.1.1', 'disabled': 'false', 'add-default-route': 'no'},
                {'id': '*B', 'interface': 'ether5', 'status': 'bound', 'address': '192.168.2.30/24', 'gateway': '192.168.2.1', 'disabled': 'false', 'add-default-route': 'no'},
                {'id': '*C', 'interface': '*2', 'status': 'stopped', 'disabled': 'false'}]
ADDRS = [{'address': '10.5.50.1/24', 'interface': 'bridge-hotspot'}, {'address': '192.168.1.20/24', 'interface': 'ether1'},
         {'address': '192.168.2.30/24', 'interface': 'ether5'}]
ROUTES = [{'id': '*R1', 'dst-address': '1.1.1.1/32', 'gateway': '192.168.1.1%ether1', 'comment': 'TapTap WAN #abc · check 1.1.1.1 via Starlink', 'active': 'true'},
          {'id': '*R2', 'dst-address': '0.0.0.0/0', 'gateway': '1.1.1.1', 'distance': '1', 'comment': 'TapTap WAN #abc · main line: Starlink', 'active': 'true'},
          {'id': '*R3', 'dst-address': '8.8.8.8/32', 'gateway': '192.168.2.1%ether5', 'comment': 'TapTap WAN #abc · check 8.8.8.8 via QCell', 'active': 'true'},
          {'id': '*R4', 'dst-address': '0.0.0.0/0', 'gateway': '8.8.8.8', 'distance': '2', 'comment': 'TapTap WAN #abc · backup 1 line: QCell', 'active': 'false'}]
NAT = [{'id': '*N1', 'chain': 'srcnat', 'action': 'masquerade', 'out-interface': 'ether1', 'comment': 'TapTap WAN #abc · hide customers behind Starlink'},
       {'id': '*N2', 'chain': 'srcnat', 'action': 'masquerade', 'out-interface': 'ether5', 'comment': 'TapTap WAN #abc · hide customers behind QCell'}]
FILTER = [{'id': '*F1', 'chain': 'input', 'action': 'accept', 'connection-state': 'established,related', 'comment': 'defconf: accept established'},
          {'id': '*F2', 'chain': 'input', 'action': 'drop', 'in-interface-list': 'WAN', 'comment': 'defconf: drop all not coming from LAN'},
          {'id': '*F3', 'chain': 'forward', 'action': 'jump', 'comment': 'TapTap IPS', 'jump-target': 'taptap-ips'}]
CONFIG = {'strategy': 'failover', 'health': True, 'links': [
    {'interface': 'ether1', 'type': 'dhcp', 'gateway': '192.168.1.1', 'label': 'Starlink', 'speed': 100, 'check': '1.1.1.1', 'enabled': True},
    {'interface': 'ether5', 'type': 'dhcp', 'gateway': '192.168.2.1', 'label': 'QCell', 'speed': 20, 'check': '8.8.8.8', 'enabled': True}]}


@override_settings(CACHES=CACHE, AUTH_EMAIL_OTP=False, SITE_URL=URL)
class Base(TestCase):
    link = True

    def setUp(self):
        cache.clear()
        cache.set('tt:lh:ttaddr', ['203.0.113.7'], 600)          # TapTap's public address (no DNS in tests)
        self.owner = User.objects.create_user('owner@x.gm', 'owner@x.gm', 'pw-123456')
        self.biz = Business.objects.create(user=self.owner, business_name='Kerr Sering', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        if self.link:
            self.router = Router.objects.create(business=self.biz, name='TapTap K', connection_mode='agent', status='Online')
            RouterAgent.objects.create(router=self.router, token_hash='h' * 64, last_seen_at=timezone.now(), enrolled_at=timezone.now(),
                                       script_version=9, ros_version='7.15.3 (stable)')
        else:
            self.router = Router.objects.create(business=self.biz, name='TapTap K', ip_address='10.0.0.1', username='a', password='p')
        self.setup = WanSetup.objects.create(router=self.router, config=json.loads(json.dumps(CONFIG)), status='active',
                                             facts={'v7': True, 'links': [], 'lans': []})
        self.client.force_login(self.owner)

    def snapshot(self, **over):
        rows = {'/interface': IFACES, '/interface/ethernet': [{'name': 'ether1'}, {'name': 'ether3'}, {'name': 'ether5'}],
                '/ip/dhcp-client': DHCP_CLIENTS, '/ip/address': ADDRS, '/ip/route': ROUTES, '/ip/firewall/mangle': MANGLE_FAILOVER,
                '/ip/firewall/nat': NAT, '/ip/firewall/filter': FILTER, '/ip/hotspot': [{'interface': 'bridge-hotspot'}],
                '/interface/bridge/port': [], '/ip/dhcp-server': [{'name': 'dhcp1', 'interface': 'bridge-hotspot', 'lease-time': '10m', 'address-pool': 'hs'}],
                '/ip/dhcp-server/network': [{'address': '10.5.50.0/24', 'gateway': '10.5.50.1', 'dns-server': ''}],
                '/ip/pool': [{'name': 'hs', 'ranges': '10.5.50.10-10.5.50.254'}]}
        rows.update(over)
        sections = {p: {'path': p, 'rows': r, 'at': NOW()} for p, r in rows.items()}
        return RouterConfigSnapshot.objects.update_or_create(router=self.router, defaults={'sections': sections})[0]

    def sessions(self, *people):
        users = {}
        for code, ip, mac in people:
            users.setdefault(code, []).append({'ip': ip, 'mac': mac, 'down_bps': 2_000_000, 'up_bps': 100_000})
        cache.set(f'tt:tr:users:{self.router.pk}', {'at': NOW(), 'users': users}, 600)

    def post(self, url, data):
        return self.client.post(url, json.dumps(data), content_type='application/json')

    def cmds(self):
        return list(AgentCommand.objects.filter(router=self.router, kind=hub.LINK_KIND).order_by('pk'))


# ═══════════════════════ the bugs on the screenshot ═══════════════════════
class DetectionFixes(TestCase):
    def test_a_gateway_looked_up_in_another_table_is_not_a_port(self):
        hops = resolve_gateways({'gateway': '192.168.1.1@starlink2', 'immediate-gw': '192.168.1.1%ether2', 'gateway-status': ''})
        self.assertEqual(hops[0]['gateway'], '192.168.1.1')
        self.assertEqual(hops[0]['interface'], 'ether2')

    def test_a_client_on_a_deleted_port_is_an_orphan_with_a_clear_message(self):
        self.assertTrue(wan.is_orphan('*2'))
        self.assertTrue(wan.is_orphan('ether9', {'ether1': {}}))
        self.assertFalse(wan.is_orphan('ether1', {'ether1': {}}))
        cfg, errors, _ = wan.normalize({'strategy': 'single', 'links': [{'interface': '*2', 'type': 'dhcp', 'enabled': True}]}, {})
        self.assertIn('no longer exists', errors[0])
        cfg, errors, _ = wan.normalize({'strategy': 'single', 'links': [{'interface': '*2', 'enabled': False},
                                                                         {'interface': 'ether1', 'type': 'static', 'gateway': '10.0.0.1'}]}, {})
        self.assertEqual(errors, [])                                       # unticked: ignored

    def test_every_line_table_has_fallbacks_so_a_moved_customer_keeps_the_internet(self):
        cfg, errors, _ = wan.normalize(CONFIG, {})
        self.assertEqual(errors, [])
        plan = wan.build_plan(cfg, {'v7': True, 'links': []})
        table_routes = [o for o in plan['ops'] if o['op'] == 'add' and o['path'] == '/ip/route'
                        and o['fields'].get('routing-table') == 'TT-ether1' and o['fields']['dst-address'] == '0.0.0.0/0']
        self.assertEqual(len(table_routes), 2)                             # own line + the other line as fallback


# ═══════════════════════ seeing who is where ═══════════════════════
class Steering(Base):
    def test_a_backup_line_setup_can_be_steered_through_its_marks(self):
        self.snapshot()
        info = ls.lanes(self.router)
        self.assertEqual(info['mode'], 'marks')
        self.assertEqual([l['mark'] for l in info['lanes']], ['TT-ether1', 'TT-ether5'])
        self.assertEqual([l['share'] for l in info['lanes']], [100, 0])
        self.assertTrue(ls.is_anchor(MANGLE_FAILOVER[3]))
        self.assertFalse(ls.is_anchor(MANGLE_FAILOVER[1]))

    def test_a_pppoe_line_is_recognised_by_its_routes(self):
        from core import wan_doctor as doc
        rows = hub.DictRows({'/ip/route': [
            {'id': '*1', 'dst-address': '9.9.9.9/32', 'gateway': 'pppoe-out1', 'comment': 'TapTap WAN #a · check 9.9.9.9 via Gamtel'},
            {'id': '*2', 'dst-address': '0.0.0.0/0', 'gateway': '9.9.9.9', 'comment': 'TapTap WAN #a · main line: Gamtel', 'active': 'true'}],
            '/interface/pppoe-client': [{'id': '*P', 'name': 'pppoe-out1', 'running': 'true'}]}, 'test')
        self.assertEqual(doc.use_state(rows, 'pppoe-out1')['state'], 'managed')   # before: "out", and "Put in use" failed

    def test_the_pulse_reuses_the_last_full_read_of_a_live_router(self):
        hub.remember_view(self.router, hub.DictRows({'/ip/dhcp-client': DHCP_CLIENTS[:2], '/interface': IFACES, '/ip/address': ADDRS,
                                                     '/ip/firewall/mangle': MANGLE_FAILOVER, '/ip/route': ROUTES}, 'Direct API'))
        with mock.patch('core.line_hub.on_link', return_value=False):
            b = hub.board(self.router)                     # no snapshot at all: the remembered read is used
        self.assertEqual([c['interface'] for c in b['columns']], ['ether1', 'ether5'])

    def test_split_by_network_is_never_steered(self):
        self.setup.config = {**CONFIG, 'strategy': 'split'}
        self.setup.save()
        self.snapshot()
        self.assertEqual(ls.lanes(self.router)['lanes'], [])

    def test_customers_are_placed_on_the_line_their_traffic_leaves_through(self):
        self.snapshot()
        self.sessions(('KS1', '10.5.50.11', 'AA:BB:CC:00:00:01'), ('KS2', '10.5.50.12', 'AA:BB:CC:00:00:02'), ('KS3', '10.5.50.13', ''))
        hub.note_nat(self.router, {'10.5.50.11': {'192.168.1.20': 9}, '10.5.50.12': {'192.168.2.30': 4, '192.168.1.20': 1}})
        b = hub.board(self.router)
        cols = {c['interface']: c for c in b['columns']}
        self.assertEqual([p['code'] for p in cols['ether1']['people']], ['KS1', 'KS3'])     # KS3: no sample → on the main line
        self.assertEqual([p['code'] for p in cols['ether5']['people']], ['KS2'])
        self.assertEqual(cols['ether1']['people'][1]['how'], 'main')
        self.assertTrue(b['can_move'])
        self.assertNotIn('*2', cols)                                     # a missing port takes no column on the board

    def test_the_traffic_sample_records_the_address_customers_leave_from(self):
        from core.traffic import ingest_connections
        ingest_connections(self.router, [
            {'.id': '*1', 'src-address': '10.5.50.11:5000', 'dst-address': '142.250.1.1:443', 'reply-dst-address': '192.168.2.30:5000',
             'orig-bytes': '5000', 'repl-bytes': '90000'}], {}, timezone.now())
        self.assertEqual(hub.nat_seen(self.router)['ips'], {'10.5.50.11': {'192.168.2.30': 1}})


# ═══════════════════════ moving customers ═══════════════════════
class Moving(Base):
    def test_moving_many_customers_through_taptap_link(self):
        self.snapshot()
        self.sessions(('KS1', '10.5.50.11', 'AA:BB:CC:00:00:01'))
        r = self.post(f'/routers/{self.router.pk}/internet/hub/move/', {'interface': 'ether5', 'devices': [
            {'ip': '10.5.50.11', 'mac': 'AA:BB:CC:00:00:01', 'code': 'KS1'}, {'ip': '10.5.50.12', 'mac': '', 'code': 'KS2'}]})
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        self.assertTrue(d['ok'] and d['via_link'])
        cmd = self.cmds()[0]
        body = agent.command_body(cmd)
        self.assertIn('new-connection-mark=TT-ether5', body)
        self.assertIn('src-mac-address=AA:BB:CC:00:00:01', body)
        self.assertIn('src-address=10.5.50.12', body)
        self.assertIn('place-before=$an', body)
        self.assertEqual(LinePin.objects.filter(router=self.router, removed_at__isnull=True, mark='TT-ether5').count(), 2)
        b = hub.board(self.router)
        ether5 = next(c for c in b['columns'] if c['interface'] == 'ether5')
        self.assertEqual([(p['code'], p['how']) for p in ether5['people']], [('KS1', 'kept')])

    def test_back_to_automatic_removes_the_pins(self):
        self.snapshot()
        self.post(f'/routers/{self.router.pk}/internet/hub/move/', {'interface': 'ether5', 'devices': [{'ip': '10.5.50.11', 'mac': '', 'code': 'KS1'}]})
        r = self.post(f'/routers/{self.router.pk}/internet/hub/move/', {'auto': True, 'devices': [{'ip': '10.5.50.11', 'mac': '', 'code': 'KS1'}]})
        self.assertTrue(r.json()['ok'])
        body = agent.command_body(self.cmds()[-1])
        self.assertIn('comment~"^TapTap line-pin ip-10-5-50-11 "', body)       # the space: never also ip-10-5-50-110
        self.assertNotIn('mangle add', body)
        self.assertFalse(LinePin.objects.filter(router=self.router, removed_at__isnull=True).exists())

    def test_a_big_move_is_split_into_link_commands_the_router_can_take(self):
        self.snapshot()
        devs = [{'ip': f'10.5.50.{i}', 'mac': f'AA:BB:CC:00:00:{i:02X}', 'code': f'K{i}'} for i in range(10, 70)]
        r = self.post(f'/routers/{self.router.pk}/internet/hub/move/', {'interface': 'ether1', 'devices': devs})
        self.assertTrue(r.json()['ok'])
        cmds = self.cmds()
        self.assertGreater(len(cmds), 1)
        for c in cmds:
            self.assertLessEqual(len(agent.command_body(c)), hub.LINK_BUDGET + 600)

    def test_nobody_is_moved_to_a_line_that_is_out_of_use(self):
        self.snapshot(**{'/ip/route': [dict(r, disabled='true') if r['id'] == '*R4' else r for r in ROUTES]})
        with mock.patch('core.wan_doctor.use_state', side_effect=lambda svc, i: {'state': 'out' if i == 'ether5' else 'managed', 'has_routes': True, 'routes_off': 1}):
            r = self.post(f'/routers/{self.router.pk}/internet/hub/move/', {'interface': 'ether5', 'devices': [{'ip': '10.5.50.11', 'code': 'KS1'}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn('out of use', r.json()['message'])

    def test_the_traffic_page_board_and_move(self):
        self.snapshot()
        r = self.client.get('/flow/lines/boards/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['routers'][0]['router'], 'TapTap K')
        r = self.post('/flow/lines/move/', {'router': self.router.pk, 'interface': 'ether5', 'devices': [{'ip': '10.5.50.11', 'code': 'KS1'}]})
        self.assertTrue(r.json()['ok'], r.content)

    def test_moving_over_the_api_places_the_rule_above_taptaps_line_rules(self):
        added, removed = [], []

        class Res:
            def __init__(self, path):
                self.path = path

            def get(self, **kw):
                if self.path == '/ip/firewall/mangle':
                    return [dict(r) for r in MANGLE_FAILOVER]
                return [{'id': '*c1', 'src-address': '10.5.50.11:5000'}, {'id': '*c2', 'src-address': '10.5.50.111:443'}]

            def add(self, **kw):
                added.append(kw)

            def remove(self, id):
                removed.append(id)

            def call(self, *a, **kw):
                return None

        svc = mock.Mock(resource=Res)
        lane = ls.lanes(self.router, MANGLE_FAILOVER)['lanes'][1]
        pin = {'key': 'ip-10-5-50-11', 'ip': '10.5.50.11', 'fields': ls.pin_fields(lane, 'ip-10-5-50-11', '', '10.5.50.11', 'KS1')}
        done, moved = hub.api_pins(svc, [pin])
        self.assertEqual(added[0]['place_before'], '*4')                   # above "route via", below "arrived on"
        self.assertEqual(added[0]['new_connection_mark'], 'TT-ether5')
        self.assertEqual(removed, ['*c1'])                                 # never 10.5.50.111


# ═══════════════════════ gateways ═══════════════════════
class Gateways(Base):
    def act(self, **data):
        return self.post(f'/routers/{self.router.pk}/internet/hub/action/', data)

    def test_state_lists_every_line_and_the_orphan(self):
        self.snapshot()
        r = self.client.get(f'/routers/{self.router.pk}/internet/hub/')
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        names = [(l['interface'], l['missing']) for l in d['lines']]
        self.assertEqual(names, [('ether1', False), ('ether5', False), ('*2', True)])
        self.assertTrue(all(l['nat']['on'] for l in d['lines'] if not l['missing']))
        self.assertEqual([p['name'] for p in d['ports']], ['ether3'])
        self.assertEqual(d['dhcp'][0]['network'], '10.5.50.0/24')

    def test_deleting_the_orphan_removes_only_its_dhcp_client(self):
        self.snapshot()
        self.setup.config['links'].append({'interface': '*2', 'type': 'dhcp', 'enabled': False})
        self.setup.save()
        r = self.act(action='delete', interface='*2')
        self.assertTrue(r.json()['ok'], r.content)
        body = agent.command_body(self.cmds()[0])
        self.assertEqual(body, '/ip dhcp-client remove [find where interface="*2"]')
        self.setup.refresh_from_db()
        self.assertNotIn('*2', [l['interface'] for l in self.setup.config['links']])
        self.assertIn('config', r.json())

    def test_a_line_in_use_cannot_be_deleted_or_lose_its_nat(self):
        self.snapshot()
        r = self.act(action='delete', interface='ether5')
        self.assertEqual(r.status_code, 400)
        self.assertIn('out of use', r.json()['message'])
        r = self.act(action='nat', interface='ether1', on=False)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.cmds(), [])

    def test_adding_a_dhcp_line_on_a_free_port(self):
        self.snapshot()
        r = self.act(action='add', port='ether3', type='dhcp', label='Africell 4G', speed=30)
        self.assertTrue(r.json()['ok'], r.content)
        body = agent.command_body(self.cmds()[0])
        self.assertIn('/ip dhcp-client add interface="ether3" add-default-route="no"', body)   # TapTap runs the lines: it carries nobody yet
        self.assertIn('/ip firewall nat add chain="srcnat" out-interface="ether3" action="masquerade"', body)
        self.setup.refresh_from_db()
        new = next(l for l in self.setup.config['links'] if l['interface'] == 'ether3')
        self.assertEqual((new['label'], new['speed'], new['enabled']), ('Africell 4G', 30, False))
        r = self.act(action='add', port='bridge-hotspot', type='dhcp', label='x')
        self.assertEqual(r.status_code, 400)

    def test_firewall_presets_and_rule_switches(self):
        self.snapshot()
        r = self.act(action='firewall', preset='dns', on=True)
        self.assertTrue(r.json()['ok'], r.content)
        body = agent.command_body(self.cmds()[0])
        self.assertIn('/interface list member add list="TT-HUB-WAN" interface="ether1"', body)
        self.assertIn('dst-port="53" action="drop"', body)
        self.assertNotIn('interface="*2"', body)                            # a missing port is never added
        r = self.act(action='rule', id='*F3', on=False)                     # TapTap's own rule: locked
        self.assertEqual(r.status_code, 400)
        r = self.act(action='rule', id='*F2', on=False)
        self.assertTrue(r.json()['ok'])
        self.assertEqual(agent.command_body(self.cmds()[-1]), '/ip firewall filter disable [find where .id=*F2]')

    def test_dhcp_server_changes_are_checked(self):
        self.snapshot()
        r = self.act(action='dhcp', server='dhcp1', lease_time='1h', dns='1.1.1.1, 8.8.8.8')
        self.assertTrue(r.json()['ok'], r.content)
        body = agent.command_body(self.cmds()[0])
        self.assertIn('lease-time="1h"', body)
        self.assertIn('dns-server="1.1.1.1,8.8.8.8"', body)
        self.assertEqual(self.act(action='dhcp', server='dhcp1', lease_time='forever').status_code, 400)
        r = self.act(action='dhcp', server='dhcp1', enabled=False)
        self.assertIn('HotSpot', r.json()['message'])                       # asks once more for a HotSpot server

    def test_choosing_the_line_that_carries_taptap(self):
        self.snapshot()
        r = self.act(action='link', interface='ether5')
        self.assertTrue(r.json()['ok'], r.content)
        body = agent.command_body(self.cmds()[0])
        self.assertIn('dst-address="203.0.113.7/32" gateway="8.8.8.8"', body)     # through the line's own check host
        self.assertIn('target-scope="11"', body)
        self.assertIn('port=ether5;', body)

    def test_a_stale_link_copy_refuses_risky_changes(self):
        self.snapshot()
        snap = RouterConfigSnapshot.objects.get(router=self.router)
        for sec in snap.sections.values():
            sec['at'] = (timezone.now() - timedelta(hours=3)).isoformat()
        snap.save()
        r = self.act(action='delete', interface='*2')
        self.assertEqual(r.status_code, 400)
        self.assertIn('out of date', r.json()['message'])


# ═══════════════════════ what may reach a router ═══════════════════════
class Safety(Base):
    def test_only_line_hubs_own_rows_can_be_removed(self):
        for bad in ({'v': 'remove', 'p': '/ip/firewall/nat', 'w': {'out-interface': 'ether1'}},
                    {'v': 'remove', 'p': '/ip/firewall/filter', 'w': {'comment^': 'TapTap WAN'}},
                    {'v': 'add', 'p': '/system/script', 'f': {'name': 'x'}},
                    {'v': 'add', 'p': '/ip/firewall/filter', 'f': {'chain': 'input', 'jump-target': 'x'}},
                    {'v': 'set', 'p': '/ip/dhcp-client', 'f': {'use-peer-dns': 'yes'}},
                    {'v': 'disable', 'p': '/ip/firewall/filter', 'id': '1; /system reset'}):
            with self.assertRaises(hub.HubError):
                hub.check_op(bad)

    def test_a_tampered_link_command_never_runs(self):
        self.snapshot()
        self.post(f'/routers/{self.router.pk}/internet/hub/action/', {'action': 'firewall', 'preset': 'legacy', 'on': True})
        cmd = self.cmds()[0]
        cmd.params = {'kind': 'ops', 'ops': [{'v': 'remove', 'p': '/ip/firewall/nat', 'w': {'chain': 'srcnat'}}]}
        with self.assertRaises(hub.HubError):
            agent.command_body(cmd)
        cmd.params = {'kind': 'pins', 'pins': [{'key': 'ip-10-5-50-1', 'ip': '10.5.50.1', 'fields': {'chain': 'prerouting', 'action': 'drop',
                                                                                                     'comment': 'TapTap line-pin ip-10-5-50-1 x'}}]}
        with self.assertRaises(hub.HubError):
            agent.command_body(cmd)

    def test_values_are_quoted_for_routeros(self):
        op = hub.check_op({'v': 'add', 'p': '/ip/firewall/nat', 'f': {'chain': 'srcnat', 'out-interface': 'ether1', 'action': 'masquerade',
                                                                     'comment': 'TapTap Line Hub: x"; /system reset; "'}})
        text = hub.cli(op)
        self.assertIn('\\"; /system reset; \\"', text)


@override_settings(CACHES=CACHE, AUTH_EMAIL_OTP=False, SITE_URL=URL)
class OverTheApi(Base):
    link = False

    def test_a_risky_change_installs_an_undo_timer_and_removes_it_after_reaching_the_router_again(self):
        self.snapshot()
        log = []

        class Res:
            def __init__(self, path):
                self.path = path
                self.rows = {'/system/scheduler': [{'id': '*S', 'name': hub.UNDO_SCHED}] if log.count('sched-added') else []}

            def get(self, **kw):
                if self.path == '/ip/firewall/filter':
                    return [dict(r) for r in FILTER]
                return list(self.rows.get(self.path, []))

            def add(self, **kw):
                log.append('sched-added' if self.path == '/system/scheduler' else f'add {self.path}')

            def set(self, **kw):
                log.append(f'set {self.path} {kw}')

            def remove(self, id):
                log.append(f'remove {self.path} {id}')

        class Svc:
            def __init__(self, router):
                pass

            def connect(self):
                return self

            def close(self):
                pass

            def resource(self, path):
                return Res(path)

            def safe_get(self, path, **kw):
                return []

        with mock.patch('core.mikrotik.MikroTikService', Svc):
            msg, via = hub.run(self.router, [{'v': 'disable', 'p': '/ip/firewall/filter', 'id': '*F2'}], 'test', None,
                               undo=[{'v': 'enable', 'p': '/ip/firewall/filter', 'id': '*F2'}])
        self.assertFalse(via)
        self.assertEqual(log[0], 'sched-added')
        self.assertIn("set /ip/firewall/filter {'id': '*F2', 'disabled': 'yes'}", log)
        self.assertEqual(log[-1], 'remove /system/scheduler *S')              # TapTap reached the router again: timer gone


# ═══════════════════════ bring a line online / share the load with it ═══════════════════════
class ReviveAndShare(Base):
    def act(self, **data):
        return self.post(f'/routers/{self.router.pk}/internet/hub/action/', data)

    def line(self, **over):
        base = {'interface': 'ether5', 'label': 'QCell', 'type': 'dhcp', 'missing': False, 'port_off': False, 'client_off': False,
                'up': True, 'use': 'managed', 'has_routes': True}
        return {**base, **over}

    def test_the_plan_follows_what_is_wrong(self):
        self.assertEqual(hub.revive_plan(self.line(client_off=True)), ['turn_on'])
        self.assertEqual(hub.revive_plan(self.line(up=False)), ['refresh'])
        self.assertEqual(hub.revive_plan(self.line(up=False, type='pppoe')), ['redial'])
        self.assertEqual(hub.revive_plan(self.line(port_off=True, up=False)), ['port_on', 'refresh'])
        self.assertEqual(hub.revive_plan(self.line(use='out')), ['use'])
        self.assertEqual(hub.revive_plan(self.line(), 'broken'), ['rewire'])
        with self.assertRaises(hub.HubError):
            hub.revive_plan(self.line(missing=True))
        with self.assertRaises(hub.HubError):                       # a static line without link: a cable / modem problem
            hub.revive_plan(self.line(type='static', up=False))

    def test_bringing_a_switched_off_line_online_through_link(self):
        clients = [dict(c) for c in DHCP_CLIENTS]
        clients[1]['disabled'] = 'true'
        self.snapshot(**{'/ip/dhcp-client': clients})
        r = self.act(action='revive', interface='ether5')
        self.assertTrue(r.json()['ok'], r.content)
        self.assertEqual([s['key'] for s in r.json()['steps']], ['turn_on'])
        cmd = AgentCommand.objects.filter(router=self.router, kind='wan_doctor').last()
        self.assertEqual(cmd.params['step'], 'turn_on')
        self.assertIn('/ip dhcp-client set [find where interface="ether5"] disabled=no', agent.command_body(cmd))

    def test_a_port_switched_off_is_switched_on(self):
        ifaces = [dict(i, disabled='true', running='false') if i['name'] == 'ether5' else i for i in IFACES]
        self.snapshot(**{'/interface': ifaces})
        r = self.act(action='revive', interface='ether5')
        self.assertTrue(r.json()['ok'], r.content)
        bodies = [agent.command_body(c) for c in AgentCommand.objects.filter(router=self.router, kind=hub.LINK_KIND)]
        self.assertIn('/interface enable [find where name="ether5"]', bodies[0])

    def test_sharing_the_load_with_a_standby_line_through_link(self):
        self.snapshot()
        r = self.act(action='share', interface='ether5')
        self.assertTrue(r.json()['ok'], r.content)
        self.assertTrue(r.json()['via_link'])
        self.setup.refresh_from_db()
        self.assertEqual(self.setup.config['strategy'], 'balance')
        self.assertEqual({l['interface']: l['enabled'] for l in self.setup.config['links']}, {'ether1': True, 'ether5': True})
        self.assertEqual(self.setup.status, 'pending')                                     # confirms by itself (core/wan_link.py)
        steps = [c.params['step'] for c in AgentCommand.objects.filter(router=self.router, kind='wan_lines').order_by('pk')]
        self.assertEqual(steps[0], 'arm')                                                   # the safety timer first
        self.assertIn('per-connection-classifier', ' '.join(agent.command_body(c) for c in AgentCommand.objects.filter(router=self.router, kind='wan_lines')))
        r = self.act(action='share', interface='ether5')                                    # one change at a time
        self.assertEqual(r.status_code, 400)

    def test_a_line_without_an_address_cannot_be_shared_yet(self):
        clients = [dict(c) for c in DHCP_CLIENTS]
        clients[1].update(status='searching...', address='', gateway='')
        self.snapshot(**{'/ip/dhcp-client': clients})
        r = self.act(action='share', interface='ether5')
        self.assertEqual(r.status_code, 400)
        self.assertIn('Bring it online first', r.json()['message'])

    def test_the_load_balancing_card_gets_the_action_urls(self):
        from core.views import _lb_config
        req = mock.Mock(tt_perms={'network.manage'})
        cfg = _lb_config(self.router, request=req)
        self.assertEqual(cfg['action_url'], f'/routers/{self.router.pk}/internet/hub/action/')
        self.assertFalse(_lb_config(self.router, request=mock.Mock(tt_perms=set()))['action_url'])
