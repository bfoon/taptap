"""Routers › Move: send a MikroTik to another business (core/router_move.py does the work)."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from . import router_move as rm


def _b(request):
    return request.user.business


@login_required
def router_move_preview(request, pk):
    """JSON for the Move dialog: where it can go and what goes with it."""
    business = _b(request)
    router = get_object_or_404(business.routers, pk=pk)
    with_vouchers = request.GET.get('vouchers', '1') != '0'
    data = {'success': True, 'preview': rm.preview(router, with_vouchers), 'targets': rm.targets(request.user, business),
            'this_business': {'id': business.pk, 'name': business.business_name}}
    bid = request.GET.get('business_id', '').strip()
    if bid:
        try:
            t = rm.find_target(request.user, business, bid)
            data['target'] = {'id': t.pk, 'name': t.business_name, 'routers': t.routers.count()}
        except rm.MoveRefused as exc:
            data['target_error'] = str(exc)
    return JsonResponse(data)


@login_required
@require_POST
def router_move(request, pk):
    business = _b(request)
    router = get_object_or_404(business.routers, pk=pk)
    typed = (request.POST.get('confirm_name') or '').strip()
    if typed.lower() != router.name.strip().lower():
        messages.error(request, f'Not moved: type the router’s name exactly ({router.name}) to confirm.')
        return redirect('routers')
    try:
        target = rm.find_target(request.user, business, request.POST.get('business_id', ''))
        summary = rm.move(router, target, request.user, with_vouchers=request.POST.get('with_vouchers', 'on') == 'on')
    except rm.MoveRefused as exc:
        messages.error(request, f'Not moved: {exc}')
        return redirect('routers')
    extra = f' with {summary["vouchers"]} vouchers' if summary['vouchers'] else ''
    copied = f' {summary["plans_copied"]} plan(s) were copied there.' if summary['plans_copied'] else ''
    messages.success(request, f'{router.name} now belongs to {target.business_name} (#{target.pk}){extra}. Sales and cash '
                              f'stay here.{copied} Its TapTap Link keeps working — nothing to change on the MikroTik.')
    for w in summary['warnings'][:3]:
        messages.warning(request, w)
    return redirect('routers')
