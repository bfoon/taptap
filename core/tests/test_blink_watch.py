"""Why the internet blinks (core/blink_watch.py) and the Internet Doctor v3 fix (core/wan_doctor.py)."""
from django.test import SimpleTestCase

from core import blink_watch as bw, nettools as nt, wan_doctor as wd


class FakeRes:
    def __init__(self, rows):
        self.rows = rows

    def get(self, **_):
        return list(self.rows)

    def add(self, **kw):
        row = {'id': f'*{len(self.rows) + 1}', **{k.replace('_', '-'): v for k, v in kw.items()}}
        self.rows.append(row)
        return row['id']

    def remove(self, id):
        self.rows[:] = [r for r in self.rows if r.get('id') != id]


class FakeSvc:
    def __init__(self, tables):
        self.t = {k: FakeRes(v) for k, v in tables.items()}

    def resource(self, path):
        return self.t.setdefault(path, FakeRes([]))

    def safe_get(self, path, **_):
        return self.resource(path).get()


class DoctorV3Tests(SimpleTestCase):
    def setUp(self):
        self.src = wd.render_source([{'interface': 'ether1', 'type': 'dhcp', 'gateway': '', 'label': 'Starlink'}])

    def test_silent_modem_is_not_a_broken_line(self):
        self.assertEqual(wd.VERSION, '4')                         # routers on older versions are upgraded automatically
        self.assertIn(':set silent true', self.src)
        self.assertNotIn('count=2] = 0) do={ :return ("down,"', self.src)
        self.assertIn(':if ($silent) do={ :return ("down," . $gw) }', self.src)
        # a DNS lookup through the line counts when ping is blocked
        self.assertIn(':resolve domain-name=\\"www.google.com\\" server=', self.src)

    def test_dhcp_renewed_only_without_address(self):
        self.assertIn('"unbound,"', self.src)
        self.assertIn('($st = "unbound") && ($kind = "dhcp")', self.src)
        self.assertNotIn('($st = "down") && ($kind = "dhcp")', self.src)

    def test_watch_installed_with_the_doctor(self):
        rsc = wd.render_install_rsc([{'interface': 'ether1', 'type': 'dhcp', 'gateway': '', 'label': 'S'}])
        self.assertIn('name=TapTap-Watch interval=5s', rsc)
        self.assertIn('TapTap Watch', wd.link_body({'step': 'watch'}))

    def test_live_check_silent_gateway_but_internet_works(self):
        from unittest import mock
        svc = FakeSvc({'/ip/dhcp-client': [{'interface': 'ether1', 'status': 'bound', 'gateway': '192.168.1.1', 'disabled': 'false'}],
                       '/ip/route': [], '/interface/ethernet': []})
        line = wd._assign_probes([{'interface': 'ether1', 'type': 'dhcp', 'gateway': '', 'label': 'Fibre'}])[0]
        with mock.patch.object(wd, '_ping', side_effect=lambda s, a, **kw: {'ok': a != '192.168.1.1'}):
            row = wd.check_line(svc, line)
        self.assertEqual(row['state'], 'ok')
        self.assertIn('does not answer ping', row['finding'])


class BlinkTests(SimpleTestCase):
    def hist(self, *rows):
        return ';'.join(rows)

    def test_causes_are_lined_up_with_the_log(self):
        hist = self.hist('2026-11-20 18:02:11|15|cpu 12% routes 192.168.1.1%ether1 |192.168.1.1%ether1 ',
                         '2026-11-20 19:30:05|10|cpu 15% routes 192.168.1.1%ether1 |192.168.1.1%ether1 ',
                         '2026-11-20 20:10:00|20|cpu 96% routes 192.168.1.1%ether1 |192.168.1.1%ether1 ',
                         '2026-11-20 21:00:00|30|cpu 10% routes 192.168.1.1%ether1 |10.0.0.1%ether2 ')
        log = [('18:02:09', 'interface,info', 'ether3 link down'), ('18:02:20', 'interface,info', 'ether3 link up'),
               ('19:30:04', 'interface,info', 'ether1 link down'), ('19:30:12', 'interface,info', 'ether1 link up')]
        r = bw.blinks_result(log, hist, {'wans': ['ether1', 'ether2'], 'uptime': '3d'})
        by = {b['at'][-8:]: b['cause'] for b in r['blinks']}
        self.assertEqual(by['18:02:11'], 'lan_port')                   # the AP chain on ether3
        self.assertEqual(by['19:30:05'], 'wan_port')                   # the modem's port
        self.assertEqual(by['20:10:00'], 'cpu')
        self.assertEqual(by['21:00:00'], 'switch')                     # moved to the other line
        self.assertEqual(r['count'], 4)
        self.assertEqual(r['total_secs'], 75)
        self.assertEqual({f['interface'] for f in r['flaps']}, {'ether1', 'ether3'})

    def test_nothing_local_means_the_provider(self):
        r = bw.blinks_result([], self.hist('2026-11-20 10:00:00|10|cpu 5% routes 192.168.1.1%ether1 |192.168.1.1%ether1 '), {'wans': ['ether1']})
        self.assertEqual(r['blinks'][0]['cause'], 'isp')
        self.assertIn('provider', r['title'])

    def test_power_cut_and_mass_logout(self):
        log = [('09:00:30', 'system,error,critical', 'router was rebooted without proper shutdown, probably power outage')]
        log += [('12:00:%02d' % i, 'hotspot,info,debug', f'U{i} (10.5.50.{i}): logged out: keepalive timeout') for i in range(6)]
        r = bw.blinks_result(log, '', {'wans': []}, 'on')
        keys = {c['key'] for c in r['causes']}
        self.assertIn('reboot', keys)
        self.assertTrue(any(e['key'] == 'logout' for e in r['timeline']))
        self.assertIn('power', r['timeline'][-1]['text'].lower() + ' '.join(e['text'].lower() for e in r['timeline']))

    def test_new_watch_and_install_once(self):
        r = bw.blinks_result([], '', {'wans': []}, 'new')
        self.assertEqual(r['title'], 'Watching for blinks now')
        svc = FakeSvc({})
        self.assertEqual(bw.install_api(svc), 'new')
        self.assertEqual(bw.install_api(svc), 'on')
        self.assertEqual(len(svc.resource('/system/script').get()), 1)
        # the watch's own "started" line after an install is not a restart; after a reboot it is
        self.assertEqual(bw._events([('10:00:00', 'script,warning', 'TapTap Watch: started (router uptime 2d03:00:00)')], set()), [])
        self.assertEqual(bw._events([('10:00:00', 'script,warning', 'TapTap Watch: started (router uptime 00:00:41)')], set())[0]['key'], 'reboot')

    def test_link_round_trip(self):
        body = bw.link_body()
        self.assertIn('TapTap-Watch', body)
        self.assertIn('/log print as-value where', body)
        up = ('watch=on\nhist=2026-11-20 18:02:11|15|cpu 12% routes 192.168.1.1%ether1 |192.168.1.1%ether1 ;\n'
              'up=1d02:00:00\ncpu=9\nwan=ether1\nl=18:02:10|interface,info|ether1 link down\n').encode()
        res = nt.parse_link('blinks', {}, up)
        self.assertEqual(res['count'], 1)
        self.assertEqual(res['blinks'][0]['cause'], 'wan_port')
        self.assertEqual(res['uptime'], '1d02:00:00')
        self.assertEqual(nt.clean_params('blinks', {})['target'], 'short internet drops')
