"""Roll a voucher back to its full time (Owner / Admin only) — see core/voucher_rollback.py."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from . import voucher_rollback as vr


@login_required
@require_POST
def voucher_rollback(request, pk):
    # TeamAccessMiddleware already refuses this URL without vouchers.rollback; checked again here so the
    # rule holds even if the URL map changes (Owner and Admin are the only roles that have it).
    if 'vouchers.rollback' not in getattr(request, 'tt_perms', frozenset()):
        messages.error(request, 'Only the Owner or an Admin can roll a voucher back.')
        return redirect('voucher_detail', pk=pk)
    v = get_object_or_404(request.user.business.vouchers.select_related('router'), pk=pk)
    try:
        ok, result = vr.rollback(v, request.user, request.POST.get('reason', ''))
    except vr.RollbackError as e:
        messages.error(request, str(e))
    else:
        from django.utils import timezone
        messages.success(request, f'{v.code} rolled back to its full time — it now ends '
                                  f'{timezone.localtime(v.expires_at):%d %b %Y %H:%M}.')
        if result:
            (messages.info if ok else messages.warning)(request, result)
    return redirect('voucher_detail', pk=pk)
