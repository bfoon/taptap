"""Connection lab, internet-line switch, idle / slow vouchers and the DHCP lease tool.

Covers what a router is asked, what comes back, and what the pages make of it — for both channels
(Direct API and TapTap Link), plus the checks that stop a tampered command from reaching a router.
"""
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core import agent, conn_lab as lab, dhcp_lease as dl, line_steer as ls, nettools as nt, traffic_idle as ti
from core.models import AgentCommand, Business, Router, RouterAgent, RouterConfigSnapshot, UsageRecord, Voucher, VoucherPlan
from core.models_flow import LinePin
from core.models_nettools import NetTest

CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'flow'}}
URL = 'https://taptap.example'

# Two lines shared 50/50 by PCC, as core/wan.py writes them.
MANGLE = [
    {'id': '*1', 'chain': 'prerouting', 'dst-address-list': 'TT-local', 'action': 'accept', 'comment': 'TapTap WAN #abc · leave local traffic alone'},
    {'id': '*2', 'chain': 'prerouting', 'in-interface': 'ether1', 'connection-state': 'new', 'connection-mark': 'no-mark',
     'action': 'mark-connection', 'new-connection-mark': 'TT-ether1', 'passthrough': 'yes', 'comment': 'TapTap WAN #abc · arrived on Gamtel'},
    {'id': '*3', 'chain': 'prerouting', 'in-interface-list': '!TT-WAN', 'connection-mark': 'no-mark', 'dst-address-type': '!local',
     'per-connection-classifier': 'src-address:2/0', 'action': 'mark-connection', 'new-connection-mark': 'TT-ether1',
     'passthrough': 'yes', 'comment': 'TapTap WAN #abc · share 1 of 2 → Gamtel'},
    {'id': '*4', 'chain': 'prerouting', 'in-interface-list': '!TT-WAN', 'connection-mark': 'no-mark', 'dst-address-type': '!local',
     'per-connection-classifier': 'src-address:2/1', 'action': 'mark-connection', 'new-connection-mark': 'TT-ether2',
     'passthrough': 'yes', 'comment': 'TapTap WAN #abc · share 2 of 2 → Africell'},
    {'id': '*5', 'chain': 'prerouting', 'connection-mark': 'TT-ether1', 'in-interface-list': '!TT-WAN', 'action': 'mark-routing',
     'new-routing-mark': 'TT-ether1', 'passthrough': 'no', 'comment': 'TapTap WAN #abc · route via Gamtel'},
]
DHCP_ROWS = [{'name': 'dhcp-hotspot', 'interface': 'bridge-hotspot', 'lease-time': '10m', 'address-pool': 'hs-pool', 'disabled': 'false'},
             {'name': 'dhcp-office', 'interface': 'ether5', 'lease-time': '1d', 'address-pool': 'office', 'disabled': 'false'}]
POOLS = [{'name': 'hs-pool', 'ranges': '10.5.50.10-10.5.50.254'}, {'name': 'office', 'ranges': '192.168.9.10-192.168.9.60'}]
HOTSPOTS = [{'interface': 'bridge-hotspot', 'disabled': 'false'}]


@override_settings(CACHES=CACHE, AUTH_EMAIL_OTP=False, SITE_URL=URL)
class Base(TestCase):
    link = True

    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('owner@x.gm', 'owner@x.gm', 'pw-123456')
        self.biz = Business.objects.create(user=self.owner, business_name='Kerr Sering', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        if self.link:
            self.router = Router.objects.create(business=self.biz, name='KerrSering', connection_mode='agent', status='Online')
            self.agent = RouterAgent.objects.create(router=self.router, token_hash='u' * 64, last_seen_at=timezone.now(),
                                                    enrolled_at=timezone.now(), script_version=9, ros_version='7.15.3 (stable)')
        else:
            self.router = Router.objects.create(business=self.biz, name='KerrSering', ip_address='10.0.0.1', username='a', password='p')
        VoucherPlan.objects.create(business=self.biz, name='Day', price=25, duration_minutes=1440, max_devices=1, speed_limit='2M/4M')

    # ── fixtures ──
    def snapshot(self, **sections):
        base = {'Firewall mangle': {'path': '/ip/firewall/mangle', 'rows': MANGLE},
                'DHCP servers': {'path': '/ip/dhcp-server', 'rows': DHCP_ROWS},
                'IP pools': {'path': '/ip/pool', 'rows': POOLS},
                'HotSpot servers': {'path': '/ip/hotspot', 'rows': HOTSPOTS}}
        base.update(sections)
        return RouterConfigSnapshot.objects.update_or_create(router=self.router, defaults={'sections': base})[0]

    def voucher(self, code='KS1234', **kw):
        now = timezone.now()
        data = dict(business=self.biz, router=self.router, code=code, plan_name='Day', duration_minutes=1440, max_devices=1,
                    status='active', used_at=now - timedelta(hours=4), expires_at=now + timedelta(hours=20))
        data.update(kw)
        return Voucher.objects.create(**data)

    def sessions(self, *rows, at=None, router=None):
        """Put a live-sync session snapshot in the cache: rows are dicts of session fields."""
        users = {}
        for r in rows:
            users.setdefault(r['user'].upper(), []).append({'ip': r.get('ip', '10.5.50.12'), 'mac': r.get('mac', 'AA:BB:CC:DD:EE:01'),
                                                            'down_bps': r.get('down', 0), 'up_bps': r.get('up', 0),
                                                            'session_down': r.get('sd', 1000), 'session_up': r.get('su', 100),
                                                            'uptime': r.get('uptime', '1h'), 'sid': r.get('sid', '*1'),
                                                            'quiet': r.get('quiet'), 'calm': r.get('calm')})
        cache.set(f'tt:tr:users:{(router or self.router).pk}', {'at': (at or timezone.now()).isoformat(), 'users': users}, 600)

    def marks(self, ip='10.5.50.12', counts=None, router=None):
        ls.note_marks(router or self.router, {ip: counts or {'TT-ether1': 8}})

    def login(self):
        self.client.force_login(self.owner)


# ═══════════════════════ which line a customer is on ═══════════════════════
class ReadingTheLines(Base):
    def test_lanes_come_from_the_routers_own_sharing_rules(self):
        self.snapshot()
        info = ls.lanes(self.router)
        self.assertEqual([l['mark'] for l in info['lanes']], ['TT-ether1', 'TT-ether2'])
        self.assertEqual([l['label'] for l in info['lanes']], ['Gamtel', 'Africell'])   # read from the rule comments
        self.assertEqual([l['share'] for l in info['lanes']], [50, 50])
        self.assertTrue(info['sticky'])                                                 # src-address: one line per customer
        self.assertEqual(info['why'], '')

    def test_line_names_follow_the_owners_internet_lines_design(self):
        from core.models import WanSetup
        WanSetup.objects.create(router=self.router, config={'strategy': 'balance', 'links': [
            {'interface': 'ether1', 'label': 'Gamtel fibre'}, {'interface': 'ether2', 'label': 'Africell 4G'}]})
        self.snapshot()
        self.assertEqual([l['label'] for l in ls.lanes(self.router)['lanes']], ['Gamtel fibre', 'Africell 4G'])

    def test_a_single_line_router_says_why_there_is_nothing_to_switch(self):
        self.snapshot(**{'Firewall mangle': {'path': '/ip/firewall/mangle', 'rows': MANGLE[:2]}})
        info = ls.lanes(self.router)
        self.assertEqual(info['lanes'], [])
        self.assertIn('does not share customers over several lines', info['why'])

    def test_a_router_never_synced_says_to_sync_first(self):
        self.assertIn('has not read', ls.lanes(self.router)['why'])

    def test_a_pin_rule_is_not_counted_as_a_sharing_rule(self):
        pinned = dict(MANGLE[3], id='*9', comment='TapTap line-pin AA:BB:CC:DD:EE:01 KS1234 to Africell', **{'src-mac-address': 'AA:BB:CC:DD:EE:01'})
        self.snapshot(**{'Firewall mangle': {'path': '/ip/firewall/mangle', 'rows': MANGLE + [pinned]}})
        info = ls.lanes(self.router)
        self.assertEqual([l['parts'] for l in info['lanes']], [1, 1])      # still 50/50, not 50/66

    def test_the_traffic_sample_says_where_a_customer_goes_without_asking_the_router(self):
        self.snapshot()
        self.marks(counts={'TT-ether1': 9, 'TT-ether2': 1})
        w = ls.where(self.router, '10.5.50.12')
        self.assertEqual((w['state'], w['lane']), ('one', 'TT-ether1'))
        self.marks(counts={'TT-ether1': 5, 'TT-ether2': 4})
        self.assertEqual(ls.where(self.router, '10.5.50.12')['state'], 'mixed')
        self.assertEqual(ls.where(self.router, '10.9.9.9'), {})            # not seen: nothing claimed

    def test_switch_target_is_the_other_line_of_a_pair(self):
        self.snapshot()
        info = ls.lanes(self.router)
        self.assertEqual(ls.next_lane(info, 'TT-ether1')['mark'], 'TT-ether2')
        self.assertEqual(ls.next_lane(info, 'TT-ether2')['mark'], 'TT-ether1')
        self.assertEqual(ls.next_lane(info, '')['mark'], 'TT-ether1')      # nothing known yet → the first line

    def test_spread_shows_the_real_share_next_to_the_designed_one(self):
        self.snapshot()
        ls.note_marks(self.router, {'10.5.50.12': {'TT-ether1': 6}, '10.5.50.13': {'TT-ether1': 4}, '10.5.50.14': {'TT-ether2': 5}})
        sp = ls.spread(self.router, [{'ip': '10.5.50.12', 'down_bps': 100, 'code': 'A'}, {'ip': '10.5.50.13', 'down_bps': 50, 'code': 'B'},
                                     {'ip': '10.5.50.14', 'down_bps': 10, 'code': 'C'}, {'ip': '10.5.50.99', 'down_bps': 1, 'code': 'D'}])
        self.assertEqual([(l['label'], l['customers'], l['real'], l['share']) for l in sp['lines']],
                         [('Gamtel', 2, 67, 50), ('Africell', 1, 33, 50)])
        self.assertEqual(sp['unknown'], 1)


class SwitchingALine(Base):
    def test_the_switch_rule_copies_the_lines_own_rule_for_one_device_only(self):
        self.snapshot()
        lane = ls.lane_by_mark(ls.lanes(self.router), 'TT-ether2')
        f = ls.pin_fields(lane, 'AA:BB:CC:DD:EE:01', 'AA:BB:CC:DD:EE:01', '10.5.50.12', 'KS1234')
        self.assertEqual(f['new-connection-mark'], 'TT-ether2')            # the line's own mark
        self.assertEqual(f['src-mac-address'], 'AA:BB:CC:DD:EE:01')        # for this phone
        self.assertNotIn('per-connection-classifier', f)                   # never the sharing classifier itself
        self.assertTrue(f['comment'].startswith('TapTap line-pin AA:BB:CC:DD:EE:01 '))

    def test_a_device_without_a_known_mac_is_pinned_by_its_address(self):
        self.snapshot()
        lane = ls.lane_by_mark(ls.lanes(self.router), 'TT-ether2')
        key = ls.pin_key('', '10.5.50.12')
        self.assertEqual(key, 'ip-10-5-50-12')
        self.assertEqual(ls.pin_fields(lane, key, '', '10.5.50.12', '')['src-address'], '10.5.50.12')

    def test_the_router_script_adds_the_rule_above_the_sharing_rules_and_clears_old_connections(self):
        self.snapshot()
        p = lab.clean('lanepin', {'ip': '10.5.50.12', 'mac': 'AA:BB:CC:DD:EE:01', 'code': 'KS1234', 'mark': 'TT-ether2'}, self.router)
        body = ls.link_body('lanepin', p)
        self.assertIn('place-before=$an', body)                            # above the 50/50 rules
        self.assertIn('/ip firewall mangle remove [find where comment~"^TapTap line-pin', body)   # never two rules for one phone
        self.assertIn('/ip firewall connection remove', body)              # it moves now, not connection by connection
        self.assertIn('err=nopcc', body)                                   # a router without sharing rules says so
        self.assertNotIn('/ip route', body)                                # routes, tables and NAT are never touched
        self.assertNotIn('/routing', body)

    def test_the_router_answer_is_read_back_and_turned_into_plain_words(self):
        self.snapshot()
        p = lab.clean('lanepin', {'ip': '10.5.50.12', 'mac': 'AA:BB:CC:DD:EE:01', 'code': 'KS1234', 'mark': 'TT-ether2'}, self.router)
        body = (b'ok=1\nmv=12\nlt=4\nlm=TT-ether1|0\nlm=TT-ether2|4\n'
                b'pin=TapTap line-pin AA:BB:CC:DD:EE:01 KS1234 to Africell\n')
        res = lab.parse_link(self.router, 'lanepin', p, nt._lines(body))
        self.assertEqual(res['title'], 'Now on Africell')
        self.assertEqual(res['pin'], 'TT-ether2')
        self.assertIn('12 open connections were moved', res['note'])
        self.assertEqual([(l['label'], l['count'], l['pinned']) for l in res['lanes']], [('Gamtel', 0, False), ('Africell', 4, True)])

    def test_a_router_that_refuses_the_rule_is_reported_not_recorded(self):
        self.snapshot()
        p = lab.clean('lanepin', {'ip': '10.5.50.12', 'mac': 'AA:BB:CC:DD:EE:01', 'code': 'KS1234', 'mark': 'TT-ether2'}, self.router)
        ls.record_pin(self.router, p['key'], p['mac'], p['ip'], p['code'], ls.lane_by_mark(ls.lanes(self.router), 'TT-ether2'), 'Gamtel', self.owner)
        res = lab.parse_link(self.router, 'lanepin', p, nt._lines(b'err=add\nlt=0\n'))
        self.assertTrue(res['failed_step'])
        self.assertIn('Run a sync', res['note'])
        self.assertFalse(LinePin.objects.filter(router=self.router, removed_at__isnull=True).exists())

    def test_a_pin_removed_on_the_router_itself_is_dropped_from_taptaps_record(self):
        self.snapshot()
        lane = ls.lane_by_mark(ls.lanes(self.router), 'TT-ether2')
        ls.record_pin(self.router, 'AA:BB:CC:DD:EE:01', 'AA:BB:CC:DD:EE:01', '10.5.50.12', 'KS1234', lane, 'Gamtel', self.owner)
        p = lab.clean('lane', {'ip': '10.5.50.12', 'mac': 'AA:BB:CC:DD:EE:01'}, self.router)
        lab.parse_link(self.router, 'lane', p, nt._lines(b'lt=3\nlm=TT-ether1|3\n'))     # no pin= line: gone on the router
        self.assertFalse(LinePin.objects.filter(router=self.router, removed_at__isnull=True).exists())

    def test_release_puts_the_customer_back_in_the_sharing(self):
        self.snapshot()
        p = lab.clean('laneoff', {'ip': '10.5.50.12', 'mac': 'AA:BB:CC:DD:EE:01'}, self.router)
        body = ls.link_body('laneoff', p)
        self.assertIn('/ip firewall mangle remove [find where comment~"^TapTap line-pin AA:BB:CC:DD:EE:01 "]', body)
        res = lab.parse_link(self.router, 'laneoff', p, nt._lines(b'ok=1\nrm=1\nmv=5\nlt=2\nlm=TT-ether1|2\n'))
        self.assertEqual(res['title'], 'Back to automatic sharing')
        self.assertIn('chooses its line again', res['note'])

    def test_switching_over_the_api_never_touches_the_sharing_rules(self):
        self.snapshot()
        p = lab.clean('lanepin', {'ip': '10.5.50.12', 'mac': 'AA:BB:CC:DD:EE:01', 'code': 'KS1234', 'mark': 'TT-ether2'}, self.router)
        added, removed = [], []

        class Res:
            def __init__(self, path):
                self.path = path

            def get(self, **kw):
                if self.path == '/ip/firewall/mangle':
                    return [dict(r) for r in MANGLE]
                return [{'id': '*c1', 'src-address': '10.5.50.12:5000'}, {'id': '*c2', 'src-address': '10.9.9.9:443'}]

            def add(self, **kw):
                added.append(kw)

            def remove(self, id):
                removed.append(id)

            def call(self, *a, **kw):
                return None

        svc = SimpleNamespace(resource=Res, safe_get=lambda *a, **k: [])
        out = ls.api_pin(svc, p)
        self.assertTrue(out['done'])
        self.assertEqual(added[0]['place_before'], '*3')                   # just above the first sharing rule
        self.assertEqual(added[0]['new_connection_mark'], 'TT-ether2')
        self.assertEqual(removed, ['*c1'])                                 # only this device's connections
        self.assertEqual(len(added), 1)


class WhatMayReachARouter(Base):
    """core/agent.queue checks a lab command again, so a tampered one never runs."""

    def good(self):
        self.snapshot()
        p = lab.clean('lanepin', {'ip': '10.5.50.12', 'mac': 'AA:BB:CC:DD:EE:01', 'code': 'KS1234', 'mark': 'TT-ether2'}, self.router)
        return {'test_id': 1, 'kind': 'lanepin', **p}

    def test_a_proper_switch_is_accepted(self):
        cmd = agent.queue(self.router, 'nettest', self.good(), label='Line switch')
        self.assertEqual(cmd.kind, 'nettest')

    def test_an_extra_field_in_the_rule_is_refused(self):
        p = self.good()
        p['fields']['action'] = 'drop'
        p['fields']['jump-target'] = 'anything'
        with self.assertRaises(ValueError):
            agent.queue(self.router, 'nettest', p, label='x')

    def test_a_rule_without_taptaps_own_comment_is_refused(self):
        p = self.good()
        p['fields']['comment'] = 'not TapTap'
        with self.assertRaises(ValueError):
            agent.queue(self.router, 'nettest', p, label='x')

    def test_a_key_that_does_not_belong_to_the_device_is_refused(self):
        p = self.good()
        p['key'] = 'BB:BB:BB:BB:BB:BB'
        with self.assertRaises(ValueError):
            agent.queue(self.router, 'nettest', p, label='x')

    def test_a_public_address_is_refused(self):
        with self.assertRaises(ValueError):
            lab.device_params('8.8.8.8')

    def test_a_lab_kind_cannot_be_started_from_the_ordinary_network_tools_form(self):
        with self.assertRaises(ValueError):
            nt.clean_params('lanepin', {'ip': '10.5.50.12'})

    def test_a_lease_time_must_look_like_a_lease_time(self):
        with self.assertRaises(ValueError):
            agent.queue(self.router, 'nettest', {'test_id': 1, 'kind': 'dhcpset', 'lease': '1h; /system reboot', 'servers': [], 'minutes': 60}, label='x')

    def test_a_dhcp_server_name_with_a_quote_is_refused(self):
        with self.assertRaises(ValueError):
            dl.clean({'minutes': 60, 'servers': ['dhcp" ; /system reboot']})


# ═══════════════════════ the connection lab ═══════════════════════
class DeviceAndSpeedChecks(Base):
    def test_the_device_script_asks_for_everything_about_one_phone(self):
        p = lab.clean('client', {'ip': '10.5.50.12', 'mac': 'AA:BB:CC:DD:EE:01', 'code': 'KS1234'}, self.router)
        body = lab.link_body('client', p, agent.rs)
        for needle in ('/ip arp find where address=10.5.50.12', '/ip dhcp-server lease find where mac-address=AA:BB:CC:DD:EE:01',
                       '/interface bridge host find', '/ip hotspot host find', 'flood-ping address=10.5.50.12',
                       'wireless registration-table', 'wifi registration-table', 'caps-man registration-table'):
            self.assertIn(needle, body)

    def test_the_device_answer_becomes_signal_lease_and_session(self):
        p = lab.clean('client', {'ip': '10.5.50.12', 'mac': 'AA:BB:CC:DD:EE:01'}, self.router)
        body = (b'arp=AA:BB:CC:DD:EE:01|bridge\nr=1|12ms\nr=1|14ms\nr=0|\nr=1|400ms\nr=1|15ms\nr=1|13ms\n'
                b'port=wlan1\nlease=bound|00:42:00|dhcp-hotspot|true|galaxy-a12\nhost=true|false|00:00:30\n'
                b'act=KS1234|4h20m|00:00:30|19h40m\nwifi=-78|65Mbps|58Mbps|71|wlan1\n')
        res = lab.parse_link(self.router, 'client', p, nt._lines(body))
        self.assertEqual(res['ping']['sent'], 6)
        self.assertEqual(res['ping']['received'], 5)
        self.assertEqual(res['ping']['loss'], 17)
        self.assertEqual(res['wifi']['signal'], -78)
        self.assertEqual(res['wifi']['ccq'], 71)
        self.assertEqual(res['lease']['status'], 'bound')
        self.assertEqual(res['lease']['host'], 'galaxy-a12')
        self.assertEqual(res['active']['user'], 'KS1234')
        self.assertTrue(res['host']['authorized'])

    def test_the_speed_script_reads_limits_drops_the_line_the_internet_and_the_log(self):
        self.snapshot()
        p = lab.clean('cspeed', {'ip': '10.5.50.12', 'mac': 'AA:BB:CC:DD:EE:01', 'code': 'KS1234'}, self.router)
        body = lab.link_body('cspeed', p, agent.rs)
        self.assertIn('/queue simple find', body)
        self.assertIn('dropped', body)
        self.assertIn('connection-mark', body)              # which line it uses, in the same visit
        self.assertIn('cpu-load', body)
        self.assertIn('message~"logged out"', body)

    def test_a_voucher_code_that_could_carry_a_regex_is_left_out_of_the_log_query(self):
        p = lab.clean('cspeed', {'ip': '10.5.50.12', 'code': 'KS.*1234'}, self.router)
        self.assertNotIn('logged out', lab.link_body('cspeed', p, agent.rs))

    def test_the_speed_answer_names_the_limit_that_holds_it_back(self):
        self.snapshot()
        p = lab.clean('cspeed', {'ip': '10.5.50.12', 'code': 'KS1234'}, self.router)
        body = (b'q=TTFUP-KS1234-10.5.50.12|10.5.50.12/32|512k/1M|300k/900k|120/4500|900/90000\n'
                b'q=<hotspot-KS1234>|10.5.50.12/32|2M/4M|300k/900k|0/0|900/90000\n'
                b'lt=6\nlm=TT-ether1|6\ni=3|40ms\ncpu=18\n')
        res = lab.parse_link(self.router, 'cspeed', p, nt._lines(body))
        q = lab.binding(res['queues'])
        self.assertEqual(q['kind'], 'fup')                                  # the lowest limit wins
        self.assertEqual(q['max_down'], 1_000_000)
        self.assertEqual(q['drop_down'], 4500)
        self.assertEqual(q['drop_pct'], 4.8)
        self.assertEqual(res['cpu'], 18)
        self.assertEqual(res['internet']['avg'], 40.0)
        self.assertEqual(res['lane']['lane'], 'TT-ether1')

    def test_plan_speeds_are_read_in_every_shape(self):
        self.assertEqual(lab.parse_rate('2M/4M'), (2_000_000, 4_000_000))
        self.assertEqual(lab.parse_rate('512k/1M 1M/2M 1M/2M 8 8 1M/2M'), (512_000, 1_000_000))
        self.assertEqual(lab.parse_rate('5M'), (5_000_000, 5_000_000))
        self.assertEqual(lab.parse_rate(''), (0, 0))

    def test_the_slow_scan_only_reports_limits_that_drop_packets(self):
        body = (b'qn=240\nqp=0/0\nq=TTFUP-A-10.5.50.2|10.5.50.2/32|512k/1M|0/900k|0/4500|0/9000\n'
                b'q=TTSPD-9-10.5.50.3|10.5.50.3/32|1M/2M|0/1M|0/50|0/9000\n')
        res = lab.parse_link(self.router, 'slowscan', {}, nt._lines(body))
        self.assertEqual(res['total'], 240)
        self.assertEqual([q['name'] for q in res['queues']], ['TTFUP-A-10.5.50.2', 'TTSPD-9-10.5.50.3'])
        self.assertIn('2 speed limits are dropping packets', res['title'])

    def test_a_router_that_cannot_report_drops_says_so(self):
        res = lab.parse_link(self.router, 'slowscan', {}, nt._lines(b'qn=12\nqp=x\n'))
        self.assertFalse(res['readable'])
        self.assertIn('did not report limiter drops', res['title'])


class TheVerdict(Base):
    def ping(self, received=6, avg=12.0, loss=0, jitter=2.0, quality='excellent'):
        return {'sent': 6, 'received': received, 'loss': loss, 'avg': avg, 'jitter': jitter, 'min': avg, 'max': avg,
                'times': [avg] * received + [None] * (6 - received), 'quality': quality, 'quality_note': 'note'}

    def test_a_weak_wifi_signal_is_named_as_the_slow_part(self):
        v = lab.verdict({'online': True, 'down_bps': 300_000},
                        {'ping': self.ping(), 'wifi': {'signal': -84, 'tx': '12Mbps', 'rx': '6Mbps', 'ccq': 40, 'interface': 'wlan1'}, 'port': 'wlan1'},
                        {'queues': [], 'internet': {'sent': 3, 'received': 3, 'avg': 30, 'loss': 0}})
        self.assertEqual(v['head']['level'], 'bad')
        self.assertIn('Very weak Wi-Fi signal', v['head']['title'])
        self.assertEqual(v['stations']['link']['state'], 'bad')
        self.assertEqual(v['stations']['internet']['state'], 'ok')

    def test_a_phone_that_does_not_answer_is_separated_from_one_that_left(self):
        gone = lab.verdict({}, {'ping': self.ping(received=0, avg=None, loss=100, quality='down'), 'arp': None}, {})
        self.assertIn('not on the network right now', gone['head']['title'])
        here = lab.verdict({}, {'ping': self.ping(received=0, avg=None, loss=100, quality='down'),
                                'arp': {'mac': 'AA:BB:CC:DD:EE:01', 'interface': 'bridge'}, 'active': {'uptime': '1h', 'idle': '5m'}}, {})
        self.assertIn('does not answer the router', here['head']['title'])
        self.assertIn('its time keeps running', here['findings'][0]['text'])

    def test_using_all_of_the_plan_speed_is_not_blamed_on_the_network(self):
        v = lab.verdict({'online': True, 'down_bps': 3_900_000},
                        {'ping': self.ping()},
                        {'queues': [lab._queue('<hotspot-KS1234>', '10.5.50.12/32', '2M/4M', '300k/3900k', '0/9000', '0/90000')],
                         'internet': {'sent': 3, 'received': 3, 'avg': 25, 'loss': 0}})
        f = next(x for x in v['findings'] if x['key'] == 'atlimit')
        self.assertIn('a faster plan is the fix, not the router', f['text'])
        self.assertIn('9,000 download packets', f['text'])

    def test_fair_usage_is_named_with_a_way_to_lift_it(self):
        v = lab.verdict({'online': True, 'down_bps': 900_000}, {'ping': self.ping()},
                        {'queues': [lab._queue('TTFUP-KS1234-10.5.50.12', '10.5.50.12/32', '512k/1M', '0/900k', '0/0', '0/9000')],
                         'internet': {'sent': 3, 'received': 3, 'avg': 25, 'loss': 0}})
        f = next(x for x in v['findings'] if x['key'] == 'fup')
        self.assertIn('Slowed to 1.0 Mb/s by fair usage', f['title'])
        self.assertEqual(f['action'], 'lift')

    def test_a_bad_internet_line_offers_the_switch(self):
        v = lab.verdict({'online': True}, {'ping': self.ping()},
                        {'queues': [], 'internet': {'sent': 3, 'received': 2, 'avg': 480, 'loss': 33}})
        f = next(x for x in v['findings'] if x['key'] == 'internet')
        self.assertEqual(f['action'], 'switch')
        self.assertIn('everyone on this line feels it', f['text'])

    def test_a_connected_phone_that_never_logged_in_is_spotted(self):
        v = lab.verdict({}, {'ping': self.ping(), 'host': {'authorized': False, 'bypassed': False, 'idle': '1m'}}, {})
        self.assertIn('not logged in', v['head']['title'])
        self.assertEqual(next(x for x in v['findings'] if x['key'] == 'login')['action'], 'fix')

    def test_a_healthy_path_says_the_network_is_not_the_problem(self):
        v = lab.verdict({'online': True, 'down_bps': 500_000}, {'ping': self.ping(), 'port': 'ether3'},
                        {'queues': [lab._queue('<hotspot>', '10.5.50.12/32', '2M/4M', '0/500k', '0/0', '0/9000')],
                         'internet': {'sent': 3, 'received': 3, 'avg': 22, 'loss': 0}})
        self.assertEqual(v['head']['level'], 'ok')
        self.assertTrue(v['done'])
        self.assertIn('it is the site or app they use', v['findings'][-1]['text'])

    def test_short_lease_sign_outs_point_at_the_dhcp_tool(self):
        drops = nt.drops_result([('nov/16 20:10:11', 'KS1234 (10.5.50.12): logged out: lost dhcp lease')] * 3)
        v = lab.verdict({'online': True}, {'ping': self.ping()}, {'queues': [], 'drops': drops,
                                                                  'internet': {'sent': 3, 'received': 3, 'avg': 20, 'loss': 0}})
        self.assertEqual(next(x for x in v['findings'] if x['key'] == 'signouts')['action'], 'dhcp')


# ═══════════════════════ idle and slow vouchers ═══════════════════════
class QuietSessions(Base):
    def line(self, prev, cur, span=30):
        """One live-sync pass: previous and current counters of one session, `span` seconds apart."""
        q = ti.Quiet(self.router, 1000 + span)
        p = (prev[0], prev[1], 1000, 'KS1234', 'AA:BB:CC:DD:EE:01')
        out = q.note('*1', p, 'KS1234', cur[0], cur[1], cur[0] - prev[0], cur[1] - prev[1], '1h')
        q.save()
        return out

    def test_a_session_moving_nothing_is_marked_silent_and_calm(self):
        silent, calm = self.line((1000, 2000), (1000, 2000))
        self.assertEqual((silent, calm), (1000, 1000))

    def test_background_chatter_is_calm_but_not_silent(self):
        silent, calm = self.line((1000, 2000), (1000, 2000 + 15_000))      # 4 kb/s over 30 s
        self.assertIsNone(silent)
        self.assertEqual(calm, 1000)

    def test_real_use_clears_both(self):
        silent, calm = self.line((1000, 2000), (1000, 2000 + 5_000_000))
        self.assertEqual((silent, calm), (None, None))

    def test_quiet_time_keeps_counting_from_when_it_started(self):
        q = ti.Quiet(self.router, 1030)
        q.note('*1', (0, 0, 1000, 'KS1234', 'M'), 'KS1234', 0, 0, 0, 0, '1h')
        q.save()
        q2 = ti.Quiet(self.router, 1060)
        silent, _ = q2.note('*1', (0, 0, 1030, 'KS1234', 'M'), 'KS1234', 0, 0, 0, 0, '1h')
        self.assertEqual(silent, 1000)                                     # still the first quiet moment

    def test_a_session_seen_for_the_first_time_is_judged_by_its_whole_uptime(self):
        q = ti.Quiet(self.router, 10_000)
        silent, calm = q.note('*1', None, 'KS1234', 100, 200, 100, 200, '2h')
        self.assertEqual(silent, 10_000 - 7200)
        self.assertEqual(calm, 10_000 - 7200)


class IdleTab(Base):
    def test_online_but_quiet_vouchers_are_listed_worst_first(self):
        now = timezone.now()
        self.voucher('KS1234')
        self.voucher('KS5678')
        self.sessions({'user': 'KS1234', 'ip': '10.5.50.12', 'calm': now.timestamp() - 3600, 'uptime': '4h'},
                      {'user': 'KS5678', 'ip': '10.5.50.13', 'calm': now.timestamp() - 900, 'uptime': '1h'},
                      {'user': 'KS9999', 'ip': '10.5.50.14'})
        out = ti.idle(self.biz, minutes=10, now=now)
        self.assertEqual([r['code'] for r in out['online']], ['KS1234', 'KS5678'])
        self.assertEqual(out['online'][0]['idle'], '60 min')
        self.assertEqual(out['sessions'], 3)

    def test_the_quiet_threshold_and_level_are_respected(self):
        now = timezone.now()
        self.voucher('KS1234')
        self.sessions({'user': 'KS1234', 'calm': now.timestamp() - 900, 'quiet': None})
        self.assertEqual(len(ti.idle(self.biz, minutes=10, now=now)['online']), 1)
        self.assertEqual(len(ti.idle(self.biz, minutes=30, now=now)['online']), 0)
        self.assertEqual(len(ti.idle(self.biz, minutes=10, level='silent', now=now)['online']), 0)

    def test_a_router_whose_live_sync_went_quiet_is_left_out_and_named(self):
        now = timezone.now()
        self.voucher('KS1234')
        self.sessions({'user': 'KS1234', 'calm': now.timestamp() - 3600}, at=now - timedelta(minutes=30))
        out = ti.idle(self.biz, now=now)
        self.assertEqual(out['stale'], ['KerrSering'])
        self.assertEqual(out['online'], [])

    def test_started_vouchers_with_nobody_signed_in_are_the_second_group(self):
        now = timezone.now()
        paid = self.voucher('KS1111')
        self.voucher('KS2222', used_at=None, expires_at=None)              # never started: not waste
        online = self.voucher('KS3333')
        self.sessions({'user': 'KS3333'})
        UsageRecord.objects.create(business=self.biz, router=self.router, username='KS1111', mac_address='AA:BB:CC:DD:EE:09',
                                   hour=(now - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0), download=5_000_000, upload=1_000_000)
        out = ti.idle(self.biz, now=now)
        rows = out['offline']['rows']
        self.assertEqual([r['code'] for r in rows], ['KS1111'])
        self.assertEqual(rows[0]['used_pct'], 17)
        self.assertFalse(rows[0]['never'])
        self.assertIn('h', rows[0]['away'])
        self.assertNotIn(online.code, [r['code'] for r in rows])

    def test_a_frozen_voucher_is_not_called_waste(self):
        now = timezone.now()
        self.voucher('KS1111', frozen_at=now, freeze_reason='unpaid')
        self.assertEqual(ti.idle(self.biz, now=now)['offline']['rows'], [])


class SlowTab(Base):
    def test_a_voucher_using_all_its_plan_speed_is_listed_with_the_reason(self):
        self.voucher('KS1234')
        self.sessions({'user': 'KS1234', 'ip': '10.5.50.12', 'down': 3_800_000, 'up': 200_000})
        out = ti.slow(self.biz)
        row = out['rows'][0]
        self.assertEqual(row['code'], 'KS1234')
        self.assertEqual(row['limit_bps'], 4_000_000)                      # the plan's 2M/4M
        self.assertEqual(row['source'], 'plan')
        self.assertEqual(row['pct'], 95)
        self.assertIn('Using all of its plan speed', row['reasons'][0]['text'])

    def test_a_comfortable_voucher_is_not_listed(self):
        self.voucher('KS1234')
        self.sessions({'user': 'KS1234', 'down': 200_000})
        self.assertEqual(ti.slow(self.biz)['rows'], [])

    def test_a_router_scan_adds_the_packets_the_limiter_dropped(self):
        self.voucher('KS1234')
        self.sessions({'user': 'KS1234', 'ip': '10.5.50.12', 'down': 900_000})
        ti.remember_scan(self.router, lab.parse_link(self.router, 'slowscan', {}, nt._lines(
            b'qn=20\nqp=0/0\nq=TTFUP-KS1234-10.5.50.12|10.5.50.12/32|512k/1M|0/900k|0/4500|0/90000\n')))
        row = ti.slow(self.biz)['rows'][0]
        self.assertEqual(row['drops'], 4500)
        self.assertEqual(row['limit_bps'], 1_000_000)
        self.assertTrue(any('dropped 4,500 packets' in x['text'] for x in row['reasons']))
        self.assertEqual(ti.slow(self.biz)['counts']['drops'], 1)


# ═══════════════════════ DHCP lease ═══════════════════════
class DhcpLease(Base):
    def test_a_short_lease_on_a_hotspot_is_called_out_with_a_better_value(self):
        rows = dl.shape(DHCP_ROWS, POOLS, HOTSPOTS, leases=[{'server': 'dhcp-hotspot', 'status': 'bound'}] * 30)
        hs = rows[0]
        self.assertEqual(hs['human'], '10 min')
        self.assertEqual(hs['pool'], 245)
        self.assertEqual(hs['full'], 12)
        self.assertEqual(hs['advice']['level'], 'bad')
        self.assertEqual(hs['advice']['suggest'], 60)
        self.assertIn('signed out', hs['advice']['text'])

    def test_a_full_pool_asks_for_a_shorter_lease_instead(self):
        rows = dl.shape([dict(DHCP_ROWS[0], **{'lease-time': '12h'})], POOLS, HOTSPOTS,
                        leases=[{'server': 'dhcp-hotspot', 'status': 'bound'}] * 230)
        self.assertEqual(rows[0]['full'], 94)
        self.assertEqual(rows[0]['advice']['suggest'], 30)
        self.assertIn('before new customers are refused', rows[0]['advice']['text'])

    def test_a_full_pool_with_a_short_lease_asks_for_more_addresses(self):
        rows = dl.shape([dict(DHCP_ROWS[0], **{'lease-time': '10m'})], POOLS, HOTSPOTS,
                        leases=[{'server': 'dhcp-hotspot', 'status': 'bound'}] * 240)
        self.assertIn('needs a bigger address pool', rows[0]['advice']['text'])

    def test_an_office_network_is_left_alone(self):
        rows = dl.shape(DHCP_ROWS, POOLS, HOTSPOTS, leases=[])
        self.assertEqual(rows[1]['advice']['level'], 'ok')
        self.assertFalse(rows[1]['hotspot'])

    def test_the_lease_time_is_written_to_the_servers_that_were_chosen(self):
        p = dl.clean({'minutes': 60, 'servers': ['dhcp-hotspot']})
        self.assertEqual(p['lease'], '1h')
        body = dl.link_body('dhcpset', p, agent.rs)
        self.assertIn('/ip dhcp-server set [find name="dhcp-hotspot"] lease-time=1h', body)
        self.assertIn('miss=', body)                                       # a name the router does not have is reported
        self.assertIn('/ip dhcp-server find', body)                        # and it reads every server back

    def test_every_network_at_once(self):
        body = dl.link_body('dhcpset', dl.clean({'minutes': 720, 'servers': []}), agent.rs)
        self.assertIn(':foreach s in=[/ip dhcp-server find] do={ :do { /ip dhcp-server set $s lease-time=12h', body)

    def test_the_read_back_confirms_the_new_value(self):
        p = dl.clean({'minutes': 60, 'servers': ['dhcp-hotspot']})
        body = (b'ch=dhcp-hotspot\npr=dhcp-hotspot|10.5.50.10-10.5.50.254\n'
                b's=dhcp-hotspot|bridge-hotspot|1h|false|30|1\ns=dhcp-office|ether5|1d|false|4|0\n')
        res = lab.parse_link(self.router, 'dhcpset', p, nt._lines(body))
        self.assertEqual(res['title'], 'Lease time is now 1 hour on dhcp-hotspot')
        self.assertIn('nobody is disconnected', res['note'])
        self.assertEqual(res['servers'][0]['minutes'], 60)
        self.assertTrue(res['servers'][0]['hotspot'])
        self.assertEqual(dl.stored(self.router)['servers'][0]['minutes'], 60)    # remembered for the page

    def test_a_router_that_did_not_change_it_is_not_reported_as_done(self):
        p = dl.clean({'minutes': 60, 'servers': ['dhcp-hotspot', 'dhcp-guest']})
        res = lab.parse_link(self.router, 'dhcpset', p, nt._lines(
            b'ch=dhcp-hotspot\nmiss=dhcp-guest\ns=dhcp-hotspot|bridge-hotspot|10m|false|30|1\n'))
        self.assertTrue(res['failed_step'])
        self.assertIn('not found: dhcp-guest', res['note'])
        self.assertIn('still the old value: dhcp-hotspot', res['note'])

    def test_the_page_opens_from_the_last_sync_without_asking_the_router(self):
        self.snapshot()
        st = dl.stored(self.router)
        self.assertEqual([s['name'] for s in st['servers']], ['dhcp-hotspot', 'dhcp-office'])
        self.assertFalse(st['live'])
        self.assertEqual(st['servers'][0]['advice']['level'], 'bad')

    def test_lease_times_are_shown_in_words(self):
        self.assertEqual((dl.human(10), dl.human(60), dl.human(90), dl.human(1440), dl.human(4320)),
                         ('10 min', '1 hour', '1 hour 30 min', '1 day', '3 days'))


# ═══════════════════════ the pages ═══════════════════════
class Pages(Base):
    def test_the_lab_says_who_is_online_with_their_limits_and_line(self):
        self.snapshot()
        v = self.voucher('KS1234')
        self.sessions({'user': 'KS1234', 'ip': '10.5.50.12', 'down': 1_500_000, 'up': 120_000, 'calm': timezone.now().timestamp() - 700})
        self.marks(counts={'TT-ether2': 7})
        self.login()
        d = json.loads(self.client.get(f'/flow/lab/?voucher={v.pk}').content)
        self.assertTrue(d['ok'])
        t = d['targets'][0]
        self.assertEqual(t['ip'], '10.5.50.12')
        self.assertEqual(t['limits']['limit_down'], 4_000_000)
        self.assertEqual(t['limits']['limit_source'], 'plan')
        self.assertEqual(t['lines']['where']['label'], 'Africell')
        self.assertTrue(t['lines']['multi'])
        self.assertTrue(t['lines']['can_switch'])
        self.assertGreater(t['quiet_s'], 600)
        self.assertEqual(d['voucher']['code'], 'KS1234')

    def test_a_voucher_that_is_not_online_is_told_so_kindly(self):
        v = self.voucher('KS1234')
        self.login()
        d = json.loads(self.client.get(f'/flow/lab/?voucher={v.pk}').content)
        self.assertEqual(d['targets'], [])
        self.assertIn('not online right now', d['offline'])
        self.assertIn("Can't connect?", d['offline'])

    def test_running_the_checks_queues_one_command_each_on_a_link_router(self):
        self.snapshot()
        v = self.voucher('KS1234')
        self.sessions({'user': 'KS1234', 'ip': '10.5.50.12'})
        self.login()
        d = json.loads(self.client.post('/flow/lab/run/', data=json.dumps(
            {'router': self.router.pk, 'ip': '10.5.50.12', 'mac': 'AA:BB:CC:DD:EE:01', 'code': 'KS1234'}),
            content_type='application/json').content)
        self.assertEqual([t['kind'] for t in d['tests']], ['client', 'cspeed'])
        self.assertEqual([t['status'] for t in d['tests']], ['waiting', 'waiting'])
        cmds = AgentCommand.objects.filter(router=self.router, kind='nettest')
        self.assertEqual(cmds.count(), 2)
        self.assertEqual(NetTest.objects.filter(business=self.biz).count(), 2)
        self.assertEqual(v.code, 'KS1234')

    def test_the_verdict_reads_the_finished_tests(self):
        self.snapshot()
        self.voucher('KS1234')
        self.sessions({'user': 'KS1234', 'ip': '10.5.50.12', 'down': 900_000})
        t = NetTest.objects.create(business=self.biz, router=self.router, kind='client', target='KS1234', status='done',
                                   params={'ip': '10.5.50.12'}, finished_at=timezone.now(),
                                   result={'ping': {'sent': 6, 'received': 6, 'loss': 0, 'avg': 9.0, 'jitter': 1.0, 'times': [9] * 6,
                                                    'quality': 'excellent', 'quality_note': ''},
                                           'wifi': {'signal': -85, 'tx': '12Mbps', 'rx': '6Mbps', 'ccq': None, 'interface': 'wlan1'}})
        self.login()
        d = json.loads(self.client.post('/flow/lab/verdict/', data=json.dumps(
            {'router': self.router.pk, 'ip': '10.5.50.12', 'code': 'KS1234', 'client': t.pk}), content_type='application/json').content)
        self.assertIn('Very weak Wi-Fi signal', d['verdict']['head']['title'])
        self.assertEqual(d['verdict']['stations']['device']['state'], 'ok')
        self.assertFalse(d['verdict']['done'])                              # the speed check has not run yet

    def test_switching_a_line_records_it_and_queues_one_command(self):
        self.snapshot()
        self.voucher('KS1234')
        self.sessions({'user': 'KS1234', 'ip': '10.5.50.12'})
        self.marks(counts={'TT-ether1': 6})
        self.login()
        d = json.loads(self.client.post('/flow/line/', data=json.dumps(
            {'router': self.router.pk, 'action': 'switch', 'mark': 'next', 'ip': '10.5.50.12',
             'mac': 'AA:BB:CC:DD:EE:01', 'code': 'KS1234'}), content_type='application/json').content)
        self.assertEqual(d['lane']['label'], 'Africell')
        pin = LinePin.objects.get(router=self.router, removed_at__isnull=True)
        self.assertEqual((pin.mark, pin.from_label, pin.code, pin.created_by), ('TT-ether2', 'Gamtel', 'KS1234', self.owner))
        self.assertFalse(pin.confirmed)                                     # until the router answers
        cmd = AgentCommand.objects.get(router=self.router, kind='nettest')
        self.assertEqual(cmd.params['kind'], 'lanepin')
        self.assertIn('Internet lines', [a.type for a in self.biz.activities.all()])

    def test_back_to_automatic_clears_the_record(self):
        self.snapshot()
        lane = ls.lane_by_mark(ls.lanes(self.router), 'TT-ether2')
        ls.record_pin(self.router, 'AA:BB:CC:DD:EE:01', 'AA:BB:CC:DD:EE:01', '10.5.50.12', 'KS1234', lane, 'Gamtel', self.owner)
        self.login()
        d = json.loads(self.client.post('/flow/line/', data=json.dumps(
            {'router': self.router.pk, 'action': 'auto', 'ip': '10.5.50.12', 'mac': 'AA:BB:CC:DD:EE:01'}),
            content_type='application/json').content)
        self.assertTrue(d['ok'])
        self.assertFalse(LinePin.objects.filter(router=self.router, removed_at__isnull=True).exists())
        self.assertEqual(AgentCommand.objects.get(router=self.router, kind='nettest').params['kind'], 'laneoff')

    def test_a_router_with_one_line_refuses_the_switch_with_a_reason(self):
        self.snapshot(**{'Firewall mangle': {'path': '/ip/firewall/mangle', 'rows': MANGLE[:2]}})
        self.login()
        r = self.client.post('/flow/line/', data=json.dumps({'router': self.router.pk, 'action': 'switch', 'mark': 'next',
                                                             'ip': '10.5.50.12'}), content_type='application/json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('does not share customers', json.loads(r.content)['message'])
        self.assertEqual(AgentCommand.objects.count(), 0)

    def test_the_internet_lines_tab_shows_both_lines_and_the_switches_in_force(self):
        self.snapshot()
        self.sessions({'user': 'KS1234', 'ip': '10.5.50.12', 'down': 900_000})
        self.marks(counts={'TT-ether1': 5})
        lane = ls.lane_by_mark(ls.lanes(self.router), 'TT-ether2')
        ls.record_pin(self.router, 'AA:BB:CC:DD:EE:02', 'AA:BB:CC:DD:EE:02', '10.5.50.13', 'KS5678', lane, 'Gamtel', self.owner)
        self.login()
        d = json.loads(self.client.get('/flow/lines/').content)
        r = d['routers'][0]
        self.assertTrue(r['multi'])
        self.assertEqual([l['label'] for l in r['lines']], ['Gamtel', 'Africell'])
        self.assertEqual(r['lines'][0]['customers'], 1)
        self.assertEqual(r['lines'][0]['people'][0]['code'], 'KS1234')
        self.assertEqual([p['code'] for p in r['pins']], ['KS5678'])

    def test_the_idle_and_slow_tabs_answer_as_json(self):
        now = timezone.now()
        self.voucher('KS1234')
        self.sessions({'user': 'KS1234', 'ip': '10.5.50.12', 'down': 3_900_000, 'calm': now.timestamp() - 1800})
        self.login()
        idle = json.loads(self.client.get('/flow/idle/?min=10').content)
        self.assertEqual(idle['online'][0]['code'], 'KS1234')
        slow = json.loads(self.client.get('/flow/slow/').content)
        self.assertEqual(slow['rows'][0]['pct'], 98)

    def test_the_dhcp_page_lists_the_networks_and_changes_the_lease(self):
        self.snapshot()
        self.login()
        d = json.loads(self.client.get('/flow/dhcp/').content)
        self.assertEqual([s['name'] for s in d['routers'][0]['servers']], ['dhcp-hotspot', 'dhcp-office'])
        self.assertIn({'minutes': 60, 'label': '1 hour'}, d['presets'])
        d = json.loads(self.client.post('/flow/dhcp/', data=json.dumps(
            {'router': self.router.pk, 'action': 'set', 'minutes': 60, 'servers': ['dhcp-hotspot']}),
            content_type='application/json').content)
        self.assertEqual(d['test']['kind'], 'dhcpset')
        cmd = AgentCommand.objects.get(router=self.router, kind='nettest')
        self.assertEqual(cmd.params['lease'], '1h')
        self.assertIn('DHCP', [a.type for a in self.biz.activities.all()])

    def test_an_impossible_lease_time_is_refused_before_anything_is_sent(self):
        self.login()
        r = self.client.post('/flow/dhcp/', data=json.dumps({'router': self.router.pk, 'action': 'set', 'minutes': 1}),
                             content_type='application/json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('between 2 minutes and 30 days', json.loads(r.content)['message'])
        self.assertEqual(AgentCommand.objects.count(), 0)

    def test_another_business_cannot_look_at_this_one(self):
        other = User.objects.create_user('other@x.gm', 'other@x.gm', 'pw-123456')
        Business.objects.create(user=other, business_name='Other', owner_name='X', phone='2',
                                trial_ends_at=timezone.now() + timedelta(days=30))
        v = self.voucher('KS1234')
        self.client.force_login(other)
        self.assertEqual(self.client.get(f'/flow/lab/?voucher={v.pk}').status_code, 404)
        r = self.client.post('/flow/line/', data=json.dumps({'router': self.router.pk, 'action': 'switch', 'mark': 'next',
                                                             'ip': '10.5.50.12'}), content_type='application/json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(AgentCommand.objects.count(), 0)


@override_settings(CACHES=CACHE, AUTH_EMAIL_OTP=False, SITE_URL=URL)
class DirectApiRouter(Base):
    link = False

    def test_the_checks_run_at_once_and_the_answer_is_on_the_page(self):
        self.snapshot()
        self.voucher('KS1234')
        self.sessions({'user': 'KS1234', 'ip': '10.5.50.12', 'down': 900_000})

        class Svc:
            def __init__(self, *a, **kw):
                pass

            def connect(self):
                return self

            def close(self):
                pass

            def safe_get(self, path, **where):
                rows = {'/ip/arp': [{'mac-address': 'aa:bb:cc:dd:ee:01', 'interface': 'bridge'}],
                        '/interface/bridge/host': [{'on-interface': 'wlan1'}],
                        '/ip/dhcp-server/lease': [{'status': 'bound', 'expires-after': '00:09:30', 'server': 'dhcp-hotspot', 'host-name': 'phone'}],
                        '/ip/hotspot/host': [{'authorized': 'true', 'bypassed': 'false', 'idle-time': '00:00:10'}],
                        '/interface/wireless/registration-table': [{'signal-strength': '-83', 'tx-rate': '12Mbps', 'rx-rate': '6Mbps',
                                                                    'tx-ccq': '45', 'interface': 'wlan1'}],
                        '/ip/hotspot/active': [{'user': 'KS1234', 'uptime': '4h', 'idle-time': '10s', 'session-time-left': '19h'}],
                        '/system/resource': [{'cpu-load': '22'}]}
                return rows.get(path, [])

            def resource(self, path):
                svc = self

                class R:
                    def get(self, **kw):
                        if path == '/queue/simple':
                            return [{'id': '*1', 'name': 'TTFUP-KS1234-10.5.50.12', 'target': '10.5.50.12/32', 'max-limit': '512k/1M',
                                     'rate': '0/900k', 'dropped': '0/4500', 'packets': '0/90000', 'disabled': 'false'}]
                        if path == '/ip/firewall/connection':
                            return [{'id': '*c1', 'src-address': '10.5.50.12:443', 'connection-mark': 'TT-ether1'}]
                        if path == '/ip/firewall/mangle':
                            return [dict(r) for r in MANGLE]
                        if path == '/log':
                            return []
                        return []

                    def call(self, verb, args=None):
                        if path == '/' and verb == 'ping':
                            return [{'seq': i, 'time': '11ms', 'host': '10.5.50.12'} for i in range(int((args or {}).get('count', 3)))]
                        return None

                return R()

        with mock.patch('core.mikrotik.MikroTikService', Svc):
            self.login()
            d = json.loads(self.client.post('/flow/lab/run/', data=json.dumps(
                {'router': self.router.pk, 'ip': '10.5.50.12', 'mac': 'AA:BB:CC:DD:EE:01', 'code': 'KS1234'}),
                content_type='application/json').content)
        tests = {t['kind']: t for t in d['tests']}
        self.assertEqual([t['status'] for t in d['tests']], ['done', 'done'])
        self.assertEqual(tests['client']['result']['wifi']['signal'], -83)
        self.assertEqual(tests['client']['result']['lease']['server'], 'dhcp-hotspot')
        self.assertEqual(tests['cspeed']['result']['queues'][0]['drop_down'], 4500)
        self.assertEqual(tests['cspeed']['result']['lane']['lane'], 'TT-ether1')
        self.assertEqual(tests['cspeed']['result']['cpu'], 22)
        self.assertIn('of 1.0 Mb/s', tests['cspeed']['summary'])


# ═══════════════════════ before the database step / when something breaks ═══════════════════════
class BeforeMigrate(Base):
    """The code is installed but `migrate` has not run: the LinePin table is missing (seen on a live server:
    the Slow tab worked, the Connection lab answered 500)."""

    def setUp(self):
        super().setUp()
        from django.db import connection
        with connection.cursor() as c:
            c.execute('DROP TABLE core_linepin')          # rolled back after the test
        ls._ready.update(ok=False, at=0.0)

    def tearDown(self):
        ls._ready.update(ok=False, at=0.0)                # the next test finds the table again
        super().tearDown()

    def test_the_lab_still_opens_and_says_what_to_run(self):
        self.snapshot()
        self.voucher('KS1234')
        self.sessions({'user': 'KS1234', 'ip': '10.5.50.12', 'down': 900_000})
        self.marks(counts={'TT-ether1': 6})
        self.login()
        r = self.client.get('/flow/lab/?router=%s&ip=10.5.50.12&mac=AA:BB:CC:DD:EE:01&code=KS1234' % self.router.pk)
        self.assertEqual(r.status_code, 200)
        d = json.loads(r.content)
        self.assertEqual(d['targets'][0]['lines']['where']['label'], 'Gamtel')   # seeing the line still works
        self.assertFalse(d['targets'][0]['lines']['can_switch'])                  # switching waits for the table
        self.assertIn('manage.py migrate core', d['setup'])

    def test_a_switch_is_refused_before_anything_reaches_the_router(self):
        self.snapshot()
        self.login()
        r = self.client.post('/flow/line/', data=json.dumps({'router': self.router.pk, 'action': 'switch', 'mark': 'next',
                                                             'ip': '10.5.50.12', 'mac': 'AA:BB:CC:DD:EE:01'}),
                             content_type='application/json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('manage.py migrate core', json.loads(r.content)['message'])
        self.assertEqual(AgentCommand.objects.count(), 0)

    def test_the_internet_lines_tab_still_shows_the_lines(self):
        self.snapshot()
        self.sessions({'user': 'KS1234', 'ip': '10.5.50.12'})
        self.marks(counts={'TT-ether1': 5})
        self.login()
        d = json.loads(self.client.get('/flow/lines/').content)
        self.assertEqual(d['routers'][0]['lines'][0]['customers'], 1)
        self.assertEqual(d['routers'][0]['pins'], [])
        self.assertFalse(d['can_switch'])
        self.assertIn('migrate core', d['setup'])

    def test_a_router_answer_is_still_read_without_the_table(self):
        self.snapshot()
        p = lab.clean('lane', {'ip': '10.5.50.12', 'mac': 'AA:BB:CC:DD:EE:01'}, self.router)
        res = lab.parse_link(self.router, 'lane', p, nt._lines(b'lt=3\nlm=TT-ether1|3\n'))
        self.assertEqual(res['title'], 'Going through Gamtel')


class NoBareServerErrors(Base):
    def test_an_unexpected_error_comes_back_as_a_message_the_modal_can_show(self):
        self.voucher('KS1234')
        self.sessions({'user': 'KS1234', 'ip': '10.5.50.12'})
        self.login()
        with mock.patch('core.line_steer.lanes', side_effect=RuntimeError('boom')), self.assertLogs('taptap.flow', 'ERROR'):
            r = self.client.get('/flow/lab/?router=%s&ip=10.5.50.12&code=KS1234' % self.router.pk)
        self.assertEqual(r.status_code, 500)
        d = json.loads(r.content)                                            # JSON, not an HTML error page
        self.assertFalse(d['ok'])
        self.assertIn('RuntimeError', d['message'])
        self.assertIn('server log', d['message'])

    def test_a_missing_table_anywhere_names_the_fix(self):
        from django.db import OperationalError
        self.login()
        with mock.patch('core.traffic_idle.slow', side_effect=OperationalError('no such table: core_something')), \
                self.assertLogs('taptap.flow', 'ERROR'):
            r = self.client.get('/flow/slow/')
        self.assertIn('manage.py migrate core', json.loads(r.content)['message'])

    def test_a_missing_page_is_still_a_404(self):
        self.login()
        self.assertEqual(self.client.get('/flow/test/999999/').status_code, 404)
