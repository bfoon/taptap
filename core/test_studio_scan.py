"""New Portal/Voucher Studio templates, "Scan the QR on your voucher", and Voucher Studio layout fixes.

Run:  DB_ENGINE=sqlite python manage.py test core.test_studio_scan
"""
import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from unittest import skipUnless

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import Business, PortalPage
from .studio_presets import PORTAL_TEMPLATES, VOUCHER_TEMPLATES, portal_template, voucher_template

STATIC = Path(settings.BASE_DIR) / 'static'
NEW_PORTAL = ['scango', 'flag', 'salon', 'clinic', 'cowork', 'youth']
NEW_VOUCHER = ['scanfirst', 'flag', 'salon', 'clinic', 'cowork', 'youth']
BLOCKS = {'logo', 'bonanza', 'heading', 'text', 'ads', 'trial', 'faq', 'ticker', 'voucher', 'plans', 'notice', 'image', 'steps',
          'payment', 'contact', 'social', 'button', 'terms', 'spacer', 'footer', 'session', 'countdown'}
ELEMENTS = {'text', 'code', 'qr', 'logo', 'rect', 'ellipse', 'line', 'icon', 'image', 'barcode', 'advert'}


class TemplateTests(TestCase):
    def test_new_portal_templates(self):
        for key in NEW_PORTAL:
            t = portal_template(key)
            self.assertEqual(PORTAL_TEMPLATES[key]['kind'], 'login')
            types = [b['type'] for b in t['blocks']]
            self.assertIn('voucher', types, key)
            self.assertTrue(set(types) <= BLOCKS, (key, set(types) - BLOCKS))
            self.assertEqual(len({b['id'] for b in t['blocks']}), len(t['blocks']), key)   # block ids unique
        scango = portal_template('scango')
        self.assertEqual([b for b in scango['blocks'] if b['type'] == 'voucher'][0]['scan_label'], 'Scan my voucher QR')

    def test_new_voucher_templates_fit_the_card(self):
        for key in NEW_VOUCHER:
            cfg = voucher_template(key)
            w, h = cfg['size']['w'], cfg['size']['h']
            for e in cfg['elements']:
                self.assertIn(e['type'], ELEMENTS, key)
                self.assertLessEqual(e['x'] + e['w'], w + .01, (key, e))
                self.assertLessEqual(e['y'] + e['h'], h + .01, (key, e))
            self.assertTrue(any(e['type'] == 'qr' for e in cfg['elements']), key)      # every new design can be scanned
            self.assertTrue(any(e['type'] == 'code' for e in cfg['elements']), key)

    def test_counts(self):
        self.assertGreaterEqual(len(PORTAL_TEMPLATES), 35)
        self.assertGreaterEqual(len(VOUCHER_TEMPLATES), 34)


@override_settings(AUTH_EMAIL_OTP=False)
class ScanTests(TestCase):
    def setUp(self):
        u = User.objects.create_user('s@x.com', 's@x.com', 'pw')
        self.biz = Business.objects.create(user=u, business_name='B', owner_name='O', phone='1', trial_ends_at=timezone.now() + timedelta(days=7))
        self.page = PortalPage.objects.create(business=self.biz, name='Login', slug='scan-test', kind='login', is_published=True,
                                              config=portal_template('scango'))

    def test_decoder_is_vendored_with_licence(self):
        js = (STATIC / 'vendor' / 'jsqr.js').read_text()
        self.assertIn('jsQR', js[:300]); self.assertIn('Apache License 2.0', js[:300])
        self.assertIn('Apache License', (STATIC / 'vendor' / 'jsqr.LICENSE').read_text())

    def test_router_login_page_carries_the_decoder_inert(self):
        from .views_studio import _export_html
        html = _export_html(self.page)
        self.assertIn('<script type="text/plain" id="tp-jsqr">', html)
        cfg = portal_template('scango')
        for b in cfg['blocks']:
            if b['type'] == 'voucher':
                b['scan'] = False
        self.page.config = cfg; self.page.save()
        self.assertNotIn('id="tp-jsqr"', _export_html(self.page))                     # switched off: page stays small

    def test_hosted_page_loads_the_decoder_on_demand(self):
        r = self.client.get(f'/p/{self.page.slug}/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'vendor/jsqr.js')
        self.assertNotContains(r, 'src="/static/vendor/jsqr.js"')                     # not loaded until someone taps Scan

    def test_renderer_and_editor(self):
        js = (STATIC / 'studio' / 'portal-render.js').read_text()
        self.assertIn('class="tp-scan"', js); self.assertIn('capture="environment"', js); self.assertIn('parseQr: parseVoucherQr', js)
        ed = (STATIC / 'studio' / 'portal-editor.js').read_text()
        self.assertIn("['scan', 'check',", ed)

    @skipUnless(shutil.which('node'), 'node not installed')
    def test_qr_contents_are_understood(self):
        script = """
global.window = global; global.document = {};
require('vm').runInThisContext(require('fs').readFileSync(process.argv[1], 'utf8'));
const P = window.TapPortal.parseQr;
console.log(JSON.stringify([
  P('http://wifi.local/login?username=K7Q2M9XP&password=K7Q2M9XP'),
  P('http://wifi.local/login?username=fatou.j&password=Kombo2026'),
  P('K7Q2 M9XP'), P('WIFI:S:Cafe;T:nopass;;'), P('https://example.com/promo'), P('')]));
"""
        out = subprocess.run(['node', '-e', script, str(STATIC / 'studio' / 'portal-render.js')], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        voucher, member, bare, wifi, other, empty = json.loads(out.stdout)
        self.assertEqual(voucher, {'user': 'K7Q2M9XP', 'pass': 'K7Q2M9XP', 'member': False})
        self.assertEqual(member, {'user': 'fatou.j', 'pass': 'Kombo2026', 'member': True})
        self.assertEqual(bare['user'], 'K7Q2M9XP')
        self.assertEqual(wifi, {'wifi': True})
        self.assertIsNone(other); self.assertIsNone(empty)


class VoucherStudioLayoutTests(TestCase):
    def test_zoomed_card_keeps_its_size_and_fingers_can_scroll(self):
        css = (STATIC / 'studio' / 'studio.css').read_text()
        self.assertIn('.vcanvas-wrap{position:relative;margin:auto;flex:none;touch-action:pan-x pan-y pinch-zoom', css)
        self.assertIn('.vcanvas .tv-el.is-sel,.vsel i{touch-action:none}', css)
        js = (STATIC / 'studio' / 'voucher-editor.js').read_text()
        self.assertIn("node.classList.add('is-sel')", js)
        self.assertIn("ev.pointerType === 'touch' && !h && t", js)
        self.assertIn('p.scrollTop = key === propsKey ? top : 0', js)
