from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from .help_guides import GUIDES
from .models import Business


class NewGuidesTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('o', 'o@x.com', 'pw12345678')
        Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1', trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.c = Client(); self.c.force_login(self.owner)

    def test_guides_exist_and_their_pictures_too(self):
        by = {g['slug']: g for g in GUIDES}
        for slug in ('register-mikrotik', 'rotating-adverts'):
            self.assertIn(slug, by)
            for s in by[slug]['steps']:
                if s.get('image'):
                    self.assertTrue((Path(settings.BASE_DIR) / 'static' / s['image']).exists(), s['image'])

    def test_pages_show_steps_and_pictures(self):
        for slug, words in (('register-mikrotik', ['Create Router &amp; Generate Link', 'help/router-winbox-terminal.svg']),
                            ('rotating-adverts', ['Carousel', 'help/ads-carousel-phone.svg'])):
            r = self.c.get(reverse('support_guide', args=[slug]))
            self.assertEqual(r.status_code, 200)
            for w in words:
                self.assertContains(r, w)
