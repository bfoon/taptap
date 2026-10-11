"""Phone clock check on the sign-in pages (online and on the router).

Run:  DB_ENGINE=sqlite python manage.py test core.test_portal_clock
"""
import json
import re
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Business, PortalPage, Voucher
from .models_voucher_reports import VoucherReport


@override_settings(AUTH_EMAIL_OTP=False, TIME_ZONE='Africa/Banjul')
class PortalClockTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user('o', 'o@x.gm', 'pw12345678')
        self.b = Business.objects.create(user=owner, business_name='TapTap K', owner_name='B', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        from .views_studio import portal_template
        self.page = PortalPage.objects.create(business=self.b, name='Login', kind='login', slug='tt-login',
                                              config=portal_template('', 'login'), is_published=True, is_default=True)

    def test_time_endpoint(self):
        before = timezone.now().timestamp() * 1000
        r = self.client.get('/p/clock.json')
        self.assertEqual(r['Access-Control-Allow-Origin'], '*')            # the router's own page asks cross-site
        self.assertIn('no-store', r['Cache-Control'])
        self.assertAlmostEqual(r.json()['now'], before, delta=5000)

    def _ctx(self, html):
        return json.loads(re.search(r'TapPortal\.render\(document\.getElementById\(.tp.\), .*?, (\{.*\})\);</script>', html, re.S).group(1))

    def test_online_page_and_router_page_carry_the_clock(self):
        html = self.client.get(reverse('portal_public', args=['tt-login'])).content.decode()
        clk = self._ctx(html)['clock']
        self.assertEqual((clk['url'], clk['tz'], clk['zone'], clk['place']), ('/p/clock.json', 0, 'Banjul', 'in Banjul'))
        self.assertAlmostEqual(clk['server'], timezone.now().timestamp() * 1000, delta=10000)
        html = self.client.get(reverse('portal_online', args=['tt-login'])).content.decode()      # the online sign-in page
        self.assertIn('"clock"', html)
        # Router page: TapTap's address for the exact check, plus when the page was made for when TapTap is out of reach.
        from .views_studio import _export_html
        out = _export_html(self.page, base='https://taptap.example')
        ctx = json.loads(re.search(r'var ctx=(\{.*?\});\nctx\.mt=', out, re.S).group(1))
        self.assertEqual(ctx['clock']['url'], 'https://taptap.example/p/clock.json')
        self.assertIn('built', ctx['clock']); self.assertNotIn('server', ctx['clock'])
        self.assertIn('function wireClock', out)                                                 # the check is baked in
        self.assertIn('android.settings.DATE_SETTINGS', out)

    def test_report_tells_staff_about_the_clock(self):
        Voucher.objects.create(business=self.b, code='ABC123', plan_name='1 DAY')
        body = {'code': 'ABC123', 'phone': '7001234', 'error': '', 'clock': '10.8 years behind'}
        r = self.client.post(reverse('portal_online_report', args=['tt-login']), json.dumps(body), content_type='text/plain')
        self.assertTrue(r.json()['ok'])
        self.assertEqual(VoucherReport.objects.get().shown_error, 'Phone clock 10.8 years behind')
        body = {'code': 'ABC123', 'phone': '7001234', 'error': 'invalid password', 'clock': '2 h behind (time zone set to GMT+1, should be GMT)'}
        self.client.post(reverse('portal_online_report', args=['tt-login']), json.dumps(body), content_type='text/plain')
        self.assertEqual(VoucherReport.objects.latest('id').shown_error,
                         'invalid password · Phone clock 2 h behind (time zone set to GMT+1, should be GMT)')

    def test_routers_get_the_new_page_once(self):
        from unittest import mock
        from . import portal_deploy
        v1 = portal_deploy.version_for(self.b)
        self.assertEqual(v1, portal_deploy.version_for(self.b))                 # stable: no re-install loop
        with mock.patch.object(portal_deploy, '_renderer_hash', return_value='newrenderer'):
            self.assertNotEqual(v1, portal_deploy.version_for(self.b))          # a new renderer re-installs the pages
