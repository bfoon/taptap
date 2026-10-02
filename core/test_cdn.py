from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, Client
from django.utils import timezone

from . import traffic as tr
from .cdn_report import cdns, others
from .models import AppUsage, Business, DeviceAppUsage, DeviceSignature, Router


class CdnRecognitionTests(TestCase):
    def test_cname_chain_names_the_service_behind_the_cdn(self):
        rows = [{'name': 'i.pinimg.com', 'type': 'CNAME', 'data': 'dualstack.pinterest.map.fastly.net'},
                {'name': 'dualstack.pinterest.map.fastly.net', 'type': 'A', 'data': '151.101.0.84'},
                {'name': 'video.example.org', 'type': 'CNAME', 'data': 'video.example.org.edgekey.net'},
                {'name': 'video.example.org.edgekey.net', 'type': 'CNAME', 'data': 'e123.a.akamaiedge.net'},
                {'name': 'e123.a.akamaiedge.net', 'type': 'A', 'data': '23.1.2.3'},
                {'name': 'www.google.com', 'address': '142.250.1.1'}]                      # RouterOS v6 row
        m = tr.dns_map_from_rows(rows)
        self.assertEqual(tr.classify_via(m['151.101.0.84'], 443, 'tcp')[:2] + (tr.classify_via(m['151.101.0.84'], 443, 'tcp')[3],), ('Pinterest', 'Social', 'Fastly'))
        app, cat, domain, via = tr.classify_via(m['23.1.2.3'], 443, 'tcp')
        self.assertEqual((app, cat, domain, via), ('Other sites', 'Video', 'example.org', 'Akamai'))   # unknown service, kind guessed from its name
        self.assertEqual(tr.classify_via(m['142.250.1.1'], 443, 'tcp')[3], '')
        self.assertEqual(tr.service_hint('reddit.map.fastly.net'), 'reddit')

    def test_ingest_stores_the_cdn(self):
        owner = User.objects.create_user('o', 'o@x.com', 'pw12345678')
        b = Business.objects.create(user=owner, business_name='K', owner_name='A', phone='1', trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        r = Router.objects.create(business=b, name='R', ip_address='1.1.1.1', username='a', password='b')
        cache.clear()
        dns = tr.dns_map_from_rows([{'name': 'i.pinimg.com', 'type': 'CNAME', 'data': 'dualstack.pinterest.map.fastly.net'},
                                    {'name': 'dualstack.pinterest.map.fastly.net', 'type': 'A', 'data': '151.101.0.84'}])
        conn = lambda down: [{'id': '*1', 'src-address': '10.5.50.9:5555', 'dst-address': '151.101.0.84:443', 'protocol': 'tcp', 'orig-bytes': 2000, 'repl-bytes': down}]
        now = timezone.now()
        tr.ingest_connections(r, conn(10_000), dns, now)
        tr.ingest_connections(r, conn(5_000_000), dns, now)
        u = AppUsage.objects.get()
        self.assertEqual((u.app, u.via), ('Pinterest', 'Fastly'))
        self.assertEqual(DeviceAppUsage.objects.get().via, 'Fastly')


class CdnReportTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('o', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1', trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=self.b, name='R', ip_address='1.1.1.1', username='a', password='b')
        h = timezone.now().replace(minute=0, second=0, microsecond=0)
        for app, cat, dom, via, mb in [('Pinterest', 'Social', 'pinimg.com', 'Fastly', 300), ('Reddit', 'Social', 'reddit.com', 'Fastly', 100),
                                       ('Other sites', 'Video', 'streamco.tv', 'Fastly', 700), ('YouTube', 'Video', 'googlevideo.com', '', 900),
                                       ('Other sites', 'Web & search', 'blog.gm', '', 50), ('Other', 'Other', 'unresolved', '', 40)]:
            AppUsage.objects.create(business=self.b, router=self.r, hour=h, app=app, category=cat, domain=dom, via=via, download=mb * 1048576)
            DeviceAppUsage.objects.create(business=self.b, router=self.r, hour=h, mac='AA:BB:CC:00:00:01', ip='10.5.50.9', app=app, category=cat, domain=dom, via=via, download=mb * 1048576)

    def test_cdn_summary_and_others(self):
        c = cdns(AppUsage.objects.all())
        self.assertEqual([x['name'] for x in c], ['Fastly'])
        self.assertEqual([s['name'] for s in c[0]['services']], ['streamco.tv', 'Pinterest', 'Reddit'])     # unknown service shown by its site
        self.assertEqual(list(c[0]['kinds'])[:2], ['Video', 'Social'])
        o = others(AppUsage.objects.all())
        self.assertEqual([s['domain'] for s in o['sites']], ['streamco.tv', 'blog.gm'])
        self.assertEqual(o['sites'][0]['via'], 'Fastly'); self.assertTrue(o['unnamed'] > 0)

    def test_endpoint_for_traffic_page_and_device(self):
        c = Client(); c.force_login(self.owner)
        d = c.get('/traffic/cdn/?range=today').json()
        self.assertEqual(d['cdns'][0]['name'], 'Fastly')
        dev = DeviceSignature.objects.create(business=self.b, fingerprint='fp', last_mac='AA:BB:CC:00:00:01', macs=['AA:BB:CC:00:00:01'])
        d = c.get(f'/traffic/cdn/?device={dev.pk}&period=7d').json()
        self.assertEqual(d['cdns'][0]['services'][0]['name'], 'streamco.tv')
        self.assertContains(c.get('/traffic/'), 'tt-cdn'); self.assertContains(c.get(f'/devices/{dev.pk}/?period=7d'), 'tt-cdn')
