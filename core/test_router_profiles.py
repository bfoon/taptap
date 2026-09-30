from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.utils import timezone

from . import router_profiles as rp
from .models import Business, Router, RouterConfigSnapshot, RouterHotspotProfile, RouterHotspotUser, VoucherPlan

MIKHMON = ':put (",rem,5000,1d,6000,,Disable,"); {:local date [/system clock get date]}'


class RouterProfilesTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1', currency='D',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=self.b, name='Kotu', ip_address='1.1.1.1', username='a', password='b')
        self.plan = VoucherPlan.objects.create(business=self.b, name='1 Day', price=25, duration_minutes=1440, mikrotik_profile_name='daily')
        for name, raw in [('daily', {'shared-users': '1', 'rate-limit': '2M/5M', 'session-timeout': '1d', 'add-mac-cookie': 'true', 'mac-cookie-timeout': '30d'}),
                          ('mikhmon-1d', {'shared-users': '2', 'on-login': MIKHMON, 'session-timeout': '1d'}),
                          ('taptap-1dev', {'shared-users': '1'}), ('default', {'shared-users': '1'})]:
            RouterHotspotProfile.objects.create(business=self.b, router=self.r, name=name, raw_data=raw, shared_users=int(raw['shared-users']))
        RouterHotspotUser.objects.create(business=self.b, router=self.r, username='A1', profile='daily', is_present=True)
        RouterHotspotUser.objects.create(business=self.b, router=self.r, username='A2', profile='daily', is_present=True)
        self.c = Client(); self.c.force_login(self.owner)

    def test_reads_profiles_and_links_plans(self):
        g = rp.gather(self.b)[0]
        by = {p['name']: p for p in g['profiles']}
        self.assertEqual(by['daily']['plan'], self.plan)
        self.assertEqual(by['daily']['users'], 2)
        self.assertTrue(by['daily']['mac_cookie'])
        self.assertEqual(by['mikhmon-1d']['price'], Decimal('6000'))       # selling price wins
        self.assertEqual(by['mikhmon-1d']['validity'], '1 day')             # minutes, shown as text
        self.assertTrue(by['taptap-1dev']['taptap'])
        self.assertTrue(by['default']['default'])
        r = self.c.get('/routers/profiles/')
        self.assertContains(r, 'mikhmon-1d'); self.assertContains(r, 'Make a plan')

    def test_make_a_plan_from_profile(self):
        r = self.c.post(f'/routers/{self.r.pk}/profiles/import/', {'name': 'mikhmon-1d', 'next': '/routers/profiles/'})
        self.assertRedirects(r, '/routers/profiles/', fetch_redirect_response=False)
        p = VoucherPlan.objects.get(name='mikhmon-1d')
        self.assertEqual((p.price, p.max_devices, p.source), (Decimal('6000'), 2, 'mikrotik'))

    def test_snapshot_used_before_first_mirror(self):
        r2 = Router.objects.create(business=self.b, name='Serekunda', ip_address='2.2.2.2', username='a', password='b')
        RouterConfigSnapshot.objects.create(router=r2, sections={'HotSpot user profiles': {'path': '/ip/hotspot/user/profile', 'count': 1,
                                                                                           'rows': [{'name': 'week', 'shared-users': '3', 'comment': 'price: 150'}]}})
        g = rp.gather(self.b, r2)[0]
        self.assertEqual(g['source'], 'snapshot')
        self.assertEqual(g['profiles'][0]['price'], Decimal('150'))

    def test_refresh_comes_back_here(self):
        with mock.patch('core.views.enqueue_router_sync', return_value=(mock.Mock(progress=0), True)):
            r = self.c.post(f'/routers/{self.r.pk}/sync/', {'next': '/routers/profiles/?router=%d' % self.r.pk})
        self.assertRedirects(r, '/routers/profiles/?router=%d' % self.r.pk, fetch_redirect_response=False)
