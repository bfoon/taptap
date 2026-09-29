"""Per-device data use: traffic split by device from the connection table, and the device page.

Run:  DB_ENGINE=sqlite python manage.py test core.test_device_data
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Business, DeviceAppUsage, DeviceSignature, Router, UsageRecord
from .models_team import TeamMember
from .traffic import collect_sessions, floor_hour, ingest_connections

MB = 1024 ** 2
HTML = {'HTTP_ACCEPT': 'text/html'}
CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


def conn(cid, src, dst, up, down):
    return {'id': cid, 'src-address': f'{src}:50000', 'dst-address': f'{dst}:443', 'protocol': 'tcp', 'orig-bytes': str(up), 'repl-bytes': str(down)}


@override_settings(AUTH_EMAIL_OTP=False, CACHES=CACHE)
class DeviceDataTests(TestCase):
    def setUp(self):
        cache.clear()
        self.now = timezone.now()
        self.owner = User.objects.create_user('dd@example.com', 'dd@example.com', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='B', owner_name='O', phone='1', trial_ends_at=self.now + timedelta(days=7))
        self.router = Router.objects.create(business=self.biz, name='R', ip_address='10.0.0.1', username='u', password='p')
        self.phone = DeviceSignature.objects.create(business=self.biz, fingerprint='f1', model='Tecno Spark', os='Android',
                                                    macs=['AA:AA:AA:AA:AA:01', 'AA:AA:AA:AA:AA:02'], vouchers=['VC1'])
        self.client.force_login(self.owner)

    def test_connections_are_split_by_device(self):
        collect_sessions(self.router, [{'id': '*1', 'user': 'VC1', 'mac-address': 'aa:aa:aa:aa:aa:02', 'address': '10.5.50.10', 'bytes-in': '0', 'bytes-out': '0', 'uptime': '1m'},
                                       {'id': '*2', 'user': 'VC2', 'mac-address': 'BB:BB:BB:BB:BB:01', 'address': '10.5.50.11', 'bytes-in': '0', 'bytes-out': '0', 'uptime': '1m'}], self.now)
        dns = {'142.250.1.1': 'rr3---sn-abc.googlevideo.com', '157.240.1.1': 'scontent.whatsapp.net'}
        ingest_connections(self.router, [], dns, self.now)              # baseline sample
        ingest_connections(self.router, [conn('c1', '10.5.50.10', '142.250.1.1', 2 * MB, 300 * MB),
                                         conn('c2', '10.5.50.11', '157.240.1.1', 1 * MB, 20 * MB)], dns, self.now)
        mine = DeviceAppUsage.objects.get(mac='AA:AA:AA:AA:AA:02')
        self.assertEqual((mine.username, mine.ip, mine.download, mine.upload), ('VC1', '10.5.50.10', 300 * MB, 2 * MB))
        self.assertEqual(mine.app, 'YouTube')
        self.assertEqual(DeviceAppUsage.objects.get(mac='BB:BB:BB:BB:BB:01').username, 'VC2')

    def fill(self):
        h = floor_hour(self.now)
        for mac, app, cat, domain, dn in [('AA:AA:AA:AA:AA:01', 'YouTube', 'Video', 'googlevideo.com', 900 * MB),
                                          ('AA:AA:AA:AA:AA:02', 'TikTok', 'Social', 'tiktokcdn.com', 400 * MB),
                                          ('AA:AA:AA:AA:AA:01', 'WhatsApp', 'Messaging & calls', 'whatsapp.net', 50 * MB),
                                          ('CC:CC:CC:CC:CC:CC', 'Netflix', 'Video', 'nflxvideo.net', 5000 * MB)]:   # someone else
            DeviceAppUsage.objects.create(business=self.biz, router=self.router, hour=h, mac=mac, ip='10.5.50.10', app=app, category=cat,
                                          domain=domain, download=dn, upload=dn // 20)
        DeviceAppUsage.objects.create(business=self.biz, router=self.router, hour=h - timedelta(days=3), mac='AA:AA:AA:AA:AA:01', app='YouTube',
                                      category='Video', domain='googlevideo.com', download=1000 * MB)
        UsageRecord.objects.create(business=self.biz, router=self.router, username='VC1', mac_address='AA:AA:AA:AA:AA:01', hour=h, download=1500 * MB, upload=50 * MB)

    def test_device_page_charts_and_top_lists(self):
        self.fill()
        r = self.client.get(reverse('device_detail', args=[self.phone.pk]), {'period': '24h'}, **HTML)
        self.assertEqual(r.status_code, 200)
        c = r.context
        self.assertEqual([a['app'] for a in c['apps']], ['YouTube', 'TikTok', 'WhatsApp'])   # the other device's Netflix is not here
        self.assertEqual(c['top_app']['app'], 'YouTube')
        self.assertEqual([s['domain'] for s in c['sites']][0], 'googlevideo.com')
        self.assertEqual({x['name'] for x in c['categories']}, {'Video', 'Social', 'Messaging & calls'})
        self.assertEqual(len(c['timeline']['labels']), 24)
        self.assertEqual(c['total'], sum(x + x // 20 for x in (900 * MB, 400 * MB, 50 * MB)))
        self.assertContains(r, 'ddTime')
        week = self.client.get(reverse('device_detail', args=[self.phone.pk]), {'period': '7d'}, **HTML).context
        self.assertEqual(len(week['timeline']['labels']), 7)
        self.assertGreater(week['total'], c['total'])      # the older YouTube day counts in 7 days

    def test_empty_device_and_list_link(self):
        r = self.client.get(reverse('device_detail', args=[self.phone.pk]), **HTML)
        self.assertContains(r, 'No app data for this device')
        self.assertContains(self.client.get(reverse('devices'), **HTML), reverse('device_detail', args=[self.phone.pk]))

    def test_access(self):
        other = User.objects.create_user('x@example.com', 'x@example.com', 'pw')
        Business.objects.create(user=other, business_name='X', owner_name='X', phone='2', trial_ends_at=self.now + timedelta(days=3))
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse('device_detail', args=[self.phone.pk]), **HTML).status_code, 404)
        u = User.objects.create_user('v@example.com', 'v@example.com', 'pw')
        TeamMember.objects.create(business=self.biz, user=u, role='viewer')
        self.client.force_login(u)
        self.assertEqual(self.client.get(reverse('device_detail', args=[self.phone.pk]), **HTML).status_code, 403)
