import json
import re
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Business


class MapTilesTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('o', 'o@x.com', 'pw12345678')
        Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1', trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.c = Client(); self.c.force_login(self.owner)

    def tiles(self, url):
        html = self.c.get(url).content.decode()
        m = re.search(r'<script id="ttMapTiles" type="application/json">(.*?)</script>', html)
        self.assertIsNotNone(m, url); self.assertIn('js/tt-tiles.js', html)
        self.assertNotIn('{s}.tile.openstreetmap.org', html)
        return json.loads(m.group(1))

    def test_both_maps_use_the_shared_tiles(self):
        for url in (reverse('topology'), reverse('topology_field')):
            d = self.tiles(url)
            self.assertEqual(d['url'], 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'); self.assertTrue(d['fallbackUrl'])

    @override_settings(MAP_TILE_URL='https://tiles.example.com/{z}/{x}/{y}.png?key=abc', MAP_TILE_ATTRIBUTION='&copy; Example')
    def test_own_provider(self):
        d = self.tiles(reverse('topology'))
        self.assertEqual(d['url'], 'https://tiles.example.com/{z}/{x}/{y}.png?key=abc'); self.assertEqual(d['attribution'], '&copy; Example')

    def test_loader_sends_the_origin(self):
        from pathlib import Path
        from django.conf import settings
        js = (Path(settings.BASE_DIR) / 'static' / 'js' / 'tt-tiles.js').read_text()
        self.assertIn("referrerPolicy: 'strict-origin-when-cross-origin'", js); self.assertIn('fallbackUrl', js)
