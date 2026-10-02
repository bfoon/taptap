import io
import zipfile
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.utils import timezone

from .models import AppUsage, Business, DeviceSignature, Router
from .platform_traffic import analysis, brand_of


class PlatformTrafficTests(TestCase):
    def setUp(self):
        self.su = User.objects.create_superuser('root', 'r@x.com', 'pw12345678')
        h = timezone.now().replace(minute=0, second=0, microsecond=0)
        for i in range(2):
            u = User.objects.create_user(f'o{i}', f'o{i}@x.com', 'pw12345678')
            b = Business.objects.create(user=u, business_name=f'B{i}', owner_name='A', phone='1', trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
            r = Router.objects.create(business=b, name='R', ip_address='1.1.1.1', username='a', password='b')
            AppUsage.objects.create(business=b, router=r, hour=h, app='YouTube', category='Video', domain='googlevideo.com', download=500 * 1048576)
            AppUsage.objects.create(business=b, router=r, hour=h, app='Pinterest', category='Social', domain='pinimg.com', via='Fastly', download=100 * 1048576)
            for n, (model, os) in enumerate([('SM-A125F', 'Android 12'), ('SM-A125F', 'Android 12'), ('iPhone', 'iOS 17'), ('TECNO KG5', 'Android 11'), ('RareModel X', 'Android 10')]):
                DeviceSignature.objects.create(business=b, fingerprint=f'fp{i}{n}', model=model, os=os, device_type='phone')
        self.c = Client(); self.c.force_login(self.su)

    def test_brands(self):
        self.assertEqual(brand_of('SM-A125F', 'Android 12'), 'Samsung')
        self.assertEqual(brand_of('iPhone', 'iOS 17'), 'Apple')
        self.assertEqual(brand_of('TECNO KG5', 'Android'), 'Tecno')
        self.assertEqual(brand_of('Redmi Note 9'), 'Xiaomi / Redmi / Poco')
        self.assertEqual(brand_of('Something', 'Android 9'), 'Other Android')

    def test_aggregates_only(self):
        d = analysis('7d')
        self.assertEqual((d['businesses'], d['devices']['count']), (2, 10))
        self.assertEqual(d['apps'][0]['name'], 'YouTube'); self.assertEqual(d['apps'][0]['businesses'], 2)
        self.assertEqual(d['cdns'][0]['name'], 'Fastly')
        self.assertEqual(d['devices']['brands'][0], {'name': 'Samsung', 'count': 4, 'pct': 40.0})
        models = {m['model']: m['count'] for m in d['devices']['models']}
        self.assertEqual(models.get('SM-A125F'), 4)
        self.assertNotIn('RareModel X', models)                     # fewer than 3: not listed by model
        self.assertEqual(d['devices']['small_models'], 6)

    def test_page_and_downloads(self):
        r = self.c.get('/platform/traffic/?period=7d')
        self.assertContains(r, 'Traffic across TapTap'); self.assertContains(r, 'Samsung')
        csv = self.c.get('/platform/traffic/?period=7d&format=csv&kind=devices').content.decode()
        self.assertIn('Brand,Samsung,4', csv); self.assertNotIn('fp0', csv)
        z = zipfile.ZipFile(io.BytesIO(self.c.get('/platform/traffic/?format=zip').content))
        self.assertEqual(sorted(z.namelist()), ['apps.csv', 'categories.csv', 'cdns.csv', 'daily.csv', 'devices.csv', 'hourly.csv', 'summary.csv'])
        self.assertEqual(self.c.get('/platform/traffic/?format=cdn').json()['cdns'][0]['name'], 'Fastly')

    def test_only_superusers(self):
        owner = User.objects.get(username='o0')
        c = Client(); c.force_login(owner)
        self.assertNotEqual(c.get('/platform/traffic/').status_code, 200)
        self.assertNotEqual(c.get('/platform/traffic/?format=zip').status_code, 200)
