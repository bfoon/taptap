"""TapTap Market (core/market.py, core/market_diligence.py): listings, due diligence with privacy, deals, customer orders."""
import hashlib
import hmac
import json
import time
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import fit_tools, market as mk, market_diligence as md
from core.models import (Business, Expense, Router, UsageRecord, Voucher, VoucherPlan, VoucherSale)
from core.models_market import MarketDeal, MarketListing, MarketOrder, MarketShop, MarketWallet, RouterOutage
from core.models_team import TeamMember

CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'market'}}


@override_settings(CACHES=CACHE, AUTH_EMAIL_OTP=False)
class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.now = timezone.now()
        self.s_user = User.objects.create_user('seller@x.gm', 'seller@x.gm', 'pw-123456')
        self.seller = Business.objects.create(user=self.s_user, business_name='Kerr Sering Wi-Fi', owner_name='S', phone='7000001',
                                              trial_ends_at=self.now + timedelta(days=30), feature_tier='pro', wifi_ssid='KerrSering-WiFi')
        Business.objects.filter(pk=self.seller.pk).update(created_at=self.now - timedelta(days=200))
        self.seller.refresh_from_db()
        self.router = Router.objects.create(business=self.seller, name='Hap', ip_address='10.9.8.7', username='u', password='p',
                                            status='Online', connection_mode='agent')
        Router.objects.filter(pk=self.router.pk).update(created_at=self.now - timedelta(days=200))
        self.router.refresh_from_db()
        self.plan = VoucherPlan.objects.create(business=self.seller, name='Day', price=50, duration_minutes=1440)
        self.b_user = User.objects.create_user('buyer@x.gm', 'buyer@x.gm', 'pw-123456')
        self.buyer = Business.objects.create(user=self.b_user, business_name='Awa Resellers', owner_name='A', phone='7000002',
                                             trial_ends_at=self.now + timedelta(days=30), feature_tier='free')
        MarketWallet.objects.create(business=self.seller, provider='wave', number='+2207000001', holder='Seller Wave')
        from core.models_market import MarketSeller
        MarketSeller.objects.create(business=self.seller, status='approved', level='partner', contact_name='S', contact_phone='7',
                                    town='Brikama', operating_since='2022', how_sell='shop', support_plan='WhatsApp',
                                    approved_at=self.now)
        MarketWallet.objects.create(business=self.buyer, provider='qmoney', number='+2207000002', holder='Awa Q')

    def listing(self, **kw):
        data = {'router': str(self.router.pk), 'plan': str(self.plan.pk), 'quantity': '10', 'price': '400', 'area': 'Brikama market',
                'precision': 'rounded'}
        data.update(kw)
        return mk.create_listing(self.seller, self.s_user, data)

    def day(self, d):
        """A moment on the local day ``d`` days ago — never across midnight, whatever time the tests run."""
        if d == 0:
            return self.now
        return timezone.make_aware(timezone.datetime.combine(timezone.localdate(self.now) - timedelta(days=d), timezone.datetime.min.time())) + timedelta(hours=12)

    def sales(self, days=40, per_day=3):
        for d in range(days):
            for k in range(per_day):
                VoucherSale.objects.create(business=self.seller, plan_name='Day', amount=50, payment_method='wave' if k else 'cash',
                                           customer_name='Secret Customer', customer_phone='+2209999999', voucher_code=f'SECRET{d}{k}',
                                           sold_at=self.day(d))

    def active_deal(self, **kw):
        l = self.listing(**kw)
        deal = mk.reserve(l.pk, self.buyer, self.b_user)
        mk.claim_deal(deal.pk, 'wave', 'TX-123456')
        return mk.confirm_deal(deal.pk, self.s_user)


class Listings(Base):
    def test_rules(self):
        with self.assertRaisesMessage(mk.MarketError, 'cannot be more'):
            self.listing(price='501')
        with self.assertRaisesMessage(mk.MarketError, 'between 5'):
            self.listing(quantity='2')
        with self.assertRaisesMessage(mk.MarketError, 'area'):
            self.listing(area='')
        MarketWallet.objects.filter(business=self.seller).update(active=False)
        with self.assertRaisesMessage(mk.MarketError, 'wallet'):
            self.listing()
        MarketWallet.objects.filter(business=self.seller).update(active=True)
        l = self.listing()
        self.assertEqual((l.face_value, l.margin, l.margin_pct, l.min_retail), (Decimal('500'), Decimal('100'), 20.0, Decimal('50')))
        self.assertTrue(l.code.startswith('MKT-'))

    def test_free_plan_cannot_sell_but_can_buy(self):
        self.client.force_login(self.b_user)
        self.assertEqual(self.client.get(reverse('market')).status_code, 200)
        r = self.client.post(reverse('market_sell'), {'plan': '1'})
        self.assertNotEqual(MarketListing.objects.filter(business=self.buyer).count(), 1)
        self.assertIn(r.status_code, (302, 403))


class Diligence(Base):
    def test_figures_privacy_and_score(self):
        self.sales()
        VoucherSale.objects.create(business=self.seller, plan_name='Market batch', amount=99999, reference='MKT-DXXXXXXX',
                                   sold_at=self.now - timedelta(days=1))
        for d in range(35):
            n = 2 if d == 3 else 8
            for i in range(n):
                UsageRecord.objects.create(business=self.seller, router=self.router, username=f'u{i}', mac_address=f'AA:BB:CC:00:00:{i:02d}',
                                           hour=self.day(d))
        RouterOutage.objects.create(router=self.router, started_at=self.now - timedelta(days=5, hours=3),
                                    ended_at=self.now - timedelta(days=5, hours=2), seconds=3600, why='Internet line')
        l = self.listing(precision='exact')
        dd = md.build(l)
        self.assertEqual(dd['sales']['last30_text'], 'D4,500')                   # 30 days × 3 × 50, market income left out
        self.assertEqual(dd['sales']['plan_daily'], 3.0)
        self.assertEqual(dd['users']['source'], 'devices')
        self.assertEqual(dd['users']['avg'] > 0, True)
        few = [x for x in dd['users']['per_day'] if x['few']]
        self.assertEqual(len(few), 1)
        self.assertIsNone(few[0]['v'])                                           # 2 devices: shown as "fewer than 5"
        self.assertEqual(dd['uptime']['outages'], 1)
        self.assertLess(dd['uptime']['pct_30'], 100)
        self.assertEqual(dd['forecast']['days'][100], 4)                         # 10 vouchers at 3 a day
        self.assertIn(dd['score']['grade'], 'ABCDE')
        json.dumps(dd)                                                           # can be saved with the deal

    def test_rounded_and_index_hide_money(self):
        self.sales(days=10, per_day=1)
        VoucherSale.objects.create(business=self.seller, plan_name='Day', amount=37, sold_at=self.now - timedelta(hours=2))
        r = md.build(self.listing(precision='rounded'))
        self.assertTrue(r['sales']['last30_text'].startswith('≈ D'))
        self.assertTrue(all(x['v'] is None or x['v'] % r['sales']['step'] == 0 for x in r['sales']['per_day']))
        i = md.build(self.listing(precision='index'))
        self.assertEqual(i['sales']['last30_text'], 'hidden')
        self.assertTrue(all(x['v'] is None for x in i['sales']['per_day']))

    def test_page_never_shows_private_details(self):
        self.sales(days=5)
        UsageRecord.objects.create(business=self.seller, router=self.router, username='SECRETUSER', mac_address='DE:AD:BE:EF:00:01',
                                   hour=self.now - timedelta(hours=3))
        Voucher.objects.create(business=self.seller, router=self.router, code='VCODE777', plan_name='Day', price=50,
                               customer_phone='+2208888888', customer_name='Binta Private')
        l = self.listing()
        self.client.force_login(self.b_user)
        page = self.client.get(reverse('market_listing', args=[l.code])).content.decode()
        for secret in ('Secret Customer', '+2209999999', 'SECRET0', 'SECRETUSER', 'DE:AD:BE:EF', 'VCODE777', '+2208888888',
                       'Binta Private', '10.9.8.7', 'Kerr Sering Wi-Fi', 'Hap'):
            self.assertNotIn(secret, page, secret)
        self.assertIn('Brikama market', page)
        self.assertEqual(MarketListing.objects.get(pk=l.pk).views, 1)

    def test_outages_are_kept_when_a_router_comes_back(self):
        from core import uptime_guard
        uptime_guard.went_down(self.router, self.now - timedelta(minutes=20), 'internet line down 10.0.0.1')
        uptime_guard.came_back(self.router, via='TapTap Link')
        o = RouterOutage.objects.get(router=self.router)
        self.assertGreaterEqual(o.seconds, 1190)
        self.assertEqual(o.why, 'Internet line')                                 # never the raw text with its address
        uptime_guard.went_down(self.router, timezone.now() - timedelta(seconds=10), 'blip')
        uptime_guard.came_back(self.router)
        self.assertEqual(RouterOutage.objects.count(), 1)                        # a blip is not an outage


class Deals(Base):
    def test_full_purchase(self):
        l = self.listing()
        with self.assertRaisesMessage(mk.MarketError, 'own listing'):
            mk.reserve(l.pk, self.seller, self.s_user)
        deal = mk.reserve(l.pk, self.buyer, self.b_user)
        self.assertEqual(MarketListing.objects.get(pk=l.pk).status, 'reserved')
        self.assertIn('score', deal.diligence)
        other = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456')
        ob = Business.objects.create(user=other, business_name='Other', owner_name='O', phone='1', trial_ends_at=self.now + timedelta(days=3))
        with self.assertRaisesMessage(mk.MarketError, 'right now'):
            mk.reserve(l.pk, ob, other)
        with self.assertRaisesMessage(mk.MarketError, 'transaction ID'):
            mk.claim_deal(deal.pk, 'wave', '1')
        with self.assertRaisesMessage(mk.MarketError, 'wallet'):
            mk.claim_deal(deal.pk, 'afrimoney', 'TX-1234')
        mk.claim_deal(deal.pk, 'wave', 'TX-123456')
        mk.confirm_deal(deal.pk, self.s_user)
        deal.refresh_from_db()
        self.assertEqual(deal.status, 'active')
        self.assertEqual(deal.batch.business, self.seller)
        self.assertEqual(MarketListing.objects.get(pk=l.pk).status, 'sold')
        sale = VoucherSale.objects.get(business=self.seller, reference=deal.reference)
        self.assertEqual((sale.amount, sale.payment_method), (Decimal('400'), 'wave'))
        self.assertTrue(Expense.objects.filter(business=self.buyer, reference=deal.reference, amount=400).exists())
        self.assertTrue(MarketShop.objects.filter(business=self.buyer).exists())
        self.assertNotIn('400', md.build(l)['sales']['last30_text'])             # market income is not in the due diligence

    def test_deleting_the_router_never_breaks(self):
        deal = self.active_deal()
        o = mk.create_order(deal.pk, 'qmoney', '7123456')
        open_l = self.listing()
        self.router.delete()                                                      # works: listings do not block it
        open_l.refresh_from_db()
        self.assertIsNone(open_l.router_id)
        self.assertFalse(md.build(open_l)['uptime']['online_now'])
        with self.assertRaisesMessage(mk.MarketError, 'cannot be sold'):
            mk.reserve(open_l.pk, self.buyer, self.b_user)
        o = mk.confirm_order(o.pk, self.b_user)
        self.assertEqual((o.status, o.voucher_id), ('review', None))              # held for a person, no voucher anywhere
        self.client.force_login(self.b_user)
        self.assertEqual(self.client.get(reverse('market_listing', args=[open_l.code])).status_code, 200)

    def test_reject_cancel_and_expiry_reopen_the_listing(self):
        l = self.listing()
        d1 = mk.reserve(l.pk, self.buyer, self.b_user)
        mk.claim_deal(d1.pk, 'wave', 'TX-9999')
        mk.reject_deal(d1.pk, 'nothing arrived')
        self.assertEqual(MarketListing.objects.get(pk=l.pk).status, 'open')
        d2 = mk.reserve(l.pk, self.buyer, self.b_user)
        mk.cancel_deal(d2.pk)
        self.assertEqual(MarketListing.objects.get(pk=l.pk).status, 'open')
        d3 = mk.reserve(l.pk, self.buyer, self.b_user)
        MarketDeal.objects.filter(pk=d3.pk).update(created_at=self.now - timedelta(hours=49))
        mk.sweep()
        self.assertEqual(MarketDeal.objects.get(pk=d3.pk).status, 'expired')
        self.assertEqual(MarketListing.objects.get(pk=l.pk).status, 'open')


class Orders(Base):
    def test_customer_buys_and_gets_a_voucher_on_the_sellers_router(self):
        deal = self.active_deal(quantity='5', price='200')
        with self.assertRaisesMessage(mk.MarketError, 'not available'):
            mk.create_order(deal.pk, 'wave', '7123456')                         # the buyer has no Wave
        o = mk.create_order(deal.pk, 'qmoney', '7123456', 'Lamin')
        self.assertEqual((o.amount, o.status), (Decimal('50'), 'pending'))
        mk.claim_order(o.pk, 'QM-77777')
        o2 = mk.create_order(deal.pk, 'qmoney', '7123457')
        with self.assertRaisesMessage(mk.MarketError, 'already used'):
            mk.claim_order(o2.pk, 'QM-77777')
        o = mk.confirm_order(o.pk, self.b_user)
        v = o.voucher
        self.assertEqual((o.status, v.business, v.router, v.plan_name), ('issued', self.seller, self.router, 'Day'))
        self.assertIsNotNone(v.sold_at)
        self.assertEqual(v.customer_phone, '')                                    # the buyer's customers stay the buyer's
        self.assertEqual(MarketDeal.objects.get(pk=deal.pk).issued, 1)
        self.assertTrue(VoucherSale.objects.filter(business=self.buyer, reference=o.reference, amount=50, payment_method='qmoney').exists())
        self.assertFalse(VoucherSale.objects.filter(business=self.seller, voucher=v).exists())
        from core.finance import mark_activated
        mark_activated(v)                                                         # first use on the router: no sale for the seller
        self.assertFalse(VoucherSale.objects.filter(business=self.seller, voucher=v).exists())

    def test_stock_limit_expiry_and_sold_out(self):
        deal = self.active_deal(quantity='5', price='200')
        orders = [mk.create_order(deal.pk, 'qmoney', f'71234{i:02d}') for i in range(3)]
        orders += [mk.create_order(deal.pk, 'qmoney', f'72234{i:02d}') for i in range(2)]
        with self.assertRaisesMessage(mk.MarketError, 'Sold out'):
            mk.create_order(deal.pk, 'qmoney', '7999999')
        MarketOrder.objects.filter(pk=orders[0].pk).update(created_at=self.now - timedelta(minutes=31))
        mk.sweep()
        self.assertEqual(mk.available(MarketDeal.objects.get(pk=deal.pk)), 1)
        late = MarketOrder.objects.get(pk=orders[0].pk)
        self.assertEqual(late.status, 'expired')
        for o in orders[1:]:
            mk.confirm_order(o.pk, self.b_user)
        last = mk.create_order(deal.pk, 'qmoney', '7555555')
        mk.confirm_order(last.pk, self.b_user)
        deal.refresh_from_db()
        self.assertEqual((deal.issued, deal.status), (5, 'completed'))
        with self.assertRaisesMessage(mk.MarketError, 'sold out'):
            mk.claim_order(late.pk, 'LATE-1234')                                 # paid late, but nothing left

    def test_late_payment_still_counts_while_stock_lasts(self):
        deal = self.active_deal()
        o = mk.create_order(deal.pk, 'qmoney', '7123456')
        MarketOrder.objects.filter(pk=o.pk).update(created_at=self.now - timedelta(minutes=40))
        mk.sweep()
        mk.claim_order(o.pk, 'QM-LATE01')
        self.assertEqual(mk.confirm_order(o.pk, self.b_user).status, 'issued')

    def test_max_open_orders_per_phone(self):
        deal = self.active_deal(quantity='10')
        for _ in range(mk.MAX_OPEN_PER_PHONE):
            mk.create_order(deal.pk, 'qmoney', '7000000')
        with self.assertRaisesMessage(mk.MarketError, 'waiting'):
            mk.create_order(deal.pk, 'qmoney', '7000000')


class WaveWebhook(Base):
    def setUp(self):
        super().setUp()
        self.w = mk.save_wallet(self.buyer, 'wave', {'number': '+2207111111', 'holder': 'Awa Wave', 'mode': 'api', 'api_key': 'wave_sn_k',
                                                     'webhook_secret': 'whsec', 'currency': 'gmd'})
        self.assertTrue(self.w.automatic)
        self.assertNotIn('whsec', self.w.webhook_secret)                          # stored encrypted

    def post(self, body, secret='whsec'):
        raw = json.dumps(body).encode()
        ts = str(int(time.time()))
        sig = hmac.new(secret.encode(), ts.encode() + raw, hashlib.sha256).hexdigest()
        return self.client.post(reverse('market_wave_webhook', args=[self.w.hook_token]), raw, content_type='application/json',
                                HTTP_WAVE_SIGNATURE=f't={ts},v1={sig}')

    def event(self, o, amount='50'):
        return {'type': 'checkout.session.completed', 'data': {'client_reference': o.reference, 'amount': amount, 'currency': 'GMD',
                                                                'transaction_id': 'T_1', 'payment_status': 'succeeded', 'checkout_status': 'complete'}}

    def test_signed_payment_issues_the_voucher(self):
        deal = self.active_deal()
        o = mk.create_order(deal.pk, 'wave', '7123456')
        self.assertEqual(o.currency, 'GMD')
        self.assertEqual(self.post(self.event(o), secret='wrong').status_code, 401)
        r = self.post(self.event(o))
        self.assertEqual(r.status_code, 200, r.content)
        o.refresh_from_db()
        self.assertEqual(o.status, 'issued')
        self.assertEqual(self.post(self.event(o)).json()['status'], 'issued')    # retries are harmless
        self.assertEqual(Voucher.objects.filter(market_order__isnull=False).count(), 1)

    def test_wrong_amount_is_held(self):
        deal = self.active_deal()
        o = mk.create_order(deal.pk, 'wave', '7123456')
        self.assertEqual(self.post(self.event(o, amount='5')).status_code, 400)
        o.refresh_from_db()
        self.assertEqual((o.status, o.voucher_id), ('review', None))
        with self.assertRaisesMessage(mk.MarketError, 'automatically'):
            mk.claim_order(mk.create_order(deal.pk, 'wave', '7000001').pk, 'TX-1111')


class Pages(Base):
    def test_hub_listing_deal_and_public_pages(self):
        self.sales(days=3)
        deal = self.active_deal()
        open_l = self.listing(quantity='20', price='900')
        self.client.force_login(self.b_user)
        for tab in ('browse', 'stock', 'selling', 'setup'):
            self.assertEqual(self.client.get(reverse('market') + f'?tab={tab}').status_code, 200, tab)
        self.assertContains(self.client.get(reverse('market') + '?tab=browse'), open_l.code)
        self.assertEqual(self.client.get(reverse('market_deal', args=[deal.reference])).status_code, 200)
        self.client.force_login(self.s_user)
        self.assertEqual(self.client.get(reverse('market_deal', args=[deal.reference])).status_code, 200)
        self.assertEqual(self.client.get(reverse('market_listing', args=[open_l.code])).status_code, 200)
        stranger = User.objects.create_user('z@x.gm', 'z@x.gm', 'pw-123456')
        Business.objects.create(user=stranger, business_name='Z', owner_name='Z', phone='1', trial_ends_at=self.now + timedelta(days=3))
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(reverse('market_deal', args=[deal.reference])).status_code, 404)
        self.client.logout()
        shop = MarketShop.objects.get(business=self.buyer)
        page = self.client.get(reverse('market_shop', args=[shop.slug]))
        self.assertContains(page, 'KerrSering-WiFi')
        r = self.client.post(reverse('market_shop', args=[shop.slug]), {'deal': deal.pk, 'provider': 'qmoney', 'phone': '7123456'})
        o = MarketOrder.objects.get()
        self.assertRedirects(r, reverse('market_order', args=[o.reference, o.access_token]), fetch_redirect_response=False)
        url = reverse('market_order', args=[o.reference, o.access_token])
        self.assertContains(self.client.get(url), '+2207000002')
        self.client.post(url, {'payment_ref': 'QM-555555'})
        self.assertEqual(self.client.get(url + '?json=1').json()['status'], 'claimed')
        self.assertEqual(self.client.get(reverse('market_order', args=[o.reference, 'wrong'])).status_code, 404)
        self.client.force_login(self.b_user)
        self.client.post(reverse('market_order_action', args=[o.reference]), {'action': 'confirm'})
        code = self.client.get(url + '?json=1').json()['code']
        self.assertTrue(code)
        self.client.logout()
        self.assertContains(self.client.get(url), code)

    def test_staff_cannot_spend_or_promise(self):
        l = self.listing()
        staff = User.objects.create_user('st@x.gm', 'st@x.gm', 'pw-123456')
        TeamMember.objects.create(business=self.buyer, user=staff, role='admin')
        Business.objects.filter(pk=self.buyer.pk).update(feature_tier='pro')
        self.client.force_login(staff)
        self.client.post(reverse('market_listing_reserve', args=[l.code]), {'agree': '1'})
        self.assertFalse(MarketDeal.objects.exists())

    def test_fit_tool(self):
        deal = self.active_deal()
        ctx = fit_tools.FitContext(business=self.buyer, user=self.b_user, perms=frozenset({'finance.view'}), settings=None)
        res = json.loads(fit_tools.call(ctx, 'market_overview', {}))
        self.assertEqual(res['batches_bought'][0]['reference'], deal.reference)
        self.assertEqual(res['batches_bought'][0]['left'], 10)
