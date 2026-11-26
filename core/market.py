"""TapTap Market — sell batches of vouchers that keep running on your network; buyers sell them on with mobile money.

Two sides
---------
* **Seller** (a network owner): lists a batch — a plan, how many, the price of the whole batch, the router the vouchers
  run on and how much the due diligence reveals (core/market_diligence.py). The vouchers are only made when a customer
  pays, so no code exists or leaks before it is sold.
* **Buyer** (any TapTap account): reads the due diligence, reserves the batch, pays the seller's wallet, and once the
  seller confirms, sells those vouchers on their own public page (/p/market/<slug>/). Customers pay the BUYER's wallet
  directly — Wave, QMoney or Afrimoney, mobile money only — and get a voucher that works on the seller's network.

How money moves (core/market_wave.py)
-------------------------------------
* Batch: buyer → seller. With Wave API (the seller's own key, or TapTap's Wave with an instant payout to the seller's
  number) the buyer pays on Wave and the batch is theirs at once; otherwise the buyer types the transaction ID and the
  seller confirms it arrived.
* Voucher: customer → the batch OWNER's wallet, the same three ways. Customers come from the owner's shop page and from
  the seller's own Online sales page (core/market_owner.py shares them between the batches on that network).
  A browser coming back from a payment page never releases a voucher.

Ownership lasts until the batch is FINISHED (every voucher used up), then it is handed over and leaves the seller
completely — see core/market_owner.py.

Lifecycle
---------
    listing: open ──reserve──▶ reserved ──confirmed──▶ sold          (withdrawn by the seller while open)
    deal:    awaiting_payment ──"I paid"──▶ claimed ──seller confirms──▶ active ──last sold──▶ completed ──last used──▶ handed_over
                 │   └──(Wave API, amount verified)─────────────────────────▲
                 └── cancelled (buyer) / expired (48 h)    └── rejected (money not received): the listing opens again
    order:   pending ──"I paid"──▶ claimed ──buyer confirms──▶ paid ──▶ issued
                 └── expired (30 min, stock released)       └── rejected
                 └── (Wave API) webhook, amount verified ──▶ paid ──▶ issued

Books: on confirmation the seller's Finance gets the batch as a sale and the buyer's Finance gets it as an expense;
each voucher the buyer sells is the buyer's sale. Nothing is counted twice: market vouchers are marked sold on the
seller's side (no automatic sale on first use), and due diligence leaves market money out (references ``MKT-``).
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models_market import WALLET_PROVIDERS, MarketDeal, MarketListing, MarketOrder, MarketShop, MarketWallet

logger = logging.getLogger('taptap.market')

DEAL_PAY_HOURS = 48
ORDER_PAY_MINUTES = 30
MAX_OPEN_PER_PHONE = 3
ORDERS_PER_IP_HOUR = 12
RETAIL_CAP = Decimal('2')          # a buyer may sell at most at twice the seller's own price
CENT = Decimal('0.01')
PROVIDERS = dict(WALLET_PROVIDERS)


class MarketError(ValueError):
    pass


def _dec(v, field='amount'):
    try:
        d = Decimal(str(v).replace(',', '').strip()).quantize(CENT)
    except (InvalidOperation, ValueError):
        raise MarketError(f'Enter a valid {field}.')
    if d <= 0:
        raise MarketError(f'The {field} must be more than 0.')
    return d


def _money(business, v):
    v = Decimal(str(v or 0))
    return f'{business.currency or "D"}{v:,.0f}' if v == v.to_integral_value() else f'{business.currency or "D"}{v:,.2f}'


def _site(request):
    from .online_buy import _site_url
    return _site_url(request)


def _tell(business, subject, body, link, key=None):
    try:
        from .notify import notify
        notify(business, 'market', subject, body, link=link, key=key)
    except Exception:
        logger.exception('market notification')


def _log(business, what, details):
    try:
        from .utils import log
        log(business, what, details[:255])
    except Exception:
        pass


# ─────────────────────────────── housekeeping ───────────────────────────────

def sweep(now=None):
    """Expire unpaid deals and orders (cheap; runs when market pages open)."""
    now = now or timezone.now()
    stale = MarketDeal.objects.filter(status='awaiting_payment', created_at__lt=now - timedelta(hours=DEAL_PAY_HOURS))
    for deal in stale.select_related('listing'):
        with transaction.atomic():
            n = MarketDeal.objects.filter(pk=deal.pk, status='awaiting_payment').update(status='expired', closed_at=now)
            if n:
                MarketListing.objects.filter(pk=deal.listing_id, status='reserved').update(status='open', updated_at=now)
    MarketOrder.objects.filter(status='pending', created_at__lt=now - timedelta(minutes=ORDER_PAY_MINUTES)) \
        .update(status='expired', updated_at=now)


def available(deal):
    """Vouchers of an active deal still free to sell (open orders hold one each)."""
    if deal.status != 'active':
        return 0
    holding = deal.orders.filter(status__in=MarketOrder.HOLDING).count()
    return max(0, deal.quantity - deal.issued - holding)


# ─────────────────────────────── wallets ───────────────────────────────

def wallets(business, active=True):
    qs = business.market_wallets.all()
    return qs.filter(active=True) if active else qs


def save_wallet(business, provider, data):
    from .tunnel import encrypt_secret
    if provider not in PROVIDERS:
        raise MarketError('Choose Wave, QMoney or Afrimoney.')
    number = re.sub(r'[^\d+]', '', str(data.get('number') or ''))
    holder = str(data.get('holder') or '').strip()[:120]
    w = business.market_wallets.filter(provider=provider).first() or MarketWallet(business=business, provider=provider)
    if not number:
        if w.pk:
            w.active = False
            w.save(update_fields=['active'])
        return w
    if len(number.lstrip('+')) < 7:
        raise MarketError(f'The {PROVIDERS[provider]} number looks too short.')
    if not holder:
        raise MarketError(f'Type the name the {PROVIDERS[provider]} wallet shows, so payers know it is right.')
    link = str(data.get('pay_link') or '').strip()
    if link and not link.startswith('https://'):
        raise MarketError('A payment link must start with https://')
    w.number, w.holder, w.pay_link, w.active = number[:40], holder, link[:300], True
    mode = data.get('mode') if provider == 'wave' else 'confirm'
    if mode == 'taptap':
        from .market_wave import platform_ready
        if not platform_ready():
            raise MarketError("TapTap's Wave is not switched on yet — use your own Wave Business API or confirm payments yourself.")
    w.mode = mode if mode in ('api', 'taptap') else 'confirm'
    if w.mode == 'api':
        for f in ('api_key', 'webhook_secret', 'signing_secret'):
            v = str(data.get(f) or '').strip()
            if v:
                setattr(w, f, encrypt_secret(v))
        w.currency = re.sub(r'[^A-Za-z]', '', str(data.get('currency') or w.currency or ''))[:3].upper()
        if not (w.api_key and w.webhook_secret and len(w.currency) == 3):
            raise MarketError('Automatic Wave needs the API key, the webhook secret and the currency code (e.g. GMD).')
    w.save()
    return w


def checkout_for(route, wallet=None):
    """The Wave adapter for a payment: the owner's own Wave account ('own') or TapTap's ('platform')."""
    from .market_wave import checkout_provider
    return checkout_provider(route, wallet)


def best_wallet(business, provider=None):
    """The wallet a payment to ``business`` should go to: automatic first (own Wave API, then TapTap's Wave)."""
    ws = list(wallets(business).filter(provider=provider) if provider else wallets(business))
    rank = {'own': 0, 'platform': 1, 'confirm': 2}
    ws.sort(key=lambda w: rank[w.route])
    return ws[0] if ws else None


# ─────────────────────────────── listings ───────────────────────────────

def create_listing(business, user, data):
    router = business.routers.filter(pk=str(data.get('router') or '0')).first() if str(data.get('router') or '').isdigit() else None
    if not router:
        raise MarketError('Choose the router the vouchers will run on.')
    plan = business.plans.filter(pk=str(data.get('plan') or '0'), active=True).first() if str(data.get('plan') or '').isdigit() else None
    if not plan or not plan.price or plan.price <= 0:
        raise MarketError('Choose an active plan with a price.')
    from .voucher_plan_lock import is_closed, message as closed_message
    if is_closed(plan):
        raise MarketError(closed_message(plan))
    try:
        qty = int(data.get('quantity') or 0)
    except ValueError:
        qty = 0
    if not 5 <= qty <= 10000:
        raise MarketError('A batch holds between 5 and 10 000 vouchers.')
    from .market_vetting import require_vetted
    require_vetted(business, qty)
    price = _dec(data.get('price'), 'batch price')
    face = plan.price * qty
    if price > face:
        raise MarketError(f'The batch price cannot be more than what the vouchers sell for ({_money(business, face)}): '
                          'the buyer must be able to make money.')
    min_retail = _dec(data.get('min_retail') or plan.price, 'lowest selling price')
    if min_retail > plan.price * RETAIL_CAP:
        raise MarketError('The lowest selling price is too high.')
    area = str(data.get('area') or '').strip()[:80]
    if len(area) < 3:
        raise MarketError('Say where the network is (an area, not an address), e.g. "Brikama, near the market".')
    if not wallets(business).exists():
        raise MarketError('Add the wallet buyers will pay you on first (Market › Wallets).')
    sell_by = None
    if data.get('sell_by'):
        try:
            sell_by = timezone.datetime.strptime(str(data['sell_by']), '%Y-%m-%d').date()
        except ValueError:
            raise MarketError('The sell-by date is not a date.')
        if sell_by <= timezone.localdate():
            raise MarketError('The sell-by date must be in the future.')
    precision = data.get('precision') if data.get('precision') in dict(MarketListing.PRECISION) else 'rounded'
    listing = MarketListing.objects.create(
        business=business, router=router, plan=plan, plan_name=plan.name, duration_minutes=plan.duration_minutes,
        max_devices=plan.max_devices, face_price=plan.price, min_retail=min_retail, quantity=qty, price=price,
        title=str(data.get('title') or '').strip()[:120], area=area, pitch=str(data.get('pitch') or '').strip()[:400],
        show_name=data.get('show_name') in ('1', 'on', True), precision=precision,
        share_costs=data.get('share_costs') in ('1', 'on', True), sell_by=sell_by, created_by=user)
    _log(business, 'Market listing', f'{listing.code}: {qty}× {plan.name} for {_money(business, price)}')
    return listing


# ─────────────────────────────── deals (a batch) ───────────────────────────────

@transaction.atomic
def reserve(listing_id, buyer, user):
    from .market_diligence import pack
    listing = MarketListing.objects.select_for_update().select_related('business').get(pk=listing_id)
    if listing.business_id == buyer.pk:
        raise MarketError('This is your own listing.')
    if listing.status != 'open':
        raise MarketError('Someone else is buying this batch right now.' if listing.status == 'reserved' else 'This batch is no longer for sale.')
    if not listing.router_id or not listing.plan_id:
        raise MarketError('The seller changed their network: this batch cannot be sold any more.')
    from .market_vetting import is_vetted
    if not is_vetted(listing.business):
        raise MarketError('This seller is not vetted by TapTap at the moment, so the batch cannot be bought.')
    if listing.sell_by and listing.sell_by <= timezone.localdate():
        raise MarketError('This batch is past its sell-by date.')
    if not wallets(listing.business).exists():
        raise MarketError('The seller has no wallet to receive the payment yet.')
    deal = MarketDeal.objects.create(listing=listing, seller=listing.business, buyer=buyer, quantity=listing.quantity,
                                     price=listing.price, retail_price=max(listing.face_price, listing.min_retail),
                                     diligence=pack(listing), created_by=user)
    listing.status = 'reserved'
    listing.save(update_fields=['status', 'updated_at'])
    _tell(listing.business, f'Market: {buyer.business_name} is buying {listing.code}',
          f'{buyer.business_name} reserved {listing.quantity}× {listing.plan_name} for {_money(listing.business, listing.price)}. '
          f'They have {DEAL_PAY_HOURS} hours to pay your wallet; confirm when the money arrives.',
          reverse('market_deal', args=[deal.reference]))
    return deal


def _deal_for(reference, business, side=None):
    deal = get_object_or_404(MarketDeal.objects.select_related('listing', 'seller', 'buyer', 'listing__router', 'batch'),
                             reference=reference)
    is_seller, is_buyer = deal.seller_id == business.pk, deal.buyer_id == business.pk
    if not (is_seller or is_buyer) or (side == 'seller' and not is_seller) or (side == 'buyer' and not is_buyer):
        raise Http404
    return deal


@transaction.atomic
def claim_deal(deal_id, provider, payment_ref):
    deal = MarketDeal.objects.select_for_update().select_related('seller', 'buyer').get(pk=deal_id)
    if deal.status != 'awaiting_payment':
        raise MarketError('This deal is not waiting for a payment.')
    if provider not in PROVIDERS or not wallets(deal.seller).filter(provider=provider).exists():
        raise MarketError("Choose the seller's wallet you paid.")
    ref = str(payment_ref or '').strip()[:80]
    if len(ref) < 4:
        raise MarketError('Type the transaction ID from your payment message.')
    deal.status, deal.paid_with, deal.payment_ref, deal.claimed_at = 'claimed', provider, ref, timezone.now()
    deal.save(update_fields=['status', 'paid_with', 'payment_ref', 'claimed_at'])
    _tell(deal.seller, f'Market: check your {PROVIDERS[provider]} — {_money(deal.seller, deal.price)} from {deal.buyer.business_name}',
          f'{deal.buyer.business_name} says they paid {_money(deal.seller, deal.price)} for {deal.listing.code} '
          f'(transaction {ref}). Confirm when you see it in your wallet.',
          reverse('market_deal', args=[deal.reference]), key=f'mkt-claim-{deal.pk}')
    return deal


@transaction.atomic
def cancel_deal(deal_id):
    deal = MarketDeal.objects.select_for_update().get(pk=deal_id)
    if deal.status != 'awaiting_payment':
        raise MarketError('Only a deal that is not paid yet can be cancelled. Ask the seller if you already paid.')
    deal.status, deal.closed_at = 'cancelled', timezone.now()
    deal.save(update_fields=['status', 'closed_at'])
    MarketListing.objects.filter(pk=deal.listing_id, status='reserved').update(status='open', updated_at=timezone.now())
    return deal


@transaction.atomic
def reject_deal(deal_id, why=''):
    deal = MarketDeal.objects.select_for_update().select_related('buyer', 'seller').get(pk=deal_id)
    if deal.status not in MarketDeal.OPEN:
        raise MarketError('This deal is already settled.')
    deal.status, deal.closed_at, deal.note = 'rejected', timezone.now(), str(why or '')[:255]
    deal.save(update_fields=['status', 'closed_at', 'note'])
    MarketListing.objects.filter(pk=deal.listing_id, status='reserved').update(status='open', updated_at=timezone.now())
    _tell(deal.buyer, f'Market: the seller did not receive your payment for {deal.listing.code}',
          f'{deal.seller.business_name} says the money did not arrive' + (f': {deal.note}' if deal.note else '.') +
          ' If you paid, contact them with your transaction ID.', reverse('market_deal', args=[deal.reference]))
    return deal


@transaction.atomic
def confirm_deal(deal_id, user, via=''):
    """The money reached the seller (they confirmed it, or Wave did): the batch is the buyer's until it is finished."""
    from .models import Expense, VoucherBatch, VoucherSale
    deal = MarketDeal.objects.select_for_update().select_related('listing', 'seller', 'buyer', 'listing__router', 'listing__plan').get(pk=deal_id)
    if deal.status not in MarketDeal.OPEN:
        raise MarketError('This deal is already settled.')
    now = timezone.now()
    listing = deal.listing
    method = deal.paid_with or (wallets(deal.seller).values_list('provider', flat=True).first() or 'other')
    deal.batch = VoucherBatch.objects.create(business=deal.seller, name=f'Market {deal.reference} · owned by {deal.buyer.business_name}'[:120],
                                             plan=listing.plan, quantity=deal.quantity, created_by=user)
    deal.status, deal.confirmed_at = 'active', now
    deal.save(update_fields=['batch', 'status', 'confirmed_at', 'updated_at'])
    MarketListing.objects.filter(pk=listing.pk).update(status='sold', updated_at=now)
    VoucherSale.objects.create(business=deal.seller, router=listing.router, plan_name=f'Market batch {deal.quantity}× {listing.plan_name}'[:120],
                               amount=deal.price, payment_method=method, reference=deal.reference,
                               customer_name=deal.buyer.business_name[:120],
                               notes=(f'TapTap Market {listing.code} — ' + (via or f'paid to your {PROVIDERS.get(method, method)}'))[:255],
                               sold_at=now, recorded_by=user)
    Expense.objects.create(business=deal.buyer, category='other', amount=deal.price, payment_method=method, reference=deal.reference,
                           description=f'Market batch {deal.quantity}× {listing.plan_name} ({listing.code})'[:200],
                           vendor=deal.seller.business_name[:120], paid_at=now)
    MarketShop.objects.get_or_create(business=deal.buyer)
    from .market_owner import rank_of
    rank, total = rank_of(deal)
    _log(deal.seller, 'Market batch sold', f'{listing.code} to {deal.buyer.business_name}: {deal.quantity}× {listing.plan_name}')
    _tell(deal.buyer, f'Market: {listing.code} is yours — start selling',
          f'{"Wave confirmed your payment" if via else deal.seller.business_name + " confirmed your payment"}. '
          f'{deal.quantity} vouchers of {listing.plan_name} are yours until the last one is used. You are #{rank} of {total} on this network: '
          'customers who buy online there pay your wallet. Share your shop link too.', reverse('market_deal', args=[deal.reference]))
    if via:
        _tell(deal.seller, f'Market: {deal.buyer.business_name} paid {_money(deal.seller, deal.price)} for {listing.code}',
              f'Wave confirmed the payment ({via}). The batch now belongs to {deal.buyer.business_name} and runs on your network '
              'until the last voucher is used; you can support those vouchers but not change them.',
              reverse('market_deal', args=[deal.reference]))
    return deal


def deal_checkout(deal, request):
    """Pay a reserved batch with Wave: on the seller's own Wave account, or through TapTap's (paid out to the seller)."""
    if deal.status != 'awaiting_payment':
        raise MarketError('This batch is not waiting for a payment.')
    wallet = best_wallet(deal.seller, 'wave')
    if not wallet or not wallet.automatic:
        raise MarketError('The seller takes Wave by hand: pay their number and type the transaction ID.')
    from .market_wave import checkout_provider
    provider = checkout_provider(wallet.route, wallet)
    MarketDeal.objects.filter(pk=deal.pk).update(route=wallet.route, currency=provider.iso_currency, paid_with='wave')
    deal.refresh_from_db()
    try:
        return provider.create_checkout(deal, request)
    except Exception as exc:
        logger.warning('Market batch checkout %s failed: %s', deal.reference, exc)
        raise MarketError('Wave could not start the payment. Try again in a moment, or pay by hand.')


# ─────────────────────────────── customer orders ───────────────────────────────

def shop_offers(shop):
    """What a buyer's shop sells: their active deals with stock left."""
    out = []
    for deal in (MarketDeal.objects.filter(buyer=shop.business, status='active')
                 .select_related('listing', 'seller').order_by('retail_price', 'pk')):
        left = available(deal)
        if left <= 0:
            continue
        s = deal.seller
        out.append({'deal': deal, 'left': left, 'network': s.wifi_ssid or s.business_name, 'area': deal.listing.area,
                    'plan': deal.listing.plan_name, 'minutes': deal.listing.duration_minutes, 'devices': deal.listing.max_devices,
                    'price': deal.retail_price})
    return out


def duration_text(minutes):
    m = int(minutes or 0)
    if m and m % 43200 == 0:
        return f'{m // 43200} month{"s" if m // 43200 != 1 else ""}'
    if m and m % 1440 == 0:
        return f'{m // 1440} day{"s" if m // 1440 != 1 else ""}'
    if m and m % 60 == 0:
        return f'{m // 60} hour{"s" if m // 60 != 1 else ""}'
    return f'{m} minutes' if m else 'Unlimited'


@transaction.atomic
def create_order(deal_id, provider, phone, name='', ip='', channel='shop', wallet=None):
    deal = MarketDeal.objects.select_for_update().select_related('buyer').get(pk=deal_id)
    if available(deal) <= 0:
        raise MarketError('Sold out — choose another voucher.')
    wallet = wallet or wallets(deal.buyer).filter(provider=provider).first()
    if not wallet or wallet.business_id != deal.buyer_id or not wallet.active:
        raise MarketError('That way of paying is not available here.')
    phone = re.sub(r'[^\d+]', '', str(phone or ''))[:40]
    if len(phone.lstrip('+')) < 7:
        raise MarketError('Type the mobile number you pay with.')
    if MarketOrder.objects.filter(deal__buyer=deal.buyer, customer_phone=phone, status__in=('pending', 'claimed')).count() >= MAX_OPEN_PER_PHONE:
        raise MarketError('You already have payments waiting. Finish or wait for those first.')
    if ip:
        k = f'tt:mkt:ip:{ip}'
        if not cache.add(k, 1, 3600) and cache.incr(k) > ORDERS_PER_IP_HOUR:
            raise MarketError('Too many orders from this connection. Try again later.')
    route = wallet.route
    currency = deal.buyer.currency or ''
    if route == 'own':
        currency = wallet.currency
    elif route == 'platform':
        from .online_buy import WaveProvider
        currency = WaveProvider().iso_currency
    return MarketOrder.objects.create(deal=deal, wallet=wallet, provider=wallet.provider, amount=deal.retail_price, currency=currency,
                                      route=route, channel=channel, customer_phone=phone, customer_name=str(name or '').strip()[:120],
                                      ip=ip[:64])


def start_payment(order, request):
    """Where the customer goes next: Wave's page (automatic) or our page with the wallet number (confirm)."""
    if order.route in ('own', 'platform'):
        from .market_wave import checkout_provider
        try:
            return checkout_provider(order.route, order.wallet).create_checkout(order, request)
        except Exception as exc:
            logger.warning('Market Wave checkout failed for %s: %s', order.reference, exc)
            MarketOrder.objects.filter(pk=order.pk).update(status='failed', error=str(exc)[:255])
            raise MarketError('Wave could not start the payment. Please try again in a moment.')
    return reverse('market_order', args=[order.reference, order.access_token])


@transaction.atomic
def claim_order(order_id, payment_ref):
    order = MarketOrder.objects.select_for_update().select_related('deal', 'deal__buyer').get(pk=order_id)
    if order.status == 'expired':           # paid a little late: fine while the batch still has a voucher for it
        MarketDeal.objects.select_for_update().filter(pk=order.deal_id).first()
        if available(order.deal) <= 0:
            raise MarketError('This order timed out and the vouchers are sold out. Contact the shop with your transaction ID.')
    elif order.status != 'pending':
        raise MarketError('This order is not waiting for a payment.')
    if order.route in ('own', 'platform'):
        raise MarketError('This payment is confirmed automatically by Wave.')
    ref = str(payment_ref or '').strip()[:80]
    if len(ref) < 4:
        raise MarketError('Type the transaction ID from your payment message.')
    if MarketOrder.objects.filter(deal__buyer=order.deal.buyer, payment_ref__iexact=ref).exclude(pk=order.pk) \
            .exclude(status__in=('rejected', 'expired')).exists():
        raise MarketError('That transaction ID was already used for another voucher.')
    order.status, order.payment_ref, order.claimed_at = 'claimed', ref, timezone.now()
    order.save(update_fields=['status', 'payment_ref', 'claimed_at', 'updated_at'])
    b = order.deal.buyer
    _tell(b, f'Market: {_money(b, order.amount)} on {PROVIDERS[order.provider]} to confirm',
          f'A customer ({order.customer_phone}) says they sent {_money(b, order.amount)} — transaction {ref}. '
          'Check your wallet and confirm to send the voucher.', reverse('market') + '?tab=stock', key=f'mkt-order-{order.pk}')
    return order


@transaction.atomic
def reject_order(order_id, user):
    order = MarketOrder.objects.select_for_update().get(pk=order_id)
    if order.status not in ('pending', 'claimed', 'review'):
        raise MarketError('This order is already settled.')
    order.status, order.decided_by, order.error = 'rejected', user, 'Payment not received'
    order.save(update_fields=['status', 'decided_by', 'error', 'updated_at'])
    return order


@transaction.atomic
def confirm_order(order_id, user):
    order = MarketOrder.objects.select_for_update().get(pk=order_id)
    if order.status == 'issued':
        return order
    if order.status not in ('pending', 'claimed', 'review'):
        raise MarketError('This order cannot be confirmed any more.')
    if order.route == 'platform' and order.status != 'review':
        raise MarketError("TapTap's Wave confirms this payment by itself.")
    order.status, order.decided_by = 'paid', user
    order.save(update_fields=['status', 'decided_by', 'updated_at'])
    issue(order.pk, user)
    order.refresh_from_db()
    return order


@transaction.atomic
def issue(order_id, user=None):
    """Make exactly one voucher on the seller's router for a paid order, and the buyer's sale."""
    from .models import Voucher, VoucherSale
    from .online_buy import _new_voucher_code
    order = MarketOrder.objects.select_for_update().select_related('wallet').get(pk=order_id)
    if order.status == 'issued' and order.voucher_id:
        return order.voucher
    if order.status != 'paid':
        raise MarketError('This order is not paid.')
    deal = MarketDeal.objects.select_for_update().select_related('listing', 'seller', 'buyer', 'listing__router').get(pk=order.deal_id)
    listing = deal.listing
    if deal.issued >= deal.quantity or deal.status not in ('active',) or not listing.router_id:
        order.status, order.error = 'review', ('The seller removed the router of this batch — contact them, or refund the customer.'
                                               if not listing.router_id else 'No vouchers left in this batch — refund or move the customer.')
        order.save(update_fields=['status', 'error', 'updated_at'])
        return None                     # kept for a person to look at (raising here would undo the save)
    now = timezone.now()
    voucher = Voucher.objects.create(
        business=deal.seller, router=listing.router, batch=deal.batch, code=_new_voucher_code(deal.seller),
        plan_name=listing.plan_name, price=listing.face_price, duration_minutes=listing.duration_minutes,
        max_devices=listing.max_devices, source='taptap', status='active', sold_at=now,
        note=f'TapTap Market {deal.reference} · owned by {deal.buyer.business_name}'[:255],
        created_by_label='TapTap Market')
    MarketDeal.objects.filter(pk=deal.pk).update(issued=F('issued') + 1)
    deal.refresh_from_db(fields=['issued'])
    if deal.issued >= deal.quantity:
        MarketDeal.objects.filter(pk=deal.pk).update(status='completed', closed_at=now)
    VoucherSale.objects.create(
        business=deal.buyer, plan_name=listing.plan_name, voucher_code=voucher.code, amount=order.amount,
        payment_method=order.provider, customer_name=order.customer_name, customer_phone=order.customer_phone,
        reference=order.reference, notes=f'Market voucher on {deal.seller.wifi_ssid or deal.seller.business_name} ({listing.code})'[:255],
        sold_at=now, recorded_by=user)
    order.voucher, order.voucher_code, order.status, order.issued_at, order.error = voucher, voucher.code, 'issued', now, ''
    order.save(update_fields=['voucher', 'voucher_code', 'status', 'issued_at', 'error', 'updated_at'])
    if order.route == 'platform' and order.wallet_id:
        from .market_wave import queue_payout
        queue_payout(deal.buyer, order.wallet, order.amount, order.currency, order=order)
    transaction.on_commit(lambda: _push(voucher.pk))
    return voucher


def _push(voucher_id):
    from .models import Voucher
    v = Voucher.objects.filter(pk=voucher_id).select_related('router').first()
    if not v:
        return
    try:
        from .voucher_push import push_vouchers
        push_vouchers([v], None)
    except Exception as exc:
        logger.exception('Market voucher %s could not be pushed at once', v.code)
        Voucher.objects.filter(pk=v.pk).update(mikrotik_sync_status='Pending', mikrotik_sync_error=str(exc)[:255])


def _check_event(thing, event):
    """'' when a verified Wave event pays ``thing`` (deal or order) exactly, else why not."""
    from .online_buy import _as_decimal
    sid = str(event.get('provider_session_id') or '')[:120]
    if sid and thing.provider_session_id and sid != thing.provider_session_id:
        return 'Payment session does not match.'
    if not event.get('paid'):
        return 'not paid'
    try:
        amount = _as_decimal(event.get('amount'))
    except ValueError:
        return 'Invalid amount.'
    if amount != thing.amount or str(event.get('currency') or '').upper() != (thing.currency or '').upper():
        return 'Amount or currency does not match.'
    return ''


@transaction.atomic
def settle(event, wallet=None, route='own'):
    """A verified Wave event — on an owner's own account (``wallet``) or on TapTap's (``route='platform'``)."""
    ref = str(event.get('reference') or '').strip()
    if ref.startswith('MKT-D'):
        qs = MarketDeal.objects.select_for_update().filter(reference=ref, route=route)
        if wallet is not None:
            qs = qs.filter(seller=wallet.business)
        deal = qs.first()
        if not deal:
            raise MarketError('Unknown batch reference.')
        if deal.status not in MarketDeal.OPEN:
            return deal                                  # already settled: Wave retries are harmless
        why = _check_event(deal, event)
        deal.provider_payload = event.get('raw') or {}
        if why:
            deal.note = ('Wave: ' + why)[:255]
            deal.save(update_fields=['provider_payload', 'note', 'updated_at'])
            return deal
        deal.payment_ref = str(event.get('transaction_id') or '')[:80]
        deal.save(update_fields=['provider_payload', 'payment_ref', 'updated_at'])
        confirm_deal(deal.pk, None, via=f'Wave {deal.payment_ref}'.strip())
        if route == 'platform':
            w = best_wallet(deal.seller, 'wave')
            if w:
                from .market_wave import queue_payout
                queue_payout(deal.seller, w, deal.price, deal.currency, deal=deal)
        deal.refresh_from_db()
        return deal
    qs = MarketOrder.objects.select_for_update().filter(reference=ref, route=route)
    if wallet is not None:
        qs = qs.filter(wallet=wallet)
    order = qs.first()
    if not order:
        raise MarketError('Unknown order reference.')
    if order.status == 'issued':
        return order
    if order.status == 'paid':
        issue(order.pk)
        order.refresh_from_db()
        return order
    why = _check_event(order, event)
    order.provider_payload = event.get('raw') or {}
    if why == 'not paid':
        order.status, order.error = 'failed', 'Payment was not completed.'
        order.save(update_fields=['status', 'error', 'provider_payload', 'updated_at'])
        return order
    if why:
        order.status, order.error = 'review', why
        order.save(update_fields=['status', 'error', 'provider_payload', 'updated_at'])
        return order                    # kept for a person to look at (raising here would undo the save)
    order.status, order.payment_ref = 'paid', str(event.get('transaction_id') or '')[:80]
    order.save(update_fields=['status', 'payment_ref', 'provider_payload', 'updated_at'])
    issue(order.pk)
    order.refresh_from_db()
    return order


def settle_wave(wallet, event):
    """A verified Wave webhook on an owner's own Wave account."""
    return settle(event, wallet=wallet, route='own')


# ─────────────────────────────── pages ───────────────────────────────

def _business(request):
    return getattr(request, 'tt_business', None) or request.user.business


def _is_owner(request):
    return getattr(request, 'tt_role', None) == 'owner' and not getattr(request, 'tt_view_as', None)


def owner_only(view):
    """Money and promises: only the business owner (not staff, not TapTap support viewing the account)."""
    @wraps(view)
    def wrapped(request, *a, **kw):
        if request.method == 'POST' and not _is_owner(request):
            messages.error(request, 'Only the business owner can do this on the Market.')
            return redirect(request.META.get('HTTP_REFERER') or reverse('market'))
        return view(request, *a, **kw)
    return wrapped


def _open(qs):
    from .voucher_plan_lock import open_plans
    return open_plans(qs)


def _seller_ok(request):
    from . import features
    return features.uses(_business(request), 'market')


@login_required
def market(request):
    sweep()
    business = _business(request)
    tab = request.GET.get('tab') or 'browse'
    q = (request.GET.get('q') or '').strip()
    from .market_vetting import seller_context, vetted_q
    listings = (MarketListing.objects.filter(status='open').filter(vetted_q()).exclude(business=business)
                .select_related('business', 'business__market_seller').order_by('-created_at'))
    if q:
        listings = listings.filter(Q(area__icontains=q) | Q(plan_name__icontains=q) | Q(title__icontains=q) | Q(code__iexact=q))
    from .market_diligence import pack
    cards = []
    for l in listings[:60]:
        try:
            dd = pack(l)
        except Exception:
            logger.exception('due diligence for %s', l.code)
            continue
        cards.append({'l': l, 'dd': dd})
    sort = request.GET.get('sort') or 'score'
    if sort == 'margin':
        cards.sort(key=lambda c: -c['l'].margin_pct)
    elif sort == 'fast':
        cards.sort(key=lambda c: c['dd']['forecast']['days'][25] or 10 ** 6)
    else:
        cards.sort(key=lambda c: -c['dd']['score']['value'])
    mine = business.market_listings.select_related('router').order_by('-created_at')
    selling_deals = MarketDeal.objects.filter(seller=business).select_related('listing', 'buyer').order_by('-created_at')[:40]
    buying = MarketDeal.objects.filter(buyer=business).select_related('listing', 'seller').order_by('-created_at')[:40]
    stock = [{'deal': d, 'left': available(d)} for d in buying if d.status == 'active']
    to_confirm = (MarketOrder.objects.filter(deal__buyer=business, status__in=('claimed', 'review'))
                  .select_related('deal', 'deal__listing').order_by('created_at'))
    recent_orders = MarketOrder.objects.filter(deal__buyer=business).select_related('deal__listing', 'voucher').order_by('-created_at')[:25]
    shop = getattr(business, 'market_shop', None) if MarketShop.objects.filter(business=business).exists() else None
    wl = {w.provider: w for w in wallets(business, active=False)}
    earned = MarketOrder.objects.filter(deal__buyer=business, status='issued').aggregate(v=Sum('amount'), n=Count('id'))
    counts = {'deals_waiting': sum(1 for d in selling_deals if d.status == 'claimed'), 'orders_waiting': to_confirm.count()}
    from .market_wave import platform_ready
    payouts = list(business.market_payouts.order_by('-created_at')[:15])
    return render(request, 'core/market.html', {
        'tab': tab, 'q': q, 'sort': sort, 'cards': cards, 'mine': mine, 'selling_deals': selling_deals, 'buying': buying,
        'stock': stock, 'to_confirm': to_confirm, 'recent_orders': recent_orders, 'shop': shop,
        'shop_url': _site(request) + reverse('market_shop', args=[shop.slug]) if shop else '',
        'wallet_rows': [(code, label, wl.get(code)) for code, label in WALLET_PROVIDERS],
        'hook_base': _site(request) + '/api/market/wave/', 'earned': earned, 'counts': counts,
        'routers': business.routers.order_by('name'), 'plans': _open(business.plans.filter(active=True, price__gt=0)).order_by('price'),
        'can_sell': _seller_ok(request), 'is_owner': _is_owner(request), 'providers': PROVIDERS,
        'platform': platform_ready(), 'payouts': payouts, 'vet': seller_context(business) if tab == 'selling' else None,
        'owned_on_my_network': sum(1 for d in selling_deals if d.status in MarketDeal.OWNED),
    })


@login_required
def listing_page(request, code):
    sweep()
    business = _business(request)
    listing = get_object_or_404(MarketListing.objects.select_related('business', 'router'), code=code)
    own = listing.business_id == business.pk
    from .market_vetting import is_vetted
    if not own and (listing.status not in ('open', 'reserved') or not is_vetted(listing.business)):
        mine = listing.deals.filter(buyer=business).first()
        if mine:
            return redirect('market_deal', reference=mine.reference)
        raise Http404
    from .market_diligence import pack
    if not own and cache.add(f'tt:mkt:view:{listing.pk}:{request.user.pk}', 1, 3600):
        MarketListing.objects.filter(pk=listing.pk).update(views=F('views') + 1)
    dd = pack(listing)
    from .market_owner import outlook
    seller_wave = best_wallet(listing.business, 'wave')
    return render(request, 'core/market_listing.html', {
        'l': listing, 'dd': dd, 'own': own, 'is_owner': _is_owner(request), 'out': outlook(listing, None if own else business),
        'wave_auto': bool(seller_wave and seller_wave.automatic),
        'deals': listing.deals.select_related('buyer') if own else None,
        'my_deal': listing.deals.filter(buyer=business).exclude(status__in=('cancelled', 'expired', 'rejected')).first(),
        'buyer_has_wallet': wallets(business).exists(),
    })


@login_required
@owner_only
@require_POST
def listing_reserve(request, code):
    listing = get_object_or_404(MarketListing, code=code)
    if request.POST.get('agree') != '1':
        messages.error(request, 'Tick that you read the due diligence and the rules first.')
        return redirect('market_listing', code=code)
    try:
        deal = reserve(listing.pk, _business(request), request.user)
    except MarketError as e:
        messages.error(request, str(e))
        return redirect('market_listing', code=code)
    messages.success(request, f'Reserved for {DEAL_PAY_HOURS} hours. Pay the seller and tell TapTap the transaction ID.')
    return redirect('market_deal', reference=deal.reference)


@login_required
@owner_only
def sell(request):
    business = _business(request)
    if request.method == 'POST':
        try:
            listing = create_listing(business, request.user, request.POST)
        except MarketError as e:
            messages.error(request, str(e))
            return redirect(reverse('market') + '?tab=selling#new')
        messages.success(request, f'{listing.code} is on the market. This is what buyers see.')
        return redirect('market_listing', code=listing.code)
    return redirect(reverse('market') + '?tab=selling#new')


@login_required
@owner_only
@require_POST
def listing_action(request, code):
    business = _business(request)
    listing = get_object_or_404(MarketListing, code=code, business=business)
    act = request.POST.get('action')
    if act == 'withdraw' and listing.status == 'open':
        listing.status = 'withdrawn'
        listing.save(update_fields=['status', 'updated_at'])
        messages.success(request, f'{listing.code} was taken off the market.')
    elif act == 'reopen' and listing.status == 'withdrawn':
        listing.status = 'open'
        listing.save(update_fields=['status', 'updated_at'])
        messages.success(request, f'{listing.code} is back on the market.')
    elif act == 'price' and listing.status == 'open':
        try:
            price = _dec(request.POST.get('price'), 'batch price')
            if price > listing.face_value:
                raise MarketError('The batch price cannot be more than what the vouchers sell for.')
        except MarketError as e:
            messages.error(request, str(e))
        else:
            listing.price = price
            listing.save(update_fields=['price', 'updated_at'])
            messages.success(request, 'Price changed.')
    else:
        messages.error(request, 'That cannot be done now.')
    return redirect('market_listing', code=code)


@login_required
def deal_page(request, reference):
    sweep()
    business = _business(request)
    deal = _deal_for(reference, business)
    seller_side = deal.seller_id == business.pk
    if request.method == 'POST':
        if not _is_owner(request):
            messages.error(request, 'Only the business owner can do this on the Market.')
            return redirect('market_deal', reference=reference)
        act = request.POST.get('action')
        try:
            if act == 'wave' and not seller_side:
                return redirect(deal_checkout(deal, request))
            elif act == 'claim' and not seller_side:
                claim_deal(deal.pk, request.POST.get('provider'), request.POST.get('payment_ref'))
                messages.success(request, 'Thank you — the seller has been asked to confirm your payment.')
            elif act == 'cancel' and not seller_side:
                cancel_deal(deal.pk)
                messages.success(request, 'Reservation cancelled.')
            elif act == 'confirm' and seller_side:
                confirm_deal(deal.pk, request.user)
                messages.success(request, 'Payment confirmed: the batch now belongs to the buyer and runs on your network.')
            elif act == 'reject' and seller_side:
                reject_deal(deal.pk, request.POST.get('why'))
                messages.success(request, 'The buyer was told the money did not arrive. The batch is back on the market.')
            elif act == 'retail' and not seller_side:
                price = _dec(request.POST.get('retail_price'), 'price')
                if price < deal.listing.min_retail or price > deal.listing.face_price * RETAIL_CAP:
                    raise MarketError(f'Choose a price from {_money(business, deal.listing.min_retail)} to '
                                      f'{_money(business, deal.listing.face_price * RETAIL_CAP)}.')
                MarketDeal.objects.filter(pk=deal.pk).update(retail_price=price)
                messages.success(request, 'Your price is saved. New customers pay it from now on.')
            elif act == 'rate' and not seller_side and deal.status in ('active', 'completed', 'handed_over'):
                stars = int(request.POST.get('rating') or 0)
                if not 1 <= stars <= 5:
                    raise MarketError('Choose 1 to 5 stars.')
                MarketDeal.objects.filter(pk=deal.pk).update(rating=stars, review=str(request.POST.get('review') or '').strip()[:300])
                messages.success(request, 'Thank you — other buyers will see your rating.')
            else:
                raise MarketError('That cannot be done now.')
        except MarketError as e:
            messages.error(request, str(e))
        return redirect('market_deal', reference=reference)
    if request.GET.get('payment') == 'error' and deal.status in MarketDeal.OPEN:
        messages.error(request, 'The Wave payment did not go through. Try again, or pay by hand.')
    orders = deal.orders.select_related('voucher').order_by('-created_at')[:30] if not seller_side else None
    seller_wallets = list(wallets(deal.seller)) if deal.status in MarketDeal.OPEN else []
    seller_wave = best_wallet(deal.seller, 'wave') if deal.status == 'awaiting_payment' else None
    unlocked = deal.status in ('active', 'completed', 'handed_over') or seller_side
    from .market_owner import monitor
    mon = monitor(deal) if deal.status in ('active', 'completed', 'handed_over') else None
    issued_vouchers = None
    if seller_side and deal.batch_id:
        from .models import Voucher
        issued_vouchers = Voucher.all_objects.filter(batch=deal.batch).aggregate(
            n=Count('id'), used=Count('id', filter=Q(used_at__isnull=False)))
    return render(request, 'core/market_deal.html', {
        'deal': deal, 'l': deal.listing, 'seller_side': seller_side, 'orders': orders, 'seller_wallets': seller_wallets,
        'unlocked': unlocked, 'left': available(deal), 'issued_vouchers': issued_vouchers, 'is_owner': _is_owner(request),
        'retail_max': deal.listing.face_price * RETAIL_CAP, 'hours': DEAL_PAY_HOURS,
        'deadline': deal.created_at + timedelta(hours=DEAL_PAY_HOURS), 'dd': deal.diligence or {},
        'steps': _steps(deal), 'mon': mon, 'wave_auto': bool(seller_wave and seller_wave.automatic),
    })


def _steps(deal):
    order = ['awaiting_payment', 'claimed', 'active', 'completed', 'handed_over']
    at = order.index(deal.status) if deal.status in order else -1
    labels = [('Reserved', 'bi-bookmark-check'), ('Paid', 'bi-phone'), ('Owned & selling', 'bi-patch-check'),
              ('All sold — in use', 'bi-hourglass-split'), ('Finished', 'bi-flag')]
    return [{'label': lbl, 'icon': icon, 'done': i <= at, 'now': i == at} for i, (lbl, icon) in enumerate(labels)]


@login_required
@owner_only
@require_POST
def setup(request):
    business = _business(request)
    try:
        with transaction.atomic():
            for code, _ in WALLET_PROVIDERS:
                data = {k[len(code) + 1:]: v for k, v in request.POST.items() if k.startswith(code + '_')}
                if data:
                    save_wallet(business, code, data)
            if request.POST.get('shop') == '1':
                shop, _ = MarketShop.objects.get_or_create(business=business)
                shop.heading = (request.POST.get('heading') or shop.heading).strip()[:120]
                shop.note = (request.POST.get('note') or '').strip()[:255]
                shop.enabled = request.POST.get('enabled') == '1'
                try:
                    shop.own_share = max(0, min(50, int(request.POST.get('own_share') or 0)))
                except ValueError:
                    pass
                shop.save()
    except MarketError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, 'Saved.')
    return redirect(reverse('market') + '?tab=setup')


@login_required
@require_POST
def order_action(request, reference):
    business = _business(request)
    perms = getattr(request, 'tt_perms', frozenset())
    if not (_is_owner(request) or 'finance.view' in perms):
        return HttpResponseForbidden('Not allowed')
    order = get_object_or_404(MarketOrder.objects.select_related('deal'), reference=reference, deal__buyer=business)
    try:
        if request.POST.get('action') == 'confirm':
            confirm_order(order.pk, request.user)
            messages.success(request, f'Confirmed — the customer has their voucher ({order.customer_phone}).')
        elif request.POST.get('action') == 'reject':
            reject_order(order.pk, request.user)
            messages.success(request, 'Marked as not paid. The voucher went back into stock.')
        else:
            raise MarketError('Unknown action.')
    except MarketError as e:
        messages.error(request, str(e))
    return redirect(request.POST.get('next') or (reverse('market') + '?tab=stock'))


# ─────────────────────────────── public pages ───────────────────────────────

def _ip(request):
    return request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', '')).split(',')[0].strip()[:64]


def shop_page(request, slug):
    sweep()
    shop = get_object_or_404(MarketShop.objects.select_related('business'), slug=slug)
    offers = shop_offers(shop) if shop.enabled else []
    pay = list(wallets(shop.business))
    if request.method == 'POST':
        if not shop.enabled or not offers:
            messages.error(request, 'This shop is not selling right now.')
            return redirect('market_shop', slug=slug)
        deal_id = str(request.POST.get('deal') or '')
        if not any(str(o['deal'].pk) == deal_id for o in offers):
            messages.error(request, 'Choose a voucher.')
            return redirect('market_shop', slug=slug)
        try:
            order = create_order(int(deal_id), request.POST.get('provider'), request.POST.get('phone'),
                                 request.POST.get('name'), _ip(request))
        except MarketError as e:
            messages.error(request, str(e))
            return redirect('market_shop', slug=slug)
        try:
            return redirect(start_payment(order, request))
        except MarketError as e:
            messages.error(request, str(e))
            return redirect('market_shop', slug=slug)
    return render(request, 'core/market_shop.html', {
        'shop': shop, 'b': shop.business, 'offers': [{**o, 'duration': duration_text(o['minutes'])} for o in offers],
        'wallets': pay})


def order_page(request, reference, token):
    order = get_object_or_404(MarketOrder.objects.select_related('deal', 'deal__seller', 'deal__buyer', 'deal__listing', 'wallet', 'voucher'),
                              reference=reference, access_token=token)
    if order.status == 'pending' and order.created_at < timezone.now() - timedelta(minutes=ORDER_PAY_MINUTES):
        sweep()
        order.refresh_from_db()
    if request.method == 'POST':
        try:
            claim_order(order.pk, request.POST.get('payment_ref'))
        except MarketError as e:
            messages.error(request, str(e))
        return redirect('market_order', reference=reference, token=token)
    if request.GET.get('payment') == 'error' and order.status in ('pending', 'failed'):
        messages.error(request, 'The Wave payment did not go through. Nothing was taken — you can start again.')
    if request.GET.get('json'):
        return JsonResponse({'status': order.status, 'code': order.voucher.code if order.status == 'issued' and order.voucher_id else ''})
    s = order.deal.seller
    return render(request, 'core/market_order.html', {
        'o': order, 'b': order.deal.buyer, 'network': s.wifi_ssid or s.business_name, 'login': s.hotspot_dns_name or '',
        'area': order.deal.listing.area, 'duration': duration_text(order.deal.listing.duration_minutes),
        'shop': MarketShop.objects.filter(business=order.deal.buyer).first(),
        'deadline': order.created_at + timedelta(minutes=ORDER_PAY_MINUTES)})


@csrf_exempt
@require_POST
def wave_webhook(request, hook):
    wallet = MarketWallet.objects.filter(hook_token=hook, provider='wave', mode='api', active=True).first()
    if not wallet:
        return JsonResponse({'ok': False, 'error': 'unknown wallet'}, status=404)
    try:
        event = checkout_for('own', wallet).verify_webhook(request)
        thing = settle_wave(wallet, event)
    except PermissionError as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=401)
    except (MarketError, ValueError) as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=400)
    except Exception:
        logger.exception('Market Wave webhook failed')
        return JsonResponse({'ok': False, 'error': 'processing failed'}, status=500)
    if thing.status == 'review' or (isinstance(thing, MarketDeal) and thing.status in MarketDeal.OPEN and thing.note.startswith('Wave:')):
        return JsonResponse({'ok': False, 'error': getattr(thing, 'error', '') or thing.note, 'reference': thing.reference}, status=400)
    return JsonResponse({'ok': True, 'reference': thing.reference, 'status': thing.status})


# ─────────────────────────────── Fit ───────────────────────────────

def fit_market(ctx, **_):
    """The business's Market at a glance: listings, batches bought, stock, payments waiting."""
    from .fit_tools import money
    b = ctx.business
    listings = b.market_listings.all()
    buying = MarketDeal.objects.filter(buyer=b)
    sold = MarketOrder.objects.filter(deal__buyer=b, status='issued').aggregate(v=Sum('amount'), n=Count('id'))
    return {
        'my_listings': [{'code': l.code, 'what': f'{l.quantity}× {l.plan_name}', 'price': money(ctx, l.price), 'status': l.get_status_display(),
                         'views': l.views} for l in listings[:10]],
        'deals_to_confirm_as_seller': MarketDeal.objects.filter(seller=b, status='claimed').count(),
        'batches_bought': [{'reference': d.reference, 'what': f'{d.quantity}× {d.listing.plan_name}', 'status': d.get_status_display(),
                            'sold': d.issued, 'left': available(d), 'your_price': money(ctx, d.retail_price)}
                           for d in buying.select_related('listing')[:10]],
        'customer_payments_to_confirm': MarketOrder.objects.filter(deal__buyer=b, status='claimed').count(),
        'vouchers_sold_to_customers': sold['n'] or 0, 'earned_from_customers': money(ctx, sold['v'] or 0),
        'open_listings_on_the_market': MarketListing.objects.filter(status='open').exclude(business=b).count(),
        'batches_others_own_on_my_network': MarketDeal.objects.filter(seller=b, status__in=MarketDeal.OWNED).count(),
        'wave_payouts_waiting': b.market_payouts.exclude(status='succeeded').count(),
        'link': reverse('market'),
    }


# ─────────────────────────────── install ───────────────────────────────

URLS = [
    ('market/', market, 'market', 'private'),
    ('market/sell/', sell, 'market_sell', 'seller'),
    ('market/setup/', setup, 'market_setup', 'private'),
    ('market/l/<str:code>/', listing_page, 'market_listing', 'private'),
    ('market/l/<str:code>/reserve/', listing_reserve, 'market_listing_reserve', 'private'),
    ('market/l/<str:code>/action/', listing_action, 'market_listing_action', 'seller'),
    ('market/d/<str:reference>/', deal_page, 'market_deal', 'private'),
    ('market/o/<str:reference>/', order_action, 'market_order_action', 'private'),
    ('p/market/<str:slug>/', shop_page, 'market_shop', 'public'),
    ('p/market/order/<str:reference>/<str:token>/', order_page, 'market_order', 'public'),
    ('api/market/wave/<str:hook>/', wave_webhook, 'market_wave_webhook', 'public'),
]


def _uptime_hook():
    from . import uptime_guard
    original = uptime_guard.came_back
    if getattr(original, '_tt_market', False):
        return

    @wraps(original)
    def came_back(router, *a, **kw):
        since = uptime_guard.down_since(router.pk)
        why = (cache.get(uptime_guard._k(router.pk, 'down')) or {}).get('why', '')
        result = original(router, *a, **kw)
        if since:
            try:
                from .market_diligence import record_outage
                record_outage(router, since, timezone.now(), why)
            except Exception:
                logger.exception('could not record the outage of %s', router)
        return result

    came_back._tt_market = True
    uptime_guard.came_back = came_back


def install():
    from django.urls import path as _path  # noqa: N811
    from . import middleware, notify, permissions, team, urls
    names = {getattr(x, 'name', None) for x in urls.urlpatterns}
    for route, view, name, kind in URLS:
        if name not in names:
            urls.urlpatterns.append(_path(route, view, name=name))
        if kind == 'public':
            permissions.MEMBER_ALLOWED.add(name)
            team.PLATFORM_OPEN.add(name)
            middleware.SubscriptionAccessMiddleware.ALLOWED.add(name)
        else:
            permissions.URL_PERMS[name] = ('finance.view',)
    notify.EVENTS.setdefault('market', ('TapTap Market', 'A buyer reserved or paid for your batch, a seller confirmed, '
                                        'or a customer paid you for a market voucher.', 'instant', 'warning', 'Business'))
    _uptime_hook()
    from . import market_owner
    market_owner.install()
    try:
        from . import fit_tools
        fit_tools.TOOLS['market_overview'] = (('finance.view',), fit_market,
                                              'TapTap Market for this business: its listings (batches for sale), batches bought and '
                                              'how many are left, customer payments waiting to be confirmed, money earned.',
                                              fit_tools._obj({}))
        S = {'type': 'string'}
        fit_tools.TOOLS['market_listings'] = (None, market_owner.fit_listings,
                                              'Batches of vouchers for sale on the TapTap Market, with their due-diligence grade, uptime, '
                                              'devices per day, margin, how many batches are already selling on that network and the share '
                                              'of online buyers a new buyer would get. Only figures the sellers share; nothing private.',
                                              fit_tools._obj({'q': {**S, 'description': 'Area, plan or MKT- code'},
                                                              'sort': {'type': 'string', 'enum': ['score', 'margin', 'fast']}}))
        fit_tools.TOOLS['market_advice'] = (None, market_owner.fit_advice,
                                            'Advice on one Market listing (MKT- code): why its grade, the queue of batches on that '
                                            'network (anonymous), your rank and share if you buy, how fast it could sell, plain tips.',
                                            fit_tools._obj({'code': S}, ['code']))
    except Exception:
        logger.exception('market Fit tool')
