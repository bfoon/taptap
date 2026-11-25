"""Team performance — what each person (the owner too) made and sold, by day or by month.

    Team › Performance            /team/performance/            everybody side by side, a leaderboard and a timeline
    one person                    /team/performance/<user id>/  their days or months, how they were paid, recent sales
    Fit                           tool "team_performance"       "how much cash did Awa bring in last month, per week?"
    Fit charts                    source "sales_by_teammate"    revenue per person for a period

Who gets credit
---------------
* Vouchers and batches: the person who created them (``created_by``, stamped on every new row).
* A sale: the person who recorded it; a sale recorded by itself (the voucher's first use on the router, a payment
  that confirmed itself) goes to whoever created that voucher. Sales through agents are counted apart ("Agents"),
  and sales nobody can be credited with (vouchers imported from the router, automatic ones) as "Unassigned".
* Cash drawer = cash sales + cash collected from agents − cash expenses, each recorded by that person: the money they
  should be holding (or have handed in) for the period. Mobile money and bank never pass through a drawer.

Who may see it: people with Finance or Team rights see everybody; anyone else sees only themselves.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce, TruncDate, TruncMonth
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from .views_team import _b

ZERO = Decimal('0')
MOBILE = ('wave', 'qmoney', 'afrimoney')
SEE_ALL = ('finance.view', 'team.manage')
UNASSIGNED, AGENTS = 'unassigned', 'agents'
METHOD_GROUPS = (('cash', 'Cash'), ('mobile', 'Mobile money'), ('auto', 'On first use'), ('other', 'Bank, card, other'))


def _d(v):
    return Decimal(str(v or 0))


def _group(method):
    if method == 'cash':
        return 'cash'
    if method in MOBILE:
        return 'mobile'
    if method == 'auto':
        return 'auto'
    return 'other'


def period_for(params):
    """A finance Period from ?range=today|7d|30d|90d|month|last_month|year|custom&start=&end= (default: this month)."""
    from .finance import resolve_period
    return resolve_period({'range': params.get('range') or 'month', 'start': params.get('start', ''), 'end': params.get('end', '')},
                          default='month')


def bucket_of(period, group=''):
    """'day' or 'month' — asked for, or what fits the period."""
    if group in ('day', 'month'):
        return group
    return 'month' if period.days > 62 else 'day'


def _keys(period, bucket):
    """Every bucket of the period in order (so a quiet day shows as zero): [(key, label)]."""
    out = []
    day = timezone.localtime(period.start).date()
    last = (timezone.localtime(period.end) - timedelta(seconds=1)).date()
    if bucket == 'month':
        cur = day.replace(day=1)
        while cur <= last:
            out.append((cur.strftime('%Y-%m'), cur.strftime('%b %Y')))
            cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        while day <= last:
            out.append((day.isoformat(), day.strftime('%a %d %b')))
            day += timedelta(days=1)
    return out


def _key(value, bucket):
    if value is None:
        return ''
    if hasattr(value, 'tzinfo') and getattr(value, 'tzinfo', None) is not None:
        value = timezone.localtime(value)
    return value.strftime('%Y-%m') if bucket == 'month' else value.strftime('%Y-%m-%d')


def people(business):
    """The owner first, then every team member (switched-off ones too: their history still counts)."""
    out = OrderedDict()
    owner = business.user
    out[owner.pk] = {'user_id': owner.pk, 'name': owner.get_full_name() or business.owner_name or owner.email, 'email': owner.email,
                     'role': 'Owner', 'active': True}
    for m in business.team.select_related('user').all():
        if m.user_id not in out:
            out[m.user_id] = {'user_id': m.user_id, 'name': m.name, 'email': m.user.email, 'role': m.role_label, 'active': m.is_active}
    return out


def _blank(person):
    return {**person, 'vouchers': 0, 'members': 0, 'stock_value': ZERO, 'batches': 0, 'batch_vouchers': 0,
            'sales': 0, 'amount': ZERO, 'commission': ZERO, 'discount': ZERO,
            'methods': {k: ZERO for k, _ in METHOD_GROUPS}, 'collected_cash': ZERO, 'collected': ZERO,
            'expenses_cash': ZERO, 'expenses': ZERO, 'prev_amount': ZERO, 'timeline': {}}


def report(business, period, group='', only=None):
    """Everybody's figures for the period. ``only``: a user id — that person alone."""
    from .models import Voucher, VoucherBatch
    bucket = bucket_of(period, group)
    trunc = TruncMonth if bucket == 'month' else TruncDate
    tz = timezone.get_current_timezone()
    team = people(business)
    if only is not None:
        team = OrderedDict((k, v) for k, v in team.items() if k == only)
    rows = OrderedDict((uid, _blank(p)) for uid, p in team.items())
    extra = {UNASSIGNED: _blank({'user_id': None, 'name': 'Unassigned', 'email': '', 'role': 'Router / automatic', 'active': True}),
             AGENTS: _blank({'user_id': None, 'name': 'Agents', 'email': '', 'role': 'Sold by agents', 'active': True})}
    start, end = period.start, period.end

    def row(uid, allow_unassigned=True):
        if uid in rows:
            return rows[uid]
        if only is None and allow_unassigned:
            return extra[UNASSIGNED]
        return None

    def tl(r, key):
        return r['timeline'].setdefault(key, {'sales': 0, 'amount': ZERO, 'vouchers': 0})

    # vouchers and members created (deleted ones too: they were made)
    vq = Voucher.all_objects.filter(business=business, source='taptap', created_at__gte=start, created_at__lt=end, created_by__isnull=False)
    if only is not None:
        vq = vq.filter(created_by_id=only)
    for x in vq.values('created_by').annotate(n=Count('id'), m=Count('id', filter=Q(login_type='member')), v=Sum('price')):
        r = row(x['created_by'], allow_unassigned=False)
        if r is not None:
            r['vouchers'] += x['n'] - x['m']
            r['members'] += x['m']
            r['stock_value'] += _d(x['v'])
    for x in vq.annotate(b=trunc('created_at', tzinfo=tz)).values('created_by', 'b').annotate(n=Count('id')):
        r = row(x['created_by'], allow_unassigned=False)
        if r is not None:
            tl(r, _key(x['b'], bucket))['vouchers'] += x['n']

    bq = VoucherBatch.all_objects.filter(business=business, created_at__gte=start, created_at__lt=end, created_by__isnull=False)
    if only is not None:
        bq = bq.filter(created_by_id=only)
    for x in bq.values('created_by').annotate(n=Count('id'), q=Sum('quantity')):
        r = row(x['created_by'], allow_unassigned=False)
        if r is not None:
            r['batches'] += x['n']
            r['batch_vouchers'] += x['q'] or 0

    # sales: the person who recorded it, else whoever created the voucher
    sq = business.sales.filter(sold_at__gte=start, sold_at__lt=end).annotate(who=Coalesce('recorded_by', 'voucher__created_by'))
    own = sq.filter(agent__isnull=True)
    if only is not None:
        own = own.filter(who=only)
    for x in own.values('who', 'payment_method').annotate(n=Count('id'), v=Sum('amount'), c=Sum('commission'), dc=Sum('discount')):
        r = row(x['who'])
        if r is None:
            continue
        r['sales'] += x['n']
        r['amount'] += _d(x['v'])
        r['commission'] += _d(x['c'])
        r['discount'] += _d(x['dc'])
        r['methods'][_group(x['payment_method'])] += _d(x['v'])
    for x in own.annotate(b=trunc('sold_at', tzinfo=tz)).values('who', 'b').annotate(n=Count('id'), v=Sum('amount')):
        r = row(x['who'])
        if r is not None:
            t = tl(r, _key(x['b'], bucket))
            t['sales'] += x['n']
            t['amount'] += _d(x['v'])
    if only is None:
        ag = extra[AGENTS]
        for x in sq.filter(agent__isnull=False).values('payment_method').annotate(n=Count('id'), v=Sum('amount'), c=Sum('commission')):
            ag['sales'] += x['n']
            ag['amount'] += _d(x['v'])
            ag['commission'] += _d(x['c'])
            ag['methods'][_group(x['payment_method'])] += _d(x['v'])

    # the previous period of the same length, for the change
    prev = business.sales.filter(sold_at__gte=period.prev_start, sold_at__lt=period.prev_end, agent__isnull=True) \
        .annotate(who=Coalesce('recorded_by', 'voucher__created_by'))
    if only is not None:
        prev = prev.filter(who=only)
    for x in prev.values('who').annotate(v=Sum('amount')):
        r = rows.get(x['who'])
        if r is not None:
            r['prev_amount'] += _d(x['v'])

    # money collected from agents and expenses paid, by whoever recorded them
    for x in business.collections.filter(collected_at__gte=start, collected_at__lt=end, recorded_by__isnull=False) \
            .values('recorded_by', 'payment_method').annotate(v=Sum('amount')):
        r = rows.get(x['recorded_by'])
        if r is not None:
            r['collected'] += _d(x['v'])
            if x['payment_method'] == 'cash':
                r['collected_cash'] += _d(x['v'])
    for x in business.expenses.filter(paid_at__gte=start, paid_at__lt=end, recorded_by__isnull=False) \
            .values('recorded_by', 'payment_method').annotate(v=Sum('amount')):
        r = rows.get(x['recorded_by'])
        if r is not None:
            r['expenses'] += _d(x['v'])
            if x['payment_method'] == 'cash':
                r['expenses_cash'] += _d(x['v'])

    keys = _keys(period, bucket)
    out_rows = [finish(r, keys) for r in rows.values()]
    for k in (UNASSIGNED, AGENTS):
        if only is None and (extra[k]['sales'] or extra[k]['amount']):
            out_rows.append(finish(extra[k], keys, pseudo=k))
    team_rows = [r for r in out_rows if not r.get('pseudo')]
    total = sum((r['amount'] for r in out_rows), ZERO)
    for r in out_rows:
        r['share'] = round(float(r['amount'] / total * 100), 1) if total else 0
    team_rows.sort(key=lambda r: (-r['amount'], -r['vouchers'], r['name'].lower()))
    _badges(team_rows, ranking=only is None)            # after sorting: a tie goes to whoever ranks first
    for i, r in enumerate(team_rows, 1):
        r['rank'] = i
    return {'period': period, 'bucket': bucket, 'keys': keys, 'rows': team_rows + [r for r in out_rows if r.get('pseudo')],
            'team': team_rows, 'total': total, 'total_cash': sum((r['methods']['cash'] for r in out_rows), ZERO),
            'total_vouchers': sum(r['vouchers'] for r in team_rows), 'total_batches': sum(r['batches'] for r in team_rows),
            'max_bucket': max([b['amount'] for r in team_rows for b in r['series']] or [ZERO]) or Decimal('1')}


def finish(r, keys, pseudo=''):
    r = dict(r)
    r['pseudo'] = pseudo
    r['net'] = r['amount'] - r['commission']
    r['mix'] = [{'key': k, 'label': label, 'amount': r['methods'][k],
                 'pct': round(float(r['methods'][k] / r['amount'] * 100), 1) if r['amount'] else 0} for k, label in METHOD_GROUPS]
    r['cash_drawer'] = r['methods']['cash'] + r['collected_cash'] - r['expenses_cash']
    r['change'] = (round(float((r['amount'] - r['prev_amount']) / r['prev_amount'] * 100), 1)
                   if r['prev_amount'] else (100.0 if r['amount'] else None))
    series = []
    for k, label in keys:
        t = r['timeline'].get(k) or {}
        series.append({'key': k, 'label': label, 'sales': t.get('sales', 0), 'amount': t.get('amount', ZERO), 'vouchers': t.get('vouchers', 0)})
    r['series'] = series
    r['peak'] = max([b['amount'] for b in series] or [ZERO]) or Decimal('1')
    r['peak_vouchers'] = max([b['vouchers'] for b in series] or [0]) or 1
    best = max(series, key=lambda b: b['amount']) if series else None
    r['best'] = best if best and best['amount'] > 0 else None
    r['active_buckets'] = sum(1 for b in series if b['amount'] or b['vouchers'])
    streak = run = 0
    for b in series:
        run = run + 1 if (b['sales'] or b['vouchers']) else 0
        streak = max(streak, run)
    r['streak'] = streak
    r['avg_sale'] = (r['amount'] / r['sales']).quantize(Decimal('0.01')) if r['sales'] else ZERO
    r.pop('timeline', None)
    return r


def _badges(rows, ranking=True):
    """A little recognition: who sold most, made most, is rising, kept a streak, handled most cash.
    ``ranking``: False when ``rows`` is one person alone — "top" means nothing without the others."""
    for r in rows:
        r['badges'] = []
    live = [r for r in rows if r['amount'] or r['vouchers']]
    if not live:
        return
    if ranking:
        _rank_badges(live)
    for r in live:
        if r['change'] is not None and r['prev_amount'] and r['change'] >= 25:
            r['badges'].append(('bi-graph-up-arrow', f'Up {r["change"]:.0f}%'))
        if r['streak'] >= 5:
            r['badges'].append(('bi-fire', f'{r["streak"]}-{"day" if len(r["series"][0]["key"]) == 10 else "month"} streak'))


def _rank_badges(live):
    top = max(live, key=lambda r: r['amount'])
    if top['amount'] > 0:
        top['badges'].append(('bi-trophy-fill', 'Top seller'))
    maker = max(live, key=lambda r: r['vouchers'])
    if maker['vouchers'] > 0:
        maker['badges'].append(('bi-printer-fill', 'Voucher maker'))
    cash = max(live, key=lambda r: r['cash_drawer'])
    if cash['cash_drawer'] > 0 and cash is not top:
        cash['badges'].append(('bi-cash-stack', 'Cash champion'))


# ─────────────────────────────── who may see what ───────────────────────────────

def sees_all(perms):
    return any(p in perms for p in SEE_ALL)


# ─────────────────────────────── pages ───────────────────────────────

RANGES = [('today', 'Today'), ('7d', '7 days'), ('30d', '30 days'), ('month', 'This month'), ('last_month', 'Last month'),
          ('year', 'This year'), ('custom', 'Custom')]


def _ctx(request, period, group):
    return {'ranges': RANGES, 'range': request.GET.get('range') or 'month', 'group': group or '',
            'start': request.GET.get('start', ''), 'end': request.GET.get('end', ''), 'methods': METHOD_GROUPS}


@login_required
def performance(request):
    business = _b(request)
    perms = getattr(request, 'tt_perms', frozenset())
    period = period_for(request.GET)
    group = request.GET.get('group', '')
    only = None if sees_all(perms) else request.user.pk
    data = report(business, period, group, only=only)
    return render(request, 'core/team_performance.html', {**_ctx(request, period, group), 'r': data, 'all': only is None})


@login_required
def person(request, user_id):
    business = _b(request)
    perms = getattr(request, 'tt_perms', frozenset())
    if int(user_id) != request.user.pk and not sees_all(perms):
        raise Http404
    if int(user_id) not in people(business):
        raise Http404
    period = period_for(request.GET)
    group = request.GET.get('group', '')
    data = report(business, period, group, only=int(user_id))
    if not data['team']:
        raise Http404
    me = data['team'][0]
    sales = (business.sales.filter(sold_at__gte=period.start, sold_at__lt=period.end, agent__isnull=True)
             .filter(Q(recorded_by_id=user_id) | Q(recorded_by__isnull=True, voucher__created_by_id=user_id))
             .order_by('-sold_at')[:25])
    from .models import VoucherBatch
    batches = (VoucherBatch.all_objects.filter(business=business, created_by_id=user_id, created_at__gte=period.start,
                                               created_at__lt=period.end).select_related('plan').order_by('-created_at')[:12])
    everyone = report(business, period, group) if sees_all(perms) else None
    if everyone:
        me['badges'] = next((x['badges'] for x in everyone['team'] if x['user_id'] == me['user_id']), me['badges'])
    return render(request, 'core/team_performance_person.html', {
        **_ctx(request, period, group), 'r': data, 'me': me, 'sales': sales, 'batches': batches,
        'team_total': everyone['total'] if everyone else None, 'rank': next((x['rank'] for x in everyone['team'] if x['user_id'] == me['user_id']), None) if everyone else None,
        'team_size': len(everyone['team']) if everyone else None, 'all': sees_all(perms),
    })


# ─────────────────────────────── Fit ───────────────────────────────

def fit_tool(ctx, period='month', person='', group='', start='', end=''):
    from .fit_tools import PERIODS, ToolError, money
    if start or end:
        p = period_for({'range': 'custom', 'start': start, 'end': end or start})
    else:
        p = period_for({'range': period if period in PERIODS else 'month'})
    only = None
    if not sees_all(ctx.perms):
        only = ctx.user.pk
    data = report(ctx.business, p, group if group in ('day', 'month') else '', only=only)
    rows = data['rows']
    if person:
        q = person.strip().lower()
        rows = [r for r in rows if q in r['name'].lower() or q in (r['email'] or '').lower() or q == (r['role'] or '').lower()]
        if not rows:
            raise ToolError(f'Nobody on the team matches "{person}".')
    detail = bool(person or group or only is not None)

    def out(r):
        o = {'name': r['name'], 'role': r['role'], 'rank': r.get('rank'),
             'vouchers_created': r['vouchers'], 'members_created': r['members'], 'batches_created': r['batches'],
             'face_value_of_vouchers_created': money(ctx, r['stock_value']),
             'sales': r['sales'], 'sales_amount': money(ctx, r['amount']), 'share_of_all_sales_pct': r['share'],
             'by_payment': {label: money(ctx, r['methods'][k]) for k, label in METHOD_GROUPS if r['methods'][k]},
             'agent_commission': money(ctx, r['commission']), 'net_after_commission': money(ctx, r['net']),
             'cash_collected_from_agents': money(ctx, r['collected']), 'expenses_recorded': money(ctx, r['expenses']),
             'cash_drawer': money(ctx, r['cash_drawer']),
             'change_vs_previous_period_pct': r['change'], 'average_sale': money(ctx, r['avg_sale']),
             'best_' + data['bucket']: ({'when': r['best']['label'], 'amount': money(ctx, r['best']['amount'])} if r['best'] else None),
             'badges': [b[1] for b in r.get('badges', [])]}
        if r.get('user_id'):
            o['link'] = reverse('team_performance_person', args=[r['user_id']])
        if detail:
            o['per_' + data['bucket']] = [{'when': b['label'], 'sales': b['sales'], 'amount': money(ctx, b['amount']),
                                           'vouchers_created': b['vouchers']} for b in r['series'][-31:] if b['sales'] or b['vouchers']]
        return o
    return {'period': p.label, 'grouped_by': data['bucket'], 'currency_total_sales': money(ctx, data['total']),
            'cash_sales_total': money(ctx, data['total_cash']),
            'how_credit_works': 'Sales go to who recorded them; automatic sales to who created the voucher. Agents and '
                                'router-made vouchers are separate rows. Cash drawer = cash sales + cash collected − cash expenses.',
            'only_yourself': only is not None, 'people': [out(r) for r in rows[:15]], 'link': reverse('team_performance')}


def s_sales_by_teammate(ctx, period='30d', **_):
    from .fit_charts import _f, _period
    p = _period(period)
    only = None if sees_all(ctx.perms) else ctx.user.pk
    data = report(ctx.business, p, only=only)
    rows = [r for r in data['rows'] if r['amount']][:12]
    return {'labels': [r['name'] for r in rows], 'series': [{'name': 'Sales', 'data': [_f(r['amount']) for r in rows], 'unit': 'money'}]}


URLS = [
    ('team/performance/', performance, 'team_performance', SEE_ALL + ('sales.daily', 'vouchers.create')),
    ('team/performance/<int:user_id>/', person, 'team_performance_person', None),
]


def install():
    """URLs, permissions, the Fit tool and chart source."""
    from django.urls import path
    from . import permissions, urls
    names = {getattr(x, 'name', None) for x in urls.urlpatterns}
    for route, view, name, perm in URLS:
        if name not in names:
            urls.urlpatterns.append(path(route, view, name=name))
        if perm:
            permissions.URL_PERMS[name] = perm
        else:
            permissions.MEMBER_ALLOWED.add(name)      # the view itself allows yourself, or everybody with Finance / Team rights
    from . import fit_charts, fit_tools
    S = {'type': 'string'}
    fit_tools.TOOLS['team_performance'] = (
        None, fit_tool,
        'What each team member (the owner too) created and sold in a period: vouchers, members and batches made, sales '
        'and their cash / mobile money / other split, agent commission, cash collected from agents, expenses, cash drawer '
        '(cash they should hold), change vs the previous period, best day or month and badges. Use group=day or '
        'group=month for the figures per day / per month; person to look at one person (name, email or "owner"). '
        'People without Finance or Team rights only ever get their own figures.',
        fit_tools._obj({'period': {'type': 'string', 'enum': list(fit_tools.PERIODS), 'description': 'Default: this month'},
                        'person': {**S, 'description': 'Optional: name, email or role'},
                        'group': {'type': 'string', 'enum': ['day', 'month']},
                        'start': {**S, 'description': 'Custom range start YYYY-MM-DD (instead of period)'},
                        'end': {**S, 'description': 'Custom range end YYYY-MM-DD'}}))
    fit_charts.SOURCES['sales_by_teammate'] = ('Sales by team member', fit_charts.FIN, 'bar', False, s_sales_by_teammate,
                                               'Revenue per person (owner and staff) for a period')
