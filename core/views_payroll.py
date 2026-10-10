"""Staff pay: setting pay terms on the Team page, paying people from Finance, payslips and My pay.

Safety rules (on top of the URL permission 'payroll.manage'):
  • nobody sets or records their own pay — someone else has to;
  • an admin cannot set or pay another admin; only the owner can.
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .models import PAYMENT_METHODS
from .models_payroll import PAY_TYPES, PAYOUT_KINDS, StaffPay, StaffPayout
from .payroll import (
    describe, member_history, member_month, month_start, parse_month, payroll_month,
    record_payout, void_payout, q2,
)
from .utils import log

MANUAL_METHODS = [m for m in PAYMENT_METHODS if m[0] != 'auto']


def _b(request):
    return request.user.business


def _dec(value, hi=None):
    try:
        v = Decimal(str(value or '0').replace(',', '').strip() or '0')
    except (InvalidOperation, ValueError):
        return Decimal('0')
    v = max(Decimal('0'), v)
    return min(v, Decimal(hi)) if hi is not None else v


def _next(request, fallback):
    target = request.POST.get('next') or request.GET.get('next') or ''
    if target and url_has_allowed_host_and_scheme(target, {request.get_host()}, require_https=request.is_secure()):
        return redirect(target)
    return redirect(fallback)


def _payroll_url(month):
    return f"{reverse('finance')}?tab=payroll&pm={month:%Y-%m}"


def _can_pay(request, member):
    """None when allowed, else the reason it is not."""
    if member.user_id == request.user.pk:
        return 'You cannot set or record your own pay. Ask the owner or another admin.'
    if member.role == 'admin' and request.tt_role != 'owner':
        return 'Only the owner can set or pay an admin.'
    return None


# ───────────────────────────── pay terms (Team page) ─────────────────────────────
@login_required
@require_POST
def team_pay_save(request):
    business = _b(request)
    member = get_object_or_404(business.team.select_related('user'), pk=request.POST.get('member'))
    refused = _can_pay(request, member)
    if refused:
        messages.error(request, refused)
        return _next(request, 'team')

    existing = StaffPay.objects.filter(member=member).first()
    if request.POST.get('action') == 'remove':
        if existing:
            existing.delete()
            log(business, 'Team', f'Removed the pay terms of {member.name}')
            messages.success(request, f'{member.name} is no longer on the payroll. Past payments are kept.')
        return _next(request, 'team')

    pay_type = request.POST.get('pay_type', 'fixed')
    if pay_type not in dict(PAY_TYPES):
        pay_type = 'fixed'
    amount = q2(_dec(request.POST.get('amount')))
    percent = _dec(request.POST.get('percent'), '100').quantize(Decimal('0.0001'))
    minimum = q2(_dec(request.POST.get('minimum')))
    cap = q2(_dec(request.POST.get('cap')))

    if pay_type == 'fixed':
        percent, minimum = Decimal('0'), Decimal('0')
        if amount <= 0:
            messages.error(request, 'Enter the monthly salary.')
            return _next(request, 'team')
    elif percent <= 0:
        messages.error(request, 'Enter the percentage they earn.')
        return _next(request, 'team')
    if cap and cap < max(minimum, amount):
        messages.error(request, 'The cap must be at least the guaranteed minimum and the basic pay.')
        return _next(request, 'team')

    site = None
    if pay_type != 'fixed' and request.POST.get('site', '').isdigit():
        site = business.routers.filter(pk=request.POST['site']).first()
    method = request.POST.get('method', 'cash')
    if method not in dict(MANUAL_METHODS):
        method = 'cash'
    try:
        pay_day = max(1, min(31, int(request.POST.get('pay_day') or 28)))
    except ValueError:
        pay_day = 28
    try:
        starts_on = datetime.strptime(request.POST.get('starts_on', ''), '%Y-%m-%d').date()
    except ValueError:
        starts_on = None

    pay = existing or StaffPay(business=business, member=member)
    pay.pay_type, pay.amount, pay.percent, pay.minimum, pay.cap = pay_type, amount, percent, minimum, cap
    pay.site, pay.method, pay.pay_day, pay.starts_on = site, method, pay_day, starts_on
    pay.show_basis = bool(request.POST.get('show_basis'))
    pay.active = request.POST.get('active', '1') != '0'
    pay.note = request.POST.get('note', '').strip()[:255]
    pay.updated_by = request.user
    pay.save()

    # The log is visible to more people than payroll, so it says that pay changed, not how much.
    log(business, 'Team', f'{"Changed" if existing else "Set"} the pay terms of {member.name}')
    messages.success(request, f'{member.name}: {describe(pay, business.currency)}.')
    return _next(request, 'team')


# ───────────────────────────── paying (Finance → Payroll) ─────────────────────────────
def _when(value):
    if not value:
        return timezone.now()
    try:
        day = datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        return timezone.now()
    if day >= timezone.localdate():
        return timezone.now()
    return timezone.make_aware(datetime.combine(day, datetime.min.time()).replace(hour=12))


@login_required
@require_POST
def payroll_pay(request):
    business = _b(request)
    member = get_object_or_404(business.team.select_related('user'), pk=request.POST.get('member'))
    month = parse_month(request.POST.get('month'))
    refused = _can_pay(request, member)
    if refused:
        messages.error(request, refused)
        return _next(request, _payroll_url(month))

    kind = request.POST.get('kind', 'salary')
    if kind not in dict(PAYOUT_KINDS):
        kind = 'salary'
    amount = q2(_dec(request.POST.get('amount')))
    if amount <= 0:
        messages.error(request, 'Enter an amount above zero.')
        return _next(request, _payroll_url(month))
    method = request.POST.get('method', 'cash')
    if method not in dict(MANUAL_METHODS):
        method = 'cash'

    p = record_payout(business, member, month, kind, amount, method=method,
                      reference=request.POST.get('reference', '').strip(),
                      note=request.POST.get('note', '').strip(),
                      paid_at=_when(request.POST.get('paid_at')), user=request.user)
    label = p.get_kind_display().lower()
    log(business, 'Finance', f'Recorded a {label} for {member.name} ({month:%B %Y})')
    from .templatetags.taptap_extras import money
    if kind == 'deduction':
        messages.success(request, f'Deduction of {money(amount, business.currency)} recorded for {member.name}.')
    else:
        messages.success(request, f'{member.name} paid {money(amount, business.currency)} ({label}, {month:%B %Y}). '
                                  'It is in Expenses under Staff & wages.')
    return _next(request, _payroll_url(month))


@login_required
@require_POST
def payroll_pay_all(request):
    """Pay every outstanding balance for a month in one go, each by the person's usual method."""
    business = _b(request)
    month = parse_month(request.POST.get('month'))
    pr = payroll_month(business, month)
    paid, skipped, total = 0, [], Decimal('0')
    when = _when(request.POST.get('paid_at'))
    for r in pr['rows']:
        m = r['member']
        balance = r['balance']
        if m is None or balance <= 0:
            continue
        if _can_pay(request, m):
            skipped.append(m.name)
            continue
        record_payout(business, m, month, 'salary', balance, method=r['pay'].method if r['pay'] else 'cash',
                      reference=request.POST.get('reference', '').strip(), note='Paid with "Pay everyone"',
                      paid_at=when, user=request.user)
        paid += 1
        total += balance
    from .templatetags.taptap_extras import money
    if paid:
        log(business, 'Finance', f'Paid {paid} staff balance{"s" if paid != 1 else ""} for {month:%B %Y}')
        messages.success(request, f'Paid {paid} {"person" if paid == 1 else "people"} — {money(total, business.currency)} '
                                  f'for {month:%B %Y}.')
    else:
        messages.info(request, 'Nobody has a balance to pay for that month.')
    if skipped:
        messages.warning(request, 'Not paid (someone else must pay them): ' + ', '.join(skipped))
    return _next(request, _payroll_url(month))


@login_required
@require_POST
def payroll_void(request, pk):
    business = _b(request)
    payout = get_object_or_404(StaffPayout.objects.select_related('member', 'expense'), pk=pk, business=business)
    if payout.member and _can_pay(request, payout.member):
        messages.error(request, _can_pay(request, payout.member))
        return _next(request, _payroll_url(payout.month))
    month, name, label = payout.month, payout.member_name, payout.get_kind_display().lower()
    void_payout(payout)
    log(business, 'Finance', f'Voided a {label} for {name} ({month:%B %Y})')
    messages.success(request, f'The {label} for {name} was voided and removed from Expenses.')
    return _next(request, _payroll_url(month))


# ───────────────────────────── payslips ─────────────────────────────
def _payslip(request, member, month, back_url, show_basis=True):
    business = _b(request)
    row = member_month(member, month)
    from .finance import amount_in_words
    net = row['paid']
    return render(request, 'core/payslip.html', {
        'member': member, 'row': row, 'calc': row['calc'], 'month': month, 'back_url': back_url,
        'terms': describe(row['pay'], business.currency) if row['pay'] else 'No pay terms',
        'number': f'SLIP-{member.pk:04d}-{month:%Y%m}',
        'words': amount_in_words(net, business.currency),
        'printed_by': request.user.get_full_name() or request.user.email,
        'show_basis': show_basis,
    })


@login_required
def payroll_payslip(request, member_pk, month):
    business = _b(request)
    member = get_object_or_404(business.team.select_related('user'), pk=member_pk)
    if member.role == 'admin' and request.tt_role != 'owner' and member.user_id != request.user.pk:
        raise Http404
    return _payslip(request, member, parse_month(month), _payroll_url(parse_month(month)))


@login_required
def my_payslip(request, month):
    member = getattr(request, 'tt_member', None)
    if member is None:
        raise Http404
    pay = getattr(member, 'pay', None)
    return _payslip(request, member, parse_month(month), reverse('my_pay'), show_basis=bool(pay and pay.show_basis))


# ───────────────────────────── My pay (every team member) ─────────────────────────────
@login_required
def my_pay(request):
    member = getattr(request, 'tt_member', None)
    pay = getattr(member, 'pay', None) if member else None
    history = member_history(member) if member else []
    current = history[0] if history else None
    return render(request, 'core/my_pay.html', {
        'member': member, 'pay': pay, 'history': history, 'current': current,
        'terms': describe(pay, _b(request).currency) if pay else '',
        'show_basis': bool(pay and pay.show_basis),
        'payments': member.payouts.order_by('-paid_at')[:24] if member else [],
        'this_month': month_start(),
    })
