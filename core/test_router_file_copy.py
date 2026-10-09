"""Save a router file to the computer before removing it; backups that no longer clash (409) or fail silently.

Run:  DB_ENGINE=sqlite python manage.py test core.test_router_file_copy
"""
import base64
import json
import re
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import backup_server as bs
from . import router_file_copy as rfc
from .models import AgentCommand, Business, Router
from .models_router_cleanup import RouterCleanup
from .test_link_install import routeros_balanced

BLOB = bytes(range(256)) * 100            # 25,600 bytes of binary: two pieces


@override_settings(AUTH_EMAIL_OTP=False, SITE_URL='https://taptap.example')
class FileCopyTests(TestCase):
    def setUp(self):
        cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = mock.patch.object(bs, 'PRIVATE_ROOT', Path(self.tmp.name))
        self.root.start()
        self.owner = User.objects.create_user('o', 'o@x.gm', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=self.b, name='TapTap K', ip_address='10.0.0.1', username='u', password='p', status='Online')
        plan = {'groups': [{'key': 'taptap_local', 'level': 'check', 'files': [{'name': 'taptap-TapTap-K-20261009-142802.backup', 'size': len(BLOB)}]},
                           {'key': 'kept', 'level': 'kept', 'files': [{'name': 'letsencrypt.crt', 'size': 3000}]}]}
        self.scan = RouterCleanup.objects.create(business=self.b, router=self.r, action='scan', status='done', result={'plan': plan})
        self.client.force_login(self.owner)

    def tearDown(self):
        self.root.stop()
        self.tmp.cleanup()

    def start_api(self, name='taptap-TapTap-K-20261009-142802.backup'):
        scripts = []
        with mock.patch('core.mikrotik.MikroTikService') as svc, \
                mock.patch('core.portctl.schedule_on_router', side_effect=lambda s, n, d, script: scripts.append(script)), \
                mock.patch('core.backup_server._ensure_trust_api'):
            svc.return_value.connect.return_value = svc.return_value
            r = self.client.post(reverse('router_cleanup_copy', args=[self.r.pk]), json.dumps({'scan': self.scan.pk, 'name': name}),
                                 content_type='application/json')
        return r, (scripts[0] if scripts else '')

    def send(self, job_id, token, **data):
        return self.client.post(reverse('router_file_upload', args=[job_id]), urlencode(data),
                                content_type='application/x-www-form-urlencoded', HTTP_X_TAPTAP_FILE=token)

    def test_script(self):
        s = rfc.router_script('a.backup', 'https://taptap.example/api/router-files/1/upload/', 'tok_abcdefghijklmnopqrstuv', 'yes-without-crl')
        self.assertTrue(routeros_balanced(s))
        self.assertIn('/file read file=$f offset=$o chunk-size=$n as-value', s)
        self.assertIn(':local n 16384', s); self.assertIn(':if ($g = 0) do={ :set n ($n / 2) }', s)
        self.assertIn('"kind=finish&total=" . $o', s); self.assertIn('kind=error&message=', s)
        self.assertIn('"X-TapTap-File: " . "tok_abcdefghijklmnopqrstuv"', s)

    def test_full_copy_and_download(self):
        r, script = self.start_api()
        job = r.json()['job']
        self.assertEqual((r.status_code, job['status'], job['name']), (200, 'waiting', 'taptap-TapTap-K-20261009-142802.backup'))
        token = re.search(r'"X-TapTap-File: " \. "([^"]+)"', script).group(1)
        self.assertEqual(self.send(job['id'], 'wrong', kind='chunk', offset=0, total=len(BLOB), data='AA==').status_code, 403)
        first, second = BLOB[:16384], BLOB[16384:]
        enc = lambda b: base64.b64encode(b).decode()
        self.assertEqual(self.send(job['id'], token, kind='chunk', offset=0, total=len(BLOB), data=enc(first)).status_code, 200)
        self.assertEqual(self.send(job['id'], token, kind='chunk', offset=0, total=len(BLOB), data=enc(first)).status_code, 200)   # repeat: fine
        self.assertEqual(self.send(job['id'], token, kind='chunk', offset=99999, total=200000, data=enc(b'x')).status_code, 409)  # gap
        mid = self.client.get(reverse('router_cleanup_job', args=[self.r.pk, job['id']])).json()['job']
        self.assertEqual((mid['status'], mid['received'], mid['total']), ('running', 16384, len(BLOB)))
        self.assertNotIn('token_sha', json.dumps(mid))
        self.send(job['id'], token, kind='chunk', offset=16384, total=len(BLOB), data=enc(second))
        self.assertEqual(self.send(job['id'], token, kind='finish', total=len(BLOB)).status_code, 200)
        done = self.client.get(reverse('router_cleanup_job', args=[self.r.pk, job['id']])).json()['job']
        self.assertEqual(done['status'], 'done')
        resp = self.client.get(done['download'])
        self.assertEqual(b''.join(resp.streaming_content), BLOB)
        self.assertIn('attachment; filename="taptap-TapTap-K-20261009-142802.backup"', resp['Content-Disposition'])
        self.assertEqual(self.send(job['id'], token, kind='chunk', offset=0, total=len(BLOB), data=enc(first)).status_code, 410)  # closed

    def test_errors_and_checks(self):
        r = self.client.post(reverse('router_cleanup_copy', args=[self.r.pk]), json.dumps({'scan': self.scan.pk, 'name': '/etc/passwd'}),
                             content_type='application/json')
        self.assertEqual(r.status_code, 400)
        r, script = self.start_api('letsencrypt.crt')                        # kept files can be saved too
        job, token = r.json()['job'], re.search(r'"X-TapTap-File: " \. "([^"]+)"', script).group(1)
        self.send(job['id'], token, kind='error', message='no such item')
        j = self.client.get(reverse('router_cleanup_job', args=[self.r.pk, job['id']])).json()['job']
        self.assertEqual(j['status'], 'failed'); self.assertIn('no such item', j['result']['error'])
        r, script = self.start_api()
        job, token = r.json()['job'], re.search(r'"X-TapTap-File: " \. "([^"]+)"', script).group(1)
        self.send(job['id'], token, kind='chunk', offset=0, total=len(BLOB), data=base64.b64encode(BLOB[:100]).decode())
        self.assertEqual(self.send(job['id'], token, kind='finish', total=len(BLOB)).status_code, 409)               # incomplete
        other = User.objects.create_user('x', 'x@x.gm', 'pw12345678')
        Business.objects.create(user=other, business_name='X', owner_name='X', phone='1', trial_ends_at=timezone.now() + timedelta(days=9))
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse('router_cleanup_download', args=[self.r.pk, job['id']])).status_code, 404)

    def test_old_copies_are_deleted(self):
        r, script = self.start_api()
        job = RouterCleanup.objects.get(pk=r.json()['job']['id'])
        p = rfc._path(job); p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b'x')
        RouterCleanup.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(hours=30))
        rfc.purge_old()
        self.assertFalse(p.exists()); job.refresh_from_db(); self.assertTrue(job.result['purged'])

    def test_link_copy(self):
        self.r.connection_mode = 'agent'; self.r.save()
        with mock.patch('core.voucher_history.channel', return_value='TapTap Link'), mock.patch('core.linkops.ensure_online'):
            r = self.client.post(reverse('router_cleanup_copy', args=[self.r.pk]),
                                 json.dumps({'scan': self.scan.pk, 'name': 'taptap-TapTap-K-20261009-142802.backup'}), content_type='application/json')
        self.assertEqual(r.json()['job']['status'], 'waiting')
        cmd = AgentCommand.objects.get(kind='filecopy')
        from .agent import queue, wrap
        body = wrap(cmd, 'https://taptap.example', 'no')
        self.assertTrue(routeros_balanced(body)); self.assertIn(f'/api/router-files/{r.json()["job"]["id"]}/upload/', body)
        for p in ({'job_id': 1, 'name': 'a"; /system reset', 'upload_url': 'https://t.x/api/router-files/1/upload/', 'upload_token': 'a' * 30},
                  {'job_id': 1, 'name': 'a.backup', 'upload_url': 'https://evil.example/steal', 'upload_token': 'a' * 30},
                  {'job_id': 1, 'name': 'a.backup', 'upload_url': 'https://t.x/api/router-files/1/upload/', 'upload_token': 'a"$b'}):
            with self.assertRaises(ValueError, msg=p):
                queue(self.r, 'filecopy', p)


class BackupClashTests(TestCase):
    def test_backups_wait_for_the_upload_and_never_run_twice(self):
        from .agent import ACK_WAIT
        self.assertGreaterEqual(ACK_WAIT['backup'], 3600)                     # no resend in the middle of an upload
        self.assertEqual(bs.CHUNK_SIZE, 16384)
        b = bs.link_backup_body({'file': 'taptap-TapTap-K-20261009-150847', 'upload_url': 'https://taptap.example/api/router-backups/9/upload/',
                                 'upload_token': 'ttb_' + 'x' * 43})
        self.assertIn(':if ($ttBkR=$ttBkBase) do={:error "busy"}; :set ttBkR $ttBkBase;', b)
        self.assertTrue(routeros_balanced(b))

    def test_clear_messages(self):
        from django.contrib.auth.models import User as U
        owner = U.objects.create_user('q', 'q@x.gm', 'pw12345678')
        biz = Business.objects.create(user=owner, business_name='K', owner_name='A', phone='1', trial_ends_at=timezone.now() + timedelta(days=9))
        r = Router.objects.create(business=biz, name='TapTap K', ip_address='10.0.0.1', username='u', password='p')
        with self.settings(SITE_URL='https://taptap.example'):
            record, storage, token = bs._make_record(r)
            url = '/' + bs._upload_url(record, None).split('://', 1)[1].split('/', 1)[1]
        for said, expect in (('action failed (6) (/system/backup/save; line 1)', 'storage is almost certainly full'),
                             ('failure: Status 409, Conflict (/tool/fetch; line 1)', 'clashed')):
            storage.status = 'uploading'; storage.save()
            self.client.post(url, urlencode({'kind': 'error', 'message': said}), content_type='application/x-www-form-urlencoded',
                             HTTP_X_TAPTAP_BACKUP=token)
            storage.refresh_from_db()
            self.assertIn(expect, storage.error)
