"""Smart search on the Devices page.

Run:  DB_ENGINE=sqlite python manage.py test core.test_device_search
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import device_search as ds
from .models import (Agent, Business, DeviceAppUsage, DeviceSignature, Router, UsageRecord, Voucher, VoucherCodeAlias)

GB = 1024 ** 3
HTML = {'HTTP_ACCEPT': 'text/html'}
CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


@override_settings(AUTH_EMAIL_OTP=False, CACHES=CACHE)
class DeviceSearchTests(TestCase):
    def setUp(self):
        cache.clear()
        self.now = timezone.now()
        self.owner = User.objects.create_user('ds@example.com', 'ds@example.com', 'pw')
        b = self.biz = Business.objects.create(user=self.owner, business_name='B', owner_name='O', phone='1', trial_ends_at=self.now + timedelta(days=7))
        self.main = Router.objects.create(business=b, name='Main Hall', ip_address='10.0.0.1', username='u', password='p')
        self.garden = Router.objects.create(business=b, name='Garden', ip_address='10.0.0.2', username='u', password='p')
        awa = Agent.objects.create(business=b, name='Awa Shop')
        Voucher.objects.create(business=b, code='KW7Q2M', plan_name='1 Day', agent=awa, customer_name='Lamin Jallow', customer_phone='7712345')
        v2 = Voucher.objects.create(business=b, code='NEW55', plan_name='Weekly')
        VoucherCodeAlias.objects.create(business=b, voucher=v2, code='OLD99')
        self.tecno = DeviceSignature.objects.create(business=b, fingerprint='abc123', model='Tecno Spark 20', os='Android', os_version='14', browser='Chrome',
                                                    device_type='phone', macs=['3A:11:22:33:44:55', '7E:AA:BB:CC:DD:EE'], ips=['10.5.50.21'], vouchers=['KW7Q2M'],
                                                    router=self.main, last_mac='7E:AA:BB:CC:DD:EE', label='Lamin phone')
        self.iphone = DeviceSignature.objects.create(business=b, fingerprint='def456', model='iPhone', os='iOS', os_version='17', device_type='phone',
                                                     macs=['00:1A:2B:3C:4D:5E'], ips=['10.5.50.30'], vouchers=['NEW55'], router=self.garden,
                                                     first_seen=self.now - timedelta(days=20), last_seen=self.now - timedelta(days=10))
        self.laptop = DeviceSignature.objects.create(business=b, fingerprint='ghi789', model='', os='Windows', device_type='desktop',
                                                     macs=['00:50:56:AA:BB:CC'], ips=['10.5.50.40'], vouchers=[], router=self.main, flagged=True)
        h = self.now.replace(minute=0, second=0, microsecond=0)
        UsageRecord.objects.create(business=b, router=self.main, username='KW7Q2M', mac_address='7E:AA:BB:CC:DD:EE', hour=h, download=3 * GB)
        UsageRecord.objects.create(business=b, router=self.garden, username='NEW55', mac_address='00:1A:2B:3C:4D:5E', hour=h, download=GB // 4)
        DeviceAppUsage.objects.create(business=b, router=self.main, hour=h, mac='7E:AA:BB:CC:DD:EE', app='YouTube', category='Video',
                                      domain='googlevideo.com', download=2 * GB)
        cache.set(f'tt:tr:users:{self.main.pk}', {'users': {'KW7Q2M': [{'mac': '7e:aa:bb:cc:dd:ee', 'ip': '10.5.50.21'}]}}, 600)

    def find(self, q):
        items, info = ds.search(self.biz, q, self.biz.device_signatures.all())
        return [d.model or d.os for d in items], info, items

    def test_parse(self):
        words, filters, errors = ds.parse('tecno os:android -is:random plan:"1 Day" used > 2 gb is:nonsense')
        self.assertEqual(words, ['tecno'])
        self.assertIn(('os', 'android', False), filters)
        self.assertIn(('is', 'random', True), filters)
        self.assertIn(('plan', '1 Day', False), filters)
        self.assertIn(('used', ('>', 2 * GB), False), filters)
        self.assertTrue(errors)

    def test_words(self):
        self.assertEqual(self.find('tecno')[0], ['Tecno Spark 20'])
        self.assertEqual(self.find('7eaabb')[0], ['Tecno Spark 20'])            # MAC without colons
        self.assertEqual(self.find('7E-AA-BB')[0], ['Tecno Spark 20'])          # with dashes
        self.assertEqual(self.find('10.5.50.30')[0], ['iPhone'])
        names, _, items = self.find('Jallow')                                      # customer name on its voucher
        self.assertEqual(names, ['Tecno Spark 20'])
        self.assertIn('customer “Jallow”', items[0].why)
        self.assertEqual(self.find('garden')[0], ['iPhone'])                    # router name
        self.assertEqual(self.find('old99')[0], ['iPhone'])                     # old voucher code as a word

    def test_filters(self):
        self.assertEqual(self.find('os:ios')[0], ['iPhone'])
        self.assertEqual(sorted(self.find('-os:ios')[0]), ['Tecno Spark 20', 'Windows'])
        self.assertEqual(self.find('type:computer')[0], ['Windows'])
        self.assertEqual(self.find('voucher:OLD99')[0], ['iPhone'])             # an old code finds the device
        self.assertEqual(self.find('plan:"1 day"')[0], ['Tecno Spark 20'])
        self.assertEqual(self.find('agent:awa')[0], ['Tecno Spark 20'])
        self.assertEqual(self.find('router:main is:flagged')[0], ['Windows'])
        self.assertEqual(self.find('app:youtube')[0], ['Tecno Spark 20'])
        self.assertEqual(self.find('site:googlevideo')[0], ['Tecno Spark 20'])
        self.assertEqual(self.find('used>1gb')[0], ['Tecno Spark 20'])
        self.assertEqual(sorted(self.find('used<1gb')[0]), ['Windows', 'iPhone'])
        self.assertEqual(self.find('is:online')[0], ['Tecno Spark 20'])
        self.assertEqual(self.find('is:random')[0], ['Tecno Spark 20'])          # 7E / 3A are locally administered
        self.assertEqual(self.find('is:multimac')[0], ['Tecno Spark 20'])
        self.assertEqual(sorted(self.find('is:new')[0]), ['Tecno Spark 20', 'Windows'])
        self.assertEqual(sorted(self.find('seen:week')[0]), ['Tecno Spark 20', 'Windows'])
        self.assertEqual(self.find('mac:00:1a')[0], ['iPhone'])

    def test_sort_and_reasons(self):
        names, info, items = self.find('sort:data')
        self.assertEqual(names[0], 'Tecno Spark 20')
        self.assertEqual(info['sort'], 'data')
        self.assertTrue(info['data'])
        names, info, items = self.find('app:youtube used>1gb')
        self.assertTrue(any('YouTube' in w for w in items[0].why))
        self.assertTrue(any('in 7 days' in w for w in items[0].why))
        self.assertEqual([c['text'] for c in info['chips']], ['app:youtube', 'used>3.0gb'.replace('3.0', '1.0')])

    def test_page(self):
        self.client.force_login(self.owner)
        r = self.client.get(reverse('devices'), {'q': 'app:youtube sort:data'}, **HTML)
        self.assertEqual(r.status_code, 200)
        self.assertEqual([d.pk for d in r.context['devices']], [self.tecno.pk])
        self.assertContains(r, 'used YouTube')
        self.assertContains(r, '<b>1</b> device found', html=False)
        self.assertContains(self.client.get(reverse('devices'), {'q': 'is:bogus'}, **HTML), 'is:bogus is not known')
        self.assertContains(self.client.get(reverse('devices'), **HTML), 'dsSuggest')
