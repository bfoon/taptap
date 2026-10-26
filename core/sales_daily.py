"""Daily sales — one day of sales, seen three ways.

    Overview   totals, last 7 days, plans / methods / sellers at a glance
    Plans      every plan sold that day; pick one for its detail
    Sellers    every agent (and the Shop) that sold that day; pick one for its detail

A plan detail shows what sold, when (hour by hour), who sold it, where and how it was paid,
compared with the plan's own 7-day average, plus the stock left. A seller detail shows what
the agent sold, the commission earned, what they owe the business for the day, cash handed in
that day, the running balance and the vouchers they still hold.

Money that belongs to agents (balances, collections) is only shown to people who may see
agent money (finance.view, finance.agents or agents.view); everyone with "Daily sales" sees the
sales themselves.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

SHOP = 'shop'
MONEY_PERMS = {'finance.view', 'finance.agents', 'agents.view'}


def d(v):
    return Decimal(str(v or 0))


def pct(part, whole):
    return round(float(part) / float(whole) * 100, 1) if whole else 0.0


def change(cur, prev):
    if not prev:
        return None
    return round((float(cur or 0) - float(prev)) / float(prev) * 100, 1)


def parse_day(value):
    try:
        day = datetime.strptime(value or '', '%Y-%m-%d').date()
    except ValueError:
        day = timezone.localdate()
    return min(day, timezone.localdate())


def day_bounds(day):
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(day, datetime.min.time()), tz)
    return start, start + timedelta(days=1)


def seller_filter(seller):
    """Q for one seller: an agent id, or SHOP for sales with no agent."""
    if seller == SHOP:
        return Q(agent__isnull=True)
    return Q(agent_id=int(seller))


def _hours(sales):
    """24 hour buckets {amount, count, pct} from a list of sales, plus the busiest hour."""
    buckets = [{'hour': h, 'amount': Decimal(0), 'count': 0} for h in range(24)]
    for s in sales:
        h = timezone.localtime(s.sold_at).hour
        buckets[h]['amount'] += d(s.amount)
        buckets[h]['count'] += 1
    peak = max((b['amount'] for b in buckets), default=0) or 1
    for b in buckets:
        b['pct'] = round(float(b['amount']) / float(peak) * 100) if b['amount'] else 0
        b['label'] = f'{b["hour"]:02d}'
    busiest = max(buckets, key=lambda b: (b['amount'], b['count'])) if any(b['count'] for b in buckets) else None
    return buckets, busiest


def _week(business, start, extra=Q()):
    rows = []
    for i in range(6, -1, -1):
        s = start - timedelta(days=i)
        agg = business.sales.filter(extra, sold_at__gte=s, sold_at__lt=s + timedelta(days=1)).aggregate(v=Sum('amount'), n=Count('id'))
        rows.append({'day': s.date(), 'amount': agg['v'] or 0, 'count': agg['n']})
    peak = max((r['amount'] for r in rows), default=0) or 1
    for r in rows:
        r['pct'] = round(float(r['amount']) / float(peak) * 100)
    return rows


def _avg_before(business, start, extra=Q(), days=7):
    """Average sales per day over the `days` days before this one (days with no sales count as 0)."""
    agg = business.sales.filter(extra, sold_at__gte=start - timedelta(days=days), sold_at__lt=start).aggregate(v=Sum('amount'), n=Count('id'))
    return {'amount': d(agg['v']) / days, 'count': round((agg['n'] or 0) / days, 1)}


def _group(qs, field):
    return list(qs.values(field).annotate(n=Count('id'), v=Sum('amount')).order_by('-v'))


def _plan_rows(business, sales, start, total):
    """One row per plan sold today, with share and change vs the plan's 7-day average."""
    rows = list(sales.values('plan_name').annotate(n=Count('id'), v=Sum('amount'), disc=Sum('discount'), comm=Sum('commission')).order_by('-v'))
    if not rows:
        return rows
    names = [r['plan_name'] for r in rows]
    week = {r['plan_name']: r for r in business.sales.filter(plan_name__in=names, sold_at__gte=start - timedelta(days=7), sold_at__lt=start)
            .values('plan_name').annotate(v=Sum('amount'), n=Count('id'))}
    sellers = defaultdict(set)
    for p, a in sales.values_list('plan_name', 'agent_id'):
        sellers[p].add(a or SHOP)
    plans = {p.name: p for p in business.plans.filter(name__in=names)}
    for r in rows:
        avg = d((week.get(r['plan_name']) or {}).get('v')) / 7
        r['net'] = d(r['v']) - d(r['comm'])
        r['share'] = pct(r['v'] or 0, total)
        r['avg7'] = avg
        r['vs_avg'] = change(r['v'], avg) if avg else None
        r['sellers'] = len(sellers[r['plan_name']])
        r['plan'] = plans.get(r['plan_name'])
    return rows


def _seller_rows(business, sales, start, end, total, can_money):
    """One row per seller that sold today (agents and the Shop) plus agents who only handed in cash."""
    rows = {}
    for r in sales.values('agent_id', 'agent__name').annotate(n=Count('id'), v=Sum('amount'), comm=Sum('commission')).order_by('-v'):
        key = r['agent_id'] or SHOP
        rows[key] = {'key': str(key), 'name': r['agent__name'] or 'Shop', 'is_shop': key == SHOP, 'n': r['n'], 'v': d(r['v']),
                     'comm': d(r['comm']), 'collected': Decimal(0)}
    top = {}
    for r in sales.values('agent_id', 'plan_name').annotate(n=Count('id')).order_by('-n'):
        top.setdefault(r['agent_id'] or SHOP, r['plan_name'])
    if can_money:
        for r in business.collections.filter(collected_at__gte=start, collected_at__lt=end).values('agent_id', 'agent__name').annotate(v=Sum('amount')):
            row = rows.setdefault(r['agent_id'], {'key': str(r['agent_id']), 'name': r['agent__name'], 'is_shop': False, 'n': 0,
                                                  'v': Decimal(0), 'comm': Decimal(0), 'collected': Decimal(0)})
            row['collected'] = d(r['v'])
        from .finance import agent_balances
        bal = {b['agent'].pk: b for b in agent_balances(business)}
    else:
        bal = {}
    out = []
    for key, r in rows.items():
        r['net'] = r['v'] - r['comm']           # what the business keeps / what the agent owes for today
        r['share'] = pct(r['v'], total)
        r['top_plan'] = top.get(key, '')
        b = bal.get(key)
        r['outstanding'] = b['outstanding'] if b else None
        r['holding'] = b['holding'] if b else None
        out.append(r)
    return sorted(out, key=lambda r: (r['v'], r['collected']), reverse=True)


def _stock(business, plan_name):
    qs = business.vouchers.filter(plan_name=plan_name, status='active', sold_at__isnull=True, used_at__isnull=True)
    agg = qs.aggregate(shop=Count('id', filter=Q(agent__isnull=True)), agents=Count('id', filter=Q(agent__isnull=False)))
    by_agent = list(qs.filter(agent__isnull=False).values('agent__name').annotate(n=Count('id')).order_by('-n')[:6])
    return {'shop': agg['shop'], 'agents': agg['agents'], 'total': (agg['shop'] or 0) + (agg['agents'] or 0), 'by_agent': by_agent}


def plan_detail(business, sales, start, plan_name, day_total):
    qs = sales.filter(plan_name=plan_name)
    agg = qs.aggregate(v=Sum('amount'), n=Count('id'), disc=Sum('discount'), comm=Sum('commission'))
    if not agg['n'] and not business.sales.filter(plan_name=plan_name).exists() and not business.plans.filter(name=plan_name).exists():
        return None
    rows = list(qs.select_related('agent', 'router', 'recorded_by')[:300])
    hours, busiest = _hours(rows)
    avg = _avg_before(business, start, Q(plan_name=plan_name))
    prev = business.sales.filter(plan_name=plan_name, sold_at__gte=start - timedelta(days=1), sold_at__lt=start).aggregate(v=Sum('amount'))['v']
    v = d(agg['v'])
    sellers = [{'key': str(r['agent_id'] or SHOP), 'name': r['agent__name'] or 'Shop', 'n': r['n'], 'v': d(r['v']), 'share': pct(r['v'] or 0, v)}
               for r in qs.values('agent_id', 'agent__name').annotate(n=Count('id'), v=Sum('amount')).order_by('-v')]
    return {
        'name': plan_name, 'plan': business.plans.filter(name=plan_name).first(),
        'amount': v, 'count': agg['n'], 'discount': d(agg['disc']), 'commission': d(agg['comm']), 'net': v - d(agg['comm']),
        'avg_price': (v / agg['n']) if agg['n'] else 0, 'share': pct(v, day_total),
        'avg7': avg, 'vs_avg': change(v, avg['amount']) if avg['amount'] else None, 'vs_prev': change(v, prev),
        'hours': hours, 'busiest': busiest, 'sellers': sellers,
        'sites': _group(qs, 'router__name'), 'methods': _group(qs, 'payment_method'),
        'stock': _stock(business, plan_name), 'sales': rows,
        'first': rows[-1].sold_at if rows else None, 'last': rows[0].sold_at if rows else None,
    }


def seller_detail(business, sales, start, end, seller, day_total, can_money):
    if seller == SHOP:
        agent = None
    else:
        agent = business.agents.filter(pk=int(seller)).first()
        if agent is None:
            return None
    flt = seller_filter(seller)
    qs = sales.filter(flt)
    agg = qs.aggregate(v=Sum('amount'), n=Count('id'), disc=Sum('discount'), comm=Sum('commission'))
    rows = list(qs.select_related('router', 'recorded_by')[:300])
    hours, busiest = _hours(rows)
    v, comm = d(agg['v']), d(agg['comm'])
    plans = [{'name': r['plan_name'], 'n': r['n'], 'v': d(r['v']), 'share': pct(r['v'] or 0, v)}
             for r in qs.values('plan_name').annotate(n=Count('id'), v=Sum('amount')).order_by('-v')]
    avg = _avg_before(business, start, flt)
    prev = business.sales.filter(flt, sold_at__gte=start - timedelta(days=1), sold_at__lt=start).aggregate(v=Sum('amount'))['v']
    held = business.vouchers.filter(status='active', sold_at__isnull=True, used_at__isnull=True)
    held = held.filter(agent__isnull=True) if agent is None else held.filter(agent=agent)
    holding = list(held.values('plan_name').annotate(n=Count('id'), v=Sum('price')).order_by('-n'))
    out = {
        'key': seller, 'agent': agent, 'name': agent.name if agent else 'Shop', 'is_shop': agent is None,
        'amount': v, 'count': agg['n'], 'discount': d(agg['disc']), 'commission': comm, 'net': v - comm,
        'share': pct(v, day_total), 'avg7': avg, 'vs_avg': change(v, avg['amount']) if avg['amount'] else None, 'vs_prev': change(v, prev),
        'hours': hours, 'busiest': busiest, 'plans': plans, 'sites': _group(qs, 'router__name'), 'methods': _group(qs, 'payment_method'),
        'holding': holding, 'holding_n': sum(h['n'] for h in holding), 'holding_v': sum(d(h['v']) for h in holding),
        'sales': rows, 'first': rows[-1].sold_at if rows else None, 'last': rows[0].sold_at if rows else None,
        'collections': [], 'collected': Decimal(0), 'balance': None,
    }
    if agent is not None and can_money:
        cols = list(business.collections.filter(agent=agent, collected_at__gte=start, collected_at__lt=end).select_related('recorded_by'))
        out['collections'] = cols
        out['collected'] = sum(d(c.amount) for c in cols)
        from .finance import agent_balances
        out['balance'] = next((b for b in agent_balances(business) if b['agent'].pk == agent.pk), None)
        out['day_gap'] = out['net'] - out['collected']   # owed for today minus what was handed in today
    return out


def fingerprint(business, params):
    """A short hash that changes whenever the day's figures change: a sale recorded, edited or deleted,
    or cash handed in. The page asks for it every few seconds and only reloads figures when it moved."""
    import hashlib
    from django.db.models import Max
    day = parse_day(params.get('date'))
    start, end = day_bounds(day)
    s = business.sales.filter(sold_at__gte=start, sold_at__lt=end).aggregate(
        n=Count('id'), m=Max('id'), v=Sum('amount'), c=Sum('commission'), d=Sum('discount'))
    k = business.collections.filter(collected_at__gte=start, collected_at__lt=end).aggregate(n=Count('id'), m=Max('id'), v=Sum('amount'))
    raw = f'{day}|{s["n"]}|{s["m"]}|{s["v"]}|{s["c"]}|{s["d"]}|{k["n"]}|{k["m"]}|{k["v"]}'
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def build(business, params, perms):
    """Everything templates/core/sales_daily.html needs."""
    day = parse_day(params.get('date'))
    start, end = day_bounds(day)
    view = params.get('view') if params.get('view') in ('overview', 'plans', 'sellers') else 'overview'
    plan = (params.get('plan') or '').strip()[:120]
    seller = (params.get('seller') or '').strip()
    if seller and seller != SHOP and not seller.isdigit():
        seller = ''
    if plan:
        view = 'plans'
    if seller:
        view = 'sellers'
    can_money = bool(MONEY_PERMS & set(perms or ()))

    sales = business.sales.filter(sold_at__gte=start, sold_at__lt=end)
    total = sales.aggregate(v=Sum('amount'), n=Count('id'), disc=Sum('discount'), comm=Sum('commission'))
    day_total = d(total['v'])
    prev = business.sales.filter(sold_at__gte=start - timedelta(days=1), sold_at__lt=start).aggregate(v=Sum('amount'))

    ctx = {
        'day': day, 'is_today': day == timezone.localdate(), 'prev_day': day - timedelta(days=1),
        'next_day': day + timedelta(days=1) if day < timezone.localdate() else None,
        'view': view, 'sel_plan': plan, 'sel_seller': seller, 'can_money': can_money,
        'total': day_total, 'count': total['n'], 'discounts': d(total['disc']), 'commission': d(total['comm']),
        'net': day_total - d(total['comm']), 'change': change(day_total, prev['v']),
        'avg': (day_total / total['n']) if total['n'] else 0,
        'live_fp': fingerprint(business, params),
    }
    week_filter = Q()
    if plan:
        week_filter = Q(plan_name=plan)
    elif seller:
        week_filter = seller_filter(seller)
    ctx['week'] = _week(business, start, week_filter)

    if view == 'overview':
        ctx['by_plan'] = _plan_rows(business, sales, start, day_total)[:8]
        ctx['by_method'] = _group(sales, 'payment_method')
        ctx['by_seller'] = _seller_rows(business, sales, start, end, day_total, can_money)[:8]
        ctx['hours'], ctx['busiest'] = _hours(list(sales.only('sold_at', 'amount')))
        ctx['sales'] = sales.select_related('agent', 'router')[:200]
    elif view == 'plans':
        ctx['plan_rows'] = _plan_rows(business, sales, start, day_total)
        if plan:
            ctx['plan_detail'] = plan_detail(business, sales, start, plan, day_total)
    else:
        ctx['seller_rows'] = _seller_rows(business, sales, start, end, day_total, can_money)
        if seller:
            ctx['seller_detail'] = seller_detail(business, sales, start, end, seller, day_total, can_money)
    return ctx


def export_rows(business, params):
    """CSV rows for the day — filtered to the chosen plan or seller when one is open."""
    day = parse_day(params.get('date'))
    start, end = day_bounds(day)
    qs = business.sales.filter(sold_at__gte=start, sold_at__lt=end).select_related('agent', 'router').order_by('sold_at')
    plan = (params.get('plan') or '').strip()
    seller = (params.get('seller') or '').strip()
    if plan:
        qs = qs.filter(plan_name=plan)
    if seller == SHOP or seller.isdigit():
        qs = qs.filter(seller_filter(seller))
    yield ['Time', 'Plan', 'Voucher', 'Seller', 'Site', 'Method', 'Amount', 'Discount', 'Commission', 'Net', 'Customer', 'Reference']
    for s in qs:
        yield [timezone.localtime(s.sold_at).strftime('%Y-%m-%d %H:%M'), s.plan_name, s.voucher_code, s.agent.name if s.agent else 'Shop',
               s.router.name if s.router else '', s.get_payment_method_display(), s.amount, s.discount, s.commission, s.amount - s.commission,
               s.customer_name or s.customer_phone, s.reference]
