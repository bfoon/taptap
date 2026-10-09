"""Clean router memory: what is safe, over the API and TapTap Link, and never anything outside the plan.

Run:  DB_ENGINE=sqlite python manage.py test core.test_router_cleanup
"""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import router_cleanup as rc
from .models import AgentCommand, Business, Router, RouterBackup
from .models_router_cleanup import RouterCleanup
from .test_link_install import routeros_balanced

OLD = (timezone.localtime() - timedelta(days=90)).strftime('%Y-%m-%d %H:%M:%S')
NEW = timezone.localtime().strftime('%Y-%m-%d %H:%M:%S')
FILES = [
    {'name': 'taptap-TapTap-K-20260701-010101.backup', 'size': '400000', 'type': 'backup', 'last-modified': OLD},
    {'name': 'taptap-TapTap-K-20260701-010101.rsc', 'size': '30000', 'type': 'script', 'last-modified': OLD},
    {'name': 'taptap-TapTap-K-20260801-010101.backup', 'size': '410000', 'type': 'backup', 'last-modified': OLD},
    {'name': 'taptap-TapTap-K-20261007-010904.backup', 'size': '420000', 'type': 'backup', 'last-modified': NEW},
    {'name': 'autosupout.rif', 'size': '900000', 'type': '.rif file', 'last-modified': OLD},
    {'name': 'log.0.txt', 'size': '120000', 'type': '.txt file', 'last-modified': OLD},
    {'name': 'routeros-7.16-arm.npk', 'size': '12000000', 'type': 'package', 'last-modified': NEW},
    {'name': 'taptap-ca-1.txt', 'size': '2000', 'type': '.txt file', 'last-modified': OLD},
    {'name': 'my-config-2024.rsc', 'size': '25000', 'type': 'script', 'last-modified': OLD},
    {'name': 'hotspot/login.html', 'size': '8000', 'type': '.html file', 'last-modified': OLD},
    {'name': 'letsencrypt.crt', 'size': '3000', 'type': '.crt file', 'last-modified': OLD},
    {'name': 'hotspot', 'size': '0', 'type': 'directory', 'last-modified': OLD},
]


class FakeRes:
    def __init__(self, svc, path):
        self.svc, self.path = svc, path

    def get(self, **kw):
        return self.svc.tables.get(self.path, [])

    def remove(self, id):
        self.svc.removed.append(id)
        self.svc.tables['/file'] = [f for f in self.svc.tables['/file'] if f['id'] != id]
        freed = sum(int(f['size']) for f in FILES if f.get('id') == id) if False else 0
        self.svc.tables['/system/resource'][0]['free-hdd-space'] = str(int(self.svc.tables['/system/resource'][0]['free-hdd-space']) + 100000)

    def set(self, id, **kw):
        self.svc.sets.append((self.path, id, kw))


class FakeSvc:
    def __init__(self):
        files = [dict(f, id=f'*{i}') for i, f in enumerate(FILES, 1)]
        self.tables = {'/system/resource': [{'free-hdd-space': '1500000', 'total-hdd-space': '16000000', 'free-memory': '20000000',
                                             'total-memory': '67108864', 'version': '7.16 (stable)', 'board-name': 'hAP ac2'}],
                       '/file': files, '/log': [{}] * 950, '/system/logging/action': [{'id': '*0', 'name': 'memory', 'memory-lines': '1000'}]}
        self.removed, self.sets = [], []

    def connect(self):
        return self

    def close(self):
        pass

    def safe_get(self, path, **kw):
        return self.tables.get(path, [])

    def resource(self, path):
        return FakeRes(self, path)


@override_settings(AUTH_EMAIL_OTP=False, SITE_URL='https://taptap.example')
class CleanupTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o', 'o@x.gm', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=self.b, name='TapTap K', ip_address='10.0.0.1', username='u', password='p', status='Online')
        # the July backup has a ready server copy
        rb = RouterBackup.objects.create(router=self.r, name='taptap-TapTap-K-20260701-010101', backup_file='x.backup', export_file='x.rsc')
        from .models_backup_storage import RouterBackupStorage
        RouterBackupStorage.objects.create(backup=rb, status='ready')
        self.client.force_login(self.owner)
        self.svc = FakeSvc()

    def groups(self, plan):
        return {g['key']: g for g in plan['groups']}

    def test_classification(self):
        plan = rc.classify(self.r, rc.api_scan(FakeSvc()))
        g = self.groups(plan)
        self.assertEqual({f['name'] for f in g['taptap_backups']['files']},
                         {'taptap-TapTap-K-20260701-010101.backup', 'taptap-TapTap-K-20260701-010101.rsc'})       # server copy exists
        self.assertEqual([f['name'] for f in g['taptap_local']['files']], ['taptap-TapTap-K-20260801-010101.backup'])
        kept = {f['name'] for f in g['kept']['files']}
        self.assertIn('taptap-TapTap-K-20261007-010904.backup', kept)                  # newest local-only backup stays
        self.assertIn('letsencrypt.crt', kept)
        self.assertEqual([f['name'] for f in g['support']['files']], ['autosupout.rif'])
        self.assertEqual([f['name'] for f in g['logs']['files']], ['log.0.txt'])
        self.assertEqual([f['name'] for f in g['leftovers']['files']], ['taptap-ca-1.txt'])
        self.assertEqual(g['packages']['level'], 'check'); self.assertFalse(g['packages']['files'][0]['selected'])
        self.assertEqual(g['other_backups']['level'], 'check')
        self.assertTrue(all(f['selected'] for f in g['support']['files']))
        self.assertEqual((plan['protected']['count'], plan['protected']['bytes']), (1, 8000))      # hotspot/ only counted
        self.assertTrue(plan['log']['suggest'])
        self.assertEqual(plan['storage']['used_pct'], round((16000000 - 1500000) * 100 / 16000000, 1))
        names = rc.cleanable_names(plan)
        self.assertNotIn('letsencrypt.crt', names); self.assertNotIn('hotspot/login.html', names)

    def api(self, url, data=None):
        with mock.patch('core.mikrotik.MikroTikService', return_value=self.svc):
            return self.client.post(url, json.dumps(data or {}), content_type='application/json')

    def test_api_scan_then_clean(self):
        j = self.api(reverse('router_cleanup_scan', args=[self.r.pk])).json()
        scan = j['job']
        self.assertEqual(scan['status'], 'done')
        r = self.api(reverse('router_cleanup_clean', args=[self.r.pk]),
                     {'scan': scan['id'], 'files': ['autosupout.rif', 'log.0.txt'], 'clear_log': True}).json()
        res = r['job']['result']
        self.assertEqual((res['removed_count'], res['failed_count'], res['log_cleared']), (2, 0, True))
        self.assertEqual(res['free_hdd'], 1700000); self.assertEqual(res['saved'], 200000)
        self.assertEqual(len(self.svc.removed), 2)
        self.assertEqual([kw for _, _, kw in self.svc.sets], [{'memory-lines': '1'}, {'memory-lines': '1000'}])   # log cleared, size restored

    def test_refuses_anything_outside_the_plan(self):
        scan = self.api(reverse('router_cleanup_scan', args=[self.r.pk])).json()['job']
        for bad in (['letsencrypt.crt'], ['hotspot/login.html'], ['taptap-TapTap-K-20261007-010904.backup'], ['../etc'], []):
            r = self.api(reverse('router_cleanup_clean', args=[self.r.pk]), {'scan': scan['id'], 'files': bad})
            self.assertEqual(r.status_code, 400, bad)
        self.assertEqual(self.svc.removed, [])
        RouterCleanup.objects.filter(pk=scan['id']).update(created_at=timezone.now() - timedelta(hours=1))
        r = self.api(reverse('router_cleanup_clean', args=[self.r.pk]), {'scan': scan['id'], 'files': ['autosupout.rif']})
        self.assertIn('scan again', r.json()['message'])

    def test_other_business_cannot_touch_the_router(self):
        other = User.objects.create_user('x', 'x@x.gm', 'pw12345678')
        Business.objects.create(user=other, business_name='X', owner_name='X', phone='1', trial_ends_at=timezone.now() + timedelta(days=9))
        self.client.force_login(other)
        self.assertEqual(self.api(reverse('router_cleanup_scan', args=[self.r.pk])).status_code, 404)

    def test_page_has_the_button(self):
        with mock.patch('core.mikrotik.MikroTikService', return_value=self.svc):
            html = self.client.get(reverse('router_control', args=[self.r.pk])).content.decode()
        self.assertIn('Clean router memory', html); self.assertIn('router_cleanup.js', html); self.assertIn('id="cleanModal"', html)


@override_settings(AUTH_EMAIL_OTP=False, SITE_URL='https://taptap.example')
class LinkCleanupTests(CleanupTests):
    def setUp(self):
        super().setUp()
        self.r.connection_mode = 'agent'; self.r.save()

    def link(self, url, data=None):
        with mock.patch('core.voucher_history.channel', return_value='TapTap Link'), mock.patch('core.linkops.ensure_online'):
            return self.client.post(url, json.dumps(data or {}), content_type='application/json')

    def upload(self, cmd, body):
        from .agent import nonce
        return self.client.post(reverse('agent_cleanup') + f'?c={cmd.pk}&n={nonce(cmd)}', body, content_type='text/plain')

    def test_api_scan_then_clean(self):
        pass

    def test_refuses_anything_outside_the_plan(self):
        pass

    def test_link_scan_clean_round_trip(self):
        from .agent import wrap
        job = self.link(reverse('router_cleanup_scan', args=[self.r.pk])).json()['job']
        self.assertEqual(job['status'], 'waiting')
        cmd = AgentCommand.objects.get(kind='cleanup')
        body = wrap(cmd, 'https://taptap.example', 'no')
        self.assertTrue(routeros_balanced(body)); self.assertIn('/file get $f last-modified', body)
        self.assertNotIn('\\$)', body)
        lines = ['fd=1500000', 'td=16000000', 'fm=20000000', 'tm=67108864', 'ver=7.16', 'bd=hAP ac2', 'll=950', 'ml=1000']
        lines += [f"f={f['name']}|{f['size']}|{f['type']}|{f['last-modified']}" for f in FILES if f['type'] != 'directory' and not f['name'].startswith('hotspot/')]
        lines += ['pc=1', 'pb=8000', 'tr=0']
        self.assertEqual(self.upload(cmd, '\n'.join(lines).encode()).status_code, 200)
        scan = self.client.get(reverse('router_cleanup_job', args=[self.r.pk, job['id']])).json()['job']
        g = self.groups(scan['result']['plan'])
        self.assertEqual(g['support']['files'][0]['name'], 'autosupout.rif')
        # clean, in two batches when the names do not fit one command
        with mock.patch('core.router_cleanup.LINK_NAMES_BUDGET', 20):
            r = self.link(reverse('router_cleanup_clean', args=[self.r.pk]),
                          {'scan': job['id'] if False else scan['id'], 'files': ['autosupout.rif', 'log.0.txt', 'taptap-ca-1.txt'], 'clear_log': True}).json()
        self.assertEqual(r['left'], 2)
        c1 = AgentCommand.objects.filter(kind='cleanup').latest('pk')
        b1 = wrap(c1, 'https://taptap.example', 'no')
        self.assertTrue(routeros_balanced(b1)); self.assertIn('{"autosupout.rif"}', b1); self.assertIn('memory-lines=1', b1)
        self.upload(c1, b'ok=1\nbad=0\nfr=900000\nlog=1\nfd=2400000\ntd=16000000\nfm=20100000\n')
        nxt = self.link(reverse('router_cleanup_clean', args=[self.r.pk]), {'continue': r['job']['id']}).json()
        c2 = AgentCommand.objects.filter(kind='cleanup').latest('pk')
        self.assertIn('"log.0.txt"', wrap(c2, 'https://taptap.example', 'no'))
        self.assertNotIn('memory-lines', wrap(c2, 'https://taptap.example', 'no'))                # only once
        self.upload(c2, b'ok=2\nbad=0\nfr=122000\nfd=2522000\ntd=16000000\nfm=20100000\n')
        done = RouterCleanup.objects.get(pk=nxt['job']['id'])
        self.assertEqual((done.result['removed_count'], done.result['saved']), (2, 1022000))      # measured against the scan

    def test_queue_refuses_tampered_names(self):
        from .agent import queue
        for p in ({'job_id': 1, 'action': 'clean', 'files': ['hotspot/login.html']},
                  {'job_id': 1, 'action': 'clean', 'files': ['a";/system reset;"']},
                  {'job_id': 1, 'action': 'scan', 'files': ['x.rif']}, {'job_id': 'x', 'action': 'scan', 'files': []}):
            with self.assertRaises(ValueError, msg=p):
                queue(self.r, 'cleanup', p)
