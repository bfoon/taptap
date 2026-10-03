"""Security → Protection: DDoS and IDS/IPS on the MikroTik.

Run:  DB_ENGINE=sqlite python manage.py test core.test_protection
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import protection as pr
from .models import AgentCommand, Business, Router, RouterConfigSnapshot
from .test_link_install import routeros_balanced


class Res:
    def __init__(self, rows, log): self.rows, self.log = rows, log
    def get(self, **f): return [r for r in self.rows if all(str(r.get(k)) == str(v) for k, v in f.items())]
    def add(self, **kw):
        row = {k.replace('_', '-'): v for k, v in kw.items()}
        row['id'] = f'*{len(self.rows) + 100}'
        before = row.pop('place-before', None)
        if before:
            i = next(i for i, r in enumerate(self.rows) if r['id'] == before)
            self.rows.insert(i, row)
        else:
            self.rows.append(row)
        return row['id']
    def remove(self, id): self.rows[:] = [r for r in self.rows if r['id'] != id]
    def call(self, cmd, args): self.log.append((cmd, args))


class FakeSvc:
    def __init__(self):
        self.store = {'/ip/firewall/filter': [{'id': '*1', 'chain': 'input', 'action': 'accept', 'connection-state': 'established,related', 'comment': 'owner rule'},
                                              {'id': '*2', 'chain': 'forward', 'action': 'fasttrack-connection', 'comment': 'owner fasttrack'}],
                      '/ip/firewall/address-list': [{'id': '*a', 'list': 'owner-list', 'address': '1.2.3.4'}]}
        self.calls = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def resource(self, path): return Res(self.store.setdefault(path, []), self.calls)
    def safe_get(self, path): return list(self.store.get(path, []))


@override_settings(AUTH_EMAIL_OTP=False, SITE_URL='https://taptap.example')
class ProtectionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('p@x.com', 'p@x.com', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='B', owner_name='O', phone='1', trial_ends_at=timezone.now() + timedelta(days=7))
        self.r = Router.objects.create(business=self.biz, name='hAP', ip_address='10.0.0.1', username='u', password='p', status='Online')
        self.client.force_login(self.owner)
        self.trusted = mock.patch('core.protection.trusted_addresses', return_value=['203.0.113.10', '10.99.0.0/16'])
        self.trusted.start(); self.addCleanup(self.trusted.stop)

    def run_api(self, svc, feature, action):
        with mock.patch('core.mikrotik.MikroTikService', return_value=svc), mock.patch('core.voucher_history.channel', return_value='Direct API'):
            return pr.apply(self.r, feature, action, self.owner)

    def test_rules_are_safe_by_construction(self):
        for feature in pr.FEATURES:
            rs = [f for _, f, _ in pr.rules(feature)]
            chains = {f['chain'] for f in rs} - {'input', 'forward'}
            for ch in chains:                                   # TapTap itself skips every check, first thing
                first = next(f for f in rs if f['chain'] == ch)
                self.assertEqual((first['action'], first.get('src-address-list')), ('return', 'taptap-trusted'), ch)
            jumps = [f for f in rs if f['chain'] in ('input', 'forward')]
            self.assertTrue(jumps and all(f['action'] == 'jump' and 'established' not in f.get('connection-state', '') for f in jumps))
            self.assertTrue(all(f['comment'].startswith(pr.FEATURES[feature]['tag']) for f in rs))

    def test_enable_puts_jumps_on_top_and_trusts_taptap(self):
        svc = FakeSvc()
        msg = self.run_api(svc, 'ips', 'enable')
        filt = svc.store['/ip/firewall/filter']
        inp = [r for r in filt if r['chain'] == 'input']
        fwd = [r for r in filt if r['chain'] == 'forward']
        self.assertEqual(inp[0]['jump-target'], 'taptap-ips')            # before the owner's rules
        self.assertEqual(fwd[0]['jump-target'], 'taptap-ips-fwd')
        trusted = {r['address'] for r in svc.store['/ip/firewall/address-list'] if r['list'] == 'taptap-trusted'}
        self.assertEqual(trusted, {'203.0.113.10', '10.99.0.0/16'})
        self.assertIn('is on', msg)
        self.run_api(svc, 'ips', 'enable')                                 # twice: no duplicates
        self.assertEqual(sum(1 for r in svc.store['/ip/firewall/filter'] if str(r.get('comment', '')).startswith('TapTap IPS')), len(pr.rules('ips')))

    def test_ddos_switches_on_syn_cookies(self):
        svc = FakeSvc()
        self.run_api(svc, 'ddos', 'enable')
        self.assertIn(('set', {'tcp_syncookies': 'yes'}), svc.calls)
        self.assertEqual([r for r in svc.store['/ip/firewall/filter'] if r['chain'] == 'input'][0]['jump-target'], 'taptap-ddos')

    def test_disable_removes_only_taptaps_rules_and_lists(self):
        svc = FakeSvc()
        self.run_api(svc, 'ips', 'enable')
        svc.store['/ip/firewall/address-list'] += [{'id': '*b1', 'list': 'taptap-ips-blocked', 'address': '198.51.100.7'}]
        self.run_api(svc, 'ips', 'disable')
        self.assertEqual([r['comment'] for r in svc.store['/ip/firewall/filter']], ['owner rule', 'owner fasttrack'])
        lists = {r['list'] for r in svc.store['/ip/firewall/address-list']}
        self.assertIn('owner-list', lists); self.assertNotIn('taptap-ips-blocked', lists)

    def test_unblock_empties_the_block_list_only(self):
        svc = FakeSvc()
        self.run_api(svc, 'ddos', 'enable')
        svc.store['/ip/firewall/address-list'].append({'id': '*b2', 'list': 'taptap-ddos-blocked', 'address': '198.51.100.8'})
        self.run_api(svc, 'ddos', 'unblock')
        self.assertFalse([r for r in svc.store['/ip/firewall/address-list'] if r['list'] == 'taptap-ddos-blocked'])
        self.assertTrue(any(str(r.get('comment', '')).startswith('TapTap DDoS') for r in svc.store['/ip/firewall/filter']))

    def test_status_and_blocked_count(self):
        svc = FakeSvc()
        self.run_api(svc, 'ips', 'enable')
        svc.store['/ip/firewall/address-list'].append({'id': '*b3', 'list': 'taptap-ips-blocked', 'address': '198.51.100.9'})
        snap = RouterConfigSnapshot.objects.get(router=self.r)
        snap.sections['Firewall address lists'] = {'rows': svc.store['/ip/firewall/address-list']}; snap.save()
        st = pr.status(self.r)
        self.assertEqual((st['ips']['on'], st['ips']['blocked'], st['ddos']['on']), (True, 1, False))

    def test_taptap_link_script(self):
        from .agent import command_body, queue
        with mock.patch('core.voucher_history.channel', return_value='TapTap Link'), mock.patch('core.linkops.ensure_online'):
            pr.apply(self.r, 'ips', 'enable', self.owner)
        cmd = AgentCommand.objects.get(kind='protection')
        body = command_body(cmd)
        self.assertTrue(routeros_balanced(body))
        self.assertIn('place-before=[:pick $top0 0]', body); self.assertIn('place-before=[:pick $top1 0]', body)
        self.assertIn('list="taptap-trusted" address="203.0.113.10"', body)
        self.assertIn('psd=21,3s,3,1', body)
        self.assertIn('pending', str(pr.status(self.r)['ips']) )
        for action in ('disable', 'unblock'):
            self.assertTrue(routeros_balanced(pr.link_script('ddos', action)))
        with self.assertRaises(ValueError):
            queue(self.r, 'protection', {'feature': 'everything', 'action': 'enable'})

    def test_security_page_and_buttons(self):
        r = self.client.get(reverse('security'))
        self.assertContains(r, 'Protection — DDoS and intrusion prevention')
        self.assertContains(r, 'value="enable"')
        svc = FakeSvc()
        with mock.patch('core.mikrotik.MikroTikService', return_value=svc), mock.patch('core.voucher_history.channel', return_value='Direct API'):
            r = self.client.post(reverse('security_protection', args=[self.r.pk]), {'feature': 'ddos', 'action': 'enable'})
        self.assertRedirects(r, '/security/#protection', fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse('security')), 'value="disable"')

    def test_only_network_managers(self):
        from .models_team import TeamMember
        u = User.objects.create_user('c@x.com', 'c@x.com', 'pw')
        TeamMember.objects.create(business=self.biz, user=u, role='voucher_creator')
        self.client.force_login(u)
        with mock.patch('core.protection.apply') as ap:
            self.client.post(reverse('security_protection', args=[self.r.pk]), {'feature': 'ddos', 'action': 'enable'})
        ap.assert_not_called()


class HotspotSafeProtectionTests(TestCase):
    """Phones on the login page must never be caught by DDoS / scan checks (they lost the login page for an hour)."""
    def test_rules_leave_hotspot_customers_alone(self):
        from .protection import rules
        ddos_jump = rules('ddos')[0][1]
        self.assertEqual(ddos_jump['hotspot'], '!from-client')
        jumps = [f for _, f, top in rules('ips') if top]
        self.assertEqual({j['chain'] for j in jumps}, {'input', 'forward'})
        for j in jumps:
            # either not a hotspot client, a logged-in customer, or only the management ports
            self.assertTrue(j['hotspot'] in ('!from-client', 'auth') or j.get('dst-port') == '21,22,23,8291,8728,8729', j)
        self.assertFalse(any(j['hotspot'] == 'from-client' and 'dst-port' not in j for j in jumps))

    def test_link_script_has_the_new_matchers(self):
        from .protection import link_script
        s = link_script('ips', 'enable')
        self.assertIn('hotspot=!from-client', s); self.assertIn('hotspot=auth', s)
        self.assertIn('(v2)', s)


class ProtectionUpgradeTests(TestCase):
    def test_old_rules_are_reapplied_once(self):
        from django.core.cache import cache
        cache.clear()
        u = User.objects.create_user('pu', 'pu@x.com', 'pw12345678')
        b = Business.objects.create(user=u, business_name='K', owner_name='A', phone='1', trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        r = Router.objects.create(business=b, name='R', ip_address='1.1.1.1', username='a', password='b')
        RouterConfigSnapshot.objects.create(router=r, sections={'Firewall filter': {'rows': [
            {'chain': 'input', 'comment': 'TapTap IPS: inspect new connections to the router'}]}})
        self.assertEqual(pr.status(r)['outdated'], ['ips'])
        with mock.patch('core.protection.apply', return_value='ok') as ap:
            self.assertEqual(pr.upgrade(r), ['ips']); self.assertEqual(pr.upgrade(r), [])
        self.assertEqual(ap.call_args.args[1:], ('ips', 'enable'))
