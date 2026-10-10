"""WAN provider names: ether1 → Gamtel, shown everywhere the load-balancing view goes.

Run:  DB_ENGINE=sqlite python manage.py test core.test_wan_names
"""
import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Business, Router, RouterConfigSnapshot, RouterInterface, RouterInterfaceRole
from .models_team import TeamMember
from .wan_names import apply_names, named, set_name

LB = {'method': 'PCC', 'warnings': ['Down: ether3'], 'wan_links': [
    {'id': 'w1', 'interface': 'ether1', 'label': 'ether1', 'state': 'active', 'expected_share': 50},
    {'id': 'w3', 'interface': 'ether3', 'label': 'ether3', 'state': 'down', 'expected_share': 50}]}


@override_settings(AUTH_EMAIL_OTP=False)
class WanNameTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('o', 'o@x.gm', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='TapTap K', owner_name='B', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=self.b, name='TapTap K', ip_address='10.0.0.1', username='u', password='p')
        for n in ('ether1', 'ether3'):
            RouterInterface.objects.create(router=self.r, name=n)
        RouterConfigSnapshot.objects.create(router=self.r, sections={}, load_balancing=LB)
        self.client.force_login(self.owner)

    def test_apply_names_keeps_interface_and_rewrites_down_warning(self):
        out = apply_names(LB, {'ether3': 'QCell'})
        link = out['wan_links'][1]
        self.assertEqual((link['label'], link['isp'], link['interface']), ('QCell', 'QCell', 'ether3'))
        self.assertEqual(out['warnings'], ['Down: QCell'])
        self.assertEqual(LB['wan_links'][1]['label'], 'ether3')            # the stored analysis is untouched

    def test_name_survives_a_role_change(self):
        RouterInterfaceRole.objects.create(router=self.r, interface_name='ether1', role='unused')
        set_name(self.r, 'ether1', '  Gamtel   fibre ')
        RouterInterfaceRole.objects.filter(router=self.r, interface_name='ether1').update(role='wan', label='')
        self.assertEqual(named(self.r, LB)['wan_links'][0]['label'], 'Gamtel fibre')
        # A port with no role row yet gets one marked WAN, not "unused".
        set_name(self.r, 'ether3', 'Starlink')
        self.assertEqual(RouterInterfaceRole.objects.get(router=self.r, interface_name='ether3').role, 'wan')

    def test_endpoint_and_pages(self):
        url = reverse('router_wan_name', args=[self.r.id])
        r = self.client.post(url, json.dumps({'interface': 'ether1', 'name': 'Gamtel'}), content_type='application/json')
        self.assertEqual(r.json(), {'success': True, 'interface': 'ether1', 'name': 'Gamtel', 'label': 'Gamtel'})
        bad = self.client.post(url, json.dumps({'interface': 'ether9', 'name': 'X'}), content_type='application/json')
        self.assertEqual(bad.status_code, 400)
        # Router Control and Topology hand the name (and the rename URL) to the load-balancing card.
        html = self.client.get(reverse('router_control', args=[self.r.id])).content.decode()
        self.assertIn('"label": "Gamtel"', html); self.assertIn('"name_url"', html); self.assertIn('Starlink', html)
        self.client.get(reverse('topology'))
        # The Map's WAN node uses it too.
        from .netgraph import build_graph
        wan = [n for n in build_graph(self.b)['nodes'] if n['type'] == 'wan' and n['iface'] == 'ether1'][0]
        self.assertEqual((wan['label'], wan['isp']), ('Gamtel', 'Gamtel'))
        # Clearing goes back to the port name.
        self.client.post(url, json.dumps({'interface': 'ether1', 'name': ''}), content_type='application/json')
        self.assertEqual(named(self.r, LB)['wan_links'][0]['label'], 'ether1')

    def test_staff_without_network_rights_cannot_rename(self):
        u = User.objects.create_user('v', 'v@x.gm', 'pw12345678')
        TeamMember.objects.create(business=self.b, user=u, role='viewer')
        self.client.force_login(u)
        r = self.client.post(reverse('router_wan_name', args=[self.r.id]), json.dumps({'interface': 'ether1', 'name': 'Hack'}),
                             content_type='application/json')
        self.assertNotEqual(r.status_code, 200)
        self.assertFalse(RouterInterfaceRole.objects.filter(isp_name='Hack').exists())
