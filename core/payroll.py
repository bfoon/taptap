"""Payroll engine — works out what each team member earns and has been paid.

How a month is worked out for one person (see models_payroll for the terms):

    basic    = amount × the part of the month they were on the payroll
    share    = percent × basis           (basis = net sales, or profit before staff pay)
    earned   = basic + share, capped at `cap`, and — once the month is over — at least `minimum`
    owed     = earned + bonuses − deductions
    balance  = owed − (salary + advance + bonus payments)

Profit for a % of profit deal is "profit before staff pay": net sales − agent
commission − expenses, leaving out every expense that came from a staff payment.
Otherwise one person's salary would shrink another person's profit share (and a
profit share would shrink itself). A negative profit gives no share.

For the month in progress the share is what has been earned so far; `projected`
extrapolates it to the end of the month at the current pace. The basic pay is
counted in full from day one: it is a commitment for the month, so Finance shows it
as "to pay this month".
"""
import calendar
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

ZERO = Decimal('0')
CENT = Decimal('0.01')


def d(value):
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def q2(value):
    return d(value).quantize(CENT, rounding=ROUND_HALF_UP)


# ───────────────────────────── months ─────────────────────────────
def month_start(day=None):
    day = day or timezone.localdate()
    return day.replace(day=1)


def next_month(m):
    return (m.replace(day=28) + timedelta(days=4)).replace(day=1)


def prev_month(m):
    return (m - timedelta(days=1)).replace(day=1)


def parse_month(value, default=None):
    """'2026-10' → date(2026, 10, 1). Future months fall back to this month."""
    this = month_start()
    try:
        m = datetime.strptime(str(value or ''), '%Y-%m').date()
    except ValueError:
        return default or this
    return min(m, this)


def _aware(day):
    return timezone.make_aware(datetime.combine(day, time.min))


def days_in(m):
    return calendar.monthrange(m.year, m.month)[1]


# ───────────────────────────── basis figures ─────────────────────────────
def basis_figures(business, start, end, site_id=None):
    """Net sales, commission, non-payroll expenses and profit before staff pay for [start, end)."""
    sales = business.sales.filter(sold_at__gte=start, sold_at__lt=end)
    exps = business.expenses.filter(paid_at__gte=start, paid_at__lt=end, staff_payout__isnull=True)
    if site_id:
        sales = sales.filter(router_id=site_id)
        exps = exps.filter(router_id=site_id)
    agg = sales.aggregate(v=Sum('amount'), c=Sum('commission'))
    rev, comm = d(agg['v']), d(agg['c'])
    exp = d(exps.aggregate(v=Sum('amount'))['v'])
    return {'sales': rev, 'commission': comm, 'expenses': exp, 'profit': rev - comm - exp}


class _BasisCache:
    """One query set per (site, window) however many people share it."""

    def __init__(self, business):
        self.business, self.store = business, {}

    def get(self, start, end, site_id):
        key = (site_id or 0, start, end)
        if key not in self.store:
            self.store[key] = basis_figures(self.business, start, end, site_id)
        return self.store[key]


# ───────────────────────────── one person, one month ─────────────────────────────
def describe(pay, currency='D'):
    """'D5,000 a month' · '10% of sales + D1,000 basic' · '15% of profit at Westfield (min D2,000)'."""
    from .templatetags.taptap_extras import money, pct
    if pay is None:
        return 'No pay set'
    if pay.pay_type == 'fixed':
        text = f'{money(pay.amount, currency)} a month'
    else:
        what = 'sales' if pay.pay_type == 'sales' else 'profit'
        text = f'{pct(pay.percent)}% of {what}'
        if pay.site_id:
            text += f' at {pay.site.name}'
        if d(pay.amount) > 0:
            text += f' + {money(pay.amount, currency)} basic'
    extra = []
    if d(pay.minimum) > 0 and pay.is_share:
        extra.append(f'min {money(pay.minimum, currency)}')
    if d(pay.cap) > 0:
        extra.append(f'max {money(pay.cap, currency)}')
    return text + (f' ({", ".join(extra)})' if extra else '')


def _clamp(value, minimum, cap, apply_minimum):
    value = d(value)
    if apply_minimum and d(minimum) > 0:
        value = max(value, d(minimum))
    if d(cap) > 0:
        value = min(value, d(cap))
    return value


def compute(pay, month, cache=None, today=None):
    """What `pay` earns for `month`. Returns a dict (all money as Decimal)."""
    today = today or timezone.localdate()
    m0 = month_start(month)
    m1 = next_month(m0)
    n_days = days_in(m0)
    blank = {
        'started': False, 'complete': m1 <= today, 'in_progress': m0 <= today < m1,
        'basic': ZERO, 'share': ZERO, 'share_projected': ZERO, 'earned': ZERO, 'projected': ZERO,
        'basis_label': '', 'basis_value': ZERO, 'basis_projected': ZERO, 'percent': ZERO,
        'figures': None, 'days_on': 0, 'days': n_days, 'elapsed': 0, 'capped': False, 'topped_up': False,
        'minimum': ZERO, 'cap': ZERO,
    }
    if pay is None or not pay.active or m0 > today:
        return blank

    eff = max(m0, pay.starts_on or m0)
    if eff >= m1:
        return blank

    cache = cache or _BasisCache(pay.business)
    complete = m1 <= today
    in_progress = not complete
    window_end = m1 if complete else min(m1, today + timedelta(days=1))
    days_on = (m1 - eff).days                        # days on the payroll this month
    elapsed = max(1, (window_end - eff).days)        # days of that window that have happened
    fraction = Decimal(days_on) / Decimal(n_days)

    basic = q2(d(pay.amount) * fraction)
    share = share_projected = basis_value = basis_projected = ZERO
    figures = None
    if pay.is_share:
        figures = cache.get(_aware(eff), _aware(window_end), pay.site_id)
        basis_value = figures['sales'] if pay.pay_type == 'sales' else max(ZERO, figures['profit'])
        share = q2(basis_value * d(pay.percent) / Decimal('100'))
        if in_progress and elapsed < days_on:
            stretch = Decimal(days_on) / Decimal(elapsed)
            basis_projected = q2(basis_value * stretch)
            share_projected = q2(share * stretch)
        else:
            basis_projected, share_projected = basis_value, share

    raw = basic + share
    raw_projected = basic + share_projected
    minimum = q2(d(pay.minimum) * fraction) if pay.is_share else ZERO
    earned = q2(_clamp(raw, minimum, pay.cap, complete))
    projected = q2(_clamp(raw_projected, minimum, pay.cap, True))
    return {
        'started': True, 'complete': complete, 'in_progress': in_progress,
        'basic': basic, 'share': share, 'share_projected': share_projected,
        'earned': earned, 'projected': projected,
        'basis_label': {'sales': 'Net sales', 'profit': 'Profit before staff pay'}.get(pay.pay_type, ''),
        'basis_value': basis_value, 'basis_projected': basis_projected, 'percent': d(pay.percent),
        'figures': figures, 'days_on': days_on, 'days': n_days, 'elapsed': min(elapsed, days_on),
        'capped': d(pay.cap) > 0 and raw > d(pay.cap),
        'topped_up': complete and minimum > 0 and raw < minimum,
        'minimum': minimum, 'cap': d(pay.cap),
    }


def calc_snapshot(pay, c, currency='D'):
    """A JSON-safe copy of the calculation, saved on each payment for the audit trail."""
    out = {'terms': describe(pay, currency) if pay else 'No pay set'}
    if pay:
        out.update({'pay_type': pay.pay_type, 'amount': str(pay.amount), 'percent': str(pay.percent),
                    'minimum': str(pay.minimum), 'cap': str(pay.cap), 'site': pay.site.name if pay.site_id else ''})
    for k in ('basic', 'share', 'earned', 'projected', 'basis_value'):
        out[k] = str(c.get(k, ZERO))
    out['basis_label'] = c.get('basis_label', '')
    out['complete'] = bool(c.get('complete'))
    return out


# ───────────────────────────── payments ─────────────────────────────
def _month_payouts(business, month):
    from .models_payroll import StaffPayout
    return StaffPayout.objects.filter(business=business, month=month_start(month))


def payout_totals(payouts):
    """Split a list of payouts into salary / advance / bonus / deduction totals."""
    t = defaultdict(lambda: ZERO)
    for p in payouts:
        t[p.kind] += d(p.amount)
    paid = t['salary'] + t['advance'] + t['bonus']
    return {'salary': t['salary'], 'advance': t['advance'], 'bonus': t['bonus'],
            'deduction': t['deduction'], 'paid': paid}


def member_month(member, month, cache=None, payouts=None):
    """Everything about one member's pay for one month."""
    # A missing reverse one-to-one raises RelatedObjectDoesNotExist, an AttributeError, so getattr works.
    pay = getattr(member, 'pay', None) if member is not None else None
    c = compute(pay, month, cache)
    if payouts is None:
        payouts = list(_month_payouts(member.business, month).filter(member=member)) if member else []
    t = payout_totals(payouts)
    owed = c['earned'] + t['bonus'] - t['deduction']
    owed_projected = c['projected'] + t['bonus'] - t['deduction']
    balance = owed - t['paid']
    target = owed_projected if c['in_progress'] else owed
    return {
        'member': member, 'month': month_start(month), 'pay': pay, 'calc': c, 'payouts': payouts, **t,
        'owed': owed, 'owed_projected': owed_projected, 'balance': balance,
        'balance_projected': owed_projected - t['paid'],
        'paid_pct': min(100, round(float(t['paid'] / target * 100))) if target > 0 else (100 if t['paid'] else 0),
        'status': ('ahead' if balance < 0 else 'paid' if balance == 0 and (t['paid'] or owed) else
                   'due' if balance > 0 else 'none'),
    }


def payroll_month(business, month, today=None):
    """The payroll for one month: a row per person plus totals.

    People appear when they have active pay terms, or when anything was paid to them that month
    (so someone removed from the team mid-month still shows what they were paid)."""
    from .models_team import TeamMember
    month = month_start(month)
    cache = _BasisCache(business)
    payouts = list(_month_payouts(business, month).select_related('member__user', 'expense'))
    by_member = defaultdict(list)
    orphans = defaultdict(list)
    for p in payouts:
        (by_member[p.member_id] if p.member_id else orphans[p.member_name]).append(p)

    members = (TeamMember.objects.filter(business=business)
               .select_related('user', 'pay', 'pay__site'))
    rows = []
    for m in members:
        has_pay = hasattr(m, 'pay') and m.pay.active
        if not has_pay and not by_member.get(m.pk):
            continue
        rows.append(member_month(m, month, cache, by_member.get(m.pk, [])))
    for name, plist in orphans.items():
        t = payout_totals(plist)
        rows.append({'member': None, 'name': name, 'pay': None, 'calc': compute(None, month), 'payouts': plist, **t,
                     'owed': t['bonus'] - t['deduction'], 'owed_projected': t['bonus'] - t['deduction'],
                     'balance': t['bonus'] - t['deduction'] - t['paid'], 'balance_projected': ZERO,
                     'paid_pct': 100, 'status': 'removed'})
    for r in rows:
        r.setdefault('name', r['member'].name if r['member'] else '')
    rows.sort(key=lambda r: (r['status'] == 'removed', -float(r['owed_projected']), r['name'].lower()))

    total = lambda key: sum((d(r[key]) for r in rows), ZERO)
    whole = cache.get(_aware(month), _aware(min(next_month(month), (today or timezone.localdate()) + timedelta(days=1))), None)
    owed, projected, paid = total('owed'), total('owed_projected'), total('paid')
    due = sum((r['balance'] for r in rows if r['balance'] > 0), ZERO)
    this = month_start(today)
    return {
        'month': month, 'is_current': month == this, 'prev': prev_month(month),
        'next': next_month(month) if month < this else None,
        'rows': rows, 'count': len(rows),
        'owed': owed, 'projected': projected, 'paid': paid, 'due': due,
        'advances': total('advance'), 'bonuses': total('bonus'), 'deductions': total('deduction'),
        'paid_pct': min(100, round(float(paid / projected * 100))) if projected > 0 else 0,
        'sales': whole['sales'], 'profit_before_pay': whole['profit'],
        'of_sales': round(float(projected / whole['sales'] * 100), 1) if whole['sales'] > 0 else None,
        'profit_after_pay': whole['profit'] - projected,
        'by_type': _by_type(rows),
        'payday': _next_payday(rows, month, today),
    }


def _by_type(rows):
    out = {'fixed': ZERO, 'sales': ZERO, 'profit': ZERO}
    for r in rows:
        if r['pay']:
            out[r['pay'].pay_type] += d(r['owed_projected'])
    return out


def _next_payday(rows, month, today=None):
    """The next pay day in this month that still has unpaid people, for the reminder banner."""
    today = today or timezone.localdate()
    if month != month_start(today):
        return None
    days = sorted({min(r['pay'].pay_day, days_in(month)) for r in rows
                   if r['pay'] and r['balance_projected'] > 0})
    for day in days:
        when = month.replace(day=day)
        if when >= today - timedelta(days=3):
            return {'date': when, 'in_days': (when - today).days,
                    'people': sum(1 for r in rows if r['pay'] and min(r['pay'].pay_day, days_in(month)) == day
                                  and r['balance_projected'] > 0)}
    return None


@transaction.atomic
def record_payout(business, member, month, kind, amount, *, method='cash', reference='', note='',
                  paid_at=None, user=None):
    """Book a salary / advance / bonus payment (with its Expense) or a deduction."""
    from .models import Expense
    from .models_payroll import StaffPayout, CASH_KINDS, PAYOUT_KINDS
    month = month_start(month)
    amount = q2(amount)
    paid_at = paid_at or timezone.now()
    try:
        pay = member.pay
    except Exception:
        pay = None
    c = compute(pay, month)
    label = dict(PAYOUT_KINDS).get(kind, 'Salary')
    expense = None
    if kind in CASH_KINDS:
        expense = Expense.objects.create(
            business=business, category='salaries',
            description=f'{label} — {member.name} ({month:%B %Y})'[:200],
            vendor=member.name[:120], amount=amount, payment_method=method,
            router=pay.site if pay and pay.site_id else None,
            reference=reference[:120], recurring=False, paid_at=paid_at, recorded_by=user,
        )
    return StaffPayout.objects.create(
        business=business, member=member, member_name=member.name[:150], month=month, kind=kind,
        amount=amount, method=method, reference=reference[:120], note=note[:255], paid_at=paid_at,
        calc=calc_snapshot(pay, c, business.currency), expense=expense, recorded_by=user,
    )


@transaction.atomic
def void_payout(payout):
    """Remove a payment; its Expense goes with it so Finance stays in step."""
    if payout.expense_id:
        payout.expense.delete()          # CASCADE removes the payout
    else:
        payout.delete()


# ───────────────────────────── live preview for the pay form ─────────────────────────────
def preview_figures(business, today=None):
    """Sales and profit-before-staff-pay for last month and this month so far, whole business and per site.

    The pay form uses this to show 'last month this deal would have paid …' while you type."""
    today = today or timezone.localdate()
    this = month_start(today)
    last = prev_month(this)
    windows = {'last': (last, this), 'this': (this, today + timedelta(days=1))}
    out = {'_': {}, 'days': days_in(this), 'elapsed': today.day, 'last_days': days_in(last),
           'last_label': f'{last:%B}', 'this_label': f'{this:%B}'}
    sites = defaultdict(dict)
    for key, (a, b) in windows.items():
        start, end = _aware(a), _aware(b)
        sales = business.sales.filter(sold_at__gte=start, sold_at__lt=end)
        exps = business.expenses.filter(paid_at__gte=start, paid_at__lt=end, staff_payout__isnull=True)
        per_s = {r['router']: (d(r['v']), d(r['c'])) for r in sales.values('router').annotate(v=Sum('amount'), c=Sum('commission'))}
        per_e = {r['router']: d(r['v']) for r in exps.values('router').annotate(v=Sum('amount'))}
        tot_s = sum((v for v, _ in per_s.values()), ZERO)
        tot_c = sum((c for _, c in per_s.values()), ZERO)
        tot_e = sum(per_e.values(), ZERO)
        out['_'][key] = {'sales': float(tot_s), 'profit': float(tot_s - tot_c - tot_e)}
        for rid in set(per_s) | set(per_e):
            if not rid:
                continue
            v, c = per_s.get(rid, (ZERO, ZERO))
            sites[str(rid)][key] = {'sales': float(v), 'profit': float(v - c - per_e.get(rid, ZERO))}
    for rid, data in sites.items():
        data.setdefault('last', {'sales': 0, 'profit': 0})
        data.setdefault('this', {'sales': 0, 'profit': 0})
        out[rid] = data
    return out


# ───────────────────────────── finance hooks ─────────────────────────────
def single_month_of(period):
    """The calendar month a Finance period covers exactly, else None."""
    start = timezone.localtime(period.start).date()
    end = timezone.localtime(period.end).date()
    if start.day != 1:
        return None
    if period.preset in ('month', 'last_month', 'bymonth'):
        return start
    return start if end == next_month(start) else None


def member_history(member, months=6):
    """The last few months for one person — used on their own 'My pay' page."""
    this = month_start()
    cache = _BasisCache(member.business)
    out, m = [], this
    for _ in range(months):
        if member.created_at and m < month_start(timezone.localtime(member.created_at).date()) and not \
                member.payouts.filter(month=m).exists():
            break
        out.append(member_month(member, m, cache))
        m = prev_month(m)
    return out
