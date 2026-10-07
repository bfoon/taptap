"""Router backups that were failing: script, upload, older RouterOS, missing certificates, error reports.

Run:  DB_ENGINE=sqlite python manage.py test core.test_backup_fixes
"""
import base64
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from . import agent
from . import backup_server as bs
from .models import AgentCommand, Business, Router, RouterAgent, RouterBackup
from .models_backup_storage import RouterBackupStorage
from .test_link_install import routeros_balanced


class ScriptTests(TestCase):
    def script(self, name='taptap-Test-20261006-190000'):
        return bs._router_script(name, 'https://taptapnetwork.com/api/router-backups/42/upload/', 'ttb_' + 'x' * 43)

    def test_failures_are_reported_to_taptap(self):
        s = self.script()
        self.assertIn(':onerror err in={', s)
        self.assertIn('kind=error&message=', s)
        self.assertTrue(routeros_balanced(s))

    def test_waits_until_the_files_are_written(self):
        s = self.script()
        self.assertIn(':while ((!$ready) and ($i < 45))', s)
        self.assertNotIn(':delay 2s; :local sendFile', s)

    def test_no_redeclared_function_parameters(self):
        s = self.script()
        self.assertNotIn(':local f $f', s)
        self.assertNotIn(':local kind $kind', s)

    def test_fits_one_link_command_even_with_a_long_router_name(self):
        self.assertLess(len(self.script('taptap-' + 'L' * 40 + '-20261006-190000')), agent.MAX_SCRIPT - 700)

    def test_version_check(self):
        for v, ok in (('6.49.10 (long-term)', False), ('7.12.1', False), ('7.13', True), ('7.16.2 (stable)', True), ('', True)):
            self.assertEqual(bs.can_upload_to_server(v), ok, v)


@override_settings(AUTH_EMAIL_OTP=False, SITE_URL='https://taptap.example')
class BackupFlowTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = mock.patch.object(bs, 'PRIVATE_ROOT', Path(self.tmp.name)); self.root.start()
        self.addCleanup(self.root.stop); self.addCleanup(self.tmp.cleanup)
        self.owner = User.objects.create_user('b@x.com', 'b@x.com', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='B', owner_name='O', phone='1', trial_ends_at=timezone.now() + timedelta(days=7))
        self.r = Router.objects.create(business=self.biz, name='hAP', ip_address='10.0.0.1', username='u', password='p', status='Online')

    # ── older RouterOS: the router-side backup instead of failing ──
    def test_old_routeros_on_link_saves_on_the_router(self):
        Router.objects.filter(pk=self.r.pk).update(connection_mode='agent'); self.r.refresh_from_db()
        RouterAgent.objects.create(router=self.r, ros_version='6.49.10 (long-term)', last_seen_at=timezone.now())
        record, storage = bs.start_backup(self.r, user=self.owner)
        self.assertIsNone(storage)
        self.assertEqual(AgentCommand.objects.get(router=self.r).kind, 'backup')
        self.assertIn('RouterOS 6.49.10 keeps the full .backup on the router', RouterBackup.objects.get(pk=record.pk).error)

    def test_old_routeros_on_api_uses_the_proven_method(self):
        legacy = RouterBackup.objects.create(router=self.r, name='taptap-hAP-old', content='/ip address\nadd address=10.0.0.1/24\n')
        svc = mock.Mock(); svc.safe_get.return_value = [{'version': '7.11.2 (stable)'}]
        with mock.patch.object(bs, '_LEGACY_BACKUP', return_value=legacy) as old:
            record, storage = bs.start_backup(self.r, user=self.owner, svc=svc)
        old.assert_called_once()
        self.assertEqual(record.pk, legacy.pk)
        self.assertEqual(storage.status, 'partial')                                     # text export saved on the server
        self.assertTrue((Path(self.tmp.name) / storage.export_path).exists())
        self.assertIn('text export is saved on the TapTap server', RouterBackup.objects.get(pk=legacy.pk).error)

    # ── direct API: certificates prepared, script scheduled ──
    def test_direct_api_prepares_https_trust_first(self):
        svc = mock.Mock(); svc.safe_get.return_value = [{'version': '7.16'}]
        certs, files, settings_ = mock.Mock(), mock.Mock(), mock.Mock()
        certs.get.return_value = [{'common-name': 'ISRG Root X1'}]
        files.get.return_value = []
        svc.resource.side_effect = lambda p: {'/certificate': certs, '/file': files, '/certificate/settings': settings_}.get(p, mock.Mock())
        with mock.patch('core.portctl.schedule_on_router') as sched:
            record, storage = bs.start_backup(self.r, user=self.owner, svc=svc)
        settings_.call.assert_called_with('set', {'builtin-trust-anchors': 'trusted'})
        self.assertEqual(files.add.call_count, 4)                                        # the four roots it did not have
        self.assertEqual(storage.status, 'queued'); sched.assert_called_once()

    # ── upload tolerance and error reports ──
    def upload(self, record, token, body):
        from django.urls import reverse
        return self.client.post(reverse('router_backup_upload', args=[record.pk]), body, content_type='application/x-www-form-urlencoded',
                                HTTP_X_TAPTAP_BACKUP=token)

    def test_plus_signs_turned_into_spaces_are_accepted(self):
        record, storage, token = bs._make_record(self.r)
        data = bytes(range(250, 256)) * 50                                                # base64 full of + and /
        enc = base64.b64encode(data).decode()
        self.assertIn('+', enc)
        r = self.upload(record, token, f'kind=backup&offset=0&total={len(data)}&data={enc.replace("/", "%2F")}')   # "+" sent raw -> space
        self.assertEqual(r.status_code, 200, r.content)
        storage.refresh_from_db(); self.assertEqual(storage.backup_size, len(data))

    def test_router_error_is_shown_instead_of_waiting(self):
        record, storage, token = bs._make_record(self.r)
        r = self.upload(record, token, 'kind=error&message=backup%20files%20not%20written%20in%2045%20s')
        self.assertEqual(r.status_code, 200)
        storage.refresh_from_db()
        self.assertEqual((storage.status, storage.error), ('failed', 'Router: backup files not written in 45 s'))
        self.assertEqual(self.upload(record, 'ttb_wrong', 'kind=error&message=x').status_code, 403)


@override_settings(AUTH_EMAIL_OTP=False, SITE_URL='https://taptap.example')
class BackupHelperTests(TestCase):
    """TapTap Link's rights cannot save a backup ("not enough permissions (9)"): a small helper script can."""
    def setUp(self):
        self.owner = User.objects.create_user('h@x.com', 'h@x.com', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='B', owner_name='O', phone='1', trial_ends_at=timezone.now() + timedelta(days=7))
        self.r = Router.objects.create(business=self.biz, name='TapTap K', ip_address='10.0.0.1', username='u', password='p', connection_mode='agent')
        RouterAgent.objects.create(router=self.r, ros_version='7.16.2', last_seen_at=timezone.now())
        self.client.force_login(self.owner)

    def test_link_command_runs_the_helper_and_fits(self):
        body = bs.link_backup_body({'file': 'taptap-TapTap-K-20261007-003827', 'upload_url': 'https://taptap.example/api/router-backups/9/upload/',
                                    'upload_token': 'ttb_' + 'x' * 43})
        self.assertIn('/system script run taptap-backup', body)
        self.assertIn('message=needs-helper', body)
        self.assertNotIn('/system backup save', body)                                 # not with Link's own rights
        self.assertTrue(routeros_balanced(body)); self.assertLess(len(body), agent.MAX_SCRIPT - 700)

    def test_helper_install_command(self):
        cmd = bs.helper_install_command('https://taptap.example')
        self.assertIn('policy=ftp,read,write,policy,test,password,sensitive dont-require-permissions=yes', cmd)
        self.assertIn(':global ttBkBase', cmd); self.assertIn('/system backup save', cmd)
        self.assertIn('name="taptap-backup-local"', cmd)
        self.assertTrue(routeros_balanced(cmd))

    def test_permission_errors_become_one_clear_instruction(self):
        from django.urls import reverse
        for said in ('needs-helper', 'not%20enough%20permissions%20(9)%20(%2Fsystem%2Fbackup%2Fsave%3B%20line%201)'):
            record, storage, token = bs._make_record(self.r)
            self.client.post(reverse('router_backup_upload', args=[record.pk]), f'kind=error&message={said}',
                             content_type='application/x-www-form-urlencoded', HTTP_X_TAPTAP_BACKUP=token)
            storage.refresh_from_db()
            self.assertEqual(storage.error, bs.NEEDS_HELPER)
        d = self.client.get(reverse('router_backups', args=[self.r.pk])).json()
        self.assertTrue(d['helper_needed']); self.assertIn('dont-require-permissions=yes', d['helper_command'])

    def test_tunnel_routers_back_up_over_the_api(self):
        svc = mock.Mock(); svc.safe_get.return_value = [{'version': '7.16'}]
        with mock.patch('core.voucher_history.channel', return_value='TapTap Tunnel'), mock.patch('core.portctl.schedule_on_router') as sched, \
                mock.patch.object(bs, '_ensure_trust_api'):
            bs.start_backup(self.r, user=self.owner, svc=svc)
        sched.assert_called_once()
        self.assertFalse(AgentCommand.objects.filter(kind='server_backup').exists())

    def test_quick_install_adds_the_helper_and_old_routeros_uses_it(self):
        s = agent.enrollment_script(self.r, 'ttl_' + 'a' * 40, None)
        self.assertIn('name="taptap-backup" policy=ftp,read,write,policy,test,password,sensitive', s)
        self.assertTrue(routeros_balanced(s))
        cmd = AgentCommand(router=self.r, kind='backup', params={'file': 'taptap-x'})
        self.assertIn('/system script run taptap-backup-local', agent.command_body(cmd))
