"""Due diligence for a Market listing — the seller's figures, read by TapTap, with the seller's privacy kept.

Every figure comes from what TapTap recorded itself (the books, the router's sessions, its connection history); the
seller cannot type or edit any of it. What it never shows, whatever the listing says: customers' names, phones, devices
(MACs), voucher codes, the router's address, team or agent names, single sales.

Privacy controls (per listing)
------------------------------
* ``show_name`` — the business name, or only the area the seller typed ("Brikama, near the market");
* ``precision`` — money as exact figures, rounded figures (to a step that hides single sales), or trends only
  (an index where the average day is 100: no money amount at all);
* ``share_costs`` — monthly running costs and margin, only if the seller says so;
* counts below ``FEW`` (5) — a day with 1–4 devices — are shown as "fewer than 5", never the number.

Figures (``pack(listing)``, JSON-ready, cached 10 minutes)
---------------------------------------------------------
sales per day and their trend, the listed plan's own sales (the base of the forecast), devices online per day and per
hour, uptime and outages of the router the vouchers run on, complaints per 100 sales, how many printed vouchers get
used, the seller's record on the market, a sell-out forecast for the batch and a score (A–E) with its reasons.
Market income itself (references starting ``MKT-``) is left out, so a seller cannot dress up their figures by trading.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Count, Q, Sum
from django.db.models.functions import ExtractHour, TruncDate
from django.utils import timezone

WINDOW = 90
FEW = 5
MOBILE = ('wave', 'qmoney', 'afrimoney')
MARKET_REF = 'MKT-'
CACHE_SECONDS = 600


# ─────────────────────────────── outages (durable) ───────────────────────────────

def record_outage(router, started, ended, why=''):
    """One stretch of 'unreachable', kept for good (uptime_guard keeps only the last 30 in the cache)."""
    from .models_market import RouterOutage
    if not started or not ended or ended <= started:
        return None
    secs = int((ended - started).total_seconds())
    if secs < 60:                       # a blip is not an outage (uptime_guard does not alert on it either)
        return None
    row, _ = RouterOutage.objects.get_or_create(router=router, started_at=started,
                                                defaults={'ended_at': ended, 'seconds': secs, 'why': _why(why)})
    return row


def backfill(router):
    """Copy what uptime_guard still remembers into the durable log (once per day per router)."""
    if not cache.add(f'tt:mkt:backfill:{router.pk}', 1, 86400):
        return
    from . import uptime_guard
    for e in uptime_guard.history(router.pk):
        if e.get('event') != 'up':
            continue
        ended = uptime_guard._dt(e.get('at'))
        secs = int(e.get('secs') or 0)
        if ended and secs:
            try:
                record_outage(router, ended - timedelta(seconds=secs), ended, e.get('why', ''))
            except Exception:
                pass


def _why(text):
    """A cause in plain words — never the raw text (it may hold addresses)."""
    t = str(text or '').lower()
    if any(w in t for w in ('power', 'reboot', 'restart', 'shutdown')):
        return 'Power or restart'
    if any(w in t for w in ('internet', 'wan', 'isp', 'line', 'dns', 'gateway')):
        return 'Internet line'
    if any(w in t for w in ('link', 'tunnel', 'check-in', 'checkin')):
        return 'Lost contact with TapTap'
    return 'Unreachable'


# ─────────────────────────────── privacy helpers ───────────────────────────────

def _nice_step(top):
    """A round step ~ 1/20 of the largest value: 1, 2, 5, 10, 20, 50…"""
    if top <= 0:
        return 1
    raw = top / 20
    mag = 10 ** math.floor(math.log10(raw)) if raw >= 1 else 1
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            return int(m * mag)
    return int(10 * mag)


class Money:
    """Shows money as the listing allows."""

    def __init__(self, precision, currency, top):
        self.precision, self.currency = precision, currency or 'D'
        self.step = _nice_step(float(top or 0)) if precision == 'rounded' else 1

    def __call__(self, v):
        if self.precision == 'index' or v is None:
            return None
        v = float(v)
        if self.precision == 'rounded':
            return float(round(v / self.step) * self.step)
        return round(v, 2)

    def text(self, v):
        x = self(v)
        if x is None:
            return 'hidden'
        approx = '≈ ' if self.precision == 'rounded' else ''
        return f'{approx}{self.currency}{x:,.0f}' if x == int(x) else f'{approx}{self.currency}{x:,.2f}'


def _few(n):
    return n is not None and 0 < n < FEW


def _pct(a, b):
    return round((a - b) / b * 100, 1) if b else (100.0 if a else None)


# ─────────────────────────────── the pack ───────────────────────────────

def pack(listing, fresh=False):
    key = f'tt:mkt:dd:{listing.pk}:{int(listing.updated_at.timestamp()) if listing.updated_at else 0}'
    if not fresh:
        hit = cache.get(key)
        if hit:
            return hit
    data = build(listing)
    cache.set(key, data, CACHE_SECONDS)
    return data


def build(listing, now=None):
    now = now or timezone.now()
    business, router = listing.business, listing.router
    today = timezone.localdate(now)
    start_day = today - timedelta(days=WINDOW - 1)
    start = timezone.make_aware(timezone.datetime.combine(start_day, timezone.datetime.min.time()))
    days = [start_day + timedelta(days=i) for i in range(WINDOW)]
    since = max(start, business.created_at or start)
    since_day = timezone.localdate(since)
    span_days = max(1, (today - since_day).days + 1)

    # ── sales (the business's own sales; market income left out) ──
    sales = business.sales.filter(sold_at__gte=start).exclude(reference__startswith=MARKET_REF)
    per = {r['d']: r for r in sales.annotate(d=TruncDate('sold_at')).values('d').annotate(v=Sum('amount'), n=Count('id'))}
    amounts = [float((per.get(d) or {}).get('v') or 0) for d in days]
    counts = [int((per.get(d) or {}).get('n') or 0) for d in days]
    live_amounts = [a for d, a in zip(days, amounts) if d >= since_day]
    avg = sum(live_amounts) / len(live_amounts) if live_amounts else 0
    money = Money(listing.precision, business.currency, max(amounts or [0]))
    total = sum(amounts)
    last30, prev30 = sum(amounts[-30:]), sum(amounts[-60:-30])
    by_method = {r['payment_method']: float(r['v'] or 0) for r in sales.values('payment_method').annotate(v=Sum('amount'))}
    mobile = sum(v for k, v in by_method.items() if k in MOBILE)
    plan_n = {r['d']: r['n'] for r in sales.filter(plan_name=listing.plan_name).annotate(d=TruncDate('sold_at'))
              .values('d').annotate(n=Count('id'))}
    plan_counts = [int(plan_n.get(d) or 0) for d in days]
    plan_daily30 = sum(plan_counts[-30:]) / min(30, span_days)
    weekday = [0.0] * 7
    wd_n = [0] * 7
    for d, a in zip(days, amounts):
        if d >= since_day:
            weekday[d.weekday()] += a
            wd_n[d.weekday()] += 1
    weekday_idx = [round((weekday[i] / wd_n[i]) / avg * 100) if wd_n[i] and avg else 0 for i in range(7)]
    best_i = max(range(WINDOW), key=lambda i: amounts[i]) if any(amounts) else None
    top = max(amounts or [0])
    sales_part = {
        'total': money(total), 'total_text': money.text(total), 'avg_day_text': money.text(avg),
        'last30_text': money.text(last30), 'trend_pct': _pct(last30, prev30) if span_days > 30 else None,
        'count_last30': int(sum(counts[-30:])), 'active_days': sum(1 for d, a in zip(days, amounts) if a and d >= since_day),
        'mobile_share_pct': round(mobile / total * 100, 1) if total else 0,
        'plan_daily': round(plan_daily30, 1), 'plan_last30': int(sum(plan_counts[-30:])),
        'best_day': days[best_i].isoformat() if best_i is not None else None,
        'weekday_index': weekday_idx,
        'per_day': [{'d': d.isoformat(), 'v': money(a), 'i': round(a / avg * 100) if avg else 0, 'n': n if not _few(n) else None,
                     'few': _few(n), 'h': round(a / top * 100) if top else 0} for d, a, n in zip(days, amounts, counts)],
        'step': money.step if listing.precision == 'rounded' else None,
    }

    # ── devices connected (on the router the vouchers run on) ──
    from .models import UsageRecord, Voucher
    usage = UsageRecord.objects.filter(router=router, hour__gte=start) if router else UsageRecord.objects.none()
    dev = {r['d']: r['n'] for r in usage.annotate(d=TruncDate('hour')).values('d')
           .annotate(n=Count('mac_address', distinct=True, filter=~Q(mac_address='')) + Count('username', distinct=True, filter=Q(mac_address='')))}
    source = 'devices'
    if not dev:
        source = 'first_logins'
        dev = {r['d']: r['n'] for r in (Voucher.all_objects.filter(router=router, used_at__gte=start) if router else Voucher.objects.none())
               .annotate(d=TruncDate('used_at')).values('d').annotate(n=Count('id'))}
        if not dev:
            source = 'none'
    users = [int(dev.get(d) or 0) for d in days]
    live_users = [u for d, u in zip(days, users) if d >= since_day]
    u_avg = sum(live_users) / len(live_users) if live_users else 0
    hours = [0.0] * 24
    if source == 'devices':
        recent = usage.filter(hour__gte=now - timedelta(days=30))
        per_hour = defaultdict(int)
        for r in recent.annotate(h=ExtractHour('hour')).values('h').annotate(n=Count('id')):
            per_hour[r['h']] = r['n']
        n_days = max(1, min(30, span_days))
        hours = [round(per_hour.get(h, 0) / n_days, 1) for h in range(24)]
    u_top, h_top = max(users or [0]), max(hours or [0])
    users_part = {
        'source': source, 'avg': round(u_avg), 'peak': max(users or [0]) if max(users or [0]) >= FEW else None,
        'trend_pct': _pct(sum(users[-30:]), sum(users[-60:-30])) if span_days > 30 else None,
        'per_day': [{'d': d.isoformat(), 'v': None if _few(u) else u, 'few': _few(u), 'h': round(u / u_top * 100) if u_top else 0}
                    for d, u in zip(days, users)],
        'hours': [{'h': h, 'v': v, 'a': round(0.06 + 0.94 * v / h_top, 2) if h_top else 0.06} for h, v in enumerate(hours)],
    }

    # ── uptime of that router ──
    from . import uptime_guard
    from .models_market import RouterOutage
    try:
        if router:
            backfill(router)
    except Exception:
        pass
    mon_from = max(start, (router.created_at if router else None) or start)
    span = max(1.0, (now - mon_from).total_seconds())
    out = list(RouterOutage.objects.filter(router=router, ended_at__gt=mon_from, started_at__lt=now).order_by('-started_at')) if router else []
    down = sum((min(o.ended_at, now) - max(o.started_at, mon_from)).total_seconds() for o in out)
    ongoing = uptime_guard.down_since(router.pk) if router else None
    if ongoing:
        down += max(0, (now - max(ongoing, mon_from)).total_seconds())
    m30 = max(mon_from, now - timedelta(days=30))
    down30 = sum((min(o.ended_at, now) - max(o.started_at, m30)).total_seconds() for o in out if o.ended_at > m30)
    if ongoing:
        down30 += max(0, (now - max(ongoing, m30)).total_seconds())
    span30 = max(1.0, (now - m30).total_seconds())
    uptime_part = {
        'pct': round(100 - down / span * 100, 2), 'pct_30': round(100 - down30 / span30 * 100, 2),
        'outages': len(out) + (1 if ongoing else 0), 'total_min': int(down // 60),
        'longest_min': int(max([o.seconds for o in out] or [0]) // 60),
        'online_now': bool(router) and not ongoing and str(router.status).lower() == 'online',
        'monitored_days': max(1, int(span // 86400)),
        'list': [{'d': timezone.localtime(o.started_at).strftime('%d %b %Y %H:%M'), 'min': max(1, o.seconds // 60), 'why': o.why}
                 for o in out[:12]],
        'per_day': _down_per_day(out, days, mon_from, now),
    }

    # ── quality ──
    from .models_voucher_reports import VoucherReport
    complaints = VoucherReport.objects.filter(business=business, created_at__gte=start).count()
    n_sales = sum(counts)
    made = Voucher.all_objects.filter(business=business, source='taptap', created_at__gte=start, created_at__lt=now - timedelta(days=7),
                                      login_type='voucher')
    made_n = made.count()
    used_n = made.filter(used_at__isnull=False).count()
    quality = {'complaints_per_100': round(complaints / n_sales * 100, 1) if n_sales else 0,
               'activation_pct': round(used_n / made_n * 100) if made_n >= FEW else None}

    # ── costs (only when the seller shares them) ──
    costs = None
    if listing.share_costs:
        exp = float(business.expenses.filter(paid_at__gte=start).aggregate(v=Sum('amount'))['v'] or 0)
        months = max(1.0, span_days / 30.0)
        costs = {'monthly_text': money.text(exp / months),
                 'margin_pct': round((total - exp) / total * 100, 1) if total else None}

    # ── the seller's record on the market ──
    from .models_market import MarketDeal
    done = MarketDeal.objects.filter(seller=business, status__in=('active', 'completed'))
    rated = done.filter(rating__isnull=False)
    record = {'deals': done.count(), 'delivered': int(done.aggregate(v=Sum('issued'))['v'] or 0),
              'rating': round(sum(d.rating for d in rated) / rated.count(), 1) if rated.exists() else None,
              'reviews': [{'stars': d.rating, 'text': d.review} for d in rated.exclude(review='').order_by('-confirmed_at')[:3]]}

    # ── forecast for this batch ──
    forecast = {'plan_daily': round(plan_daily30, 2), 'quantity': listing.quantity,
                'face_value_text': f'{business.currency}{float(listing.face_value):,.0f}',
                'price_text': f'{business.currency}{float(listing.price):,.0f}',
                'margin_text': f'{business.currency}{float(listing.margin):,.0f}', 'margin_pct': listing.margin_pct,
                'unit_cost_text': f'{business.currency}{float(listing.unit_cost):,.2f}',
                'days': {s: (math.ceil(listing.quantity / (plan_daily30 * s / 100)) if plan_daily30 else None) for s in (10, 25, 50, 100)}}

    data = {
        'generated_at': timezone.localtime(now).strftime('%d %b %Y %H:%M'), 'window_days': WINDOW,
        'from': start_day.isoformat(), 'to': today.isoformat(), 'precision': listing.precision, 'currency': business.currency,
        'history_days': span_days,
        'identity': {'label': business.business_name if listing.show_name else listing.area, 'area': listing.area,
                     'named': listing.show_name, 'account_age_days': (today - timezone.localdate(business.created_at)).days if business.created_at else 0,
                     'routers': business.routers.count()},
        'sales': sales_part, 'users': users_part, 'uptime': uptime_part, 'quality': quality, 'costs': costs,
        'record': record, 'forecast': forecast,
    }
    data['score'] = score(data)
    return data


def _down_per_day(outages, days, mon_from, now):
    """Minutes unreachable per day (for the strip of 90 squares)."""
    mins = defaultdict(float)
    for o in outages:
        a, b = max(o.started_at, mon_from), min(o.ended_at, now)
        while a < b:
            day = timezone.localdate(a)
            nxt = timezone.make_aware(timezone.datetime.combine(day + timedelta(days=1), timezone.datetime.min.time()))
            seg_end = min(b, nxt)
            mins[day] += (seg_end - a).total_seconds() / 60
            a = seg_end
    first = timezone.localdate(mon_from)
    def level(m, watched):
        if not watched:
            return 'nw'
        return '' if m < 1 else 'l1' if m < 15 else 'l2' if m < 60 else 'l3' if m < 240 else 'l4'
    return [{'d': d.isoformat(), 'min': int(mins.get(d, 0)), 'l': level(mins.get(d, 0), d >= first)} for d in days]


def _clamp(x):
    return max(0.0, min(1.0, x))


def score(data):
    up, s, u, q = data['uptime'], data['sales'], data['users'], data['quality']
    days = max(1, min(WINDOW, data['history_days']))
    parts = [
        ('Uptime', 35 * _clamp((up['pct'] - 90) / 10), 35, f"{up['pct']}% online over {up['monitored_days']} days"),
        ('Steady sales', 20 * _clamp(s['active_days'] / days), 20, f"sales on {s['active_days']} of {days} days"),
        ('Customers trend', 15 * _clamp(0.5 + (u['trend_pct'] or 0) / 200), 15,
         'devices ' + (f"{u['trend_pct']:+.0f}% vs the 30 days before" if u['trend_pct'] is not None else 'not enough history yet')),
        ('Few complaints', 15 * _clamp(1 - q['complaints_per_100'] / 5), 15, f"{q['complaints_per_100']} per 100 sales"),
        ('History', 15 * _clamp(days / WINDOW), 15, f'{days} days of data'),
    ]
    value = round(sum(p[1] for p in parts))
    grade = 'A' if value >= 80 else 'B' if value >= 65 else 'C' if value >= 50 else 'D' if value >= 35 else 'E'
    return {'value': value, 'grade': grade,
            'parts': [{'label': a, 'pts': round(b, 1), 'max': c, 'why': d} for a, b, c, d in parts]}
