"""The TapTap phone app on the landing page: the newest published APK, a counted direct download (never GitHub's
pages), a folder or link of your own instead, and pages that still work before any APK exists."""
import json
import os
import tempfile
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings

from core import app_download as ad
from core.models_app_download import AppDownload

UA = {'HTTP_USER_AGENT': 'Mozilla/5.0 (Linux; Android 14; Pixel 8) Mobile'}
SHA = 'ab' * 32


def releases():
    def rel(tag, size, draft=False, name='TapTap.apk', body='', digest=''):
        return {'tag_name': tag, 'draft': draft, 'published_at': '2026-11-20T10:00:00Z', 'body': body,
                'assets': [{'name': name, 'size': size, 'digest': digest,
                            'browser_download_url': f'https://github.com/bfoon/taptap/releases/download/{tag}/{name}'}]}
    return [
        rel('app-v1.8.0-10', 31_000_000, body=f'- Calls ring faster\n- **Sales** buzz\n\nSHA-256 of TapTap.apk: {SHA}\n'),
        rel('app-v1.9.0-11', 33_000_000, draft=True),                 # a draft is never offered
        rel('app-v1.7.2-9', 30_000_000, digest='sha256:' + 'cd' * 32),
        rel('v2.0.0', 1, name='other.zip'),                          # not an app release
        rel('app-v1.8.0-9', 30_500_000),
    ]


def github(data):
    resp = mock.MagicMock()
    resp.__enter__.return_value.read.return_value = json.dumps(data).encode()
    return mock.patch('urllib.request.urlopen', return_value=resp)


@override_settings(APP_DISCOVER=True)
class Release(TestCase):
    def setUp(self):
        cache.clear()

    def test_newest_published_release_with_notes_and_checksum(self):
        with github(releases()):
            r = ad.release()
        self.assertEqual((r['version'], r['build'], r['label']), ('1.8.0', '10', '1.8.0 (build 10)'))
        self.assertEqual(r['size_text'], '29.6 MB')
        self.assertEqual(r['sha256'], SHA)                               # read from the release notes
        self.assertEqual(r['notes'], ['Calls ring faster', 'Sales buzz'])
        self.assertTrue(r['url'].endswith('/app-v1.8.0-10/TapTap.apk'))

    def test_nothing_published_yet_or_github_down(self):
        with github([]):
            self.assertIsNone(ad.release())
        cache.clear()
        with mock.patch('urllib.request.urlopen', side_effect=OSError('offline')):
            self.assertIsNone(ad.release())


class Download(TestCase):
    def setUp(self):
        cache.clear()

    @override_settings(APP_DISCOVER=True)
    def test_counted_redirect_to_the_file(self):
        with github(releases()):
            r = self.client.get('/app/get/android/', **UA)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], 'https://github.com/bfoon/taptap/releases/download/app-v1.8.0-10/TapTap.apk')
        d = AppDownload.objects.get()
        self.assertEqual((d.platform, d.version, d.count), ('android', '1.8.0 (build 10)', 1))
        self.client.get('/app/get/android/', **UA)                       # cached: no second GitHub call needed
        self.assertEqual(AppDownload.objects.get().count, 2)
        self.client.get('/app/get/android/', HTTP_USER_AGENT='WhatsApp/2.23')
        self.assertEqual(AppDownload.objects.get().count, 2)             # link previews are not downloads

    def test_no_apk_yet_is_404_not_an_error(self):
        self.assertEqual(self.client.get('/app/get/android/', **UA).status_code, 404)

    @override_settings(APP_ANDROID_URL='https://cdn.example.com/TapTap-2.0.apk', APP_ANDROID_VERSION='2.0.0')
    def test_own_link(self):
        r = self.client.get('/app/get/android/', **UA)
        self.assertEqual(r['Location'], 'https://cdn.example.com/TapTap-2.0.apk')

    def test_folder_on_this_server_newest_file_as_attachment(self):
        with tempfile.TemporaryDirectory() as d:
            old, new = os.path.join(d, 'TapTap-1.7.0.apk'), os.path.join(d, 'TapTap-1.8.0-10.apk')
            open(old, 'wb').write(b'old'); open(new, 'wb').write(b'new apk')
            os.utime(old, (1, 1))
            with override_settings(APP_LOCAL_DIR=d):
                r = self.client.get('/app/get/android/', **UA)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r['Content-Type'], 'application/vnd.android.package-archive')
                self.assertIn('TapTap-1.8.0.apk', r['Content-Disposition'])
                self.assertEqual(b''.join(r.streaming_content), b'new apk')
                r.close()
                rel = ad.release()
            self.assertEqual((rel['version'], rel['build'], rel['source']), ('1.8.0', '10', 'server'))
            self.assertEqual(len(rel['sha256']), 64)


class Pages(TestCase):
    def setUp(self):
        cache.clear()

    @override_settings(APP_DISCOVER=True)
    def test_landing_section_with_qr_and_download(self):
        with github(releases()):
            r = self.client.get('/', HTTP_USER_AGENT='Mozilla/5.0 (Windows NT 10.0)')
        self.assertContains(r, 'id="app"')
        self.assertContains(r, 'data-has="1"')
        self.assertContains(r, 'data-ap-qr="http://testserver/app/get/android/"')
        self.assertContains(r, 'href="/app/get/android/"')
        self.assertContains(r, '1.8.0 (build 10) · 29.6 MB')
        self.assertContains(r, 'app/app-download.js')
        self.assertNotContains(r, 'github.com/bfoon')                   # visitors never see GitHub

    def test_before_any_apk_the_pages_still_help(self):
        r = self.client.get('/', **UA)
        self.assertContains(r, 'data-has="0"')
        self.assertContains(r, 'The Android app is on its way')
        self.assertNotContains(r, 'href="/app/get/android/"')
        r = self.client.get('/app/', **UA)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Three taps to install.')

    @override_settings(APP_DISCOVER=True)
    def test_app_page_whats_new_own_server_hint_and_staff_numbers(self):
        with github(releases()):
            r = self.client.get('/app/', **UA)
        self.assertContains(r, 'In 1.8.0 (build 10)')
        self.assertContains(r, 'Calls ring faster')
        self.assertContains(r, 'Using your own TapTap server?')          # testserver is not taptapnetwork.com
        self.assertContains(r, '<code>http://testserver</code>', html=False)
        self.assertNotContains(r, 'only TapTap staff see this')
        self.client.force_login(User.objects.create_superuser('root@x.gm', 'root@x.gm', 'pw-123456'))
        r = self.client.get('/app/', **UA)
        self.assertContains(r, 'only TapTap staff see this')

    @override_settings(ALLOWED_HOSTS=['taptapnetwork.com', 'testserver'])
    def test_no_server_hint_on_the_main_address(self):
        r = self.client.get('/app/', HTTP_HOST='taptapnetwork.com', **UA)
        self.assertNotContains(r, 'Using your own TapTap server?')

    def test_landing_never_fails_because_of_the_app(self):
        with mock.patch('core.app_download.release', side_effect=RuntimeError('boom')):
            r = self.client.get('/', **UA)
        self.assertEqual(r.status_code, 200)
