from datetime import time, timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.utils import timezone

from . import app_control as ac
from .agent import command_body
from .models import Business, Router
from .models_apps import AppRule, AppControlState


class AppControlTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=self.b, name='Kotu', ip_address='1.1.1.1', username='a', password='b', connection_mode='agent')
        self.c = Client(); self.c.force_login(self.owner)

    def test_block_rule_compiles_to_filter_rules_with_time(self):
        AppRule.objects.create(business=self.b, name='No TikTok at night', services=['tiktok'], action='block', when='window',
                               from_time=time(22, 0), to_time=time(6, 0), days=['mon', 'tue'])
        spec = ac.compile_spec(self.r)
        times = {row.get('time') for row in spec['filter']}
        self.assertEqual(times, {'22:00:00-23:59:59,mon,tue', '00:00:00-06:00:00,mon,tue'})     # overnight split in two
        self.assertTrue(any(row.get('tls-host') == '*tiktok.com' and row['action'] == 'drop' and row.get('hotspot') == 'auth' for row in spec['filter']))
        self.assertTrue(any(row.get('protocol') == 'udp' and row.get('dst-port') == '443' for row in spec['filter']))   # QUIC
        self.assertIn(('TT-APP-%d' % AppRule.objects.get().pk, 'tiktok.com'), spec['lists'])

    def test_slow_rule_marks_and_shapes_per_device(self):
        AppRule.objects.create(business=self.b, name='Slow YouTube', services=['youtube'], action='slow', down_mbps=1, up_mbps=0.5)
        spec = ac.compile_spec(self.r)
        self.assertEqual({q['pcq-rate'] for q in spec['qtypes']}, {'1000k', '500k'})
        self.assertTrue(any(m['action'] == 'mark-connection' and m.get('tls-host') == '*googlevideo.com' for m in spec['mangle']))
        self.assertTrue(any(g.get('connection-mark') for g in spec['guard']))          # kept out of FastTrack

    def test_allow_comes_first_and_peak_hours(self):
        self.b.peak_from, self.b.peak_to = time(18, 0), time(23, 0); self.b.save()
        AppRule.objects.create(business=self.b, name='Block social', services=['facebook'], action='block', when='peak')
        AppRule.objects.create(business=self.b, name='Allow WhatsApp', services=['whatsapp'], action='allow')
        spec = ac.compile_spec(self.r)
        self.assertEqual(spec['filter'][0]['action'], 'accept')
        self.assertTrue(all(r.get('time') == '18:00:00-23:00:00' for r in spec['filter'] if r['action'] == 'drop'))

    def test_temporary_rule_starts_and_ends(self):
        now = timezone.now()
        AppRule.objects.create(business=self.b, name='Exam week', services=['games'], action='block',
                               starts_at=now + timedelta(hours=1), ends_at=now + timedelta(hours=3))
        self.assertEqual(ac.compile_spec(self.r, now)['filter'], [])
        self.assertTrue(ac.compile_spec(self.r, now + timedelta(hours=2))['filter'])
        self.assertEqual(ac.compile_spec(self.r, now + timedelta(hours=4))['filter'], [])

    def test_link_script_is_escaped_and_pushed_once(self):
        AppRule.objects.create(business=self.b, name='Evil "name" $x', services=[], custom_domains=['example.com'], action='block')
        from types import SimpleNamespace as S
        body = command_body(S(kind='app_control', params={'spec': ac.compile_spec(self.r)}))
        self.assertIn('tls-host="*example.com"', body)
        self.assertIn('\\"name\\" \\$x', body)
        with mock.patch('core.linkops.send') as send:
            ac.push(self.r); ac.push(self.r)
            self.assertEqual(send.call_count, 1)           # unchanged: not sent again
        self.assertEqual(AppControlState.objects.get(router=self.r).status, 'queued')

    def test_page_and_save(self):
        self.assertEqual(self.c.get('/traffic/apps/').status_code, 200)
        with mock.patch('core.linkops.send'):
            self.c.post('/traffic/apps/save/', {'services': ['youtube', 'tiktok'], 'custom_domains': 'bet9ja.com, not a domain', 'action': 'slow',
                                                'down_mbps': '2', 'up_mbps': '1', 'when': 'window', 'from_time': '18:00', 'to_time': '23:00',
                                                'days': ['mon', 'fri'], 'span': '3h', 'scope': 'customers', 'block_quic': 'on', 'enabled': 'on'})
        r = AppRule.objects.get()
        self.assertEqual((r.services, r.custom_domains, r.action, str(r.down_mbps), r.days), (['youtube', 'tiktok'], ['bet9ja.com'], 'slow', '2.00', ['mon', 'fri']))
        self.assertTrue(r.ends_at and r.ends_at > timezone.now())
        self.assertContains(self.c.get('/traffic/apps/'), 'Slow down YouTube, TikTok')


class ApiApplyTests(TestCase):
    def test_direct_api_replaces_only_taptap_rules(self):
        owner = User.objects.create_user('o2', 'o2@x.com', 'pw12345678')
        b = Business.objects.create(user=owner, business_name='K', owner_name='A', phone='1', trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        r = Router.objects.create(business=b, name='R', ip_address='1.1.1.1', username='a', password='b')
        AppRule.objects.create(business=b, name='Block games', services=['games'], action='block')
        class Res:
            def __init__(s, rows): s.rows = rows
            def get(s, **k): return list(s.rows)
            def add(s, place_before=None, **k):
                row = {kk.replace('_', '-'): v for kk, v in k.items()}; row['id'] = f'*{len(s.rows) + 100}'
                i = next((n for n, x in enumerate(s.rows) if x['id'] == place_before), len(s.rows)); s.rows.insert(i, row)
            def remove(s, id): s.rows = [x for x in s.rows if x['id'] != id]
        res = {'/ip/firewall/filter': Res([{'id': '*1', 'action': 'fasttrack-connection', 'comment': 'defconf'}, {'id': '*2', 'action': 'drop', 'comment': 'TT-APP old'}]),
               '/ip/firewall/mangle': Res([]), '/queue/tree': Res([]), '/queue/type': Res([]), '/ip/firewall/address-list': Res([])}
        class Svc:
            def resource(s, p): return res[p]
        ac._apply_api(Svc(), ac.compile_spec(r))
        rows = res['/ip/firewall/filter'].rows
        self.assertNotIn('*2', [x['id'] for x in rows])                       # old TapTap rule replaced
        self.assertEqual(rows[-1]['comment'], 'defconf')                      # TapTap rules sit above the router's own
        self.assertTrue(all(x['comment'].startswith('TT-APP') for x in rows[:-1]))
