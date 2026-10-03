from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models_voucher_entry import VoucherEntryDevice, VoucherEntryPolicy


def _business(request):
    return request.user.business


def _can_manage(request):
    perms = getattr(request, 'tt_perms', frozenset())
    return (
        request.user == request.user.business.user
        or 'network.manage' in perms
        or request.user.is_superuser
    )


def _bounded_int(value, default, minimum, maximum):
    try:
        number = int(str(value or '').strip())
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


@login_required
@require_POST
def voucher_entry_policy_save(request):
    if not _can_manage(request):
        messages.error(request, 'You do not have permission to change network security settings.')
        return redirect('security')

    business = _business(request)

    max_attempts = _bounded_int(
        request.POST.get('max_attempts'),
        5,
        2,
        50,
    )
    warning_remaining = _bounded_int(
        request.POST.get('warning_remaining'),
        2,
        1,
        max_attempts - 1,
    )
    window_minutes = _bounded_int(
        request.POST.get('window_minutes'),
        10,
        1,
        1440,
    )
    block_minutes = _bounded_int(
        request.POST.get('block_minutes'),
        30,
        1,
        10080,
    )

    policy, _ = VoucherEntryPolicy.objects.update_or_create(
        business=business,
        defaults={
            'enabled': request.POST.get('enabled') == 'on',
            'max_attempts': max_attempts,
            'warning_remaining': warning_remaining,
            'window_minutes': window_minutes,
            'block_minutes': block_minutes,
            'warning_text': (request.POST.get('warning_text') or '').strip()[:1000],
            'blocked_text': (request.POST.get('blocked_text') or '').strip()[:1500],
        },
    )

    if policy.enabled:
        messages.success(
            request,
            (
                'Voucher-entry protection is ON. '
                f'Devices are blocked after {policy.max_attempts} wrong entries '
                f'within {policy.window_minutes} minute'
                f'{"s" if policy.window_minutes != 1 else ""}.'
            ),
        )
    else:
        messages.success(
            request,
            'Voucher-entry protection is OFF. Existing temporary blocks remain visible but no new attempts are counted.',
        )

    return redirect('/security/#voucher-entry-security')


@login_required
@require_POST
def voucher_entry_unblock(request, pk):
    if not _can_manage(request):
        messages.error(request, 'You do not have permission to unblock devices.')
        return redirect('security')

    row = get_object_or_404(
        VoucherEntryDevice,
        pk=pk,
        business=_business(request),
    )

    label = row.mac_address or row.ip_address or 'Device'

    row.attempts = 0
    row.window_started_at = None
    row.blocked_at = None
    row.blocked_until = None
    row.last_unblocked_at = timezone.now()
    row.last_unblocked_by = request.user
    row.save(
        update_fields=[
            'attempts',
            'window_started_at',
            'blocked_at',
            'blocked_until',
            'last_unblocked_at',
            'last_unblocked_by',
            'last_seen_at',
        ],
    )

    messages.success(
        request,
        f'{label} was unblocked and its wrong-entry counter was reset.',
    )

    return redirect('/security/#voucher-entry-security')


@login_required
@require_POST
def voucher_entry_unblock_all(request):
    if not _can_manage(request):
        messages.error(request, 'You do not have permission to unblock devices.')
        return redirect('security')

    business = _business(request)
    now = timezone.now()

    qs = VoucherEntryDevice.objects.filter(
        business=business,
        blocked_until__gt=now,
    )

    count = qs.count()

    qs.update(
        attempts=0,
        window_started_at=None,
        blocked_at=None,
        blocked_until=None,
        last_unblocked_at=now,
        last_unblocked_by=request.user,
    )

    messages.success(
        request,
        f'{count} blocked device{"s" if count != 1 else ""} unblocked.',
    )

    return redirect('/security/#voucher-entry-security')
