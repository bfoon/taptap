from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.utils import timezone

from .models import Business
from .models_team import TeamMember


class SecurityStickyTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('o', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1', trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.c = Client(); self.c.force_login(self.owner)

    def test_card_and_toggle(self):
        page = self.c.get('/security/')
        self.assertContains(page, 'Sticky vouchers'); self.assertContains(page, 'Lock each voucher to its devices')
        with mock.patch('core.sticky.apply_all', return_value=[]) as apply:
            self.c.post('/security/sticky/', {'sticky_sessions': 'on', 'sticky_keepalive': '2h'})    # lock off, sessions unchanged
            self.b.refresh_from_db()
            self.assertEqual((self.b.device_lock, self.b.sticky_sessions), (False, True))
            self.assertFalse(apply.called)                                                         # nothing changed for the routers
            self.c.post('/security/sticky/', {'device_lock': 'on', 'sticky_keepalive': '30m'})    # sessions off, keep-alive changed
            self.b.refresh_from_db()
            self.assertEqual((self.b.device_lock, self.b.sticky_sessions, self.b.sticky_keepalive), (True, False, '30m'))
            self.assertTrue(apply.called)                                                          # sent to the routers

    def test_only_network_managers(self):
        u = User.objects.create_user('v', 'v@x.com', 'pw12345678')
        TeamMember.objects.create(business=self.b, user=u, role='viewer')
        c = Client(); c.force_login(u)
        c.post('/security/sticky/', {})
        self.b.refresh_from_db(); self.assertTrue(self.b.device_lock)
