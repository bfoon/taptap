"""Strict voucher expiry and automatic router cleanup.

When a voucher's time runs out:
1. TapTap marks it Expired.
2. It is disabled immediately on MikroTik and active sessions are dropped.
3. After the business retention policy from Settings (default 7 days), TapTap removes the HotSpot
   user from MikroTik.
4. The TapTap voucher record is NOT deleted.  Its status becomes Archived.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger('taptap.expiry')
LINK_REQUEUE_SECONDS = 600
API_RETRY_SECONDS = 60
LINK_BATCH = 100


def _seconds(value):
    from .sync import _routeros_seconds
    return _routeros_seconds(value)


def uptime_used_up(row):
    """True when RouterOS says the HotSpot user used all limit-uptime."""
    limit = _seconds((row or {}).get('limit-uptime', ''))
    return (
        limit > 0
        and _seconds((row or {}).get('uptime', '')) >= limit
    )


def fill_missing_ends(qs):
    """Store first-use + duration when an old used voucher has no expires_at."""
    from .models import Voucher

    count = 0
    rows = qs.filter(
        expires_at__isnull=True,
        used_at__isnull=False,
        duration_minutes__gt=0,
    ).only(
        'pk',
        'used_at',
        'duration_minutes',
    )[:5000]

    for voucher in rows:
        count += Voucher.objects.filter(
            pk=voucher.pk,
            expires_at__isnull=True,
        ).update(
            expires_at=(
                voucher.used_at
                + timedelta(minutes=voucher.duration_minutes)
            )
        )

    return count


def _running(qs):
    return qs.filter(
        status='active',
        frozen_at__isnull=True,
    )


def mark_expired(vouchers, now, why, via, detail=''):
    """Set status=expired and record the automatic expiry event."""
    from .live import push_event
    from .models import Voucher
    from .voucher_history import record

    changed = []

    for voucher in vouchers:
        if not Voucher.objects.filter(
            pk=voucher.pk,
            status='active',
            frozen_at__isnull=True,
        ).update(status='expired'):
            continue

        before = 'active'
        voucher.status = 'expired'

        record(
            voucher,
            'time_up',
            source='auto',
            via=via,
            reason=why,
            status_before=before,
            status_after='expired',
            router_result=(
                'Switching off on the router'
                if voucher.router_id
                else 'No router — marked expired in TapTap'
            ),
            text=detail,
        )
        changed.append(voucher)

    if changed:
        business_id = changed[0].business_id
        names = ', '.join(v.code for v in changed[:5])
        if len(changed) > 5:
            names += f' and {len(changed) - 5} more'

        push_event(
            business_id,
            f'Time ran out: {names} — switched off',
            'fix',
        )

    return changed


def _free_devices(svc, voucher, macs=()):
    """Drop sessions/cookies/hosts so customers get the login page again."""
    from .models import VoucherDeviceBinding

    known = set(macs)

    for current, previous in VoucherDeviceBinding.objects.filter(
        voucher=voucher
    ).values_list(
        'current_mac',
        'previous_mac',
    ):
        known.update(
            mac
            for mac in (current, previous)
            if mac
        )

    release = getattr(svc, 'release_devices', None)
    if release:
        return release(voucher.code, known)

    svc.reset_active_by_name(voucher.code)
    return 0


def enforce_on_router(router, svc, users, active, now):
    """Expire and disable vouchers using fresh rows from a live API router."""
    from .models import RouterHotspotUser, Voucher
    from .voucher_history import channel

    via = channel(router)

    rows = {
        str(row.get('name', '')).strip().upper(): row
        for row in users
        if row.get('name')
    }
    online = {
        str(session.get('user', '')).strip().upper()
        for session in active
        if session.get('user')
    }

    base = Voucher.objects.filter(
        business=router.business
    )
    fill_missing_ends(
        base.filter(router=router)
    )

    # Calendar expiry.
    due = list(
        _running(
            base.filter(
                router=router,
                expires_at__lte=now,
            )
        )
    )
    newly = mark_expired(
        due,
        now,
        'Time ran out',
        via,
        f'on {router.name}',
    )

    # Router uptime reached limit.
    used_up = [
        name
        for name, row in rows.items()
        if uptime_used_up(row)
    ]

    if used_up:
        codes = {
            code.upper(): code
            for code in used_up
        }
        candidates = [
            voucher
            for voucher in _running(
                base.filter(
                    Q(router=router)
                    | Q(router__isnull=True)
                )
            ).filter(
                code__in=(
                    list(codes)
                    + [name.lower() for name in codes]
                )
            )
            if voucher.code.upper() in rows
        ]

        newly += mark_expired(
            candidates,
            now,
            'Used all its time on the router (uptime limit reached)',
            via,
            f'on {router.name}',
        )

    # Keep expired vouchers disabled until archive cleanup removes them.
    switched = set()

    for voucher in base.filter(
        router=router,
        status='expired',
        frozen_at__isnull=True,
    ).only(
        'pk',
        'code',
    ):
        key = voucher.code.upper()
        row = rows.get(key)

        enabled = (
            row is not None
            and str(
                row.get('disabled', 'false')
            ).lower() not in ('true', 'yes')
        )

        if not enabled and key not in online:
            continue

        try:
            if enabled:
                svc.disable_voucher(voucher.code)
                RouterHotspotUser.objects.filter(
                    router=router,
                    username__iexact=voucher.code,
                ).update(disabled=True)

            macs = [
                session.get('mac-address')
                for session in active
                if str(
                    session.get('user', '')
                ).strip().upper() == key
            ]

            _free_devices(
                svc,
                voucher,
                macs,
            )
            switched.add(key)

        except Exception as exc:
            logger.warning(
                'expiry: could not switch off %s on %s: %s',
                voucher.code,
                router.name,
                exc,
            )

    # Clear stale cookies of expired vouchers.
    if (
        hasattr(svc, 'release_devices')
        and cache.add(
            f'tt:exp:cookies:{router.pk}',
            1,
            60,
        )
    ):
        try:
            with_cookie = {
                str(cookie.get('user', '')).strip().upper()
                for cookie in (
                    svc.resource('/ip/hotspot/cookie').get()
                )
            }
        except Exception:
            with_cookie = set()

        if with_cookie:
            for voucher in base.filter(
                router=router,
                status='expired',
            ).only(
                'pk',
                'code',
            ):
                if (
                    voucher.code.upper() in with_cookie
                    and voucher.code.upper() not in switched
                ):
                    try:
                        _free_devices(
                            svc,
                            voucher,
                        )
                        switched.add(
                            voucher.code.upper()
                        )
                    except Exception as exc:
                        logger.info(
                            'expiry: could not free devices of %s: %s',
                            voucher.code,
                            exc,
                        )

    return switched, len(newly)


def _confirmed_key(router_id, code):
    return (
        f'tt:exp:done:{router_id}:'
        f'{code.upper()}'
    )


def _needs_router_action(voucher, mirror):
    mirror_row = mirror.get(
        (
            voucher.router_id,
            voucher.code.upper(),
        )
    )

    if mirror_row is not None:
        return (
            mirror_row.is_present
            and not mirror_row.disabled
        )

    return not cache.get(
        _confirmed_key(
            voucher.router_id,
            voucher.code,
        )
    )


def sweep(
    business=None,
    now=None,
    watched=(),
    recheck=False,
):
    """Expire due vouchers, enforce RouterOS shutdown, then archive old ones."""
    from .linkops import uses_link
    from .models import (
        Router,
        RouterHotspotUser,
        Voucher,
    )
    from .voucher_history import channel

    now = now or timezone.now()

    qs = (
        Voucher.objects.all()
        if business is None
        else Voucher.objects.filter(
            business=business
        )
    )

    fill_missing_ends(qs)

    summary = {
        'expired': 0,
        'pushed': 0,
        'queued': 0,
        'archive_eligible': 0,
        'archived': 0,
        'archive_healed': 0,
    }

    # 1. Mark calendar-expired vouchers.
    due = list(
        _running(
            qs.filter(
                expires_at__lte=now,
            )
        ).select_related(
            'router',
            'business',
        )[:5000]
    )

    by_router = {}
    for voucher in due:
        by_router.setdefault(
            voucher.router_id,
            [],
        ).append(voucher)

    for router_id, vouchers in by_router.items():
        via = (
            channel(vouchers[0].router)
            if router_id
            else 'TapTap only'
        )
        summary['expired'] += len(
            mark_expired(
                vouchers,
                now,
                'Time ran out',
                via,
            )
        )

    # 2. Make sure all expired router vouchers stay disabled.
    pending = (
        qs.filter(
            status='expired',
            frozen_at__isnull=True,
            router__isnull=False,
        )
        .exclude(
            router_id__in=list(watched)
        )
    )

    gate = (
        f'tt:exp:recheck:'
        f'{business.pk if business else "all"}'
    )

    if (
        not recheck
        and not cache.add(
            gate,
            1,
            60,
        )
    ):
        pending = pending.filter(
            pk__in=[voucher.pk for voucher in due]
        )

    mirror = {
        (
            row.router_id,
            row.username.upper(),
        ): row
        for row in RouterHotspotUser.objects.filter(
            router_id__in=pending.values(
                'router_id'
            ),
            username__in=pending.values(
                'code'
            ),
        )
    }

    todo = {}

    for voucher in pending.only(
        'pk',
        'code',
        'router_id',
    )[:20000]:
        if _needs_router_action(
            voucher,
            mirror,
        ):
            todo.setdefault(
                voucher.router_id,
                [],
            ).append(voucher)

    for router in Router.objects.filter(
        pk__in=list(todo)
    ):
        vouchers = todo[router.pk]

        if uses_link(router):
            summary['queued'] += _queue_link(
                router,
                vouchers,
            )
        else:
            summary['pushed'] += _push_api(
                router,
                vouchers,
            )

    # 3. Retention cleanup:
    #    Expired -> remove from MikroTik -> Archived in TapTap.
    try:
        from .voucher_archive import cleanup

        result = cleanup(
            qs=qs,
            now=now,
        )
        summary['archive_eligible'] = result.get(
            'eligible',
            0,
        )
        summary['archived'] = result.get(
            'archived',
            0,
        )
        summary['archive_healed'] = result.get(
            'healed',
            0,
        )

    except Exception:
        # Archiving must never interrupt strict expiry enforcement.
        logger.exception(
            'expired voucher archive cleanup failed'
        )

    return summary


def _queue_link(router, vouchers):
    """TapTap Link batch disable for newly/previously expired vouchers."""
    from .linkops import send

    names = [
        voucher.code
        for voucher in vouchers
        if cache.add(
            (
                f'tt:exp:link:{router.pk}:'
                f'{voucher.code.upper()}'
            ),
            1,
            LINK_REQUEUE_SECONDS,
        )
    ]

    queued = 0

    for start in range(
        0,
        len(names),
        LINK_BATCH,
    ):
        part = names[
            start:start + LINK_BATCH
        ]

        try:
            send(
                router,
                'hotspot_users_disable',
                {
                    'names': part,
                    'disabled': True,
                    'reason': 'expired',
                },
                label=(
                    f'Switch off {len(part)} voucher'
                    f'{"s" if len(part) != 1 else ""} '
                    f'whose time ran out'
                ),
            )
            queued += len(part)

        except ValueError as exc:
            logger.info(
                'expiry (link) %s: %s',
                router.name,
                exc,
            )

            for name in part:
                cache.delete(
                    (
                        f'tt:exp:link:{router.pk}:'
                        f'{name.upper()}'
                    )
                )
            break

    return queued


def _push_api(router, vouchers):
    """Disable expired vouchers on Direct API/Tunnel routers."""
    from .mikrotik import MikroTikService
    from .models import RouterHotspotUser

    if not cache.add(
        f'tt:exp:api:{router.pk}',
        1,
        API_RETRY_SECONDS,
    ):
        return 0

    done = 0

    try:
        svc = MikroTikService(
            router,
            timeout=getattr(
                settings,
                'MIKROTIK_LIVE_TIMEOUT',
                5,
            ),
        ).connect()

    except Exception as exc:
        logger.info(
            'expiry (api) %s offline: %s',
            router.name,
            exc,
        )
        return 0

    try:
        for voucher in vouchers:
            try:
                svc.disable_voucher(
                    voucher.code
                )
                _free_devices(
                    svc,
                    voucher,
                )

                RouterHotspotUser.objects.filter(
                    router=router,
                    username__iexact=voucher.code,
                ).update(
                    disabled=True,
                )

                cache.set(
                    _confirmed_key(
                        router.pk,
                        voucher.code,
                    ),
                    1,
                    86400,
                )
                done += 1

            except Exception as exc:
                logger.warning(
                    'expiry: could not switch off %s '
                    'on %s: %s',
                    voucher.code,
                    router.name,
                    exc,
                )

    finally:
        svc.close()

    return done


def link_ack(cmd, ok):
    """TapTap Link confirmed/refused an expired-voucher disable."""
    from .models import RouterHotspotUser

    names = [
        str(name)
        for name in cmd.params.get(
            'names',
            [],
        )
    ]

    if ok and names:
        for name in names:
            RouterHotspotUser.objects.filter(
                router=cmd.router,
                username__iexact=name,
            ).update(
                disabled=True,
            )

            cache.set(
                _confirmed_key(
                    cmd.router_id,
                    name,
                ),
                1,
                86400,
            )

    elif not ok:
        for name in names:
            cache.delete(
                (
                    f'tt:exp:link:{cmd.router_id}:'
                    f'{name.upper()}'
                )
            )
