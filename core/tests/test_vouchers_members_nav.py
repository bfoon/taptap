"""From Vouchers you can always get to Members: a sub-item under Vouchers in the menus and a switch on both pages."""
import re
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from core.models import Business, Voucher


class VouchersToMembersTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user('owner', 'o@x.gm', 'pw')
        self.biz = Business.objects.create(user=self.u, business_name='Kunda Net', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        Voucher.objects.create(business=self.biz, code='ABC123', plan_name='1Day', source='taptap')
        Voucher.objects.create(business=self.biz, code='awa', password='secret', login_type='member', plan_name='Family', source='taptap')
        self.client.force_login(self.u)

    def page(self, url):
        r = self.client.get(url, follow=True)
        self.assertEqual(r.status_code, 200, url)
        return r.content.decode()

    def sidebar(self, html):
        return re.search(r'<aside\b[^>]*sidebar.*?</aside>', html, re.S).group(0)

    def test_members_sits_right_under_vouchers_in_the_sidebar(self):
        side = self.sidebar(self.page('/vouchers/'))
        links = re.findall(r'<a\b[^>]*href="([^"]+)"', side)
        self.assertIn('/members/', links, 'Members link survives the Account Management menu')
        self.assertEqual(links[links.index('/vouchers/') + 1], '/members/', 'and comes right after Vouchers')
        self.assertIn('/manage/', links, 'Account Management entry still there')
        self.assertNotIn('/plans/', links, 'Plans still lives under Account Management')

    def test_mobile_menu_has_it_too(self):
        html = self.page('/vouchers/')
        mobile = re.search(r'<div\b[^>]*class="[^"]*mobile-nav[^"]*"[^>]*>.*?</div>', html, re.S).group(0)
        self.assertIn('href="/members/"', mobile)

    def test_switch_on_both_pages_with_counts(self):
        for url, on in (('/vouchers/', 'Vouchers'), ('/members/', 'Members')):
            html = self.page(url)
            sw = re.search(r'<nav class="tt-vm-switch".*?</nav>', html, re.S)
            self.assertIsNotNone(sw, url)
            sw = sw.group(0)
            self.assertIn('href="/vouchers/"', sw); self.assertIn('href="/members/"', sw)
            self.assertRegex(sw, r'class="on"[^>]*aria-current="page">\s*<i[^>]*></i><span>' + on)
            self.assertRegex(sw, r'<span>Vouchers</span><b>1</b>'); self.assertRegex(sw, r'<span>Members</span><b>1</b>')

    def test_hidden_when_members_is_switched_off(self):
        self.biz.features_off = ['members']; self.biz.save(update_fields=['features_off'])
        html = self.page('/vouchers/')
        self.assertNotIn('tt-vm-switch', html)
        self.assertNotIn('href="/members/"', self.sidebar(html))
