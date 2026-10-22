"""What Fit can look at.

Every tool gets a FitContext made by the view from the *request*: the business is the one the
TeamAccessMiddleware attached (request.tt_business) and the permissions are the person's own
(request.tt_perms). Nothing the model writes can choose another business: there is no business
argument anywhere, every query starts from ``ctx.business``, and objects are always looked up
inside it (``ctx.business.vouchers.filter(...)``, ``ctx.business.routers.filter(...)``).

Each tool also names the permissions it needs (any of). Tools a person's role cannot use are not
even described to the model, and are refused again if called.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Count, Max, Q, Sum
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger('taptap.fit')

VOUCHER_PERMS = ('vouchers.view', 'vouchers.support', 'vouchers.create')
FINANCE_PERMS = ('finance.view',)
AGENT_PERMS = ('finance.view', 'finance.agents', 'agents.view')
NETWORK_PERMS = ('network.manage',)
PERIODS = ('today', '7d', '30d', '90d', 'month', 'last_month', 'year')


@dataclass
class FitContext:
    business: object
    user: object
    perms: frozenset
    settings: object
    conversation: object = None
    role_label: str = ''
    cards: dict = field(default_factory=dict)       # filled by tools: actions, tests, links

    def can(self, need):
        return not need or any(p in self.perms for p in need)


class ToolError(ValueError):
    pass


# ─────────────────────────────── formatting ───────────────────────────────

def money(ctx, v):
    v = Decimal(str(v or 0)).quantize(Decimal('0.01'))
    s = f'{v:,.2f}'.rstrip('0').rstrip('.') if v == v.to_integral() else f'{v:,.2f}'
    return f'{ctx.business.currency or "D"}{s}'


def when(dt):
    return timezone.localtime(dt).strftime('%d %b %H:%M') if dt else None


def ago(dt):
    if not dt:
        return None
    s = int((timezone.now() - dt).total_seconds())
    if s < 90:
        return 'just now'
    if s < 5400:
        return f'{s // 60} min ago'
    if s < 172800:
        return f'{s // 3600} h ago'
    return f'{s // 86400} days ago'


def pct(cur, prev):
    cur, prev = float(cur or 0), float(prev or 0)
    if prev == 0:
        return None if cur == 0 else 100.0
    return round((cur - prev) / prev * 100, 1)


def mb(n):
    n = int(n or 0)
    return f'{n / 1e9:.2f} GB' if n >= 1e9 else f'{n / 1e6:.0f} MB'


def _router(ctx, name_or_id):
    """A router of THIS business by name or id. Without one: the only router, if there is exactly one."""
    qs = ctx.business.routers.all()
    v = str(name_or_id or '').strip()
    if v:
        r = qs.filter(pk=int(v)).first() if v.isdigit() else None
        r = r or qs.filter(name__iexact=v).first() or qs.filter(name__icontains=v).first()
        if r:
            return r
        raise ToolError(f'No router called “{v}” in this business. Routers: ' + ', '.join(qs.values_list('name', flat=True)[:20]))
    if qs.count() == 1:
        return qs.first()
    names = ', '.join(qs.values_list('name', flat=True)[:20])
    raise ToolError(f'Which router? This business has: {names}.' if names else 'This business has no routers yet.')


# ─────────────────────────────── overview ───────────────────────────────

def t_overview(ctx):
    b, now = ctx.business, timezone.now()
    out = {'business': b.business_name, 'currency': b.currency, 'now': when(now)}
    if ctx.can(VOUCHER_PERMS):
        v = b.vouchers
        out['vouchers'] = {'active_unused_stock': v.filter(status='active', used_at__isnull=True, sold_at__isnull=True).count(),
                           'in_use_now': v.filter(status='active', used_at__isnull=False, expires_at__gt=now).count(),
                           'frozen': v.filter(frozen_at__isnull=False).count(),
                           'link': reverse('vouchers')}
        from .models_voucher_reports import VoucherReport
        out['open_customer_reports'] = {'count': VoucherReport.objects.filter(business=b, status='open').count(),
                                        'link': reverse('voucher_reports')}
    if ctx.can(('finance.view', 'sales.daily', 'dashboard.view')):
        start = timezone.make_aware(datetime.combine(timezone.localdate(), dtime.min))
        s = b.sales.filter(sold_at__gte=start).aggregate(n=Count('id'), v=Sum('amount'))
        out['sales_today'] = {'count': s['n'] or 0, 'amount': money(ctx, s['v'])}
    if ctx.can(NETWORK_PERMS):
        out['routers'] = _router_rows(ctx)
        out['open_session_incidents'] = b.session_incidents.filter(status='open').count()
    return out


def _router_rows(ctx):
    from .linkops import is_link
    rows = []
    for r in ctx.business.routers.select_related('agent').all()[:30]:
        row = {'name': r.name, 'status': r.status, 'mode': 'TapTap Link' if is_link(r) else 'Direct API',
               'last_checked': ago(r.last_watch_at or r.last_tested_at), 'link': reverse('router_control', args=[r.pk])}
        if r.last_error:
            row['last_error'] = r.last_error[:160]
        if is_link(r):
            try:
                a = r.agent
                row.update({'link_online': a.online, 'last_seen': ago(a.last_seen_at), 'cpu_load_pct': a.cpu_load,
                            'active_sessions': a.active_sessions, 'uptime': a.uptime, 'routeros': a.ros_version,
                            'memory_free_pct': round(a.memory_free / a.memory_total * 100) if a.memory_total else None})
            except Exception:
                row['link_online'] = False
        rows.append(row)
    return rows


# ─────────────────────────────── finance ───────────────────────────────

def t_finance(ctx, period='30d'):
    from .finance import finance_summary, resolve_period
    p = resolve_period({'range': period if period in PERIODS else '30d'})
    f = finance_summary(ctx.business, p)
    exps = (ctx.business.expenses.filter(paid_at__gte=p.start, paid_at__lt=p.end).values('category')
            .annotate(v=Sum('amount')).order_by('-v')[:5])
    from .finance import CATEGORY_LABELS
    plans = (ctx.business.sales.filter(sold_at__gte=p.start, sold_at__lt=p.end).values('plan_name')
             .annotate(n=Count('id'), v=Sum('amount')).order_by('-v')[:6])
    return {
        'period': p.label, 'compared_with_previous_period_of_same_length': True,
        'revenue': money(ctx, f['revenue']), 'revenue_change_pct': f['revenue_change'],
        'agent_commission': money(ctx, f['commission']), 'discounts': money(ctx, f['discounts']),
        'expenses': money(ctx, f['expenses']), 'expenses_change_pct': f['expenses_change'],
        'profit': money(ctx, f['profit']), 'profit_change_pct': f['profit_change'], 'margin_pct': f['margin'],
        'sales_count': f['sales_count'], 'sales_change_pct': f['sales_change'], 'average_sale': money(ctx, f['avg_ticket']),
        'money_agents_still_owe': money(ctx, f['outstanding']), 'cash_position_all_time': money(ctx, f['cash_position']),
        'this_month': {'revenue': money(ctx, f['month_revenue']), 'target': money(ctx, f['target']) if f['target'] else None,
                       'projected_month_end': money(ctx, f['projected']), 'target_pct_so_far': f['target_pct']},
        'unsold_stock': {'vouchers': f['stock_count'], 'value': money(ctx, f['stock_value'])},
        'top_expenses': [{'category': CATEGORY_LABELS.get(e['category'], e['category']), 'amount': money(ctx, e['v'])} for e in exps],
        'best_selling_plans': [{'plan': x['plan_name'], 'sold': x['n'], 'revenue': money(ctx, x['v'])} for x in plans],
        'link': reverse('finance'),
    }


def agent_rows(ctx, period='30d'):
    from .finance import agent_balances, resolve_period
    p = resolve_period({'range': period if period in PERIODS else '30d'})
    cur = {r['agent']: r for r in ctx.business.sales.filter(agent__isnull=False, sold_at__gte=p.start, sold_at__lt=p.end)
           .values('agent').annotate(n=Count('id'), v=Sum('amount'))}
    prev = {r['agent']: r for r in ctx.business.sales.filter(agent__isnull=False, sold_at__gte=p.prev_start, sold_at__lt=p.prev_end)
            .values('agent').annotate(n=Count('id'), v=Sum('amount'))}
    last = dict(ctx.business.sales.filter(agent__isnull=False).values('agent').annotate(m=Max('sold_at')).values_list('agent', 'm'))
    bal = {r['agent'].pk: r for r in agent_balances(ctx.business)}
    rows = []
    for a in ctx.business.agents.filter(active=True):
        c, pr, bl = cur.get(a.pk, {}), prev.get(a.pk, {}), bal.get(a.pk, {})
        change = pct(c.get('v'), pr.get('v'))
        flag = ''
        if pr.get('v') and change is not None and change <= -30:
            flag = 'sales dropped'
        elif pr.get('n') and not c.get('n'):
            flag = 'no sales this period'
        if bl.get('outstanding') and bl['outstanding'] > 0 and bl.get('collection_rate', 100) < 60:
            flag = (flag + '; ' if flag else '') + 'low cash collection'
        rows.append({'agent': a.name, 'location': a.location, 'sold': c.get('n', 0), 'revenue': money(ctx, c.get('v')),
                     'previous_period_revenue': money(ctx, pr.get('v')), 'change_pct': change, 'flag': flag,
                     'last_sale': ago(last.get(a.pk)), 'owes_now': money(ctx, bl.get('outstanding')),
                     'collection_rate_pct': bl.get('collection_rate'), 'vouchers_holding': bl.get('holding', 0),
                     'link': reverse('agent_detail', args=[a.pk]), '_change': change if change is not None else 0,
                     '_flag': bool(flag)})
    rows.sort(key=lambda r: (not r['_flag'], r['_change']))
    return p, rows


def t_agents(ctx, period='30d', agent=''):
    p, rows = agent_rows(ctx, period)
    if agent:
        rows = [r for r in rows if agent.lower() in r['agent'].lower()]
    for r in rows:
        r.pop('_change'), r.pop('_flag')
    return {'period': p.label, 'compared_with': 'the previous period of the same length', 'agents': rows[:25],
            'link': reverse('finance') + '#agents'}


# ─────────────────────────────── vouchers & reports ───────────────────────────────

def _events(v, n=8):
    return [{'when': when(e.created_at), 'event': e.get_event_display(), 'reason': e.reason, 'via': e.via,
             'by': (e.user.get_full_name() or e.user.username) if e.user_id else e.get_source_display()}
            for e in v.events.select_related('user')[:n]]


def t_find_voucher(ctx, query):
    from .online_portal import diagnose
    from .voucher_freeze import time_left_seconds
    q = str(query or '').strip()
    if not q:
        raise ToolError('Give a voucher code, serial, customer name or phone.')
    b = ctx.business
    exact = b.vouchers.filter(code__iexact=q.replace(' ', '')).select_related('router', 'agent', 'batch').first()
    found = [exact] if exact else list(b.vouchers.filter(Q(serial__iexact=q) | Q(customer_phone__icontains=q) | Q(customer_name__icontains=q))
                                       .select_related('router', 'agent', 'batch')[:5])
    if not found:
        return {'found': False, 'message': f'No voucher matching “{q}” in {b.business_name}.'}
    out = []
    for v in found:
        d = diagnose(b, v.code)
        left = time_left_seconds(v)
        out.append({'code': v.code, 'member': v.is_member, 'plan': v.plan_name, 'price': money(ctx, v.price), 'status': v.status,
                    'frozen': bool(v.frozen_at), 'freeze_reason': v.freeze_reason or None, 'first_used': when(v.used_at),
                    'ends': when(v.expires_at), 'time_left_minutes': left // 60 if left is not None else None,
                    'devices_allowed': v.max_devices, 'devices_locked': v.device_bindings.count(),
                    'router': v.router.name if v.router_id else None, 'agent': v.agent.name if v.agent_id else None,
                    'batch': v.batch.name if v.batch_id else None, 'customer': v.customer_name or None,
                    'diagnosis': {'state': d['state'], 'title': d['title'], 'checks': [t for _, t in d['checks']]},
                    'recent_history': _events(v, 6) if len(found) == 1 else None, 'link': reverse(
                        'member_detail' if v.is_member else 'voucher_detail', args=[v.pk])})
    return {'found': True, 'vouchers': out}


def t_reports(ctx, status='open', auto_close=True):
    from .models_voucher_reports import VoucherReport
    from .online_portal import diagnose
    qs = VoucherReport.objects.filter(business=ctx.business).select_related('voucher')
    qs = qs if status == 'all' else qs.filter(status='open')
    rows, closed = [], []
    allow_auto = ctx.settings.allows('reports') and ctx.settings.auto_close_reports and ctx.can(('vouchers.support',))
    for r in qs[:25]:
        now = diagnose(ctx.business, r.code)
        before = (r.diagnosis or {}).get('state')
        # The problem the customer had is gone: close it (only when the owner switched this on).
        if allow_auto and auto_close and r.status == 'open' and before and before != 'ok' and now['state'] == 'ok':
            r.status, r.resolved_at = 'resolved', timezone.now()
            r.staff_note = f'Closed by Fit: the voucher was “{(r.diagnosis or {}).get("title", before)}” and now works.'[:500]
            r.save(update_fields=['status', 'resolved_at', 'staff_note'])
            closed.append({'id': r.pk, 'code': r.code})
            continue
        rows.append({'id': r.pk, 'code': r.code, 'sent': ago(r.created_at), 'status': r.get_status_display(),
                     'customer_name': r.name or None, 'customer_phone': r.phone or None,
                     'customer_message_UNTRUSTED': r.message or None, 'portal_told_them': r.shown_error or None,
                     'state_when_sent': before, 'state_now': now['state'], 'diagnosis_now': now['title'],
                     'checks_now': [t for _, t in now['checks']], 'staff_note': r.staff_note or None})
    out = {'reports': rows, 'link': reverse('voucher_reports')}
    if closed:
        out['auto_closed_because_fixed'] = closed
    return out


# ─────────────────────────────── network ───────────────────────────────

def t_network_status(ctx):
    b = ctx.business
    since = timezone.now() - timedelta(hours=24)
    down = b.device_alerts.filter(event='offline', created_at__gte=since).select_related('router')
    back = set(b.device_alerts.filter(event='online', created_at__gte=since).values_list('device_key', flat=True))
    return {'routers': _router_rows(ctx),
            'open_session_incidents': [{'user': i.username, 'router': i.router.name, 'reason': i.get_reason_display(), 'since': ago(i.first_seen)}
                                       for i in b.session_incidents.filter(status='open').select_related('router')[:10]],
            'devices_offline_now': [{'name': a.name or a.ip_address or a.mac_address, 'kind': a.kind, 'router': a.router.name,
                                     'since': ago(a.offline_since or a.created_at)} for a in down if a.device_key not in back][:15],
            'links': {'routers': reverse('routers'), 'alerts': reverse('alerts'), 'topology': reverse('topology')}}


def t_network_events(ctx, hours=24):
    from .models import VoucherEvent
    from .models_voucher_reports import VoucherReport
    hours = max(1, min(168, int(hours or 24)))
    b, since = ctx.business, timezone.now() - timedelta(hours=hours)
    out = {'window_hours': hours}
    if ctx.can(NETWORK_PERMS):
        al = b.device_alerts.filter(created_at__gte=since)
        out['device_alerts'] = {'went_offline': al.filter(event='offline').count(), 'came_back': al.filter(event='online').count(),
                                'most_affected': [{'device': x['name'] or '?', 'times_offline': x['n']} for x in
                                                  al.filter(event='offline').values('name').annotate(n=Count('id')).order_by('-n')[:5]]}
        inc = b.session_incidents.filter(last_seen__gte=since)
        out['sessions_that_should_not_be_online'] = {'total': inc.count(), 'fixed_automatically': inc.filter(fixed_by='auto').count(),
                                                     'still_open': inc.filter(status='open').count()}
        out['sharing_suspects'] = [{'voucher': c.code, 'mac': c.mac, 'score': c.score, 'status': c.get_status_display()}
                                   for c in b.sharing_cases.filter(last_seen__gte=since).order_by('-score')[:6]]
        out['network_tests'] = [{'test': t.get_kind_display(), 'router': t.router.name, 'status': t.status, 'when': ago(t.created_at)}
                                for t in b.net_tests.filter(created_at__gte=since).select_related('router')[:6]]
        out['blocked_device_attempts'] = b.device_signatures.filter(blacklist_last_hit__gte=since).count()
    if ctx.can(VOUCHER_PERMS):
        ev = VoucherEvent.objects.filter(business=b, created_at__gte=since).values('event').annotate(n=Count('id')).order_by('-n')
        labels = dict(VoucherEvent.EVENTS)
        out['voucher_events'] = {labels.get(e['event'], e['event']): e['n'] for e in ev[:12]}
        out['new_customer_reports'] = VoucherReport.objects.filter(business=b, created_at__gte=since).count()
    if ctx.can(('finance.view', 'sales.daily', 'dashboard.view')):
        s = b.sales.filter(sold_at__gte=since).aggregate(n=Count('id'), v=Sum('amount'))
        out['sales'] = {'count': s['n'] or 0, 'amount': money(ctx, s['v'])}
    out['activity_log'] = [{'when': when(a.created_at), 'what': a.type, 'details': a.details[:120], 'by': a.actor}
                           for a in b.activities.filter(created_at__gte=since).order_by('-created_at')[:10]] if ctx.can(NETWORK_PERMS) else None
    return out


def t_usage(ctx, hours=24):
    from .models import AppUsage, UsageRecord
    hours = max(1, min(168, int(hours or 24)))
    since = timezone.now() - timedelta(hours=hours)
    users = (UsageRecord.objects.filter(business=ctx.business, hour__gte=since).values('username')
             .annotate(d=Sum('download'), u=Sum('upload')).order_by('-d')[:10])
    apps = (AppUsage.objects.filter(business=ctx.business, hour__gte=since).values('app')
            .annotate(d=Sum('download'), u=Sum('upload')).order_by('-d')[:10])
    tot = UsageRecord.objects.filter(business=ctx.business, hour__gte=since).aggregate(d=Sum('download'), u=Sum('upload'))
    return {'window_hours': hours, 'total_download': mb(tot['d']), 'total_upload': mb(tot['u']),
            'top_users': [{'voucher_or_user': x['username'], 'download': mb(x['d']), 'upload': mb(x['u'])} for x in users],
            'top_apps': [{'app': x['app'], 'download': mb(x['d'])} for x in apps], 'link': reverse('traffic')}


def t_find_device(ctx, query):
    from .device_block import blocked_macs, norm
    q = str(query or '').strip()
    if len(q) < 3:
        raise ToolError('Give at least 3 characters of a MAC, IP, name or model.')
    mac = norm(q)
    b = ctx.business
    sig = b.device_signatures.filter(Q(last_mac__iexact=mac or q) | Q(label__icontains=q) | Q(model__icontains=q) | Q(last_ip=q))[:6]
    from .models import RouterDevice
    dev = RouterDevice.objects.filter(router__business=b).filter(
        Q(mac_address__iexact=mac or q) | Q(ip_address=q) | Q(hostname__icontains=q)).select_related('router')[:8]
    blocked = blocked_macs(b)
    return {'portal_devices': [{'name': str(s), 'type': s.device_type, 'os': s.os, 'mac': s.last_mac, 'ip': s.last_ip,
                                'vouchers_used': (s.vouchers or [])[-5:], 'flag': s.flag_level or None,
                                'blacklisted': bool(s.blacklisted_at), 'last_seen': ago(s.last_seen),
                                'link': reverse('device_detail', args=[s.pk])} for s in sig],
            'router_devices': [{'name': d.hostname or None, 'mac': d.mac_address, 'ip': d.ip_address, 'router': d.router.name,
                                'interface': d.interface_name, 'type': d.connection_type, 'online': d.is_online,
                                'blocked': norm(d.mac_address) in blocked, 'last_seen': ago(d.last_seen_at)} for d in dev]}


# Live pulls: read-only menus, secrets redacted, rows capped.
PULLS = {
    'resources': '/system/resource', 'sessions': '/ip/hotspot/active', 'interfaces': '/interface',
    'dhcp_leases': '/ip/dhcp-server/lease', 'ip_bindings': '/ip/hotspot/ip-binding', 'neighbors': '/ip/neighbor',
    'hotspot_hosts': '/ip/hotspot/host', 'routes': '/ip/route', 'dns': '/ip/dns', 'logs': '/log',
    'hotspot_users': '/ip/hotspot/user', 'queues': '/queue/simple', 'health': '/system/health',
}
PULL_KEEP = ('name', 'type', 'running', 'disabled', 'comment', 'user', 'address', 'mac-address', 'host-name', 'status',
             'uptime', 'session-time-left', 'bytes-in', 'bytes-out', 'rx-byte', 'tx-byte', 'login-by', 'server',
             'cpu-load', 'free-memory', 'total-memory', 'version', 'board-name', 'architecture-name', 'free-hdd-space',
             'time', 'topics', 'message', 'dst-address', 'gateway', 'distance', 'active', 'identity', 'interface',
             'platform', 'servers', 'dynamic-servers', 'expires-after', 'last-seen', 'to-address', 'profile',
             'limit-uptime', 'max-limit', 'target', 'value', 'cpu-count', 'link-downs', 'temperature', 'voltage')


def _trim(row):
    from .mikrotik import redact
    row = redact(dict(row))
    return {k: str(v)[:80] for k, v in row.items() if k in PULL_KEEP and v not in (None, '')}


def t_router_live(ctx, router='', what='resources', refresh=False):
    from .linkops import SnapshotService, refresh as link_refresh, uses_link
    from .linklive import link_state, sessions
    from .mikrotik import MikroTikService
    r = _router(ctx, router)
    if what not in PULLS:
        raise ToolError(f'Choose one of: {", ".join(PULLS)}.')
    path, via = PULLS[what], 'Direct API'
    note = None
    if uses_link(r):
        via = 'TapTap Link'
        online, why = link_state(r)
        if refresh and online:
            try:
                link_refresh(r, user=ctx.user)
                note = 'A fresh inventory pull was started over TapTap Link; it lands within a minute. These rows are the latest synced copy.'
            except Exception as exc:  # noqa: BLE001
                note = f'Could not start a fresh pull: {exc}'
        if what == 'sessions':
            rows = sessions(r)
        elif what == 'resources':
            a = r.agent
            rows = [{'identity': a.identity, 'version': a.ros_version, 'board-name': a.board, 'uptime': a.uptime,
                     'cpu-load': a.cpu_load, 'free-memory': a.memory_free, 'total-memory': a.memory_total}]
        else:
            rows = SnapshotService(r).resource(path).get()
            if not rows and what == 'logs':
                note = (note or '') + ' Router logs are not part of the TapTap Link sync — open MikroTik Control for them.'
        if not online:
            note = (note or '') + f' {why}'
    else:
        try:
            with MikroTikService(r) as svc:
                rows = svc.safe_get(path)
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f'{r.name} did not answer: {exc}')
    rows = [dict(x) for x in rows or []]
    if what == 'logs':
        rows = rows[-30:]
    total = len(rows)
    return {'router': r.name, 'via': via, 'menu': path, 'rows_total': total, 'rows': [_trim(x) for x in rows[:40]],
            'note': note, 'link': reverse('router_control', args=[r.pk])}


def t_router_query(ctx, router='', menu='resources', where=None, limit=30):
    """Read straight from a MikroTik (live API / Tunnel; else the latest TapTap Link copy)."""
    from . import fit_router
    r = _router(ctx, router)
    try:
        out = fit_router.query(r, menu, where or [], limit)
    except fit_router.RouterOpError as exc:
        raise ToolError(str(exc))
    out['link'] = reverse('router_control', args=[r.pk])
    return out


def t_make_chart(ctx, source, type='', title='', period='', days=None, hours=None, router=''):
    """Draw a chart in the chat from a TapTap data source (figures computed by TapTap, never by the model)."""
    from . import fit_charts
    spec = {'source': source, 'type': type, 'title': title, 'period': period, 'days': days, 'hours': hours, 'router': router}
    try:
        d = fit_charts.data(ctx, {k: v for k, v in spec.items() if v not in (None, '')})
    except (fit_charts.ChartError, ValueError) as exc:
        raise ToolError(str(exc))
    if d.get('error'):
        raise ToolError(d['error'])
    ctx.cards.setdefault('charts', []).append(d)
    return {'chart_shown': True, 'title': d['spec']['title'], 'live': d['live'], 'figures': fit_charts.summary_for_model(d),
            'next': 'The chart is now shown under your reply. Comment on what it shows in one or two sentences.'}


def t_create_dashboard(ctx, title, widgets, refresh_seconds=30, dashboard_id=None):
    """Save (or extend) a live dashboard of charts. It refreshes itself on its own page."""
    from . import fit_charts
    from .models_fit import FitDashboard
    specs = []
    for w in (widgets or [])[:12]:
        try:
            spec = fit_charts.clean_spec(w if isinstance(w, dict) else {'source': str(w)})
        except fit_charts.ChartError as exc:
            raise ToolError(str(exc))
        if not ctx.can(fit_charts.SOURCES[spec['source']][1]):
            raise ToolError(f'Your role cannot see “{spec["title"]}”.')
        specs.append(spec)
    if not specs:
        raise ToolError('A dashboard needs at least one chart (widgets).')
    refresh = max(10, min(600, int(refresh_seconds or 30)))
    if dashboard_id:
        d = FitDashboard.objects.filter(business=ctx.business, pk=dashboard_id).first()
        if not d:
            raise ToolError('There is no such dashboard in this business.')
        d.widgets = (d.widgets or []) + specs
        d.refresh_seconds = refresh
        if title:
            d.title = str(title)[:120]
        d.save()
    else:
        d = FitDashboard.objects.create(business=ctx.business, created_by=ctx.user, title=str(title or 'My dashboard')[:120],
                                        widgets=specs, refresh_seconds=refresh)
    link = reverse('fit_dashboard', args=[d.pk])
    ctx.cards.setdefault('dashboards', []).append({'id': d.pk, 'title': d.title, 'link': link, 'widgets': len(d.widgets),
                                                   'refresh_seconds': d.refresh_seconds})
    return {'saved': True, 'dashboard_id': d.pk, 'title': d.title, 'widgets': len(d.widgets), 'refresh_seconds': d.refresh_seconds,
            'link': link, 'next': f'Give the person the link as markdown: [Open {d.title}]({link}).'}


def t_dashboards(ctx):
    from .models_fit import FitDashboard
    return {'dashboards': [{'id': d.pk, 'title': d.title, 'widgets': [w.get('title') for w in d.widgets or []],
                            'refresh_seconds': d.refresh_seconds, 'link': reverse('fit_dashboard', args=[d.pk])}
                           for d in FitDashboard.objects.filter(business=ctx.business)[:20]]}


# ─────────────────────────────── network tests ───────────────────────────────

def t_run_test(ctx, router='', kind='doctor', target=''):
    """Start a test on one router of this business (the same code path as the Network tools page)."""
    from . import nettools as nt
    from .models_nettools import NetTest
    from .voucher_history import channel
    if not ctx.settings.allows('tests'):
        raise ToolError('The business owner has not allowed Fit to run network tests. They can switch on “Network tests” in Fit settings.')
    if kind not in ('doctor', 'ping', 'trace', 'dns', 'web', 'speed'):
        raise ToolError('Choose doctor (internet check), ping, trace, dns, web or speed.')
    r = _router(ctx, router)
    params = nt.clean_params(kind, {'target': target})
    nt.limit_check(ctx.business, r, kind)
    via = channel(r)
    t = NetTest.objects.create(business=ctx.business, router=r, kind=kind, target=params.get('target', ''), params=dict(params),
                               via=via, created_by=ctx.user, status='waiting' if via == 'TapTap Link' else 'running')
    if via == 'TapTap Link':
        from .linkops import send
        try:
            cmd = send(r, 'nettest', {'test_id': t.pk, 'kind': kind, **params},
                       label=f'Fit: {t.get_kind_display()} {params.get("target", "")}'.strip(), user=ctx.user, minutes=6)
            t.command_id = cmd.pk
            t.save(update_fields=['command_id'])
        except ValueError as exc:
            t.status, t.result, t.finished_at = 'failed', {'error': f'{r.name} cannot take commands right now: {exc}'}, timezone.now()
            t.save(update_fields=['status', 'result', 'finished_at'])
        deadline = time.monotonic() + 20                       # the router checks in every few seconds
        while t.status == 'waiting' and time.monotonic() < deadline:
            time.sleep(2)
            t.refresh_from_db()
            nt.expire_waiting(t)
    else:
        try:
            t.result, t.status = nt.run_api(t), 'done'
        except Exception as exc:  # noqa: BLE001
            t.status, t.result = 'failed', {'error': nt._ros_error(exc)}
        t.finished_at = timezone.now()
        t.save(update_fields=['status', 'result', 'finished_at'])
    ctx.cards.setdefault('tests', []).append({'id': t.pk, 'kind': t.get_kind_display(), 'router': r.name,
                                              'target': t.target, 'status': t.status, 'summary': nt.summary(t)})
    return _test_json(t)


def _test_json(t):
    from . import nettools as nt
    res = t.result or {}
    slim = {k: v for k, v in res.items() if k not in ('raw', 'lines', 'times') and not isinstance(v, (list, dict))}
    if isinstance(res.get('steps'), list):
        slim['steps'] = [{k: s.get(k) for k in ('title', 'ok', 'text', 'detail') if k in s} for s in res['steps'][:8]]
    if isinstance(res.get('hops'), list):
        slim['hops'] = [{k: h.get(k) for k in ('n', 'address', 'avg', 'loss') if k in h} for h in res['hops'][:15]]
    return {'test_id': t.pk, 'test': t.get_kind_display(), 'router': t.router.name, 'target': t.target, 'status': t.status,
            'summary': nt.summary(t), 'result': slim, 'link': reverse('network_tools'),
            'note': 'Still waiting for the router; call test_result with this test_id in a moment.' if t.status == 'waiting' else None}


def t_test_result(ctx, test_id):
    from . import nettools as nt
    t = ctx.business.net_tests.select_related('router').filter(pk=int(test_id or 0)).first()
    if not t:
        raise ToolError('No such test in this business.')
    nt.expire_waiting(t)
    return _test_json(t)


# ─────────────────────────────── chat & help ───────────────────────────────

def t_chat_recent(ctx, limit=15):
    from . import chat
    from .models_chat import ChatMessage
    t = chat.team_thread(ctx.business)
    msgs = list(ChatMessage.objects.filter(thread=t).select_related('sender').order_by('-id')[:max(1, min(30, int(limit or 15)))])
    return {'thread': f'{ctx.business.business_name} team', 'messages_UNTRUSTED': [
        {'when': when(m.created_at), 'from': chat.display_name(m.sender) if m.sender_id else 'TapTap', 'text': (m.body or m.page_title)[:300]}
        for m in reversed(msgs)], 'link': reverse('chat_page')}


def t_help(ctx, query):
    from .help_guides import GUIDES
    words = [w for w in str(query or '').lower().split() if len(w) > 2]
    scored = []
    for g in GUIDES:
        hay = ' '.join([g['title'], g.get('summary', ''), g.get('keywords', '')]).lower()
        s = sum(hay.count(w) for w in words)
        if s:
            scored.append((s, g))
    scored.sort(key=lambda x: -x[0])
    return {'guides': [{'title': g['title'], 'summary': g.get('summary', ''), 'link': reverse('support_guide', args=[g['slug']])}
                       for _, g in scored[:4]]}


def t_propose(ctx, kind, **args):
    from .fit_actions import propose
    card = propose(ctx, kind, args)
    out = {'proposed': True, 'action_id': card['id'], 'title': card['title'], 'detail': card['detail'],
           'owner_allows_this_group': card['authorized'],
           'next': 'A “Take action” card is now shown. Nothing changes until the person presses it.'
                   + ('' if card['authorized'] else ' The business owner must first allow “' + card['group_label'] + '” in Fit settings.')}
    if card.get('facts') or card.get('warnings'):      # group change: tell the person the context in your reply too
        out.update({'facts': card.get('facts'), 'warnings': card.get('warnings'), 'sample_codes': card.get('sample'),
                    'type_to_confirm': card.get('confirm') or None})
    return out


def t_count_vouchers(ctx, **filters):
    from .fit_bulk import t_count_vouchers as count
    return count(ctx, **filters)


# ─────────────────────────────── monitors ───────────────────────────────

def t_slots_report(ctx, plans=''):
    from .fit_monitors import slots_report
    out = slots_report(ctx.business, plans)
    out['hint'] = ('full = every device slot taken; refused_today = new devices turned away today. To be told (or to free an '
                   'idle slot automatically) when that happens, offer create_monitor with monitor_kind slot_refused.')
    return out


def t_monitors(ctx, include_paused=True):
    from .fit_monitors import as_json
    from .models_fit import FitMonitor, FitMonitorEvent
    qs = FitMonitor.objects.filter(business=ctx.business)
    if not include_paused:
        qs = qs.filter(active=True)
    mons = [as_json(m) for m in qs[:30]]
    events = [{'monitor': e.monitor_id, 'at': when(e.created_at), 'title': e.title, 'action': e.action or None,
               'result': e.result or None, 'voucher': e.voucher_code or None, 'member_emailed': e.member_emailed or None}
              for e in FitMonitorEvent.objects.filter(business=ctx.business).order_by('-id')[:15]]
    return {'monitors': mons, 'recent_events': events, 'page': '/fit/monitors/'}


def t_sync_status(ctx, scope='voucher', codes='', batch='', plan='', router=''):
    from . import fit_sync
    try:
        out = fit_sync.status(ctx.business, scope=scope or 'voucher', codes=codes, batch=batch, plan=plan, router=router)
    except fit_sync.SyncError as exc:
        raise ToolError(str(exc))
    out['hint'] = ('To fix differences offer propose_action kind=sync with the same scope. Scopes: '
                   + '; '.join(f'{k} = {v}' for k, v in fit_sync.SCOPES.items()))
    return out


def t_monitor_test(ctx, monitor_id):
    from .fit_monitors import dry_run
    from .models_fit import FitMonitor
    m = FitMonitor.objects.filter(business=ctx.business, pk=monitor_id).select_related('business').first()
    if not m:
        raise ToolError(f'No monitor #{monitor_id} in this business.')
    return {'monitor': m.name, 'dry_run': dry_run(m), 'note': 'Nothing was sent or changed.'}


# ─────────────────────────────── registry ───────────────────────────────

def t_taptap_only(ctx):
    from .router_assign import overview
    ov = overview(ctx.business)
    routers = [{'name': r.name, 'link': r.connection_mode == 'agent'} for r in ctx.business.routers.order_by('name')]
    return {**ov, 'routers': routers, 'page': reverse('vouchers_to_router'),
            'hint': 'Offer propose_action kind=put_on_router with router and which=batch (+batch name) / codes (+codes) / '
                    'singles / everything. Ask the person which router if there are several and nothing tells you.'}


def _obj(props, required=()):
    return {'type': 'object', 'properties': props, 'required': list(required)}


S = {'type': 'string'}
PERIOD = {'type': 'string', 'enum': list(PERIODS), 'description': 'Time range; default 30d'}

TOOLS = {
    'overview': (None, t_overview, 'Quick health check of the business: vouchers, sales today, routers, open reports.', _obj({})),
    'finance_report': (FINANCE_PERMS, t_finance, 'Revenue, expenses, profit, margins, agent debts, month target and best plans for a period, '
                       'compared with the previous period of the same length.', _obj({'period': PERIOD})),
    'agent_sales': (AGENT_PERMS, t_agents, 'Sales per agent for a period vs the previous period, with drops, money owed and '
                    'collection rate. Flagged agents come first.', _obj({'period': PERIOD, 'agent': {**S, 'description': 'Optional name filter'}})),
    'find_voucher': (VOUCHER_PERMS, t_find_voucher, 'Look up a voucher/member by code, serial, customer name or phone: state, time left, '
                     'devices, diagnosis of why it does or does not work, history.', _obj({'query': S}, ['query'])),
    'customer_reports': (VOUCHER_PERMS, t_reports, 'Vouchers customers reported from the sign-in page, each re-diagnosed now.',
                         _obj({'status': {'type': 'string', 'enum': ['open', 'all']}})),
    'network_status': (NETWORK_PERMS, t_network_status, 'Routers (online, CPU, memory, sessions, errors), open session incidents and '
                       'devices that are offline right now.', _obj({})),
    'network_events': (None, t_network_events, 'What happened in the last N hours: outages, enforcement, sharing suspects, voucher events, '
                       'tests, sales and the activity log.', _obj({'hours': {'type': 'integer', 'description': '1-168, default 24'}})),
    'usage_top': (NETWORK_PERMS, t_usage, 'Data used in the last N hours: totals, heaviest users and apps.',
                  _obj({'hours': {'type': 'integer'}})),
    'find_device': (('network.manage', 'vouchers.support'), t_find_device, 'Find a device by MAC, IP, name or model: vouchers it used, '
                    'flags, blocked or not, which router.', _obj({'query': S}, ['query'])),
    'router_live': (NETWORK_PERMS, t_router_live, 'Read live data straight from a MikroTik (through TapTap Link or the API). Read only.',
                    _obj({'router': {**S, 'description': 'Router name; optional when there is only one'},
                          'what': {'type': 'string', 'enum': list(PULLS)},
                          'refresh': {'type': 'boolean', 'description': 'TapTap Link only: also start a fresh full pull'}}, ['what'])),
    'run_network_test': (NETWORK_PERMS, t_run_test, 'Run a test from a router now with live traffic: doctor = full internet check '
                         '(router→ISP→internet→DNS→web), ping, trace, dns, web (URL), speed.',
                         _obj({'router': S, 'kind': {'type': 'string', 'enum': ['doctor', 'ping', 'trace', 'dns', 'web', 'speed']},
                               'target': {**S, 'description': 'Host/IP for ping/trace/dns, URL for web'}}, ['kind'])),
    'test_result': (NETWORK_PERMS, t_test_result, 'Result of a network test started earlier.', _obj({'test_id': {'type': 'integer'}}, ['test_id'])),
    'team_chat_recent': (None, t_chat_recent, 'Latest messages in the team chat room (to summarise).', _obj({'limit': {'type': 'integer'}})),
    'slots_report': (VOUCHER_PERMS, t_slots_report, 'Device slots per plan right now: active vouchers/members, how many have every '
                     'slot full (with codes) and how many new devices were refused today. Optional plans: comma-separated names.',
                     _obj({'plans': S})),
    'monitors': (VOUCHER_PERMS + NETWORK_PERMS, t_monitors, 'The standing monitors (watches) of this business and what they '
                 'recently saw and did.', _obj({'include_paused': {'type': 'boolean'}})),
    'monitor_test': (VOUCHER_PERMS + NETWORK_PERMS, t_monitor_test, 'Dry-run one monitor: what it would catch right now. '
                     'Sends nothing, changes nothing.', _obj({'monitor_id': {'type': 'integer'}}, ['monitor_id'])),
    'sync_status': (VOUCHER_PERMS + NETWORK_PERMS, t_sync_status, 'Compare TapTap with the routers (read only): which of the '
                    'given vouchers / batch / plan / members differ from the router (missing, profile, time limit, on/off, last '
                    'send failed), plus each router\'s last sync and waiting/failed commands. scope: voucher, batch, plan, '
                    'members, unsynced, router.', _obj({'scope': S, 'codes': S, 'batch': S, 'plan': S, 'router': S})),
    'taptap_only': (VOUCHER_PERMS, t_taptap_only, 'Vouchers that exist only in TapTap (created "locally only", not on any '
                    'router yet): how many, by batch, plus the routers they could go on. To send them use propose_action '
                    'kind=put_on_router.', _obj({})),
    'help_guides': (None, t_help, 'Find TapTap how-to guides with links.', _obj({'query': S}, ['query'])),
    'router_query': (NETWORK_PERMS, t_router_query, 'Read straight from a MikroTik router (live over the API / TapTap Tunnel; '
                     'TapTap Link routers return the latest synced copy). Filter rows with where. Read only — to change '
                     'something use propose_action kind=router_change or router_task.',
                     _obj({'router': {**S, 'description': 'Router name (empty = the only router)'},
                           'menu': {'type': 'string', 'enum': None},
                           'where': {'type': 'array', 'items': {'type': 'object', 'properties': {
                               'field': S, 'op': {'type': 'string', 'enum': ['=', '!=', '~']}, 'value': S}}},
                           'limit': {'type': 'integer'}}, ['menu'])),
    'make_chart': (None, t_make_chart, 'Draw a chart in the chat. Pick a data source; TapTap computes the figures. '
                   'Use it whenever a picture explains better than a list (trends, shares, comparisons, live load).',
                   _obj({'source': {'type': 'string', 'enum': None}, 'type': {'type': 'string', 'enum': None},
                         'title': S, 'period': {'type': 'string', 'enum': list(PERIODS)},
                         'days': {'type': 'integer'}, 'hours': {'type': 'integer'}, 'router': S}, ['source'])),
    'create_dashboard': (None, t_create_dashboard, 'Save a live dashboard (a page of charts that refreshes itself) — or add '
                         'charts to an existing one with dashboard_id. Each widget is {source, type?, title?, period?, days?, hours?, router?}.',
                         _obj({'title': S, 'widgets': {'type': 'array', 'items': {'type': 'object', 'properties': {
                             'source': {'type': 'string'}, 'type': {'type': 'string'}, 'title': S, 'period': S,
                             'days': {'type': 'integer'}, 'hours': {'type': 'integer'}, 'router': S}, 'required': ['source']}},
                               'refresh_seconds': {'type': 'integer'}, 'dashboard_id': {'type': 'integer'}}, ['title', 'widgets'])),
    'dashboards': (None, t_dashboards, 'The saved live dashboards of this business.', _obj({})),
    'count_vouchers': (VOUCHER_PERMS, t_count_vouchers,
                       'How many vouchers/members match a filter (state, plan, batch, router, agent, codes, dates): totals by '
                       'state, online now, members, by router and plan, sample codes, and whether this person may change them '
                       'in bulk. Use before offering a group change. Read only.',
                       _obj({'state': {'type': 'string', 'description': 'frozen, warned, paused, disabled, in_use, online, expired, '
                                                                        'unused, stock, sold_unused or any'},
                             'plan': S, 'batch': S, 'router': S, 'agent': S, 'codes': S,
                             'members': {'type': 'string', 'enum': ['include', 'exclude', 'only']},
                             'created_after': S, 'created_before': S})),
}


def propose_spec(ctx):
    from .fit_actions import kinds_for
    from .fit_monitors import KINDS as MONITOR_KINDS
    from .fit_sync import SCOPES as SYNC_SCOPES
    kinds = kinds_for(ctx.perms)
    return _obj({
        'kind': {'type': 'string', 'enum': kinds},
        'code': {**S, 'description': 'Voucher code (most kinds)'},
        'minutes': {'type': 'integer', 'description': 'add_time / reduce_time'},
        'reason': {**S, 'description': 'Short reason kept in the history'},
        'message': {**S, 'description': 'warn_voucher: text the customer must agree to'},
        'mac': {**S, 'description': 'block_device / unblock_device'},
        'label': {**S, 'description': 'Device name for block/unblock'},
        'report_id': {'type': 'integer', 'description': 'resolve_report'},
        'note': {**S, 'description': 'resolve_report note / track_voucher note'},
        'to': {**S, 'description': 'send_chat: "team", "support" or a colleague first name'},
        'body': {**S, 'description': 'send_chat: the message text'},
        # monitors (create_monitor / pause_monitor / resume_monitor / delete_monitor) and free_idle_slot
        'monitor_kind': {'type': 'string', 'enum': list(MONITOR_KINDS),
                         'description': 'create_monitor: ' + '; '.join(f'{k} = {v[0]} ({v[1]})' for k, v in MONITOR_KINDS.items())},
        'name': {**S, 'description': 'create_monitor: short name'},
        'plans': {**S, 'description': 'create_monitor: comma-separated plan names (empty = all plans)'},
        'codes': {**S, 'description': 'create_monitor / put_on_router which=codes: comma-separated voucher/member codes'},
        'routers': {**S, 'description': 'create_monitor: comma-separated router names (empty = all)'},
        'members_only': {'type': 'boolean', 'description': 'create_monitor: only members'},
        'threshold': {'type': 'number', 'description': 'heavy_user Mbit/s · online_drop percent'},
        'days': {'type': 'integer', 'description': 'idle_slots: free slots unseen for this many days'},
        'min_users': {'type': 'integer', 'description': 'online_drop: ignore routers with fewer users at peak'},
        'min_idle_minutes': {'type': 'integer', 'description': 'free_slot / free_idle_slot: device must be unused this long (default 20)'},
        'once': {'type': 'boolean', 'description': 'voucher_online: stop after the first alert'},
        'response': {'type': 'string', 'enum': ['notify', 'free_slot', 'disconnect'],
                     'description': 'create_monitor: notify only; free_slot (slot_refused: free one idle slot so the new device '
                                    'gets in); disconnect (sharing: kick devices not locked to the voucher)'},
        'audience': {'type': 'string', 'enum': ['me', 'team'], 'description': 'create_monitor: who gets the in-app alert'},
        'email': {'type': 'boolean', 'description': 'create_monitor: also email the business notification recipients'},
        'notify_member': {'type': 'boolean', 'description': 'create_monitor: email the member/customer it is about'},
        'hours': {'type': 'integer', 'description': 'create_monitor: stop after this many hours (empty = until paused)'},
        'cooldown_minutes': {'type': 'integer', 'description': 'create_monitor: same device/voucher told once per this many minutes'},
        'monitor_id': {'type': 'integer', 'description': 'pause_monitor / resume_monitor / delete_monitor'},
        # sync
        'scope': {'type': 'string', 'enum': list(SYNC_SCOPES),
                  'description': 'sync: ' + '; '.join(f'{k} = {v}' for k, v in SYNC_SCOPES.items())},
        'batch': {**S, 'description': 'sync scope=batch / put_on_router which=batch: the batch name'},
        'which': {'type': 'string', 'enum': ['batch', 'codes', 'singles', 'everything'],
                  'description': 'put_on_router: which TapTap-only vouchers — a batch, some codes, the single (non-batch) ones, or everything waiting'},
        'plan': {**S, 'description': 'sync scope=plan: the plan name'},
        'router': {**S, 'description': 'sync: one router name (empty or "all" = every router) · put_on_router / router_change / router_task: the router'},
        # router control (core/fit_router)
        'menu': {'type': 'string', 'enum': list(_router_menus()), 'description': 'router_change: what to change'},
        'verb': {'type': 'string', 'enum': ['add', 'set', 'remove', 'enable', 'disable', 'make-static'],
                 'description': 'router_change: the action (each menu allows some of them)'},
        'where': {'type': 'array', 'description': 'router_change: which entries, e.g. [{"field":"user","op":"=","value":"ABC123"}]',
                  'items': {'type': 'object', 'properties': {'field': S, 'op': {'type': 'string', 'enum': ['=', '!=', '~']}, 'value': S}}},
        'values': {'type': 'array', 'description': 'router_change add/set: [{"field":"max-limit","value":"5M/5M"}]',
                   'items': {'type': 'object', 'properties': {'field': S, 'value': S}}},
        'task': {'type': 'string', 'enum': list(_router_tasks()), 'description': 'router_task: reboot · flush_dns · sync_clock'},
        **(_bulk_props() if 'bulk_vouchers' in kinds else {}),
    }, ['kind'])


def _bulk_props():
    from .fit_bulk import SPEC_PROPS
    return SPEC_PROPS


def _router_menus():
    from .fit_router import MENUS
    return MENUS


def _router_tasks():
    from .fit_router import TASKS
    return TASKS


def _fill_enums():
    from . import fit_charts, fit_router
    TOOLS['router_query'][3]['properties']['menu']['enum'] = list(fit_router.READ_MENUS)
    TOOLS['make_chart'][3]['properties']['source']['enum'] = list(fit_charts.SOURCES)
    TOOLS['make_chart'][3]['properties']['type']['enum'] = list(fit_charts.TYPES)


def specs(ctx):
    """OpenAI-style tool list for this person (only the tools their role can use)."""
    _fill_enums()
    out = []
    for name, (need, _, desc, schema) in TOOLS.items():
        if ctx.can(need):
            out.append({'type': 'function', 'function': {'name': name, 'description': desc, 'parameters': schema}})
    from .fit_actions import kinds_for
    if kinds_for(ctx.perms):
        out.append({'type': 'function', 'function': {
            'name': 'propose_action',
            'description': 'Offer a change for the person to approve (freeze/unfreeze/warn/disable/enable a voucher, add_time, '
                           'reduce_time, reset_devices, block_device, unblock_device, disconnect_session, resolve_report, '
                           'track_voucher, send_chat, free_idle_slot, create_monitor, pause_monitor, resume_monitor, '
                           'delete_monitor, sync, put_on_router, router_change, router_task, and — Owner/Admin only — bulk_vouchers for many '
                           'vouchers at once). It is NOT carried out: a card with “Take action” appears.',
            'parameters': propose_spec(ctx)}})
    return out


MAX_RESULT = 4500


def call(ctx, name, args):
    """Run one tool for the model. Always returns a JSON string (errors included) — never raises."""
    try:
        if name == 'propose_action':
            args = dict(args or {})
            res = t_propose(ctx, args.pop('kind', ''), **{k: v for k, v in args.items() if v not in (None, '')})
        else:
            spec = TOOLS.get(name)
            if not spec:
                raise ToolError(f'Unknown tool {name}.')
            need, fn, _, schema = spec
            if not ctx.can(need):
                raise ToolError('This person’s role cannot see that information.')
            allowed = set(schema['properties'])
            res = fn(ctx, **{k: v for k, v in (args or {}).items() if k in allowed})
    except (ToolError, ValueError) as exc:
        res = {'error': str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception('Fit tool %s failed', name)
        res = {'error': f'That lookup failed ({exc.__class__.__name__}).'}
    text = json.dumps(res, default=str, ensure_ascii=False)
    return text if len(text) <= MAX_RESULT else text[:MAX_RESULT] + '…(cut)'


# ─────────────────────────────── insights (no AI needed) ───────────────────────────────

def insights(ctx):
    """Short things worth a look, for Fit's speech bubble. Cheap queries, cached for 2 minutes."""
    key = f'fit:ins:{ctx.business.pk}:{abs(hash(tuple(sorted(ctx.perms)))) % 10 ** 8}'
    hit = cache.get(key)
    if hit is not None:
        return hit
    out = []
    b = ctx.business
    try:
        if ctx.can(VOUCHER_PERMS):
            from .models_voucher_reports import VoucherReport
            n = VoucherReport.objects.filter(business=b, status='open').count()
            if n:
                out.append({'level': 'warn', 'icon': 'report', 'text': f'{n} customer report{"s" if n != 1 else ""} waiting',
                            'ask': 'Go through the open customer reports and tell me how to fix each one.'})
        if ctx.can(NETWORK_PERMS):
            from .linkops import is_link
            for r in b.routers.select_related('agent')[:20]:
                off = False
                if is_link(r):
                    try:
                        off = not r.agent.online
                    except Exception:
                        off = True
                elif r.status and r.status.lower() not in ('connected', 'online', 'ok') and r.last_error:
                    off = True
                if off:
                    out.append({'level': 'bad', 'icon': 'router', 'text': f'{r.name} is not reachable',
                                'ask': f'Router {r.name} looks offline. What happened and what should I check?'})
            n = b.session_incidents.filter(status='open').count()
            if n:
                out.append({'level': 'warn', 'icon': 'shield', 'text': f'{n} session{"s" if n != 1 else ""} online that should not be',
                            'ask': 'Explain the open session incidents and what I should do.'})
        if ctx.can(AGENT_PERMS) and b.agents.filter(active=True).exists():
            _, rows = agent_rows(ctx, '7d')
            drops = [r for r in rows if r['_flag'] and 'sales' in r['flag']]
            if drops:
                r = drops[0]
                out.append({'level': 'warn', 'icon': 'agent', 'text': f'{r["agent"]}: {r["flag"]}'
                            + (f' ({r["change_pct"]:+.0f}%)' if r['change_pct'] is not None else ''),
                            'ask': f'Sales by agent {r["agent"]} dropped this week. Why might that be and what should I do?'})
        if ctx.can(FINANCE_PERMS) and b.monthly_revenue_target:
            from .finance import finance_summary, resolve_period
            f = finance_summary(b, resolve_period({'range': 'month'}))
            if f['target'] and f['projected'] < f['target'] * Decimal('0.9') and timezone.localdate().day >= 7:
                out.append({'level': 'info', 'icon': 'money', 'text': f'On track for {money(ctx, f["projected"])} of {money(ctx, f["target"])} this month',
                            'ask': 'I am behind my monthly revenue target. Look at my finances and agents and advise me.'})
    except Exception:
        logger.exception('Fit insights')
    cache.set(key, out, 120)
    return out
