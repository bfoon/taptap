"""Drag & Drop Configuration Studio: set up, change and manage a DHCP server.

Run:  DB_ENGINE=sqlite python manage.py test core.test_dhcp_designer
"""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import control_designer as cd
from .models import Business, Router, RouterConfigSnapshot, RouterInterface


class FakeResource:
    """In-memory RouterOS menu that behaves like routeros_api (underscores become dashes)."""
    def __init__(self, rows):
        self.rows = rows

    @staticmethod
    def _k(d):
        return {k.replace('_', '-'): str(v) for k, v in d.items()}

    def get(self, **find):
        f = self._k(find)
        return [dict(r) for r in self.rows if all(str(r.get(k, '')) == v for k, v in f.items())]

    def add(self, **kw):
        row = self._k(kw); row['id'] = f'*{len(self.rows) + 100}'; self.rows.append(row)

    def set(self, id=None, **kw):
        for r in self.rows:
            if r.get('id') == id:
                r.update(self._k(kw))

    def remove(self, id=None):
        self.rows[:] = [r for r in self.rows if r.get('id') != id]


class FakeService:
    def __init__(self, db):
        self.db = db

    def resource(self, path):
        return FakeResource(self.db.setdefault(path, []))

    def close(self):
        pass


SECTIONS = {
    'IP addresses': {'rows': [{'address': '192.168.88.1/24', 'interface': 'bridge1'}, {'address': '10.0.0.2/24', 'interface': 'ether1'}]},
    'DHCP servers': {'rows': [{'name': 'dhcp1', 'interface': 'bridge1', 'address-pool': 'dhcp_pool0', 'lease-time': '30m', 'disabled': 'false'}]},
    'IP pools': {'rows': [{'name': 'dhcp_pool0', 'ranges': '192.168.88.10-192.168.88.254'}]},
    'DHCP networks': {'rows': [{'address': '192.168.88.0/24', 'gateway': '192.168.88.1', 'dns-server': '192.168.88.1'}]},
    'DHCP leases': {'rows': [{'address': '192.168.88.20', 'mac-address': 'AA:BB:CC:00:00:01', 'server': 'dhcp1', 'dynamic': 'true', 'host-name': 'phone'}]},
}


class PlanTests(TestCase):
    def test_server_plan_and_validation(self):
        plan = cd.build_plan('dhcp_server', 'bridge2', {'gateway': '192.168.50.1/24', 'pool_start': '192.168.50.20',
                                                        'pool_end': '192.168.50.200', 'lease_time': '2h'})
        self.assertEqual([op['resource'] for op in plan], ['/ip/address', '/ip/pool', '/ip/dhcp-server', '/ip/dhcp-server/network'])
        self.assertEqual(plan[1]['values']['ranges'], '192.168.50.20-192.168.50.200')
        self.assertEqual(plan[2]['values']['name'], 'dhcp-bridge2'); self.assertEqual(plan[2]['values']['address_pool'], 'pool-bridge2')
        self.assertEqual(plan[3]['values']['dns_server'], '192.168.50.1')           # blank DNS = the router
        bad = [
            ({'gateway': '192.168.50.1', 'pool_start': '192.168.50.2', 'pool_end': '192.168.50.9'}, 'prefix'),
            ({'gateway': '192.168.50.1/24', 'pool_start': '192.168.50.1', 'pool_end': '192.168.50.9'}, "router's own address"),
            ({'gateway': '192.168.50.1/24', 'pool_start': '192.168.60.2', 'pool_end': '192.168.50.9'}, 'not a usable address'),
            ({'gateway': '192.168.50.1/24', 'pool_start': '192.168.50.90', 'pool_end': '192.168.50.9'}, 'must come before'),
            ({'gateway': '192.168.50.1/24', 'pool_start': '192.168.50.2', 'pool_end': '192.168.50.9', 'lease_time': 'soon'}, 'Lease time'),
            ({'gateway': '192.168.50.1/24', 'pool_start': '192.168.50.2', 'pool_end': '192.168.50.9', 'lease_time': '10s'}, 'between 1 minute'),
        ]
        for params, msg in bad:
            with self.assertRaisesRegex(ValueError, msg):
                cd.build_plan('dhcp_server', 'bridge2', {'lease_time': '1h', **params})

    def test_lease_plan_and_link_script(self):
        plan = cd.build_plan('dhcp_lease', 'bridge1', {'mac': 'aa-bb-cc-00-00-01', 'address': '192.168.88.50', 'server': 'dhcp1'})
        self.assertEqual(plan[0]['find'], {'mac_address': 'AA:BB:CC:00:00:01', 'dynamic': 'true'})
        script = cd.link_script(plan)
        self.assertIn('dynamic=yes', script)                                   # a boolean literal, not the string "true"
        self.assertIn('/ip/dhcp-server/lease add mac-address="AA:BB:CC:00:00:01"', script)
        with self.assertRaisesRegex(ValueError, 'MAC'):
            cd.build_plan('dhcp_lease', 'bridge1', {'mac': 'nope', 'address': '192.168.88.50'})

    def test_apply_and_roll_back_on_a_router(self):
        db = {'/ip/address': [{'id': '*1', 'interface': 'bridge1', 'address': '192.168.88.1/24'}],
              '/ip/pool': [{'id': '*2', 'name': 'dhcp_pool0', 'ranges': '192.168.88.10-192.168.88.254'}],
              '/ip/dhcp-server': [{'id': '*3', 'name': 'dhcp1', 'interface': 'bridge1', 'address-pool': 'dhcp_pool0', 'lease-time': '30m', 'disabled': 'false'}],
              '/ip/dhcp-server/network': [{'id': '*4', 'address': '192.168.88.0/24', 'gateway': '192.168.88.1', 'dns-server': '192.168.88.1'}],
              '/ip/dhcp-server/lease': [{'id': '*5', 'mac-address': 'AA:BB:CC:00:00:01', 'dynamic': 'true', 'address': '192.168.88.20'}]}
        svc = FakeService(db)
        # Change the existing server: narrower range, longer lease, other DNS. Names kept.
        plan = cd.build_plan('dhcp_server', 'bridge1', {'gateway': '192.168.88.1/24', 'pool_start': '192.168.88.100', 'pool_end': '192.168.88.200',
                                                        'lease_time': '1d', 'dns': '1.1.1.1', 'server': 'dhcp1', 'pool': 'dhcp_pool0'})
        before = cd.snapshot_plan(svc, plan); rollback = cd._rollback_from(before, plan)
        cd.apply_direct(svc, plan)
        self.assertEqual(len(db['/ip/dhcp-server']), 1)
        self.assertEqual(db['/ip/dhcp-server'][0]['lease-time'], '1d')
        self.assertEqual(db['/ip/pool'][0]['ranges'], '192.168.88.100-192.168.88.200')
        self.assertEqual(db['/ip/dhcp-server/network'][0]['dns-server'], '1.1.1.1')
        cd.apply_direct(svc, rollback)                                          # rollback restores every value
        self.assertEqual(db['/ip/dhcp-server'][0]['lease-time'], '30m')
        self.assertEqual(db['/ip/pool'][0]['ranges'], '192.168.88.10-192.168.88.254')
        # Fixed IP: the temporary lease goes, a static one is created; rollback removes only what TapTap added.
        plan = cd.build_plan('dhcp_lease', 'bridge1', {'mac': 'AA:BB:CC:00:00:01', 'address': '192.168.88.50', 'server': 'dhcp1'})
        before = cd.snapshot_plan(svc, plan); rollback = cd._rollback_from(before, plan)
        cd.apply_direct(svc, plan)
        self.assertEqual([(r['address'], r.get('dynamic')) for r in db['/ip/dhcp-server/lease']], [('192.168.88.50', None)])
        self.assertFalse(any(x['action'] == 'manual_restore' for x in rollback))
        cd.apply_direct(svc, rollback)
        self.assertEqual(db['/ip/dhcp-server/lease'], [])


@override_settings(AUTH_EMAIL_OTP=False)
class RouterTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('o', 'o@x.gm', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='TapTap K', owner_name='B', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=self.b, name='TapTap K', ip_address='10.0.0.1', username='u', password='p', status='Online')
        RouterInterface.objects.create(router=self.r, name='bridge1', interface_type='bridge')
        RouterInterface.objects.create(router=self.r, name='ether1', interface_type='ether')
        RouterInterface.objects.create(router=self.r, name='ether2', interface_type='ether', raw_data={'bridge_port': {'bridge': 'bridge1'}})
        RouterInterface.objects.create(router=self.r, name='ether5', interface_type='ether')
        RouterConfigSnapshot.objects.create(router=self.r, sections=SECTIONS,
                                            load_balancing={'wan_links': [{'interface': 'ether1', 'state': 'active'}]})
        self.client.force_login(self.owner)

    def preview(self, recipe, target, **params):
        return self.client.post(reverse('control_designer_preview', args=[self.r.id]),
                                json.dumps({'recipe': recipe, 'target': target, 'parameters': params}), content_type='application/json').json()

    def test_current_settings_prefill_the_blocks(self):
        d = self.client.get(reverse('control_designer_inspect', args=[self.r.id]), {'scope': 'port', 'target': 'bridge1'}).json()
        adj = {a['recipe']: a['parameters'] for a in d['adjustments']}
        self.assertEqual(adj['dhcp_server'], {'gateway': '192.168.88.1/24', 'pool_start': '192.168.88.10', 'pool_end': '192.168.88.254',
                                              'dns': '', 'lease_time': '30m', 'enabled': 'yes', 'server': 'dhcp1', 'pool': 'dhcp_pool0'})
        self.assertEqual(adj['dhcp_lease']['server'], 'dhcp1')
        self.assertEqual(adj['dhcp_remove'], {'network': '192.168.88.0/24', 'pool': 'dhcp_pool0'})
        self.assertNotIn('bridge_member', adj)                      # a bridge is not put in a bridge
        titles = [s['title'] for s in d['sections']]
        self.assertIn('DHCP leases (1)', titles); self.assertIn('DHCP address pool', titles); self.assertIn('DHCP network', titles)
        # A port inside the bridge gets no DHCP blocks; a free port gets "set up a server" (with a suggestion from its address, if any).
        d = self.client.get(reverse('control_designer_inspect', args=[self.r.id]), {'scope': 'port', 'target': 'ether2'}).json()
        self.assertNotIn('dhcp_server', {a['recipe'] for a in d['adjustments']})
        d = self.client.get(reverse('control_designer_inspect', args=[self.r.id]), {'scope': 'port', 'target': 'ether5'}).json()
        self.assertEqual([a['recipe'] for a in d['adjustments']][0], 'dhcp_server')

    def test_checks_against_the_router(self):
        ok = self.preview('dhcp_server', 'bridge1', gateway='192.168.88.1/24', pool_start='192.168.88.50', pool_end='192.168.88.99', lease_time='1h')
        self.assertTrue(ok['success'], ok)
        # blank names keep the existing server's own names (no accidental rename / orphaned pool)
        self.assertEqual(ok['plan'][2]['values']['name'], 'dhcp1'); self.assertEqual(ok['plan'][1]['find']['name'], 'dhcp_pool0')
        cases = [
            (('dhcp_server', 'ether2'), dict(gateway='192.168.90.1/24', pool_start='192.168.90.10', pool_end='192.168.90.20'), 'part of bridge1'),
            (('dhcp_server', 'ether1'), dict(gateway='192.168.90.1/24', pool_start='192.168.90.10', pool_end='192.168.90.20'), 'Internet \\(WAN\\) port'),
            (('dhcp_server', 'ether5'), dict(gateway='192.168.88.5/24', pool_start='192.168.88.10', pool_end='192.168.88.20'), 'already on bridge1'),
            (('dhcp_server', 'ether5'), dict(gateway='192.168.90.1/24', pool_start='192.168.90.10', pool_end='192.168.90.20', server='dhcp1'), 'already runs on bridge1'),
            (('dhcp_lease', 'bridge1'), dict(mac='AA:BB:CC:00:00:09', address='10.9.9.9'), "not in this server's network"),
            (('dhcp_lease', 'bridge1'), dict(mac='AA:BB:CC:00:00:09', address='192.168.88.9', server='nope'), 'no DHCP server called'),
            (('dhcp_remove', 'ether5'), dict(), 'no DHCP server on ether5'),
        ]
        for (recipe, target), params, msg in cases:
            d = self.preview(recipe, target, lease_time='1h', **params)
            self.assertFalse(d['success'], (recipe, target, d)); self.assertRegex(d['message'], msg)
        # A lease with no server name uses the server on that target.
        d = self.preview('dhcp_lease', 'bridge1', mac='AA:BB:CC:00:00:09', address='192.168.88.9')
        self.assertTrue(d['success'], d); self.assertEqual(d['plan'][1]['values']['server'], 'dhcp1')

    def test_apply_through_direct_api_and_remove_needs_apply_word(self):
        db = {'/ip/dhcp-server': [{'id': '*3', 'name': 'dhcp1', 'interface': 'bridge1', 'address-pool': 'dhcp_pool0', 'lease-time': '30m'}]}
        fake = FakeService(db)
        conn = mock.MagicMock(); conn.connect.return_value = fake
        with mock.patch('core.mikrotik.MikroTikService', return_value=conn):
            r = self.client.post(reverse('control_designer_apply', args=[self.r.id]), json.dumps({
                'recipe': 'dhcp_server', 'target': 'bridge1',
                'parameters': {'gateway': '192.168.88.1/24', 'pool_start': '192.168.88.10', 'pool_end': '192.168.88.254', 'lease_time': '8h'}}),
                content_type='application/json').json()
        self.assertTrue(r['success'], r)
        self.assertEqual(db['/ip/dhcp-server'][0]['lease-time'], '8h'); self.assertEqual(db['/ip/dhcp-server'][0]['name'], 'dhcp1')
        r = self.client.post(reverse('control_designer_apply', args=[self.r.id]), json.dumps({
            'recipe': 'dhcp_remove', 'target': 'bridge1', 'parameters': {'network': '192.168.88.0/24', 'pool': 'dhcp_pool0'}}),
            content_type='application/json').json()
        self.assertFalse(r['success']); self.assertIn('APPLY', r['message'])
