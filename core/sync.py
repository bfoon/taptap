import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from collections import defaultdict

from django.db import transaction
from django.utils import timezone

from .mikrotik import MikroTikService, ros_bool, redact
from .models import (
    Voucher, VoucherPlan, RouterHotspotProfile, RouterHotspotUser,
    SyncedIPBinding, RouterInterface, RouterNeighbor, RouterDevice,
    RouterInterfaceRole, RouterConfigSnapshot,
)
from .durations import best_unit, parse_routeros as _routeros_minutes, router_limit
from .utils import log, voucher_profile
from .finance import mark_activated


def _has_uptime(value):
    text = str(value or '').strip().lower()
    return (
        bool(text)
        and text not in {'0', '0s', '00:00:00', 'none'}
        and any(ch.isdigit() and ch != '0' for ch in text)
    )


def _routeros_seconds(value):
    """'1w2d3h4m5s' / '12:30:00' / '1d 02:00:00' -> seconds."""
    text = str(value or '').strip().lower()
    total = 0

    for number, unit in re.findall(
        r'(\d+)(w|d|h|m|s)(?![a-z])',
        text,
    ):
        total += int(number) * {
            'w': 604800,
            'd': 86400,
            'h': 3600,
            'm': 60,
            's': 1,
        }[unit]

    clock = re.search(
        r'(\d{1,2}):(\d{2}):(\d{2})',
        text,
    )

    if clock:
        h, m, sec = (
            int(x)
            for x in clock.groups()
        )
        total += h * 3600 + m * 60 + sec

    return total


def first_use_estimate(
    uptime,
    now,
    created_at=None,
):
    """
    Estimate when a voucher was first used from RouterOS uptime.
    """
    when = now - timedelta(
        seconds=_routeros_seconds(uptime)
    )

    if created_at and when < created_at:
        when = created_at

    return min(when, now)


def _clean(row):
    return {
        str(k).lstrip('.'): v
        for k, v in dict(row or {}).items()
    }


def _safe_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _routeros_hours(
    value,
    default=24,
):
    text = str(value or '').strip().lower()

    if not text or text in {
        '0',
        'none',
        'unlimited',
    }:
        return default

    total_seconds = 0

    for number, unit in re.findall(
        r'(\d+)(w|d|h|m|s)',
        text,
    ):
        n = int(number)

        total_seconds += n * {
            'w': 604800,
            'd': 86400,
            'h': 3600,
            'm': 60,
            's': 1,
        }[unit]

    if total_seconds:
        return max(
            1,
            int(
                (
                    total_seconds
                    + 3599
                )
                // 3600
            ),
        )

    match = re.search(
        r'(?:(\d+)d)?\s*(\d{1,2}):(\d{2}):(\d{2})',
        text,
    )

    if match:
        days, hours, mins, secs = [
            int(x or 0)
            for x in match.groups()
        ]

        total_seconds = (
            days * 86400
            + hours * 3600
            + mins * 60
            + secs
        )

        return max(
            1,
            int(
                (
                    total_seconds
                    + 3599
                )
                // 3600
            ),
        )

    return default


def _neighbor_kind(row):
    hay = ' '.join(
        str(row.get(k, ''))
        for k in (
            'identity',
            'platform',
            'board',
        )
    ).lower()

    if any(
        word in hay
        for word in (
            'cap',
            'wap',
            'hap',
            'access point',
            'wifi',
            'wireless',
            'unifi',
            'omada',
        )
    ):
        return 'wifi'

    if any(
        word in hay
        for word in (
            'switch',
            'sw-',
            'crs',
        )
    ):
        return 'switch'

    if any(
        word in hay
        for word in (
            'router',
            'rb',
            'ccr',
            'hex',
        )
    ):
        return 'router'

    return 'network'


def _normalize_mac(value):
    return (
        str(value or '')
        .strip()
        .upper()
        .replace('-', ':')
    )


MIKHMON_RE = re.compile(
    r'",\s*([a-z]*)\s*,\s*([\d.]+)\s*,\s*([0-9wdhms:]*)\s*,\s*([\d.]*)\s*,',
    re.I,
)

PRICE_WORD_RE = re.compile(
    r'(?:price|prix|amount|cost|tarif)\s*[:=]?\s*(?:[A-Za-z$]{1,4}\s*)?(\d[\d,]*(?:\.\d+)?)',
    re.I,
)

AMOUNT_UNIT_RE = re.compile(
    r'(\d[\d,]*(?:\.\d+)?)\s?(?:gmd|dalasis?)\b',
    re.I,
)


def _num(text):
    try:
        value = Decimal(
            str(text).replace(',', '')
        )

        return (
            value
            if value > 0
            else None
        )

    except (
        InvalidOperation,
        ValueError,
    ):
        return None


def parse_mikhmon(script):
    m = MIKHMON_RE.search(
        str(script or '')
    )

    if not m:
        return None, None

    price = (
        _num(m.group(4))
        or _num(m.group(2))
    )

    validity = (
        _routeros_minutes(
            m.group(3),
            0,
        )
        if m.group(3)
        else 0
    )

    return price, (
        validity
        or None
    )


def price_from_text(
    text,
    currency='D',
):
    text = str(text or '')

    if not text:
        return None

    m = (
        PRICE_WORD_RE.search(text)
        or AMOUNT_UNIT_RE.search(text)
    )

    if m:
        return _num(
            m.group(1)
        )

    cur = re.escape(
        str(currency or 'D').strip()
    )

    if cur:
        m = re.search(
            r'(?:(?<![A-Za-z])'
            + cur
            + r'|GMD)\s?(\d[\d,]*(?:\.\d+)?)(?![\d.]*\s*(?i:h|hr|hrs|hours?|d|days?|m|mins?|w|weeks?|mb|gb|mbps|kbps|k)\b)',
            text,
        )

        if m:
            return _num(
                m.group(1)
            )

    return None


def profile_price(
    row,
    currency='D',
):
    price, validity = parse_mikhmon(
        row.get(
            'on-login',
            row.get(
                'on_login',
                '',
            ),
        )
    )

    if price:
        return (
            price,
            validity,
            'Mikhmon on-login script',
        )

    for key, label in (
        (
            'comment',
            'profile comment',
        ),
        (
            'name',
            'profile name',
        ),
    ):
        found = price_from_text(
            row.get(
                key,
                '',
            ),
            currency,
        )

        if found:
            return (
                found,
                validity,
                label,
            )

    return None, validity, ''


def _pm_create(row):
    from .profile_time import profile_minutes
    return profile_minutes(row)[0]


def _profile_to_plan(
    router,
    row,
    summary,
    now,
):
    name = str(
        row.get(
            'name',
            '',
        )
    ).strip()

    if not name:
        return None

    shared = max(
        1,
        _safe_int(
            row.get(
                'shared-users',
                row.get(
                    'shared_users',
                    1,
                ),
            )
        )
        or 1,
    )

    price, validity, price_source = profile_price(
        row,
        router.business.currency,
    )

    rate = str(
        row.get(
            'rate-limit',
            row.get(
                'rate_limit',
                '',
            ),
        )
        or ''
    )

    session = str(
        row.get(
            'session-timeout',
            row.get(
                'session_timeout',
                '',
            ),
        )
        or ''
    )

    RouterHotspotProfile.objects.update_or_create(
        router=router,
        name=name,
        defaults={
            'business': router.business,
            'mikrotik_id': str(
                row.get(
                    'id',
                    '',
                )
            ),
            'rate_limit': rate,
            'shared_users': shared,
            'session_timeout': session,
            'idle_timeout': str(
                row.get(
                    'idle-timeout',
                    row.get(
                        'idle_timeout',
                        '',
                    ),
                )
                or ''
            ),
            'keepalive_timeout': str(
                row.get(
                    'keepalive-timeout',
                    row.get(
                        'keepalive_timeout',
                        '',
                    ),
                )
                or ''
            ),
            'address_pool': str(
                row.get(
                    'address-pool',
                    row.get(
                        'address_pool',
                        '',
                    ),
                )
                or ''
            ),
            'is_present': True,
            'raw_data': row,
            'last_seen_at': now,
        },
    )

    plan = (
        VoucherPlan.objects
        .filter(
            business=router.business,
            name__iexact=name,
        )
        .first()
    )

    if (
        not plan
        and VoucherPlan.all_objects
        .binned()
        .filter(
            business=router.business,
            name__iexact=name,
        )
        .exists()
    ):
        summary[
            'deleted_plans_skipped'
        ] = (
            summary.get(
                'deleted_plans_skipped',
                0,
            )
            + 1
        )

        return None

    if plan:
        summary[
            'duplicate_plans_skipped'
        ] += 1

        if plan.source == 'mikrotik':
            plan.max_devices = shared
            plan.speed_limit = rate

            from .profile_time import profile_minutes as _pm
            _from_profile = _pm(row)[0]
            if (
                plan.duration_unit
                != 'unlimited'
                or (plan.source == 'mikrotik' and _from_profile and not plan.is_free)   # profile says 24h / 30 days: not unlimited (free staff plans stay as you set them)
            ):
                minutes = _routeros_minutes(
                    session,
                    validity
                    or _from_profile
                    or plan.duration_minutes
                    or 1440,
                )

                if (
                    minutes
                    != plan.duration_minutes
                ):
                    plan.duration_minutes = (
                        minutes
                    )

                    plan.duration_unit = (
                        best_unit(minutes)
                    )

            plan.mikrotik_profile_name = name

            if (
                not plan.imported_from_router_id
            ):
                plan.imported_from_router = (
                    router
                )

            fields = [
                'max_devices',
                'speed_limit',
                'duration_minutes',
                'duration_unit',
                'mikrotik_profile_name',
                'imported_from_router',
            ]

            if (
                price
                and plan.price != price
                and plan.price_source
                != 'manual'
                and not plan.is_free
            ):
                plan.price = price
                plan.price_source = (
                    'router'
                )

                fields += [
                    'price',
                    'price_source',
                ]

                summary[
                    'prices_found'
                ] = (
                    summary.get(
                        'prices_found',
                        0,
                    )
                    + 1
                )

            plan.save(
                update_fields=fields
            )

            if (
                not plan.price
                and not plan.is_free
            ):
                summary.setdefault(
                    'plans_without_price',
                    [],
                ).append(name)

        elif (
            price
            and not plan.price
            and not plan.is_free
        ):
            plan.price = price
            plan.price_source = 'router'

            plan.save(
                update_fields=[
                    'price',
                    'price_source',
                ]
            )

            summary[
                'prices_found'
            ] = (
                summary.get(
                    'prices_found',
                    0,
                )
                + 1
            )

        return plan

    plan = VoucherPlan.objects.create(
        business=router.business,
        name=name,
        price=price or 0,
        price_source=(
            'router'
            if price
            else ''
        ),
        duration_minutes=_routeros_minutes(
            session,
            validity
            or _pm_create(row)
            or 1440,
        ),
        duration_unit=best_unit(
            _routeros_minutes(
                session,
                validity
                or _pm_create(row)
                or 1440,
            )
        ),
        max_devices=shared,
        speed_limit=rate,
        active=True,
        source='mikrotik',
        imported_from_router=router,
        mikrotik_profile_name=name,
    )

    summary[
        'pulled_plans'
    ] += 1

    if price:
        summary[
            'prices_found'
        ] = (
            summary.get(
                'prices_found',
                0,
            )
            + 1
        )

    else:
        summary.setdefault(
            'plans_without_price',
            [],
        ).append(name)

    return plan


def sync_router(
    router,
    progress=None,
):
    def notify(
        percent,
        phase,
    ):
        if not progress:
            return

        try:
            progress(
                max(
                    0,
                    min(
                        100,
                        int(percent),
                    ),
                ),
                str(phase),
            )
        except Exception:
            pass

    notify(
        2,
        'Opening RouterOS connection',
    )

    summary = {
        'pulled_plans': 0,
        'duplicate_plans_skipped': 0,
        'pulled_vouchers': 0,
        'duplicate_vouchers_skipped': 0,
        'pulled_users': 0,
        'pulled_bindings': 0,
        'pushed_vouchers': 0,
        'updated_vouchers': 0,
        'pushed_bindings': 0,
        'updated_bindings': 0,
        'devices_discovered': 0,
        'config_sections': 0,
        'errors': [],
        'unassigned_vouchers': (
            router.business.vouchers
            .filter(
                router__isnull=True,
                source='taptap',
            )
            .count()
        ),
    }

    svc = MikroTikService(
        router
    ).connect()

    now = timezone.now()

    notify(
        8,
        'Connected — reading HotSpot plans',
    )

    try:
        # Read first, then mark what is gone: a failed read must neither stop the sync
        # (vouchers still go out) nor make every profile look deleted.
        try:
            profile_rows = list(svc.hotspot_profiles())
            profiles_read = True
        except Exception as exc:
            profile_rows, profiles_read = [], False
            summary['errors'].append(f'HotSpot profiles could not be read ({exc}) — the last known plans were kept')
        if profiles_read:
            RouterHotspotProfile.objects.filter(
                router=router
            ).update(
                is_present=False
            )

        profile_map = {}

        for raw in profile_rows:
            row = _clean(raw)

            plan = _profile_to_plan(
                router,
                row,
                summary,
                now,
            )

            if plan:
                profile_map[
                    plan.name.lower()
                ] = plan

        notify(
            22,
            'Plans imported — reading vouchers and HotSpot users',
        )

        # Read the users BEFORE marking anything gone, with one retry (a big table can time out once).
        try:
            users_read_first = list(svc.hotspot_users())
        except Exception:
            import time as _time
            _time.sleep(2)
            users_read_first = list(svc.hotspot_users())

        RouterHotspotUser.objects.filter(
            router=router
        ).update(
            is_present=False
        )

        from .voucher_bin import (
            deleted_codes,
            remove_with_service,
        )

        from .voucher_codes import (
            aliases as code_aliases,
            rename_with_service,
        )

        binned = deleted_codes(
            router.business
        )

        binned_seen = []

        renamed = code_aliases(
            router.business
        )

        renamed_seen = []

        router_rows = users_read_first

        present = {
            str(
                _clean(r).get(
                    'name',
                    '',
                )
            ).upper()
            for r in router_rows
        }

        for raw in router_rows:
            row = _clean(raw)

            username = str(
                row.get(
                    'name',
                    '',
                )
            ).strip()

            if not username:
                continue

            if username.upper() in binned:
                binned_seen.append(
                    username
                )

                continue

            if username.upper() in renamed:
                renamed_seen.append(
                    (
                        username,
                        renamed[
                            username.upper()
                        ].code,
                    )
                )

                continue

            profile_name = str(
                row.get(
                    'profile',
                    'default',
                )
                or 'default'
            )
            from .orphan_profiles import resolve as _resolve_profile     # "*1" → its name, when the router still has it
            profile_name = _resolve_profile(router, profile_name)

            plan = (
                profile_map.get(
                    profile_name.lower()
                )
                or router.business.plans
                .filter(
                    name__iexact=profile_name
                )
                .first()
            )

            from .profile_time import profile_devices
            max_devices = (
                plan.max_devices
                if plan
                else profile_devices(router, profile_name)   # no plan: the profile's shared-users, not 1
            )

            duration_minutes = (
                _routeros_minutes(
                    row.get(
                        'limit-uptime',
                        row.get(
                            'limit_uptime',
                            '',
                        ),
                    ),
                    (
                        plan.duration_minutes
                        if plan
                        else 1440
                    ),
                )
            )
            if not duration_minutes and plan is not None and plan.duration_minutes:
                duration_minutes = plan.duration_minutes      # "0s" / no limit on the user: the profile's length counts

            disabled = ros_bool(
                row.get(
                    'disabled',
                    False,
                )
            )

            existing_voucher = (
                Voucher.all_objects
                .filter(
                    code__iexact=username
                )
                .first()
            )

            router_pw = row.get(
                'password'
            )

            router_pw = (
                None
                if (
                    router_pw is None
                    or str(
                        router_pw
                    ).startswith('•')
                )
                else str(router_pw)
            )

            is_member_row = (
                router_pw is not None
                and router_pw != ''
                and router_pw
                != username
            )

            source = (
                'taptap'
                if (
                    existing_voucher
                    and existing_voucher.business_id
                    == router.business_id
                    and existing_voucher.source
                    == 'taptap'
                )
                else 'mikrotik'
            )

            RouterHotspotUser.objects.update_or_create(
                router=router,
                username=username,
                defaults={
                    'business': router.business,
                    'mikrotik_id': str(
                        row.get(
                            'id',
                            '',
                        )
                    ),
                    'profile': profile_name,
                    'mac_address': _normalize_mac(
                        row.get(
                            'mac-address',
                            row.get(
                                'mac_address',
                                '',
                            ),
                        )
                    ),
                    'comment': str(
                        row.get(
                            'comment',
                            '',
                        )
                    ),
                    'limit_uptime': str(
                        row.get(
                            'limit-uptime',
                            row.get(
                                'limit_uptime',
                                '',
                            ),
                        )
                    ),
                    'uptime': str(
                        row.get(
                            'uptime',
                            '',
                        )
                    ),
                    'disabled': disabled,
                    'source': source,
                    'is_present': True,
                    'raw_data': row,
                    'last_seen_at': now,
                },
            )

            summary[
                'pulled_users'
            ] += 1

            user_price = price_from_text(
                row.get(
                    'comment',
                    '',
                ),
                router.business.currency,
            )

            plan_price = (
                plan.price
                if (
                    plan
                    and plan.price
                )
                else None
            )

            if existing_voucher:
                if (
                    existing_voucher.business_id
                    != router.business_id
                ):
                    summary[
                        'errors'
                    ].append(
                        f'Voucher name {username} already belongs to another TapTap business; import skipped.'
                    )

                    continue

                summary[
                    'duplicate_vouchers_skipped'
                ] += 1

                fields = [
                    'mikrotik_sync_status',
                    'mikrotik_sync_error',
                ]

                existing_voucher.mikrotik_sync_status = (
                    'Synced'
                )

                existing_voucher.mikrotik_sync_error = (
                    ''
                )

                if (
                    existing_voucher.router_id
                    is None
                ):
                    existing_voucher.router = (
                        router
                    )

                    fields.append(
                        'router'
                    )

                if (
                    existing_voucher.source
                    == 'mikrotik'
                    and not existing_voucher.frozen_at
                ):
                    existing_voucher.mikrotik_id = (
                        str(
                            row.get(
                                'id',
                                '',
                            )
                        )
                    )

                    existing_voucher.plan_name = (
                        profile_name
                    )

                    existing_voucher.duration_minutes = (
                        duration_minutes
                    )

                    existing_voucher.max_devices = (
                        max_devices
                    )

                    _was = (
                        existing_voucher.status
                    )

                    existing_voucher.status = (
                        'expired'
                        if _was == 'expired'
                        else (
                            'disabled'
                            if disabled
                            else 'active'
                        )
                    )

                    if (
                        _was
                        != existing_voucher.status
                    ):
                        from .voucher_history import record

                        record(
                            existing_voucher,
                            (
                                'router_disabled'
                                if disabled
                                else 'router_enabled'
                            ),
                            source='router',
                            via='Full sync',
                            status_before=_was,
                            status_after=existing_voucher.status,
                            text=f'Changed on {router.name}',
                        )

                    fields += [
                        'mikrotik_id',
                        'plan_name',
                        'duration_minutes',
                        'max_devices',
                        'status',
                    ]

                    if router_pw is not None:
                        existing_voucher.login_type = (
                            'member'
                            if is_member_row
                            else existing_voucher.login_type
                        )

                        existing_voucher.password = (
                            router_pw[:64]
                            if is_member_row
                            else ''
                        )

                        fields += [
                            'login_type',
                            'password',
                        ]

                new_price = (
                    user_price
                    or plan_price
                )

                if (
                    new_price
                    and not existing_voucher.price
                    and not existing_voucher.sold_at
                ):
                    existing_voucher.price = (
                        new_price
                    )

                    fields.append(
                        'price'
                    )

                    summary[
                        'prices_repaired'
                    ] = (
                        summary.get(
                            'prices_repaired',
                            0,
                        )
                        + 1
                    )

                existing_voucher.save(
                    update_fields=list(
                        dict.fromkeys(
                            fields
                        )
                    )
                )

                if (
                    _has_uptime(
                        row.get(
                            'uptime'
                        )
                    )
                    and not existing_voucher.used_at
                ):
                    try:
                        when = first_use_estimate(
                            row.get(
                                'uptime'
                            ),
                            now,
                            (
                                existing_voucher.created_at
                                if existing_voucher.source
                                == 'taptap'
                                else None
                            ),
                        )

                        if mark_activated(
                            existing_voucher,
                            when,
                        ):
                            summary[
                                'activated_vouchers'
                            ] = (
                                summary.get(
                                    'activated_vouchers',
                                    0,
                                )
                                + 1
                            )

                    except Exception as exc:
                        summary[
                            'errors'
                        ].append(
                            f'Activation tracking for {username}: {exc}'
                        )

            else:
                try:
                    new_voucher = (
                        Voucher.objects.create(
                            business=router.business,
                            router=router,
                            code=username,
                            plan_name=profile_name,
                            price=(
                                user_price
                                or plan_price
                                or 0
                            ),
                            duration_minutes=duration_minutes,
                            max_devices=max_devices,
                            status=(
                                'disabled'
                                if disabled
                                else 'active'
                            ),
                            source='mikrotik',
                            mikrotik_id=str(
                                row.get(
                                    'id',
                                    '',
                                )
                            ),
                            mikrotik_sync_status='Synced',
                            mikrotik_sync_error='',
                            login_type=(
                                'member'
                                if is_member_row
                                else 'voucher'
                            ),
                            password=(
                                router_pw[:64]
                                if is_member_row
                                else ''
                            ),
                        )
                    )

                    summary[
                        'pulled_vouchers'
                    ] += 1

                    if _has_uptime(
                        row.get(
                            'uptime'
                        )
                    ):
                        when = first_use_estimate(
                            row.get(
                                'uptime'
                            ),
                            now,
                        )

                        if (
                            router.sales_baseline_at
                        ):
                            if mark_activated(
                                new_voucher,
                                when,
                            ):
                                summary[
                                    'activated_vouchers'
                                ] = (
                                    summary.get(
                                        'activated_vouchers',
                                        0,
                                    )
                                    + 1
                                )
                        else:
                            Voucher.objects.filter(
                                pk=new_voucher.pk
                            ).update(
                                used_at=when
                            )

                except Exception as exc:
                    summary[
                        'errors'
                    ].append(
                        f'Could not import RouterOS voucher {username}: {exc}'
                    )

        if renamed_seen:
            try:
                rename_with_service(
                    svc,
                    renamed_seen,
                    present,
                )

                summary[
                    'codes_renamed'
                ] = len(
                    renamed_seen
                )

            except Exception as exc:
                summary[
                    'errors'
                ].append(
                    f'Could not rename {len(renamed_seen)} changed voucher code(s): {exc}'
                )

        try:
            from .voucher_codes import (
                repair_passwords,
                stale_passwords,
            )

            fixed = repair_passwords(
                router,
                stale_passwords(
                    router.business,
                    router,
                    [
                        _clean(r)
                        for r in router_rows
                    ],
                ),
                svc=svc,
            )

            if fixed:
                summary[
                    'code_logins_repaired'
                ] = fixed

        except Exception as exc:
            summary[
                'errors'
            ].append(
                f'Could not repair the login of changed voucher codes: {exc}'
            )

        if binned_seen:
            try:
                remove_with_service(
                    svc,
                    binned_seen,
                )

                Voucher.all_objects.filter(
                    business=router.business,
                    code__in=binned_seen,
                ).update(
                    router_removal='removed',
                    router_removal_note='Removed during full sync',
                )

                summary[
                    'deleted_removed'
                ] = len(
                    binned_seen
                )

            except Exception as exc:
                summary[
                    'errors'
                ].append(
                    f'Could not remove {len(binned_seen)} deleted voucher(s): {exc}'
                )

        notify(
            40,
            'Router vouchers imported — pushing TapTap vouchers',
        )

        try:
            from .sticky import apply_api

            apply_api(
                svc,
                router.business,
            )

        except Exception as exc:
            summary[
                'errors'
            ].append(
                f'Sticky sessions: {exc}'
            )

        # IMPORTANT:
        # Archived vouchers stay permanently in TapTap,
        # but they must never be recreated on MikroTik.
        for voucher in (
            router.vouchers
            .filter(
                source='taptap'
            )
            .exclude(
                status='archived'
            )
        ):
            try:
                plan = (
                    router.business.plans
                    .filter(
                        name__iexact=voucher.plan_name
                    )
                    .first()
                )

                (
                    profile_name,
                    shared,
                    rate,
                ) = voucher_profile(
                    voucher,
                    plan,
                )

                svc.ensure_hotspot_profile(
                    profile_name,
                    shared,
                    rate,
                )

                action, item_id = (
                    svc.upsert_voucher(
                        voucher.code,
                        profile_name,
                        limit_uptime=router_limit(
                            voucher
                        ),
                        comment=(
                            f'TapTap member {voucher.code}'
                            if voucher.is_member
                            else f'TapTap voucher {voucher.code}'
                        ),
                        disabled=(
                            voucher.status
                            != 'active'
                        ),
                        password=voucher.login_password,
                    )
                )

                voucher.mikrotik_id = str(
                    item_id
                    or voucher.mikrotik_id
                )

                voucher.mikrotik_sync_status = (
                    'Synced'
                )

                voucher.mikrotik_sync_error = (
                    ''
                )

                voucher.save(
                    update_fields=[
                        'mikrotik_id',
                        'mikrotik_sync_status',
                        'mikrotik_sync_error',
                    ]
                )

                if action == 'created':
                    summary[
                        'pushed_vouchers'
                    ] += 1

                else:
                    summary[
                        'updated_vouchers'
                    ] += 1

            except Exception as exc:
                voucher.mikrotik_sync_status = (
                    'Error'
                )

                voucher.mikrotik_sync_error = (
                    str(exc)
                )

                voucher.save(
                    update_fields=[
                        'mikrotik_sync_status',
                        'mikrotik_sync_error',
                    ]
                )

                summary[
                    'errors'
                ].append(
                    f'Voucher {voucher.code}: {exc}'
                )

        notify(
            55,
            'TapTap vouchers pushed — importing RouterOS IP bindings',
        )

        try:
            binding_rows = list(svc.bindings())
            bindings_read = True
        except Exception as exc:
            binding_rows, bindings_read = [], False
            summary['errors'].append(f'IP bindings could not be read ({exc}) — kept as they were')
        if bindings_read:
            SyncedIPBinding.objects.filter(
                router=router
            ).update(
                is_present=False
            )

        for raw in binding_rows:
            row = _clean(raw)

            item_id = str(
                row.get(
                    'id',
                    '',
                )
            )

            mac = _normalize_mac(
                row.get(
                    'mac-address',
                    row.get(
                        'mac_address',
                        '',
                    ),
                )
            )

            address = str(
                row.get(
                    'address',
                    '',
                )
            )

            existing = (
                SyncedIPBinding.objects
                .filter(
                    router=router,
                    mikrotik_id=item_id,
                )
                .first()
                if item_id
                else None
            )

            if (
                not existing
                and mac
            ):
                existing = (
                    SyncedIPBinding.objects
                    .filter(
                        router=router,
                        mac_address__iexact=mac,
                        address=address,
                    )
                    .first()
                )

            defaults = {
                'business': router.business,
                'mikrotik_id': item_id,
                'mac_address': mac,
                'address': address,
                'server': row.get(
                    'server',
                    '',
                ),
                'binding_type': row.get(
                    'type',
                    'bypassed',
                ),
                'comment': row.get(
                    'comment',
                    '',
                ),
                'disabled': ros_bool(
                    row.get(
                        'disabled',
                        False,
                    )
                ),
                'source': (
                    existing.source
                    if existing
                    else 'mikrotik'
                ),
                'sync_status': 'Synced',
                'sync_error': '',
                'is_present': True,
                'raw_data': row,
                'last_seen_at': now,
            }

            if existing:
                for key, value in defaults.items():
                    setattr(
                        existing,
                        key,
                        value,
                    )

                existing.save()

            else:
                SyncedIPBinding.objects.create(
                    router=router,
                    **defaults,
                )

            summary[
                'pulled_bindings'
            ] += 1

        for binding in (
            router.synced_ip_bindings
            .filter(
                source='taptap'
            )
        ):
            try:
                action, item_id = (
                    svc.upsert_binding(
                        binding
                    )
                )

                binding.mikrotik_id = str(
                    item_id
                    or binding.mikrotik_id
                )

                binding.sync_status = (
                    'Synced'
                )

                binding.sync_error = (
                    ''
                )

                binding.is_present = (
                    True
                )

                binding.last_seen_at = (
                    now
                )

                binding.save(
                    update_fields=[
                        'mikrotik_id',
                        'sync_status',
                        'sync_error',
                        'is_present',
                        'last_seen_at',
                        'updated_at',
                    ]
                )

                summary[
                    (
                        'pushed_bindings'
                        if action
                        == 'created'
                        else 'updated_bindings'
                    )
                ] += 1

            except Exception as exc:
                binding.sync_status = (
                    'Error'
                )

                binding.sync_error = (
                    str(exc)
                )

                binding.save(
                    update_fields=[
                        'sync_status',
                        'sync_error',
                        'updated_at',
                    ]
                )

                summary[
                    'errors'
                ].append(
                    f'IP binding {binding.mac_address or binding.address}: {exc}'
                )

        notify(
            70,
            'Bindings synchronized — scanning MACs, ports and neighbors',
        )

        try:
            topo_data = (
                svc.topology_data()
            )

            topology = _persist_topology(
                router,
                topo_data,
                now,
            )
        except Exception as exc:
            summary['errors'].append(f'Network topology could not be read ({exc}) — the last map was kept')

        summary[
            'devices_discovered'
        ] = (
            router.devices
            .filter(
                is_online=True
            )
            .count()
        )

        notify(
            86,
            'Topology discovered — reading RouterOS configuration',
        )

        try:
            cfg = (
                svc.configuration_snapshot()
            )

            RouterConfigSnapshot.objects.update_or_create(
                router=router,
                defaults={
                    'sections': cfg[
                        'sections'
                    ],
                    'load_balancing': cfg[
                        'load_balancing'
                    ],
                    'captured_at': cfg[
                        'captured_at'
                    ],
                },
            )

            summary[
                'config_sections'
            ] = len(
                cfg['sections']
            )

        except Exception as exc:
            summary[
                'errors'
            ].append(
                f'Configuration snapshot: {exc}'
            )

        notify(
            96,
            'Finalizing database inventory',
        )

        router.status = 'Online'
        router.last_error = ''
        router.last_tested_at = now

        router.save(
            update_fields=[
                'status',
                'last_error',
                'last_tested_at',
            ]
        )

        log(
            router.business,
            'Router Sync',
            (
                f'{router.name}: '
                f'{summary["pulled_plans"]} new plans, '
                f'{summary["pulled_vouchers"]} new vouchers, '
                f'{summary["devices_discovered"]} devices; '
                f'de-duplicated existing records'
            )
            + (
                f'; {summary["prices_found"]} plan prices read from the router'
                if summary.get(
                    'prices_found'
                )
                else ''
            )
            + (
                f'; {summary["prices_repaired"]} voucher prices repaired'
                if summary.get(
                    'prices_repaired'
                )
                else ''
            )
            + (
                '; no price found for: '
                + ', '.join(
                    summary[
                        'plans_without_price'
                    ][:6]
                )
                + ' — set it on the Plans page'
                if summary.get(
                    'plans_without_price'
                )
                else ''
            ),
        )

        if (
            not router.sales_baseline_at
        ):
            type(
                router
            ).objects.filter(
                pk=router.pk,
                sales_baseline_at__isnull=True,
            ).update(
                sales_baseline_at=timezone.now()
            )

        # TapTap's vouchers must sit on TapTap's profile on the router: put back any that drifted.
        try:
            from .profile_time import reconcile_router
            summary['profiles_reconciled'] = reconcile_router(router)
        except Exception as exc:
            summary['errors'].append(f'Profile check: {exc}')

        notify(
            100,
            'Synchronization complete',
        )

        return summary

    finally:
        svc.close()


BULK_BATCH = 500


def _fast_bulk_update(
    model,
    objs,
    fields,
):
    if not objs:
        return

    from django.db import connection

    if (
        connection.vendor
        != 'postgresql'
    ):
        model.objects.bulk_update(
            objs,
            fields,
            batch_size=BULK_BATCH,
        )
        return

    meta = model._meta
    pk = meta.pk

    cols = [
        meta.get_field(f)
        for f in fields
    ]

    table = (
        connection.ops.quote_name(
            meta.db_table
        )
    )

    qn = (
        connection.ops.quote_name
    )

    names = [
        qn(pk.column)
    ] + [
        qn(c.column)
        for c in cols
    ]

    types = [
        pk.db_type(connection)
    ] + [
        c.db_type(connection)
        for c in cols
    ]

    row_sql = (
        '('
        + ', '.join(
            f'%s::{t}'
            for t in types
        )
        + ')'
    )

    set_sql = ', '.join(
        f'{qn(c.column)} = v.{qn(c.column)}'
        for c in cols
    )

    with connection.cursor() as cur:
        for i in range(
            0,
            len(objs),
            BULK_BATCH,
        ):
            batch = objs[
                i:i + BULK_BATCH
            ]

            params = []

            for obj in batch:
                params.append(
                    pk.get_db_prep_value(
                        obj.pk,
                        connection,
                    )
                )

                for c in cols:
                    params.append(
                        c.get_db_prep_save(
                            getattr(
                                obj,
                                c.attname,
                            ),
                            connection,
                        )
                    )

            cur.execute(
                f'UPDATE {table} AS t '
                f'SET {set_sql} '
                f'FROM (VALUES '
                + ', '.join(
                    [row_sql]
                    * len(batch)
                )
                + f') AS v({", ".join(names)}) '
                f'WHERE t.{qn(pk.column)} '
                f'= v.{qn(pk.column)}',
                params,
            )


def _upsert(
    model,
    router,
    key_field,
    desired,
    fields,
    now,
    present_flag,
    extra_create=None,
):
    existing = {
        getattr(
            o,
            key_field,
        ): o
        for o in model.objects.filter(
            router=router
        )
    }

    changed = []
    unchanged_pks = []
    new = []

    for key, values in desired.items():
        obj = existing.get(key)

        if obj is None:
            new.append(
                model(
                    router=router,
                    **{
                        key_field: key
                    },
                    **values,
                    **(
                        extra_create
                        or {}
                    ),
                )
            )
            continue

        if any(
            getattr(
                obj,
                f,
            )
            != values[f]
            for f in fields
        ):
            for f in fields:
                setattr(
                    obj,
                    f,
                    values[f],
                )

            changed.append(obj)

        else:
            unchanged_pks.append(
                obj.pk
            )

    stamp = {
        present_flag: True,
        'last_seen_at': now,
    }

    if any(
        f.name == 'updated_at'
        for f in model._meta.fields
    ):
        stamp[
            'updated_at'
        ] = now

        for obj in changed:
            obj.updated_at = now

    for obj in changed:
        setattr(
            obj,
            present_flag,
            True,
        )

        obj.last_seen_at = now

    if changed:
        _fast_bulk_update(
            model,
            changed,
            list(
                dict.fromkeys(
                    fields
                    + list(stamp)
                )
            ),
        )

    for i in range(
        0,
        len(unchanged_pks),
        2000,
    ):
        model.objects.filter(
            pk__in=unchanged_pks[
                i:i + 2000
            ]
        ).update(
            **stamp
        )

    if new:
        model.objects.bulk_create(
            new,
            batch_size=BULK_BATCH,
        )

    gone = [
        o.pk
        for k, o in existing.items()
        if (
            k not in desired
            and getattr(
                o,
                present_flag,
            )
        )
    ]

    for i in range(
        0,
        len(gone),
        2000,
    ):
        model.objects.filter(
            pk__in=gone[
                i:i + 2000
            ]
        ).update(
            **{
                present_flag: False
            }
        )

    if new:
        existing = {
            getattr(
                o,
                key_field,
            ): o
            for o in model.objects.filter(
                router=router,
                **{
                    key_field
                    + '__in':
                    list(desired)
                },
            )
        }

    else:
        for obj in changed:
            existing[
                getattr(
                    obj,
                    key_field,
                )
            ] = obj

    return {
        k: existing[k]
        for k in desired
        if k in existing
    }


def _persist_topology(
    router,
    data,
    now=None,
):
    now = (
        now
        or timezone.now()
    )

    interfaces = [
        _clean(x)
        for x in data.get(
            'interfaces',
            [],
        )
    ]

    ethernet = {
        _clean(x).get(
            'name'
        ): _clean(x)
        for x in data.get(
            'ethernet',
            [],
        )
    }

    bridge_ports = {
        _clean(x).get(
            'interface'
        ): _clean(x)
        for x in data.get(
            'bridge_ports',
            [],
        )
    }

    bridge_hosts = [
        _clean(x)
        for x in data.get(
            'bridge_hosts',
            [],
        )
    ]

    dhcp = [
        _clean(x)
        for x in data.get(
            'dhcp_leases',
            [],
        )
    ]

    arp = [
        _clean(x)
        for x in data.get(
            'arp',
            [],
        )
    ]

    hotspot_hosts = [
        _clean(x)
        for x in data.get(
            'hotspot_hosts',
            [],
        )
    ]

    active = [
        _clean(x)
        for x in data.get(
            'active_users',
            [],
        )
    ]

    wifi_regs = [
        _clean(x)
        for x in data.get(
            'wifi_registrations',
            [],
        )
    ]

    remote_caps = [
        _clean(x)
        for x in data.get(
            'remote_caps',
            [],
        )
    ]

    with transaction.atomic():
        iface_fields = [
            'default_name',
            'interface_type',
            'mac_address',
            'comment',
            'running',
            'disabled',
            'mtu',
            'rx_byte',
            'tx_byte',
            'raw_data',
        ]

        wanted_ifaces = {}

        for row in interfaces:
            name = str(
                row.get(
                    'name',
                    '',
                )
            ).strip()

            if not name:
                continue

            eth = ethernet.get(
                name,
                {},
            )

            wanted_ifaces[
                name
            ] = {
                'default_name': eth.get(
                    'default-name',
                    eth.get(
                        'default_name',
                        '',
                    ),
                ),
                'interface_type': row.get(
                    'type',
                    '',
                ),
                'mac_address': _normalize_mac(
                    row.get(
                        'mac-address',
                        row.get(
                            'mac_address',
                            eth.get(
                                'mac-address',
                                '',
                            ),
                        ),
                    )
                ),
                'comment': row.get(
                    'comment',
                    '',
                ),
                'running': ros_bool(
                    row.get(
                        'running',
                        False,
                    )
                ),
                'disabled': ros_bool(
                    row.get(
                        'disabled',
                        False,
                    )
                ),
                'mtu': str(
                    row.get(
                        'actual-mtu',
                        row.get(
                            'mtu',
                            '',
                        ),
                    )
                ),
                'rx_byte': _safe_int(
                    row.get(
                        'rx-byte',
                        row.get(
                            'rx_byte',
                            0,
                        ),
                    )
                ),
                'tx_byte': _safe_int(
                    row.get(
                        'tx-byte',
                        row.get(
                            'tx_byte',
                            0,
                        ),
                    )
                ),
                'raw_data': {
                    **row,
                    'ethernet': eth,
                    'bridge_port': bridge_ports.get(
                        name,
                        {},
                    ),
                },
            }

        interface_map = _upsert(
            RouterInterface,
            router,
            'name',
            wanted_ifaces,
            iface_fields,
            now,
            'is_present',
        )

        known_roles = set(
            RouterInterfaceRole.objects
            .filter(
                router=router
            )
            .values_list(
                'interface_name',
                flat=True,
            )
        )

        RouterInterfaceRole.objects.bulk_create(
            [
                RouterInterfaceRole(
                    router=router,
                    interface_name=n,
                    role='unused',
                )
                for n in wanted_ifaces
                if n not in known_roles
            ],
            batch_size=BULK_BATCH,
            ignore_conflicts=True,
        )

        nb_fields = [
            'identity',
            'address',
            'mac_address',
            'interface_name',
            'platform',
            'board',
            'version',
            'discovered_by',
            'device_kind',
            'raw_data',
        ]

        wanted_nb = {}

        for raw in data.get(
            'neighbors',
            [],
        ):
            row = _clean(raw)

            identity = str(
                row.get(
                    'identity',
                    '',
                )
            ).strip()

            address = str(
                row.get(
                    'address',
                    '',
                )
            ).strip()

            mac = _normalize_mac(
                row.get(
                    'mac-address',
                    row.get(
                        'mac_address',
                        '',
                    ),
                )
            )

            iface = str(
                row.get(
                    'interface',
                    '',
                )
            ).split(',')[0].strip()

            key = (
                mac
                or '|'.join(
                    [
                        identity,
                        address,
                        iface,
                    ]
                )
            )

            if not key:
                continue

            wanted_nb[
                key
            ] = {
                'identity': identity,
                'address': address,
                'mac_address': mac,
                'interface_name': iface,
                'platform': row.get(
                    'platform',
                    '',
                ),
                'board': row.get(
                    'board',
                    '',
                ),
                'version': row.get(
                    'version',
                    '',
                ),
                'discovered_by': row.get(
                    'discovered-by',
                    row.get(
                        'discovered_by',
                        '',
                    ),
                ),
                'device_kind': _neighbor_kind(
                    row
                ),
                'raw_data': row,
            }

        for row in remote_caps:
            identity = str(
                row.get(
                    'identity',
                    row.get(
                        'name',
                        'Remote CAP',
                    ),
                )
            )

            mac = _normalize_mac(
                row.get(
                    'base-mac',
                    row.get(
                        'base_mac',
                        '',
                    ),
                )
            )

            address = str(
                row.get(
                    'address',
                    '',
                )
            )

            key = (
                mac
                or f'cap|{identity}|{address}'
            )

            wanted_nb[
                key
            ] = {
                'identity': identity,
                'address': address,
                'mac_address': mac,
                'interface_name': str(
                    row.get(
                        'interface',
                        'CAPsMAN',
                    )
                ),
                'platform': 'CAPsMAN',
                'board': row.get(
                    'board-name',
                    row.get(
                        'board_name',
                        '',
                    ),
                ),
                'version': row.get(
                    'version',
                    '',
                ),
                'discovered_by': 'capsman',
                'device_kind': 'wifi',
                'raw_data': row,
            }

        neighbors = list(
            _upsert(
                RouterNeighbor,
                router,
                'neighbor_key',
                wanted_nb,
                nb_fields,
                now,
                'is_online',
            ).values()
        )

        merged = {}

        def touch(
            mac='',
            ip='',
            hostname='',
            iface='',
            source='',
            kind='',
            raw=None,
        ):
            mac = _normalize_mac(mac)
            ip = str(ip or '').strip()
            hostname = str(
                hostname or ''
            ).strip()
            iface = str(
                iface or ''
            ).strip()

            key = (
                mac
                or (
                    f'ip:{ip}'
                    if ip
                    else (
                        f'name:{hostname}'
                        if hostname
                        else ''
                    )
                )
            )

            if not key:
                return

            item = merged.setdefault(
                key,
                {
                    'mac': mac,
                    'ip': ip,
                    'hostname': hostname,
                    'iface': iface,
                    'sources': set(),
                    'kind': (
                        kind
                        or 'wired'
                    ),
                    'raw': {},
                },
            )

            if mac:
                item['mac'] = mac

            if ip:
                item['ip'] = ip

            if hostname:
                item['hostname'] = (
                    hostname
                )

            if iface:
                item['iface'] = iface

            if kind:
                item['kind'] = kind

            if source:
                item[
                    'sources'
                ].add(source)

            if raw:
                item['raw'][
                    source
                    or 'source'
                ] = raw

        for x in bridge_hosts:
            if not ros_bool(
                x.get(
                    'local',
                    False,
                )
            ):
                touch(
                    x.get(
                        'mac-address'
                    ),
                    iface=x.get(
                        'on-interface',
                        x.get(
                            'on_interface',
                            '',
                        ),
                    ),
                    source='bridge',
                    kind='wired',
                    raw=x,
                )

        for x in dhcp:
            touch(
                x.get(
                    'mac-address'
                ),
                x.get(
                    'address'
                ),
                x.get(
                    'host-name',
                    x.get(
                        'comment',
                        '',
                    ),
                ),
                x.get(
                    'interface',
                    '',
                ),
                source='dhcp',
                raw=x,
            )

        for x in arp:
            touch(
                x.get(
                    'mac-address'
                ),
                x.get(
                    'address'
                ),
                '',
                x.get(
                    'interface',
                    '',
                ),
                source='arp',
                raw=x,
            )

        for x in hotspot_hosts:
            touch(
                x.get(
                    'mac-address'
                ),
                x.get(
                    'address'
                ),
                x.get(
                    'user',
                    '',
                ),
                '',
                source='hotspot-host',
                raw=x,
            )

        for x in active:
            touch(
                x.get(
                    'mac-address'
                ),
                x.get(
                    'address'
                ),
                x.get(
                    'user',
                    '',
                ),
                '',
                source='hotspot-active',
                raw=x,
            )

        for x in wifi_regs:
            touch(
                x.get(
                    'mac-address'
                ),
                '',
                x.get(
                    'comment',
                    '',
                ),
                x.get(
                    'interface',
                    '',
                ),
                source='wifi',
                kind='wifi',
                raw=x,
            )

        for n in neighbors:
            touch(
                n.mac_address,
                n.address,
                n.identity,
                n.interface_name,
                source='neighbor',
                kind=n.device_kind,
                raw=n.raw_data,
            )

        neighbors_by_iface = {}

        for n in sorted(
            neighbors,
            key=lambda n: (
                n.identity
                or ''
            ),
        ):
            if (
                n.interface_name
                and n.interface_name
                not in neighbors_by_iface
            ):
                neighbors_by_iface[
                    n.interface_name
                ] = n

        dev_fields = [
            'mac_address',
            'ip_address',
            'hostname',
            'interface_name',
            'parent_identity',
            'connection_type',
            'sources',
            'raw_data',
        ]

        wanted_dev = {}

        for key, item in merged.items():
            parent = neighbors_by_iface.get(
                item['iface']
            )

            wanted_dev[
                key
            ] = {
                'mac_address': item[
                    'mac'
                ],
                'ip_address': item[
                    'ip'
                ],
                'hostname': item[
                    'hostname'
                ],
                'interface_name': item[
                    'iface'
                ],
                'parent_identity': (
                    parent.identity
                    or parent.address
                    or parent.mac_address
                )
                if parent
                else '',
                'connection_type': item[
                    'kind'
                ],
                'sources': ', '.join(
                    sorted(
                        item[
                            'sources'
                        ]
                    )
                ),
                'raw_data': item[
                    'raw'
                ],
            }

        _upsert(
            RouterDevice,
            router,
            'device_key',
            wanted_dev,
            dev_fields,
            now,
            'is_online',
            extra_create={
                'first_seen_at': now
            },
        )

    clients_by_interface = (
        defaultdict(list)
    )

    for key in sorted(
        wanted_dev
    ):
        dev = wanted_dev[
            key
        ]

        if dev[
            'interface_name'
        ]:
            clients_by_interface[
                dev[
                    'interface_name'
                ]
            ].append(
                {
                    'mac': dev[
                        'mac_address'
                    ],
                    'name': dev[
                        'hostname'
                    ],
                    'ip': dev[
                        'ip_address'
                    ],
                    'kind': dev[
                        'connection_type'
                    ],
                    'parent': dev[
                        'parent_identity'
                    ],
                }
            )

    neighbors_on_port = (
        defaultdict(list)
    )

    for n in sorted(
        neighbors,
        key=lambda n: (
            n.pk
            or 0
        ),
    ):
        if n.interface_name:
            neighbors_on_port[
                n.interface_name
            ].append(n)

    role_map = {
        x.interface_name: x
        for x in router.interface_roles.all()
    }

    ports = []

    for row in interfaces:
        name = str(
            row.get(
                'name',
                '',
            )
        )

        itype = str(
            row.get(
                'type',
                '',
            )
        ).lower()

        if (
            itype not in {
                'ether',
                'ethernet',
            }
            and not name.lower().startswith(
                (
                    'ether',
                    'sfp',
                    'qsfp',
                    'combo',
                )
            )
        ):
            continue

        obj = interface_map.get(
            name
        )

        role = role_map.get(
            name
        )

        ports.append(
            {
                'name': name,
                'running': (
                    bool(
                        obj.running
                    )
                    if obj
                    else False
                ),
                'disabled': (
                    bool(
                        obj.disabled
                    )
                    if obj
                    else False
                ),
                'mac_address': (
                    obj.mac_address
                    if obj
                    else ''
                ),
                'comment': (
                    obj.comment
                    if obj
                    else ''
                ),
                'rx_byte': (
                    obj.rx_byte
                    if obj
                    else 0
                ),
                'tx_byte': (
                    obj.tx_byte
                    if obj
                    else 0
                ),
                'neighbors': neighbors_on_port.get(
                    name,
                    [],
                ),
                'clients': clients_by_interface.get(
                    name,
                    [],
                )[:8],
                'client_count': len(
                    clients_by_interface.get(
                        name,
                        [],
                    )
                ),
                'bridge': bridge_ports.get(
                    name,
                    {},
                ).get(
                    'bridge',
                    '',
                ),
                'pvid': bridge_ports.get(
                    name,
                    {},
                ).get(
                    'pvid',
                    '',
                ),
                'role': (
                    role.role
                    if role
                    else 'unused'
                ),
                'role_label': (
                    role.get_role_display()
                    if role
                    else 'Unused'
                ),
            }
        )

    wifi_clients_view = [
        {
            **x,
            'mac_address': x.get(
                'mac-address',
                x.get(
                    'mac_address',
                    '',
                ),
            ),
            'last_activity': x.get(
                'last-activity',
                x.get(
                    'last_activity',
                    '',
                ),
            ),
        }
        for x in wifi_regs
    ]

    return {
        'router': router,
        'identity': _clean(
            data.get(
                'identity',
                {},
            )
        ),
        'routerboard': _clean(
            data.get(
                'routerboard',
                {},
            )
        ),
        'ports': ports,
        'neighbors': list(
            router.neighbors.all()
            .order_by(
                '-is_online',
                'identity',
            )
        ),
        'wifi_clients': wifi_clients_view,
        'devices': list(
            router.devices
            .filter(
                is_online=True
            )
            .order_by(
                'interface_name',
                'hostname',
                'mac_address',
            )
        ),
        'captured_at': now,
        'error': '',
    }


def refresh_router_topology(
    router,
    timeout=None,
):
    svc = MikroTikService(
        router,
        timeout=timeout,
    ).connect()

    now = timezone.now()

    try:
        data = (
            svc.topology_data()
        )

        snap = _persist_topology(
            router,
            data,
            now,
        )

        lb = svc.analyze_load_balancing(
            routes=data.get(
                'routes',
                [],
            ),
            mangle=data.get(
                'mangle',
                [],
            ),
            bonding=data.get(
                'bonding',
                [],
            ),
            routing_tables=data.get(
                'routing_tables',
                [],
            ),
            routing_rules=data.get(
                'routing_rules',
                [],
            ),
            addresses=data.get(
                'addresses',
                [],
            ),
            dhcp_clients=data.get(
                'dhcp_clients',
                [],
            ),
            interfaces=data.get(
                'interfaces',
                [],
            ),
            hotspot_servers=data.get(
                'hotspot_servers',
                [],
            ),
        )

        snap[
            'load_balancing'
        ] = lb

        identity = _clean(
            data.get(
                'identity',
                {},
            )
        )

        board = _clean(
            data.get(
                'routerboard',
                {},
            )
        )

        cfg, created = (
            RouterConfigSnapshot.objects
            .get_or_create(
                router=router,
                defaults={
                    'sections': {},
                    'load_balancing': lb,
                    'captured_at': now,
                },
            )
        )

        if not created:
            cfg.load_balancing = lb

        sections = dict(
            cfg.sections
            or {}
        )

        sections[
            '_identity'
        ] = {
            'path': '/system/identity',
            'count': 1,
            'rows': [
                {
                    'name': identity.get(
                        'name',
                        '',
                    ),
                    'model': board.get(
                        'model',
                        '',
                    ),
                    'serial': board.get(
                        'serial-number',
                        '',
                    ),
                }
            ],
        }

        sections[
            'IP addresses'
        ] = {
            'path': '/ip/address',
            'count': len(
                data.get(
                    'addresses',
                    [],
                )
            ),
            'rows': redact(
                [
                    dict(x)
                    for x in data.get(
                        'addresses',
                        [],
                    )[:500]
                ]
            ),
        }

        cfg.sections = sections

        cfg.save(
            update_fields=[
                'load_balancing',
                'sections',
                'updated_at',
            ]
        )

        router.status = 'Online'
        router.last_error = ''
        router.last_tested_at = now

        router.save(
            update_fields=[
                'status',
                'last_error',
                'last_tested_at',
            ]
        )

        return snap

    finally:
        svc.close()


def snapshot_from_database(
    router,
):
    ports = []

    role_map = {
        x.interface_name: x
        for x in router.interface_roles.all()
    }

    for obj in (
        router.interfaces
        .filter(
            is_present=True
        )
        .order_by(
            'name'
        )
    ):
        name = obj.name

        if (
            obj.interface_type.lower()
            not in {
                'ether',
                'ethernet',
            }
            and not name.lower().startswith(
                (
                    'ether',
                    'sfp',
                    'qsfp',
                    'combo',
                )
            )
        ):
            continue

        role = role_map.get(
            name
        )

        devices = list(
            router.devices.filter(
                is_online=True,
                interface_name=name,
            )[:8]
        )

        ports.append(
            {
                'name': name,
                'running': obj.running,
                'disabled': obj.disabled,
                'mac_address': obj.mac_address,
                'comment': obj.comment,
                'rx_byte': obj.rx_byte,
                'tx_byte': obj.tx_byte,
                'neighbors': list(
                    router.neighbors.filter(
                        interface_name=name
                    ).order_by(
                        '-is_online',
                        'identity',
                    )
                ),
                'clients': [
                    {
                        'mac': d.mac_address,
                        'name': d.hostname,
                        'ip': d.ip_address,
                        'kind': d.connection_type,
                        'parent': d.parent_identity,
                    }
                    for d in devices
                ],
                'client_count': (
                    router.devices
                    .filter(
                        is_online=True,
                        interface_name=name,
                    )
                    .count()
                ),
                'bridge': (
                    obj.raw_data.get(
                        'bridge_port',
                        {},
                    ).get(
                        'bridge',
                        '',
                    )
                    if obj.raw_data
                    else ''
                ),
                'pvid': (
                    obj.raw_data.get(
                        'bridge_port',
                        {},
                    ).get(
                        'pvid',
                        '',
                    )
                    if obj.raw_data
                    else ''
                ),
                'role': (
                    role.role
                    if role
                    else 'unused'
                ),
                'role_label': (
                    role.get_role_display()
                    if role
                    else 'Unused'
                ),
            }
        )

    try:
        lb = (
            router.config_snapshot
            .load_balancing
        )
    except Exception:
        lb = {}

    return {
        'router': router,
        'identity': {},
        'routerboard': {},
        'ports': ports,
        'neighbors': list(
            router.neighbors.all()
            .order_by(
                '-is_online',
                'identity',
            )
        ),
        'wifi_clients': [],
        'devices': list(
            router.devices
            .filter(
                is_online=True
            )
            .order_by(
                'interface_name',
                'hostname',
            )
        ),
        'load_balancing': lb,
        'captured_at': (
            router.last_tested_at
        ),
        'error': (
            router.last_error
            or (
                'Router currently unreachable. '
                'Showing the last saved discovery snapshot.'
            )
        ),
    }