"""Portal Studio › Customers who called: the scrollable list, its filters and the live "on the page now" check."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core import portal_call
from core.models import Business, PortalPage, Voucher
from core.models_portal_call import PortalCaller


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'callerslist'}})
class CallersList(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('owner', 'o@x.gm', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='Kunda Net', owner_name='O', phone='7771234',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        PortalPage.objects.create(business=self.biz, name='Login', slug='kunda-login', kind='login', is_published=True,
                                  config={'theme': {}, 'blocks': [{'id': 'c', 'type': 'call', 'hidden': False}]})
        Voucher.objects.create(business=self.biz, code='KUNDA001', plan_name='Day', duration_minutes=1440)
        self.callers = []
        for i in range(30):
            self.callers.append(PortalCaller.objects.create(
                business=self.biz, name=f'Customer {i}', phone=f'+220 7{i:06d}', mac=f'AA:BB:CC:00:00:{i:02X}',
                key_hash=f'{i:064x}', calls=1 + i % 3, username='KUNDA001' if i == 3 else '',
                last_kind=['call', 'callback', 'voice'][i % 3], last_outcome=['answered', 'missed', 'waiting'][i % 3]))
        cache.set(portal_call.seen_key(self.biz.pk, self.callers[5].pk), 1, 60)
        self.client.force_login(self.owner)

    def test_long_list_scrolls_with_filters(self):
        r = self.client.get('/studio/portal/')
        self.assertContains(r, 'id="pclScroll"')
        self.assertEqual(r.content.decode().count(' data-tags='), 30)          # not cut at 12 any more
        self.assertContains(r, 'Call me back')
        self.assertContains(r, 'data-copy="AA:BB:CC:00:00:01"')
        self.assertContains(r, 'class="pcl-day">Today<')
        self.assertContains(r, 'wa.me/')
        n = r.context['calls']['caller_counts']
        self.assertEqual((n['all'], n['here'], n['callback'], n['voice'], n['missed']), (30, 1, 10, 10, 10))
        here = [c for c in r.context['calls']['callers'] if c['id'] == self.callers[5].pk][0]
        self.assertIn('here', here['tags'])
        self.assertIn('kunda001', [c for c in r.context['calls']['callers'] if c['id'] == self.callers[3].pk][0]['search'])

    def test_live_check(self):
        d = self.client.get('/studio/portal/calls/live/').json()
        self.assertEqual(d['here'], [self.callers[5].pk])
        self.assertEqual(len(d['newest']), 10)
        stranger = User.objects.create_user('s', 's@x.gm', 'pw')
        Business.objects.create(user=stranger, business_name='S', owner_name='S', phone='1', trial_ends_at=timezone.now() + timedelta(days=3))
        self.client.force_login(stranger)
        self.assertEqual(self.client.get('/studio/portal/calls/live/').json()['here'], [])
