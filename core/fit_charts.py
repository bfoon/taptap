"""Charts and live dashboards Fit can make.

Rule 1 of Fit — facts only from tools — holds for charts too: the model never writes numbers into a chart.
It picks a *data source* (and a period / router), TapTap computes the figures from the database or the live
routers, and the browser draws them (Chart.js). A dashboard is a saved list of such charts that refreshes
itself; "live" sources (users online, router load, KPIs) also keep a short history in the cache, so a
dashboard opened now already shows the last minutes as a moving line.

Every source names the permissions it needs; a person who lacks them sees "not allowed" in that tile.
"""
from __future__ import annotations

import time
from datetime import datetime, time as dtime, timedelta, timezone as dt_timezone

from django.core.cache import cache
from django.db.models import Count, Sum
from django.db.models.functions import TruncDate, TruncHour
from django.utils import timezone

FIN = ('finance.view', 'sales.daily', 'dashboard.view')
VOU = ('vouchers.view', 'vouchers.support', 'vouchers.create')
NET = ('network.manage',)
TYPES = ('line', 'bar', 'area', 'doughnut', 'pie', 'kpi')
HISTORY_POINTS = 120          # live history kept per source (with a 10 s floor: about 20 minutes)
HISTORY_MIN_GAP = 10


class ChartError(ValueError):
    pass


def _f(v):
    return float(v or 0)


def _period(period):
    from .finance import resolve_period
    from .fit_tools import PERIODS
    return resolve_period({'range': period if period in PERIODS else '30d'})


def _routers(ctx, router=''):
    qs = ctx.business.routers.select_related('agent').all()
    if router and router != 'all':
        qs = qs.filter(name__iexact=router) if not str(router).isdigit() else qs.filter(pk=int(router))
    return list(qs[:30])


def _online(r):
    try:
        a = r.agent
        if a.online:
            return int(a.active_sessions or 0)
    except Exception:
        pass
    snap = cache.get(f'tt:tr:users:{r.pk}') or {}
    users = snap.get('users') or {}
    return sum(len(v) for v in users.values()) if users else 0


def _cpu(r):
    try:
        return int(r.agent.cpu_load or 0)
    except Exception:
        return int((cache.get(f'tt:rt:cpu:{r.pk}') or 0))


# ─────────────────────────────── sources ───────────────────────────────

def s_sales_by_day(ctx, days=14, **_):
    days = max(2, min(120, int(days or 14)))
    start = timezone.make_aware(datetime.combine(timezone.localdate() - timedelta(days=days - 1), dtime.min))
    rows = {r['d']: r for r in ctx.business.sales.filter(sold_at__gte=start).annotate(d=TruncDate('sold_at'))
            .values('d').annotate(v=Sum('amount'), n=Count('id'))}
    labels, money, count = [], [], []
    for i in range(days):
        d = (start + timedelta(days=i)).date()
        labels.append(d.strftime('%d %b'))
        money.append(_f(rows.get(d, {}).get('v')))
        count.append(int(rows.get(d, {}).get('n') or 0))
    return {'labels': labels, 'series': [{'name': 'Revenue', 'data': money, 'unit': 'money'},
                                         {'name': 'Vouchers sold', 'data': count, 'unit': 'count', 'axis': 'right'}]}


def s_sales_by_hour(ctx, **_):
    today = timezone.make_aware(datetime.combine(timezone.localdate(), dtime.min))
    def per_hour(a, b):
        out = [0.0] * 24
        for r in ctx.business.sales.filter(sold_at__gte=a, sold_at__lt=b).annotate(h=TruncHour('sold_at')).values('h').annotate(v=Sum('amount')):
            out[timezone.localtime(r['h']).hour] += _f(r['v'])
        return out
    return {'labels': [f'{h:02d}:00' for h in range(24)],
            'series': [{'name': 'Today', 'data': per_hour(today, today + timedelta(days=1)), 'unit': 'money'},
                       {'name': 'Yesterday', 'data': per_hour(today - timedelta(days=1), today), 'unit': 'money', 'muted': True}]}


def s_sales_by_plan(ctx, period='30d', **_):
    p = _period(period)
    rows = list(ctx.business.sales.filter(sold_at__gte=p.start, sold_at__lt=p.end).values('plan_name')
                .annotate(v=Sum('amount')).order_by('-v')[:10])
    return {'labels': [r['plan_name'] or '—' for r in rows], 'series': [{'name': 'Revenue', 'data': [_f(r['v']) for r in rows], 'unit': 'money'}]}


def s_sales_by_agent(ctx, period='30d', **_):
    p = _period(period)
    rows = list(ctx.business.sales.filter(agent__isnull=False, sold_at__gte=p.start, sold_at__lt=p.end).values('agent__name')
                .annotate(v=Sum('amount')).order_by('-v')[:12])
    return {'labels': [r['agent__name'] for r in rows], 'series': [{'name': 'Revenue', 'data': [_f(r['v']) for r in rows], 'unit': 'money'}]}


def s_vouchers_by_state(ctx, **_):
    v, now = ctx.business.vouchers, timezone.now()
    data = {
        'In stock': v.filter(status='active', used_at__isnull=True, sold_at__isnull=True).count(),
        'Sold, not used': v.filter(status='active', used_at__isnull=True, sold_at__isnull=False).count(),
        'In use': v.filter(status='active', used_at__isnull=False, expires_at__gt=now).count(),
        'Frozen': v.filter(frozen_at__isnull=False).count(),
        'Expired': v.filter(status='expired').count(),
    }
    return {'labels': list(data), 'series': [{'name': 'Vouchers', 'data': list(data.values()), 'unit': 'count'}]}


def s_online_users(ctx, router='', **_):
    rs = _routers(ctx, router)
    return {'labels': [r.name for r in rs], 'series': [{'name': 'Users online', 'data': [_online(r) for r in rs], 'unit': 'count'}],
            'total': sum(_online(r) for r in rs)}


def s_router_load(ctx, router='', **_):
    rs = _routers(ctx, router)
    return {'labels': [r.name for r in rs], 'series': [{'name': 'CPU %', 'data': [_cpu(r) for r in rs], 'unit': 'pct'}]}


def s_data_by_hour(ctx, hours=24, **_):
    from .models import UsageRecord
    hours = max(2, min(168, int(hours or 24)))
    since = timezone.now() - timedelta(hours=hours)
    rows = {timezone.localtime(r['hour']).strftime('%d %H:00'): r for r in UsageRecord.objects.filter(business=ctx.business, hour__gte=since)
            .values('hour').annotate(d=Sum('download'), u=Sum('upload')).order_by('hour')}
    labels = list(rows)
    mb = lambda x: round(_f(x) / 1048576, 1)
    return {'labels': labels, 'series': [{'name': 'Download MB', 'data': [mb(rows[k]['d']) for k in labels], 'unit': 'mb'},
                                         {'name': 'Upload MB', 'data': [mb(rows[k]['u']) for k in labels], 'unit': 'mb'}]}


def s_top_users(ctx, hours=24, **_):
    from .models import UsageRecord
    hours = max(1, min(168, int(hours or 24)))
    rows = list(UsageRecord.objects.filter(business=ctx.business, hour__gte=timezone.now() - timedelta(hours=hours))
                .values('username').annotate(d=Sum('download')).order_by('-d')[:10])
    return {'labels': [r['username'] for r in rows], 'series': [{'name': 'Download MB', 'data': [round(_f(r['d']) / 1048576, 1) for r in rows], 'unit': 'mb'}]}


def s_top_apps(ctx, hours=24, **_):
    from .models import AppUsage
    hours = max(1, min(168, int(hours or 24)))
    rows = list(AppUsage.objects.filter(business=ctx.business, hour__gte=timezone.now() - timedelta(hours=hours))
                .values('app').annotate(d=Sum('download')).order_by('-d')[:8])
    return {'labels': [r['app'] for r in rows], 'series': [{'name': 'Download MB', 'data': [round(_f(r['d']) / 1048576, 1) for r in rows], 'unit': 'mb'}]}


def s_kpis(ctx, **_):
    b, tiles = ctx.business, []
    if ctx.can(FIN):
        start = timezone.make_aware(datetime.combine(timezone.localdate(), dtime.min))
        s = b.sales.filter(sold_at__gte=start).aggregate(v=Sum('amount'), n=Count('id'))
        y = b.sales.filter(sold_at__gte=start - timedelta(days=1), sold_at__lt=timezone.now() - timedelta(days=1)).aggregate(v=Sum('amount'))
        tiles.append({'name': 'Sales today', 'value': _f(s['v']), 'unit': 'money', 'compare': _f(y['v']), 'compare_label': 'same time yesterday'})
        tiles.append({'name': 'Vouchers sold today', 'value': int(s['n'] or 0), 'unit': 'count'})
    if ctx.can(NET):
        rs = _routers(ctx)
        tiles.append({'name': 'Users online', 'value': sum(_online(r) for r in rs), 'unit': 'count', 'live': True})
        tiles.append({'name': 'Routers online', 'value': sum(1 for r in rs if (r.status or '').lower() == 'online'), 'unit': 'count',
                      'of': len(rs)})
    if ctx.can(VOU):
        from .models_voucher_reports import VoucherReport
        tiles.append({'name': 'Open customer reports', 'value': VoucherReport.objects.filter(business=b, status='open').count(), 'unit': 'count'})
    return {'tiles': tiles}


# key: (title, permissions, default type, live, function, help)
SOURCES = {
    'kpis': ('Key numbers', (), 'kpi', True, s_kpis, 'Sales today, users online, routers online, open reports'),
    'sales_by_day': ('Sales per day', FIN, 'bar', False, s_sales_by_day, 'Revenue and vouchers sold per day (days = 2–120)'),
    'sales_by_hour': ('Sales per hour — today vs yesterday', FIN, 'line', True, s_sales_by_hour, 'Hourly revenue, today against yesterday'),
    'sales_by_plan': ('Sales by plan', FIN, 'doughnut', False, s_sales_by_plan, 'Revenue share per plan for a period'),
    'sales_by_agent': ('Sales by agent', FIN, 'bar', False, s_sales_by_agent, 'Revenue per agent for a period'),
    'vouchers_by_state': ('Vouchers by state', VOU, 'doughnut', False, s_vouchers_by_state, 'Stock, sold, in use, frozen, expired'),
    'online_users': ('Users online per router', NET, 'bar', True, s_online_users, 'Live users per router (history builds up on a dashboard)'),
    'router_load': ('Router CPU load', NET, 'bar', True, s_router_load, 'Live CPU % per router'),
    'data_by_hour': ('Data used per hour', NET, 'area', False, s_data_by_hour, 'Download / upload MB per hour (hours = 2–168)'),
    'top_users': ('Heaviest users', NET, 'bar', False, s_top_users, 'Top 10 users by download (hours)'),
    'top_apps': ('Top apps', NET, 'doughnut', False, s_top_apps, 'Apps by download (hours)'),
}


def clean_spec(spec):
    """A chart the model asked for, reduced to known keys."""
    src = str(spec.get('source') or '').strip()
    if src not in SOURCES:
        raise ChartError(f'Unknown data source “{src}”. Choose one of: {", ".join(SOURCES)}.')
    typ = str(spec.get('type') or SOURCES[src][2]).strip()
    if typ not in TYPES:
        typ = SOURCES[src][2]
    if src == 'kpis':
        typ = 'kpi'
    out = {'source': src, 'type': typ, 'title': str(spec.get('title') or SOURCES[src][0])[:80]}
    for k, lo, hi in (('days', 2, 120), ('hours', 1, 168)):
        if spec.get(k) not in (None, ''):
            out[k] = max(lo, min(hi, int(spec[k])))
    if spec.get('period'):
        out['period'] = str(spec['period'])[:12]
    if spec.get('router'):
        out['router'] = str(spec['router'])[:60]
    return out


def data(ctx, spec):
    """Figures for one chart (computed now). Adds the live history for live sources."""
    spec = clean_spec(spec)
    title, need, _, live, fn, _ = SOURCES[spec['source']]
    if not ctx.can(need):
        return {'spec': spec, 'error': 'Your role cannot see this information.'}
    out = fn(ctx, **{k: v for k, v in spec.items() if k in ('days', 'hours', 'period', 'router')})
    out.update(spec=spec, live=live, at=timezone.now().isoformat())
    if live and spec['source'] in ('online_users', 'router_load', 'kpis'):
        out['history'] = _history(ctx.business.pk, spec, out)
    return out


def _history(business_id, spec, out):
    key = f'fit:chart:hist:{business_id}:{spec["source"]}:{spec.get("router", "")}'
    hist = cache.get(key) or []
    now = time.time()
    if spec['source'] == 'kpis':
        point = {t['name']: t['value'] for t in out.get('tiles', []) if t.get('live')}
    else:
        point = dict(zip(out['labels'], out['series'][0]['data']))
    if not hist or now - hist[-1]['t'] >= HISTORY_MIN_GAP:
        hist.append({'t': now, 'v': point})
        hist = hist[-HISTORY_POINTS:]
        cache.set(key, hist, 3 * 3600)
    return [{'t': timezone.localtime(datetime.fromtimestamp(h["t"], tz=dt_timezone.utc)).strftime('%H:%M:%S'), 'v': h['v']} for h in hist]


def summary_for_model(d):
    """A compact version of the figures for the model, so it can comment on what the chart shows."""
    if d.get('error'):
        return {'error': d['error']}
    if 'tiles' in d:
        return {'tiles': [{k: t[k] for k in ('name', 'value', 'unit') if k in t} for t in d['tiles']]}
    s = d['series'][0]
    pairs = list(zip(d['labels'], s['data']))
    top = sorted(pairs, key=lambda x: -float(x[1] or 0))[:5]
    return {'points': len(pairs), 'series': [x['name'] for x in d['series']], 'total_first_series': round(sum(float(v or 0) for v in s['data']), 2),
            'highest': top, 'last': pairs[-3:]}


def dashboard_data(ctx, dash):
    return {'id': dash.pk, 'title': dash.title, 'refresh_seconds': dash.refresh_seconds,
            'widgets': [data(ctx, w) for w in dash.widgets or []]}
