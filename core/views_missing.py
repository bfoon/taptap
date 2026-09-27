from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models_missing import MissingVoucherReport
from .utils import log


def _business(request):
    return request.user.business


def _safe_next(request, default='missing_vouchers'):
    nxt = request.POST.get('next', '').strip()
    return nxt if nxt.startswith('/') and not nxt.startswith('//') else default


def _parse_ids(raw):
    ids = []
    for part in str(raw or '').split(','):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return list(dict.fromkeys(ids))


@login_required
def missing_vouchers(request):
    business = _business(request)
    reports = list(
        business.missing_voucher_reports
        .select_related('batch', 'reported_by', 'resolved_by')
        .order_by('-reported_at')
    )

    open_reports = [r for r in reports if r.status == 'open']
    resolved_reports = [r for r in reports if r.status == 'resolved'][:100]

    return render(
        request,
        'core/missing_vouchers.html',
        {
            'open_reports': open_reports,
            'resolved_reports': resolved_reports,
        },
    )


@login_required
@require_POST
def report_missing_vouchers(request):
    business = _business(request)
    ids = _parse_ids(request.POST.get('ids'))

    vouchers = list(
        business.vouchers
        .filter(id__in=ids)
        .select_related('batch')
        .order_by('id')
    )

    if not vouchers:
        messages.error(request, 'Select at least one voucher to report as missing.')
        return redirect(_safe_next(request, 'vouchers'))

    codes = [v.code for v in vouchers]
    batch_ids = {v.batch_id for v in vouchers if v.batch_id}
    all_have_batch = all(v.batch_id for v in vouchers)

    batch = None
    if all_have_batch and len(batch_ids) == 1:
        batch = vouchers[0].batch

    report_type = 'single' if len(vouchers) == 1 else 'group'

    report = MissingVoucherReport.objects.create(
        business=business,
        batch=batch,
        report_type=report_type,
        voucher_codes=codes,
        voucher_count=len(codes),
        details=(request.POST.get('details') or '').strip(),
        action_state='done' if request.POST.get('action_state') == 'done' else 'planned',
        action_notes=(request.POST.get('action_notes') or '').strip(),
        reference=(request.POST.get('reference') or '').strip()[:160],
        reported_by=request.user,
    )

    log(
        business,
        'Missing Voucher Report',
        f'{report.voucher_count} voucher(s) reported missing'
        + (f' from {batch.name}' if batch else ''),
    )
    messages.warning(
        request,
        f'{report.voucher_count} voucher{"s" if report.voucher_count != 1 else ""} reported missing.',
    )
    return redirect(_safe_next(request, 'missing_vouchers'))


@login_required
@require_POST
def report_missing_batch(request, pk):
    business = _business(request)
    batch = get_object_or_404(
        business.batches.prefetch_related('vouchers'),
        pk=pk,
    )
    codes = list(batch.vouchers.order_by('id').values_list('code', flat=True))

    if not codes:
        messages.error(request, 'This batch has no vouchers to report.')
        return redirect(_safe_next(request, 'batches'))

    report = MissingVoucherReport.objects.create(
        business=business,
        batch=batch,
        report_type='batch',
        voucher_codes=codes,
        voucher_count=len(codes),
        details=(request.POST.get('details') or '').strip(),
        action_state='done' if request.POST.get('action_state') == 'done' else 'planned',
        action_notes=(request.POST.get('action_notes') or '').strip(),
        reference=(request.POST.get('reference') or '').strip()[:160],
        reported_by=request.user,
    )

    log(
        business,
        'Missing Batch Report',
        f'{batch.name}: {report.voucher_count} voucher(s) reported missing',
    )
    messages.warning(
        request,
        f'Batch {batch.name} reported missing ({report.voucher_count} vouchers).',
    )
    return redirect(_safe_next(request, 'missing_vouchers'))


@login_required
@require_POST
def resolve_missing_report(request, pk):
    business = _business(request)
    report = get_object_or_404(
        business.missing_voucher_reports,
        pk=pk,
    )

    report.status = 'resolved'
    report.resolved_by = request.user
    report.resolved_at = timezone.now()
    report.resolution_notes = (request.POST.get('resolution_notes') or '').strip()
    report.save(
        update_fields=[
            'status',
            'resolved_by',
            'resolved_at',
            'resolution_notes',
        ]
    )

    log(
        business,
        'Missing Voucher Report Resolved',
        f'Report #{report.id}: {report.voucher_count} voucher(s)',
    )
    messages.success(request, f'Missing voucher report #{report.id} marked resolved.')
    return redirect(_safe_next(request, 'missing_vouchers'))
