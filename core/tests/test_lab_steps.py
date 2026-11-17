"""Connection lab on a busy TapTap Link router (seen live on "TapTap K": both checks failed after 6 minutes).

* The lab now asks the router in two small steps that each fit one protected command (the old device check was too
  big, so it travelled unprotected and a router that refused it went silent).
* A step a person is watching gets an express seat on RouterOS 7 check-ins, behind the vouchers, never ahead of them.
* The lab starts from what TapTap already knows, so it is never empty; the router's measurement replaces it.
* A late answer explains itself with facts; an answer TapTap cannot read is reported instead of lost.
"""
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.utils import timezone

from core import agent, conn_lab as lab, line_steer as ls, nettools as nt
from core.models import AgentCommand, NetTest, Voucher
from core.tests.test_flow_lab import MANGLE, Base, URL


def cmd_for(kind, params, router_id=12):
    return SimpleNamespace(pk=99987654, kind='nettest', params={'test_id': 99123456, 'kind': kind, **params},
                           router=SimpleNamespace(pk=router_id), router_id=router_id, attempts=0)


class StepsFitOneProtectedCommand(Base):
    WORST = {'ip': '172.16.100.113', 'mac': '4A:F8:4D:33:AB:FA', 'code': 'kerrsering-member-' + 'x' * 30, 'watch': True, 'rid': 12345,
             'marks': ['TT-sfp-sfpplus1', 'TT-ether10-gamtel', 'TT-lte1', 'TT-ether2'], 'key': '4A:F8:4D:33:AB:FA', 'wifi': [0]}

    def test_both_steps_are_protected_even_in_the_worst_case(self):
        for url in (URL, 'https://hotspot-manager.taptap-wifi-gambia.com'):
            for kind in ('labq', 'labd'):
                w = agent.wrap(cmd_for(kind, self.WORST), url, 'yes')
                self.assertLessEqual(len(w), agent.MAX_SCRIPT, (kind, url))
                self.assertTrue(w.startswith(':do { :local ttRun [:parse'), f'{kind} must travel protected')

    def test_the_old_device_check_was_too_big_to_be_protected(self):
        w = agent.wrap(cmd_for('client', {'ip': '172.16.1.113', 'mac': '4A:F8:4D:33:AB:FA', 'code': 'anna'}), URL, 'no')
        self.assertGreater(len(w), agent.MAX_SCRIPT)                     # why "took the test but did not answer"
        self.assertFalse(w.startswith(':do { :local ttRun [:parse'))

    def test_no_routeros_string_holds_a_raw_line_break(self):
        params = {'ip': '10.5.50.12', 'mac': 'AA:BB:CC:DD:EE:01', 'code': 'anna', 'marks': ['TT-ether1', 'TT-ether2'],
                  'key': 'AA:BB:CC:DD:EE:01', 'rid': 1, 'wifi': [0, 1, 2], 'ips': ['10.5.50.12'], 'minutes': 60, 'lease': '1h',
                  'servers': ['dhcp1'], 'rates': [['Family', '2M/2M']], 'unparent': ['Family'], 'guard': True, 'nets': ['10.5.50.0/24'],
                  'mark': 'TT-ether2', 'fields': {'chain': 'prerouting', 'action': 'mark-connection', 'new-connection-mark': 'TT-ether2',
                                                  'src-mac-address': 'AA:BB:CC:DD:EE:01', 'comment': 'TapTap line-pin AA:BB:CC:DD:EE:01 anna to B'}}
        for kind in lab.LAB_KINDS:
            body = nt.link_body(cmd_for(kind, params), URL, 'no', 'n' * 24)
            self.assertNotIn('\n', body, kind)

    def test_the_quick_step_never_walks_every_queue_on_a_busy_router(self):
        body = lab.link_body('labq', {'ip': '172.16.1.113', 'code': 'anna', 'rid': 12}, agent.rs)
        self.assertNotIn('[/queue simple find]', body)
        self.assertIn('[/queue simple find where name~"anna"]', body)
        self.assertIn('[/queue simple find where name~"TTSPD-12-172-16-1-113"]', body)

    def test_a_code_that_could_carry_a_regex_is_not_used_to_find_queues(self):
        body = lab.link_body('labq', {'ip': '172.16.1.113', 'code': 'a.*b', 'rid': 12}, agent.rs)
        self.assertNotIn('name~"a.*b"', body)

    def test_the_lab_upload_retries_and_still_finishes(self):
        body = nt.link_body(cmd_for('labq', {'ip': '172.16.1.113', 'code': 'anna'}), URL, 'no', 'n' * 24)
        self.assertIn(':for t from=1 to=3 do={ :if ($up = 0)', body)
        self.assertEqual(body.count('/tool fetch'), 1)                    # written once, tried three times


class ReadingTheSteps(Base):
    def test_the_quick_answer(self):
        self.snapshot_lines()
        p = lab.clean('labq', {'ip': '10.5.50.12', 'mac': 'AA:BB:CC:DD:EE:01', 'code': 'KS1234'}, self.router)
        self.assertTrue(p['watch'])
        res = lab.parse_link(self.router, 'labq', p, nt._lines(
            b'arp=AA:BB:CC:DD:EE:01|bridge\nr=1|12ms\nr=1|14ms\nr=0|\nr=1|20ms\nr=1|13ms\nact=KS1234|4h|00:00:05\n'
            b'q=<hotspot-KS1234>|10.5.50.12/32|2M/4M|0/3900k|0/12000|0/90000\nlt=6\nlm=TT-ether1|0\nlm=TT-ether2|6\n'))
        self.assertEqual(sorted(res), ['active', 'arp', 'lane', 'ping', 'queues'])
        self.assertEqual(res['ping']['received'], 4)
        self.assertEqual(res['queues'][0]['drop_down'], 12000)
        self.assertEqual(res['lane']['label'], 'Africell')

    def test_a_quick_answer_without_pings_is_unknown_not_down(self):
        p = lab.clean('labq', {'ip': '10.5.50.12', 'code': 'KS1234'}, self.router)
        self.assertIsNone(lab.parse_link(self.router, 'labq', p, nt._lines(b'act=KS1234|4h|0s\n'))['ping'])

    def test_the_detail_answer(self):
        p = lab.clean('labd', {'ip': '10.5.50.12', 'mac': 'AA:BB:CC:DD:EE:01', 'code': 'KS1234'}, self.router)
        res = lab.parse_link(self.router, 'labd', p, nt._lines(
            b'cpu=34\ni=3|41ms\nport=wlan2\nlease=bound|00:20:00|dhcp-hs|true|redmi\nhost=true|false|0s\nwifi=-79|24Mbps|6Mbps|55|wlan2\n'
            b't=2\nl=nov/17 14:01:01|KS1234 (10.5.50.12): logged out: keepalive timeout\n'))
        self.assertEqual(sorted(res), ['cpu', 'drops', 'host', 'internet', 'lease', 'port', 'wifi'])
        self.assertEqual((res['wifi']['signal'], res['cpu'], res['internet']['avg']), (-79, 34, 41.0))
        self.assertEqual((res['drops']['total'], res['drops']['in_log']), (1, 2))      # lines read / sign-outs the router counted

    def snapshot_lines(self):
        self.snapshot()


class ExpressSeat(Base):
    def busy(self, n=60):
        for i in range(n):
            Voucher.objects.create(business=self.biz, router=self.router, code=f'V{i:04}', plan_name='Day', mikrotik_sync_status='Pending')
        agent.push_pending_vouchers(self.router)

    def lab_step(self, watch=True):
        p = lab.clean('labq', {'ip': '10.5.50.12', 'code': 'KS1234'}, self.router)
        if not watch:
            p.pop('watch')
        t = NetTest.objects.create(business=self.biz, router=self.router, kind='labq', params=p, status='waiting', via='TapTap Link')
        return agent.queue(self.router, 'nettest', {'test_id': t.pk, 'kind': 'labq', **p}, label='lab', minutes=6)

    def test_a_watched_step_rides_behind_the_vouchers_at_once_on_routeros_7(self):
        self.busy()
        cmd = self.lab_step()
        script = agent.build_response(self.router, URL)
        kinds = set(AgentCommand.objects.filter(router=self.router, status='sent').values_list('kind', flat=True))
        self.assertEqual(kinds, {'hotspot_users', 'nettest'})              # vouchers first, the lab step right behind
        self.assertLess(script.index('hotspot'), script.index(f'c={cmd.pk}&'))
        self.assertGreater(len(script), agent.MAX_SCRIPT)                  # past the batch budget, as RouterOS 7 allows

    def test_routeros_6_keeps_the_old_rule(self):
        self.agent.ros_version = '6.49.10 (long-term)'
        self.agent.save(update_fields=['ros_version'])
        self.busy()
        self.lab_step()
        agent.build_response(self.router, URL)
        self.assertEqual(set(AgentCommand.objects.filter(router=self.router, status='sent').values_list('kind', flat=True)), {'hotspot_users'})

    def test_only_a_test_a_person_is_watching_gets_the_seat(self):
        self.busy()
        self.lab_step(watch=False)
        agent.build_response(self.router, URL)
        self.assertNotIn('nettest', set(AgentCommand.objects.filter(router=self.router, status='sent').values_list('kind', flat=True)))

    def test_a_restart_still_travels_alone(self):
        self.lab_step()
        agent.queue(self.router, 'reboot', {}, label='Restart')
        agent.build_response(self.router, URL)
        self.assertEqual(set(AgentCommand.objects.filter(router=self.router, status='sent').values_list('kind', flat=True)), {'reboot'})


class LateAnswersExplainThemselves(Base):
    def waiting(self, polls0=100):
        t = NetTest.objects.create(business=self.biz, router=self.router, kind='labq', params={'ip': '10.5.50.12'}, status='waiting',
                                   via='TapTap Link', result={'polls0': polls0})
        cmd = agent.queue(self.router, 'nettest', {'test_id': t.pk, 'kind': 'labq', 'ip': '10.5.50.12'}, label='lab', minutes=30)
        t.command_id = cmd.pk
        t.save(update_fields=['command_id'])
        NetTest.objects.filter(pk=t.pk).update(created_at=timezone.now() - timedelta(minutes=7))
        t.refresh_from_db()
        return t, cmd

    def test_a_router_too_busy_to_take_it_says_how_busy(self):
        self.agent.polls = 109
        self.agent.save(update_fields=['polls'])
        t, _cmd = self.waiting()
        for i in range(3):
            v = Voucher.objects.create(business=self.biz, router=self.router, code=f'B{i}', plan_name='Day')
            c = agent.queue(self.router, 'hotspot_user_set', {'name': v.code}, label='v')
            AgentCommand.objects.filter(pk=c.pk).update(status='done', sent_at=timezone.now())
        nt.expire_waiting(t)
        self.assertIn('checked in 9 times in 6 minutes, but every check-in was already full (3 voucher changes went first', t.result['error'])

    def test_a_router_that_never_checked_in_is_called_that(self):
        self.agent.polls = 100
        self.agent.save(update_fields=['polls'])
        t, _cmd = self.waiting()
        nt.expire_waiting(t)
        self.assertIn('did not check in during those 6 minutes', t.result['error'])

    def test_a_router_that_took_it_but_never_answered(self):
        self.agent.polls = 104
        self.agent.save(update_fields=['polls'])
        t, cmd = self.waiting()
        AgentCommand.objects.filter(pk=cmd.pk).update(status='sent', sent_at=timezone.now() - timedelta(minutes=5))
        nt.expire_waiting(t)
        self.assertIn('took the test at', t.result['error'])
        self.assertIn('4 check-ins since, so the router is alive', t.result['error'])

    def test_a_router_that_refused_the_script_says_so(self):
        t, cmd = self.waiting()
        agent.handle_ack(cmd.pk, agent.nonce(cmd), 'fail')
        NetTest.objects.filter(pk=t.pk).update(created_at=timezone.now() - timedelta(minutes=1))
        t.refresh_from_db()
        nt.expire_waiting(t)
        self.assertIn('stopped with an error (this RouterOS version refused part of it)', t.result['error'])

    def test_an_answer_taptap_cannot_read_is_reported_not_lost(self):
        t, cmd = self.waiting()
        with mock.patch('core.conn_lab.parse_stage', side_effect=KeyError('x')), self.assertLogs('taptap.nettools', 'ERROR'):
            r = self.client.post(f'/api/agent/v1/nettest?c={cmd.pk}&n={agent.nonce(cmd)}', data=b'arp=x|y\n', content_type='text/plain')
        self.assertEqual(r.status_code, 200)                               # the router is not told to retry for 6 minutes
        t.refresh_from_db()
        self.assertEqual(t.status, 'failed')
        self.assertIn('could not read its answer (KeyError)', t.result['error'])


class NeverAnEmptyLab(Base):
    def test_before_the_router_answers_the_stations_come_from_taptaps_own_data(self):
        ctx = {'online': True, 'down_bps': 1_900_000, 'limit_down': 2_000_000, 'limit_source': 'plan', 'quiet_s': 0,
               'lane': {'state': 'one', 'label': 'Gamtel'}, 'snap': {'iface': 'wlan2', 'age': 600}, 'heard_s': 8}
        v = lab.verdict(ctx)
        st = v['stations']
        self.assertEqual({k: s['src'] for k, s in st.items()}, {k: 'known' for k in st})
        self.assertEqual((st['line']['detail'], st['internet']['detail']), ('Gamtel', 'router reached TapTap just now'))
        self.assertIn('95% used now', st['limit']['detail'])
        self.assertEqual(v['head']['title'], 'Using all the speed it is allowed (1.9 Mb/s of 2.0 Mb/s)')
        self.assertFalse(v['head']['measured'])

    def test_a_weak_signal_from_the_last_sync_is_shown_and_replaced_when_measured(self):
        ctx = {'online': True, 'snap': {'wifi': {'signal': -83, 'interface': 'wlan2'}, 'age': 900}}
        known = lab.verdict(ctx)
        self.assertIn('at the last sync, 15 min ago', known['findings'][0]['title'])
        ping = {'sent': 5, 'received': 5, 'loss': 0, 'avg': 9.0, 'jitter': 1.0, 'times': [9] * 5, 'quality': 'excellent', 'quality_note': ''}
        measured = lab.verdict(ctx, {'ping': ping, 'wifi': {'signal': -61, 'tx': '144Mbps', 'rx': '72Mbps', 'ccq': None, 'interface': 'wlan2'}}, {})
        self.assertFalse(any(f['key'] == 'signal' for f in measured['findings']))   # the old reading is gone
        self.assertEqual(measured['stations']['link']['src'], 'measured')
        self.assertTrue(measured['head']['measured'])

    def test_the_lab_starts_both_steps_and_shows_where_they_are(self):
        self.snapshot()
        self.voucher('KS1234')
        self.sessions({'user': 'KS1234', 'ip': '10.5.50.12', 'down': 900_000})
        self.login()
        d = json.loads(self.client.post('/flow/lab/run/', data=json.dumps({'router': self.router.pk, 'ip': '10.5.50.12', 'code': 'KS1234'}),
                                        content_type='application/json').content)
        self.assertEqual([t['kind'] for t in d['tests']], ['labq', 'labd'])
        self.assertTrue(all(t['delivery']['link'] and t['delivery']['express'] for t in d['tests']))
        cmds = AgentCommand.objects.filter(router=self.router, kind='nettest').order_by('pk')
        self.assertTrue(all(c.params['watch'] for c in cmds))
        f = json.loads(self.client.get(f'/flow/test/{d["tests"][0]["id"]}/').content)['test']['delivery']
        self.assertEqual(f['stage'], 'queued')
        self.assertIn('usual_s', f)

    def test_the_verdict_mixes_known_and_measured(self):
        self.snapshot()
        self.voucher('KS1234')
        self.sessions({'user': 'KS1234', 'ip': '10.5.50.12', 'down': 900_000})
        ls.note_marks(self.router, {'10.5.50.12': {'TT-ether1': 4}})
        q = NetTest.objects.create(business=self.biz, router=self.router, kind='labq', status='done', finished_at=timezone.now(),
                                   result={'ping': {'sent': 5, 'received': 5, 'loss': 0, 'avg': 8.0, 'jitter': 1.0, 'times': [8] * 5,
                                                    'quality': 'excellent', 'quality_note': ''}, 'queues': [], 'lane': None})
        self.login()
        v = json.loads(self.client.post('/flow/lab/verdict/', data=json.dumps({'router': self.router.pk, 'ip': '10.5.50.12', 'code': 'KS1234',
                                                                             'labq': q.pk}), content_type='application/json').content)['verdict']
        self.assertEqual(v['stations']['device']['src'], 'measured')
        self.assertEqual(v['stations']['line']['src'], 'known')
        self.assertEqual(v['stations']['line']['detail'], 'Gamtel')
        self.assertFalse(v['done'])                                        # the detail step has not answered yet


class DirectApiSteps(Base):
    link = False

    def test_both_steps_answer_at_once(self):
        self.voucher('KS1234')
        self.sessions({'user': 'KS1234', 'ip': '10.5.50.12'})

        class Svc:
            def __init__(self, *a, **kw):
                pass

            def connect(self):
                return self

            def close(self):
                pass

            def safe_get(self, path, **where):
                return {'/ip/arp': [{'mac-address': 'aa:bb:cc:dd:ee:01', 'interface': 'bridge'}],
                        '/interface/bridge/host': [{'on-interface': 'wlan1'}],
                        '/interface/wireless/registration-table': [{'signal-strength': '-58', 'tx-rate': '144Mbps', 'interface': 'wlan1'}],
                        '/system/resource': [{'cpu-load': '12'}]}.get(path, [])

            def resource(self, path):
                class R:
                    def get(self, **kw):
                        return [{'name': '<hotspot-KS1234>', 'target': '10.5.50.12/32', 'max-limit': '2M/4M', 'rate': '0/1M',
                                 'dropped': '0/0', 'packets': '0/900'}] if path == '/queue/simple' else []

                    def call(self, verb, args=None):
                        if verb == 'ping':
                            return [{'seq': i, 'time': '7ms'} for i in range(int((args or {}).get('count', 3)))]
                        return None
                return R()

        with mock.patch('core.mikrotik.MikroTikService', Svc):
            self.login()
            d = json.loads(self.client.post('/flow/lab/run/', data=json.dumps({'router': self.router.pk, 'ip': '10.5.50.12', 'code': 'KS1234'}),
                                            content_type='application/json').content)
        q, dd = d['tests']
        self.assertEqual((q['status'], dd['status']), ('done', 'done'))
        self.assertEqual(q['result']['queues'][0]['max_down'], 4_000_000)
        self.assertEqual(dd['result']['wifi']['signal'], -58)
        self.assertEqual(dd['result']['internet']['avg'], 7.0)
        self.assertFalse(q['delivery']['link'])
