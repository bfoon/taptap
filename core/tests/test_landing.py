"""The public landing page and its adverts, managed from the platform console (core/landing.py)."""
import io
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Business
from core.models_landing import LandingAd

CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'landing'}}
BROWSER = {'HTTP_USER_AGENT': 'Mozilla/5.0 (Linux; Android 14) Chrome/130'}


def png(w=2400, h=1400):
    from PIL import Image
    out = io.BytesIO()
    Image.new('RGB', (w, h), (23, 105, 224)).save(out, 'PNG')
    return SimpleUploadedFile('ad.png', out.getvalue(), content_type='image/png')


@override_settings(CACHES=CACHE, AUTH_EMAIL_OTP=False)
class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_superuser('root@x.gm', 'root@x.gm', 'pw-123456')

    def ad(self, **kw):
        data = {'title': 'Fibre in Serrekunda', 'placement': 'spotlight', 'link': 'https://example.gm/offer', 'active': True}
        data.update(kw)
        return LandingAd.objects.create(**data)


class PublicPage(Base):
    def test_page_works_without_adverts_and_needs_no_outside_files(self):
        r = self.client.get('/', **BROWSER)
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        for name in ('Free', 'Basic', 'Pro', 'Enterprise'):
            self.assertIn(f'<h3>{name}</h3>', body)
        self.assertIn(reverse('register'), body)
        self.assertNotIn('cdn.jsdelivr', body)
        self.assertNotIn('class="strip', body)
        self.assertNotIn('id="offers"', body)

    def test_each_place_shows_only_live_adverts(self):
        now = timezone.now()
        live = self.ad(title='Live offer')
        self.ad(title='Paused offer', active=False)
        self.ad(title='Future offer', starts_at=now + timedelta(days=1))
        self.ad(title='Old offer', ends_at=now - timedelta(minutes=1))
        self.ad(title='Big news', placement='strip', badge='NEW')
        self.ad(title='Hero partner', placement='hero', advertiser='QCell')
        self.ad(title='Wide banner', placement='banner')
        body = self.client.get('/', **BROWSER).content.decode()
        for t in ('Live offer', 'Big news', 'Hero partner', 'Wide banner', 'NEW', 'Sponsored'):
            self.assertIn(t, body)
        for t in ('Paused offer', 'Future offer', 'Old offer'):
            self.assertNotIn(t, body)
        self.assertIn(reverse('landing_ad_click', args=[live.pk]), body)

    def test_views_count_people_not_bots_or_staff(self):
        a = self.ad()
        self.client.get('/', **BROWSER)
        self.client.get('/', HTTP_USER_AGENT='Googlebot/2.1')
        self.client.force_login(self.admin)
        self.client.get('/', **BROWSER)
        a.refresh_from_db()
        self.assertEqual(a.views, 1)

    def test_a_click_is_counted_and_forwarded(self):
        a = self.ad()
        r = self.client.get(reverse('landing_ad_click', args=[a.pk]), **BROWSER)
        self.assertRedirects(r, 'https://example.gm/offer', fetch_redirect_response=False)
        a.refresh_from_db()
        self.assertEqual(a.clicks, 1)
        bad = self.ad(link='')
        LandingAd.objects.filter(pk=bad.pk).update(link='javascript:alert(1)')
        self.assertRedirects(self.client.get(reverse('landing_ad_click', args=[bad.pk])), '/', fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse('landing_ad_click', args=[999])), '/', fetch_redirect_response=False)

    @override_settings(LANDING_STATS_MIN=1)
    def test_live_figures_appear_once_there_are_businesses(self):
        u = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456')
        Business.objects.create(user=u, business_name='Shop', owner_name='O', phone='1', trial_ends_at=timezone.now())
        body = self.client.get('/', **BROWSER).content.decode()
        self.assertIn('Wi-Fi businesses', body)

    def test_signed_in_owner_gets_a_way_back_in(self):
        u = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456')
        Business.objects.create(user=u, business_name='Shop', owner_name='O', phone='1', trial_ends_at=timezone.now() + timedelta(days=3))
        self.client.force_login(u)
        r = self.client.get('/', **BROWSER)
        self.assertContains(r, 'Open TapTap')
        self.assertContains(r, reverse('dashboard'))

    def test_preview_is_for_staff_only(self):
        a = self.ad(title='Secret draft', active=False)
        self.assertNotContains(self.client.get(f'/?preview={a.pk}', **BROWSER), 'Secret draft')
        self.client.force_login(self.admin)
        r = self.client.get(f'/?preview={a.pk}', **BROWSER)
        self.assertContains(r, 'Secret draft')
        self.assertContains(r, 'only you see it')


class Console(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def test_create_with_a_picture_made_small(self):
        r = self.client.post(reverse('platform_landing_save'), {
            'title': 'Starlink kits', 'text': 'Install in a day', 'placement': 'hero', 'style': 'night', 'cta': 'Order',
            'link': 'https://shop.example/starlink', 'advertiser': 'Kerr Tech', 'weight': '50', 'active': '1', 'image': png()})
        self.assertEqual(r.status_code, 302)
        a = LandingAd.objects.get()
        self.assertEqual((a.placement, a.style, a.weight, a.created_by), ('hero', 'night', 10, self.admin))
        self.assertTrue(a.image.startswith('data:image/webp;base64,'))
        r = self.client.get(reverse('landing_ad_image', args=[a.pk]))
        self.assertEqual(r['Content-Type'], 'image/webp')
        from PIL import Image
        img = Image.open(io.BytesIO(r.content))
        self.assertLessEqual(img.size[0], 1600)
        self.assertLessEqual(img.size[1], 900)
        self.assertEqual(self.client.get(reverse('landing_ad_image', args=[a.pk]), HTTP_IF_NONE_MATCH=r['ETag']).status_code, 304)

    def test_bad_input_is_refused_with_a_reason(self):
        r = self.client.post(reverse('platform_landing_save'), {'title': 'X', 'link': 'ftp://x'}, follow=True)
        self.assertContains(r, 'must start with http')
        r = self.client.post(reverse('platform_landing_save'), {'title': 'X', 'starts_at': '2026-12-02T10:00', 'ends_at': '2026-12-01T10:00'}, follow=True)
        self.assertContains(r, 'end must come after')
        r = self.client.post(reverse('platform_landing_save'), {'title': 'X', 'image': SimpleUploadedFile('a.png', b'not a picture')}, follow=True)
        self.assertContains(r, 'not a picture')
        self.assertFalse(LandingAd.objects.exists())

    def test_page_and_actions(self):
        a = self.ad(title='Promo', views=10, clicks=2)
        r = self.client.get(reverse('platform_landing'))
        self.assertContains(r, 'Promo')
        self.assertContains(r, 'Where adverts appear')
        self.assertContains(self.client.get(reverse('platform_landing') + f'?edit={a.pk}'), 'Edit “Promo”')
        act = reverse('platform_landing_action', args=[a.pk])
        self.client.post(act, {'action': 'pause'}); a.refresh_from_db(); self.assertFalse(a.active)
        self.client.post(act, {'action': 'resume'}); a.refresh_from_db(); self.assertTrue(a.active)
        self.client.post(act, {'action': 'reset'}); a.refresh_from_db(); self.assertEqual((a.views, a.clicks), (0, 0))
        self.client.post(act, {'action': 'duplicate'})
        copy = LandingAd.objects.exclude(pk=a.pk).get()
        self.assertEqual((copy.title, copy.active), ('Promo (copy)', False))
        self.client.post(act, {'action': 'delete'})
        self.assertFalse(LandingAd.objects.filter(pk=a.pk).exists())

    def test_only_platform_staff(self):
        u = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456')
        Business.objects.create(user=u, business_name='Shop', owner_name='O', phone='1', trial_ends_at=timezone.now() + timedelta(days=3))
        self.client.force_login(u)
        self.assertNotEqual(self.client.get(reverse('platform_landing')).status_code, 200)
        self.client.post(reverse('platform_landing_save'), {'title': 'Hack'})
        self.assertFalse(LandingAd.objects.exists())


class WithoutPillow(Base):
    """The server image had no Pillow: uploading a picture gave "Server Error (500)". Now it is kept as uploaded."""
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def post(self, data, name):
        import io
        from django.core.files.uploadedfile import SimpleUploadedFile
        from unittest import mock
        with mock.patch.dict('sys.modules', {'PIL': None}):          # "from PIL import …" raises ImportError
            return self.client.post(reverse('platform_landing_save'), {
                'title': 'Trade on the TapTap Network', 'placement': 'spotlight', 'style': 'night', 'active': 'on', 'weight': '1',
                'image': SimpleUploadedFile(name, data)})

    def test_small_webp_png_jpeg_are_kept_as_they_are(self):
        webp = b'RIFF\x24\x00\x00\x00WEBPVP8 ' + b'\x00' * 40
        png = b'\x89PNG\r\n\x1a\n' + b'\x00' * 40
        jpg = b'\xff\xd8\xff\xe0' + b'\x00' * 40
        for data, name, kind in ((webp, 'a.webp', 'webp'), (png, 'a.png', 'png'), (jpg, 'a.jpg', 'jpeg')):
            r = self.post(data, name)
            self.assertEqual(r.status_code, 302)
            ad = LandingAd.objects.latest('pk')
            self.assertTrue(ad.image.startswith(f'data:image/{kind};base64,'))
            self.assertEqual(self.client.get(reverse('landing_ad_image', args=[ad.pk]))['Content-Type'], f'image/{kind}')

    def test_not_a_picture_or_too_big_is_a_message_not_a_500(self):
        n = LandingAd.objects.count()
        r = self.post(b'hello world, not an image', 'a.png')
        self.assertEqual(r.status_code, 302)
        r = self.post(b'\x89PNG\r\n\x1a\n' + b'\x00' * (3 * 1024 * 1024), 'big.png')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(LandingAd.objects.count(), n)
        msgs = [str(m) for m in self.client.get(reverse('platform_landing')).context['messages']]
        self.assertTrue(any('not a picture' in m for m in msgs), msgs)
        self.assertTrue(any('under 2 MB' in m for m in msgs), msgs)

    def test_any_other_failure_shows_the_reason(self):
        from unittest import mock
        with mock.patch('core.landing.save_ad', side_effect=RuntimeError('disk full')):
            r = self.client.post(reverse('platform_landing_save'), {'title': 'x'}, follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'RuntimeError: disk full')
