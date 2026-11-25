"""TapTap Market — owning a batch until it is finished, sharing a network fairly, advice and housekeeping.

Ownership until the batch is finished
-------------------------------------
A batch belongs to its buyer from the moment it is paid until its LAST voucher is finished — not for a number of hours.
While the buyer owns it (deal ``active`` = selling, ``completed`` = all sold, vouchers still running):

* the seller may only **support** those vouchers: look, diagnose, fix a login, reset devices, warn about sharing;
* the seller may **not** delete, pause, disable, change plan, profile, time or code, roll back, mark missing,
  give to an agent or delete the batch — the request is refused with a message (``_gate`` hook below, plus the bin and
  freeze rules, so bulk actions and Fit skip those vouchers too).

When every voucher is finished (time over, expired, or sold and never used for ``ABANDONED_DAYS``), the batch is
**handed over**: each voucher's code, first use and end are kept on the buyer's order (the buyer keeps the history),
the vouchers leave the seller's router (the normal removal channel, retried by router sync) and are then deleted
from the seller's account for good, batch included. The seller keeps only their own income from the batch sale.

Several batches on one network
------------------------------
Customers who buy online on the SELLER's network (the seller's Online sales page) buy from the batches sold on that
network. Each purchase goes to one batch, drawn at random with weights: every batch has a chance, earlier buyers a
bigger one (``weight = 1 + (N − rank) / N``), and a batch close to its sell-by date gets ×1.5 so it finishes on time.
The money goes straight to that batch owner's wallet. The seller may keep a share of these sales for their own stock
(Market › Wallets & shop › "your share"), 0 % by default.
"""
from __future__ import annotations

import logging
import random
from datetime import timedelta
from decimal import Decimal
from functools import wraps

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import redirect
from django.urls import Resolver404, resolve, reverse
from django.utils import timezone

from .models_market import MarketDeal, MarketListing, MarketOrder, MarketShop

logger = logging.getLogger('taptap.market')

ABANDONED_DAYS = 90         # a voucher sold but never used for this long counts as finished
PURGE_AFTER_DAYS = 14       # handed-over vouchers the router never confirmed removing are deleted after this
URGENT_DAYS = 7
URGENT_BOOST = 1.5

# what a seller may NOT do to a voucher / batch the buyer owns (URL names → kind of id in the URL)
LOCKED_VOUCHER_URLS = {'disable_voucher', 'voucher_delete', 'voucher_set_profile', 'voucher_archive_now', 'change_voucher_code',
                       'voucher_rollback', 'voucher_freeze', 'voucher_time_point', 'mark_voucher_missing'}
LOCKED_BATCH_URLS = {'batch_delete', 'batch_freeze', 'batch_assign', 'report_missing_batch'}


# ─────────────────────────────── the lock ───────────────────────────────

def owned_batches(business_id=None):
    qs = MarketDeal.objects.filter(status__in=MarketDeal.OWNED, batch__isnull=False)
    if business_id:
        qs = qs.filter(seller_id=business_id)
    return qs


def owner_of_batch(batch_id):
    d = owned_batches().filter(batch_id=batch_id).select_related('buyer').first()
    return d.buyer.business_name if d else None


def lock_reason(voucher):
    if not getattr(voucher, 'batch_id', None):
        return None
    owner = owner_of_batch(voucher.batch_id)
    return f'owned by {owner} (TapTap Market) — you can support it, not change it' if owner else None


def _wrap_rule(module, name):
    original = getattr(module, name)
    if getattr(original, '_tt_market', False):
        return

    @wraps(original)
    def rule(v, *a, **kw):
        return lock_reason(v) or original(v, *a, **kw)
    rule._tt_market = True
    setattr(module, name, rule)


def _gate_hook():
    """Refuse a seller's change to a voucher or batch the buyer owns, before the view runs."""
    from . import team
    original = team.TeamAccessMiddleware._gate
    if getattr(original, '_tt_market', False):
        return

    @wraps(original)
    def _gate(self, request):
        early = original(self, request)
        if early is not None or request.method != 'POST':
            return early
        try:
            match = resolve(request.path_info)
        except Resolver404:
            return None
        name, pk = match.url_name, match.kwargs.get('pk')
        if name == 'delete_expired':
            try:
                snapshot_finished(getattr(request, 'tt_business', None))
            except Exception:
                logger.exception('market snapshot before delete_expired')
            return None
        if not pk or (name not in LOCKED_VOUCHER_URLS and name not in LOCKED_BATCH_URLS):
            return None
        from .models import Voucher
        batch_id = pk if name in LOCKED_BATCH_URLS else Voucher.all_objects.filter(pk=pk).values_list('batch_id', flat=True).first()
        owner = owner_of_batch(batch_id) if batch_id else None
        if not owner:
            return None
        messages.error(request, f'This {"batch" if name in LOCKED_BATCH_URLS else "voucher"} belongs to {owner} through the TapTap Market '
                                'until its last voucher is used. You can support it (check, fix a login, reset devices) but not change it.')
        return redirect(request.META.get('HTTP_REFERER') or '/vouchers/')
    _gate._tt_market = True
    team.TeamAccessMiddleware._gate = _gate


def install_lock():
    from . import voucher_bin, voucher_freeze
    _wrap_rule(voucher_bin, 'why_not_deletable')
    _wrap_rule(voucher_freeze, 'why_not_freezable')
    _gate_hook()


# ─────────────────────────────── finishing and handing over ───────────────────────────────

def is_finished(v, now):
    if v is None or v.deleted_at:
        return True
    if v.status in ('expired', 'archived'):
        return True
    if v.expires_at and v.expires_at <= now:
        return True
    return bool(v.used_at is None and v.sold_at and v.sold_at < now - timedelta(days=ABANDONED_DAYS))


def snapshot_finished(business):
    """Copy first use / end of market vouchers onto the buyer's orders (before anything can delete them)."""
    if business is None:
        return 0
    n = 0
    for o in MarketOrder.objects.filter(deal__seller=business, voucher__isnull=False).select_related('voucher'):
        v = o.voucher
        if (o.used_at, o.ends_at) != (v.used_at, v.expires_at):
            MarketOrder.objects.filter(pk=o.pk).update(used_at=v.used_at, ends_at=v.expires_at, voucher_code=o.voucher_code or v.code)
            n += 1
    return n


def hand_over(deal, now=None):
    """Every voucher is finished: keep the history with the buyer, take the vouchers off the seller."""
    from .models import Voucher
    now = now or timezone.now()
    with transaction.atomic():
        deal = MarketDeal.objects.select_for_update().select_related('buyer', 'seller', 'listing').get(pk=deal.pk)
        if deal.status != 'completed':
            return False
        vouchers = list(Voucher.all_objects.filter(batch_id=deal.batch_id).select_related('router')) if deal.batch_id else []
        if any(not is_finished(v, now) for v in vouchers):
            return False
        for o in deal.orders.filter(voucher__isnull=False).select_related('voucher'):
            v = o.voucher
            MarketOrder.objects.filter(pk=o.pk).update(voucher_code=o.voucher_code or v.code, used_at=v.used_at, ends_at=v.expires_at)
        live = [v for v in vouchers if not v.deleted_at]
        Voucher.all_objects.filter(pk__in=[v.pk for v in live]).update(
            deleted_at=now, status='disabled', delete_reason=f'Handed over to {deal.buyer.business_name} (TapTap Market)'[:255],
            router_removal='failed', router_removal_note='Waiting to be removed from the router')
        Voucher.all_objects.filter(pk__in=[v.pk for v in live if not v.router_id]).update(router_removal='not_needed', router_removal_note='')
        deal.status, deal.handed_over_at = 'handed_over', now
        deal.save(update_fields=['status', 'handed_over_at', 'updated_at'])
    by_router = {}
    for v in live:
        if v.router_id:
            by_router.setdefault(v.router, []).append(v)
    from .voucher_bin import remove_from_router
    for router, vs in by_router.items():
        try:
            remove_from_router(router, vs)
        except Exception:
            logger.exception('market hand-over: removing from %s', router)
    from .market import _tell
    _tell(deal.buyer, f'Market: {deal.reference} is finished',
          f'All {deal.issued} vouchers of {deal.listing.plan_name} have been used. The batch is closed and its history is yours.',
          reverse('market_deal', args=[deal.reference]))
    _tell(deal.seller, f'Market: {deal.reference} handed over',
          f'The last voucher of the batch owned by {deal.buyer.business_name} has ended. Its vouchers are being removed '
          'from your router and your account.', reverse('market') + '?tab=selling')
    return True


def purge(deal, now=None):
    """Delete handed-over vouchers once the router let them go (or after PURGE_AFTER_DAYS), then the batch."""
    from .models import Voucher, VoucherBatch
    now = now or timezone.now()
    if deal.status != 'handed_over' or not deal.batch_id:
        return 0
    gone = Voucher.all_objects.filter(batch_id=deal.batch_id).filter(
        Q(router_removal__in=('removed', 'not_needed')) | Q(deleted_at__lt=now - timedelta(days=PURGE_AFTER_DAYS)))
    n, _ = gone.delete()
    if not Voucher.all_objects.filter(batch_id=deal.batch_id).exists():
        batch_id = deal.batch_id
        MarketDeal.objects.filter(pk=deal.pk).update(batch=None)
        VoucherBatch.all_objects.filter(pk=batch_id).delete()
    return n


def tick(now=None, payouts=True):
    """Everything time-based (Celery every 5 minutes; cheap parts also run when market pages open)."""
    from .market import sweep
    now = now or timezone.now()
    sweep(now)
    today = timezone.localdate(now)
    out = {'closed': 0, 'handed_over': 0, 'purged': 0, 'payouts': 0}
    for d in MarketDeal.objects.filter(status='active', listing__sell_by__lt=today):
        out['closed'] += MarketDeal.objects.filter(pk=d.pk, status='active').update(
            status='completed', closed_at=now, note='Sell-by date reached: no more sales from this batch.')
    for d in MarketDeal.objects.filter(status='completed'):
        try:
            out['handed_over'] += int(hand_over(d, now))
        except Exception:
            logger.exception('market hand-over of %s', d.reference)
    for d in MarketDeal.objects.filter(status='handed_over', batch__isnull=False):
        try:
            out['purged'] += purge(d, now)
        except Exception:
            logger.exception('market purge of %s', d.reference)
    try:
        from .market_vetting import watch_sellers
        out['watched'] = watch_sellers(now)
    except Exception:
        logger.exception('market seller watch')
    if payouts:
        from .market_wave import check_payouts
        try:
            out['payouts'] = check_payouts()
        except Exception:
            logger.exception('market payouts')
    return out


# ─────────────────────────────── the queue on one network ───────────────────────────────

def _weights(deals, today):
    n = len(deals)
    out = []
    for rank, d in enumerate(deals, 1):
        w = 1 + (n - rank) / n
        sb = d.listing.sell_by
        if sb and (sb - today).days <= URGENT_DAYS:
            w *= URGENT_BOOST
        out.append(w)
    return out


def network_deals(seller, plan_id=None, plan_name='', selling_only=True):
    """Batches sold on a seller's network for one plan, first bought first."""
    qs = MarketDeal.objects.filter(seller=seller, status__in=('active',) if selling_only else MarketDeal.OWNED)
    qs = qs.filter(Q(listing__plan_id=plan_id) | Q(listing__plan_name=plan_name)) if plan_id else qs.filter(listing__plan_name=plan_name)
    return list(qs.select_related('listing', 'buyer').order_by('confirmed_at', 'pk'))


def queue(seller, plan_id=None, plan_name='', viewer=None, with_new=0):
    """The batches on a network with their rank and share of online customers. ``with_new``: add a would-be batch of
    that many vouchers at the end (for a buyer thinking about buying). Buyers are never named to each other."""
    from .market import available
    today = timezone.localdate()
    deals = [d for d in network_deals(seller, plan_id, plan_name) if available(d) > 0]
    weights = _weights(deals, today)
    if with_new:
        n = len(deals) + 1
        weights = [1 + (n - r) / n for r in range(1, n + 1)]
        for i, d in enumerate(deals):
            sb = d.listing.sell_by
            if sb and (sb - today).days <= URGENT_DAYS:
                weights[i] *= URGENT_BOOST
    total = sum(weights) or 1
    rows = [{'rank': i + 1, 'bought': d.confirmed_at, 'quantity': d.quantity, 'sold': d.issued, 'left': d.remaining,
             'share': round(weights[i] / total * 100), 'you': viewer is not None and d.buyer_id == viewer.pk,
             'reference': d.reference if viewer is not None and d.buyer_id == viewer.pk else ''} for i, d in enumerate(deals)]
    new = {'rank': len(deals) + 1, 'share': round(weights[-1] / total * 100), 'quantity': with_new} if with_new else None
    return {'rows': rows, 'new': new, 'ahead': sum(r['left'] for r in rows), 'batches': len(rows)}


def rank_of(deal):
    deals = network_deals(deal.seller, deal.listing.plan_id, deal.listing.plan_name, selling_only=False)
    ids = [d.pk for d in deals]
    return (ids.index(deal.pk) + 1 if deal.pk in ids else len(ids) + 1), max(1, len(ids))


def pick(seller, plan, rnd=random):
    """Which batch serves the next online customer of this plan on this network (None: the seller's own sale)."""
    from .market import available, best_wallet
    shop = MarketShop.objects.filter(business=seller).first()
    if shop and shop.own_share and rnd.random() * 100 < shop.own_share:
        return None
    deals = [d for d in network_deals(seller, plan.pk, plan.name) if available(d) > 0 and best_wallet(d.buyer)]
    if not deals:
        return None
    return rnd.choices(deals, weights=_weights(deals, timezone.localdate()), k=1)[0]


def online_daily(seller, plan_id, plan_name, days=30):
    """Customers buying this plan ONLINE on this network per day (Online sales + market sales routed there)."""
    from .models_collaboration_buy import OnlineVoucherPurchase
    since = timezone.now() - timedelta(days=days)
    match = (Q(plan_id=plan_id) | Q(plan__name=plan_name)) if plan_id else Q(plan__name=plan_name)
    a = OnlineVoucherPurchase.objects.filter(business=seller, status='issued', created_at__gte=since).filter(match).count()
    b = MarketOrder.objects.filter(deal__seller=seller, channel='network', status='issued', created_at__gte=since,
                                   deal__listing__plan_name=plan_name).count()
    return round((a + b) / days, 2)


def outlook(listing, viewer=None):
    """What buying this listing would look like for ``viewer``: rank, share, speed, advice."""
    from .market_diligence import pack
    dd = pack(listing)
    q = queue(listing.business, listing.plan_id, listing.plan_name, viewer=viewer, with_new=listing.quantity)
    daily_online = online_daily(listing.business, listing.plan_id, listing.plan_name)
    mine = daily_online * q['new']['share'] / 100
    days = int(-(-listing.quantity // mine)) if mine else None
    return {'queue': q, 'online_daily': daily_online, 'your_daily': round(mine, 2), 'days': days,
            'advice': advice(listing, dd, q, daily_online, mine, days)}


def advice(listing, dd, q, daily_online, mine, days):
    """A few plain sentences — the figures already shown, put into words. Never anything private."""
    out = []
    up = dd['uptime']['pct_30']
    if up >= 99.5:
        out.append(('good', 'bi-wifi', f'The network was up {up}% of the last 30 days — customers rarely lose their time.'))
    elif up < 97:
        out.append(('warn', 'bi-wifi-off', f'The network was down {round(100 - up, 1)}% of the last 30 days. Outages bring complaints to whoever sold the voucher.'))
    if q['batches']:
        out.append(('info', 'bi-list-ol', f'{q["batches"]} batch{"es" if q["batches"] != 1 else ""} of this plan already sell here, '
                    f'{q["ahead"]} vouchers ahead of you. As #{q["new"]["rank"]} you would get about {q["new"]["share"]}% of online buyers '
                    '— more as the batches before you finish.'))
    else:
        out.append(('good', 'bi-1-circle', 'No other batch of this plan sells here: you would get every online buyer of it.'))
    if daily_online < 0.5:
        out.append(('warn', 'bi-megaphone', 'Few customers buy this plan online on this network yet. Plan to sell most of it yourself '
                    '(your shop link on WhatsApp, a QR poster where people wait).'))
    elif days:
        out.append(('info', 'bi-hourglass-split', f'At today\'s pace, online buyers alone would finish it in about {days} days '
                    f'({mine:.1f} a day).'))
    if listing.sell_by and days and (listing.sell_by - timezone.localdate()).days < days:
        out.append(('warn', 'bi-calendar-x', 'That is after the sell-by date: what is not sold by then stops selling. Consider a smaller batch.'))
    if mine:
        good = int(mine * 30)
        if good and good < listing.quantity * 0.5:
            out.append(('info', 'bi-box', f'About {good} vouchers a month would sell through the network — a batch near that size turns over faster.'))
    m = listing.margin_pct
    if m < 10:
        out.append(('warn', 'bi-percent', f'The margin is thin ({m}%). Mobile-money fees and slow days can eat it.'))
    elif m >= 25:
        out.append(('good', 'bi-percent', f'A {m}% margin leaves room for a small discount on your own shop.'))
    t = dd['users']['trend_pct']
    if t is not None and t <= -15:
        out.append(('warn', 'bi-graph-down-arrow', f'Fewer devices than the month before ({t:+.0f}%). Ask the seller why.'))
    elif t is not None and t >= 15:
        out.append(('good', 'bi-graph-up-arrow', f'More devices than the month before ({t:+.0f}%).'))
    return [{'tone': a, 'icon': b, 'text': c} for a, b, c in out[:6]]


def monitor(deal):
    """The buyer's view of their batch: sold, in use, finished, by channel and by day (their own customers only)."""
    now = timezone.now()
    orders = list(deal.orders.filter(status='issued').select_related('voucher'))
    in_use = finished = unused = 0
    for o in orders:
        v = o.voucher
        if is_finished(v, now):
            finished += 1
        elif v.used_at:
            in_use += 1
        else:
            unused += 1
    since = timezone.localdate() - timedelta(days=29)
    per = {}
    days = [since + timedelta(days=i) for i in range(30)]
    for o in orders:
        if o.issued_at:
            d = timezone.localdate(o.issued_at)
            if d >= since:
                per[d] = per.get(d, 0) + 1
    top = max(per.values() or [0])
    by = deal.orders.filter(status='issued').values('channel').annotate(n=Count('id'), v=Sum('amount'))
    rank, total = rank_of(deal) if deal.status in MarketDeal.OWNED else (None, None)
    share = None
    if deal.status == 'active':
        q = queue(deal.seller, deal.listing.plan_id, deal.listing.plan_name, viewer=deal.buyer)
        share = next((r['share'] for r in q['rows'] if r['you']), None)
    return {'sold': len(orders), 'in_use': in_use, 'finished': finished, 'unused': unused,
            'days': [{'d': d, 'n': per.get(d, 0), 'h': round(per.get(d, 0) / top * 100) if top else 0} for d in days],
            'channels': {r['channel']: {'n': r['n'], 'v': r['v'] or Decimal('0')} for r in by},
            'rank': rank, 'of': total, 'share': share,
            'payouts': list(deal.buyer.market_payouts.filter(order__deal=deal).order_by('-created_at')[:10])}


# ─────────────────────────────── customers on the seller's network ───────────────────────────────

def _store_hook():
    """The seller's Online sales page: a customer buying a plan that sold batches cover is served from one of them."""
    from . import online_buy, urls
    original = online_buy.online_buy_store
    if getattr(original, '_tt_market', False):
        return

    @wraps(original)
    def online_buy_store(request, slug):
        if request.method == 'POST':
            try:
                routed = _route_network_sale(request, slug)
            except Exception:
                logger.exception('market routing on the online shop %s', slug)
                routed = None
            if routed is not None:
                return routed
        return original(request, slug)
    online_buy_store._tt_market = True
    online_buy.online_buy_store = online_buy_store
    for p in urls.urlpatterns:
        if getattr(p, 'name', None) == 'online_buy_store':
            p.callback = online_buy_store


def _route_network_sale(request, slug):
    from .market import MarketError, _ip, best_wallet, create_order, start_payment
    from .models_collaboration_buy import OnlineStorefront
    from .online_buy import _store_plans
    store = OnlineStorefront.objects.select_related('business').filter(public_slug=slug, enabled=True).first()
    if not store:
        return None
    plan = _store_plans(store).filter(pk=request.POST.get('plan')).first()
    if not plan:
        return None
    deal = pick(store.business, plan)
    if deal is None:
        return None
    wallet = best_wallet(deal.buyer, request.POST.get('provider')) or best_wallet(deal.buyer)
    try:
        order = create_order(deal.pk, wallet.provider, request.POST.get('phone'), request.POST.get('name'), _ip(request),
                             channel='network', wallet=wallet)
        return redirect(start_payment(order, request))
    except MarketError as e:
        messages.error(request, str(e))
        return redirect('online_buy_store', slug=slug)


def _platform_settle_hook():
    """TapTap's own Wave webhook (Online sales) also carries market payments (references MKT-…)."""
    from . import online_buy
    original = online_buy.settle_provider_event
    if getattr(original, '_tt_market', False):
        return

    @wraps(original)
    def settle_provider_event(provider_code, event):
        if str(event.get('reference') or '').startswith('MKT-'):
            from .market import settle
            return settle(event, route='platform')
        return original(provider_code, event)
    settle_provider_event._tt_market = True
    online_buy.settle_provider_event = settle_provider_event


def install():
    install_lock()
    _store_hook()
    _platform_settle_hook()


# ─────────────────────────────── Fit (privacy-safe: due diligence figures and anonymous queues only) ───────────────────────────────

def fit_listings(ctx, q='', sort='score', **_):
    from .fit_tools import money
    from .market_diligence import pack
    from .market_vetting import vetted_q
    qs = MarketListing.objects.filter(status='open').filter(vetted_q()).exclude(business=ctx.business).select_related('business')
    if q:
        qs = qs.filter(Q(area__icontains=q) | Q(plan_name__icontains=q) | Q(title__icontains=q) | Q(code__iexact=q))
    rows = []
    for l in qs[:30]:
        dd = pack(l)
        o = outlook(l, ctx.business)
        rows.append({'code': l.code, 'title': l.title or l.plan_name, 'where': dd['identity']['label'], 'vouchers': f'{l.quantity}× {l.plan_name}',
                     'price': money(ctx, l.price), 'worth': money(ctx, l.face_value), 'margin_pct': l.margin_pct,
                     'grade': dd['score']['grade'], 'score': dd['score']['value'], 'uptime_30d_pct': dd['uptime']['pct_30'],
                     'devices_per_day': dd['users']['avg'], 'sales_last_30d': dd['sales']['last30_text'],
                     'batches_ahead': o['queue']['batches'], 'vouchers_ahead': o['queue']['ahead'], 'your_rank': o['queue']['new']['rank'],
                     'your_share_of_online_buyers_pct': o['queue']['new']['share'], 'days_to_sell_online': o['days'],
                     'link': reverse('market_listing', args=[l.code])})
    key = {'margin': lambda r: -r['margin_pct'], 'fast': lambda r: r['days_to_sell_online'] or 10 ** 6}.get(sort, lambda r: -r['score'])
    rows.sort(key=key)
    return {'listings': rows[:12], 'privacy': 'Only TapTap-read figures each seller agreed to share; no customer or private data.'}


def fit_advice(ctx, code='', **_):
    from .fit_tools import ToolError
    from .market_diligence import pack
    from .market_vetting import vetted_q
    l = MarketListing.objects.filter(code__iexact=str(code).strip(), status__in=('open', 'reserved')).filter(vetted_q()).first()
    if not l:
        raise ToolError('No listing with that code is on the market.')
    dd, o = pack(l), outlook(l, ctx.business)
    return {'code': l.code, 'grade': dd['score']['grade'], 'why': [f"{p['label']}: {p['why']}" for p in dd['score']['parts']],
            'queue': [{'rank': r['rank'], 'bought': r['bought'].date().isoformat() if r['bought'] else '', 'left': r['left'],
                       'share_pct': r['share'], 'yours': r['you']} for r in o['queue']['rows']],
            'if_you_buy': {'rank': o['queue']['new']['rank'], 'share_pct': o['queue']['new']['share'],
                           'online_buyers_per_day_for_this_plan': o['online_daily'], 'days_to_sell_online': o['days']},
            'advice': [a['text'] for a in o['advice']], 'link': reverse('market_listing', args=[l.code])}
