"""Fit tables — saved, filtered tables Fit builds on request ("a table of vouchers older than a month").

A table is a saved question, not a copy of data: it keeps its source, its filters, its columns and its sort
(models_fit.FitTable), and every time it is opened TapTap runs it again on the live data. Relative filters
("older than 30 days") stay relative, so the table is always right.

SOURCES
    vouchers  vouchers and members: state, plan, batch, router, agent, age, expiry, search …
    sales     voucher sales: last N days, plan, agent, router, payment method, minimum amount
    routers   every router: online or not, people online, CPU, mode, last contact
    reports   customer reports from the sign-in page

Where you see it
    * in Fit's chat: a small card with the first rows and the total — click it to open the table
    * its own page /fit/tables/<id>/: change the filters live, pick the columns, sort by any column, search, page
      through, download CSV, click the summary chips to narrow it down, save the filters or save as a new table, pin it
      to Fit's workspace, ask Fit about it
    * the phone app: Fit → workspace → the table, or tap the card in Fit's chat

Privacy: a table is opened with the rights of the person who opens it, not of the one who made it — someone who
cannot see sales never sees a sales table. A private table is seen only by the person who made it.
"""
from __future__ import annotations

import csv
import io
from collections import Counter, OrderedDict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

MAX_ROWS = 20000                 # rows looked at for one table (sorted in memory)
PER_PAGE = 50
PREVIEW = 6

VOUCHER_PERMS = ('vouchers.view', 'vouchers.support', 'vouchers.create')
SALES_PERMS = ('finance.view', 'sales.daily', 'dashboard.view')
NETWORK_PERMS = ('network.manage',)


class TableError(ValueError):
    pass


# column: (label, kind) — kind: text · date · money · num · state
SOURCES = OrderedDict([
    ('vouchers', {
        'label': 'Vouchers', 'icon': '🎟️', 'perms': VOUCHER_PERMS,
        'columns': OrderedDict([
            ('code', ('Code', 'text')), ('state', ('State', 'state')), ('plan', ('Plan', 'text')), ('kind', ('Type', 'text')),
            ('price', ('Price', 'money')), ('batch', ('Batch', 'text')), ('router', ('Router', 'text')), ('agent', ('Agent', 'text')),
            ('created', ('Created', 'date')), ('age_days', ('Age (days)', 'num')), ('sold', ('Sold', 'date')),
            ('first_used', ('First used', 'date')), ('expires', ('Ends', 'date')), ('devices', ('Devices', 'num')),
            ('customer', ('Customer', 'text')), ('phone', ('Phone', 'text')), ('serial', ('Serial', 'text')), ('note', ('Note', 'text')),
        ]),
        'default': ['code', 'state', 'plan', 'router', 'created', 'age_days', 'first_used', 'expires'],
        'sort': ('created', 'asc'),
        'filters': OrderedDict([
            ('state', 'frozen, warned, paused, disabled, in_use, online, expired, unused, stock, sold_unused or any'),
            ('plan', 'plan name(s), comma-separated'), ('batch', 'batch name(s)'), ('router', 'router name(s)'), ('agent', 'agent name'),
            ('members', 'include / exclude / only'), ('older_than_days', 'created more than N days ago'),
            ('newer_than_days', 'created in the last N days'), ('created_after', 'YYYY-MM-DD'), ('created_before', 'YYYY-MM-DD'),
            ('ends_within_days', 'time ends in the next N days'), ('search', 'code, serial, customer, phone or note contains'),
        ]),
    }),
    ('sales', {
        'label': 'Sales', 'icon': '💰', 'perms': SALES_PERMS,
        'columns': OrderedDict([
            ('sold', ('Sold', 'date')), ('code', ('Voucher', 'text')), ('plan', ('Plan', 'text')), ('amount', ('Amount', 'money')),
            ('discount', ('Discount', 'money')), ('commission', ('Commission', 'money')), ('method', ('Paid by', 'text')),
            ('agent', ('Agent', 'text')), ('router', ('Router', 'text')), ('customer', ('Customer', 'text')), ('phone', ('Phone', 'text')),
            ('reference', ('Reference', 'text')),
        ]),
        'default': ['sold', 'code', 'plan', 'amount', 'method', 'agent', 'router'],
        'sort': ('sold', 'desc'),
        'filters': OrderedDict([
            ('days', 'last N days (default 30)'), ('created_after', 'YYYY-MM-DD'), ('created_before', 'YYYY-MM-DD'),
            ('plan', 'plan name(s)'), ('agent', 'agent name'), ('router', 'router name(s)'),
            ('method', 'cash, wave, qmoney, afrimoney, bank, card, auto, other'), ('min_amount', 'at least this amount'),
            ('search', 'voucher, customer, phone or reference contains'),
        ]),
    }),
    ('routers', {
        'label': 'Routers', 'icon': '📡', 'perms': NETWORK_PERMS,
        'columns': OrderedDict([
            ('name', ('Router', 'text')), ('online', ('Online', 'state')), ('people', ('People online', 'num')),
            ('cpu', ('CPU %', 'num')), ('mode', ('Connection', 'text')), ('last_contact', ('Last contact', 'date')),
            ('uptime', ('Uptime', 'text')), ('ros', ('RouterOS', 'text')), ('error', ('Last error', 'text')),
        ]),
        'default': ['name', 'online', 'people', 'cpu', 'mode', 'last_contact'],
        'sort': ('name', 'asc'),
        'filters': OrderedDict([('status', 'online / offline / any'), ('search', 'name contains')]),
    }),
    ('reports', {
        'label': 'Customer reports', 'icon': '📣', 'perms': VOUCHER_PERMS,
        'columns': OrderedDict([
            ('created', ('Sent', 'date')), ('code', ('Voucher', 'text')), ('status', ('Status', 'state')), ('name', ('Customer', 'text')),
            ('phone', ('Phone', 'text')), ('message', ('Message', 'text')), ('shown_error', ('Portal said', 'text')),
            ('note', ('Staff note', 'text')),
        ]),
        'default': ['created', 'code', 'status', 'name', 'phone', 'message'],
        'sort': ('created', 'desc'),
        'filters': OrderedDict([('status', 'open / resolved / any (default open)'), ('days', 'last N days'),
                                ('search', 'code, name, phone or message contains')]),
    }),
])


def can(perms, source):
    need = SOURCES[source]['perms']
    return any(p in perms for p in need)


# ─────────────────────────────── cleaning what Fit (or the page) sent ───────────────────────────────

def _s(v):
    return str(v if v is not None else '').strip()


def _names(v):
    if isinstance(v, (list, tuple)):
        return [_s(x) for x in v if _s(x)]
    return [x.strip() for x in _s(v).replace(';', ',').split(',') if x.strip()]


def _int(v, name, lo=0, hi=36500):
    if v in (None, ''):
        return None
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        raise TableError(f'{name} must be a number.')
    if not lo <= n <= hi:
        raise TableError(f'{name} must be between {lo} and {hi}.')
    return n


def _day(v, name):
    v = _s(v)
    if not v:
        return ''
    try:
        return datetime.strptime(v[:10], '%Y-%m-%d').date().isoformat()
    except ValueError:
        raise TableError(f'{name} must look like 2026-10-22.')


def clean_filters(business, source, raw):
    """Only the filters this source knows, checked against this business. Relative ones stay relative."""
    from .fit_bulk import STATES
    if source not in SOURCES:
        raise TableError('Which data? ' + ', '.join(SOURCES))
    raw = raw or {}
    out = {}
    known = SOURCES[source]['filters']
    for k in known:
        v = raw.get(k)
        if v in (None, '', [], 'any') and k not in ('members',):
            continue
        if k == 'state':
            v = _s(v).lower()
            if v not in STATES:
                raise TableError(f'state is one of: {", ".join(STATES)}.')
            out[k] = v
        elif k == 'members':
            v = _s(v).lower()
            if v in ('exclude', 'only'):
                out[k] = v
        elif k in ('older_than_days', 'newer_than_days', 'ends_within_days', 'days'):
            n = _int(v, k, 0)
            if n is not None:
                out[k] = n
        elif k in ('created_after', 'created_before'):
            d = _day(v, k)
            if d:
                out[k] = d
        elif k == 'min_amount':
            try:
                out[k] = float(_s(v).replace(',', ''))
            except ValueError:
                raise TableError('min_amount must be a number.')
        elif k == 'plan':
            plans = _names(v)
            have = {p.lower(): p for p in business.vouchers.exclude(plan_name='').values_list('plan_name', flat=True).distinct()[:3000]}
            have.update({p.lower(): p for p in business.plans.values_list('name', flat=True)})
            bad = [p for p in plans if p.lower() not in have]
            if bad:
                raise TableError(f'No plan {", ".join(bad)}. Plans: {", ".join(sorted(set(have.values()))[:20])}.')
            out[k] = [have[p.lower()] for p in plans]
        elif k == 'batch':
            got = []
            for n in _names(v):
                b = business.batches.filter(name__iexact=n).first()
                if b is None:
                    raise TableError(f'There is no batch “{n}”.')
                got.append(b.name)
            out[k] = got
        elif k == 'router':
            got = []
            for n in _names(v):
                if n.lower() in ('all', 'any', 'every'):
                    continue
                r = business.routers.filter(pk=int(n)).first() if n.isdigit() else business.routers.filter(name__iexact=n).first()
                if r is None:
                    raise TableError(f'There is no router “{n}”. Routers: {", ".join(business.routers.values_list("name", flat=True)[:12])}.')
                got.append(r.name)
            if got:
                out[k] = got
        elif k == 'agent':
            a = business.agents.filter(name__iexact=_s(v)).first()
            if a is None:
                raise TableError(f'There is no agent “{_s(v)}”.')
            out[k] = a.name
        elif k == 'status':
            v = _s(v).lower()
            ok = ('online', 'offline') if source == 'routers' else ('open', 'resolved', 'any')
            if v not in ok:
                raise TableError(f'status is one of: {", ".join(ok)}.')
            out[k] = v
        elif k == 'method':
            from .models import PAYMENT_METHODS
            m = _s(v).lower()
            if m not in dict(PAYMENT_METHODS):
                raise TableError('method is one of: ' + ', '.join(dict(PAYMENT_METHODS)))
            out[k] = m
        else:
            out[k] = _s(v)[:80]
    return out


def clean_columns(source, cols):
    have = SOURCES[source]['columns']
    cols = [c for c in _names(cols) if c in have]
    return cols or list(SOURCES[source]['default'])


def clean_sort(source, by, direction):
    have = SOURCES[source]['columns']
    d_by, d_dir = SOURCES[source]['sort']
    by = by if by in have else d_by
    direction = 'desc' if _s(direction).lower() == 'desc' else ('asc' if _s(direction).lower() == 'asc' else (d_dir if by == d_by else 'asc'))
    return {'by': by, 'dir': direction}


# ─────────────────────────────── reading the data ───────────────────────────────

def _start(day_iso, end=False):
    d = datetime.strptime(day_iso, '%Y-%m-%d').date()
    return timezone.make_aware(datetime.combine(d, time.max if end else time.min))


def _cut(days):
    return timezone.now() - timedelta(days=days)


def _vouchers(business, f, now):
    from .fit_bulk import select
    from .voucher_history import display_state
    sel = {'state': f.get('state') or 'any', 'plans': f.get('plan') or [], 'batches': [], 'routers': [], 'codes': [],
           'agent': '', 'members': f.get('members') or 'include', 'created_after': f.get('created_after', ''),
           'created_before': f.get('created_before', '')}
    if f.get('batch'):
        sel['batches'] = list(business.batches.filter(name__in=f['batch']).values_list('pk', flat=True)) or [-1]
    if f.get('router'):
        sel['routers'] = list(business.routers.filter(name__in=f['router']).values_list('pk', flat=True)) or [-1]
    if f.get('agent'):
        a = business.agents.filter(name__iexact=f['agent']).first()
        sel['agent'] = a.pk if a else -1
    rows, complete = select(business, sel, now)
    if f.get('older_than_days') is not None:
        cut = _cut(f['older_than_days'])
        rows = [v for v in rows if v.created_at and v.created_at < cut]
    if f.get('newer_than_days') is not None:
        cut = _cut(f['newer_than_days'])
        rows = [v for v in rows if v.created_at and v.created_at >= cut]
    if f.get('ends_within_days') is not None:
        end = now + timedelta(days=f['ends_within_days'])
        rows = [v for v in rows if v.expires_at and now <= v.expires_at <= end]
    q = _s(f.get('search')).lower()
    if q:
        rows = [v for v in rows if q in ' '.join([v.code, v.serial or '', v.customer_name or '', v.customer_phone or '', v.note or '']).lower()]
    ids = [v.pk for v in rows]
    batch_names = dict(business.batches.values_list('pk', 'name'))
    agents = dict(business.agents.values_list('pk', 'name'))
    try:
        from .models import VoucherDeviceBinding
        from django.db.models import Count
        devs = dict(VoucherDeviceBinding.objects.filter(business=business).values('voucher_id').annotate(n=Count('id'))
                    .values_list('voucher_id', 'n')) if ids else {}
    except Exception:
        devs = {}
    out = []
    for v in rows:
        key, label = display_state(v, now)
        out.append({
            'code': v.code, 'state': label, '_state': key, 'plan': v.plan_name or '—',
            'kind': 'Member' if v.login_type == 'member' else 'Voucher', 'price': v.price, 'batch': batch_names.get(v.batch_id, ''),
            'router': v.router.name if v.router_id and v.router else '', 'agent': agents.get(v.agent_id, ''),
            'created': v.created_at, 'age_days': (now - v.created_at).days if v.created_at else None, 'sold': v.sold_at,
            'first_used': v.used_at, 'expires': v.expires_at, 'devices': devs.get(v.pk, 0), 'customer': v.customer_name,
            'phone': v.customer_phone, 'serial': v.serial, 'note': v.note, '_link': f'/vouchers/{v.pk}/', '_id': v.pk,
        })
    return out, complete


def _sales(business, f, now):
    qs = business.sales.select_related('router', 'agent')
    if f.get('created_after') or f.get('created_before'):
        if f.get('created_after'):
            qs = qs.filter(sold_at__gte=_start(f['created_after']))
        if f.get('created_before'):
            qs = qs.filter(sold_at__lte=_start(f['created_before'], end=True))
    else:
        qs = qs.filter(sold_at__gte=_cut(f.get('days') or 30))
    if f.get('plan'):
        qs = qs.filter(plan_name__in=f['plan'])
    if f.get('agent'):
        qs = qs.filter(agent__name__iexact=f['agent'])
    if f.get('router'):
        qs = qs.filter(router__name__in=f['router'])
    if f.get('method'):
        qs = qs.filter(payment_method=f['method'])
    if f.get('min_amount') is not None:
        qs = qs.filter(amount__gte=Decimal(str(f['min_amount'])))
    q = _s(f.get('search'))
    if q:
        qs = qs.filter(Q(voucher_code__icontains=q) | Q(customer_name__icontains=q) | Q(customer_phone__icontains=q) | Q(reference__icontains=q))
    rows = list(qs.order_by('-sold_at')[:MAX_ROWS + 1])
    complete = len(rows) <= MAX_ROWS
    out = []
    for s in rows[:MAX_ROWS]:
        out.append({
            'sold': s.sold_at, 'code': s.voucher_code, 'plan': s.plan_name, 'amount': s.amount, 'discount': s.discount,
            'commission': s.commission, 'method': s.get_payment_method_display(), 'agent': s.agent.name if s.agent_id and s.agent else '',
            'router': s.router.name if s.router_id and s.router else '', 'customer': s.customer_name, 'phone': s.customer_phone,
            'reference': s.reference, '_link': f'/vouchers/{s.voucher_id}/' if s.voucher_id else '', '_id': s.pk,
        })
    return out, complete


def _routers(business, f, now):
    from .router_schedules import is_online, last_contact, online_count
    out = []
    for r in business.routers.select_related('agent').order_by('name')[:500]:
        on = is_online(r)
        if f.get('status') == 'online' and not on or f.get('status') == 'offline' and on:
            continue
        if _s(f.get('search')).lower() and _s(f.get('search')).lower() not in r.name.lower():
            continue
        cpu = uptime = ros = None
        if r.connection_mode == 'agent':
            try:
                cpu, uptime, ros = r.agent.cpu_load, r.agent.uptime, r.agent.ros_version
            except Exception:
                pass
        out.append({'name': r.name, 'online': 'Online' if on else 'Offline', '_state': 'online' if on else 'offline',
                    'people': online_count(r), 'cpu': cpu, 'mode': 'TapTap Link' if r.connection_mode == 'agent' else 'Direct API',
                    'last_contact': last_contact(r), 'uptime': uptime or '', 'ros': ros or '', 'error': (r.last_error or '')[:160],
                    '_link': f'/routers/{r.pk}/control/', '_id': r.pk})
    return out, True


def _reports(business, f, now):
    from .models_voucher_reports import VoucherReport
    qs = VoucherReport.objects.filter(business=business)
    st = f.get('status') or 'open'
    if st != 'any':
        qs = qs.filter(status=st)
    if f.get('days'):
        qs = qs.filter(created_at__gte=_cut(f['days']))
    q = _s(f.get('search'))
    if q:
        qs = qs.filter(Q(code__icontains=q) | Q(name__icontains=q) | Q(phone__icontains=q) | Q(message__icontains=q))
    rows = list(qs.order_by('-created_at')[:MAX_ROWS])
    return [{'created': x.created_at, 'code': x.code, 'status': x.get_status_display(), '_state': x.status, 'name': x.name,
             'phone': x.phone, 'message': x.message, 'shown_error': x.shown_error, 'note': x.staff_note,
             '_link': f'/vouchers/{x.voucher_id}/' if x.voucher_id else '/vouchers/reports/', '_id': x.pk} for x in rows], True


READERS = {'vouchers': _vouchers, 'sales': _sales, 'routers': _routers, 'reports': _reports}


def _key(v):
    if v is None or v == '':
        return (1, '')
    if isinstance(v, datetime):
        return (0, v.timestamp())
    if isinstance(v, (int, float, Decimal)):
        return (0, float(v))
    return (0, str(v).lower())


def cell(v, kind, currency=''):
    """What a cell shows (text)."""
    if v is None or v == '':
        return ''
    if kind == 'date' and isinstance(v, datetime):
        return timezone.localtime(v).strftime('%d %b %Y %H:%M')
    if kind == 'money':
        n = float(v)
        return f'{currency}{n:,.0f}' if n == int(n) else f'{currency}{n:,.2f}'
    return str(v)


def summary(source, rows, currency=''):
    """Chips under the title: each one narrows the table when clicked ({label, value, filter})."""
    chips = []
    if source == 'vouchers':
        c = Counter(r['_state'] for r in rows)
        names = {'frozen': 'Frozen', 'warned': 'Warned', 'disabled': 'Disabled', 'expired': 'Time ran out', 'used': 'In use',
                 'unused': 'Not used yet', 'archived': 'Archived'}
        filt = {'frozen': 'frozen', 'warned': 'warned', 'disabled': 'disabled', 'expired': 'expired', 'used': 'in_use', 'unused': 'unused'}
        for k, n in c.most_common(6):
            chips.append({'label': names.get(k, k.title()), 'value': n, 'filter': {'state': filt[k]} if k in filt else None})
        plans = Counter(r['plan'] for r in rows).most_common(3)
        for p, n in plans:
            if p and p != '—':
                chips.append({'label': p, 'value': n, 'filter': {'plan': p}})
        money = sum(float(r['price'] or 0) for r in rows)
        if money:
            chips.append({'label': 'Face value', 'value': cell(money, 'money', currency), 'filter': None})
    elif source == 'sales':
        total = sum(float(r['amount'] or 0) for r in rows)
        chips.append({'label': 'Total', 'value': cell(total, 'money', currency), 'filter': None})
        if rows:
            chips.append({'label': 'Average', 'value': cell(total / len(rows), 'money', currency), 'filter': None})
        for p, n in Counter(r['plan'] for r in rows).most_common(3):
            chips.append({'label': p, 'value': n, 'filter': {'plan': p}})
    elif source == 'routers':
        c = Counter(r['_state'] for r in rows)
        chips += [{'label': 'Online', 'value': c.get('online', 0), 'filter': {'status': 'online'}},
                  {'label': 'Offline', 'value': c.get('offline', 0), 'filter': {'status': 'offline'}}]
        ppl = sum(r['people'] or 0 for r in rows)
        chips.append({'label': 'People online', 'value': ppl, 'filter': None})
    elif source == 'reports':
        c = Counter(r['_state'] for r in rows)
        for k, n in c.most_common(3):
            chips.append({'label': k.title(), 'value': n, 'filter': {'status': k}})
    return chips


def run(business, source, filters, columns=None, sort=None, page=1, per_page=PER_PAGE, now=None):
    """The table now: {'columns': [{key,label,kind}], 'rows': [{'cells': [...], 'link'}], 'total', 'page', 'pages', 'chips'}."""
    now = now or timezone.now()
    spec = SOURCES[source]
    cols = clean_columns(source, columns)
    srt = sort if isinstance(sort, dict) and sort.get('by') in spec['columns'] else clean_sort(source, '', '')
    rows, complete = READERS[source](business, filters or {}, now)
    rows.sort(key=lambda r: _key(r.get(srt['by'])), reverse=srt['dir'] == 'desc')
    if srt['dir'] == 'desc':                     # empty cells last either way
        rows.sort(key=lambda r: r.get(srt['by']) in (None, ''))
    cur = getattr(business, 'currency', '') or ''
    total = len(rows)
    per_page = max(1, min(500, int(per_page or PER_PAGE)))
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(pages, int(page or 1)))
    chunk = rows[(page - 1) * per_page: page * per_page]
    return {
        'source': source, 'source_label': spec['label'], 'icon': spec['icon'],
        'columns': [{'key': c, 'label': spec['columns'][c][0], 'kind': spec['columns'][c][1]} for c in cols],
        'rows': [{'cells': [cell(r.get(c), spec['columns'][c][1], cur) for c in cols], 'link': r.get('_link') or '',
                  'state': r.get('_state') or '', 'id': r.get('_id')} for r in chunk],
        'total': total, 'complete': complete, 'page': page, 'pages': pages, 'per_page': per_page, 'sort': srt,
        'chips': summary(source, rows, cur), '_all': rows, '_cols': cols,
    }


def csv_bytes(business, source, filters, columns, sort):
    data = run(business, source, filters, columns, sort, page=1, per_page=500)
    spec = SOURCES[source]
    cur = getattr(business, 'currency', '') or ''
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c['label'] for c in data['columns']])
    for r in data['_all']:
        w.writerow([cell(r.get(c), spec['columns'][c][1], cur) for c in data['_cols']])
    return ('﻿' + buf.getvalue()).encode('utf-8')


def describe_filters(source, f):
    """Human chips for the saved filters: ['State: frozen', 'Older than 30 days', …]."""
    labels = {'older_than_days': 'Older than {} days', 'newer_than_days': 'Last {} days', 'ends_within_days': 'Ends within {} days',
              'days': 'Last {} days', 'min_amount': 'At least {}'}
    out = []
    for k, v in (f or {}).items():
        if k in labels:
            out.append(labels[k].format(v))
        elif isinstance(v, list):
            out.append(f'{k.replace("_", " ").title()}: {", ".join(map(str, v))}')
        else:
            out.append(f'{k.replace("_", " ").title()}: {v}')
    return out or ['Everything']


# ─────────────────────────────── saved tables ───────────────────────────────

def visible(user, business, perms):
    from .models_fit import FitTable
    qs = FitTable.objects.filter(business=business).filter(Q(shared=True) | Q(created_by=user)).select_related('created_by')
    return [t for t in qs.order_by('-pinned', '-updated_at') if can(perms, t.source)]


def card(t, data=None, business=None):
    """The chat card: title, total, filter chips, the first rows — the whole card opens the table."""
    data = data or run(t.business if business is None else business, t.source, t.filters, t.columns, t.sort, per_page=PREVIEW)
    return {'id': t.pk, 'title': t.title, 'link': f'/fit/tables/{t.pk}/', 'source': t.source, 'source_label': data['source_label'],
            'icon': data['icon'], 'total': data['total'], 'filters': describe_filters(t.source, t.filters),
            'columns': [c['label'] for c in data['columns']][:5],
            'rows': [{'cells': r['cells'][:5], 'link': r['link']} for r in data['rows'][:PREVIEW]],
            'chips': [{'label': c['label'], 'value': c['value']} for c in data['chips'][:4]]}


def as_json(t, user=None):
    return {'id': t.pk, 'title': t.title, 'source': t.source, 'source_label': SOURCES[t.source]['label'], 'icon': SOURCES[t.source]['icon'],
            'filters': describe_filters(t.source, t.filters), 'pinned': t.pinned, 'shared': t.shared, 'link': f'/fit/tables/{t.pk}/',
            'by': (t.created_by.get_full_name() or t.created_by.first_name or t.created_by.email) if t.created_by else '',
            'mine': bool(user and t.created_by_id == user.pk), 'updated_at': timezone.localtime(t.updated_at).isoformat()}
