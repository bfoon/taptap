"""“Share the load” really shares — and the Live load-balancing card says so (Nov 2026, TapTap K: ether5 stayed on
Standby after “Share the load”).

Three things kept the second line on standby:
* the card credited each of TapTap's per-line tables (which hold the other lines as fallbacks) to the first line,
* hand-made steering on the router (marks / routing rules into a table such as "starlink2") marked every connection
  before TapTap's sharing rules, which only share unmarked connections,
* the card read the marking rules from TapTap's stored copy of the router, which was not refreshed after the change.
"""
from unittest import mock

from django.test import TestCase

from core import wan
from core.routeros_analysis import analyze_wan

FACTS = {'v7': True, 'lans': [], 'links': [
    {'interface': 'ether1', 'type': 'dhcp', 'gateway': '192.168.1.1'},
    {'interface': 'ether5', 'type': 'dhcp', 'gateway': '192.168.2.1'}]}
CONFIG = {'strategy': 'balance', 'health': True, 'sticky': 'customer', 'links': [
    {'interface': 'ether1', 'type': 'dhcp', 'gateway': '192.168.1.1', 'label': 'Starlink', 'speed': 50, 'check': '1.1.1.1'},
    {'interface': 'ether5', 'type': 'dhcp', 'gateway': '192.168.2.1', 'label': 'QCell', 'speed': 50, 'check': '8.8.8.8'}]}
INTERFACES = [{'name': n, 'running': 'true'} for n in ('ether1', 'ether2', 'ether5', 'bridge')]
ADDRESSES = [{'address': '192.168.1.20/24', 'interface': 'ether1'}, {'address': '192.168.2.30/24', 'interface': 'ether5'},
             {'address': '10.5.50.1/24', 'interface': 'bridge'}]
# What the router had before TapTap: its own PCC into a table of its own.
HAND = [{'id': '*H1', 'chain': 'prerouting', 'action': 'mark-connection', 'new-connection-mark': 'sl2_conn', 'passthrough': 'yes',
         'per-connection-classifier': 'both-addresses:2/0', 'in-interface': 'bridge', 'comment': 'my starlink'},
        {'id': '*H2', 'chain': 'prerouting', 'action': 'mark-routing', 'new-routing-mark': 'starlink2', 'connection-mark': 'sl2_conn',
         'in-interface': 'bridge', 'passthrough': 'no', 'comment': 'my starlink'},
        {'id': '*H3', 'chain': 'prerouting', 'action': 'mark-routing', 'new-routing-mark': 'vpn', 'dst-address': '10.77.0.0/16',
         'comment': 'office VPN'},
        {'id': '*H4', 'chain': 'forward', 'action': 'mark-connection', 'new-connection-mark': 'games', 'comment': 'queues'}]
HAND_RULES = [{'id': '*U1', 'src-address': '10.5.50.0/24', 'action': 'lookup', 'table': 'starlink2', 'comment': ''},
              {'id': '*U2', 'dst-address': '10.77.0.0/16', 'action': 'lookup', 'table': 'vpn'}]


def router_rows(plan, own_table_up=True):
    """The rows TapTap's plan leaves on a router, with RouterOS' "active" flags filled in."""
    routes, mangle = [], []
    for k, op in enumerate(plan['ops']):
        if op['op'] != 'add':
            continue
        f = dict(op['fields'])
        if op['path'] == '/ip/route':
            table = f.get('routing-table', 'main')
            line = 'ether5' if ('8.8.8.8' in f['gateway'] or 'ether5' in f['gateway']) else 'ether1'
            dist = int(f.get('distance') or 1)
            own = table in ('main', f'TT-{line}')
            active = f['dst-address'].endswith('/32') or (dist == 1 and own)
            if line == 'ether5' and not own_table_up and f['dst-address'] == '0.0.0.0/0':
                active = False
            if not own_table_up and line == 'ether1' and table == 'TT-ether5':
                active = True                               # the fallback carries ether5's share
            routes.append({'id': f'*R{k}', **f, 'active': 'true' if active else 'false'})
        elif op['path'] == '/ip/firewall/mangle':
            mangle.append({'id': f'*M{k}', **f})
    return routes, mangle


def analyse(routes, mangle, rules=()):
    return analyze_wan(routes=routes, mangle=mangle, interfaces=INTERFACES, addresses=ADDRESSES, routing_rules=list(rules),
                       dhcp_clients=[{'interface': 'ether1', 'gateway': '192.168.1.1'}, {'interface': 'ether5', 'gateway': '192.168.2.1'}])


class TheCardReadsTapTapsSharing(TestCase):
    def setUp(self):
        cfg, errors, _ = wan.normalize(CONFIG, FACTS)
        self.assertEqual(errors, [])
        self.plan = wan.build_plan(cfg, FACTS)

    def test_both_lines_carry_their_share(self):
        routes, mangle = router_rows(self.plan)
        lb = analyse(routes, mangle)
        self.assertEqual(lb['method'], 'PCC')
        links = {l['interface']: l for l in lb['wan_links']}
        self.assertEqual((links['ether1']['expected_share'], links['ether5']['expected_share']), (50, 50))   # before: 100 / 0
        self.assertEqual((links['ether1']['state'], links['ether5']['state']), ('active', 'active'))       # before: ether5 standby
        self.assertEqual(links['ether5']['why'], '')

    def test_a_line_whose_health_check_does_not_answer_says_why(self):
        routes, mangle = router_rows(self.plan, own_table_up=False)
        ether5 = next(l for l in analyse(routes, mangle)['wan_links'] if l['interface'] == 'ether5')
        self.assertEqual(ether5['state'], 'standby')
        self.assertIn('health check 8.8.8.8 does not answer through ether5', ether5['why'])

    def test_hand_made_steering_is_named(self):
        routes, mangle = router_rows(self.plan)
        lb = analyse(routes, HAND + mangle, HAND_RULES)
        self.assertTrue(any('hand-made rule(s) steer customers' in w and 'starlink2' in w for w in lb['warnings']))


class ShareTheLoadSwitchesHandMadeSteeringOff(TestCase):
    def test_only_rules_that_steer_internet_traffic_are_found(self):
        found = wan.steering(HAND + [{'id': '*T', 'chain': 'prerouting', 'action': 'mark-routing', 'new-routing-mark': 'TT-ether1',
                                      'comment': 'TapTap WAN #a · route via Starlink'}], HAND_RULES)
        self.assertEqual(sorted(r['id'] for _m, r in found), ['*H1', '*H2', '*U1'])   # not the VPN, the queues, or TapTap's own

    def test_the_plan_switches_them_off_and_every_undo_puts_them_back(self):
        cfg, _e, _w = wan.normalize(CONFIG, FACTS)
        plan = wan.build_plan(cfg, FACTS)
        self.assertIn('disable_steering', [o['op'] for o in plan['ops']])
        script = wan.render_script(plan, cfg, FACTS)
        self.assertIn(f'comment=($c . "{wan.LINK_OFF}")', script)
        undo = wan.render_undo({}, True)
        self.assertIn(f'/ip firewall mangle find where comment~"{wan.LINK_OFF.strip()}"', undo)
        self.assertIn(f'/routing rule find where comment~"{wan.LINK_OFF.strip()}"', undo)
        single, _e, _w = wan.normalize({**CONFIG, 'strategy': 'single'}, FACTS)
        self.assertNotIn('disable_steering', [o['op'] for o in wan.build_plan(single, FACTS)['ops']])   # one line: left alone

    def test_over_the_api(self):
        rows = {'/ip/firewall/mangle': [dict(r) for r in HAND], '/routing/rule': [dict(r) for r in HAND_RULES]}

        class Res:
            def __init__(self, path):
                self.path = path

            def get(self, **kw):
                return rows.get(self.path, [])

            def set(self, id, **kw):
                row = next(r for r in rows[self.path] if r['id'] == id)
                row.update({k.replace('_', '-'): v for k, v in kw.items()})

        svc = mock.Mock(resource=Res)
        wan.apply_plan(svc, {'v7': True, 'run': 'abc', 'ops': [{'op': 'disable_steering', 'group': 'clients'}]}, FACTS, {}, undo_minutes=0)
        off = {r['id']: r for rs in rows.values() for r in rs if r.get('disabled') == 'yes'}
        self.assertEqual(sorted(off), ['*H1', '*H2', '*U1'])
        self.assertTrue(off['*H2']['comment'].endswith(wan.LINK_OFF))
        self.assertEqual(wan.restore_steering(svc, True), 3)
        self.assertEqual({r['comment'] for r in rows['/ip/firewall/mangle'] if r['id'] in ('*H1', '*H2')}, {'my starlink'})
        self.assertFalse(any(r.get('disabled') == 'yes' for rs in rows.values() for r in rs))
        self.assertEqual(rows['/routing/rule'][0]['comment'], '')           # a rule without a comment gets none back

    def test_the_preview_says_so(self):
        from core.views_wan import summary
        cfg, _e, _w = wan.normalize(CONFIG, FACTS)
        lines, _f = summary(cfg, {**FACTS, 'steering': [{'menu': 'mangle', 'mark': 'starlink2'}]})
        self.assertTrue(any('starlink2' in x and 'switched off' in x for x in lines))
