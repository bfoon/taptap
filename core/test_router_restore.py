"""Restore a router from the computer, the TapTap server or its own storage — safely, once, and watched until it is back.

Run:  DB_ENGINE=sqlite python manage.py test core.test_router_restore
"""
import json
import re
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import backup_server as bs
from . import router_restore as rr
from .models import AgentCommand, Business, Router, RouterBackup
from .models_router_restore import RouterRestore
from .test_link_install import routeros_balanced

BACKUP = b'\x88\xac\xa1\xb1' + bytes(range(256)) * 40


@override_settings(AUTH_EMAIL_OTP=False, SITE_URL='https://taptap.example')
class RestoreTests(TestCase):
    def setUp(self):
        cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = mock.patch.object(bs, 'PRIVATE_ROOT', Path(self.tmp.name))
        self.root.start()
        self.owner = User.objects.create_user('o', 'o@x.gm', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=self.b, name='TapTap K', ip_address='10.0.0.1', username='u', password='p', status='Online')
        self.client.force_login(self.owner)

    def tearDown(self):
        self.root.stop()
        self.tmp.cleanup()

    def server_backup(self, router=None):
        router = router or self.r
        rec = RouterBackup.objects.create(router=router, name=f'taptap-{router.name.replace(" ", "-")}-20261009-150847',
                                          backup_file='x.backup', export_file='x.rsc')
        from .models_backup_storage import RouterBackupStorage
        rel = bs._server_relpath(rec, 'backup')
        full = bs._full_path(rel); full.parent.mkdir(parents=True, exist_ok=True); full.write_bytes(BACKUP)
        RouterBackupStorage.objects.create(backup=rec, status='ready', backup_path=rel, backup_size=len(BACKUP), expected_backup_size=len(BACKUP))
        return rec

    def start_api(self, rid, **data):
        scripts = []
        with mock.patch('core.mikrotik.MikroTikService') as svc, \
                mock.patch('core.portctl.schedule_on_router', side_effect=lambda s, n, d, script: scripts.append(script)), \
                mock.patch('core.backup_server._ensure_trust_api'):
            svc.return_value.connect.return_value = svc.return_value
            resp = self.client.post(reverse('router_restore_start', args=[self.r.pk, rid]), json.dumps({'confirm': 'TapTap K', **data}),
                                    content_type='application/json')
        return resp, (scripts[0] if scripts else '')

    def report(self, rid, token, kind, message=''):
        return self.client.post(reverse('router_restore_report', args=[rid]), urlencode({'kind': kind, 'message': message}),
                                content_type='application/x-www-form-urlencoded', HTTP_X_TAPTAP_RESTORE=token)

    # ── scripts ──
    def test_scripts(self):
        r = RouterRestore(source='server', kind='backup', file_name='a.backup', size=1234, safety_copy=True)
        api = rr.router_script(r, file_url='https://t.x/api/router-restore/5/file/', report_url='https://t.x/api/router-restore/5/report/',
                               token='t' * 30, check='no', link=False, password='')
        self.assertTrue(routeros_balanced(api))
        self.assertIn('/tool fetch url="https://t.x/api/router-restore/5/file/" http-header-field=$h dst-path=$f', api)
        self.assertIn(':if ($z != 1234)', api)                                   # size check before anything changes
        self.assertLess(api.index('backup save name=taptap-before-restore'), api.index('/system backup load name=$f'))
        self.assertLess(api.index('k="loading"'), api.index('/system backup load'))  # TapTap is told before the reboot
        link = rr.router_script(r, file_url='https://t.x/api/router-restore/5/file/', report_url='https://t.x/api/router-restore/5/report/',
                                token='t' * 30, check='no', link=True)
        self.assertIn(':error "needs-restore-helper"', link); self.assertIn('/system script run taptap-restore-local', link)
        self.assertNotIn('/system backup load', link)
        r.source, r.kind = 'router', 'rsc'
        local = rr.router_script(r, file_url='', report_url='https://t.x/api/router-restore/5/report/', token='t' * 30, check='no', link=False)
        self.assertNotIn('dst-path', local)
        self.assertIn('/system reset-configuration no-defaults=yes skip-backup=yes run-after-reset=$f', local)
        self.assertTrue(routeros_balanced(rr.HELPER_SOURCE))
        self.assertIn('policy=ftp,reboot,read,write,policy,test,password,sensitive', rr.helper_install_command())

    # ── from the computer ──
    def test_upload_then_full_restore_over_api(self):
        bad = self.client.post(reverse('router_restore_upload', args=[self.r.pk]), {'file': SimpleUploadedFile('x.exe', b'MZ')})
        self.assertEqual(bad.status_code, 400)
        up = self.client.post(reverse('router_restore_upload', args=[self.r.pk]),
                              {'file': SimpleUploadedFile('taptap-TapTap-K-20261009-142802.backup', BACKUP)}).json()
        rid = up['restore']['id']
        self.assertEqual((up['restore']['source'], up['restore']['kind'], up['restore']['size']), ('upload', 'backup', len(BACKUP)))
        self.assertTrue(any('restarts during the restore' in w[1] for w in up['warnings']))
        self.assertEqual(self.client.post(reverse('router_restore_start', args=[self.r.pk, rid]), json.dumps({'confirm': 'wrong'}),
                                          content_type='application/json').status_code, 400)
        resp, script = self.start_api(rid, safety=True)
        self.assertEqual(resp.json()['restore']['status'], 'sending')
        token = re.search(r'"X-TapTap-Restore: " \. "([^"]+)"', script).group(1)
        self.assertEqual(self.client.get(reverse('router_restore_file', args=[rid])).status_code, 403)
        f = self.client.get(reverse('router_restore_file', args=[rid]), HTTP_X_TAPTAP_RESTORE=token)
        self.assertEqual(b''.join(f.streaming_content), BACKUP)
        self.report(rid, token, 'sending'); self.report(rid, token, 'ready', str(len(BACKUP)))
        self.assertEqual(RouterRestore.objects.get(pk=rid).status, 'checking')
        self.report(rid, token, 'loading')
        r = RouterRestore.objects.get(pk=rid)
        self.assertEqual(r.status, 'restoring'); self.assertIsNotNone(r.loading_at)
        self.assertEqual(self.client.get(reverse('router_restore_file', args=[rid]), HTTP_X_TAPTAP_RESTORE=token).status_code, 410)
        # the router comes back: uptime shorter than the time since the restore began
        RouterRestore.objects.filter(pk=rid).update(loading_at=timezone.now() - timedelta(seconds=90))
        with mock.patch('core.mikrotik.MikroTikService') as svc:
            svc.return_value.connect.return_value = svc.return_value
            svc.return_value.safe_get.return_value = [{'uptime': '45s'}]
            st = self.client.get(reverse('router_restore_status', args=[self.r.pk, rid])).json()['restore']
        self.assertEqual(st['status'], 'done')
        self.assertTrue(any('back online' in e['text'] for e in st['events']))

    def test_router_still_on_old_boot_is_not_done(self):
        rec = self.server_backup()
        rid = self.client.post(reverse('router_restore_prepare', args=[self.r.pk]), json.dumps({'source': 'server', 'backup': rec.pk}),
                               content_type='application/json').json()['restore']['id']
        resp, script = self.start_api(rid)
        token = re.search(r'"X-TapTap-Restore: " \. "([^"]+)"', script).group(1)
        self.report(rid, token, 'loading')
        RouterRestore.objects.filter(pk=rid).update(loading_at=timezone.now() - timedelta(seconds=60))
        with mock.patch('core.mikrotik.MikroTikService') as svc:
            svc.return_value.connect.return_value = svc.return_value
            svc.return_value.safe_get.return_value = [{'uptime': '3d4h'}]        # has not restarted yet
            st = self.client.get(reverse('router_restore_status', args=[self.r.pk, rid])).json()['restore']
        self.assertEqual(st['status'], 'rebooting')
        RouterRestore.objects.filter(pk=rid).update(loading_at=timezone.now() - timedelta(minutes=13))
        cache.clear()
        with mock.patch('core.mikrotik.MikroTikService', side_effect=OSError('unreachable')):
            st = self.client.get(reverse('router_restore_status', args=[self.r.pk, rid])).json()['restore']
        self.assertEqual(st['status'], 'failed'); self.assertIn('taptap-before-restore.backup', st['error'])

    # ── from the server / the router ──
    def test_prepare_checks(self):
        r = self.client.post(reverse('router_restore_prepare', args=[self.r.pk]), json.dumps({'source': 'server', 'backup': 999}),
                             content_type='application/json')
        self.assertEqual(r.status_code, 400)
        other = Router.objects.create(business=self.b, name='Brikama', ip_address='10.0.0.2', username='u', password='p')
        rec = self.server_backup(other)
        j = self.client.post(reverse('router_restore_prepare', args=[self.r.pk]), json.dumps({'source': 'server', 'backup': rec.pk}),
                             content_type='application/json').json()
        self.assertIn('made on Brikama', j['warnings'][0][1])
        self.assertTrue(any('another router' in w[1] for w in j['warnings']))
        for name in ('../../etc/x.backup', 'a.exe', 'hotspot/login.html'):
            self.assertEqual(self.client.post(reverse('router_restore_prepare', args=[self.r.pk]), json.dumps({'source': 'router', 'name': name}),
                                              content_type='application/json').status_code, 400)
        j = self.client.post(reverse('router_restore_prepare', args=[self.r.pk]), json.dumps({'source': 'router', 'name': 'old-config.rsc'}),
                             content_type='application/json').json()
        self.assertEqual(j['restore']['kind'], 'rsc'); self.assertTrue(any('full reset' in w[1] for w in j['warnings']))

    def test_storage_full_and_one_at_a_time(self):
        from .link_system_health import _cache_key
        cache.set(_cache_key(self.r.pk), {'at': timezone.now(), 'resource': {'free-hdd-space': '20000'}}, 600)
        rec = self.server_backup()
        rid = self.client.post(reverse('router_restore_prepare', args=[self.r.pk]), json.dumps({'source': 'server', 'backup': rec.pk}),
                               content_type='application/json').json()['restore']['id']
        resp, _ = self.start_api(rid)
        self.assertEqual(resp.status_code, 400); self.assertIn('Clean router memory', resp.json()['message'])
        cache.clear()
        resp, _ = self.start_api(rid)
        self.assertEqual(resp.status_code, 200)
        rid2 = self.client.post(reverse('router_restore_prepare', args=[self.r.pk]), json.dumps({'source': 'server', 'backup': rec.pk}),
                                content_type='application/json').json()['restore']['id']
        resp, _ = self.start_api(rid2)
        self.assertIn('Another restore is running', resp.json()['message'])

    # ── TapTap Link ──
    def test_link_restore_is_sent_once(self):
        self.r.connection_mode = 'agent'; self.r.save()
        rec = self.server_backup()
        rid = self.client.post(reverse('router_restore_prepare', args=[self.r.pk]), json.dumps({'source': 'server', 'backup': rec.pk}),
                               content_type='application/json').json()['restore']['id']
        with mock.patch('core.voucher_history.channel', return_value='TapTap Link'), mock.patch('core.linkops.ensure_online'):
            resp = self.client.post(reverse('router_restore_start', args=[self.r.pk, rid]),
                                    json.dumps({'confirm': 'taptap k', 'password': 'secret1'}), content_type='application/json')
        self.assertEqual(resp.json()['restore']['status'], 'sending')
        cmd = AgentCommand.objects.get(kind='restore')
        from .agent import wrap, queue
        body = wrap(cmd, 'https://taptap.example', 'no')
        self.assertTrue(routeros_balanced(body)); self.assertIn('/system script run taptap-restore-local', body)
        token = cmd.params['token']
        self.report(rid, token, 'error', 'needs-restore-helper')
        r = RouterRestore.objects.get(pk=rid)
        self.assertEqual(r.status, 'failed'); self.assertIn('Allow restores', r.error)
        cmd.refresh_from_db(); self.assertEqual(cmd.params['password'], '')             # the password is not kept
        # a restore that was sent but never confirmed is not re-sent
        AgentCommand.objects.filter(pk=cmd.pk).update(status='sent', sent_at=timezone.now() - timedelta(hours=2), attempts=1)
        from .agent import build_response
        try:
            build_response(self.r, 'https://taptap.example')
        except Exception:
            pass
        cmd.refresh_from_db()
        self.assertEqual(cmd.status, 'done')
        for p in ({'restore_id': 1, 'file_url': 'https://evil/x', 'report_url': 'https://t/api/router-restore/1/report/', 'token': 'a' * 30},
                  {'restore_id': 1, 'file_url': 'https://t/api/router-restore/1/file/', 'report_url': 'https://t/api/router-restore/1/report/',
                   'token': 'a' * 30, 'password': 'x"; /system reset'}):
            with self.assertRaises(ValueError, msg=p):
                queue(self.r, 'restore', p)

    def test_other_business_and_page(self):
        with mock.patch('core.mikrotik.MikroTikService'):
            html = self.client.get(reverse('router_control', args=[self.r.pk])).content.decode()
        self.assertIn('id="restoreModal"', html); self.assertIn('router_restore.js', html); self.assertIn('Restore…', html)
        other = User.objects.create_user('x', 'x@x.gm', 'pw12345678')
        Business.objects.create(user=other, business_name='X', owner_name='X', phone='1', trial_ends_at=timezone.now() + timedelta(days=9))
        self.client.force_login(other)
        self.assertEqual(self.client.post(reverse('router_restore_prepare', args=[self.r.pk]), json.dumps({'source': 'router', 'name': 'a.backup'}),
                                          content_type='application/json').status_code, 404)
