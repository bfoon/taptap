"""Finance & reporting services.

Everything that turns vouchers, sales, expenses and agent remittances into
numbers lives here so the Finance page, the Reports page, the JSON chart
endpoints and the CSV exports all agree with each other.
"""
from collections import OrderedDict, defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Sum, Q, F
from django.db.models.functions import TruncDay, TruncHour, TruncMonth, ExtractWeekDay, ExtractHour
from django.utils import timezone

from .models import (
    Voucher, VoucherSale, Expense, CashCollection, Agent, EXPENSE_CATEGORIES, PAYMENT_METHODS,
)

ZERO = Decimal('0')
PRESETS = OrderedDict([
    ('today', 'Today'), ('7d', 'Last 7 days'), ('30d', 'Last 30 days'), ('90d', 'Last 90 days'),
    ('month', 'This month'), ('last_month', 'Last month'), ('year', 'This year'), ('custom', 'Custom'),
])
CATEGORY_LABELS = dict(EXPENSE_CATEGORIES)
METHOD_LABELS = dict(PAYMENT_METHODS)


def d(value):
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


# ───────────────────────────── periods ─────────────────────────────
class Period:
    def __init__(self, start, end, preset, label):
        self.start, self.end, self.preset, self.label = start, end, preset, label
        span = end - start
        self.prev_end = start
        self.prev_start = start - span
        days = max(1, span.days + (1 if span.seconds else 0))
        self.days = days
        # Bucket size keeps charts readable: hours for a day, months for > ~4 months.
        self.bucket = 'hour' if days <= 2 else ('month' if days > 124 else 'day')

    def as_dict(self):
        return {'start': timezone.localtime(self.start).date().isoformat(),
                'end': (timezone.localtime(self.end) - timedelta(seconds=1)).date().isoformat(),
                'preset': self.preset, 'label': self.label, 'bucket': self.bucket, 'days': self.days}


def _day_start(day):
    return timezone.make_aware(datetime.combine(day, time.min))


def resolve_period(params, default='30d'):
    preset = params.get('range') or default
    today = timezone.localdate()
    if preset == 'custom':
        try:
            s = datetime.strptime(params.get('start', ''), '%Y-%m-%d').date()
            e = datetime.strptime(params.get('end', ''), '%Y-%m-%d').date()
            if e < s: s, e = e, s
            return Period(_day_start(s), _day_start(e + timedelta(days=1)), 'custom', f'{s:%d %b %Y} – {e:%d %b %Y}')
        except ValueError:
            preset = default
    if preset == 'today':
        return Period(_day_start(today), _day_start(today + timedelta(days=1)), preset, 'Today')
    if preset in {'7d', '30d', '90d'}:
        n = int(preset[:-1])
        return Period(_day_start(today - timedelta(days=n - 1)), _day_start(today + timedelta(days=1)), preset, PRESETS[preset])
    if preset == 'month':
        s = today.replace(day=1)
        return Period(_day_start(s), _day_start(today + timedelta(days=1)), preset, f'{s:%B %Y}')
    if preset == 'last_month':
        e = today.replace(day=1); s = (e - timedelta(days=1)).replace(day=1)
        return Period(_day_start(s), _day_start(e), preset, f'{s:%B %Y}')
    if preset == 'year':
        s = today.replace(month=1, day=1)
        return Period(_day_start(s), _day_start(today + timedelta(days=1)), preset, f'{s:%Y} to date')
    return resolve_period({'range': default}, default)


def _buckets(period):
    """Ordered list of (key, label) for every bucket in the period, so gaps render as zero."""
    out = []
    tz = timezone.get_current_timezone()
    cur = timezone.localtime(period.start)
    end = timezone.localtime(period.end)
    if period.bucket == 'hour':
        cur = cur.replace(minute=0, second=0, microsecond=0)
        while cur < end:
            out.append((cur.strftime('%Y-%m-%d %H'), cur.strftime('%H:00') if period.days <= 1 else cur.strftime('%a %H:00')))
            cur += timedelta(hours=1)
    elif period.bucket == 'month':
        cur = cur.replace(day=1)
        while cur < end:
            out.append((cur.strftime('%Y-%m'), cur.strftime('%b %Y')))
            cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        day = cur.date()
        while _day_start(day) < period.end:
            out.append((day.isoformat(), day.strftime('%d %b')))
            day += timedelta(days=1)
    return out


def _bucket_key(dt, bucket):
    dt = timezone.localtime(dt) if timezone.is_aware(dt) else dt
    if bucket == 'hour': return dt.strftime('%Y-%m-%d %H')
    if bucket == 'month': return dt.strftime('%Y-%m')
    return dt.date().isoformat() if hasattr(dt, 'date') else dt.isoformat()


def _trunc(bucket, field):
    return {'hour': TruncHour, 'month': TruncMonth}.get(bucket, TruncDay)(field)


def series(qs, date_field, period, value=None, bucket=None):
    """Return a list aligned to _buckets(period) with Sum(value) or Count per bucket."""
    bucket = bucket or period.bucket
    agg = Sum(value) if value else Count('id')
    rows = (qs.filter(**{f'{date_field}__gte': period.start, f'{date_field}__lt': period.end})
              .annotate(_b=_trunc(bucket, date_field)).values('_b').annotate(v=agg).order_by('_b'))
    got = defaultdict(lambda: ZERO)
    for r in rows:
        if r['_b'] is None: continue
        got[_bucket_key(r['_b'], bucket)] += d(r['v'])
    return [float(got.get(k, 0)) for k, _ in _buckets(period)]


def pct_change(cur, prev):
    cur, prev = float(cur or 0), float(prev or 0)
    if prev == 0: return None if cur == 0 else 100.0
    return round((cur - prev) / abs(prev) * 100, 1)


# ───────────────────────────── recording ─────────────────────────────
def effective_price(voucher):
    """A voucher's price, falling back to its plan's price when the voucher was stored at 0."""
    if d(voucher.price) > 0:
        return d(voucher.price)
    plan = voucher.business.plans.filter(name=voucher.plan_name).only('price').first()
    return d(plan.price) if plan else ZERO


def commission_for(agent, amount):
    if not agent: return ZERO
    return (d(amount) * d(agent.commission_percent) / Decimal('100')).quantize(Decimal('0.01'))


AUTO = object()   # "whoever holds the voucher" — the default seller for a voucher sale


@transaction.atomic
def record_sale(business, voucher=None, *, plan_name='', amount=None, method='cash', agent=AUTO,
                customer_name='', customer_phone='', reference='', notes='', discount=ZERO, user=None, when=None):
    when = when or timezone.now()
    if voucher is not None:
        voucher = Voucher.objects.select_for_update().get(pk=voucher.pk)
        if VoucherSale.objects.filter(voucher=voucher).exists():
            return None
        plan_name = plan_name or voucher.plan_name
        if amount is None:
            amount = effective_price(voucher)
            if d(voucher.price) == 0 and amount > 0:
                Voucher.objects.filter(pk=voucher.pk).update(price=amount)
    if agent is AUTO:
        agent = getattr(voucher, 'agent', None) if voucher is not None else None
    gross = max(ZERO, d(amount) - d(discount))
    sale = VoucherSale.objects.create(
        business=business, voucher=voucher, router=getattr(voucher, 'router', None), agent=agent,
        plan_name=plan_name or 'Walk-in', voucher_code=getattr(voucher, 'code', ''), amount=gross, discount=d(discount),
        commission=commission_for(agent, gross), payment_method=method, customer_name=customer_name,
        customer_phone=customer_phone, reference=reference, notes=notes, sold_at=when, recorded_by=user,
    )
    if voucher is not None and not voucher.sold_at:
        Voucher.objects.filter(pk=voucher.pk).update(sold_at=when)
    return sale


def sell_from_stock(business, plan_name, quantity, **kwargs):
    """Pick the oldest unsold active vouchers of a plan and record a sale for each.
    Sold by an agent → take from that agent's stock; sold by the shop → take from the shop's own stock."""
    agent = kwargs.get('agent', AUTO)
    stock = business.vouchers.filter(plan_name=plan_name, status='active', sold_at__isnull=True, used_at__isnull=True)
    stock = stock.filter(agent=agent) if agent not in (AUTO, None) else stock.filter(agent__isnull=True)
    stock = list(stock.order_by('created_at')[:quantity])
    sales = []
    for v in stock:
        s = record_sale(business, v, **kwargs)
        if s: sales.append(s)
    return sales


def mark_activated(voucher, when=None):
    """Called by router sync the first time a voucher shows uptime. Auto-books revenue if enabled."""
    now = timezone.now()
    when = min(when or now, now)
    # A TapTap voucher cannot be used before it was printed. Router-made vouchers were created on the
    # router before TapTap imported them, so their TapTap creation time is no lower bound.
    if voucher.source == 'taptap' and voucher.created_at and when < voucher.created_at:
        when = voucher.created_at
    if voucher.used_at:
        return False
    Voucher.objects.filter(pk=voucher.pk, used_at__isnull=True).update(used_at=when)
    voucher.used_at = when
    business = voucher.business
    if business.auto_record_sales and not voucher.sold_at and effective_price(voucher) > 0 \
            and not VoucherSale.objects.filter(voucher=voucher).exists():
        record_sale(business, voucher, method='auto', notes='Auto-recorded on first router activation', when=when)
    return True


# ───────────────────────────── agents ─────────────────────────────
def agent_balances(business):
    """Per agent: gross sold, commission earned, owed to business, collected, outstanding."""
    sales = {r['agent']: r for r in business.sales.filter(agent__isnull=False).values('agent')
             .annotate(gross=Sum('amount'), comm=Sum('commission'), n=Count('id'))}
    cols = {r['agent']: r['v'] for r in business.collections.values('agent').annotate(v=Sum('amount'))}
    last_col = {r['agent']: r['last'] for r in business.collections.values('agent').annotate(last=models_max('collected_at'))}
    held = {r['agent']: r for r in business.vouchers.filter(agent__isnull=False, status='active', sold_at__isnull=True, used_at__isnull=True)
            .values('agent').annotate(n=Count('id'), v=Sum('price'))}
    out = []
    for a in business.agents.all():
        s = sales.get(a.id, {}); h = held.get(a.id, {})
        gross, comm = d(s.get('gross')), d(s.get('comm'))
        owed = gross - comm; collected = d(cols.get(a.id))
        out.append({'agent': a, 'sold': s.get('n', 0), 'gross': gross, 'commission': comm, 'owed': owed,
                    'collected': collected, 'outstanding': owed - collected, 'last_collection': last_col.get(a.id),
                    'collection_rate': round(float(collected / owed * 100), 1) if owed > 0 else 100.0,
                    'holding': h.get('n', 0), 'holding_value': d(h.get('v'))})
    return sorted(out, key=lambda r: r['outstanding'], reverse=True)


def models_max(field):
    from django.db.models import Max
    return Max(field)


# ───────────────────────────── finance summary ─────────────────────────────
def finance_summary(business, period):
    sales = business.sales.filter(sold_at__gte=period.start, sold_at__lt=period.end)
    prev_sales = business.sales.filter(sold_at__gte=period.prev_start, sold_at__lt=period.prev_end)
    exps = business.expenses.filter(paid_at__gte=period.start, paid_at__lt=period.end)
    prev_exps = business.expenses.filter(paid_at__gte=period.prev_start, paid_at__lt=period.prev_end)

    rev = d(sales.aggregate(v=Sum('amount'))['v']); prev_rev = d(prev_sales.aggregate(v=Sum('amount'))['v'])
    comm = d(sales.aggregate(v=Sum('commission'))['v']); prev_comm = d(prev_sales.aggregate(v=Sum('commission'))['v'])
    disc = d(sales.aggregate(v=Sum('discount'))['v'])
    exp = d(exps.aggregate(v=Sum('amount'))['v']); prev_exp = d(prev_exps.aggregate(v=Sum('amount'))['v'])
    profit = rev - comm - exp; prev_profit = prev_rev - prev_comm - prev_exp
    n = sales.count(); prev_n = prev_sales.count()

    balances = agent_balances(business)
    outstanding = sum((r['outstanding'] for r in balances if r['outstanding'] > 0), ZERO)

    # Cash position: all direct (non-agent) takings + agent collections − expenses, all-time.
    direct_all = d(business.sales.filter(agent__isnull=True).aggregate(v=Sum('amount'))['v'])
    collected_all = d(business.collections.aggregate(v=Sum('amount'))['v'])
    expenses_all = d(business.expenses.aggregate(v=Sum('amount'))['v'])

    # Month target progress is always "this month", independent of the selected period.
    today = timezone.localdate(); m_start = _day_start(today.replace(day=1))
    month_rev = d(business.sales.filter(sold_at__gte=m_start).aggregate(v=Sum('amount'))['v'])
    target = d(business.monthly_revenue_target)
    import calendar
    dim = calendar.monthrange(today.year, today.month)[1]
    projected = month_rev / Decimal(today.day) * Decimal(dim) if today.day else month_rev

    stock = business.vouchers.filter(status='active', sold_at__isnull=True, used_at__isnull=True)
    return {
        'revenue': rev, 'revenue_change': pct_change(rev, prev_rev),
        'commission': comm, 'discounts': disc,
        'expenses': exp, 'expenses_change': pct_change(exp, prev_exp),
        'profit': profit, 'profit_change': pct_change(profit, prev_profit),
        'margin': round(float(profit / rev * 100), 1) if rev > 0 else 0.0,
        'sales_count': n, 'sales_change': pct_change(n, prev_n),
        'avg_ticket': (rev / n).quantize(Decimal('0.01')) if n else ZERO,
        'outstanding': outstanding, 'cash_position': direct_all + collected_all - expenses_all,
        'month_revenue': month_rev, 'target': target, 'projected': projected.quantize(Decimal('1')),
        'target_pct': min(999, round(float(month_rev / target * 100), 1)) if target > 0 else None,
        'stock_count': stock.count(), 'stock_value': d(stock.aggregate(v=Sum('price'))['v']),
        'agents': balances,
    }


def finance_charts(business, period):
    sales = business.sales.all(); exps = business.expenses.all()
    labels = [l for _, l in _buckets(period)]
    rev = series(sales, 'sold_at', period, 'amount')
    comm = series(sales, 'sold_at', period, 'commission')
    exp = series(exps, 'paid_at', period, 'amount')
    profit = [round(r - c - e, 2) for r, c, e in zip(rev, comm, exp)]
    running, cum = 0.0, []
    for p in profit:
        running += p; cum.append(round(running, 2))

    # 12-month P&L regardless of selected period — the "shape of the business".
    today = timezone.localdate()
    y_start = (today.replace(day=1) - timedelta(days=334)).replace(day=1)
    year = Period(_day_start(y_start), _day_start(today + timedelta(days=1)), 'custom', '12 months'); year.bucket = 'month'
    y_labels = [l for _, l in _buckets(year)]
    y_rev = series(sales, 'sold_at', year, 'amount', 'month'); y_exp = series(exps, 'paid_at', year, 'amount', 'month')
    y_comm = series(sales, 'sold_at', year, 'commission', 'month')

    in_p = lambda qs, f: qs.filter(**{f'{f}__gte': period.start, f'{f}__lt': period.end})
    by_cat = list(in_p(exps, 'paid_at').values('category').annotate(v=Sum('amount')).order_by('-v'))
    by_method = list(in_p(sales, 'sold_at').values('payment_method').annotate(v=Sum('amount'), n=Count('id')).order_by('-v'))
    by_site = list(in_p(sales, 'sold_at').values('router__name').annotate(v=Sum('amount')).order_by('-v')[:10])
    exp_site = {r['router__name']: float(r['v']) for r in in_p(exps, 'paid_at').values('router__name').annotate(v=Sum('amount'))}

    # This month vs last month, cumulative by day-of-month.
    m0 = today.replace(day=1); lm = (m0 - timedelta(days=1)).replace(day=1)
    def cum_month(start, days):
        rows = sales.filter(sold_at__gte=_day_start(start), sold_at__lt=_day_start(start + timedelta(days=days))) \
            .annotate(_b=TruncDay('sold_at')).values('_b').annotate(v=Sum('amount'))
        per = defaultdict(float)
        for r in rows: per[timezone.localtime(r['_b']).day if timezone.is_aware(r['_b']) else r['_b'].day] += float(r['v'])
        acc, out = 0.0, []
        for i in range(1, days + 1): acc += per.get(i, 0); out.append(round(acc, 2))
        return out
    import calendar
    this_days = today.day; last_days = calendar.monthrange(lm.year, lm.month)[1]

    return {
        'labels': labels, 'revenue': rev, 'expenses': exp, 'commission': comm, 'profit': profit, 'cumulative': cum,
        'year': {'labels': y_labels, 'revenue': y_rev, 'expenses': y_exp,
                 'profit': [round(r - c - e, 2) for r, c, e in zip(y_rev, y_comm, y_exp)]},
        'categories': {'labels': [CATEGORY_LABELS.get(r['category'], r['category']) for r in by_cat], 'values': [float(r['v']) for r in by_cat]},
        'methods': {'labels': [METHOD_LABELS.get(r['payment_method'], r['payment_method']) for r in by_method],
                    'values': [float(r['v']) for r in by_method], 'counts': [r['n'] for r in by_method]},
        'sites': {'labels': [r['router__name'] or 'Unassigned' for r in by_site], 'revenue': [float(r['v']) for r in by_site],
                  'expenses': [exp_site.get(r['router__name'], 0) for r in by_site]},
        'mom': {'labels': [str(i) for i in range(1, last_days + 1)], 'this': cum_month(m0, this_days), 'last': cum_month(lm, last_days),
                'this_label': f'{m0:%B}', 'last_label': f'{lm:%B}'},
    }


# ───────────────────────────── reports ─────────────────────────────
def report_data(business, period, router_id=None, plan=None):
    V = business.vouchers.all(); S = business.sales.all()
    if router_id:
        V = V.filter(router_id=router_id); S = S.filter(router_id=router_id)
    if plan:
        V = V.filter(plan_name=plan); S = S.filter(plan_name=plan)
    within = lambda qs, f, p=period: qs.filter(**{f'{f}__gte': p.start, f'{f}__lt': p.end})
    prev = type('P', (), {'start': period.prev_start, 'end': period.prev_end})

    gen = within(V, 'created_at').count(); gen_p = within(V, 'created_at', prev).count()
    sold = within(S, 'sold_at'); sold_p = within(S, 'sold_at', prev)
    rev = d(sold.aggregate(v=Sum('amount'))['v']); rev_p = d(sold_p.aggregate(v=Sum('amount'))['v'])
    n_sold = sold.count(); n_sold_p = sold_p.count()
    act = within(V, 'used_at').count(); act_p = within(V, 'used_at', prev).count()

    labels = [l for _, l in _buckets(period)]
    trend = {'labels': labels, 'revenue': series(S, 'sold_at', period, 'amount'), 'sold': series(S, 'sold_at', period),
             'generated': series(V, 'created_at', period), 'activated': series(V, 'used_at', period)}

    plan_rows = list(sold.values('plan_name').annotate(n=Count('id'), v=Sum('amount')).order_by('-v'))
    gen_by_plan = {r['plan_name']: r['n'] for r in within(V, 'created_at').values('plan_name').annotate(n=Count('id'))}
    act_by_plan = {r['plan_name']: r['n'] for r in within(V, 'used_at').values('plan_name').annotate(n=Count('id'))}
    plans_all = sorted(set(gen_by_plan) | {r['plan_name'] for r in plan_rows} | set(act_by_plan))
    plan_table = []
    for name in plans_all:
        r = next((x for x in plan_rows if x['plan_name'] == name), {})
        plan_table.append({'plan': name, 'generated': gen_by_plan.get(name, 0), 'sold': r.get('n', 0),
                           'activated': act_by_plan.get(name, 0), 'revenue': float(r.get('v') or 0),
                           'share': round(float(d(r.get('v')) / rev * 100), 1) if rev > 0 else 0})
    plan_table.sort(key=lambda x: -x['revenue'])

    # Weekday × hour heatmap of sales (Django week_day: 1=Sunday … 7=Saturday).
    heat = [[0] * 24 for _ in range(7)]
    for r in sold.annotate(wd=ExtractWeekDay('sold_at'), hr=ExtractHour('sold_at')).values('wd', 'hr').annotate(n=Count('id')):
        heat[(r['wd'] + 5) % 7][r['hr']] += r['n']   # re-index so Monday = 0

    routers = list(sold.values('router__name').annotate(n=Count('id'), v=Sum('amount')).order_by('-v')[:12])
    agents = list(sold.filter(agent__isnull=False).values('agent__name').annotate(n=Count('id'), v=Sum('amount')).order_by('-v')[:8])

    # Inventory health — unsold stock by age.
    now = timezone.now(); stock = V.filter(status='active', sold_at__isnull=True, used_at__isnull=True)
    aging = []
    for lo, hi, label in [(0, 7, '< 1 week'), (7, 30, '1–4 weeks'), (30, 90, '1–3 months'), (90, 100000, '> 3 months')]:
        q = stock.filter(created_at__lte=now - timedelta(days=lo), created_at__gt=now - timedelta(days=hi))
        aging.append({'label': label, 'count': q.count(), 'value': float(d(q.aggregate(v=Sum('price'))['v']))})

    funnel = [{'label': 'Generated', 'value': gen}, {'label': 'Sold', 'value': n_sold},
              {'label': 'Activated', 'value': act},
              {'label': 'Expired / disabled', 'value': within(V, 'created_at').filter(Q(status__in=['expired', 'disabled'])).count()}]

    best_day = max(zip(trend['labels'], trend['revenue']), key=lambda x: x[1], default=('—', 0))
    peak = max(((i, j, heat[i][j]) for i in range(7) for j in range(24)), key=lambda x: x[2], default=(0, 0, 0))
    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    insights = []
    if rev_p and rev:
        ch = pct_change(rev, rev_p)
        insights.append(f"Revenue is {'up' if ch >= 0 else 'down'} {abs(ch)}% on the previous {period.days} days.")
    if plan_table and plan_table[0]['revenue']:
        insights.append(f"{plan_table[0]['plan']} brings in {plan_table[0]['share']}% of revenue.")
    if peak[2]:
        insights.append(f"Busiest selling window: {days[peak[0]]}s around {peak[1]:02d}:00.")
    if best_day[1]:
        insights.append(f"Best {'hour' if period.bucket == 'hour' else ('month' if period.bucket == 'month' else 'day')}: {best_day[0]} with {business.currency}{best_day[1]:,.0f}.")
    old = aging[2]['count'] + aging[3]['count']
    if old:
        insights.append(f"{old} unsold voucher{'s' if old != 1 else ''} are older than a month — consider a promotion or disabling them.")
    sold_of_gen = within(V, 'created_at').filter(sold_at__isnull=False).count()
    if gen >= 10 and sold_of_gen / gen < .5:
        insights.append(f"Only {round(sold_of_gen / gen * 100)}% of vouchers generated this period have been sold so far.")

    return {
        'period': period.as_dict(), 'currency': business.currency,
        'kpis': {
            'revenue': {'value': float(rev), 'change': pct_change(rev, rev_p)},
            'sold': {'value': n_sold, 'change': pct_change(n_sold, n_sold_p)},
            'generated': {'value': gen, 'change': pct_change(gen, gen_p)},
            'activated': {'value': act, 'change': pct_change(act, act_p)},
            'avg_ticket': {'value': float(rev / n_sold) if n_sold else 0, 'change': pct_change(rev / n_sold if n_sold else 0, rev_p / n_sold_p if n_sold_p else 0)},
            'sell_through': {'value': round(within(V, 'created_at').filter(sold_at__isnull=False).count() / gen * 100, 1) if gen else 0, 'change': None},
        },
        'trend': trend, 'plans': plan_table,
        'plan_mix': {'labels': [p['plan'] for p in plan_table if p['revenue']], 'values': [p['revenue'] for p in plan_table if p['revenue']]},
        'heatmap': heat, 'routers': {'labels': [r['router__name'] or 'Unassigned' for r in routers], 'values': [float(r['v']) for r in routers], 'counts': [r['n'] for r in routers]},
        'agents': {'labels': [r['agent__name'] for r in agents], 'values': [float(r['v']) for r in agents], 'counts': [r['n'] for r in agents]},
        'aging': aging, 'funnel': funnel, 'insights': insights,
    }


# ───────────────────────────── agent batches ─────────────────────────────
@transaction.atomic
def assign_batch(batch, agent, settlement='credit', method='cash', user=None, reference=''):
    """Hand a batch to an agent (or back to the shop when agent is None).

    credit  — vouchers move to the agent; each is booked as the agent's sale when it is sold or first used.
    prepaid — the agent buys every unsold voucher now: sales are recorded with commission and the agent's
              payment is recorded as a hand-in, so they owe nothing for this batch.
    Returns (moved_count, sales_recorded).
    """
    business = batch.business
    unsold = batch.vouchers.filter(status='active', sold_at__isnull=True, used_at__isnull=True)
    moved = unsold.update(agent=agent)
    batch.agent = agent
    batch.settlement = settlement if agent else 'credit'
    batch.issued_at = timezone.now() if agent else None
    batch.save(update_fields=['agent', 'settlement', 'issued_at'])
    sales = []
    if agent and settlement == 'prepaid':
        for v in batch.vouchers.filter(agent=agent, status='active', sold_at__isnull=True, used_at__isnull=True).order_by('id'):
            s = record_sale(business, v, method=method, agent=agent, user=user, reference=reference, notes=f'Bought upfront in batch {batch.name}')
            if s: sales.append(s)
        net = sum((s.amount - s.commission for s in sales), ZERO)
        if net > 0:
            CashCollection.objects.create(business=business, agent=agent, amount=net, payment_method=method, reference=reference,
                                          note=f'Paid upfront for batch {batch.name} ({len(sales)} vouchers)', recorded_by=user)
    return moved, sales


# ───────────────────────────── agent cash-flow statement ─────────────────────────────
def collection_ref(c):
    """The reference printed for a hand-in: the one typed in, else COL-<date>-<id>."""
    return c.reference or f'COL-{timezone.localtime(c.collected_at):%Y%m%d}-{c.pk:06d}'


def agent_statement(business, agent, period):
    """Cash-flow statement of one agent for a period.

    What the agent owes grows when their vouchers are sold (sale amount minus their commission)
    and shrinks when they hand money in. So:

        opening balance  (owed at the start of the period)
      + sales credited to the agent            (gross)
      − commission they earned on those sales
      − cash handed in                         (any method)
      = closing balance  (still to hand in; negative = paid ahead)

    Sales are grouped per day, batch and plan so a busy agent's statement stays readable;
    every hand-in is its own line."""
    start, end = period.start, period.end
    sales = business.sales.filter(agent=agent)
    cols = business.collections.filter(agent=agent)
    before = sales.filter(sold_at__lt=start).aggregate(g=Sum('amount'), c=Sum('commission'))
    opening = d(before['g']) - d(before['c']) - d(cols.filter(collected_at__lt=start).aggregate(v=Sum('amount'))['v'])

    groups = OrderedDict()
    for s in (sales.filter(sold_at__gte=start, sold_at__lt=end)
              .values('sold_at', 'plan_name', 'amount', 'commission', 'voucher__batch_id', 'voucher__batch__name', 'notes')
              .order_by('sold_at')):
        day = timezone.localtime(s['sold_at']).date()
        label = s['voucher__batch__name'] or ('Member renewal' if 'renewal' in (s['notes'] or '') else 'Single sales')
        key = (day, s['voucher__batch_id'] or 0, label, s['plan_name'])
        g = groups.setdefault(key, {'when': s['sold_at'], 'n': 0, 'gross': ZERO, 'comm': ZERO})
        g['when'] = s['sold_at']; g['n'] += 1; g['gross'] += d(s['amount']); g['comm'] += d(s['commission'])
    lines = []
    for (day, _bid, label, plan), g in groups.items():
        lines.append({'when': g['when'], 'kind': 'sale', 'ref': label,
                      'text': f"{g['n']} × {plan} sold", 'gross': g['gross'], 'comm': g['comm'],
                      'debit': g['gross'] - g['comm'], 'credit': ZERO})
    period_cols = list(cols.filter(collected_at__gte=start, collected_at__lt=end).select_related('recorded_by').order_by('collected_at'))
    for c in period_cols:
        by = c.recorded_by.get_full_name() or c.recorded_by.username if c.recorded_by else ''
        lines.append({'when': c.collected_at, 'kind': 'in', 'ref': collection_ref(c),
                      'text': f'Handed in ({METHOD_LABELS.get(c.payment_method, c.payment_method)})' + (f' — {c.note}' if c.note else ''),
                      'by': by, 'gross': ZERO, 'comm': ZERO, 'debit': ZERO, 'credit': d(c.amount), 'method': c.payment_method})
    lines.sort(key=lambda r: (r['when'], 0 if r['kind'] == 'sale' else 1))
    bal = opening
    for r in lines:
        bal += r['debit'] - r['credit']
        r['balance'] = bal

    gross = sum((r['gross'] for r in lines), ZERO); comm = sum((r['comm'] for r in lines), ZERO)
    net = gross - comm; collected = sum((r['credit'] for r in lines), ZERO)
    closing = opening + net - collected
    due = opening + net
    methods = OrderedDict()
    for c in period_cols:
        m = methods.setdefault(c.payment_method, {'label': METHOD_LABELS.get(c.payment_method, c.payment_method), 'n': 0, 'amount': ZERO})
        m['n'] += 1; m['amount'] += d(c.amount)
    for m in methods.values():
        m['share'] = round(float(m['amount'] / collected * 100), 1) if collected else 0

    batches = []
    for b in (agent.batches.select_related('plan').annotate(
            total=Count('vouchers', filter=Q(vouchers__deleted_at__isnull=True)),
            sold=Count('vouchers', filter=Q(vouchers__sold_at__isnull=False, vouchers__deleted_at__isnull=True)),
            left=Count('vouchers', filter=Q(vouchers__sold_at__isnull=True, vouchers__used_at__isnull=True, vouchers__status='active', vouchers__deleted_at__isnull=True)),
            value=Sum('vouchers__price', filter=Q(vouchers__deleted_at__isnull=True)))
            .order_by('-issued_at', '-created_at')):
        bs = business.sales.filter(agent=agent, voucher__batch=b).aggregate(g=Sum('amount'), c=Sum('commission'))
        batches.append({'b': b, 'total': b.total, 'sold': b.sold, 'left': b.left, 'value': d(b.value),
                        'gross': d(bs['g']), 'net': d(bs['g']) - d(bs['c'])})
    held = business.vouchers.filter(agent=agent, status='active', sold_at__isnull=True, used_at__isnull=True).aggregate(n=Count('id'), v=Sum('price'))
    end_day = timezone.localtime(end - timedelta(seconds=1)).date()
    if closing > 0:
        status = ('due', 'BALANCE DUE')
    elif closing < 0:
        status = ('credit', 'IN CREDIT')
    else:
        status = ('settled', 'SETTLED')
    return {
        'number': f'STM-{agent.pk:04d}-{end_day:%Y%m%d}',
        'from': timezone.localtime(start).date(), 'to': end_day,
        'opening': opening, 'gross': gross, 'commission': comm, 'net': net, 'collected': collected,
        'closing': closing, 'due': due, 'status': status,
        'rate': min(100.0, round(float(collected / due * 100), 1)) if due > 0 else 100.0,
        'lines': lines, 'methods': list(methods.values()), 'batches': batches,
        'held_n': held['n'] or 0, 'held_value': d(held['v']),
        'sold_n': sum(g['n'] for g in groups.values()), 'collections_n': len(period_cols),
    }


# ───────────────────────────── batch receipt ─────────────────────────────
_ONES = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten', 'eleven', 'twelve',
         'thirteen', 'fourteen', 'fifteen', 'sixteen', 'seventeen', 'eighteen', 'nineteen']
_TENS = ['', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety']


def _words(n):
    n = int(n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ('-' + _ONES[n % 10] if n % 10 else '')
    if n < 1000:
        return _ONES[n // 100] + ' hundred' + (' and ' + _words(n % 100) if n % 100 else '')
    for size, word in ((10 ** 9, 'billion'), (10 ** 6, 'million'), (1000, 'thousand')):
        if n >= size:
            rest = n % size
            return _words(n // size) + ' ' + word + ((' and ' if rest < 100 else ' ') + _words(rest) if rest else '')
    return str(n)


def amount_in_words(amount, currency='D'):
    """1136.36 → 'One thousand one hundred and thirty-six dalasi and thirty-six butut only'."""
    amount = d(amount).quantize(Decimal('0.01'))
    whole, cents = int(amount), int((amount - int(amount)) * 100)
    major, minor = ('dalasi', 'butut') if currency in ('D', 'GMD') else ('', 'cents')
    text = _words(whole) + (f' {major}' if major else '') + (f' and {_words(cents)} {minor}' if cents else '') + ' only'
    return text[0].upper() + text[1:]


def batch_receipt(batch):
    """Everything printed on the receipt handed over with a batch."""
    vouchers = batch.vouchers.all()
    agg = vouchers.aggregate(n=Count('id'), v=Sum('price'))
    n, value = agg['n'] or 0, d(agg['v'])
    prices = sorted({d(p) for p in vouchers.values_list('price', flat=True)})
    serials = [s for s in vouchers.exclude(serial='').order_by('id').values_list('serial', flat=True)]
    agent = batch.agent
    pct = d(agent.commission_percent) if agent else ZERO
    # Commission is booked per voucher when each one sells (see record_sale), so it is added up the
    # same way here: the receipt then matches exactly what the agent's statement will show.
    commission = sum((commission_for(agent, p) for p in vouchers.values_list('price', flat=True)), ZERO) if agent else ZERO
    paid = ZERO
    if agent and batch.settlement == 'prepaid':
        s = batch.business.sales.filter(voucher__batch=batch, agent=agent).aggregate(g=Sum('amount'), c=Sum('commission'))
        paid = d(s['g']) - d(s['c'])
        commission = d(s['c'])
    expected = value - commission
    created = timezone.localtime(batch.created_at)
    return {
        'number': f'BRC-{created:%Y%m%d}-{batch.pk:06d}', 'count': n, 'value': value,
        'unit': prices[0] if len(prices) == 1 else None, 'prices': prices,
        'serial_first': serials[0] if serials else '', 'serial_last': serials[-1] if serials else '',
        'agent': agent, 'pct': pct, 'commission': commission, 'expected': expected,
        'prepaid': bool(agent and batch.settlement == 'prepaid'), 'paid': paid,
        'balance': max(ZERO, expected - paid) if agent else ZERO,
        'words': amount_in_words(expected if agent else value, batch.business.currency),
        'issued': timezone.localtime(batch.issued_at) if batch.issued_at else created,
    }


def stock_movement(business, period):
    """Voucher stock roll-forward for a period (members not included):

        carried forward (unsold at the start) + generated − sold or used − deleted = in stock at the end

    A voucher leaves stock when it is sold or first used, whichever comes first. Vouchers held by
    agents are counted separately from shop stock. Value = the vouchers' prices.
    """
    from .models import Voucher
    start, end = period.start, period.end
    base = Voucher.all_objects.filter(business=business).exclude(login_type='member')

    def unsold_at(t):
        return (Q(created_at__lt=t) & (Q(sold_at__isnull=True) | Q(sold_at__gte=t))
                & (Q(used_at__isnull=True) | Q(used_at__gte=t)) & (Q(deleted_at__isnull=True) | Q(deleted_at__gte=t)))

    def tally(qs):
        r = qs.aggregate(n=Count('id'), v=Sum('price'))
        return {'n': r['n'] or 0, 'value': r['v'] or Decimal('0')}

    opening_q = base.filter(unsold_at(start))
    added_q = base.filter(created_at__gte=start, created_at__lt=end)
    pool = base.filter(Q(pk__in=opening_q.values('pk')) | Q(pk__in=added_q.values('pk')))
    left_q = pool.filter((Q(sold_at__gte=start) & Q(sold_at__lt=end)) | (Q(sold_at__isnull=True) & Q(used_at__gte=start) & Q(used_at__lt=end))
                         | (Q(used_at__gte=start) & Q(used_at__lt=end) & Q(sold_at__gte=end)))
    removed_q = pool.exclude(pk__in=left_q.values('pk')).filter(deleted_at__gte=start, deleted_at__lt=end)
    closing_q = base.filter(unsold_at(end))
    out = {
        'opening': tally(opening_q), 'opening_shop': tally(opening_q.filter(agent__isnull=True)),
        'opening_agents': tally(opening_q.filter(agent__isnull=False)),
        'added': tally(added_q), 'left': tally(left_q), 'removed': tally(removed_q), 'closing': tally(closing_q),
        'since': timezone.localtime(start),
    }
    # per plan
    rows = {}
    for key, qs in (('opening', opening_q), ('added', added_q), ('left', left_q), ('removed', removed_q), ('closing', closing_q)):
        for r in qs.values('plan_name').annotate(n=Count('id'), v=Sum('price')):
            row = rows.setdefault(r['plan_name'] or '—', {'plan': r['plan_name'] or '—', 'opening': 0, 'added': 0, 'left': 0, 'removed': 0, 'closing': 0, 'closing_value': Decimal('0')})
            row[key] = r['n']
            if key == 'closing':
                row['closing_value'] = r['v'] or Decimal('0')
    out['plans'] = sorted(rows.values(), key=lambda r: (-r['opening'] - r['added'], r['plan']))
    return out
