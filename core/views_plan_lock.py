"""Owner-only lock / seal / reopen of a voucher plan (rules in core/voucher_plan_lock.py)."""
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import voucher_plan_lock as vpl


def _until(value):
    """<input type=datetime-local> or a date, in the business's local time → aware datetime, or None."""
    v = str(value or '').strip()
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v if 'T' in v or ' ' in v else v + 'T00:00')
    except ValueError:
        raise vpl.PlanClosedError('That reopening date is not valid.')
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


@login_required
@require_POST
def plan_sale_lock(request, pk):
    business = request.user.business
    plan = get_object_or_404(business.plans, pk=pk)
    f = request.POST
    try:
        if f.get('action') == 'unlock':
            vpl.unlock(plan, request)
            messages.success(request, f'🔓 “{plan.name}” is open again: new vouchers can be made with it.')
        else:
            kind = 'sealed' if f.get('kind') == 'sealed' else 'locked'
            rid = f.get('replacement') or ''
            replacement = (vpl.open_plans(business.plans.filter(active=True)).exclude(pk=plan.pk).filter(pk=rid).first()
                           if str(rid).isdigit() else None)
            vpl.lock(plan, request, kind=kind, reason=f.get('reason', ''), until=_until(f.get('until')) if kind == 'locked' else None,
                     replacement=replacement, confirm=f.get('confirm', ''))
            s = vpl.sunset(plan)
            head = '🔒 “%s” is %s.' % (plan.name, 'sealed for good' if kind == 'sealed' else 'locked')
            body = (f' No new vouchers can be made with it anywhere — Generate, the app, agents, the online shop, the portal or the Market.'
                    f' The {s["total"]} voucher{"s" if s["total"] != 1 else ""} already out there keep working until they run out.')
            if plan.sale_lock_until:
                body += f' It reopens by itself on {timezone.localtime(plan.sale_lock_until):%d %b %Y %H:%M}.'
            if replacement:
                body += f' Staff will be pointed to “{replacement.name}”.'
            messages.success(request, head + body)
    except vpl.PlanClosedError as e:
        messages.error(request, str(e))
    return redirect('plans')
