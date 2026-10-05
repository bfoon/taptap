"""Every app page loads the responsive safety net, so no page scrolls sideways or crops on a phone.

Run:  DB_ENGINE=sqlite python manage.py test core.test_responsive
"""
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

CSS = Path(settings.BASE_DIR) / 'static' / 'css'


@override_settings(AUTH_EMAIL_OTP=False)
class ResponsiveTests(TestCase):
    def setUp(self):
        from .models import Business
        u = User.objects.create_user('m@x.com', 'm@x.com', 'pw')
        Business.objects.create(user=u, business_name='B', owner_name='O', phone='1', trial_ends_at=timezone.now() + timedelta(days=7))
        self.client.force_login(u)

    def test_app_pages_load_it_last(self):
        html = self.client.get(reverse('dashboard')).content.decode()
        self.assertIn('css/responsive.css', html)
        self.assertGreater(html.index('css/responsive.css'), html.index('css/app.css'))      # after the page's own styles

    def test_key_rules(self):
        css = (CSS / 'responsive.css').read_text()
        self.assertIn('.topbar { flex-wrap: wrap;', css)                                     # top bar buttons wrap on phones
        self.assertIn('[class*="grid"] > * { min-width: 0; }', css)
        self.assertNotIn('.d-flex > * { min-width: 0', css)                                  # it squeezed toolbars on wide screens

    def test_long_real_data_rules(self):
        """Long emails, usernames and router names must not hold a box wider than a phone (found with long real data)."""
        css = (CSS / 'responsive.css').read_text()
        self.assertIn('overflow-wrap: anywhere', css)                         # names, emails, codes
        self.assertIn('.filter-bar select', css)                              # Reports filter dropdowns
        self.assertIn('.custom-range { display: flex; flex-wrap: wrap', css)  # date range + Apply
        self.assertIn('.lk-diag { flex-wrap: wrap', css)                      # TapTap Link diagram
        self.assertIn('.nx-save { margin-right: 64px; }', css)                # save bar clear of the chat bubble
