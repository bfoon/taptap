"""Delete vouchers and batches to the recycle bin, and browse the bin (read-only)."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import Voucher, VoucherBatch
from .voucher_bin import BinError, delete_batch, delete_vouchers


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
def voucher_bin(request):
    """Everything deleted: read-only. Nothing here can be deleted again or restored."""
    business = _b(request)
    tab = 'batches' if request.GET.get('tab') == 'batches' else 'vouchers'
    q = request.GET.get('q', '').strip()
    vouchers = (Voucher.all_objects.binned().filter(business=business)
                .select_related('deleted_by', 'router', 'batch').order_by('-deleted_at'))
    batches = (VoucherBatch.all_objects.binned().filter(business=business)
               .select_related('deleted_by', 'plan', 'agent').order_by('-deleted_at'))
    if q:
        vouchers = vouchers.filter(Q(code__icontains=q) | Q(plan_name__icontains=q) | Q(delete_reason__icontains=q) | Q(batch__name__icontains=q))
        batches = batches.filter(Q(name__icontains=q) | Q(delete_reason__icontains=q))
    counts = {'vouchers': vouchers.count(), 'batches': batches.count()}
    page = Paginator(vouchers if tab == 'vouchers' else batches, 50).get_page(request.GET.get('page'))
    return render(request, 'core/voucher_bin.html', {'tab': tab, 'q': q, 'page_obj': page, 'counts': counts,
                                                     'pending': Voucher.all_objects.binned().filter(business=business, router_removal__in=['queued', 'failed']).count()})
