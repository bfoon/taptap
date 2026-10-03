"""Topology: which MikroTik is connected to which — suggestions, placing routers by hand, no loops.

Run:  DB_ENGINE=sqlite python manage.py test core.test_topology_links
"""
import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import topology_links as tl
from .models import Business, Router, RouterConfigSnapshot, RouterDevice, RouterInterface, RouterNeighbor, SiteRouter
from .netgraph import build_graph


@override_settings(AUTH_EMAIL_OTP=False)
class TopologyLinkTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('t@x.com', 't@x.com', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='B', owner_name='O', phone='1', trial_ends_at=timezone.now() + timedelta(days=7))
        self.a = Router.objects.create(business=self.biz, name='Serrekunda hAP', ip_address='10.0.0.1', username='u', password='p', status='Online')
        self.b = Router.objects.create(business=self.biz, name='Hotel CCR', ip_address='10.0.0.2', username='u', password='p', status='Online')
        RouterInterface.objects.create(router=self.b, name='ether1', mac_address='DC:2C:6E:00:00:01')
        self.client.force_login(self.owner)

    def seen_on_port(self):
        RouterDevice.objects.create(router=self.a, device_key='DC:2C:6E:00:00:01', mac_address='DC:2C:6E:00:00:01', ip_address='192.168.88.20',
                                    interface_name='bridge', is_online=True, raw_data={'bridge': {'on-interface': 'ether5'}})

    def post(self, **data):
        return self.client.post(reverse('topology_router_action'), json.dumps(data), content_type='application/json').json()

    # ── suggestions ──
    def test_network_card_seen_on_a_port(self):
        self.seen_on_port()
        s = tl.suggestions(self.biz)
        self.assertEqual((s[0]['child'], s[0]['parent'], s[0]['port']), (self.b, self.a, 'ether5'))
        self.assertIn('seen on Serrekunda hAP’s ether5', s[0]['reasons'][0])

    def test_default_gateway_and_neighbour_list(self):
        RouterConfigSnapshot.objects.create(router=self.a, sections={'IP addresses': {'rows': [{'address': '192.168.88.1/24'}]}})
        RouterConfigSnapshot.objects.create(router=self.b, sections={'Routes': {'rows': [{'dst-address': '0.0.0.0/0', 'gateway': '192.168.88.1'}]}})
        RouterNeighbor.objects.create(router=self.a, neighbor_key='n1', identity='Hotel CCR', interface_name='ether3')
        s = tl.suggestions(self.biz)[0]
        self.assertEqual((s['child'], s['parent'], s['port']), (self.b, self.a, 'ether3'))
        self.assertEqual(len(s['reasons']), 2)

    def test_ignored_and_placed_routers_are_not_suggested(self):
        self.seen_on_port()
        tl.ignore(self.b, self.a.pk)
        self.b.refresh_from_db()
        self.assertEqual(tl.suggestions(self.biz), [])
        Router.objects.filter(pk=self.b.pk).update(uplink_ignored=[], uplink_internet=True)
        self.assertEqual(tl.suggestions(self.biz), [])               # the owner decided

    # ── placing ──
    def test_place_under_a_port_and_no_loops(self):
        tl.place(self.b, f'router:{self.a.pk}', 'ether5')
        self.b.refresh_from_db()
        self.assertEqual(tl.parent_of(self.b), ('router', self.a.pk, 'ether5'))
        with self.assertRaises(ValueError):
            tl.place(self.a, f'router:{self.b.pk}', 'ether2')       # A under B under A
        with self.assertRaises(ValueError):
            tl.place(self.b, f'router:{self.b.pk}')                 # under itself
        with self.assertRaises(ValueError):
            tl.place(self.b, f'router:{self.a.pk}', 'bad port;')
        tl.place(self.b, 'auto'); self.b.refresh_from_db()
        self.assertEqual(tl.parent_of(self.b), ('', None, ''))

    def test_place_under_a_tplink_and_loop_through_it(self):
        site = SiteRouter.objects.create(business=self.biz, router=self.a, port='ether2', mac_address='14:CC:20:00:00:01', name='Archer')
        tl.place(self.b, f'site:{site.pk}')
        self.b.refresh_from_db()
        self.assertEqual(tl.parent_of(self.b), ('site', site.pk, ''))
        with self.assertRaises(ValueError):
            tl.site_place_check(self.biz, site, ('router', self.b.pk))   # the Archer under the CCR that hangs from the Archer

    # ── the map ──
    def test_map_follows_the_placement(self):
        tl.place(self.b, f'router:{self.a.pk}', 'ether5')
        g = build_graph(self.biz)
        into_b = [e for e in g['edges'] if e['target'] == f'router:{self.b.pk}']
        self.assertEqual(len(into_b), 1)
        self.assertEqual((into_b[0]['source'], into_b[0]['iface'], into_b[0]['router_id']), (f'router:{self.a.pk}', 'ether5', self.a.pk))
        self.assertIn('ether5', g['live_interfaces'][str(self.a.pk)])          # its link animates with ether5's traffic

    def test_map_under_a_tplink(self):
        SiteRouter.objects.create(business=self.biz, router=self.a, port='ether2', mac_address='14:CC:20:00:00:01', name='Archer')
        RouterDevice.objects.create(router=self.a, device_key='14:CC:20:00:00:01', mac_address='14:CC:20:00:00:01', interface_name='bridge',
                                    is_online=True, raw_data={'bridge': {'on-interface': 'ether2'}})
        site = SiteRouter.objects.get(name='Archer')
        tl.place(self.b, f'site:{site.pk}')
        g = build_graph(self.biz)
        self.assertTrue(any(e['source'] == f'site:sr:{site.pk}' and e['target'] == f'router:{self.b.pk}' for e in g['edges']))

    # ── Detail tab ──
    def test_detail_actions(self):
        self.seen_on_port()
        d = self.client.get(reverse('topology_routers')).json()
        self.assertEqual(d['links'][0]['child'], 'Hotel CCR')
        d = self.post(action='accept_link', router_id=self.b.pk, parent_id=self.a.pk, port='ether5')
        self.assertTrue(d['success'])
        self.assertEqual([r['uplink'] for r in d['routers'] if r['id'] == self.b.pk][0], {'kind': 'router', 'id': self.a.pk, 'port': 'ether5'})
        self.assertEqual(d['links'], [])
        bad = self.post(action='place_router', router_id=self.a.pk, target=f'router:{self.b.pk}')
        self.assertFalse(bad['success']); self.assertIn('loop', bad['message'])
        self.assertTrue(self.post(action='place_router', router_id=self.b.pk, target='internet')['success'])
        self.assertFalse(self.post(action='place_router', router_id=999, target='internet').get('success'))

    def test_ignore_from_the_page(self):
        self.seen_on_port()
        self.assertEqual(self.post(action='ignore_link', router_id=self.b.pk, parent_id=self.a.pk)['links'], [])

    def test_page_has_the_editing_dialogs(self):
        r = self.client.get(reverse('topology'))
        self.assertContains(r, 'id="tdUplink"'); self.assertContains(r, 'id="tdPortAsk"'); self.assertContains(r, 'id="tdLinks"')
