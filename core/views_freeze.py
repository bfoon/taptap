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
