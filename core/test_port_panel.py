"""Control Center front panel: physical ports on the panel, bridges / loopback / WireGuard… in RouterOS,
and what a bridge carries shown on its member ports.

Run:  DB_ENGINE=sqlite python manage.py test core.test_port_panel
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Business, Router, RouterConfigSnapshot, RouterInterface
from .port_panel import kind_of, panel

SECTIONS = {
    'IP addresses': {'rows': [{'address': '192.168.88.1/24', 'interface': 'bridge1'}, {'address': '10.9.0.2/24', 'interface': 'taptap-wg'}]},
    'DHCP servers': {'rows': [{'name': 'dhcp1', 'interface': 'bridge1'}]},
    'HotSpot servers': {'rows': [{'name': 'hs1', 'interface': 'bridge1'}]},
    'DHCP clients': {'rows': [{'interface': 'ether1'}]},
}


def make(router):
    def add(name, itype, bridge='', running=True, **raw):
        data = dict(raw)
        if bridge:
            data['bridge_port'] = {'bridge': bridge, 'pvid': '1'}
        return RouterInterface.objects.create(router=router, name=name, interface_type=itype, running=running, raw_data=data)
    add('bridge1', 'bridge'); add('lo', 'loopback'); add('taptap-wg', 'wg')
    add('ether1', 'ether')
    for n in ('ether2', 'ether3', 'ether4', 'ether10'):
        add(n, 'ether', bridge='bridge1')
    add('ether5', 'ether', running=False)
    add('wifi1', 'wifi', bridge='bridge1'); add('wifi2', 'wifi', bridge='bridge1', running=False)
    add('wifi3-guest', 'wifi', bridge='bridge1', **{'master-interface': 'wifi1'})   # a second SSID: virtual
    add('', '', )  # nameless junk row must not break anything


@override_settings(AUTH_EMAIL_OTP=False)
class PortPanelTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('o', 'o@x.gm', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='TapTap K', owner_name='B', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=self.b, name='TapTap K', ip_address='10.0.0.1', username='u', password='p', status='Online')
        make(self.r)
        self.snap = RouterConfigSnapshot.objects.create(router=self.r, sections=SECTIONS, load_balancing={})
        self.client.force_login(self.owner)

    def test_split_and_order(self):
        ports, virtual = panel(list(self.r.interfaces.exclude(name='')), self.snap)
        self.assertEqual([p.name for p in ports], ['ether1', 'ether2', 'ether3', 'ether4', 'ether5', 'ether10', 'wifi1', 'wifi2'])
        self.assertEqual([v.name for v in virtual], ['bridge1', 'taptap-wg', 'wifi3-guest', 'lo'])
        by = {i.name: i for i in ports + virtual}
        self.assertEqual(by['bridge1'].ui_members, ['ether2', 'ether3', 'ether4', 'ether10', 'wifi1', 'wifi2', 'wifi3-guest'])
        # What is configured on bridge1 shows on its ports, marked as coming from bridge1.
        self.assertEqual(by['ether3'].ui_bridge, 'bridge1')
        self.assertEqual([l for _, l in by['ether3'].ui_via], ['192.168.88.1/24', 'DHCP server', 'HotSpot'])
        self.assertEqual([l for _, l in by['ether1'].ui_tags], ['DHCP client'])
        self.assertEqual((by['ether1'].ui_bridge, by['ether1'].ui_via), ('', []))
        self.assertEqual((by['taptap-wg'].ui_kind_label, by['lo'].ui_kind_label), ('WireGuard', 'Loopback'))

    def test_kind_falls_back_to_name_when_type_unknown(self):
        cases = {'ether7': 'eth', 'sfp-sfpplus1': 'eth', 'wlan1': 'wifi', 'vlan20': 'virtual'}
        for name, want in cases.items():
            i = RouterInterface(router=self.r, name=name, interface_type='')
            self.assertEqual(kind_of(i), want, name)

    def test_page_and_inspector(self):
        html = self.client.get(reverse('router_control', args=[self.r.id])).content.decode()
        panel_html = html.split('class="port-bank"')[1].split('router-footer')[0]
        self.assertNotIn('data-target="bridge1"', panel_html)            # not drawn as a port…
        self.assertNotIn('data-target="lo"', panel_html)
        self.assertIn('class="vif vif-bridge', html)                     # …but as a RouterOS chip, still a drop target
        self.assertIn('data-scope="port" data-target="bridge1"', html)
        self.assertIn('data-via="bridge1"', panel_html)                  # ports show their bridge
        url = reverse('control_designer_inspect', args=[self.r.id])
        d = self.client.get(url, {'scope': 'port', 'target': 'ether3'}).json()
        titles = [s['title'] for s in d['sections']]
        self.assertIn('From bridge1: IP addresses', titles); self.assertIn('From bridge1: DHCP server', titles)
        self.assertIn('From bridge1: HotSpot server', titles)
        d = self.client.get(url, {'scope': 'port', 'target': 'bridge1'}).json()
        self.assertEqual(d['sections'][1]['title'], 'Ports in bridge1')
        self.assertEqual([m['port'] for m in d['sections'][1]['rows']], ['ether2', 'ether3', 'ether4', 'ether10', 'wifi1', 'wifi2', 'wifi3-guest'])
        self.assertIn('ether10', d['summary']['ports_in_it'])
