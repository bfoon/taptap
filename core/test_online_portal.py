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
