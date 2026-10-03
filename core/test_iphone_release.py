"""iPhones after a voucher ends: the phone must be freed (session, cookie, host, Wi-Fi) using every MAC TapTap knows,
so it reconnects and the login sheet opens — even when the router already dropped the session itself."""
from datetime import timedelta
from types import SimpleNamespace as S
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from . import expiry
from .agent import command_body, queue
from .models import Business, DeviceAppUsage, Router, Voucher


class IphoneReleaseTests(TestCase):
    def setUp(self):
        cache.clear()
        u = User.objects.create_user('o', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=u, business_name='K', owner_name='A', phone='1', trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=self.b, name='R', ip_address='1.1.1.1', username='a', password='b')
        self.v = Voucher.objects.create(business=self.b, router=self.r, code='IPHONE01', plan_name='1 Hour', duration_minutes=60,
                                        used_at=timezone.now() - timedelta(hours=2), status='expired')

    def test_macs_from_traffic_and_last_reading(self):
        DeviceAppUsage.objects.create(business=self.b, router=self.r, hour=timezone.now().replace(minute=0, second=0, microsecond=0),
                                      mac='aa:bb:cc:00:00:01', ip='10.5.50.9', username='IPHONE01', app='YouTube', category='Video', domain='x', download=1)
        cache.set(f'tt:tr:users:{self.r.pk}', {'users': {'IPHONE01': [{'mac': '02:11:22:33:44:55'}]}}, 600)
        self.assertEqual(expiry.voucher_macs(self.v), ['02:11:22:33:44:55', 'AA:BB:CC:00:00:01'])

    def test_api_frees_phones_of_a_voucher_the_router_already_switched_off(self):
        cache.set(f'tt:tr:users:{self.r.pk}', {'users': {'IPHONE01': [{'mac': '02:11:22:33:44:55'}]}}, 600)
        svc = mock.Mock()
        svc.release_devices.return_value = 1
        users = [{'name': 'IPHONE01', 'disabled': 'true', 'limit-uptime': '1h', 'uptime': '1h'}]
        expiry.enforce_on_router(self.r, svc, users, [], timezone.now())
        self.assertEqual(svc.release_devices.call_args.args, ('IPHONE01', {'02:11:22:33:44:55'}))
        svc.release_devices.reset_mock()
        expiry.enforce_on_router(self.r, svc, users, [], timezone.now())
        self.assertFalse(svc.release_devices.called)                          # once, not every minute

    def test_link_command_frees_known_macs(self):
        body = command_body(S(kind='hotspot_users_disable', params={'names': ['IPHONE01'], 'disabled': True, 'reason': 'expired',
                                                                      'macs': ['02:11:22:33:44:55']}))
        self.assertIn('$drop m="02:11:22:33:44:55"', body)
        with self.assertRaises(ValueError):
            queue(self.r, 'hotspot_users_disable', {'names': ['IPHONE01'], 'disabled': True, 'macs': ['nope; /system reset']})

    def test_deploy_gives_the_hotspot_an_easy_address(self):
        from .portal_deploy import install_script, _dns_name
        self.assertEqual(_dns_name(self.b), 'login.wifi')
        s = install_script('https://taptapnetwork.com', 'T', ['login'], 'taptapnetwork.com', 'yes', 'login.wifi')
        self.assertIn('dns-name=""', s); self.assertIn('dns-name="login.wifi"', s); self.assertIn('!(login-by~"https")', s)
        self.b.hotspot_dns_name = 'bad name; x'; self.assertEqual(_dns_name(self.b), '')
