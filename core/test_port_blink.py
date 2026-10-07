"""Control Center: flash a port's lights on the real router so a technician can find it.

Run:  DB_ENGINE=sqlite python manage.py test core.test_port_blink
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import port_blink as pb
from .models import AgentCommand, Business, Router, RouterInterface
from .test_link_install import routeros_balanced


class SyncThread:
    def __init__(self, target, args, daemon=True): self.target, self.args = target, args
    def start(self): self.target(*self.args)


@override_settings(AUTH_EMAIL_OTP=False)
class PortBlinkTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('p@x.com', 'p@x.com', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='B', owner_name='O', phone='1', trial_ends_at=timezone.now() + timedelta(days=7))
        self.r = Router.objects.create(business=self.biz, name='hAP', ip_address='10.0.0.1', username='u', password='p', status='Online')
        for n in ('ether3', 'sfp1', 'wlan1'):
            RouterInterface.objects.create(router=self.r, name=n)
        self.client.force_login(self.owner)

    def test_api_blinks_that_port(self):
        svc = mock.MagicMock(); svc.__enter__.return_value = svc
        eth = svc.resource.return_value; eth.get.return_value = [{'id': '*3', 'name': 'ether3'}]
        with mock.patch('core.mikrotik.MikroTikService', return_value=svc), mock.patch('core.voucher_history.channel', return_value='Direct API'), \
                mock.patch('core.port_blink.threading.Thread', SyncThread):
            msg = pb.blink(self.r, 'ether3', self.owner)
        svc.resource.assert_called_with('/interface/ethernet')
        eth.call.assert_called_once_with('blink', {'.id': '*3', 'duration': '15s'})
        self.assertIn('ether3 is flashing', msg)

    def test_taptap_link(self):
        from .agent import command_body, queue
        with mock.patch('core.voucher_history.channel', return_value='TapTap Link'), mock.patch('core.linkops.ensure_online'):
            msg = pb.blink(self.r, 'sfp1', self.owner)
        cmd = AgentCommand.objects.get(kind='port_blink')
        self.assertEqual(command_body(cmd), '/interface ethernet blink [find where name="sfp1"] duration=15s')
        self.assertTrue(routeros_balanced(command_body(cmd)))
        self.assertIn('next checks in', msg)
        for bad in ({'port': 'wlan1', 'seconds': 15}, {'port': 'ether3"; /system reset', 'seconds': 15}, {'port': 'ether3', 'seconds': 600}):
            with self.assertRaises(ValueError, msg=bad):
                queue(self.r, 'port_blink', bad)

    def test_only_ports_with_lights(self):
        for name in ('wlan1', 'ether9', ''):
            with self.assertRaises(ValueError, msg=name):
                pb.blink(self.r, name)

    def test_page_and_endpoint(self):
        r = self.client.get(reverse('router_control', args=[self.r.pk]))
        self.assertContains(r, f'data-blink-url="{reverse("port_blink", args=[self.r.pk])}"')
        with mock.patch('core.port_blink.blink', return_value='ether3 is flashing') as b:
            d = self.client.post(reverse('port_blink', args=[self.r.pk]), {'port': 'ether3'}).json()
        self.assertTrue(d['success']); b.assert_called_once()
        self.assertEqual(self.client.post(reverse('port_blink', args=[self.r.pk]), {'port': 'wlan1'}).status_code, 400)

    def test_only_network_managers(self):
        from .models_team import TeamMember
        u = User.objects.create_user('v@x.com', 'v@x.com', 'pw')
        TeamMember.objects.create(business=self.biz, user=u, role='voucher_creator')
        self.client.force_login(u)
        with mock.patch('core.port_blink.blink') as b:
            self.client.post(reverse('port_blink', args=[self.r.pk]), {'port': 'ether3'})
        b.assert_not_called()
