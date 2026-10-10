"""Freeze / unfreeze a voucher, a selection of vouchers, or a whole batch."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from . import voucher_freeze as vf


def _b(request): return request.user.business


def _say(request, out, verb):
    for level, msg in vf.summary_messages(out, verb):
        getattr(messages, level)(request, msg)


def _back(request, default):
    nxt = request.POST.get('next', '')
    return redirect(nxt if nxt.startswith('/') and not nxt.startswith('//') else default)


def _run(request, vouchers, default):
    action = request.POST.get('action')
    try:
        if action == 'freeze':
            _say(request, vf.freeze(vouchers, request.user, request.POST.get('reason', '')), 'frozen — time stands still until unfrozen')
        elif action == 'unfreeze':
            _say(request, vf.unfreeze(vouchers, request.user, request.POST.get('reason', '')), 'unfrozen — time continues from where it stopped')
        else:
            messages.error(request, 'Unknown action.')
    except vf.FreezeError as e:
        messages.error(request, str(e))
    return _back(request, default)


@login_required
@require_POST
def voucher_freeze(request, pk):
    v = get_object_or_404(_b(request).vouchers.select_related('router'), pk=pk)
    return _run(request, [v], f'/vouchers/{pk}/')


@login_required
@require_POST
def vouchers_freeze(request):
    ids = [int(x) for x in request.POST.get('ids', '').split(',') if x.strip().isdigit()][:1000]
    if not ids:
        messages.error(request, 'Select the vouchers first.'); return _back(request, 'vouchers')
    return _run(request, list(_b(request).vouchers.filter(pk__in=ids).select_related('router')), 'vouchers')


@login_required
@require_POST
def batch_freeze(request, pk):
    batch = get_object_or_404(_b(request).batches, pk=pk)
    qs = batch.vouchers.select_related('router')
    qs = qs.filter(frozen_at__isnull=False) if request.POST.get('action') == 'unfreeze' else qs.filter(frozen_at__isnull=True)
    return _run(request, list(qs), 'batches')


# ─────────────────────────── manual warning (Owner / Admin) ───────────────────────────

def _warn(request, vouchers, default):
    try:
        out = vf.freeze(vouchers, request.user, request.POST.get('reason', ''), kind='warning',
                        message=request.POST.get('message', '').strip() or vf.MANUAL_WARNING)
        _say(request, out, 'warned — internet paused until the customer reads the message and presses "I agree"')
    except vf.FreezeError as e:
        messages.error(request, str(e))
    return _back(request, default)


@login_required
@require_POST
def voucher_warn(request, pk):
    """Warn the customer of one voucher (every device using it)."""
    v = get_object_or_404(_b(request).vouchers.select_related('router'), pk=pk)
    return _warn(request, [v], f'/vouchers/{pk}/')


@login_required
@require_POST
def session_warn(request):
    """Warn from Active Users: find the voucher the device is logged in with."""
    from .models import VoucherCodeAlias
    business = _b(request)
    code = request.POST.get('user', '').strip()
    v = business.vouchers.select_related('router').filter(code__iexact=code).first() if code else None
    if not v and code:
        alias = VoucherCodeAlias.objects.filter(business=business, code__iexact=code, voucher__deleted_at__isnull=True).select_related('voucher__router').first()
        v = alias.voucher if alias else None
    if not v:
        messages.error(request, f'{code or "This device"} is not logged in with a TapTap voucher, so it cannot be warned. '
                                'You can disconnect it instead.')
        return _back(request, 'active_users')
    return _warn(request, [v], 'active_users')


@login_required
@require_POST
def voucher_time_point(request, pk):
    """The voucher time bar was clicked: end the time at that point, plan a freeze there, or cancel a planned freeze."""
    from .voucher_schedule import TimePointError, cancel_plan, end_at, parse_point, plan_freeze
    from .voucher_freeze import FreezeError
    from .voucher_history import VoucherActionError
    v = get_object_or_404(request.user.business.vouchers.select_related('router', 'business'), pk=pk)
    action = request.POST.get('action', '')
    try:
        if action == 'cancel_freeze':
            msg = cancel_plan(v, request.user)
        else:
            when = parse_point(request.POST.get('at', ''))
            reason = request.POST.get('reason', '')
            if action == 'end':
                msg = end_at(v, when, request.user, reason)
            elif action == 'freeze':
                msg = plan_freeze(v, when, request.user, reason)
            else:
                raise TimePointError('Choose what should happen at that point.')
        messages.success(request, msg)
    except (TimePointError, FreezeError, VoucherActionError) as exc:
        messages.error(request, str(exc))
    return redirect('voucher_detail', pk=v.pk)
