"""Delete vouchers and batches to the recycle bin, and browse the bin (read-only)."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import Voucher, VoucherBatch, VoucherPlan
from .utils import code_search_q
from .voucher_bin import BinError, delete_batch, delete_plan, delete_vouchers


def _b(request): return request.user.business


def _back(request, default='vouchers'):
    nxt = request.POST.get('next', '')
    if nxt.startswith('/') and not nxt.startswith('//'):
        return redirect(nxt)
    return redirect(default)


def _report(request, s, what):
    """Turn a delete summary into messages."""
    cur = request.user.business.currency
    if s['deleted']:
        msg = f'{s["deleted"]} {what} moved to the bin.'
        if s['sales_removed']:
            msg += f' {s["sales_removed"]} sale(s) worth {cur}{s["sales_amount"]:,.2f} were taken out of finance.'
        messages.success(request, msg)
    for name, ok, text in s['router']:
        (messages.info if ok else messages.warning)(request, text)
    if s['skipped']:
        used = [c for c, why in s['skipped'] if 'used' in why]
        other = [f'{c} ({why})' for c, why in s['skipped'] if 'used' not in why]
        if used:
            messages.warning(request, f'{len(used)} used voucher(s) kept — only unused vouchers can be deleted: '
                                      + ', '.join(used[:8]) + ('…' if len(used) > 8 else ''))
        if other:
            messages.warning(request, 'Not deleted: ' + ', '.join(other[:8]) + ('…' if len(other) > 8 else ''))
    if not s['deleted'] and not s['skipped']:
        messages.info(request, 'Nothing was deleted.')


@login_required
@require_POST
def voucher_delete(request, pk):
    business = _b(request)
    v = get_object_or_404(Voucher.all_objects.filter(business=business).select_related('router', 'agent', 'batch'), pk=pk)
    if v.deleted_at:
        messages.info(request, f'{v.code} is already in the bin. Items in the bin cannot be deleted again.')
        return redirect('voucher_bin')
    try:
        s = delete_vouchers(business, [v], user=request.user, reason=request.POST.get('reason', ''))
    except BinError as e:
        messages.error(request, str(e)); return redirect('voucher_detail', pk=pk)
    _report(request, s, 'voucher')
    return redirect('voucher_bin') if s['deleted'] else redirect('voucher_detail', pk=pk)


@login_required
@require_POST
def vouchers_delete(request):
    business = _b(request)
    ids = [int(x) for x in request.POST.get('ids', '').split(',') if x.strip().isdigit()][:1000]
    if not ids:
        messages.error(request, 'Select the vouchers to delete first.'); return _back(request)
    vs = list(business.vouchers.filter(pk__in=ids).select_related('router', 'agent', 'batch'))
    try:
        s = delete_vouchers(business, vs, user=request.user, reason=request.POST.get('reason', ''))
    except BinError as e:
        messages.error(request, str(e)); return _back(request)
    _report(request, s, 'voucher(s)')
    return _back(request)


@login_required
@require_POST
def batch_delete(request, pk):
    business = _b(request)
    batch = get_object_or_404(business.batches.select_related('plan', 'agent'), pk=pk)
    try:
        s = delete_batch(batch, user=request.user, reason=request.POST.get('reason', ''))
    except BinError as e:
        messages.error(request, str(e)); return redirect('batches')
    _report(request, s, f'voucher(s) from {batch.name}')
    if s['batch_binned']:
        messages.success(request, f'Batch {batch.name} is in the bin.')
    elif s.get('kept_used'):
        messages.info(request, f'{batch.name} stays in your batches with its {s["kept_used"]} used voucher(s).')
    return redirect('batches')


@login_required
@require_POST
def plan_delete(request, pk):
    business = _b(request)
    plan = get_object_or_404(business.plans, pk=pk)
    try:
        s = delete_plan(plan, user=request.user, reason=request.POST.get('reason', ''), perms=getattr(request, 'tt_perms', frozenset()))
    except BinError as e:
        messages.error(request, str(e)); return redirect('plans')
    _report(request, s, f'unused voucher(s) of {plan.name}')
    msg = f'Plan {plan.name} is in the bin.'
    if s['kept_used']:
        msg += f' Its {s["kept_used"]} used voucher(s) stay as history and keep working until their time runs out.'
    if s['batches_binned']:
        msg += f' {len(s["batches_binned"])} empty batch(es) went to the bin with it.'
    messages.success(request, msg)
    from .portal_deploy import schedule_redeploy; schedule_redeploy(business)
    return redirect('plans')


@login_required
def voucher_bin(request):
    """Everything deleted: read-only. Nothing here can be deleted again or restored."""
    business = _b(request)
    tab = request.GET.get('tab') if request.GET.get('tab') in ('batches', 'plans') else 'vouchers'
    q = request.GET.get('q', '').strip()
    vouchers = (Voucher.all_objects.binned().filter(business=business)
                .select_related('deleted_by', 'router', 'batch').order_by('-deleted_at'))
    batches = (VoucherBatch.all_objects.binned().filter(business=business)
               .select_related('deleted_by', 'plan', 'agent').order_by('-deleted_at'))
    plans = VoucherPlan.all_objects.binned().filter(business=business).select_related('deleted_by').order_by('-deleted_at')
    if q:
        vouchers = vouchers.filter(code_search_q(q, 'code', ('plan_name', 'delete_reason', 'batch__name')))
        batches = batches.filter(Q(name__icontains=q) | Q(delete_reason__icontains=q))
        plans = plans.filter(Q(name__icontains=q) | Q(delete_reason__icontains=q))
    counts = {'vouchers': vouchers.count(), 'batches': batches.count(), 'plans': plans.count()}
    page = Paginator({'vouchers': vouchers, 'batches': batches, 'plans': plans}[tab], 50).get_page(request.GET.get('page'))
    return render(request, 'core/voucher_bin.html', {'tab': tab, 'q': q, 'page_obj': page, 'counts': counts,
                                                     'pending': Voucher.all_objects.binned().filter(business=business, router_removal__in=['queued', 'failed']).count()})


@login_required
@require_POST
def voucher_set_profile(request, pk):
    """Choose the hotspot user profile a voucher uses on its router (fixes "unknown user profile")."""
    from .voucher_history import record
    from .voucher_push import push_vouchers
    business = _b(request)
    v = get_object_or_404(business.vouchers.select_related('router'), pk=pk)
    choice = request.POST.get('profile', '')
    before = v.router_profile or '(from plan)'
    if choice.startswith('plan:'):
        plan = business.plans.filter(pk=choice[5:] if choice[5:].isdigit() else 0).first()
        if not plan:
            messages.error(request, 'That plan does not exist any more.'); return redirect('voucher_detail', pk=pk)
        v.plan_name, v.router_profile = plan.name, ''
        if not v.used_at and not v.expires_at:
            # not started yet: it takes the plan's devices and length too
            v.max_devices, v.duration_minutes = plan.max_devices, plan.duration_minutes
        after = f'plan {plan.name} ({plan.mikrotik_profile_name or plan.name})'
    elif choice.startswith('router:'):
        name = choice[7:].strip()[:120]
        if not name:
            messages.error(request, 'Choose a profile.'); return redirect('voucher_detail', pk=pk)
        v.router_profile = name
        after = name
    else:
        v.router_profile = ''
        after = '(from plan)'
    # Only the profile changes: used_at / expires_at are never touched, and the router user is updated in
    # place ("set"), so a voucher in use keeps the time it already used — the clock does not restart.
    v.save(update_fields=['plan_name', 'router_profile', 'max_devices', 'duration_minutes'])
    in_use = bool(v.used_at or v.expires_at)
    record(v, 'note', user=request.user, text=f'Router profile changed: {before} → {after}' + (' (in use — time already used kept)' if in_use else ''))
    msgs = push_vouchers([v], request.user)
    messages.success(request, f'{v.code} now uses {after} on the router.' + (' Its clock keeps running from where it was.' if in_use else '')
                     + (' ' + ' '.join(msgs.values()) if msgs else ''))
    return redirect('voucher_detail', pk=pk)


@login_required
@require_POST
def voucher_archive_now(request, pk):
    """Archive an expired voucher now: removed from its router, kept in TapTap (history, sale).
    The fix for an expired voucher whose router profile no longer exists — nothing to send any more."""
    from .voucher_history import record, time_is_up
    business = _b(request)
    v = get_object_or_404(business.vouchers.select_related('router'), pk=pk)
    if not (v.status == 'expired' or time_is_up(v)):
        messages.error(request, f'{v.code} still has time. Change its profile instead — the clock keeps running.')
        return redirect('voucher_detail', pk=pk)
    msg = 'No router copy.'
    if v.router_id:
        from .voucher_bin import remove_from_router
        ok, msg = remove_from_router(v.router, [v], user=request.user)
    from .models import Voucher
    Voucher.objects.filter(pk=v.pk).update(status='archived', mikrotik_sync_status='Synced', mikrotik_sync_error='')
    record(v, 'archived', user=request.user, reason='Archived by hand', status_before=v.status, status_after='archived', router_result=msg)
    messages.success(request, f'{v.code} archived — off the router, kept in TapTap. {msg}')
    return redirect('voucher_detail', pk=pk)
