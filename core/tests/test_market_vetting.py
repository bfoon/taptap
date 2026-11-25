"""TapTap vets Market sellers (core/market_vetting.py): apply, automatic checks, decisions, limits, suspension, watch."""
import importlib
import json
from datetime import timedelta

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from core import fit_tools, market as mk, market_vetting as mv
from core.models_market import MarketDeal, MarketListing, MarketSeller, MarketVettingEvent, RouterOutage
from core.tests.test_market import Base

PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
       b'\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82')
FORM = {'contact_name': 'Musa', 'contact_phone': '7000001', 'town': 'Brikama', 'operating_since': '2022', 'sites': '2',
        'how_sell': 'A shop sells printed vouchers', 'support_plan': 'WhatsApp 7000001, 8am to 10pm', 'agree': '1'}


class Vetting(Base):
    def setUp(self):
        super().setUp()
        MarketSeller.objects.filter(business=self.seller).delete()      # start unvetted
        self.admin = User.objects.create_superuser('root@x.gm', 'root@x.gm', 'pw-123456')

    def apply(self, **extra):
        self.client.force_login(self.s_user)
        data = {**FORM, **extra}
        if 'site_photo' not in extra:
            data['site_photo'] = SimpleUploadedFile('site.png', PNG, content_type='image/png')
        return self.client.post(reverse('market_apply'), data)

    def test_cannot_list_before_vetting_then_apply_and_get_approved(self):
        with self.assertRaisesMessage(mk.MarketError, 'vetted by TapTap'):
            self.listing()
        self.client.force_login(self.s_user)
        page = self.client.get(reverse('market') + '?tab=selling')
        self.assertContains(page, 'Become a vetted seller')
        self.assertContains(page, 'What TapTap measures')
        self.client.post(reverse('market_apply'), {**FORM})                     # no photo
        self.assertFalse(MarketSeller.objects.exists())
        self.apply()
        s = MarketSeller.objects.get(business=self.seller)
        self.assertEqual(s.status, 'applied')
        self.assertEqual(len(s.checks), 10)
        self.assertTrue(s.site_photo.startswith('data:image/png;base64,'))
        self.assertTrue(any('Market seller to vet' in m.subject for m in mail.outbox))
        self.assertContains(self.client.get(reverse('market') + '?tab=selling'), 'Waiting for TapTap')
        # the platform
        self.assertEqual(self.client.get(reverse('platform_market')).status_code, 403)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse('platform_market')), 'Kerr Sering Wi-Fi')
        self.assertContains(self.client.get(reverse('platform_market_seller', args=[s.pk])), 'Automatic checks today')
        f = self.client.get(reverse('platform_market_file', args=[s.pk, 'photo']))
        self.assertEqual((f.status_code, f['Content-Type'], f.content), (200, 'image/png', PNG))
        self.assertEqual(self.client.get(reverse('platform_market_file', args=[s.pk, 'document'])).status_code, 404)
        self.client.post(reverse('platform_market_action', args=[s.pk]), {'action': 'approve', 'level': 'starter'})
        s.refresh_from_db()
        self.assertEqual((s.status, s.level, s.reviewed_by), ('approved', 'starter', self.admin))
        self.assertEqual(list(s.events.values_list('action', flat=True)), ['approve', 'applied'])
        # starter limits: 300 per batch, 2 listings at a time
        with self.assertRaisesMessage(mk.MarketError, '300 vouchers'):
            self.listing(quantity='301', price='100')
        self.listing()
        self.listing()
        with self.assertRaisesMessage(mk.MarketError, '2 listings'):
            self.listing()
        mv.decide(s, self.admin, 'level', level='trusted')
        self.listing()

    def test_ask_reject_and_reapply_date(self):
        self.apply()
        s = MarketSeller.objects.get()
        with self.assertRaisesMessage(mv.VettingError, 'Say what'):
            mv.decide(s, self.admin, 'info')
        mv.decide(s, self.admin, 'info', note='Send a clearer photo please')
        page = self.client.get(reverse('market') + '?tab=selling')
        self.assertContains(page, 'Send a clearer photo please')
        self.assertContains(page, 'Send again to TapTap')
        self.apply(site_photo=SimpleUploadedFile('site.png', PNG, content_type='image/png'))
        s.refresh_from_db()
        self.assertEqual(s.status, 'applied')
        mv.decide(s, self.admin, 'reject', note='Network too young', days=30)
        with self.assertRaisesMessage(mv.VettingError, 'apply again from'):
            mv.apply(self.seller, self.s_user, FORM, {})
        MarketSeller.objects.filter(pk=s.pk).update(reapply_after=timezone.localdate())
        mv.apply(self.seller, self.s_user, FORM, {})                         # the old photo still counts
        self.assertEqual(MarketSeller.objects.get().status, 'applied')

    def test_files_are_checked(self):
        self.apply(site_photo=SimpleUploadedFile('x.html', b'<script>', content_type='text/html'))
        self.assertFalse(MarketSeller.objects.exists())

    def test_suspension_hides_listings_but_never_takes_from_buyers(self):
        MarketSeller.objects.create(business=self.seller, status='approved', level='partner', contact_name='S', contact_phone='7',
                                    town='T', operating_since='1', how_sell='x', support_plan='y')
        deal = self.active_deal()
        open_l = self.listing()
        s = MarketSeller.objects.get()
        mv.decide(s, self.admin, 'suspend', note='Too many complaints')
        self.client.force_login(self.b_user)
        self.assertNotContains(self.client.get(reverse('market')), open_l.code)
        self.assertEqual(self.client.get(reverse('market_listing', args=[open_l.code])).status_code, 404)
        with self.assertRaisesMessage(mk.MarketError, 'not vetted'):
            mk.reserve(open_l.pk, self.buyer, self.b_user)
        o = mk.create_order(deal.pk, 'qmoney', '7123456')                       # the buyer's batch keeps selling
        self.assertEqual(mk.confirm_order(o.pk, self.b_user).status, 'issued')
        self.assertEqual(MarketListing.objects.get(pk=open_l.pk).status, 'open')  # nothing deleted
        ctx = fit_tools.FitContext(business=self.buyer, user=self.b_user, perms=frozenset(), settings=None)
        self.assertEqual(json.loads(fit_tools.call(ctx, 'market_listings', {}))['listings'], [])
        mv.decide(s, self.admin, 'reinstate')
        self.assertContains(self.client.get(reverse('market')), open_l.code)

    def test_daily_watch_flags_a_slipping_seller(self):
        s = MarketSeller.objects.create(business=self.seller, status='approved', level='starter', contact_name='S', contact_phone='7',
                                        town='T', operating_since='1', how_sell='x', support_plan='y')
        RouterOutage.objects.create(router=self.router, started_at=self.now - timedelta(days=3), ended_at=self.now - timedelta(days=1),
                                    seconds=172800, why='Internet line')
        cache.delete('tt:mkt:vet-watch')
        self.assertEqual(mv.watch_sellers(), 1)
        s.refresh_from_db()
        self.assertTrue(s.watch)
        self.assertIn('Uptime', s.watch_note)
        self.assertEqual(s.status, 'approved')                                  # a person decides, not the robot
        self.assertEqual(mv.watch_sellers(), 0)                                 # once a day

    def test_existing_sellers_are_grandfathered(self):
        MarketSeller.objects.create(business=self.seller, status='approved', level='partner', contact_name='S', contact_phone='7',
                                    town='T', operating_since='1', how_sell='x', support_plan='y')
        self.listing()
        MarketSeller.objects.all().delete()
        mig = importlib.import_module('core.migrations.0135_market_vetting')
        mig.grandfather(django_apps, None)
        s = MarketSeller.objects.get(business=self.seller)
        self.assertEqual((s.status, s.level), ('approved', 'starter'))
        self.assertTrue(MarketVettingEvent.objects.filter(seller=s, action='approved').exists())
        self.assertFalse(MarketDeal.objects.exists())
