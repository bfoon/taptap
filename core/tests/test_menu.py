"""The menu: find / pin / fold script loaded, Network split in two, the removed module gone, add-on links intact."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Business
from core.models_team import TeamMember


class Menu(TestCase):
    def setUp(self):
        self.u = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456')
        self.b = Business.objects.create(user=self.u, business_name='B', owner_name='O', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), feature_tier='pro')

    def menu(self, user):
        self.client.force_login(user)
        html = self.client.get(reverse('support'), follow=True).content.decode()
        start = html.index('<aside class="sidebar')
        return html, html[start:html.index('</aside>', start)]

    def test_owner_menu(self):
        html, side = self.menu(self.u)
        self.assertIn('js/nav.js', html)
        self.assertIn('css/nav.css', html)
        self.assertIn('<div class="nav-label">Network</div>', side)
        self.assertIn('<div class="nav-label">Watch &amp; protect</div>', side)
        self.assertLess(side.index('Network tools'), side.index('Watch &amp;'))
        self.assertLess(side.index('Watch &amp;'), side.index('> Traffic</a>'))
        self.assertNotIn('Other routers', html)
        # links other add-ons hang their own links on are unchanged
        self.assertIn('<a href="/finance/"><i class="bi bi-cash-stack"></i> Finance</a>', side)
        self.assertIn('<a href="/support/"><i class="bi bi-headset"></i> Support</a>', side)
        self.assertIn('<div class="nav-label">Business</div>', side)

    def test_staff_without_network_rights_sees_no_empty_sections(self):
        staff = User.objects.create_user('s@x.gm', 's@x.gm', 'pw-123456')
        TeamMember.objects.create(business=self.b, user=staff, role='cashier')
        _, side = self.menu(staff)
        self.assertNotIn('Watch &amp; protect', side)
