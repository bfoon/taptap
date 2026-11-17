"""Speed per device: each device of a shared voucher or member gets the plan's whole speed, its own lane.

What the router is asked, what it answers, what TapTap makes of it, the fix, and TapTap's own lanes enforced on
every live-sync pass (core/traffic_speed.py) — for Direct API and TapTap Link routers.
"""
import json
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.utils import timezone

from core import agent, conn_lab as lab, device_lanes as dl, line_steer as ls, nettools as nt, traffic_speed as ts
from core.models import AgentCommand, NetTest, Voucher, VoucherPlan
from core.models_flow import DeviceLane
from core.tests.test_flow_lab import Base

PROFILES = 'p=Family|2M/2M||3|first\np=default|||1|first\nsp=hsprof1|20M/20M\n'


def body(*queue_lines, ft=0, fg=0, profiles=PROFILES, extra=''):
    return ('n=40\n' + ''.join(l + '\n' for l in queue_lines) + profiles + f'ft={ft}\nfg={fg}\nhn=10.5.50.0/24\n' + extra).encode()


class Setup(Base):
    def setUp(self):
        super().setUp()
        VoucherPlan.objects.create(business=self.biz, name='Family', price=100, duration_minutes=1440 * 7, max_devices=3, speed_limit='2M/2M')

    def family(self, code='FAM001', **kw):
        return self.voucher(code, plan_name='Family', max_devices=3, **kw)

    def three_online(self, code='FAM001'):
        self.sessions({'user': code, 'ip': '10.5.50.11', 'mac': 'AA:BB:CC:00:00:01', 'down': 1_500_000},
                      {'user': code, 'ip': '10.5.50.12', 'mac': 'AA:BB:CC:00:00:02', 'down': 900_000},
                      {'user': code, 'ip': '10.5.50.13', 'mac': 'AA:BB:CC:00:00:03', 'down': 100_000})

    def read(self, raw_body, code='FAM001', ips=('10.5.50.11', '10.5.50.12', '10.5.50.13')):
        p = {'ips': list(ips), 'codes': [code]}
        return lab.parse_link(self.router, 'lanes', p, nt._lines(raw_body))


# ═══════════════════════ how the speed is issued ═══════════════════════
class HowSpeedIsIssued(Setup):
    def test_a_plan_voucher_gets_its_plans_profile_and_speed(self):
        rate, up, down, profile = dl.speed_of(self.family())
        self.assertEqual((rate, up, down, profile), ('2M/2M', 2_000_000, 2_000_000, 'Family'))

    def test_a_member_gets_its_member_plans_profile_and_speed(self):
        m = self.voucher('MEMBER01', plan_name='Gold members', login_type='member', max_devices=4,
                         router_profile='TapTap-member-Gold', rate_limit='5M/10M')
        self.assertEqual(dl.speed_of(m), ('5M/10M', 5_000_000, 10_000_000, 'TapTap-member-Gold'))

    def test_a_custom_voucher_uses_its_own_speed(self):
        v = self.voucher('CUSTOM1', plan_name='', max_devices=2, rate_limit='1M/3M')
        rate, _up, down, profile = dl.speed_of(v)
        self.assertEqual((rate, down), ('1M/3M', 3_000_000))
        self.assertEqual(profile, 'taptap-2-devices-1M-3M')

    def test_the_math_says_each_device_gets_the_whole_speed(self):
        m = dl.math('2M/2M', 3)
        self.assertEqual(m['each'], '2.0 Mb/s down · 2.0 Mb/s up')
        self.assertEqual(m['total'], '6.0 Mb/s down · 6.0 Mb/s up')
        self.assertIn('Each of its 3 devices gets 2.0 Mb/s down', m['text'])
        self.assertIn('No speed limit', dl.math('', 3)['text'])


# ═══════════════════════ what the router check finds ═══════════════════════
class TheCheck(Setup):
    def test_three_devices_each_with_their_own_lane_is_all_good(self):
        self.family()
        self.three_online()
        res = self.read(body('d=1|<hotspot-FAM001-1>|10.5.50.11/32|2M/2M|', 'd=2|<hotspot-FAM001-2>|10.5.50.12/32|2M/2M|',
                             'd=3|<hotspot-FAM001-3>|10.5.50.13/32|2M/2M|', 'h=30|<hs-hotspot1>|bridge-hotspot|20M/20M|'))
        self.assertEqual(res['level'], 'ok')
        v = res['vouchers'][0]
        self.assertTrue(v['ok'])
        self.assertEqual([d['state'] for d in v['devices']], ['own', 'own', 'own'])
        self.assertIn('Each of its 3 devices online has its own 2.0 Mb/s down', v['verdict'])
        self.assertEqual(res['fix']['lanes'], [])

    def test_a_parent_queue_that_squeezes_the_devices_together_is_found(self):
        self.family()
        self.three_online()
        res = self.read(body('s=0|Hotspot total|10.5.50.0/24|2M/2M|', 'd=1|<hotspot-FAM001-1>|10.5.50.11/32|2M/2M|Hotspot total',
                             'd=2|<hotspot-FAM001-2>|10.5.50.12/32|2M/2M|Hotspot total', 'd=3|<hotspot-FAM001-3>|10.5.50.13/32|2M/2M|Hotspot total',
                             profiles='p=Family|2M/2M|Hotspot total|3|first\n'))
        states = [d['state'] for d in res['vouchers'][0]['devices']]
        self.assertEqual(states, ['shared', 'shared', 'shared'])      # the /24 parent sits above: it catches them first
        res2 = self.read(body('d=1|<hotspot-FAM001-1>|10.5.50.11/32|2M/2M|Hotspot total', 'd=2|<hotspot-FAM001-2>|10.5.50.12/32|2M/2M|Hotspot total',
                              'd=3|<hotspot-FAM001-3>|10.5.50.13/32|2M/2M|Hotspot total', 's=9|Hotspot total||2M/2M|',
                              profiles='p=Family|2M/2M|Hotspot total|3|first\n'))
        self.assertEqual([d['state'] for d in res2['vouchers'][0]['devices']], ['parent'] * 3)
        self.assertEqual(res2['fix']['unparent'], ['Family'])
        self.assertIn('puts every device under one parent queue', res2['findings'][0]['title'])

    def test_a_hand_made_network_queue_above_the_lanes_makes_them_share(self):
        self.family()
        self.three_online()
        res = self.read(body('s=0|Customers|10.5.50.0/24|2M/2M|', 'd=1|<hotspot-FAM001-1>|10.5.50.11/32|2M/2M|',
                             'd=2|<hotspot-FAM001-2>|10.5.50.12/32|2M/2M|', 'd=3|<hotspot-FAM001-3>|10.5.50.13/32|2M/2M|'))
        d = res['vouchers'][0]['devices'][0]
        self.assertEqual(d['state'], 'shared')
        self.assertIn('“Customers” (a whole network)', d['text'])
        self.assertTrue(any(f['key'] == 'broad' for f in res['findings']))
        self.assertEqual(res['fix']['lanes'], [Voucher.objects.get(code='FAM001').pk])   # lanes go above it

    def test_a_queue_below_the_lanes_does_not_count(self):
        self.family()
        self.three_online()
        res = self.read(body('d=1|<hotspot-FAM001-1>|10.5.50.11/32|2M/2M|', 'd=2|<hotspot-FAM001-2>|10.5.50.12/32|2M/2M|',
                             'd=3|<hotspot-FAM001-3>|10.5.50.13/32|2M/2M|', 's=20|Customers|10.5.50.0/24|50M/50M|'))
        self.assertTrue(res['vouchers'][0]['ok'])

    def test_a_profile_without_a_speed_falls_into_the_hotspots_shared_queue(self):
        self.family()
        self.three_online()
        res = self.read(body('h=30|<hs-hotspot1>|bridge-hotspot|20M/20M|', profiles='p=Family|||3|first\nsp=hsprof1|20M/20M\n'))
        self.assertEqual([d['state'] for d in res['vouchers'][0]['devices']], ['shared'] * 3)
        self.assertIn('the hotspot server’s queue', res['vouchers'][0]['devices'][0]['text'])
        self.assertEqual(res['fix']['rates'], [['Family', '2M/2M']])
        self.assertTrue(any(f['key'] == 'rate' for f in res['findings']))

    def test_a_speed_changed_while_online_is_called_old(self):
        self.family()
        self.three_online()
        res = self.read(body('d=1|<hotspot-FAM001-1>|10.5.50.11/32|1M/1M|', 'd=2|<hotspot-FAM001-2>|10.5.50.12/32|2M/2M|',
                             'd=3|<hotspot-FAM001-3>|10.5.50.13/32|2000000/2000000|'))
        self.assertEqual([d['state'] for d in res['vouchers'][0]['devices']], ['old', 'own', 'own'])  # 2000000 = 2M
        self.assertIn('at 1.0 Mb/s instead of the plan’s 2.0 Mb/s', res['vouchers'][0]['devices'][0]['text'])

    def test_no_queue_at_all_means_no_limit(self):
        self.family()
        self.three_online()
        res = self.read(body(profiles='p=Family|2M/2M||3|first\n'))
        self.assertEqual([d['state'] for d in res['vouchers'][0]['devices']], ['none'] * 3)
        self.assertEqual(res['summary']['none'], 3)
        self.assertEqual(res['level'], 'bad')

    def test_fasttrack_without_a_guard_is_the_first_finding(self):
        self.family()
        self.three_online()
        res = self.read(body('d=1|<hotspot-FAM001-1>|10.5.50.11/32|2M/2M|', 'd=2|<hotspot-FAM001-2>|10.5.50.12/32|2M/2M|',
                             'd=3|<hotspot-FAM001-3>|10.5.50.13/32|2M/2M|', ft=1))
        self.assertEqual(res['findings'][0]['key'], 'fasttrack')
        self.assertTrue(res['fix']['guard'])
        self.assertEqual(res['fix']['nets'], ['10.5.50.0/24'])
        self.assertTrue(res['vouchers'][0]['devices'][0]['fasttrack'])
        self.assertIn('FastTrack skips them', res['title'])
        guarded = self.read(body('d=1|<hotspot-FAM001-1>|10.5.50.11/32|2M/2M|', 'd=2|<hotspot-FAM001-2>|10.5.50.12/32|2M/2M|',
                                 'd=3|<hotspot-FAM001-3>|10.5.50.13/32|2M/2M|', ft=1, fg=2))
        self.assertFalse(guarded['fix']['guard'])

    def test_without_a_hotspot_network_the_guard_is_not_offered_and_the_page_says_how(self):
        self.family()
        self.three_online()
        res = self.read(('n=3\nd=1|<hotspot-FAM001-1>|10.5.50.11/32|2M/2M|\n' + PROFILES + 'ft=1\nfg=0\n').encode())
        self.assertFalse(res['fix']['guard'])
        self.assertIn('could not find the hotspot’s network', res['findings'][0]['text'])

    def test_a_taptap_lane_above_counts_as_its_own(self):
        self.family()
        self.three_online()
        res = self.read(body('t=0|TTSPD-1-10-5-50-11|10.5.50.11/32|2000k/2000k|', 'd=1|<hotspot-FAM001-1>|10.5.50.11/32|1M/1M|',
                             'd=2|<hotspot-FAM001-2>|10.5.50.12/32|2M/2M|', 'd=3|<hotspot-FAM001-3>|10.5.50.13/32|2M/2M|'))
        self.assertEqual(res['vouchers'][0]['devices'][0]['state'], 'taptap')
        self.assertTrue(res['vouchers'][0]['ok'])

    def test_a_device_that_came_online_after_the_question_is_not_judged(self):
        self.family()
        self.three_online()
        res = self.read(body('d=1|<hotspot-FAM001-1>|10.5.50.11/32|2M/2M|'), ips=('10.5.50.11',))
        self.assertEqual(len(res['vouchers'][0]['devices']), 1)

    def test_the_result_is_kept_for_the_pages(self):
        self.family()
        self.three_online()
        self.read(body('d=1|<hotspot-FAM001-1>|10.5.50.11/32|2M/2M|'))
        self.assertEqual(dl.last(self.router)['result']['summary']['devices'], 3)


class RouterScripts(Setup):
    def test_the_read_script_walks_the_queues_once_and_reports_only_what_matters(self):
        b = dl.link_body('lanes', {'ips': ['10.5.50.11', '10.5.50.12']}, agent.rs)
        self.assertIn('"|10.5.50.11/32|10.5.50.12/32|"', b)
        self.assertEqual(b.count('/queue simple find]'), 2)              # the count, and one walk
        for needle in ('parent-queue', 'insert-queue-before', '/ip hotspot profile', 'fasttrack-connection', f'comment="{dl.GUARD}"', '/ip address'):
            self.assertIn(needle, b)
        self.assertNotIn(' set ', b)                                     # a check changes nothing
        self.assertNotIn(' add ', b)

    def test_the_fix_script_writes_speeds_lifts_parents_and_guards_fasttrack(self):
        p = {'ips': ['10.5.50.11'], 'rates': [['Family', '2M/2M']], 'unparent': ['Family'], 'guard': True, 'nets': ['10.5.50.0/24']}
        b = dl.link_body('lanefix', p, agent.rs)
        self.assertIn('/ip hotspot user profile set [find name="Family"] rate-limit="2M/2M"', b)
        self.assertIn('/ip hotspot user profile unset [find name="Family"] parent-queue', b)
        self.assertIn('connection-state=established,related src-address-list=TTHS-net', b)   # new connections still meet the drop rules
        self.assertIn('place-before=[:pick $ft 0]', b)
        self.assertIn('/ip firewall address-list add list=TTHS-net address=10.5.50.0/24', b)
        self.assertNotIn('/queue simple remove', b)                       # never deletes a queue the owner made

    def test_scripts_are_balanced_and_fit_a_check_in(self):
        p = {'ips': [f'10.5.50.{i}' for i in range(10, 50)], 'rates': [[f'Plan {i}', '2M/2M'] for i in range(10)],
             'unparent': ['Family'], 'guard': True, 'nets': ['10.5.50.0/24']}
        for kind in ('lanes', 'lanefix'):
            b = lab.link_body(kind, p, agent.rs)
            depth = q = 0
            esc = False
            for ch in b:
                if esc:
                    esc = False
                    continue
                if ch == '\\':
                    esc = True
                elif ch == '"':
                    q ^= 1
                elif not q and ch in '{[':
                    depth += 1
                elif not q and ch in '}]':
                    depth -= 1
            self.assertEqual((depth, q), (0, 0), kind)
            self.assertLess(len(b), 9000, kind)

    def test_a_tampered_fix_is_refused_before_it_reaches_a_router(self):
        good = {'test_id': 1, 'kind': 'lanefix', 'ips': ['10.5.50.11'], 'rates': [['Family', '2M/2M']], 'unparent': [], 'guard': False, 'nets': []}
        agent.queue(self.router, 'nettest', good, label='ok')
        for bad in ({'rates': [['Family', '2M/2M"; /system reboot; "']]}, {'unparent': ['x"]; /system reset-configuration; [find name="']},
                    {'nets': ['0.0.0.0/0; /ip firewall filter remove [find]']}, {'ips': ['8.8.8.8; /system reboot']}):
            with self.assertRaises(ValueError):
                agent.queue(self.router, 'nettest', {**good, **bad}, label='x')

    def test_only_private_addresses_can_be_looked_up(self):
        with self.assertRaises(ValueError):
            dl.clean({'ips': ['8.8.8.8']})

    def test_link_speed_queues_now_sit_above_the_hotspots_own_and_below_fair_usage(self):
        from types import SimpleNamespace
        cmd = SimpleNamespace(kind='speed_queues', params={'set': [['TTSPD-1-10-5-50-11', '10.5.50.11', '2000k/2000k']], 'remove': []},
                              router=self.router, pk=1)
        b = agent._catalogue_body(cmd)
        self.assertIn('/queue simple move $sp destination=$d', b)
        self.assertIn('[/queue simple find name~"^TTFUP-"]', b)            # the move target skips fair usage caps


# ═══════════════════════ TapTap's own lanes, every pass ═══════════════════════
class LanesEveryPass(Setup):
    def active(self, *rows):
        return [{'user': u, 'address': ip, 'mac-address': mac} for u, ip, mac in rows]

    def test_nothing_happens_until_a_lane_is_asked_for(self):
        self.family()
        want = ts.desired_caps(self.router, self.active(('FAM001', '10.5.50.11', 'AA:BB:CC:00:00:01')))
        self.assertEqual(want, {})

    def test_a_guaranteed_voucher_gets_one_queue_per_device_at_the_whole_plan_speed(self):
        v = self.family()
        dl.add_lanes(self.biz, [v], self.owner, reason='test')
        want = ts.desired_caps(self.router, self.active(('FAM001', '10.5.50.11', 'A'), ('FAM001', '10.5.50.12', 'B'),
                                                        ('FAM001', '10.5.50.13', 'C'), ('OTHER9', '10.5.50.99', 'D')))
        self.assertEqual(sorted(x[0] for x in want.values()), ['10.5.50.11', '10.5.50.12', '10.5.50.13'])
        self.assertEqual({(x[1], x[2]) for x in want.values()}, {(2000, 2000)})       # 2M/2M EACH, not 2M split three ways
        self.assertEqual({x[4] for x in want.values()}, {dl.LANE_LABEL})

    def test_the_always_on_guarantee_covers_every_shared_voucher_and_member_but_not_single_device_ones(self):
        self.family()
        self.voucher('SOLO01', plan_name='Day')                                        # 1 device
        self.voucher('MEMBER01', plan_name='Gold', login_type='member', max_devices=2, router_profile='M-Gold', rate_limit='5M/5M')
        dl.set_shared(self.biz, True, self.owner)
        want = ts.desired_caps(self.router, self.active(('FAM001', '10.5.50.11', 'A'), ('SOLO01', '10.5.50.20', 'B'),
                                                        ('MEMBER01', '10.5.50.30', 'C'), ('MEMBER01', '10.5.50.31', 'D')))
        got = {x[0]: (x[1], x[2]) for x in want.values()}
        self.assertEqual(got, {'10.5.50.11': (2000, 2000), '10.5.50.30': (5000, 5000), '10.5.50.31': (5000, 5000)})

    def test_an_owners_own_speed_rule_wins_over_a_lane(self):
        from core.models_apps import TrafficSpeedRule
        v = self.family()
        rule = TrafficSpeedRule.objects.create(business=self.biz, name='Family slow', scope='plans', down_mbps=1, up_mbps=1, enabled=True)
        rule.plans.add(VoucherPlan.objects.get(business=self.biz, name='Family'))
        dl.add_lanes(self.biz, [v], self.owner)
        want = ts.desired_caps(self.router, self.active(('FAM001', '10.5.50.11', 'A')))
        self.assertEqual([(x[1], x[2], x[4]) for x in want.values()], [(1000, 1000, 'Family slow')])

    def test_a_lane_ends_with_the_vouchers_time_and_can_be_stopped(self):
        v = self.family(expires_at=timezone.now() + timedelta(hours=2))
        dl.add_lanes(self.biz, [v], self.owner)
        lane = DeviceLane.objects.get(voucher=v)
        self.assertAlmostEqual(lane.until, v.expires_at, delta=timedelta(minutes=1))
        dl.stop(self.biz, lane.pk, self.owner)
        self.assertEqual(ts.desired_caps(self.router, self.active(('FAM001', '10.5.50.11', 'A'))), {})

    def test_an_expired_lane_is_ignored(self):
        v = self.family()
        DeviceLane.objects.create(business=self.biz, scope='voucher', voucher=v, until=timezone.now() - timedelta(minutes=1))
        self.assertEqual(ts.desired_caps(self.router, self.active(('FAM001', '10.5.50.11', 'A'))), {})

    def test_api_routers_get_the_lanes_on_the_next_pass(self):
        v = self.family()
        dl.add_lanes(self.biz, [v], self.owner)
        added = []

        class Q:
            def get(self, **kw):
                return []

            def add(self, **kw):
                added.append(kw)

            def call(self, *a, **kw):
                return None

        svc = mock.Mock()
        svc.resource.return_value = Q()
        ts.enforce(self.router, self.active(('FAM001', '10.5.50.11', 'A'), ('FAM001', '10.5.50.12', 'B')), svc=svc)
        self.assertEqual(sorted(a['target'] for a in added), ['10.5.50.11/32', '10.5.50.12/32'])
        self.assertEqual({a['max_limit'] for a in added}, {'2000k/2000k'})

    def test_link_routers_get_them_in_one_batched_command(self):
        v = self.family()
        dl.add_lanes(self.biz, [v], self.owner)
        ts.enforce(self.router, self.active(('FAM001', '10.5.50.11', 'A'), ('FAM001', '10.5.50.12', 'B')), force=True)
        cmd = AgentCommand.objects.get(router=self.router, kind='speed_queues')
        self.assertEqual(sorted(r[1] for r in cmd.params['set']), ['10.5.50.11', '10.5.50.12'])


# ═══════════════════════ the pages ═══════════════════════
class Pages(Setup):
    def test_the_voucher_card_shows_one_lane_per_device_with_the_math(self):
        v = self.family()
        self.three_online()
        self.login()
        d = json.loads(self.client.get(f'/flow/speed/?voucher={v.pk}').content)
        self.assertEqual(len(d['devices']), 3)
        self.assertEqual(d['voucher']['down_bps'], 2_000_000)
        self.assertEqual(d['devices'][0]['pct'], 75)                    # 1.5 of its own 2 Mb/s
        self.assertIn('Each of its 3 devices gets 2.0 Mb/s down', d['math']['text'])
        self.assertFalse(d['guaranteed'])
        self.assertTrue(d['can_lane'])

    def test_guarantee_from_the_voucher_page(self):
        v = self.family()
        self.login()
        r = json.loads(self.client.post('/flow/speed/lane/', data=json.dumps({'action': 'add', 'voucher': v.pk}),
                                        content_type='application/json').content)
        self.assertTrue(r['ok'])
        d = json.loads(self.client.get(f'/flow/speed/?voucher={v.pk}').content)
        self.assertTrue(d['guaranteed'])
        self.assertEqual(d['lane']['voucher'], 'FAM001')
        self.assertIn('Speed per device', [a.type for a in self.biz.activities.all()])

    def test_a_voucher_without_a_speed_cannot_be_guaranteed(self):
        v = self.voucher('FREE01', plan_name='', max_devices=3, rate_limit='')
        self.login()
        r = self.client.post('/flow/speed/lane/', data=json.dumps({'action': 'add', 'voucher': v.pk}), content_type='application/json')
        self.assertIn('has no speed limit', json.loads(r.content)['message'])

    def test_checking_a_voucher_asks_its_router_about_exactly_its_devices(self):
        v = self.family()
        self.three_online()
        self.login()
        d = json.loads(self.client.post('/flow/speed/check/', data=json.dumps({'voucher': v.pk}), content_type='application/json').content)
        self.assertEqual(d['tests'][0]['kind'], 'lanes')
        cmd = AgentCommand.objects.get(router=self.router, kind='nettest')
        self.assertEqual(sorted(cmd.params['ips']), ['10.5.50.11', '10.5.50.12', '10.5.50.13'])
        self.assertEqual(cmd.params['codes'], ['FAM001'])

    def test_a_voucher_that_is_not_online_has_nothing_to_check(self):
        v = self.family()
        self.login()
        r = self.client.post('/flow/speed/check/', data=json.dumps({'voucher': v.pk}), content_type='application/json')
        self.assertIn('not online right now', json.loads(r.content)['message'])

    def test_the_fix_guarantees_the_vouchers_and_fixes_the_router_in_one_go(self):
        self.family()
        self.three_online()
        self.read(body('h=30|<hs-hotspot1>|bridge-hotspot|20M/20M|', ft=1, profiles='p=Family|||3|first\n'))
        self.login()
        d = json.loads(self.client.post('/flow/speed/fix/', data=json.dumps({'router': self.router.pk}), content_type='application/json').content)
        self.assertEqual(d['lanes_made'], 1)
        self.assertEqual(d['test']['kind'], 'lanefix')
        cmd = AgentCommand.objects.get(router=self.router, kind='nettest')
        self.assertEqual(cmd.params['rates'], [['Family', '2M/2M']])
        self.assertTrue(cmd.params['guard'])
        self.assertTrue(DeviceLane.objects.filter(voucher__code='FAM001', removed_at__isnull=True).exists())

    def test_the_fix_can_leave_fasttrack_alone(self):
        self.family()
        self.three_online()
        self.read(body('h=30|<hs-hotspot1>|bridge-hotspot|20M/20M|', ft=1, profiles='p=Family|||3|first\n'))
        self.login()
        self.client.post('/flow/speed/fix/', data=json.dumps({'router': self.router.pk, 'guard': False, 'lanes': False}),
                         content_type='application/json')
        cmd = AgentCommand.objects.get(router=self.router, kind='nettest')
        self.assertFalse(cmd.params['guard'])
        self.assertFalse(DeviceLane.objects.exists())

    def test_the_fix_answer_reads_back_from_the_router(self):
        self.family()
        self.three_online()
        p = {'ips': ['10.5.50.11', '10.5.50.12', '10.5.50.13'], 'rates': [['Family', '2M/2M']], 'unparent': [], 'guard': True, 'nets': ['10.5.50.0/24']}
        res = lab.parse_link(self.router, 'lanefix', p, nt._lines(
            b'ch=Family\ngd=1\nn=40\nd=1|<hotspot-FAM001-1>|10.5.50.11/32|2M/2M|\np=Family|2M/2M||3|first\nft=1\nfg=2\n'))
        self.assertIn('speed written on Family', res['fix_title'])
        self.assertIn('hotspot customers kept out of FastTrack', res['fix_title'])
        self.assertFalse(res.get('failed_step'))

    def test_the_traffic_tab_lists_routers_lanes_and_what_each_shared_plan_gives(self):
        v = self.family()
        self.three_online()
        dl.add_lanes(self.biz, [v], self.owner, reason='test')
        self.login()
        d = json.loads(self.client.get('/flow/speed/').content)
        self.assertEqual(d['routers'][0]['shared_online'], 3)
        self.assertEqual([l['voucher'] for l in d['lanes']], ['FAM001'])
        fam = next(p for p in d['plans'] if p['name'] == 'Family')
        self.assertEqual(fam['total'], '6.0 Mb/s down · 6.0 Mb/s up')

    def test_the_always_on_switch(self):
        self.login()
        r = json.loads(self.client.post('/flow/speed/lane/', data=json.dumps({'action': 'shared_on'}), content_type='application/json').content)
        self.assertTrue(r['shared_on'])
        self.assertTrue(json.loads(self.client.get('/flow/speed/').content)['shared_on'])
        self.client.post('/flow/speed/lane/', data=json.dumps({'action': 'shared_off'}), content_type='application/json')
        self.assertFalse(json.loads(self.client.get('/flow/speed/').content)['shared_on'])

    def test_pages_render_with_the_new_parts(self):
        v = self.family()
        self.login()
        for url, needle in ((f'/vouchers/{v.pk}/', 'data-lanes-voucher'), ('/traffic/', 'data-lanes-tab'), ('/plans/', 'lanes.js')):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertIn(needle, r.content.decode(), url)

    def test_the_member_page_renders_with_its_lanes(self):
        m = self.voucher('MEMBER01', plan_name='Gold', login_type='member', max_devices=2, router_profile='M-Gold', rate_limit='5M/5M')
        self.login()
        r = self.client.get(f'/members/{m.pk}/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('data-lanes-voucher', r.content.decode())


class BeforeTheDatabaseStep(Setup):
    def setUp(self):
        super().setUp()
        from django.db import connection
        with connection.cursor() as c:
            c.execute('DROP TABLE core_devicelane')
        cache.delete('tt:lanes:table')

    def tearDown(self):
        cache.delete('tt:lanes:table')
        super().tearDown()

    def test_speeds_still_flow_and_the_page_says_what_to_run(self):
        v = self.family()
        self.three_online()
        self.assertEqual(ts.desired_caps(self.router, [{'user': 'FAM001', 'address': '10.5.50.11'}]), {})   # live sync unharmed
        self.login()
        d = json.loads(self.client.get(f'/flow/speed/?voucher={v.pk}').content)
        self.assertIn('migrate core', d['setup'])
        self.assertFalse(d['can_lane'])
        r = self.client.post('/flow/speed/lane/', data=json.dumps({'action': 'add', 'voucher': v.pk}), content_type='application/json')
        self.assertIn('migrate core', json.loads(r.content)['message'])
