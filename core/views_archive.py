from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from .models_archive import VoucherArchivePolicy


@login_required
@require_POST
def voucher_archive_settings(request):
    business = request.user.business

    policy, _ = VoucherArchivePolicy.objects.get_or_create(
        business=business,
        defaults={
            'enabled': True,
            'retention_days': 7,
        },
    )

    enabled = request.POST.get('enabled') == 'on'

    try:
        days = int(request.POST.get('retention_days') or 7)
    except (TypeError, ValueError):
        days = 7

    days = max(1, min(days, 3650))

    policy.enabled = enabled
    policy.retention_days = days
    policy.save(update_fields=['enabled', 'retention_days', 'updated_at'])

    if enabled:
        messages.success(
            request,
            f'Expired voucher cleanup is active. '
            f'Expired vouchers will be removed from MikroTik after {days} day'
            f'{"s" if days != 1 else ""} and kept in TapTap as Archived.',
        )
    else:
        messages.success(
            request,
            'Expired voucher cleanup is off. Expired vouchers will still be '
            'disabled when their time ends, but TapTap will not automatically '
            'remove them from MikroTik.',
        )

    return redirect('settings')
