"""Expired voucher retention and archive cleanup.

Lifecycle:
    Active -> Expired -> retention period -> removed from MikroTik -> Archived

The TapTap voucher row is never deleted by this feature.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from .models_archive import VoucherArchivePolicy

logger = logging.getLogger('taptap.archive')

RETRY_SECONDS = 600


def policy_for(business):
    policy, _ = VoucherArchivePolicy.objects.get_or_create(
        business=business,
        defaults={
            'enabled': True,
            'retention_days': 7,
        },
    )
    return policy


def _mark_archived(vouchers, now, via='Automatic cleanup'):
    from .models import Voucher
    from .voucher_history import record

    changed = 0

    for voucher in vouchers:
        updated = Voucher.objects.filter(
            pk=voucher.pk,
            status='expired',
        ).update(
            status='archived',
            mikrotik_sync_status='Synced',
            mikrotik_sync_error='',
        )
        if not updated:
            continue

        before = voucher.status
        voucher.status = 'archived'
        voucher.mikrotik_sync_status = 'Synced'
        voucher.mikrotik_sync_error = ''

        try:
            record(
                voucher,
                'archived',
                source='auto',
                via=via,
                reason='Expired voucher retention period ended',
                status_before=before,
                status_after='archived',
                router_result=(
                    'Removed from MikroTik; kept permanently in TapTap'
                    if voucher.router_id
                    else 'No router copy; kept permanently in TapTap'
                ),
                text=f'Archived automatically at {timezone.localtime(now):%Y-%m-%d %H:%M}',
            )
        except Exception:
            logger.exception(
                'Could not record archive history for %s',
                voucher.code,
            )

        changed += 1

    return changed


def _remove_group(router, vouchers):
    """Use TapTap's existing API/Tunnel/Link removal path."""
    from .voucher_bin import remove_from_router

    try:
        ok, message = remove_from_router(
            router,
            vouchers,
            user=None,
        )
        logger.info(
            'archive cleanup %s: ok=%s %s',
            router.name,
            ok,
            message,
        )
        return ok
    except Exception:
        logger.exception(
            'archive cleanup failed on %s',
            router.name,
        )
        return False


def cleanup(qs=None, now=None):
    from .models import Router, RouterHotspotUser, Voucher

    now = now or timezone.now()
    base = qs if qs is not None else Voucher.objects.all()

    summary = {
        'eligible': 0,
        'queued_or_removed': 0,
        'archived': 0,
        'healed': 0,
    }

    # Group by business because each business controls its own retention policy.
    business_ids = list(
        base.values_list('business_id', flat=True).distinct()
    )

    for business_id in business_ids:
        business_rows = base.filter(
            business_id=business_id,
        )
        sample = business_rows.select_related('business').first()
        if not sample:
            continue

        policy = policy_for(sample.business)
        if not policy.enabled:
            continue

        days = max(1, min(int(policy.retention_days or 7), 3650))
        cutoff = now - timedelta(days=days)

        eligible = business_rows.filter(
            status='expired',
            expires_at__isnull=False,
            expires_at__lte=cutoff,
            frozen_at__isnull=True,
        ).select_related(
            'router',
            'business',
        )

        summary['eligible'] += eligible.count()

        # No router means there is nothing to remove first.
        no_router = list(
            eligible.filter(router__isnull=True)[:5000]
        )
        if no_router:
            summary['archived'] += _mark_archived(
                no_router,
                now,
                via='TapTap only',
            )

        # Direct API/Tunnel or a previous Link ACK confirmed removal.
        confirmed = list(
            eligible.filter(
                router__isnull=False,
                router_removal='removed',
            )[:5000]
        )
        if confirmed:
            summary['archived'] += _mark_archived(
                confirmed,
                now,
            )

        pending = (
            eligible
            .filter(router__isnull=False)
            .exclude(router_removal__in=['removed', 'queued'])
        )

        grouped = {}
        for voucher in pending[:10000]:
            grouped.setdefault(
                voucher.router_id,
                [],
            ).append(voucher)

        for router in Router.objects.filter(pk__in=grouped):
            key = f'tt:archive:remove:{router.pk}'
            if not cache.add(key, 1, RETRY_SECONDS):
                continue

            vouchers = grouped[router.pk]

            if _remove_group(router, vouchers):
                summary['queued_or_removed'] += len(vouchers)

            # Direct API/Tunnel removal may have completed in this call.
            ids = [voucher.pk for voucher in vouchers]
            ready = list(
                Voucher.objects.filter(
                    pk__in=ids,
                    status='expired',
                    router_removal='removed',
                ).select_related(
                    'router',
                    'business',
                )
            )
            if ready:
                summary['archived'] += _mark_archived(
                    ready,
                    now,
                )

        # Self-heal archived codes that somebody manually recreates in WinBox.
        archived = business_rows.filter(
            status='archived',
            router__isnull=False,
        )

        mirrors = RouterHotspotUser.objects.filter(
            router_id__in=archived.values('router_id'),
            is_present=True,
        ).values_list(
            'router_id',
            'username',
        )

        present = {
            (router_id, str(username).upper())
            for router_id, username in mirrors
        }

        heal_groups = {}
        for voucher in archived.only(
            'pk',
            'code',
            'router_id',
        )[:10000]:
            if (
                voucher.router_id,
                voucher.code.upper(),
            ) not in present:
                continue

            heal_groups.setdefault(
                voucher.router_id,
                [],
            ).append(voucher)

        for router in Router.objects.filter(pk__in=heal_groups):
            key = f'tt:archive:heal:{router.pk}'
            if not cache.add(key, 1, RETRY_SECONDS):
                continue

            vouchers = heal_groups[router.pk]

            Voucher.objects.filter(
                pk__in=[v.pk for v in vouchers]
            ).update(
                router_removal='failed',
                router_removal_note=(
                    'Archived code reappeared on the router; removing it again'
                ),
            )

            if _remove_group(router, vouchers):
                summary['healed'] += len(vouchers)

    return summary
