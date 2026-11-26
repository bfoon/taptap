"""The login page warns about random ("private") Wi-Fi addresses and guides the customer (static/studio/portal-render.js)."""
from django.test import SimpleTestCase

from core.views_studio import STATIC_DIR


class MacHelp(SimpleTestCase):
    def setUp(self):
        self.js = (STATIC_DIR / 'studio' / 'portal-render.js').read_text(encoding='utf-8')

    def test_wired_into_every_page(self):
        self.assertIn('wireClock(root, ctx); wireMacHelp(root, ctx);', self.js)
        self.assertIn('CLOCK_CSS + MAC_CSS', self.js)
        for brand in ('ios', 'samsung', 'transsion', 'xiaomi', 'oppo', 'huawei', 'vivo', 'android'):
            self.assertIn(f"['{brand}',", self.js)

    def test_safe_inside_a_router_template(self):
        # RouterOS replaces every "$(name)" in a hotspot page: the renderer must never contain one by accident.
        self.assertNotIn('$(', self.js)

    def test_editor_offers_the_setting(self):
        editor = (STATIC_DIR / 'studio' / 'portal-editor.js').read_text(encoding='utf-8')
        self.assertIn('data-sk="mac_help"', editor)
