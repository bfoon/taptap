"""A line with internet but no customers: missing health-check route, no NAT, not in the WAN list.
Built from the hAP on two Starlinks (22 Oct 2026): ether1 carried everything, ether3 nothing."""
import copy
import re

from django.test import SimpleTestCase

from core import wan_wiring as ww

LINES = [{'interface': 'ether1', 'type': 'dhcp', 'label': 'Starlink 1'},
         {'interface': 'ether3', 'type': 'dhcp', 'label': 'Starlink 2'}]

HAP = {
    '/ip/route': [
        {'id': '*1', 'dst-address': '1.1.1.1/32', 'gateway': '192.168.1.1%ether1', 'scope': '10',
         'comment': 'TapTap check Starlink 1', 'disabled': 'false', 'dynamic': 'false'},
        {'id': '*2', 'dst-address': '0.0.0.0/0', 'gateway': '1.1.1.1', 'immediate-gw': '192.168.1.1%ether1',
         'comment': 'TapTap Starlink 1 pinned', 'disabled': 'false', 'dynamic': 'false', 'active': 'true'},
        {'id': '*3', 'dst-address': '0.0.0.0/0', 'gateway': '8.8.8.8', 'immediate-gw': '',
         'comment': 'TapTap Starlink 2 pinned', 'disabled': 'false', 'dynamic': 'false', 'active': 'false'},
    ],
    '/ip/dhcp-client': [
        {'id': '*A', 'interface': 'ether1', 'status': 'bound', 'gateway': '192.168.1.1', 'disabled': 'false',
         'script': ':if ($bound=1) do={/ip route set [find comment="TapTap check Starlink 1"] gateway=($"gateway-address" . "%ether1")}'},
        {'id': '*B', 'interface': 'ether3', 'status': 'bound', 'gateway': '192.168.0.1', 'disabled': 'false',
         'script': ':if ($bound=1) do={/ip route set [find comment="TapTap check Starlink 2"] gateway=($"gateway-address" . "%ether3")}'},
    ],
    '/ip/firewall/nat': [
        {'id': '*N1', 'chain': 'srcnat', 'action': 'masquerade', 'out-interface-list': 'WAN', 'disabled': 'false'},
        {'id': '*N2', 'chain': 'srcnat', 'action': 'masquerade', 'src-address': '192.168.89.0/24', 'comment': 'masq. vpn traffic', 'disabled': 'false'},
        {'id': '*N3', 'chain': 'srcnat', 'action': 'masquerade', 'out-interface': 'ether1', 'comment': 'TapTap Quick WAN', 'disabled': 'false'},
    ],
    '/interface/list': [{'id': '*L1', 'name': 'WAN'}, {'id': '*L2', 'name': 'LAN'}],
    '/interface/list/member': [{'id': '*M1', 'list': 'WAN', 'interface': 'ether1', 'disabled': 'false'},
                               {'id': '*M2', 'list': 'LAN', 'interface': 'bridge1', 'disabled': 'false'}],
    '/interface': [{'name': 'ether1', 'type': 'ether'}, {'name': 'ether3', 'type': 'ether'},
                   {'name': 'bridge1', 'type': 'bridge'}, {'name': 'taptap-wg', 'type': 'wg'}],
    '/ip/address': [{'address': '172.16.4.1/22', 'interface': 'bridge1', 'disabled': 'false'},
                    {'address': '192.168.1.182/24', 'interface': 'ether1', 'disabled': 'false'},
                    {'address': '192.168.0.35/24', 'interface': 'ether3', 'disabled': 'false'}],
    '/ip/hotspot': [{'name': 'hotspot1', 'interface': 'bridge1', 'disabled': 'false'}],
}


class Res:
    def __init__(self, rows):
        self.rows = rows

    def get(self, **kw):
        return [r for r in self.rows if all(r.get(k.replace('_', '-')) == v for k, v in kw.items())]

    def set(self, id, **kw):
        next(r for r in self.rows if r['id'] == id).update({k.replace('_', '-'): v for k, v in kw.items()})

    def add(self, **kw):
        self.rows.append({'id': f'*n{len(self.rows)}', 'disabled': 'false', 'dynamic': 'false',
                          **{k.replace('_', '-'): v for k, v in kw.items()}})

    def remove(self, id):
        self.rows[:] = [r for r in self.rows if r['id'] != id]


class Svc:
    def __init__(self, tables):
        self.tables = copy.deepcopy(tables)

    def resource(self, path):
        return Res(self.tables.setdefault(path, []))

    def safe_get(self, path, **kw):
        return self.resource(path).get(**kw)


def body_only(src):
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', src)


class FindsTheFault(SimpleTestCase):
    def test_ether3_is_not_wired(self):
        w = ww.line_wiring(Svc(HAP), LINES[1], LINES)
        self.assertEqual({p['code'] for p in w['problems']}, {'check_route', 'nat', 'wan_list'})
        self.assertEqual(w['host'], '8.8.8.8')
        self.assertEqual(w['comment'], 'TapTap check Starlink 2')     # what the DHCP script already updates
        self.assertFalse(w['script'])
        self.assertTrue(w['fixable'])

    def test_ether1_is_fine(self):
        w = ww.line_wiring(Svc(HAP), LINES[0], LINES)
        self.assertEqual(w['problems'], [])
        self.assertEqual(w['host'], '1.1.1.1')

    def test_vpn_masquerade_does_not_count_for_customers(self):
        self.assertFalse(ww._nat_covers(Svc(HAP), 'ether3', ['ether1', 'ether3']))

    def test_check_host_through_the_wrong_modem(self):
        t = copy.deepcopy(HAP)
        t['/ip/route'].append({'id': '*9', 'dst-address': '8.8.8.8/32', 'gateway': '192.168.1.1%ether1',
                               'comment': 'TapTap check Starlink 2', 'disabled': 'false', 'dynamic': 'false'})
        w = ww.line_wiring(Svc(t), LINES[1], LINES)
        self.assertIn('goes out of ether1', next(p['text'] for p in w['problems'] if p['code'] == 'check_route'))

    def test_line_taken_out_on_purpose_is_left_alone(self):
        t = copy.deepcopy(HAP)
        t['/ip/route'][2]['comment'] += ' ~owner-off:ether3'
        w = ww.line_wiring(Svc(t), LINES[1], LINES)
        self.assertTrue(w['held'])
        self.assertFalse(any(p['code'] == 'check_route' for p in w['problems']))
        self.assertFalse(w['fixable'])


class RepairsIt(SimpleTestCase):
    def test_api_repair(self):
        svc = Svc(HAP)
        msg = ww.repair(svc, LINES[1], LINES)
        r = [x for x in svc.tables['/ip/route'] if x['dst-address'] == '8.8.8.8/32']
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]['gateway'], r[0]['scope'], r[0]['comment']), ('192.168.0.1%ether3', '10', 'TapTap check Starlink 2'))
        self.assertTrue(any(n.get('out-interface') == 'ether3' and n['action'] == 'masquerade' for n in svc.tables['/ip/firewall/nat']))
        self.assertTrue(any(m['list'] == 'WAN' and m['interface'] == 'ether3' for m in svc.tables['/interface/list/member']))
        self.assertIn('Starlink 2', msg)
        self.assertEqual(ww.line_wiring(svc, LINES[1], LINES)['problems'], [])
        # the working line is untouched
        self.assertEqual(svc.tables['/ip/route'][:2], HAP['/ip/route'][:2])

    def test_wrong_port_route_is_moved_not_duplicated(self):
        t = copy.deepcopy(HAP)
        t['/ip/route'].append({'id': '*9', 'dst-address': '8.8.8.8/32', 'gateway': '192.168.1.1%ether1',
                               'comment': 'TapTap check Starlink 2', 'disabled': 'false', 'dynamic': 'false'})
        svc = Svc(t)
        ww.repair(svc, LINES[1], LINES)
        r = [x for x in svc.tables['/ip/route'] if x['dst-address'] == '8.8.8.8/32']
        self.assertEqual([x['gateway'] for x in r], ['192.168.0.1%ether3'])

    def test_hand_made_conflict_is_switched_off_not_deleted(self):
        t = copy.deepcopy(HAP)
        t['/ip/route'].append({'id': '*8', 'dst-address': '8.8.8.8/32', 'gateway': '192.168.1.1', 'disabled': 'false', 'dynamic': 'false'})
        svc = Svc(t)
        ww.repair(svc, LINES[1], LINES)
        hand = next(x for x in svc.tables['/ip/route'] if x['id'] == '*8')
        self.assertEqual(hand['disabled'], 'yes')
        self.assertTrue(hand['comment'].endswith(ww.OFF_SUFFIX))

    def test_already_fine(self):
        svc = Svc(HAP)
        ww.repair(svc, LINES[1], LINES)
        self.assertIn('nothing to repair', ww.repair(svc, LINES[1], LINES))


class ThroughLink(SimpleTestCase):
    def setUp(self):
        self.p = ww.link_params(LINES[1], ww.line_wiring(Svc(HAP), LINES[1], LINES))
        self.src = ww.link_body(self.p)

    def test_balanced_and_small(self):
        code = body_only(self.src)
        for o, c in ('{}', '[]', '()'):
            self.assertEqual(code.count(o), code.count(c), f'unbalanced {o}{c}')
        self.assertLess(len(self.src), 3000)

    def test_uses_the_live_gateway_and_all_three_fixes(self):
        self.assertIn('/ip dhcp-client get [find where interface="ether3"] gateway', self.src)
        self.assertIn('dst-address="8.8.8.8/32"', self.src)
        self.assertIn('scope=10', self.src)
        self.assertIn('out-interface="ether3"', self.src)
        self.assertIn('/interface list member add list=WAN interface="ether3"', self.src)

    def test_rejects_bad_input(self):
        for bad in ({'interface': 'ether3; /system reset'}, {'host': '8.8.8.8; /x'}, {'comment': ''}):
            with self.assertRaises(ValueError):
                ww.link_body({**self.p, **bad})
