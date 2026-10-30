"""Agents are reachable from Daily sales: a sub-item under Daily sales in the menus and a switch on both pages."""
import re
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from core.models import Agent, Business


class SalesToAgentsTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user('owner', 'o@x.gm', 'pw')
        self.biz = Business.objects.create(user=self.u, business_name='Kunda Net', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.agent = Agent.objects.create(business=self.biz, name='Fatou')
        Agent.objects.create(business=self.biz, name='Old', active=False)
        self.client.force_login(self.u)

    def page(self, url):
        r = self.client.get(url, follow=True)
        self.assertEqual(r.status_code, 200, url)
        return r.content.decode()

    def side(self, html):
        return re.search(r'<aside\b[^>]*sidebar.*?</aside>', html, re.S).group(0)

    def test_agents_right_under_daily_sales(self):
        from django.urls import reverse
        side = self.side(self.page('/vouchers/'))
        links = re.findall(r'<a\b[^>]*href="([^"]+)"', side)
        self.assertEqual(links[links.index(reverse('sales_daily')) + 1], '/finance/?tab=agents')

    def test_mobile_menu(self):
        html = self.page('/vouchers/')
        mobile = re.search(r'<div\b[^>]*class="[^"]*mobile-nav[^"]*"[^>]*>.*?</div>', html, re.S).group(0)
        self.assertIn('href="/finance/?tab=agents"', mobile)

    def test_switch_on_both_pages(self):
        from django.urls import reverse
        for url, on in ((reverse('sales_daily'), 'Daily sales'), ('/finance/?tab=agents', 'Agents')):
            sw = re.search(r'<nav class="tt-vm-switch tt-sa-switch".*?</nav>', self.page(url), re.S)
            self.assertIsNotNone(sw, url)
            sw = sw.group(0)
            self.assertRegex(sw, r'class="on"[^>]*aria-current="page"[^>]*>\s*<i[^>]*></i><span>' + on)
            self.assertRegex(sw, r'<span>Agents</span><b>1</b>')               # active agents only

    def test_highlighted_on_agent_pages(self):
        side = self.side(self.page(f'/finance/agents/{self.agent.pk}/benefits/'))
        self.assertRegex(side, r'href="/finance/\?tab=agents" class="tt-keep nav-sub active"')

    def test_hidden_when_agents_switched_off(self):
        self.biz.features_off = ['agents']; self.biz.save(update_fields=['features_off'])
        from django.urls import reverse
        html = self.page(reverse('sales_daily'))
        self.assertNotIn('tt-sa-switch', html)
        self.assertNotIn('?tab=agents', self.side(html))
