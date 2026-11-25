"""TapTap Market — owned until finished, seller locked out, hand-over, fair sharing on a network, Wave API (own and TapTap's)."""
import hashlib
import hmac
import io
import json
import random
import time
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from core import fit_tools, market as mk, market_owner as mo, market_wave as mw
from core.models import Business, Voucher, VoucherBatch, VoucherSale
from core.models_collaboration_buy import OnlineStorefront
from core.models_market import MarketDeal, MarketOrder, MarketPayout, MarketShop, MarketWallet
from core.tests.test_market import Base

PLATFORM_ENV = {'TAPTAP_WAVE_API_KEY': 'wave_sn_platform', 'TAPTAP_WAVE_WEBHOOK_SECRET': 'platform_whsec', 'TAPTAP_WAVE_CURRENCY': 'GMD'}


def sign(raw, secret):
    ts = str(int(time.time()))
    return f't={ts},v1=' + hmac.new(secret.encode(), ts.encode() + raw, hashlib.sha256).hexdigest()


def event(ref, amount, currency='GMD', tx='T_9'):
    return {'type': 'checkout.session.completed', 'data': {'client_reference': ref, 'amount': str(amount), 'currency': currency,
                                                            'transaction_id': tx, 'payment_status': 'succeeded', 'checkout_status': 'complete'}}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Ownership(Base):
    def sell_all(self, deal):
        out = []
        for i in range(deal.quantity):
            o = mk.create_order(deal.pk, 'qmoney', f'73{i:05d}')
            out.append(mk.confirm_order(o.pk, self.b_user))
        return out

    def test_seller_can_support_but_not_change(self):
        deal = self.active_deal(quantity='5', price='200')
        o = self.sell_all(deal)[0]
        v = o.voucher
        from core.voucher_bin import delete_vouchers, why_not_deletable
        from core.voucher_freeze import why_not_freezable
        self.assertIn('Awa Resellers', why_not_deletable(v))
        self.assertIn('TapTap Market', why_not_freezable(v))
        s = delete_vouchers(self.seller, [v], user=self.s_user, reason='tidy up')
        self.assertEqual(s['deleted'], 0)
        self.client.force_login(self.s_user)
        for name, pk in (('voucher_delete', v.pk), ('disable_voucher', v.pk), ('change_voucher_code', v.pk),
                         ('batch_delete', deal.batch_id), ('batch_assign', deal.batch_id)):
            r = self.client.post(reverse(name, args=[pk]), {'reason': 'x'})
            self.assertEqual(r.status_code, 302, name)
        v.refresh_from_db()
        self.assertEqual((v.status, v.deleted_at, v.code), ('active', None, o.voucher_code))
        self.assertTrue(VoucherBatch.objects.filter(pk=deal.batch_id).exists())
        own = Voucher.objects.create(business=self.seller, router=self.router, code='MYOWN01', plan_name='Day', price=50)
        self.assertIsNone(mo.lock_reason(own))                                   # the seller's own vouchers: untouched

    def test_finished_batch_moves_to_the_buyer_and_leaves_the_seller(self):
        deal = self.active_deal(quantity='5', price='200')
        orders = self.sell_all(deal)
        deal.refresh_from_db()
        self.assertEqual(deal.status, 'completed')
        mo.tick(payouts=False)
        self.assertEqual(MarketDeal.objects.get(pk=deal.pk).status, 'completed')    # vouchers still running: still the buyer's
        now = timezone.now()
        Voucher.objects.filter(batch=deal.batch).update(used_at=now - timedelta(days=2), expires_at=now - timedelta(days=1))
        mo.tick(payouts=False)
        deal.refresh_from_db()
        self.assertEqual(deal.status, 'handed_over')
        for o in MarketOrder.objects.filter(deal=deal):
            self.assertTrue(o.voucher_code and o.used_at and o.ends_at)          # the buyer keeps the history
        self.assertFalse(Voucher.objects.filter(batch=deal.batch).exists())        # gone from the seller's lists
        Voucher.all_objects.filter(batch=deal.batch).update(router_removal='removed')
        mo.tick(payouts=False)
        deal.refresh_from_db()
        self.assertIsNone(deal.batch_id)
        self.assertFalse(Voucher.all_objects.filter(code__in=[o.voucher_code for o in orders]).exists())
        self.assertTrue(VoucherSale.objects.filter(business=self.seller, reference=deal.reference).exists())  # the seller keeps their income
        self.client.force_login(self.b_user)
        page = self.client.get(reverse('market_deal', args=[deal.reference]))
        self.assertContains(page, 'Finished')
        self.assertContains(page, orders[0].voucher_code)

    def test_sell_by_closes_sales_but_keeps_ownership(self):
        deal = self.active_deal(quantity='5', price='200', sell_by=(timezone.localdate() + timedelta(days=3)).isoformat())
        self.sell_all(deal)[:0]
        MarketDeal.objects.filter(pk=deal.pk).update(status='active', issued=1)
        from core.models_market import MarketListing
        MarketListing.objects.filter(pk=deal.listing_id).update(sell_by=timezone.localdate() - timedelta(days=1))
        mo.tick(payouts=False)
        self.assertEqual(MarketDeal.objects.get(pk=deal.pk).status, 'completed')


class SharingANetwork(Base):
    def deals(self, n=3):
        out = []
        for i in range(n):
            u = User.objects.create_user(f'b{i}@x.gm', f'b{i}@x.gm', 'pw-123456')
            b = Business.objects.create(user=u, business_name=f'Buyer {i}', owner_name='B', phone='7', trial_ends_at=timezone.now() + timedelta(days=9))
            MarketWallet.objects.create(business=b, provider='wave', number=f'+22070000{i}0', holder=f'B{i}')
            l = self.listing(quantity='50', price='2000')
            d = mk.reserve(l.pk, b, u)
            mk.confirm_deal(d.pk, self.s_user)
            MarketDeal.objects.filter(pk=d.pk).update(confirmed_at=timezone.now() - timedelta(days=10 - i))
            out.append(d)
        return out

    def test_random_but_first_buyers_get_more(self):
        deals = self.deals()
        rnd = random.Random(42)
        counts = {d.pk: 0 for d in deals}
        for _ in range(3000):
            counts[mo.pick(self.seller, self.plan, rnd).pk] += 1
        c = [counts[d.pk] for d in deals]
        self.assertTrue(c[0] > c[1] > c[2] > 0, c)                               # everyone gets some, the first the most
        q = mo.queue(self.seller, self.plan.pk, 'Day', with_new=40)
        self.assertEqual([r['rank'] for r in q['rows']], [1, 2, 3])
        self.assertEqual((q['new']['rank'], q['ahead']), (4, 150))
        self.assertEqual(sum(r['share'] for r in q['rows']) + q['new']['share'] in (99, 100, 101), True)
        MarketShop.objects.update_or_create(business=self.seller, defaults={'own_share': 50})
        rnd = random.Random(1)
        mine = sum(1 for _ in range(1000) if mo.pick(self.seller, self.plan, rnd) is None)
        self.assertTrue(400 < mine < 600, mine)

    def test_customers_on_the_sellers_online_shop_pay_the_batch_owner(self):
        deals = self.deals(1)
        store = OnlineStorefront.objects.create(business=self.seller, enabled=True, router=self.router)
        r = self.client.post(reverse('online_buy_store', args=[store.public_slug]),
                             {'plan': self.plan.pk, 'provider': 'wave', 'phone': '7123456'})
        o = MarketOrder.objects.get()
        self.assertEqual((o.channel, o.deal_id, o.wallet.business.business_name), ('network', deals[0].pk, 'Buyer 0'))
        self.assertRedirects(r, reverse('market_order', args=[o.reference, o.access_token]), fetch_redirect_response=False)
        MarketDeal.objects.update(status='completed')                           # nothing left to share: the seller's own sale
        r = self.client.post(reverse('online_buy_store', args=[store.public_slug]),
                             {'plan': self.plan.pk, 'provider': 'wave', 'phone': '7123457'})
        self.assertEqual(MarketOrder.objects.count(), 1)

    def test_listing_shows_rank_share_and_advice(self):
        self.deals(2)
        l = self.listing(quantity='30', price='1200')
        self.client.force_login(self.b_user)
        page = self.client.get(reverse('market_listing', args=[l.code])).content.decode()
        self.assertIn('Batches already selling here', page)
        self.assertIn('you, if you buy', page)
        self.assertIn('#3', page)
        self.assertNotIn('Buyer 0', page)                                        # buyers are never named to each other
        ctx = fit_tools.FitContext(business=self.buyer, user=self.b_user, perms=frozenset(), settings=None)
        res = json.loads(fit_tools.call(ctx, 'market_advice', {'code': l.code}))
        self.assertEqual(res['if_you_buy']['rank'], 3)
        self.assertTrue(res['advice'])
        self.assertNotIn('Buyer 0', json.dumps(res))
        lst = json.loads(fit_tools.call(ctx, 'market_listings', {}))
        self.assertEqual(lst['listings'][0]['code'], l.code)
        self.assertNotIn('+2209', json.dumps(lst))


class WaveBatchPayment(Base):
    def test_buyer_pays_the_batch_on_the_sellers_wave_and_it_is_theirs(self):
        w = mk.save_wallet(self.seller, 'wave', {'number': '+2207000001', 'holder': 'Seller', 'mode': 'api', 'api_key': 'k',
                                                 'webhook_secret': 'seller_whsec', 'currency': 'GMD'})
        l = self.listing()
        deal = mk.reserve(l.pk, self.buyer, self.b_user)
        with mock.patch('core.online_buy.urlopen', return_value=FakeResponse(json.dumps(
                {'id': 'cos-1', 'wave_launch_url': 'https://pay.wave.com/c/cos-1'}).encode())):
            self.client.force_login(self.b_user)
            r = self.client.post(reverse('market_deal', args=[deal.reference]), {'action': 'wave'})
        self.assertEqual(r['Location'], 'https://pay.wave.com/c/cos-1')
        deal.refresh_from_db()
        self.assertEqual((deal.route, deal.currency, deal.provider_session_id), ('own', 'GMD', 'cos-1'))
        url = reverse('market_wave_webhook', args=[w.hook_token])
        bad = json.dumps(event(deal.reference, 1)).encode()
        self.assertEqual(self.client.post(url, bad, content_type='application/json', HTTP_WAVE_SIGNATURE=sign(bad, 'seller_whsec')).status_code, 400)
        self.assertEqual(MarketDeal.objects.get(pk=deal.pk).status, 'awaiting_payment')
        raw = json.dumps(event(deal.reference, 400)).encode()
        r = self.client.post(url, raw, content_type='application/json', HTTP_WAVE_SIGNATURE=sign(raw, 'seller_whsec'))
        self.assertEqual(r.status_code, 200, r.content)
        deal.refresh_from_db()
        self.assertEqual(deal.status, 'active')
        self.assertTrue(deal.batch_id)
        self.assertIn('Wave', VoucherSale.objects.get(business=self.seller, reference=deal.reference).notes)


@mock.patch.dict('os.environ', PLATFORM_ENV)
class TapTapWave(Base):
    def test_platform_checkout_and_instant_payout(self):
        self.assertTrue(mw.platform_ready())
        w = mk.save_wallet(self.buyer, 'wave', {'number': '7111111', 'holder': 'Awa Wave', 'mode': 'taptap'})
        self.assertEqual(w.route, 'platform')
        deal = self.active_deal()
        o = mk.create_order(deal.pk, 'wave', '7123456')
        self.assertEqual((o.route, o.currency), ('platform', 'GMD'))
        with self.assertRaisesMessage(mk.MarketError, 'automatically'):
            mk.claim_order(o.pk, 'TX-0001')
        raw = json.dumps(event(o.reference, 50)).encode()
        paid = FakeResponse(json.dumps({'id': 'pt-77', 'status': 'succeeded'}).encode())
        with mock.patch('core.market_wave.urlopen', return_value=paid) as up, self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(reverse('online_buy_webhook', args=['wave']), raw, content_type='application/json',
                                 HTTP_WAVE_SIGNATURE=sign(raw, 'platform_whsec'))
        self.assertEqual(r.status_code, 200, r.content)
        o.refresh_from_db()
        self.assertEqual(o.status, 'issued')
        p = MarketPayout.objects.get(order=o)
        self.assertEqual((p.status, p.mobile, p.amount, p.wave_id), ('succeeded', '+2207111111', Decimal('50'), 'pt-77'))
        body = json.loads(up.call_args[0][0].data)
        self.assertEqual((body['receive_amount'], body['mobile'], body['currency']), ('50', '+2207111111', 'GMD'))
        self.assertTrue(up.call_args[0][0].headers.get('Idempotency-key'))

    def test_failed_payout_is_retried_with_a_new_key_then_stops(self):
        w = mk.save_wallet(self.buyer, 'wave', {'number': '+2207111111', 'holder': 'Awa', 'mode': 'taptap'})
        p = mw.queue_payout(self.buyer, w, Decimal('50'), 'GMD')
        keys = []

        def fake(req, timeout=0):
            keys.append(req.headers.get('Idempotency-key'))
            return FakeResponse(json.dumps({'id': f'pt-{len(keys)}', 'status': 'failed', 'payout_error': {'error_message': 'no'}}).encode())
        with mock.patch('core.market_wave.urlopen', side_effect=fake):
            for _ in range(mw.MAX_ATTEMPTS + 2):
                mw.send_payout(p.pk)
        p.refresh_from_db()
        self.assertEqual(p.status, 'stuck')
        self.assertEqual(len(keys), mw.MAX_ATTEMPTS)
        self.assertEqual(len(set(keys)), len(keys))

    def test_a_timeout_never_pays_twice(self):
        w = mk.save_wallet(self.buyer, 'wave', {'number': '+2207111111', 'holder': 'Awa', 'mode': 'taptap'})
        p = mw.queue_payout(self.buyer, w, Decimal('50'), 'GMD')
        keys = []

        def flaky(req, timeout=0):
            keys.append(req.headers.get('Idempotency-key'))
            if len(keys) == 1:
                raise TimeoutError('slow')
            return FakeResponse(json.dumps({'id': 'pt-1', 'status': 'succeeded'}).encode())
        with mock.patch('core.market_wave.urlopen', side_effect=flaky):
            mw.send_payout(p.pk)
            MarketPayout.objects.filter(pk=p.pk).update(updated_at=timezone.now() - timedelta(minutes=5))
            mw.check_payouts()
        p.refresh_from_db()
        self.assertEqual(p.status, 'succeeded')
        self.assertEqual(keys[0], keys[1])                                       # asked again with the SAME key


class NotSwitchedOn(Base):
    def test_taptap_mode_needs_the_platform(self):
        with self.assertRaisesMessage(mk.MarketError, 'not switched on'):
            mk.save_wallet(self.buyer, 'wave', {'number': '+2207111111', 'holder': 'Awa', 'mode': 'taptap'})
