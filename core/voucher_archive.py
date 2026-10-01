"""Automatic archive cleanup for expired vouchers.

Lifecycle:
    Active -> Expired -> wait N days -> remove from MikroTik -> Archived in TapTap

The TapTap record is NEVER deleted.  Only the RouterOS HotSpot user is removed.

Retention is controlled by environment variable:
    TAPTAP_EXPIRED_ARCHIVE_DAYS=7

Default: 7 days.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger("taptap.archive")

RETRY_SECONDS = 600


def archive_days():
    """Configured number of days an expired voucher stays on MikroTik."""
    try:
        days = int(os.getenv("TAPTAP_EXPIRED_ARCHIVE_DAYS", "7"))
    except (TypeError, ValueError):
        days = 7
    return max(1, min(days, 3650))


def _mark_archived(vouchers, now, via="Automatic cleanup"):
    """Mark vouchers archived only after their router user has been removed."""
    from .models import Voucher
    from .voucher_history import record

    changed = 0
    for voucher in vouchers:
        updated = Voucher.objects.filter(
            pk=voucher.pk,
            status="expired",
        ).update(
            status="archived",
            # Keep Link's normal pending-voucher sender from trying to recreate it.
            mikrotik_sync_status="Synced",
            mikrotik_sync_error="",
        )
        if not updated:
            continue

        before = voucher.status
        voucher.status = "archived"
        voucher.mikrotik_sync_status = "Synced"
        voucher.mikrotik_sync_error = ""

        try:
            record(
                voucher,
                "archived",
                source="auto",
                via=via,
                reason=f"Expired for {archive_days()} day(s)",
                status_before=before,
                status_after="archived",
                router_result=(
                    "Removed from MikroTik; kept permanently in TapTap"
                    if voucher.router_id
                    else "No router copy; kept permanently in TapTap"
                ),
                text=(
                    f"Archived automatically at "
                    f"{timezone.localtime(now):%Y-%m-%d %H:%M}"
                ),
            )
        except Exception:
            logger.exception(
                "Could not record archive history for voucher %s",
                voucher.pk,
            )

        changed += 1

    return changed


def _remove_group(router, vouchers):
    """Use TapTap's existing router-removal path for API, Tunnel or Link."""
    from .voucher_bin import remove_from_router

    try:
        ok, message = remove_from_router(router, vouchers)
        logger.info(
            "expired archive cleanup on %s: ok=%s %s",
            router.name,
            ok,
            message,
        )
        return ok
    except Exception:
        logger.exception(
            "expired archive cleanup failed on %s",
            router.name,
        )
        return False


def cleanup(qs=None, now=None):
    """Archive expired vouchers after the retention period.

    Direct API / Tunnel:
      remove_from_router() removes the HotSpot user immediately and marks
      router_removal='removed'.  We then change the TapTap status to archived.

    TapTap Link:
      remove_from_router() queues hotspot_users_remove.  The existing Link ACK
      handler changes router_removal to 'removed'.  A later sweep then changes
      the TapTap status to archived.

    Archived codes are also healed: if a later router inventory shows an
    archived code has been manually recreated, TapTap removes it again.
    """
    from .models import Router, RouterHotspotUser, Voucher

    now = now or timezone.now()
    cutoff = now - timedelta(days=archive_days())

    base = qs if qs is not None else Voucher.objects.all()

    summary = {
        "eligible": 0,
        "queued_or_removed": 0,
        "archived": 0,
        "healed": 0,
    }

    eligible = base.filter(
        status="expired",
        expires_at__isnull=False,
        expires_at__lte=cutoff,
        frozen_at__isnull=True,
    ).select_related("router", "business")

    summary["eligible"] = eligible.count()

    # Expired vouchers that were never assigned to a router can be archived
    # immediately: there is no RouterOS copy to remove.
    no_router = list(
        eligible.filter(router__isnull=True)[:5000]
    )
    if no_router:
        summary["archived"] += _mark_archived(
            no_router,
            now,
            via="TapTap only",
        )

    # A previous Direct API removal or TapTap Link ACK confirmed the user is gone.
    confirmed = list(
        eligible.filter(
            router__isnull=False,
            router_removal="removed",
        )[:5000]
    )
    if confirmed:
        summary["archived"] += _mark_archived(
            confirmed,
            now,
        )

    # Anything not yet confirmed is sent through the same safe removal path used
    # by TapTap's recycle bin.  This path already supports Direct API, Tunnel,
    # and TapTap Link.
    pending = (
        eligible
        .filter(router__isnull=False)
        .exclude(router_removal__in=["removed", "queued"])
    )

    grouped = {}
    for voucher in pending[:10000]:
        grouped.setdefault(voucher.router_id, []).append(voucher)

    for router in Router.objects.filter(pk__in=grouped):
        key = f"tt:archive:remove:{router.pk}"
        if not cache.add(key, 1, RETRY_SECONDS):
            continue

        vouchers = grouped[router.pk]
        if _remove_group(router, vouchers):
            summary["queued_or_removed"] += len(vouchers)

        # Direct API/Tunnel may have completed immediately.
        ids = [v.pk for v in vouchers]
        ready = list(
            Voucher.objects.filter(
                pk__in=ids,
                status="expired",
                router_removal="removed",
            ).select_related("router", "business")
        )
        if ready:
            summary["archived"] += _mark_archived(
                ready,
                now,
            )

    # Self-heal.  If somebody manually recreates an archived user in WinBox,
    # the next inventory marks RouterHotspotUser.is_present=True.  Remove it
    # again, but keep the TapTap archive row.
    archived = base.filter(
        status="archived",
        router__isnull=False,
    )

    present_pairs = set(
        RouterHotspotUser.objects.filter(
            router_id__in=archived.values("router_id"),
            is_present=True,
        ).values_list("router_id", "username")
    )
    present_pairs = {
        (router_id, str(username).upper())
        for router_id, username in present_pairs
    }

    heal_groups = {}
    for voucher in archived.only(
        "pk",
        "code",
        "router_id",
        "router_removal",
    )[:10000]:
        if (voucher.router_id, voucher.code.upper()) not in present_pairs:
            continue
        heal_groups.setdefault(voucher.router_id, []).append(voucher)

    for router in Router.objects.filter(pk__in=heal_groups):
        key = f"tt:archive:heal:{router.pk}"
        if not cache.add(key, 1, RETRY_SECONDS):
            continue

        vouchers = heal_groups[router.pk]

        # Force the existing removal helper to treat this as work to do again.
        Voucher.objects.filter(
            pk__in=[v.pk for v in vouchers]
        ).update(
            router_removal="failed",
            router_removal_note=(
                "Archived code reappeared on the router; removing it again"
            ),
        )

        if _remove_group(router, vouchers):
            summary["healed"] += len(vouchers)

    return summary
