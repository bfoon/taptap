from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from .models_archive import VoucherArchivePolicy


def _active_business(request):
    """
    Return the business selected by TeamAccessMiddleware.

    request.tt_business is the source of truth for multi-business accounts.
    request.user.business is only a compatibility fallback.
    """
    business = getattr(request, 'tt_business', None)

    if business is not None:
        return business

    try:
        return request.user.business
    except Exception:
        return None


@login_required
@require_POST
def voucher_archive_settings(request):
    business = _active_business(request)

    if business is None:
        messages.error(
            request,
            'No active business was found. Please select a business and try again.',
        )
        return redirect('settings')

    # Browser checkboxes normally submit "on" when checked.
    # Accept other common true values as well so this is robust.
    enabled_value = str(
        request.POST.get('enabled', '')
    ).strip().lower()

    enabled = enabled_value in {
        'on',
        '1',
        'true',
        'yes',
        'checked',
    }

    try:
        days = int(
            request.POST.get('retention_days') or 7
        )
    except (TypeError, ValueError):
        days = 7

    days = max(
        1,
        min(days, 3650),
    )

    policy, _created = VoucherArchivePolicy.objects.update_or_create(
        business=business,
        defaults={
            'enabled': enabled,
            'retention_days': days,
        },
    )

    # Read it back from the database so the confirmation is based on the
    # persisted values rather than only the submitted form.
    policy.refresh_from_db()

    if policy.enabled:
        messages.success(
            request,
            (
                'Expired voucher cleanup is ACTIVE. '
                f'Expired vouchers will be removed from MikroTik after '
                f'{policy.retention_days} day'
                f'{"s" if policy.retention_days != 1 else ""} '
                'and kept in TapTap as Archived.'
            ),
        )
    else:
        messages.success(
            request,
            (
                'Expired voucher cleanup is OFF. '
                'Vouchers will still expire and disconnect normally, '
                'but TapTap will not automatically remove expired vouchers '
                'from MikroTik.'
            ),
        )

    return redirect('settings')
