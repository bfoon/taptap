"""Voucher fix — "Why can't it connect?" and the Fix-it button (core/voucher_unstick.py).

GET  vouchers/<pk>/fix/            findings (what stops it) · ?cmd=<id> → the router's read-back of a running fix
POST vouchers/<pk>/fix/            run the fix (Reset devices + router command, read back)
GET  vouchers/fix/find/?code=X     Network tools: look a code up (same answer as GET above)

All need voucher support rights (core/permissions.URL_PERMS), like Reset devices.
"""
import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from . import voucher_unstick as vu
from .voucher_history import channel, display_state


def _b(request):
    return request.user.business


def _summary(v):
    key, label = display_state(v)
    return {'id': v.pk, 'code': v.code, 'state': key, 'state_label': label, 'router': v.router.name if v.router_id else '',
            'via': channel(v.router), 'member': v.is_member, 'url': f'/vouchers/{v.pk}/'}


def _answer(v):
    found = vu.findings(v)
    return {'ok': True, 'voucher': _summary(v), 'findings': found,
            'blocked': vu.blocking(v), 'fixable': not vu.blocking(v),
            'clean': not any(f['level'] in ('bad', 'warn') for f in found)}


@login_required
def voucher_fix(request, pk):
    v = _b(request).vouchers.select_related('router', 'business').filter(pk=pk).first()
    if v is None:
        return JsonResponse({'ok': False, 'error': 'That voucher is gone.'}, status=404)
    if request.method == 'POST':
        try:
            reason = str((json.loads(request.body or b'{}') if request.content_type == 'application/json' else request.POST).get('reason') or '')
        except ValueError:
            reason = ''
        out = vu.unstick(v, request.user, reason.strip()[:200])
        v.refresh_from_db()
        out['voucher'] = _summary(v)
        out['findings_after'] = vu.findings(v)
        return JsonResponse(out)
    cmd = request.GET.get('cmd')
    if cmd:
        try:
            return JsonResponse({'ok': True, **vu.status(v, int(cmd))})
        except ValueError:
            return JsonResponse({'ok': False, 'error': 'Bad command.'}, status=400)
    return JsonResponse(_answer(v))


@login_required
@require_GET
def voucher_fix_find(request):
    code = str(request.GET.get('code') or '').strip()
    if not code:
        return JsonResponse({'ok': False, 'error': 'Type a voucher code or member username.'}, status=400)
    qs = _b(request).vouchers.select_related('router', 'business')
    v = qs.filter(code__iexact=code, deleted_at__isnull=True).first() or qs.filter(code__iexact=code).first()
    if v is None:
        try:
            from .models import VoucherCodeAlias
            alias = VoucherCodeAlias.objects.filter(business=_b(request), code__iexact=code).select_related('voucher').first()
            v = alias.voucher if alias else None
        except Exception:
            v = None
    if v is None:
        return JsonResponse({'ok': False, 'error': f'No voucher or member "{code}" in this business.'}, status=404)
    return JsonResponse(_answer(v))
