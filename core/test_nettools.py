"""Network tools: ping, traceroute, DNS, website, speed test and the Internet check — over the API and TapTap Link.

Run:  DB_ENGINE=sqlite python manage.py test core.test_nettools
"""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import nettools as nt
from .models import AgentCommand, Business, Router
from .models_nettools import NetTest
from .test_link_install import routeros_balanced


class FakeResource:
    def __init__(self, svc, path):
        self.svc, self.path = svc, path

    def call(self, cmd, args):
        self.svc.calls.append((self.path, cmd, dict(args)))
        h = self.svc.handlers.get((self.path, cmd))
        if isinstance(h, Exception):
            raise h
        return h(args) if callable(h) else (h or [])

    def get(self, **kw):
        return self.svc.tables.get(self.path, [])


class FakeSvc:
    def __init__(self, handlers=None, tables=None):
        self.handlers, self.tables, self.calls = handlers or {}, tables or {}, []

    def connect(self):
        return self

    def close(self):
        pass

    def resource(self, path):
        return FakeResource(self, path)

    def safe_get(self, path, **kw):
        return self.tables.get(path, [])


def ping_rows(times, host='8.8.8.8'):
    rows = []
    for i, t in enumerate(times):
        rows.append({'seq': str(i), 'host': host, 'size': '56', **({'time': t, 'ttl': '117'} if t else {'status': 'timeout'})})
    return rows


def healthy(extra=None):
    h = {('/', 'ping'): lambda a: ping_rows(['12ms'] * int(a['count']), host='142.250.1.1' if a['address'] == 'google.com' else a['address']),
         ('/tool', 'fetch'): [{'status': 'finished', 'downloaded': '0'}]}
    h.update(extra or {})
    return FakeSvc(h, {'/ip/route': [{'dst-address': '0.0.0.0/0', 'gateway': '192.168.1.1', 'active': 'true'}],
                       '/ip/dns': [{'servers': '1.1.1.1,8.8.8.8', 'dynamic-servers': '41.223.1.1'}]})


class ParsingTests(TestCase):
    def test_durations(self):
        for v, want in (('12ms', 12), ('12ms345us', 12.345), ('345us', 0.345), ('1s200ms', 1200), ('00:00:01.5', 1500), ('7', 7), ('timeout', None), ('', None), ('x', None)):
            got = nt.parse_ms(v)
            self.assertEqual(round(got, 3) if got is not None else None, want, v)
        self.assertEqual(nt.parse_kib('2048'), 2048 * 1024)
        self.assertEqual(nt.parse_kib('1.5MiB'), int(1.5 * 1024 * 1024))

    def test_targets_are_checked(self):
        self.assertEqual(nt.clean_host(' Google.COM '), 'google.com')
        self.assertEqual(nt.clean_host('https://google.com/x'), 'google.com')
        self.assertEqual(nt.clean_host('192.168.88.20'), '192.168.88.20')
        for bad in ('8.8.8.8; /system reset', 'a"b', '$(x)', 'a b', '127.0.0.1', '-bad.com', '', 'x' * 300):
            with self.assertRaises(ValueError, msg=bad):
                nt.clean_host(bad)
        self.assertEqual(nt.clean_url('example.com'), 'https://example.com/')
        self.assertEqual(nt.clean_url('http://example.com:8080/a/b?x=1&y=2'), 'http://example.com:8080/a/b?x=1&y=2')
        for bad in ('http://x.com/"]; /system reset', 'http://x.com/$a', 'ftp://x.com', 'http://u:p@x.com/', 'http://x.com/a b'):
            with self.assertRaises(ValueError, msg=bad):
                nt.clean_url(bad)
        self.assertEqual(nt.clean_params('ping', {'target': '1.1.1.1', 'count': '500', 'size': '1'}), {'target': '1.1.1.1', 'count': 20, 'size': 28})

    def test_ping_and_quality(self):
        svc = FakeSvc({('/', 'ping'): ping_rows(['10ms', '12ms', None, '11ms500us'])})
        r = nt.run_ping(svc, {'target': '8.8.8.8', 'count': 4, 'size': 56})
        self.assertEqual((r['sent'], r['received'], r['loss'], r['min'], r['max']), (4, 3, 25, 10, 12))
        self.assertEqual(r['quality'], 'poor')            # 25% loss
        r = nt.ping_stats([20, 22, 21, 20])
        self.assertEqual(r['quality'], 'excellent')

    def test_traceroute_takes_the_final_table(self):
        rows = [{'.section': '0', 'address': '192.168.1.1', 'loss': '0%', 'last': '1ms'},
                {'.section': '0', 'address': '', 'loss': '100%', 'last': ''},
                {'.section': '1', 'address': '192.168.1.1', 'loss': '0%', 'last': '1ms'},
                {'.section': '1', 'address': '100.64.0.1', 'loss': '0%', 'last': '9ms'},
                {'.section': '1', 'address': '8.8.8.8', 'loss': '0%', 'last': '210ms'}]
        r = nt.run_trace(FakeSvc({('/tool', 'traceroute'): rows}), {'target': '8.8.8.8'})
        self.assertEqual([h['address'] for h in r['hops']], ['192.168.1.1', '100.64.0.1', '8.8.8.8'])
        self.assertEqual([h['zone'] for h in r['hops']], ['local', 'isp', 'internet'])
        self.assertTrue(r['reached']); self.assertIn('Latency jumps at hop 3', r['note'])

    def test_speed_test_picks_the_size(self):
        sizes = []

        def fetch(a):
            sizes.append(a['url'])
            return [{'status': 'finished', 'downloaded': str(int(a['url'].split('=')[1]) // 1024)}]
        with mock.patch('core.nettools.time.monotonic', side_effect=[0, 0.1, 10, 12]):      # probe 80 Mbit/s → big download
            r = nt.run_speed(FakeSvc({('/tool', 'fetch'): fetch}), {})
        self.assertIn('bytes=25000000', sizes[1])
        self.assertTrue(r['ok']); self.assertAlmostEqual(r['mbps'], 100.0, delta=1)
        self.assertGreater(r['people'], 50)


class DiagnosisTests(TestCase):
    def test_everything_works(self):
        d = nt.run_doctor(healthy())
        self.assertEqual(d['level'], 'ok')
        self.assertEqual([c['state'] for c in d['chain']], ['ok'] * 5)

    def test_dns_broken(self):
        def ping(a):
            if a['address'] == 'google.com':
                raise Exception("('Error \"failure: could not resolve dns name\" executing command', b'failure: could not resolve dns name')")
            return ping_rows(['15ms'] * int(a['count']), a['address'])
        d = nt.run_doctor(healthy({('/', 'ping'): ping}))
        self.assertEqual(d['level'], 'bad'); self.assertIn('DNS', d['title'])

    def test_modem_only(self):
        d = nt.run_doctor(healthy({('/', 'ping'): lambda a: ping_rows(['2ms'] * 3, a['address']) if a['address'] == '192.168.1.1' else ping_rows([None] * int(a['count']), a['address']),
                                   ('/tool', 'fetch'): Exception('timeout')}))
        self.assertIn('modem answers', d['title'])
        self.assertEqual(d['chain'][1]['state'], 'ok'); self.assertEqual(d['chain'][2]['state'], 'bad')

    def test_no_route(self):
        svc = healthy({('/', 'ping'): lambda a: ping_rows([None] * int(a['count']), a['address']), ('/tool', 'fetch'): Exception('x')})
        svc.tables['/ip/route'] = []
        d = nt.run_doctor(svc)
        self.assertEqual(d['title'], 'No Internet line on the router')


@override_settings(AUTH_EMAIL_OTP=False, SITE_URL='https://taptap.example')
class PageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o', 'o@x.gm', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1', trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=self.b, name='hAP', ip_address='10.0.0.1', username='u', password='p', status='Online')
        self.client.force_login(self.owner)

    def run_test(self, data, svc=None):
        with mock.patch('core.mikrotik.MikroTikService', return_value=svc or healthy()):
            return self.client.post(reverse('network_tools_run'), json.dumps({'router': self.r.pk, **data}), content_type='application/json')

    def test_page_and_api_run(self):
        page = self.client.get(reverse('network_tools'))
        self.assertContains(page, 'Check the Internet'); self.assertContains(page, 'Direct API')
        r = self.run_test({'kind': 'ping', 'target': '8.8.8.8', 'count': 3}).json()
        self.assertEqual((r['test']['status'], r['test']['result']['received']), ('done', 3))
        r = self.run_test({'kind': 'doctor'}).json()
        self.assertEqual(r['test']['result']['level'], 'ok')
        self.assertContains(self.client.get(reverse('network_tools')), 'The Internet is working')     # in the history
        bad = self.run_test({'kind': 'ping', 'target': '8.8.8.8; /system reset'})
        self.assertEqual(bad.status_code, 400)

    def test_router_error_is_shown(self):
        svc = FakeSvc({('/tool', 'traceroute'): Exception('no route to host')})
        r = self.run_test({'kind': 'trace', 'target': '8.8.8.8'}, svc).json()
        self.assertEqual(r['test']['status'], 'failed'); self.assertIn('no route', r['test']['result']['error'])

    def test_speed_tests_are_spaced_out(self):
        svc = FakeSvc({('/tool', 'fetch'): [{'status': 'finished', 'downloaded': '1000'}]})
        self.assertEqual(self.run_test({'kind': 'speed'}, svc).status_code, 200)
        r = self.run_test({'kind': 'speed'}, svc)
        self.assertEqual(r.status_code, 400); self.assertIn('Wait two minutes', r.json()['message'])

    def test_other_business_router_refused(self):
        other = User.objects.create_user('x', 'x@x.gm', 'pw12345678')
        ob = Business.objects.create(user=other, business_name='X', owner_name='X', phone='1', trial_ends_at=timezone.now() + timedelta(days=9))
        orr = Router.objects.create(business=ob, name='theirs', ip_address='10.9.9.9', username='u', password='p')
        r = self.client.post(reverse('network_tools_run'), json.dumps({'router': orr.pk, 'kind': 'ping', 'target': '8.8.8.8'}), content_type='application/json')
        self.assertEqual(r.status_code, 400)
        t = NetTest.objects.create(business=ob, router=orr, kind='ping', target='8.8.8.8')
        self.assertEqual(self.client.get(reverse('network_tools_test', args=[t.pk])).status_code, 404)

    def test_staff_without_network_rights_refused(self):
        from .models_team import TeamMember
        u = User.objects.create_user('s', 's@x.gm', 'pw12345678')
        TeamMember.objects.create(business=self.b, user=u, role='voucher_creator')
        self.client.force_login(u)
        self.assertNotEqual(self.client.get(reverse('network_tools')).status_code, 200)


@override_settings(AUTH_EMAIL_OTP=False, SITE_URL='https://taptap.example')
class LinkTests(PageTests):
    def setUp(self):
        super().setUp()
        self.r.connection_mode = 'agent'; self.r.save()

    def link_run(self, data):
        with mock.patch('core.voucher_history.channel', return_value='TapTap Link'), mock.patch('core.linkops.ensure_online'):
            return self.client.post(reverse('network_tools_run'), json.dumps({'router': self.r.pk, **data}), content_type='application/json').json()['test']

    def upload(self, cmd, body, n=None):
        from .agent import nonce
        return self.client.post(reverse('agent_nettest') + f'?c={cmd.pk}&n={n if n is not None else nonce(cmd)}', body, content_type='text/plain')

    def test_page_and_api_run(self):      # replaced for Link routers
        pass

    def test_router_error_is_shown(self):
        pass

    def test_speed_tests_are_spaced_out(self):
        pass

    def test_ping_round_trip(self):
        t = self.link_run({'kind': 'ping', 'target': 'google.com', 'count': 3})
        self.assertEqual(t['status'], 'waiting')
        cmd = AgentCommand.objects.get(kind='nettest')
        self.assertEqual(cmd.params['test_id'], t['id'])
        from .agent import wrap
        body = wrap(cmd, 'https://taptap.example', 'yes-without-crl')
        self.assertTrue(routeros_balanced(body))
        self.assertIn(':resolve "google.com"', body); self.assertIn('flood-ping address=$ip count=1 size=56', body)
        self.assertIn(f'/api/agent/v1/nettest?c={cmd.pk}&n=', body)
        self.assertEqual(self.upload(cmd, b'ip=142.250.1.1\nr=21\nr=-\nr=00:00:00.019\n').status_code, 200)
        got = self.client.get(reverse('network_tools_test', args=[t['id']])).json()['test']
        self.assertEqual((got['status'], got['result']['received'], got['result']['host']), ('done', 2, '142.250.1.1'))

    def test_doctor_round_trip(self):
        t = self.link_run({'kind': 'doctor'})
        cmd = AgentCommand.objects.get(kind='nettest')
        from .agent import wrap
        self.assertTrue(routeros_balanced(wrap(cmd, 'https://taptap.example', 'no')))
        self.upload(cmd, b'gw=192.168.1.1\ngwp=3|2\np=1.1.1.1|3|18\np=8.8.8.8|3|20\ndns=x\nservers=\ndyn=41.223.1.1\nweb=x\n')
        got = NetTest.objects.get(pk=t['id'])
        self.assertEqual(got.status, 'done'); self.assertIn('DNS', got.result['title'])

    def test_all_scripts_are_well_formed(self):
        from .agent import wrap
        for data in ({'kind': 'trace', 'target': '8.8.8.8'}, {'kind': 'dns', 'target': 'google.com'}, {'kind': 'web', 'target': 'https://example.com/a?b=1'}, {'kind': 'speed'}):
            cache.clear()
            self.link_run(data)
            cmd = AgentCommand.objects.filter(kind='nettest').order_by('-pk').first()
            self.assertTrue(routeros_balanced(wrap(cmd, 'https://taptap.example', 'no')), data)

    def test_trace_web_speed_answers(self):
        self.link_run({'kind': 'trace', 'target': '8.8.8.8'})
        cmd = AgentCommand.objects.filter(kind='nettest').latest('pk')
        self.upload(cmd, b'h=192.168.1.1|0%|1ms\nh=8.8.8.8|0%|30ms\n')
        self.assertTrue(NetTest.objects.get(pk=cmd.params['test_id']).result['reached'])
        self.link_run({'kind': 'speed'})
        cmd = AgentCommand.objects.filter(kind='nettest').latest('pk')
        self.upload(cmd, b'st=finished\ndl=2930\ndu=00:00:03\n')
        r = NetTest.objects.get(pk=cmd.params['test_id']).result
        self.assertTrue(r['ok']); self.assertAlmostEqual(r['mbps'], 8.0, delta=0.2)

    def test_upload_is_protected_and_lost_tests_fail(self):
        t = self.link_run({'kind': 'dns', 'target': 'google.com'})
        cmd = AgentCommand.objects.get(kind='nettest')
        self.assertEqual(self.upload(cmd, b'ip=1.2.3.4', n='bad').status_code, 403)
        self.assertEqual(self.upload(cmd, b'x' * 20_000).status_code, 413)
        AgentCommand.objects.filter(pk=cmd.pk).update(status='expired')
        self.assertEqual(self.upload(cmd, b'ip=1.2.3.4').status_code, 410)
        got = self.client.get(reverse('network_tools_test', args=[t['id']])).json()['test']
        self.assertEqual(got['status'], 'failed'); self.assertIn('did not run', got['result']['error'])

    def test_queue_refuses_tampered_commands(self):
        from .agent import queue
        for p in ({'test_id': 1, 'kind': 'ping', 'target': '8.8.8.8"; /system reset', 'count': 3, 'size': 56},
                  {'test_id': 1, 'kind': 'ping', 'target': '8.8.8.8', 'count': 99, 'size': 56},
                  {'test_id': 'x', 'kind': 'dns', 'target': 'google.com'}, {'test_id': 1, 'kind': 'shell', 'target': 'x'}):
            with self.assertRaises(ValueError, msg=p):
                queue(self.r, 'nettest', p)

    def test_link_offline_is_explained(self):
        with mock.patch('core.voucher_history.channel', return_value='TapTap Link'), \
                mock.patch('core.linkops.ensure_online', side_effect=ValueError('TapTap Link has not checked in for 20 minutes')):
            t = self.client.post(reverse('network_tools_run'), json.dumps({'router': self.r.pk, 'kind': 'doctor'}), content_type='application/json').json()['test']
        self.assertEqual(t['status'], 'failed'); self.assertIn('20 minutes', t['result']['error'])


@override_settings(AUTH_EMAIL_OTP=False, SITE_URL='https://taptap.example')
class LinkPingFixTests(LinkTests):
    """flood-ping has no as-value: the Link script must use do={} with a plain /ping fallback,
    and a page that loads means the Internet works even when pings get no reply."""

    def test_ping_script_uses_do_block_and_ping_fallback(self):
        from .agent import wrap
        self.link_run({'kind': 'ping', 'target': '8.8.8.8', 'count': 3})
        body = wrap(AgentCommand.objects.get(kind='nettest'), 'https://taptap.example', 'no')
        self.assertNotIn('flood-ping address=$ip count=1 size=56 timeout', body)
        self.assertNotRegex(body, r'flood-ping[^\]]*as-value')
        self.assertIn('/tool flood-ping address=$ip count=1 size=56 do={ :if ($sent = 1) do={ :set rc $received; :set av $"avg-rtt" } }', body)
        self.assertIn(':set rc [/ping $ip count=1 size=56]', body)
        self.assertTrue(routeros_balanced(body))

    def test_doctor_script_has_no_flood_ping_as_value(self):
        from .agent import wrap
        self.link_run({'kind': 'doctor'})
        body = wrap(AgentCommand.objects.get(kind='nettest'), 'https://taptap.example', 'no')
        self.assertNotRegex(body, r'flood-ping[^\]]*as-value')
        for ip in ('$gip', '1.1.1.1', '8.8.8.8'):
            self.assertIn(f'/tool flood-ping address={ip} count=3 do=', body)
            self.assertIn(f'[/ping {ip} count=3]', body)
        self.assertTrue(routeros_balanced(body))

    def test_new_reply_format_including_untimed_replies(self):
        t = self.link_run({'kind': 'ping', 'target': '8.8.8.8', 'count': 4})
        cmd = AgentCommand.objects.get(kind='nettest')
        self.upload(cmd, b'ip=8.8.8.8\nr=1|23\nr=0|\nr=1|\nr=1|0\n')
        r = NetTest.objects.get(pk=t['id']).result
        self.assertEqual((r['sent'], r['received'], r['loss']), (4, 3, 25))
        self.assertEqual(r['times'], [23, None, 'ok', 0])
        self.assertEqual((r['min'], r['max']), (0, 23))

    def test_pings_blocked_but_web_loads_is_not_no_internet(self):
        """The reported case: gateway and 1.1.1.1/8.8.8.8 give no reply, DNS and the web page work."""
        t = self.link_run({'kind': 'doctor'})
        cmd = AgentCommand.objects.get(kind='nettest')
        self.upload(cmd, b'gw=192.168.1.1\ngwp=0|\np=1.1.1.1|0|\np=8.8.8.8|0|\ndns=192.178.223.138\nservers=\ndyn=192.168.1.1\nweb=finished|00:00:01\n')
        r = NetTest.objects.get(pk=t['id']).result
        self.assertEqual(r['level'], 'ok'); self.assertEqual(r['title'], 'The Internet is working')
        self.assertIn('blocking ping', r['detail'])
        states = {c['key']: (c['state'], c['detail']) for c in r['chain']}
        self.assertEqual(states['internet'], ('warn', 'ping blocked'))
        self.assertEqual(states['modem'][0], 'warn')
        self.assertEqual(states['web'], ('ok', 'about 1 s'))

    def test_ping_fallback_answer_counts_as_online(self):
        t = self.link_run({'kind': 'doctor'})
        cmd = AgentCommand.objects.get(kind='nettest')
        self.upload(cmd, b'gw=192.168.1.1\ngwp=3|\np=1.1.1.1|3|\np=8.8.8.8|3|\ndns=1.2.3.4\nweb=finished|00:00:00\n')
        r = NetTest.objects.get(pk=t['id']).result
        self.assertEqual(r['level'], 'ok')
        states = {c['key']: (c['state'], c['detail']) for c in r['chain']}
        self.assertEqual(states['modem'], ('ok', '192.168.1.1 · answers'))
        self.assertEqual(states['web'], ('ok', 'under 1 s'))

    def test_web_tool_says_whole_seconds(self):
        self.link_run({'kind': 'web', 'target': 'google.com'})
        cmd = AgentCommand.objects.get(kind='nettest')
        self.upload(cmd, b'st=finished\ndl=20\ndu=00:00:01\n')
        r = NetTest.objects.get(pk=cmd.params['test_id']).result
        self.assertTrue(r['ok']); self.assertIsNone(r['ms']); self.assertEqual(r['time_text'], 'about 1 s')

    def test_real_outage_still_says_no_internet(self):
        t = self.link_run({'kind': 'doctor'})
        cmd = AgentCommand.objects.get(kind='nettest')
        self.upload(cmd, b'gw=192.168.1.1\ngwp=0|\np=1.1.1.1|0|\np=8.8.8.8|0|\ndns=x\nweb=x\n')
        self.assertEqual(NetTest.objects.get(pk=t['id']).result['title'], 'No Internet')
