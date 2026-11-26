"""TikLab downloads on the landing page: direct file downloads (never the repository page), new versions found by
themselves, counted downloads, phones and every system covered."""
import json
import os
import tempfile
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings

from core import tiklab
from core.models_tiklab import TikLabDownload

UA = {'HTTP_USER_AGENT': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}


class Catalog(TestCase):
    def setUp(self):
        cache.clear()

    def test_built_in_list_newest_first_with_manual_and_checksums(self):
        c = tiklab.catalog()
        rel = tiklab.latest(c)
        self.assertEqual(rel['version'], '1.4.0')
        self.assertEqual([p['key'] for p in rel['platforms']], ['windows', 'linux-x64', 'linux-arm64', 'source'])
        self.assertEqual(rel['manual']['name'], 'TikLab-1.4-User-Manual.pdf')
        self.assertEqual(len(rel['windows']['sha256']), 64)
        self.assertEqual([r['version'] for r in c['releases']], ['1.4.0', '1.3.0', '1.2.0'])
        self.assertIsNone(c['releases'][1]['manual'])            # no manual was written for 1.3: none is offered with it

    @override_settings(TIKLAB_DISCOVER=True)
    def test_a_new_version_in_the_repo_appears_by_itself(self):
        listing = [{'type': 'file', 'name': n, 'size': s} for n, (s, _) in tiklab.BUILT_IN.items()]
        listing += [{'type': 'file', 'name': 'TikLab-1.5.0-windows-x64.zip', 'size': 5_000_000},
                    {'type': 'file', 'name': 'TikLab-1.5-User-Manual.pdf', 'size': 6_500_000},
                    {'type': 'file', 'name': 'README.md', 'size': 10}, {'type': 'dir', 'name': 'docs', 'size': 0}]
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps(listing).encode()
        with mock.patch('urllib.request.urlopen', return_value=resp):
            c = tiklab.catalog()
        rel = tiklab.latest(c)
        self.assertTrue(c['live'])
        self.assertEqual(rel['version'], '1.5.0')
        self.assertEqual(rel['manual']['name'], 'TikLab-1.5-User-Manual.pdf')
        self.assertEqual(rel['windows']['sha256'], '')                       # unknown file: no checksum claimed
        self.assertEqual(c['releases'][1]['manual']['name'], 'TikLab-1.4-User-Manual.pdf')   # 1.4 keeps its own manual

    @override_settings(TIKLAB_DISCOVER=True)
    def test_github_down_uses_the_built_in_list(self):
        with mock.patch('urllib.request.urlopen', side_effect=OSError('offline')):
            c = tiklab.catalog()
        self.assertFalse(c['live'])
        self.assertEqual(tiklab.latest(c)['version'], '1.4.0')


class Downloads(TestCase):
    def setUp(self):
        cache.clear()

    def test_button_gives_the_file_itself_not_the_repository(self):
        r = self.client.get('/tiklab/get/TikLab-1.4.0-windows-x64.zip', **UA)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], 'https://raw.githubusercontent.com/bfoon/mikrotik_simulation/main/TikLab-1.4.0-windows-x64.zip')
        self.assertEqual(TikLabDownload.objects.get().count, 1)
        self.client.get('/tiklab/get/TikLab-1.4.0-windows-x64.zip', **UA)
        self.assertEqual(TikLabDownload.objects.get().count, 2)

    def test_latest_short_links_and_manual(self):
        r = self.client.get('/tiklab/get/latest/linux-arm64/', **UA)
        self.assertTrue(r['Location'].endswith('/TikLab-1.4.0-linux-arm64.tar.gz'))
        r = self.client.get('/tiklab/get/latest/manual/', **UA)
        self.assertTrue(r['Location'].endswith('/TikLab-1.4-User-Manual.pdf'))
        self.assertEqual(self.client.get('/tiklab/get/latest/macos/', **UA).status_code, 404)

    def test_only_known_files(self):
        for bad in ('evil.exe', '..%2F..%2Fetc%2Fpasswd', 'TikLab-9.9.9-windows-x64.zip'):
            self.assertEqual(self.client.get('/tiklab/get/' + bad, **UA).status_code, 404)
        self.assertFalse(TikLabDownload.objects.exists())

    def test_bots_are_not_counted(self):
        self.client.get('/tiklab/get/TikLab-1.4.0-source.zip', HTTP_USER_AGENT='WhatsApp/2.0')
        self.assertFalse(TikLabDownload.objects.exists())

    @override_settings(TIKLAB_DOWNLOAD_BASE='https://cdn.example.com/tiklab/')
    def test_own_cdn(self):
        r = self.client.get('/tiklab/get/TikLab-1.4.0-linux-x64.tar.gz', **UA)
        self.assertEqual(r['Location'], 'https://cdn.example.com/tiklab/TikLab-1.4.0-linux-x64.tar.gz')

    def test_files_on_this_server_are_sent_as_attachments(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'TikLab-1.4-User-Manual.pdf'), 'wb') as f:
                f.write(b'%PDF-1.7 test')
            with override_settings(TIKLAB_LOCAL_DIR=d):
                r = self.client.get('/tiklab/get/TikLab-1.4-User-Manual.pdf', **UA)
            self.assertEqual(r.status_code, 200)
            self.assertIn('attachment', r['Content-Disposition'])
            self.assertIn('TikLab-1.4-User-Manual.pdf', r['Content-Disposition'])
            self.assertEqual(b''.join(r.streaming_content), b'%PDF-1.7 test')
            r.close()


class Pages(TestCase):
    def setUp(self):
        cache.clear()

    def test_landing_page_offers_it_to_everybody(self):
        r = self.client.get('/', **UA)
        self.assertContains(r, 'id="simulator"')
        self.assertContains(r, 'href="/tiklab/get/TikLab-1.4.0-windows-x64.zip"')
        self.assertContains(r, 'href="/tiklab/get/TikLab-1.4-User-Manual.pdf"')
        self.assertContains(r, 'tiklab/tiklab.js')
        self.assertNotContains(r, 'github.com/bfoon')                  # visitors never see the repository

    def test_tiklab_page_public_and_signed_in(self):
        r = self.client.get('/tiklab/', **UA)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Unzip, start, log in.')
        self.assertContains(r, 'Older versions (2)')
        self.assertContains(r, '34fb7f59e1c6f66c662180f25081159a81dc8685c807f6000e3e2eb79603e9b5')
        self.assertNotContains(r, 'only TapTap staff see this')
        root = User.objects.create_superuser('root@x.gm', 'root@x.gm', 'pw-123456')
        self.client.force_login(root)
        self.client.get('/tiklab/get/TikLab-1.4.0-windows-x64.zip', **UA)
        r = self.client.get('/tiklab/', **UA)
        self.assertContains(r, 'only TapTap staff see this')

    def test_landing_never_fails_because_of_tiklab(self):
        with mock.patch('core.tiklab.catalog', side_effect=RuntimeError('boom')):
            r = self.client.get('/', **UA)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'id="simulator"')
