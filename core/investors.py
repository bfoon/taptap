"""Investors — terms engine, daily accruals, projections, the investor portal and the deal lifecycle.

Money rule: every figure an investor sees comes from ``step()``. Accruals, today's live figure, projections and
the opportunity calculator all run the same function, so the portal, the daily email, Fit and the owner pages
can never disagree.

Profit of a scope = sales (amount − agent commission) − expenses, exactly like Finance (core/finance.py).
A "sites" scope counts only sales and expenses booked on those routers.

Lifecycle of a deal (``Investment.status``)::

    offered ──accept──▶ awaiting_payment ──investor "I paid"──▶ payment_review ──owner confirms──▶ active ──▶ completed
       └─reject─▶ rejected                     └── hosted checkout webhook (amount verified) ──────────────┘
    (the owner can also add a deal directly as active, even with a past start date: it is back-filled)
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import secrets
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import wraps

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q, Sum
from django.db.models.functions import TruncDay
from django.urls import path, reverse
from django.utils import timezone

logger = logging.getLogger('taptap.investors')

ZERO = Decimal('0')
CENT = Decimal('0.01')
MAGIC_MINUTES = 30
SUMMARY_LINK_HOURS = 12
SESSION_SECONDS = 12 * 3600
SESSION_ID, SESSION_AV = 'tt_investor_id', 'tt_investor_av'
BACKFILL_MAX_DAYS = 3 * 366
SCENARIOS = (('cautious', Decimal('0.75')), ('expected', Decimal('1')), ('strong', Decimal('1.25')))
FUNDED_STATES = ('awaiting_payment', 'payment_review', 'active', 'completed')
_INSTALLED = False


def q(v):
    return (v if isinstance(v, Decimal) else Decimal(str(v or 0))).quantize(CENT, rounding=ROUND_HALF_UP)


def dec(v, default=None):
    try:
        text = str(v).strip().replace(',', '')
        return Decimal(text) if text != '' else default
    except Exception:
        return default


def new_reference(prefix):
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return f'{prefix}-' + ''.join(secrets.choice(alphabet) for _ in range(7))


def settings_for(business):
    from .models_investors import InvestorSettings
    return InvestorSettings.objects.get_or_create(business=business)[0]


def site_url(request=None, route=''):
    site = (getattr(settings, 'SITE_URL', '') or '').rstrip('/')
    if site:
        return f'{site}{route}'
    return request.build_absolute_uri(route) if request else route


def log_event(investor, event, text, investment=None, by='', request=None):
    from .models import Activity
    from .models_investors import InvestorEvent
    ip = None
    if request is not None:
        ip = (request.META.get('HTTP_X_FORWARDED_FOR') or '').split(',')[0].strip() or request.META.get('REMOTE_ADDR') or None
    InvestorEvent.objects.create(business=investor.business, investor=investor, investment=investment, event=event,
                                 text=text[:300], by=by[:150], ip=ip)
    try:
        Activity.objects.create(business=investor.business, type=f'Investors · {event.replace("_", " ")}',
                                details=f'{investor.name}: {text}'[:255], actor=by[:150] or investor.name)
    except Exception:
        logger.exception('activity for investor event')


# ─────────────────────────────── scope profit ───────────────────────────────

def _router_ids(obj):
    if getattr(obj, 'scope', 'business') != 'sites':
        return None
    if getattr(obj, 'pk', None):
        return list(obj.routers.values_list('pk', flat=True))
    return list(getattr(obj, '_router_ids', []) or [])


def daily_profit(business, router_ids, start, end):
    """{day: (revenue net of commission, costs)} for start..end inclusive (local days)."""
    from .finance import _day_start
    from .models import Expense, VoucherSale
    lo, hi = _day_start(start), _day_start(end + timedelta(days=1))
    sales = VoucherSale.objects.filter(business=business, sold_at__gte=lo, sold_at__lt=hi)
    exps = Expense.objects.filter(business=business, paid_at__gte=lo, paid_at__lt=hi)
    if router_ids is not None:
        sales, exps = sales.filter(router_id__in=router_ids), exps.filter(router_id__in=router_ids)
    out = {}

    def day_of(value):
        if hasattr(value, 'hour'):
            value = timezone.localtime(value) if timezone.is_aware(value) else value
            return value.date()
        return value
    for r in sales.annotate(_d=TruncDay('sold_at')).values('_d').annotate(a=Sum('amount'), c=Sum('commission')):
        d = day_of(r['_d'])
        rev, cost = out.get(d, (ZERO, ZERO))
        out[d] = (rev + (r['a'] or ZERO) - (r['c'] or ZERO), cost)
    for r in exps.annotate(_d=TruncDay('paid_at')).values('_d').annotate(a=Sum('amount')):
        d = day_of(r['_d'])
        rev, cost = out.get(d, (ZERO, ZERO))
        out[d] = (rev, cost + (r['a'] or ZERO))
    return out


def profit_history(business, router_ids, days=90, until=None):
    """Daily profit for the last ``days`` full days (zeros included), oldest first."""
    until = until or (timezone.localdate() - timedelta(days=1))
    start = until - timedelta(days=days - 1)
    rows = daily_profit(business, router_ids, start, until)
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        rev, cost = rows.get(d, (ZERO, ZERO))
        out.append((d, rev, cost, rev - cost))
    return out


# ─────────────────────────────── the terms engine ───────────────────────────────

@dataclass
class State:
    earned_total: Decimal = ZERO
    repaid: Decimal = ZERO
    returned: Decimal = ZERO
    loss_carry: Decimal = ZERO

    @classmethod
    def of(cls, inv):
        return cls(q(inv.earned_total), q(inv.principal_repaid), q(inv.return_total), q(inv.loss_carry))


def current_share(inv, st):
    if inv.terms == 'profit_share' and inv.after_payback_share_pct is not None and st.repaid >= inv.principal:
        return Decimal(inv.after_payback_share_pct)
    return Decimal(inv.share_pct)


def is_complete(inv, st):
    due = inv.total_due
    return due is not None and st.earned_total >= due


def step(inv, st, profit):
    """Apply one day's scope profit to the deal. Mutates ``st``. Returns a dict for the day."""
    profit = q(profit)
    pct = current_share(inv, st)
    offset = ZERO
    if is_complete(inv, st):
        return {'profit': profit, 'share_pct': pct, 'loss_offset': ZERO, 'earned': ZERO, 'to_principal': ZERO, 'to_return': ZERO}
    if inv.carry_losses:
        if profit < 0:
            st.loss_carry = q(st.loss_carry - profit)
            base = ZERO
        else:
            offset = min(st.loss_carry, profit)
            st.loss_carry = q(st.loss_carry - offset)
            base = profit - offset
    else:
        base = max(profit, ZERO)
    earned = q(base * pct / 100)
    due = inv.total_due
    if due is not None:
        earned = min(earned, max(ZERO, q(due - st.earned_total)))
    remaining = max(ZERO, q(inv.principal - st.repaid))
    if inv.terms == 'fixed':
        rate = Decimal(inv.fixed_return_pct)
        to_p = min(q(earned * 100 / (100 + rate)) if rate > -100 else earned, remaining)
        if due is not None and q(st.earned_total + earned) >= due:
            to_p = remaining                     # last payment: the capital is exactly repaid
    else:
        to_p = min(q(earned * Decimal(inv.payback_pct) / 100), remaining) if remaining > 0 else ZERO
    to_p = min(to_p, earned)
    to_r = q(earned - to_p)
    st.earned_total = q(st.earned_total + earned)
    st.repaid = q(st.repaid + to_p)
    st.returned = q(st.returned + to_r)
    return {'profit': profit, 'share_pct': pct, 'loss_offset': q(offset), 'earned': earned, 'to_principal': to_p, 'to_return': to_r}


# ─────────────────────────────── accruals ───────────────────────────────

def accrue(inv, through=None):
    """Book every full day from the last accrued day to ``through`` (default yesterday). Returns days booked."""
    from .models_investors import InvestmentAccrual, Investment
    if inv.status != 'active' or not inv.start_date:
        return 0
    through = through or (timezone.localdate() - timedelta(days=1))
    first = (inv.accrued_through + timedelta(days=1)) if inv.accrued_through else inv.start_date
    first = max(first, through - timedelta(days=BACKFILL_MAX_DAYS))
    if first > through:
        return 0
    rows = daily_profit(inv.business, _router_ids(inv), first, through)
    booked = 0
    with transaction.atomic():
        inv = Investment.objects.select_for_update().get(pk=inv.pk)
        if inv.accrued_through and inv.accrued_through >= first:      # someone else got here first
            first = inv.accrued_through + timedelta(days=1)
        st = State.of(inv)
        d = first
        while d <= through:
            rev, cost = rows.get(d, (ZERO, ZERO))
            r = step(inv, st, rev - cost)
            InvestmentAccrual.objects.update_or_create(investment=inv, day=d, defaults={
                'revenue': q(rev), 'costs': q(cost), 'profit': r['profit'], 'share_pct': r['share_pct'],
                'loss_offset': r['loss_offset'], 'earned': r['earned'], 'to_principal': r['to_principal'], 'to_return': r['to_return']})
            booked += 1
            d += timedelta(days=1)
            if is_complete(inv, st):
                break
        inv.earned_total, inv.principal_repaid, inv.return_total, inv.loss_carry = st.earned_total, st.repaid, st.returned, st.loss_carry
        inv.accrued_through = d - timedelta(days=1)
        fields = ['earned_total', 'principal_repaid', 'return_total', 'loss_carry', 'accrued_through', 'updated_at']
        if is_complete(inv, st):
            inv.status, inv.completed_at = 'completed', timezone.now()
            fields += ['status', 'completed_at']
        inv.save(update_fields=fields)
    if inv.status == 'completed':
        log_event(inv.investor, 'completed', f'{inv.reference} reached its full return of {inv.total_due}', inv, by='TapTap')
        _notify_investor(inv, 'Your investment is complete',
                         [f'{inv.title} has paid its full agreed return of {money(inv.total_due, inv.business)}.',
                          'Thank you for investing with us.'])
    return booked


def recalculate(inv, user=None):
    """Rebuild every accrual from the start date (after back-dated sales/expenses were changed)."""
    from .models_investors import Investment
    with transaction.atomic():
        inv.accruals.all().delete()
        Investment.objects.filter(pk=inv.pk).update(accrued_through=None, earned_total=0, principal_repaid=0, return_total=0,
                                                    loss_carry=0, completed_at=None,
                                                    status='active' if inv.status == 'completed' else inv.status)
    inv.refresh_from_db()
    n = accrue(inv)
    log_event(inv.investor, 'recalculated', f'{inv.reference}: earnings rebuilt from {inv.start_date} ({n} days)', inv, by=_who(user))
    return n


def accrue_all():
    from .models_investors import Investment
    n = 0
    for inv in Investment.objects.filter(status='active').select_related('business', 'investor'):
        try:
            n += accrue(inv)
        except Exception:
            logger.exception('accrual failed for %s', inv.reference)
    return n


def today_live(inv):
    """Today's figure so far (not booked yet)."""
    if inv.status != 'active' or not inv.start_date:
        return {'profit': ZERO, 'earned': ZERO, 'to_principal': ZERO, 'to_return': ZERO, 'revenue': ZERO, 'costs': ZERO}
    today = timezone.localdate()
    if today < inv.start_date:
        return {'profit': ZERO, 'earned': ZERO, 'to_principal': ZERO, 'to_return': ZERO, 'revenue': ZERO, 'costs': ZERO}
    accrue(inv)
    inv.refresh_from_db()
    rev, cost = daily_profit(inv.business, _router_ids(inv), today, today).get(today, (ZERO, ZERO))
    r = step(inv, State.of(inv), rev - cost)
    r.update(revenue=q(rev), costs=q(cost))
    return r


def paid_out(inv):
    return q(inv.payouts.aggregate(v=Sum('amount'))['v'])


# ─────────────────────────────── projections ───────────────────────────────

def _trend(values):
    """Least-squares slope per day."""
    n = len(values)
    if n < 14:
        return 0.0
    xs = range(n)
    mx, my = (n - 1) / 2, sum(values) / n
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, values)) / den if den else 0.0


def baseline(business, router_ids, expected_monthly=None):
    """(daily profit baseline, monthly growth rate, confidence label, days of real data)."""
    hist = [float(p) for _, _, _, p in profit_history(business, router_ids, 90)]
    active_days = sum(1 for v in hist if v)
    if active_days < 14 and expected_monthly:
        return float(expected_monthly) / 30, 0.0, 'Owner estimate — new site, little history yet', active_days
    last30 = hist[-30:]
    avg30, avg90 = sum(last30) / 30, sum(hist) / 90
    base = 0.6 * avg30 + 0.4 * avg90
    growth = 0.0
    if base > 0:
        growth = max(-0.08, min(0.08, _trend(hist) * 30 / base))
    mean = sum(last30) / 30
    sd = math.sqrt(sum((v - mean) ** 2 for v in last30) / 30) if last30 else 0
    cv = sd / mean if mean > 0 else 9
    conf = ('High' if active_days >= 60 and cv < 1 else 'Medium' if active_days >= 30 else 'Low') + f' — {active_days} trading days of data'
    return base, growth, conf, active_days


def simulate(inv, st, base_daily, growth, horizon=365, mult=Decimal('1')):
    """Run the deal forward. Returns weekly cumulative earned, payback/complete day numbers and 12-month total."""
    st = State(st.earned_total, st.repaid, st.returned, st.loss_carry)
    weekly, payback_day, complete_day, earned_12m = [], None, None, ZERO
    start_earned = st.earned_total
    for day in range(1, horizon + 1):
        profit = Decimal(str(base_daily * ((1 + growth) ** (day / 30)))) * mult
        r = step(inv, st, profit)
        if day <= 365:
            earned_12m += r['earned']
        if payback_day is None and st.repaid >= inv.principal:
            payback_day = day
        if complete_day is None and is_complete(inv, st):
            complete_day = day
        if day % 7 == 0:
            weekly.append(float(st.earned_total))
    return {'weekly': weekly, 'payback_day': payback_day, 'complete_day': complete_day,
            'earned_12m': q(earned_12m), 'gain': q(st.earned_total - start_earned)}


def project(inv, horizon=730):
    """Projection for a live (or not-yet-funded) deal from the scope's own performance."""
    opp_expected = inv.opportunity.expected_monthly_profit if inv.opportunity_id else None
    base, growth, conf, days = baseline(inv.business, _router_ids(inv), opp_expected)
    st = State.of(inv)
    today = timezone.localdate()
    out = {'confidence': conf, 'base_daily_profit': round(base, 2), 'monthly_growth_pct': round(growth * 100, 1),
           'share_pct': float(current_share(inv, st)), 'labels': [], 'scenarios': {}, 'principal': float(inv.principal),
           'total_due': float(inv.total_due) if inv.total_due is not None else None}
    for i in range(horizon // 7):
        out['labels'].append((today + timedelta(days=7 * (i + 1))).isoformat())
    for name, mult in SCENARIOS:
        r = simulate(inv, st, base, growth, horizon, mult)
        out['scenarios'][name] = {
            'weekly': r['weekly'], 'earned_12m': float(r['earned_12m']), 'monthly': round(float(r['earned_12m']) / 12, 2),
            'payback_date': (today + timedelta(days=r['payback_day'])).isoformat() if r['payback_day'] else None,
            'complete_date': (today + timedelta(days=r['complete_day'])).isoformat() if r['complete_day'] else None,
            'payback_months': round(r['payback_day'] / 30.4, 1) if r['payback_day'] else None,
        }
    if st.repaid >= inv.principal:
        for s in out['scenarios'].values():
            s['payback_date'], s['payback_months'] = 'repaid', 0
    return out


def ticket_terms(opp, amount):
    """Pro-rata terms an investor gets for ``amount`` on an opportunity."""
    amount = q(amount)
    ratio = amount / opp.target_amount if opp.target_amount else ZERO
    share = (Decimal(opp.share_pct) * ratio).quantize(Decimal('0.001'))
    after = (Decimal(opp.after_payback_share_pct) * ratio).quantize(Decimal('0.001')) if opp.after_payback_share_pct is not None else None
    return {'principal': amount, 'share_pct': share, 'after_payback_share_pct': after, 'terms': opp.terms,
            'payback_pct': opp.payback_pct, 'fixed_return_pct': opp.fixed_return_pct, 'cap_pct': opp.cap_pct}


def estimate(opp, amount):
    """What-if calculator on an opportunity: the investor's money, the pro-rata share, the projection."""
    from .models_investors import Investment
    t = ticket_terms(opp, amount)
    inv = Investment(business=opp.business, principal=t['principal'], terms=t['terms'], share_pct=t['share_pct'],
                     payback_pct=t['payback_pct'], after_payback_share_pct=t['after_payback_share_pct'],
                     fixed_return_pct=t['fixed_return_pct'], cap_pct=t['cap_pct'], carry_losses=True)
    rids = list(opp.routers.values_list('pk', flat=True)) if opp.scope == 'sites' else None
    base, growth, conf, _ = baseline(opp.business, rids, opp.expected_monthly_profit)
    res = {'amount': float(t['principal']), 'share_pct': float(t['share_pct']),
           'after_payback_share_pct': float(t['after_payback_share_pct']) if t['after_payback_share_pct'] is not None else None,
           'split': [float(x) for x in inv.split_now], 'total_due': float(inv.total_due) if inv.total_due is not None else None,
           'confidence': conf, 'scenarios': {}}
    for name, mult in SCENARIOS:
        r = simulate(inv, State(), base, growth, 1095, mult)
        res['scenarios'][name] = {'monthly': round(float(r['earned_12m']) / 12, 2), 'earned_12m': float(r['earned_12m']),
                                  'payback_months': round(r['payback_day'] / 30.4, 1) if r['payback_day'] else None,
                                  'complete_months': round(r['complete_day'] / 30.4, 1) if r['complete_day'] else None,
                                  'roi_12m_pct': round(float(r['earned_12m']) / float(t['principal']) * 100, 1) if t['principal'] else 0}
    return res


# ─────────────────────────────── opportunities ───────────────────────────────

def committed(opp):
    return q(opp.investments.filter(status__in=FUNDED_STATES).aggregate(v=Sum('principal'))['v'])


def pending_offers(opp):
    return q(opp.investments.filter(status='offered').aggregate(v=Sum('principal'))['v'])


def remaining(opp):
    return max(ZERO, q(opp.target_amount - committed(opp)))


def visible_opportunities(investor):
    from .models_investors import InvestmentOpportunity
    return (InvestmentOpportunity.objects.filter(business=investor.business, status__in=['open', 'funded'])
            .filter(Q(visibility='all') | Q(invited=investor)).distinct().order_by('-created_at'))


def refresh_funded(opp):
    if opp.status == 'open' and committed(opp) >= opp.target_amount:
        opp.status = 'funded'
        opp.save(update_fields=['status', 'updated_at'])


def share_load(business):
    """How much of each scope's profit is promised to active investors (owner warning)."""
    from .models_investors import Investment
    load = {'business': ZERO, 'sites': {}}
    for inv in Investment.objects.filter(business=business, status__in=['active', 'awaiting_payment', 'payment_review']).prefetch_related('routers'):
        if inv.scope == 'business':
            load['business'] += Decimal(inv.share_pct)
        else:
            for r in inv.routers.all():
                load['sites'][r.name] = load['sites'].get(r.name, ZERO) + Decimal(inv.share_pct)
    return load


# ─────────────────────────────── deal lifecycle ───────────────────────────────

def _who(user):
    if not user:
        return 'TapTap'
    return user.get_full_name() or user.email or user.username


def money(v, business):
    return f'{business.currency}{q(v):,.2f}'


def place_offer(investor, opp, amount, note='', request=None):
    from .models_investors import Investment
    amount = dec(amount)
    if amount is None or amount <= 0:
        raise ValueError('Enter the amount you would like to invest.')
    amount = q(amount)
    if opp.status != 'open' or not visible_opportunities(investor).filter(pk=opp.pk).exists():
        raise ValueError('This opportunity is not open for offers.')
    if opp.closes_at and opp.closes_at < timezone.localdate():
        raise ValueError('This opportunity has closed.')
    if amount < opp.min_ticket:
        raise ValueError(f'The minimum investment is {money(opp.min_ticket, opp.business)}.')
    if opp.max_ticket and amount > opp.max_ticket:
        raise ValueError(f'The maximum investment is {money(opp.max_ticket, opp.business)}.')
    if amount > remaining(opp):
        raise ValueError(f'Only {money(remaining(opp), opp.business)} is still open on this opportunity.')
    if Investment.objects.filter(investor=investor, opportunity=opp, status='offered').exists():
        raise ValueError('You already have an offer waiting on this opportunity.')
    t = ticket_terms(opp, amount)
    with transaction.atomic():
        inv = Investment.objects.create(
            business=opp.business, investor=investor, opportunity=opp, reference=new_reference('INV'), title=opp.title,
            principal=amount, scope=opp.scope, terms=t['terms'], share_pct=t['share_pct'], payback_pct=t['payback_pct'],
            after_payback_share_pct=t['after_payback_share_pct'], fixed_return_pct=t['fixed_return_pct'], cap_pct=t['cap_pct'],
            status='offered', offer_note=(note or '')[:500])
        if opp.scope == 'sites':
            inv.routers.set(opp.routers.all())
    log_event(investor, 'offer', f'Offered {money(amount, opp.business)} on {opp.title} ({inv.reference})', inv, by=investor.name, request=request)
    _notify_owner(opp.business, f'New investment offer: {money(amount, opp.business)} on {opp.title}',
                  f'{investor.name} ({investor.email}) offered {money(amount, opp.business)} on "{opp.title}".\n'
                  f'Their share would be {t["share_pct"]}% of profit.\n\nNote from the investor: {note or "—"}\n\n'
                  f'Review it: {site_url(None, reverse("investment_admin", args=[inv.pk]))}')
    return inv


def decide(inv, accept, note='', user=None, amount=None):
    """Owner accepts (optionally for a smaller amount) or declines an offer."""
    if inv.status != 'offered':
        raise ValueError('This offer was already answered.')
    if accept:
        if amount not in (None, ''):
            new = dec(amount)
            if new is None or new <= 0 or new > inv.principal:
                raise ValueError('The accepted amount must be more than zero and not above the offer.')
            if inv.opportunity_id and q(new) != inv.principal:
                t = ticket_terms(inv.opportunity, new)
                inv.principal, inv.share_pct, inv.after_payback_share_pct = t['principal'], t['share_pct'], t['after_payback_share_pct']
        if inv.opportunity_id and inv.principal > remaining(inv.opportunity):
            raise ValueError(f'Only {money(remaining(inv.opportunity), inv.business)} is still open on this opportunity.')
        inv.status = 'awaiting_payment'
    else:
        inv.status = 'rejected'
    inv.decision_note = (note or '')[:500]
    inv.save()
    by = _who(user)
    if accept:
        log_event(inv.investor, 'accepted', f'Offer {inv.reference} accepted for {money(inv.principal, inv.business)}', inv, by=by)
        instr = settings_for(inv.business).payment_instructions.strip()
        _notify_investor(inv, 'Your offer was accepted', [
            f'Good news — your offer on {inv.title} was accepted for {money(inv.principal, inv.business)}.',
            f'Your share: {inv.share_pct}% of profit. Of every earning, {inv.split_now[0]}% repays your capital and '
            f'{inv.split_now[1]}% is your return until your money is back.',
            'How to pay: ' + (instr or 'the business will contact you with payment details.'),
            'After paying, open the investment in your portal and press “I have paid” with your payment reference.',
        ] + ([f'Note from the business: {note}'] if note else []), button='Open my investment', investment=inv)
        if inv.opportunity_id:
            refresh_funded(inv.opportunity)
    else:
        log_event(inv.investor, 'rejected', f'Offer {inv.reference} declined', inv, by=by)
        _notify_investor(inv, 'About your investment offer', [
            f'Thank you for your offer on {inv.title}. The business is not able to accept it this time.',
        ] + ([f'Note from the business: {note}'] if note else []), investment=inv)
    return inv


def mark_paid(inv, method, reference, request=None):
    if inv.status != 'awaiting_payment':
        raise ValueError('This investment is not waiting for a payment.')
    reference = (reference or '').strip()
    if len(reference) < 3:
        raise ValueError('Enter the payment reference so the business can find your payment.')
    inv.status, inv.payment_method, inv.payment_reference = 'payment_review', (method or '')[:60], reference[:120]
    inv.save(update_fields=['status', 'payment_method', 'payment_reference', 'updated_at'])
    log_event(inv.investor, 'payment_sent', f'{inv.reference}: paid {money(inv.principal, inv.business)} ({method} {reference})', inv,
              by=inv.investor.name, request=request)
    _notify_owner(inv.business, f'Investor payment to confirm: {money(inv.principal, inv.business)} from {inv.investor.name}',
                  f'{inv.investor.name} says they paid {money(inv.principal, inv.business)} for "{inv.title}".\n'
                  f'Method: {method or "—"}  Reference: {reference}\n\n'
                  f'Confirm it once the money is in: {site_url(None, reverse("investment_admin", args=[inv.pk]))}')
    return inv


def activate(inv, user=None, start_date=None, reference='', by=None):
    if inv.status not in ('awaiting_payment', 'payment_review', 'offered'):
        raise ValueError('This investment cannot be activated now.')
    today = timezone.localdate()
    inv.status, inv.funded_at = 'active', timezone.now()
    inv.start_date = start_date or (today + timedelta(days=1))
    if reference:
        inv.payment_reference = reference[:120]
    inv.save()
    log_event(inv.investor, 'activated', f'{inv.reference} is live: {money(inv.principal, inv.business)}, earnings from {inv.start_date:%d %b %Y}',
              inv, by=by or _who(user))
    _notify_investor(inv, 'Your investment is live', [
        f'We received your {money(inv.principal, inv.business)} for {inv.title}. Thank you!',
        f'Your earnings start on {inv.start_date:%d %B %Y}: {inv.share_pct}% of the profit, visible live in your portal '
        'and summarised in your daily email.',
    ], button='Watch it live', investment=inv)
    if inv.opportunity_id:
        refresh_funded(inv.opportunity)
    accrue(inv)
    return inv


def cancel(inv, user=None, note=''):
    if inv.status in ('completed', 'cancelled', 'rejected'):
        raise ValueError('This investment is already closed.')
    if inv.status == 'active' and inv.earned_total > 0:
        raise ValueError('An investment that has earned cannot be cancelled — complete it with a final payout instead.')
    inv.status, inv.decision_note = 'cancelled', (note or inv.decision_note)[:500]
    inv.save(update_fields=['status', 'decision_note', 'updated_at'])
    log_event(inv.investor, 'cancelled', f'{inv.reference} cancelled', inv, by=_who(user))
    return inv


def record_payout(inv, amount, method='', reference='', note='', user=None, when=None):
    from .models_investors import InvestorPayout
    amount = dec(amount)
    if amount is None or amount <= 0:
        raise ValueError('Enter the amount paid to the investor.')
    accrue(inv)
    inv.refresh_from_db()
    balance = q(inv.earned_total - paid_out(inv))
    if q(amount) > balance:
        raise ValueError(f'That is more than the investor has earned and not yet been paid ({money(balance, inv.business)}).')
    p = InvestorPayout.objects.create(business=inv.business, investment=inv, amount=q(amount), method=(method or '')[:60],
                                      reference=(reference or '')[:120], note=(note or '')[:255], recorded_by=user if getattr(user, 'pk', None) else None,
                                      paid_at=when or timezone.now())
    log_event(inv.investor, 'payout', f'{inv.reference}: paid out {money(amount, inv.business)} {method} {reference}'.strip(), inv, by=_who(user))
    _notify_investor(inv, f'Payout of {money(amount, inv.business)} sent', [
        f'{money(amount, inv.business)} from {inv.title} has been paid to you{(" by " + method) if method else ""}.'
        + (f' Reference: {reference}.' if reference else ''),
        f'Earned so far: {money(inv.earned_total, inv.business)} · paid out: {money(paid_out(inv), inv.business)}.',
    ], investment=inv)
    return p


def create_direct(business, investor, data, user=None):
    """Owner adds a deal (e.g. money received before TapTap). A past start date is back-filled."""
    from .models import Router
    from .models_investors import Investment
    principal = dec(data.get('principal'))
    share = dec(data.get('share_pct'))
    if not principal or principal <= 0:
        raise ValueError('Enter the amount invested.')
    terms = data.get('terms') if data.get('terms') in ('profit_share', 'fixed') else 'profit_share'
    if share is None or share <= 0 or share > 100:
        raise ValueError('Profit share must be between 0 and 100 %.')
    payback = dec(data.get('payback_pct'), Decimal('70'))
    if payback < 0 or payback > 100:
        raise ValueError('The capital part must be between 0 and 100 %.')
    fixed = dec(data.get('fixed_return_pct'), ZERO)
    if terms == 'fixed' and (fixed is None or fixed < 0):
        raise ValueError('Enter the fixed return % (0 or more).')
    scope = 'sites' if data.get('scope') == 'sites' else 'business'
    raw = data.getlist('routers') if hasattr(data, 'getlist') else (data.get('routers') or [])
    router_ids = [int(x) for x in raw if str(x).isdigit()]
    routers = list(Router.objects.filter(business=business, pk__in=router_ids))
    if scope == 'sites' and not routers:
        raise ValueError('Choose at least one site for a site-only investment.')
    start = None
    if data.get('start_date'):
        try:
            start = date.fromisoformat(data['start_date'])
        except ValueError:
            raise ValueError('Start date must be a date.')
    after = dec(data.get('after_payback_share_pct'))
    inv = Investment.objects.create(
        business=business, investor=investor, reference=new_reference('INV'), title=(data.get('title') or 'Investment')[:150],
        principal=q(principal), scope=scope, terms=terms, share_pct=share, payback_pct=payback, fixed_return_pct=fixed or ZERO,
        after_payback_share_pct=after, cap_pct=dec(data.get('cap_pct')), carry_losses=bool(data.get('carry_losses', True)),
        show_basis=bool(data.get('show_basis', True)), status='awaiting_payment')
    if routers:
        inv.routers.set(routers)
    log_event(investor, 'created', f'{inv.reference} added by the business: {money(principal, business)}', inv, by=_who(user))
    if data.get('already_paid'):
        activate(inv, user=user, start_date=start or timezone.localdate(), reference=data.get('payment_reference', ''))
    return inv


# ─────────────────────────────── hosted checkout (optional) ───────────────────────────────

def checkout_configured():
    return bool(getattr(settings, 'INVESTOR_PAYMENT_CHECKOUT_URL', '') and getattr(settings, 'INVESTOR_PAYMENT_WEBHOOK_SECRET', ''))


def checkout_url(inv, request):
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
    if not checkout_configured():
        raise ValueError('Online payment is not set up — please pay using the instructions shown.')
    if inv.status != 'awaiting_payment':
        raise ValueError('This investment is not waiting for a payment.')
    parts = list(urlparse(settings.INVESTOR_PAYMENT_CHECKOUT_URL))
    query = dict(parse_qsl(parts[4], keep_blank_values=True))
    query.update({'reference': inv.reference, 'amount': f'{inv.principal:.2f}', 'currency': inv.business.currency,
                  'email': inv.investor.email, 'return_url': site_url(request, reverse('investor_investment', args=[inv.reference])),
                  'webhook_url': site_url(request, reverse('investor_payment_webhook'))})
    parts[4] = urlencode(query)
    log_event(inv.investor, 'checkout', f'{inv.reference}: online checkout started', inv, by=inv.investor.name, request=request)
    return urlunparse(parts)


def verify_webhook(raw, signature):
    secret = str(getattr(settings, 'INVESTOR_PAYMENT_WEBHOOK_SECRET', '') or '')
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix('sha256=').strip())


def settle_webhook(payload):
    from .models_investors import Investment
    ref = str(payload.get('reference') or '').strip()
    status = str(payload.get('status') or '').lower()
    with transaction.atomic():
        inv = Investment.objects.select_for_update().select_related('business', 'investor').filter(reference=ref).first()
        if not inv:
            raise ValueError('Unknown reference.')
        if inv.status == 'active':
            return inv
        if status not in ('paid', 'success', 'successful', 'completed'):
            log_event(inv.investor, 'payment_failed', f'{inv.reference}: online payment {status or "failed"}', inv, by='Payment provider')
            return inv
        if q(dec(payload.get('amount'), ZERO)) != inv.principal:
            inv.status = 'payment_review'
            inv.save(update_fields=['status', 'updated_at'])
            log_event(inv.investor, 'payment_review', f'{inv.reference}: amount mismatch from provider, needs review', inv, by='Payment provider')
            raise ValueError('Amount does not match.')
    return activate(inv, reference=str(payload.get('provider_reference') or '')[:120], by='Online payment')


# ─────────────────────────────── portal auth ───────────────────────────────

def _hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_link(investor, request=None, reason='Portal access', hours=None):
    from .models_investors import InvestorMagicLink
    raw = secrets.token_urlsafe(36)
    now = timezone.now()
    if reason == 'Portal access':
        InvestorMagicLink.objects.filter(investor=investor, used_at__isnull=True, expires_at__gt=now, reason=reason).update(expires_at=now)
    InvestorMagicLink.objects.create(investor=investor, token_hash=_hash(raw), reason=reason[:60],
                                     expires_at=now + (timedelta(hours=hours) if hours else timedelta(minutes=MAGIC_MINUTES)))
    return site_url(request, reverse('investor_access', args=[raw]))


def send_link(investor, request=None, by=''):
    from .member_notifications import _send
    url = issue_link(investor, request)
    _send(investor.email, f'[{investor.business.business_name}] Your investor portal link', 'core/email/investor_magic.txt',
          'core/email/investor_magic.html', {'business': investor.business, 'investor': investor, 'url': url, 'minutes': MAGIC_MINUTES})
    log_event(investor, 'link_sent', f'Portal link sent to {investor.email}', by=by or investor.name, request=request)
    return url


def request_link(request, email):
    """Always answers the same way (never reveals whether an email is an investor)."""
    from .models_investors import Investor
    email = (email or '').strip().lower()
    if not email:
        return
    ipkey = _hash(request.META.get('REMOTE_ADDR') or 'x')[:20]
    for key, limit in ((f'inv-ip:{ipkey}', 20), (f'inv-em:{_hash(email)[:20]}', 5)):
        n = int(cache.get(key, 0) or 0)
        if n >= limit:
            return
        cache.set(key, n + 1, 3600)
    for inv in Investor.objects.filter(email__iexact=email, status='active').select_related('business')[:5]:
        if not settings_for(inv.business).enabled:
            continue
        try:
            send_link(inv, request)
        except Exception:
            logger.exception('investor link failed for %s', inv.pk)


@transaction.atomic
def consume_link(raw):
    from .models_investors import InvestorMagicLink
    now = timezone.now()
    row = InvestorMagicLink.objects.select_for_update().select_related('investor__business').filter(token_hash=_hash(raw)).first()
    if not row or row.used_at or row.expires_at <= now or row.investor.status != 'active':
        return None
    row.used_at = now
    row.save(update_fields=['used_at'])
    row.investor.last_login_at = now
    row.investor.save(update_fields=['last_login_at'])
    return row.investor


def start_session(request, investor):
    request.session.cycle_key()
    request.session[SESSION_ID], request.session[SESSION_AV] = investor.pk, investor.auth_version
    request.session.set_expiry(SESSION_SECONDS)
    log_event(investor, 'login', 'Signed in to the portal', by=investor.name, request=request)


def current_investor(request):
    from .models_investors import Investor
    pk = request.session.get(SESSION_ID)
    if not pk:
        return None
    inv = Investor.objects.filter(pk=pk, status='active').select_related('business').first()
    if not inv or int(request.session.get(SESSION_AV) or 0) != inv.auth_version:
        return None
    if not settings_for(inv.business).enabled:
        return None
    return inv


def sign_out_everywhere(investor, user=None):
    investor.auth_version += 1
    investor.save(update_fields=['auth_version'])
    log_event(investor, 'signed_out', 'Signed out of every device', by=_who(user))


# ─────────────────────────────── what the investor sees ───────────────────────────────

def f(v):
    return float(q(v))


def deal_card(inv, live=True):
    today = today_live(inv) if live and inv.status == 'active' else {'earned': ZERO, 'to_principal': ZERO, 'to_return': ZERO, 'profit': ZERO}
    po = paid_out(inv)
    cap_pct, ret_pct = inv.split_now
    return {
        'reference': inv.reference, 'title': inv.title, 'status': inv.status, 'status_label': inv.get_status_display(),
        'terms': inv.terms, 'terms_label': inv.get_terms_display(), 'principal': f(inv.principal),
        'share_pct': float(current_share(inv, State.of(inv))), 'payback_pct': float(inv.payback_pct),
        'fixed_return_pct': float(inv.fixed_return_pct),
        'after_payback_share_pct': float(inv.after_payback_share_pct) if inv.after_payback_share_pct is not None else None,
        'cap_pct': float(inv.cap_pct) if inv.cap_pct else None,
        'total_due': f(inv.total_due) if inv.total_due is not None else None,
        'split_capital_pct': float(cap_pct), 'split_return_pct': float(ret_pct),
        'earned': f(inv.earned_total + today['earned']), 'earned_booked': f(inv.earned_total),
        'repaid': f(inv.principal_repaid + today['to_principal']), 'returned': f(inv.return_total + today['to_return']),
        'today': f(today['earned']), 'today_capital': f(today['to_principal']), 'today_return': f(today['to_return']),
        'paid_out': f(po), 'balance': f(inv.earned_total + today['earned'] - po),
        'payback_progress': round(min(100.0, float((inv.principal_repaid + today['to_principal']) / inv.principal * 100)) if inv.principal else 0, 1),
        'start_date': inv.start_date.isoformat() if inv.start_date else None,
        'scope_label': inv.get_scope_display() if inv.scope == 'business' else 'Sites: ' + ', '.join(inv.routers.values_list('name', flat=True)),
        'loss_carry': f(inv.loss_carry),
    }


def earnings_series(investments, days=60):
    from .models_investors import InvestmentAccrual
    end = timezone.localdate()
    start = end - timedelta(days=days - 1)
    rows = InvestmentAccrual.objects.filter(investment__in=investments, day__gte=start).values('day').annotate(
        c=Sum('to_principal'), r=Sum('to_return'))
    per = {r['day']: (float(r['c'] or 0), float(r['r'] or 0)) for r in rows}
    labels, cap, ret = [], [], []
    for i in range(days):
        d = start + timedelta(days=i)
        labels.append(d.isoformat())
        c, r = per.get(d, (0.0, 0.0))
        cap.append(round(c, 2))
        ret.append(round(r, 2))
    avg7 = [round(sum(cap[max(0, i - 6):i + 1]) / min(7, i + 1) + sum(ret[max(0, i - 6):i + 1]) / min(7, i + 1), 2) for i in range(days)]
    return {'labels': labels, 'capital': cap, 'return': ret, 'avg7': avg7}


def dashboard(investor):
    from .models_investors import Investment
    deals = list(Investment.objects.filter(investor=investor).exclude(status__in=['rejected', 'cancelled'])
                 .select_related('business', 'opportunity').prefetch_related('routers'))
    cards = [deal_card(d) for d in deals]
    earning = [d for d in deals if d.status in ('active', 'completed')]
    tot = lambda k: round(sum(c[k] for c in cards if c['status'] in ('active', 'completed')), 2)
    invested = round(sum(c['principal'] for c in cards if c['status'] in ('active', 'completed')), 2)
    month_start = timezone.localdate().replace(day=1)
    mtd = float(q(sum((a.earned for d in earning for a in d.accruals.filter(day__gte=month_start)), ZERO))) + tot('today')
    opps = []
    for o in visible_opportunities(investor)[:12]:
        raised = committed(o)
        opps.append({'reference': o.reference, 'title': o.title, 'summary': o.summary, 'status': o.status, 'terms': o.get_terms_display(),
                     'target': f(o.target_amount), 'raised': f(raised), 'pct': round(min(100.0, float(raised / o.target_amount * 100)), 1) if o.target_amount else 0,
                     'min_ticket': f(o.min_ticket), 'share_pct': float(o.share_pct), 'fixed_return_pct': float(o.fixed_return_pct)})
    return {
        'currency': investor.business.currency, 'name': investor.name, 'now': timezone.localtime().isoformat(),
        'totals': {'invested': invested, 'earned': tot('earned'), 'today': tot('today'), 'paid_out': tot('paid_out'),
                   'balance': tot('balance'), 'repaid': tot('repaid'), 'returned': tot('returned'), 'mtd': round(mtd, 2),
                   'payback_progress': round(tot('repaid') / invested * 100, 1) if invested else 0},
        'deals': cards, 'series': earnings_series(earning), 'opportunities': opps,
    }


def deal_detail(inv, with_projection=True, owner=False):
    from .models_investors import InvestorEvent
    card = deal_card(inv)
    out = {'deal': card, 'currency': inv.business.currency, 'series': earnings_series([inv], 90),
           'payouts': [{'amount': f(p.amount), 'method': p.method, 'reference': p.reference, 'paid_at': timezone.localtime(p.paid_at).isoformat()}
                       for p in inv.payouts.all()[:50]],
           'events': [{'at': timezone.localtime(e.created_at).isoformat(), 'text': e.text} for e in
                      InvestorEvent.objects.filter(investment=inv).exclude(event__in=['login']).order_by('-created_at')[:30]],
           'cumulative': [], 'basis': None}
    acc = list(inv.accruals.order_by('day').values('day', 'earned', 'revenue', 'costs', 'profit'))
    run = 0.0
    for a in acc:
        run += float(a['earned'])
        out['cumulative'].append({'day': a['day'].isoformat(), 'earned': round(run, 2)})
    if inv.show_basis or owner:
        hist = profit_history(inv.business, _router_ids(inv), 60, until=timezone.localdate())
        out['basis'] = {'labels': [d.isoformat() for d, _, _, _ in hist], 'revenue': [f(r) for _, r, _, _ in hist],
                        'costs': [f(c) for _, _, c, _ in hist], 'profit': [f(p) for _, _, _, p in hist]}
    if with_projection and inv.status in ('active', 'awaiting_payment', 'payment_review', 'offered'):
        out['projection'] = project(inv)
    return out


# ─────────────────────────────── notifications ───────────────────────────────

def _notify_investor(inv, title, lines, button='Open my portal', investment=None):
    from .member_notifications import _send
    investor = inv.investor
    try:
        url = issue_link(investor, None, reason='Notice link', hours=SUMMARY_LINK_HOURS)
        _send(investor.email, f'[{inv.business.business_name}] {title}', 'core/email/investor_notice.txt', 'core/email/investor_notice.html',
              {'business': inv.business, 'investor': investor, 'title': title, 'lines': lines, 'url': url, 'button': button,
               'investment': investment})
    except Exception:
        logger.exception('investor notice failed for %s', investor.pk)


def _notify_owner(business, subject, body):
    from django.core.mail import send_mail
    to = getattr(business.user, 'email', '')
    if not to:
        return
    try:
        send_mail(f'[TapTap] {subject}'[:180], body, getattr(settings, 'DEFAULT_FROM_EMAIL', None), [to], fail_silently=False)
    except Exception:
        logger.exception('owner investor notice failed')
    try:
        from .live import push_event
        push_event(business.pk, subject, 'sale')
    except Exception:
        pass


def send_summary(investor, day=None):
    from .member_notifications import _send
    data = dashboard(investor)
    live = [d for d in data['deals'] if d['status'] in ('active', 'completed', 'awaiting_payment', 'payment_review')]
    if not live:
        return False
    from .models_investors import Investment, InvestmentAccrual
    y = (day or timezone.localdate()) - timedelta(days=1)
    yrows = InvestmentAccrual.objects.filter(investment__investor=investor, day=y).aggregate(e=Sum('earned'), c=Sum('to_principal'), r=Sum('to_return'))
    projections = []
    for inv in Investment.objects.filter(investor=investor, status='active'):
        p = project(inv, horizon=730)['scenarios']['expected']
        projections.append({'title': inv.title, 'monthly': p['monthly'], 'payback_date': p['payback_date']})
    url = issue_link(investor, None, reason='Daily summary', hours=SUMMARY_LINK_HOURS)
    _send(investor.email, f'[{investor.business.business_name}] Your investment summary — {y:%d %b %Y}',
          'core/email/investor_daily.txt', 'core/email/investor_daily.html',
          {'business': investor.business, 'investor': investor, 'data': data, 'deals': live, 'day': y, 'url': url,
           'yesterday': {'earned': f(yrows['e']), 'capital': f(yrows['c']), 'return': f(yrows['r'])},
           'projections': projections, 'cur': investor.business.currency,
           'open_opps': [o for o in data['opportunities'] if o['status'] == 'open']})
    return True


def send_due_summaries(now=None):
    from .models_investors import Investor, InvestorSummaryLog
    now = timezone.localtime(now or timezone.now())
    sent = 0
    for investor in Investor.objects.filter(status='active', daily_summary=True).select_related('business'):
        s = settings_for(investor.business)
        if not s.enabled or now.hour < s.summary_hour:
            continue
        log, _ = InvestorSummaryLog.objects.get_or_create(investor=investor, day=now.date(), defaults={'status': 'pending'})
        if log.status in ('sent', 'skipped') or log.attempts >= 3:
            continue
        log.attempts += 1
        try:
            log.status = 'sent' if send_summary(investor, now.date()) else 'skipped'
            log.error = ''
            sent += log.status == 'sent'
        except Exception as exc:
            logger.exception('investor summary for %s', investor.pk)
            log.status, log.error = 'failed', str(exc)[:255]
        log.save(update_fields=['status', 'attempts', 'error'])
    return sent


def tick(now=None):
    """Every 15 minutes: book finished days, then send the daily summaries that are due."""
    return {'days': accrue_all(), 'summaries': send_due_summaries(now)}


# ─────────────────────────────── install ───────────────────────────────

ADMIN_URLS = ('investors_admin', 'investors_action', 'investment_admin', 'investment_admin_data')
NAV_DESKTOP = '<a href="/investors/"><i class="bi bi-graph-up-arrow"></i> Investors</a>'
NAV_MOBILE = '<a href="/investors/">Investors</a>'


def _install_permissions():
    from . import permissions
    permissions.URL_PERMS['investors_admin'] = ('finance.manage', 'finance.view')
    permissions.URL_PERMS['investment_admin'] = ('finance.manage', 'finance.view')
    permissions.URL_PERMS['investment_admin_data'] = ('finance.manage', 'finance.view')
    permissions.URL_PERMS['investors_action'] = 'finance.manage'


def _install_urls():
    from . import urls
    from . import views_investors as v
    names = {getattr(p, 'name', None) for p in urls.urlpatterns}
    routes = [
        ('investors_admin', 'investors/', v.admin_page),
        ('investors_action', 'investors/action/', v.admin_action),
        ('investment_admin', 'investors/deal/<int:pk>/', v.admin_deal),
        ('investment_admin_data', 'investors/deal/<int:pk>/data/', v.admin_deal_data),
        # /p/ is outside the team gate: investors are not TapTap users, the portal has its own session.
        ('investor_portal', 'p/investor/', v.portal),
        ('investor_request_link', 'p/investor/link/', v.portal_request_link),
        ('investor_access', 'p/investor/access/<str:token>/', v.portal_access),
        ('investor_logout', 'p/investor/logout/', v.portal_logout),
        ('investor_live', 'p/investor/live/', v.portal_live),
        ('investor_fit', 'p/investor/fit/', v.portal_fit),
        ('investor_investment', 'p/investor/i/<str:ref>/', v.portal_investment),
        ('investor_investment_data', 'p/investor/i/<str:ref>/data/', v.portal_investment_data),
        ('investor_mark_paid', 'p/investor/i/<str:ref>/paid/', v.portal_mark_paid),
        ('investor_checkout', 'p/investor/i/<str:ref>/checkout/', v.portal_checkout),
        ('investor_opportunity', 'p/investor/o/<str:ref>/', v.portal_opportunity),
        ('investor_estimate', 'p/investor/o/<str:ref>/estimate/', v.portal_estimate),
        ('investor_offer', 'p/investor/o/<str:ref>/offer/', v.portal_offer),
        ('investor_payment_webhook', 'api/investor/payment/webhook/', v.payment_webhook),
    ]
    # Inserted at the front: the public portal pages use a catch-all ``p/<slug>/`` that would swallow /p/investor/.
    missing = [path(route, view, name=name) for name, route, view in routes if name not in names]
    urls.urlpatterns[0:0] = missing


def _inject_nav(request, response):
    perms = getattr(request, 'tt_perms', frozenset())
    if (not getattr(request, 'tt_business', None) or not ({'finance.manage', 'finance.view'} & set(perms))
            or getattr(response, 'streaming', False) or response.status_code != 200
            or 'text/html' not in response.get('Content-Type', '').lower()):
        return response
    body = response.content.decode(response.charset or 'utf-8')
    if 'href="/investors/"' in body:
        return response
    anchor = '<a href="/finance/"><i class="bi bi-cash-stack"></i> Finance</a>'
    if anchor in body:
        body = body.replace(anchor, anchor + NAV_DESKTOP, 1)
    mobile = '<a href="/finance/">Finance</a>'
    if mobile in body:
        body = body.replace(mobile, mobile + NAV_MOBILE, 1)
    out = body.encode(response.charset or 'utf-8')
    response.content = out
    if response.has_header('Content-Length'):
        response['Content-Length'] = str(len(out))
    return response


def _install_nav():
    from .team import TeamAccessMiddleware
    original = TeamAccessMiddleware.__call__
    if getattr(original, '_taptap_investors_nav', False):
        return

    @wraps(original)
    def wrapped(self, request):
        return _inject_nav(request, original(self, request))
    wrapped._taptap_investors_nav = True
    TeamAccessMiddleware.__call__ = wrapped


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _install_permissions()
    _install_urls()
    _install_nav()
    _INSTALLED = True
