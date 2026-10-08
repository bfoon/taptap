"""One member's page: account, time, scheduled pause, password & access, devices, billing, security, history."""
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import member_support as ms
from .models import Voucher

PW_SESSION = 'tt_member_pw'


def _b(request):
    return request.user.business


def _can(request, perm):
    perms = getattr(request, 'tt_perms', None)
    return perms is None or perm in perms


def _member(request, pk):
    return get_object_or_404(Voucher.all_objects.filter(business=_b(request), login_type='member')
                             .select_related('router', 'frozen_by', 'business'), pk=pk)


def _when(value):
    """<input type=datetime-local> (or a date) in the business's local time → aware datetime, or None."""
    v = str(value or '').strip()
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v if 'T' in v or ' ' in v else v + 'T08:00')
    except ValueError:
        raise ms.SupportError('That date is not valid.')
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


def _charge_kinds():
    from .models_member_charges import MemberCharge
    return MemberCharge.KINDS


def _ledger(member):
    """Every renewal (what was charged, what was paid then) and every later balance payment, newest first,
    with the balance after each line."""
    from decimal import Decimal
    from .models_member_plans import MemberRenewal
    try:
        from .models_member_arrears import MemberBalancePayment
        payments = list(MemberBalancePayment.objects.filter(member=member).select_related('recorded_by'))
    except Exception:
        payments = []
    from .views_agents import MANUAL_METHODS
    names = dict(MANUAL_METHODS)
    label = lambda code: names.get(code, str(code or '').replace('_', ' ').title())
    rows = []
    for r in MemberRenewal.objects.filter(member=member).select_related('recorded_by'):
        rows.append({'kind': 'renewal', 'at': r.renewed_at, 'label': f'Renewal · {r.plan_name}', 'charged': r.plan_price,
                     'paid': r.amount_collected, 'method': label(r.payment_method),
                     'reference': getattr(r, 'reference', ''), 'id': r.pk, 'number': r.receipt_number, 'currency': r.currency,
                     'by': r.recorded_by})
    for p in payments:
        rows.append({'kind': 'payment', 'at': p.paid_at, 'label': 'Balance payment', 'charged': Decimal('0'), 'paid': p.amount_collected,
                     'method': label(p.payment_method),
                     'reference': p.reference, 'id': p.pk, 'number': p.receipt_number, 'currency': p.currency, 'by': p.recorded_by})
    from .models_member_charges import MemberCharge
    from .member_arrears import _charge_paid
    cpaid = _charge_paid(payments)
    for c in MemberCharge.objects.filter(member=member).select_related('created_by'):
        rows.append({'kind': 'charge', 'at': c.charged_at, 'label': c.label, 'charged': c.amount, 'paid': Decimal('0'),
                     'method': '', 'reference': '', 'id': c.pk, 'number': c.receipt_number, 'currency': c.currency, 'by': c.created_by})
        if c.waived_at:
            left = max(Decimal('0'), c.amount - cpaid[c.pk])
            if left:
                rows.append({'kind': 'waived', 'at': c.waived_at, 'label': f'Waived · {c.get_kind_display()}', 'charged': -left,
                             'paid': Decimal('0'), 'method': c.waive_reason, 'reference': '', 'id': c.pk, 'number': c.receipt_number,
                             'currency': c.currency, 'by': c.waived_by})
    order = {'renewal': 0, 'charge': 1, 'waived': 2, 'payment': 3}
    rows.sort(key=lambda x: (x['at'], order[x['kind']]))
    bal = Decimal('0')
    for x in rows:
        bal = max(Decimal('0'), bal + x['charged'] - x['paid'])
        x['balance'] = bal
    paid = sum((x['paid'] for x in rows), Decimal('0'))
    charged = sum((x['charged'] for x in rows if x['kind'] != 'waived'), Decimal('0'))
    return list(reversed(rows)), paid, charged


@login_required
def member_detail(request, pk):
    from . import members as mem
    from . import voucher_history as vh
    from .models import VoucherEvent
    from .models_member_plans import MemberRenewal
    from .views import _time_pct
    from .views_agents import MANUAL_METHODS
    business = _b(request)
    v = _member(request, pk)
    now = timezone.now()
    end = vh.ends_at(v)
    state_key, state_label = vh.display_state(v, now)
    plan = mem.plan_for_member(v)
    pref = mem.notification_settings_for(v)
    shown = request.session.pop(PW_SESSION, None)
    if shown and shown.get('pk') != v.pk:
        shown = None
    try:
        from .member_arrears import balance_rows, total_arrears
        arrears_all = balance_rows(v)
        arrears, arrears_rows = total_arrears(v), [r for r in arrears_all if r['due'] > 0][:10]
    except Exception:
        arrears, arrears_rows, arrears_all = None, [], []
    ledger, paid_total, charged_total = _ledger(v)
    left_frozen = v.frozen_left if v.frozen_at else None
    schedules = list(v.member_schedules.select_related('created_by').order_by('-created_at')[:12])
    return render(request, 'core/member_detail.html', {
        'v': v, 'plan': plan, 'pref': pref, 'email': (pref.email if pref else ''),
        'state_key': state_key, 'state_label': state_label, 'ends_at': end,
        'time_left': (end - now) if end and end > now else None, 'time_pct': _time_pct(v, end, now),
        'frozen_left_minutes': (left_frozen // 60) if left_frozen else None,
        'pending': [s for s in schedules if s.status == 'pending'], 'schedules': [s for s in schedules if s.status != 'pending'][:8],
        'plans': business.member_plans.filter(active=True).order_by('name'),
        'bindings': v.device_bindings.order_by('slot_no'),
        'security': ms.security(v),
        'renewals': MemberRenewal.objects.filter(member=v).order_by('-renewed_at')[:10],
        'arrears': arrears, 'arrears_rows': arrears_rows,
        'cur': business.currency or 'D',
        'charge_kinds': _charge_kinds(),
        'open_charges': [r for r in arrears_all if r.get('charge') is not None and r['due'] > 0],
        'ledger': ledger[:15], 'paid_total': paid_total, 'charged_total': charged_total,
        'notes': VoucherEvent.objects.filter(voucher=v, event='note', detail__support_note=True).select_related('user').order_by('-created_at')[:20],
        'timeline': vh.timeline(v, now)[:60],
        'channel': vh.channel(v.router), 'shown_password': shown.get('pw') if shown else '',
        'shown_emailed': shown.get('emailed', '') if shown else '',
        'methods': list(MANUAL_METHODS), 'agents': business.agents.filter(active=True).order_by('name'),
        'can_support': _can(request, 'vouchers.support'), 'can_create': _can(request, 'vouchers.create'),
        'now_local': timezone.localtime(now),
    })


@login_required
@require_POST
def member_support(request, pk):
    """Every control on the member page posts here with an ``action``."""
    v = _member(request, pk)
    if v.deleted_at:
        messages.error(request, f'{v.code} is in the bin — restore it before changing anything.')
        return redirect('member_detail', pk=v.pk)
    act = request.POST.get('action', '')
    user = request.user
    reason = request.POST.get('reason', '').strip()
    anchor = {'pause_now': 'pause', 'unpause': 'pause', 'schedule': 'pause', 'schedule_resume': 'pause', 'cancel': 'pause',
              'password': 'access', 'portal_link': 'access', 'portal_signout': 'access', 'wifi_signout': 'access',
              'devices': 'devices', 'block': 'security', 'unblock': 'security', 'details': 'details', 'plan': 'details',
              'note': 'notes', 'charge': 'billing', 'waive': 'billing'}.get(act, '')
    back = redirect(reverse('member_detail', args=[v.pk]) + (f'#{anchor}' if anchor else ''))
    try:
        if act == 'pause_now':
            ms.pause_now(v, reason, user=user)
            messages.success(request, f'{v.code} is paused. Their time left stands still until you unpause.')
        elif act == 'unpause':
            ms.unpause_now(v, user=user, reason=reason)
            v.refresh_from_db()
            messages.success(request, f'{v.code} is back on.' + (f' Their time now ends {timezone.localtime(v.expires_at):%d %b %Y %H:%M}.'
                                                                  if v.expires_at else ''))
        elif act == 'schedule':
            mode = request.POST.get('when_mode', 'date')
            pause, resume = ms.schedule_pause(
                v, when=_when(request.POST.get('pause_at')) if mode == 'date' else None,
                days_left=request.POST.get('days_left') if mode == 'days' else None,
                resume_at=_when(request.POST.get('resume_at')), reason=reason, user=user)
            messages.success(request, f'{v.code} will be paused on {timezone.localtime(pause.run_at):%d %b %Y %H:%M}'
                             + (f' and unpaused on {timezone.localtime(resume.run_at):%d %b %Y %H:%M}.' if resume else ' and stay paused until you unpause.'))
        elif act == 'schedule_resume':
            when = _when(request.POST.get('resume_at'))
            if not when:
                raise ms.SupportError('Choose when to unpause.')
            s = ms.schedule_resume(v, when=when, reason=reason, user=user)
            messages.success(request, f'{v.code} will be unpaused on {timezone.localtime(s.run_at):%d %b %Y %H:%M}.')
        elif act == 'cancel':
            s = ms.cancel_schedule(v, request.POST.get('schedule'), user=user)
            messages.success(request, f'{s.get_action_display()} schedule cancelled.')
        elif act == 'password':
            pw, result, emailed = ms.reset_password(v, mode=request.POST.get('mode', 'random'), password=request.POST.get('password', ''),
                                                    reason=reason, user=user, email=bool(request.POST.get('email')))
            request.session[PW_SESSION] = {'pk': v.pk, 'pw': pw, 'emailed': emailed}
            messages.success(request, f'Password of {v.code} reset. {result}.')
        elif act == 'portal_link':
            to = ms.send_portal_link(v, request=request, user=user)
            messages.success(request, f'Member-portal access link sent to {to}.')
        elif act == 'portal_signout':
            ms.portal_sign_out(v, user=user, reason=reason)
            messages.success(request, f'{v.code} is signed out of the member portal on every device.')
        elif act == 'wifi_signout':
            ok, result = ms.wifi_sign_out(v, user=user, reason=reason)
            (messages.success if ok else messages.warning)(request, f'{v.code}: {result}.')
        elif act == 'devices':
            ok, result = ms.reset_devices(v, user=user, reason=reason)
            (messages.success if ok else messages.warning)(request, f'Device slots of {v.code} freed — the next devices to log in take them. {result}.')
        elif act == 'block':
            ok, result = ms.block(v, user=user, reason=reason)
            (messages.success if ok else messages.warning)(request, f'{v.code} is blocked. {result}.')
        elif act == 'unblock':
            ok, result = ms.unblock(v, user=user, reason=reason)
            (messages.success if ok else messages.warning)(request, f'{v.code} is unblocked. {result}.')
        elif act == 'details':
            changed = ms.update_details(v, request.POST, user=user)
            messages.success(request, ('Saved: ' + ', '.join(changed) + '.') if changed else 'Nothing changed.')
        elif act == 'plan':
            if not _can(request, 'vouchers.create'):
                raise ms.SupportError('Your role cannot change Member Plans.')
            plan, result = ms.change_plan(v, request.POST.get('plan'), user=user)
            messages.success(request, f'{v.code} is now on {plan.name} — {result}.')
        elif act in ('charge', 'waive'):
            if not _can(request, 'vouchers.create'):
                raise ms.SupportError('Your role cannot add charges or take payments.')
            from . import member_arrears as ma
            try:
                if act == 'charge':
                    collect = None
                    if request.POST.get('pay_now'):
                        from .views_agents import MANUAL_METHODS
                        method = request.POST.get('method') if request.POST.get('method') in dict(MANUAL_METHODS) else 'cash'
                        collect = {'amount': request.POST.get('paid_amount') or request.POST.get('amount'), 'method': method,
                                   'reference': request.POST.get('reference', ''),
                                   'agent': _b(request).agents.filter(pk=request.POST.get('agent') or 0).first()}
                    charge, payment = ma.add_charge(v, kind=request.POST.get('kind', 'late_fee'), amount=request.POST.get('amount'),
                                                    description=request.POST.get('description', ''), user=user, collect=collect)
                    if payment:
                        messages.success(request, f'{charge.label} of {payment.currency}{charge.amount:,.2f} added and '
                                                  f'{payment.currency}{payment.amount_collected:,.2f} collected. '
                                                  f'Still owed: {payment.currency}{payment.balance_after:,.2f}. No time was added.')
                        from urllib.parse import urlencode
                        return redirect(reverse('members') + '?' + urlencode({'balance_receipt': payment.pk,
                                                                              'next': reverse('member_detail', args=[v.pk]) + '#billing'}))
                    messages.success(request, f'{charge.label} of {charge.currency}{charge.amount:,.2f} added to {v.code}’s account — '
                                              f'it shows as owed until paid. No time was added.')
                else:
                    charge, due = ma.waive_charge(v, request.POST.get('charge'), reason=reason, user=user)
                    messages.success(request, f'{charge.label}: {charge.currency}{due:,.2f} waived.')
            except ma.BalanceError as exc:
                raise ms.SupportError(str(exc))
        elif act == 'note':
            ms.add_note(v, request.POST.get('text', ''), user=user)
            messages.success(request, 'Note added.')
        else:
            messages.error(request, 'Unknown action.')
    except ms.SupportError as exc:
        messages.error(request, str(exc))
    return back
