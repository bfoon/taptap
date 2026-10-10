"""Online sign-in by QR, customers sending a voucher to staff, clear voucher messages, sleeping iPhones.

Run:  DB_ENGINE=sqlite python manage.py test core.test_online_portal
"""
import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import online_portal as op
from .models import Business, PortalPage, Voucher, VoucherDeviceBinding
from .models_voucher_reports import VoucherReport


@override_settings(AUTH_EMAIL_OTP=False, SITE_URL='https://taptapnetwork.com')
class OnlinePortalTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o', 'o@x.gm', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='Kairaba Net', owner_name='A', phone='2207771234',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True, hotspot_dns_name='login.wifi')
        self.page = PortalPage.objects.create(business=self.b, name='Login', kind='login', slug='kairaba-login')
        now = timezone.now()
        self.ok = Voucher.objects.create(business=self.b, code='GOOD12', plan_name='24 HOURS', status='active', max_devices=1,
                                         used_at=now - timedelta(hours=2), expires_at=now + timedelta(hours=22))
        self.old = Voucher.objects.create(business=self.b, code='OLD123', plan_name='24 HOURS', status='active',
                                          used_at=now - timedelta(days=3), expires_at=now - timedelta(days=2))
        self.client.force_login(self.owner)

    # ── iPhones asleep ──
    def test_sleeping_phone_is_not_logged_out_after_two_minutes(self):
        from . import sticky
        self.b.sticky_sessions = False
        self.assertEqual(sticky.profile_values(self.b)['keepalive-timeout'], '20m')
        self.b.sticky_sessions = True
        self.assertEqual(sticky.profile_values(self.b)['keepalive-timeout'], '2h')
        self.assertEqual(sticky.profile_values(self.b)['add-mac-cookie'], 'yes')

    def test_taptap_is_reachable_before_login(self):
        from .free_access import _desired
        self.assertEqual(_desired(self.b)[0]['pattern'], 'taptapnetwork.com')

    # ── online sign-in ──
    def test_online_page(self):
        r = self.client.get(reverse('portal_online', args=['kairaba-login']) + '?code=GOOD12')
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        for text in ('Kairaba Net Wi-Fi', "HOST='login.wifi'", '/p/kairaba-login/check/', '/p/kairaba-login/report/', 'value="GOOD12"',
                     'Send it to us'):
            self.assertIn(text, html, text)
        self.assertEqual(self.client.get(reverse('portal_online', args=['nope'])).status_code, 404)
        qr = self.client.get(reverse('portal_qr', args=[self.page.pk])).content.decode()
        self.assertIn('https://taptapnetwork.com/p/kairaba-login/go/', qr); self.assertIn('vendor/qrcode.js', qr)

    def test_expired_message_says_when(self):
        r = self.client.post(reverse('portal_check', args=['kairaba-login']), json.dumps({'code': 'OLD123'}), content_type='application/json')
        j = r.json()
        self.assertEqual((r.status_code, j['success']), (403, False))
        self.assertTrue('finished' in j['message'] or 'expired on' in j['message'], j['message'])   # never a bare "error"

    # ── diagnosis ──
    def test_diagnosis(self):
        self.assertEqual(op.diagnose(self.b, 'NOPE')['state'], 'unknown')
        d = op.diagnose(self.b, 'old123')
        self.assertEqual(d['state'], 'expired'); self.assertIn('expired on', d['customer'])
        self.assertEqual(op.diagnose(self.b, 'GOOD12')['state'], 'ok')
        VoucherDeviceBinding.objects.create(business=self.b, voucher=self.ok, slot_no=1, current_mac='AA:BB:CC:DD:EE:01', label='iPhone XR')
        d = op.diagnose(self.b, 'GOOD12')
        self.assertEqual(d['state'], 'slots_full'); self.assertIn('the most it allows', d['customer'])
        self.assertTrue(any('iPhone XR' in t for _, t in d['checks']))
        Voucher.objects.filter(pk=self.ok.pk).update(status='disabled')
        self.assertEqual(op.diagnose(self.b, 'GOOD12')['state'], 'disabled')
        Voucher.objects.filter(pk=self.ok.pk).update(frozen_at=timezone.now(), freeze_reason='Travelling')
        d = op.diagnose(self.b, 'GOOD12')
        self.assertEqual(d['state'], 'paused'); self.assertIn('Travelling', d['customer'])

    # ── customer reports ──
    def test_customer_sends_a_voucher_to_staff(self):
        url = reverse('portal_online_report', args=['kairaba-login'])
        self.client.logout()
        self.assertEqual(self.client.post(url, json.dumps({'code': 'OLD123'}), content_type='application/json').status_code, 400)  # phone needed
        r = self.client.post(url, json.dumps({'code': 'OLD123', 'name': 'Awa', 'phone': '+220 777 1111', 'message': 'Not working',
                                              'error': 'Voucher expired: ...', 'mac': 'aa:bb:cc:dd:ee:ff'}), content_type='application/json')
        j = r.json()
        self.assertEqual((r.status_code, j['title']), (200, 'Expired'))
        rep = VoucherReport.objects.get()
        self.assertEqual((rep.code, rep.voucher_id, rep.phone, rep.mac, rep.diagnosis['state']), ('OLD123', self.old.pk, '+220 777 1111', 'AA:BB:CC:DD:EE:FF', 'expired'))
        from .models import Notification
        self.assertTrue(Notification.objects.filter(business=self.b, event='voucher_report').exists())
        for _ in range(5):
            self.client.post(url, json.dumps({'code': 'X', 'phone': '1'}), content_type='application/json')
        self.assertEqual(self.client.post(url, json.dumps({'code': 'X', 'phone': '1'}), content_type='application/json').status_code, 429)

    def test_staff_inbox(self):
        rep = VoucherReport.objects.create(business=self.b, code='OLD123', voucher=self.old, phone='1', diagnosis={'state': 'ok', 'title': 'Fine'})
        html = self.client.get(reverse('voucher_reports')).content.decode()
        self.assertIn('OLD123', html); self.assertIn('Expired', html); self.assertIn('When sent: Fine', html)   # checked again now
        self.client.post(reverse('voucher_reports'), {'report': rep.pk, 'action': 'resolve', 'note': 'Sold a new voucher'})
        rep.refresh_from_db(); self.assertEqual((rep.status, rep.staff_note), ('resolved', 'Sold a new voucher'))
        self.assertNotIn('OLD123', self.client.get(reverse('voucher_reports')).content.decode())
        self.assertIn('Sold a new voucher', self.client.get(reverse('voucher_reports') + '?show=all').content.decode())
        other = User.objects.create_user('x', 'x@x.gm', 'pw12345678')
        Business.objects.create(user=other, business_name='X', owner_name='X', phone='1', trial_ends_at=timezone.now() + timedelta(days=9))
        self.client.force_login(other)
        self.assertNotIn('OLD123', self.client.get(reverse('voucher_reports') + '?show=all').content.decode())
        self.assertEqual(self.client.get(reverse('portal_qr', args=[self.page.pk])).status_code, 404)


@override_settings(AUTH_EMAIL_OTP=False, SITE_URL='https://taptapnetwork.com')
class QuickLoginLinkTests(TestCase):
    """Voucher QR / WhatsApp "Quick login": <login address>/login?username=CODE&password=CODE. When the login address is
    the online portal, that is /p/<slug>/login — it used to answer "Not Found"."""

    def setUp(self):
        owner = User.objects.create_user('o', 'o@x.gm', 'pw12345678')
        self.b = Business.objects.create(user=owner, business_name='TapTap KerrSering', owner_name='A', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True,
                                         hotspot_url='https://taptapnetwork.com/p/taptap-kerrsering-login-8d89')
        PortalPage.objects.create(business=self.b, name='Login', kind='login', slug='taptap-kerrsering-login-8d89')
        self.client.force_login(owner)

    def test_the_link_on_the_card_opens_and_signs_in(self):
        r = self.client.get('/p/taptap-kerrsering-login-8d89/login?username=G2UZG&password=G2UZG')
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn('value="G2UZG"', html)
        self.assertIn("setTimeout(function(){$('signin').click();},400)", html)          # signs in by itself
        self.assertNotIn('document.querySelector(\'[data-t="member"]\').click()', html)  # a voucher, not a member

    def test_member_link_and_no_parameters(self):
        html = self.client.get('/p/taptap-kerrsering-login-8d89/login/?username=jimmy&password=s3cret').content.decode()
        self.assertIn('document.querySelector(\'[data-t="member"]\').click()', html); self.assertIn("$('pw').value='s3cret'", html)
        plain = self.client.get('/p/taptap-kerrsering-login-8d89/login').content.decode()
        self.assertNotIn("$('signin').click();},400", plain)
        self.assertEqual(self.client.get('/p/no-such-portal/login?username=A&password=A').status_code, 404)

    def test_voucher_card_link_resolves(self):
        import re
        v = Voucher.objects.create(business=self.b, code='G2UZG', plan_name='Zero voucher', status='active')
        html = self.client.get(reverse('voucher_card', args=[v.pk])).content.decode()
        link = re.search(r'Quick login: (\S+)', html).group(1).replace('&amp;', '&')
        self.assertEqual(link, 'https://taptapnetwork.com/p/taptap-kerrsering-login-8d89/login?username=G2UZG&password=G2UZG')
        path = link.split('taptapnetwork.com', 1)[1]
        self.assertEqual(self.client.get(path).status_code, 200)


@override_settings(AUTH_EMAIL_OTP=False, SITE_URL='https://taptapnetwork.com')
class RouterLoginAddressTests(TestCase):
    """The phone is sent to the router's hotspot IP — "login.wifi" fails with Private DNS or when the name was never set."""

    def setUp(self):
        from .models import Router, RouterConfigSnapshot
        owner = User.objects.create_user('o', 'o@x.gm', 'pw12345678')
        self.b = Business.objects.create(user=owner, business_name='TapTap KerrSering', owner_name='A', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True, hotspot_dns_name='login.wifi')
        PortalPage.objects.create(business=self.b, name='Login', kind='login', slug='kk-login')
        self.k = Router.objects.create(business=self.b, name='TapTap K', ip_address='41.223.1.10', username='u', password='p')
        RouterConfigSnapshot.objects.create(router=self.k, sections={
            'HotSpot servers': {'rows': [{'name': 'hs1', 'interface': 'bridge-hotspot', 'profile': 'hsprof1', 'disabled': 'false'},
                                         {'name': 'old', 'interface': 'ether5', 'profile': 'default', 'disabled': 'true'}]},
            'HotSpot server profiles': {'rows': [{'name': 'hsprof1', 'hotspot-address': '', 'dns-name': ''}]},
            'IP addresses': {'rows': [{'address': '10.5.50.1/24', 'interface': 'bridge-hotspot'}, {'address': '192.168.88.1/24', 'interface': 'ether5'}]}})
        self.client.force_login(owner)

    def snap(self, router, ip):
        from .models import RouterConfigSnapshot
        RouterConfigSnapshot.objects.create(router=router, sections={
            'HotSpot servers': {'rows': [{'name': 'hs1', 'interface': 'bridge', 'profile': 'p1'}]},
            'HotSpot server profiles': {'rows': [{'name': 'p1', 'hotspot-address': ip}]}, 'IP addresses': {'rows': []}})

    def test_ip_from_the_configuration(self):
        self.assertEqual(op.hotspot_ips(self.k), ['10.5.50.1'])          # interface address; the disabled server is ignored
        from .models import Router
        b2 = Router.objects.create(business=self.b, name='Brikama', ip_address='41.223.9.9', username='u', password='p')
        self.snap(b2, '172.16.0.1')
        self.assertEqual(op.hotspot_ips(b2), ['172.16.0.1'])             # profile hotspot-address wins

    def test_page_sends_the_phone_to_the_ip(self):
        html = self.client.get('/p/kk-login/login?username=G2UZG&password=G2UZG').content.decode()
        self.assertIn("HOST='10.5.50.1'", html)                          # only router → its IP, not login.wifi

    def test_choosing_the_router(self):
        from .models import Router
        b2 = Router.objects.create(business=self.b, name='Brikama', ip_address='41.223.9.9', username='u', password='p')
        self.snap(b2, '172.16.0.1')
        # two routers, different networks: the phone's public address picks one
        html = self.client.get('/p/kk-login/go/', REMOTE_ADDR='41.223.9.9').content.decode()
        self.assertIn("HOST='172.16.0.1'", html)
        html = self.client.get('/p/kk-login/go/?r=%d' % self.k.pk, REMOTE_ADDR='8.8.8.8').content.decode()
        self.assertIn("HOST='10.5.50.1'", html)                          # a router-specific QR
        html = self.client.get('/p/kk-login/go/', REMOTE_ADDR='8.8.8.8').content.decode()
        self.assertIn("HOST='login.wifi'", html)                         # cannot tell which: the easy name
        qr = self.client.get(reverse('portal_qr', args=[PortalPage.objects.get().pk]) + '?r=%d' % b2.pk).content.decode()
        self.assertIn('/p/kk-login/go/?r=%d' % b2.pk, qr); self.assertIn('172.16.0.1', qr)

    def test_routers_sharing_one_ip(self):
        from .models import Router
        b2 = Router.objects.create(business=self.b, name='Brikama', ip_address='41.223.9.9', username='u', password='p')
        self.snap(b2, '10.5.50.1')
        html = self.client.get('/p/kk-login/go/', REMOTE_ADDR='8.8.8.8').content.decode()
        self.assertIn("HOST='10.5.50.1'", html)


@override_settings(AUTH_EMAIL_OTP=False, SITE_URL='https://taptapnetwork.com')
class PrepareRoutersTests(TestCase):
    def setUp(self):
        from .models import Router
        owner = User.objects.create_user('o', 'o@x.gm', 'pw12345678')
        self.b = Business.objects.create(user=owner, business_name='K', owner_name='A', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.api = Router.objects.create(business=self.b, name='A-api', ip_address='10.0.0.1', username='u', password='p')
        self.link = Router.objects.create(business=self.b, name='B-link', ip_address='', username='u', password='p', connection_mode='agent')
        self.client.force_login(owner)

    def test_script(self):
        from .test_link_install import routeros_balanced
        s = op.setup_script('taptapnetwork.com')
        self.assertTrue(routeros_balanced(s))
        self.assertIn('/ip hotspot walled-garden ip add dst-host="taptapnetwork.com" action=accept', s)   # HTTPS before login
        self.assertIn('login-by=($s . "http-pap")', s)                                                 # added, never replaced
        self.assertIn(':if ([:typeof [:find $s "http-pap"]] = "nil")', s)

    def test_prepare_all_routers(self):
        from unittest import mock
        from .models import AgentCommand
        scripts = []
        chan = lambda r: 'TapTap Link' if r.connection_mode == 'agent' else 'Direct API'
        with mock.patch('core.voucher_history.channel', side_effect=chan), mock.patch('core.linkops.ensure_online'), \
                mock.patch('core.mikrotik.MikroTikService') as svc, \
                mock.patch('core.portctl.schedule_on_router', side_effect=lambda s, n, d, script: scripts.append(script)):
            svc.return_value.connect.return_value = svc.return_value
            j = self.client.post(reverse('portal_online_prepare')).json()
        self.assertEqual([(x['router'], x['ok']) for x in j['results']], [('A-api', True), ('B-link', True)])
        self.assertIn('http-pap', scripts[0])
        cmd = AgentCommand.objects.get(kind='online_signin')
        from .agent import queue, wrap
        self.assertIn('taptapnetwork.com', wrap(cmd, 'https://taptapnetwork.com', 'no'))
        with self.assertRaises(ValueError):
            queue(self.link, 'online_signin', {'host': 'x"; /system reset'})
