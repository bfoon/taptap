from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.utils import timezone

from .models import Business


class SecurityTabsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('o', 'o@x.com', 'pw12345678')
        Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1', trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.c = Client(); self.c.force_login(self.owner)

    def test_four_tabs_and_everything_in_its_tab(self):
        html = self.c.get('/security/').content.decode()
        for t in ('tab-overview', 'tab-live', 'tab-access', 'tab-fair'):
            self.assertIn(f'id="{t}"', html)
        def pane(name):
            i = html.index(f'id="tab-{name}"'); j = html.find('class="sec-pane"', i + 10)
            return html[i:j if j > 0 else len(html)]
        self.assertIn('id="incidents"', pane('live'))
        self.assertIn('id="sharing"', pane('access')); self.assertIn('id="sticky"', pane('access'))
        self.assertIn('Voucher entry protection', pane('access'))          # injected card lands in Access control
        self.assertIn('id="fair-usage"', pane('fair'))
        self.assertIn('Rescan', html.split('id="tab-overview"')[0])           # the summary stays above the tabs
