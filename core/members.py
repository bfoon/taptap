"""Members: username/password HotSpot users assigned to reusable Member Plans.

Important design:
- A member is still a core.Voucher with login_type='member'. Existing voucher
  enforcement, expiry, device lock, finance, reports and router sync therefore
  continue to work without a second customer/account engine.
- Every NEW member must be assigned to a MemberPlan created by the business.
- Devices, speed, price and validity come from that MemberPlan. They are not
  entered independently on the New Member form.
- Every MemberPlan has one stable MikroTik profile name:
      taptap-member-plan-<id>
  so all members on that plan share the same HotSpot user profile.
- Member contact/reminder choices are stored separately from the Voucher row.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from .models import Voucher, VoucherCodeAlias, VoucherSale
from .models_member_plans import (
    MemberNotificationSettings,
    MemberPlan,
    MemberPlanAssignment,
    MemberRenewal,
)


USERNAME_RE = re.compile(r'^[a-z0-9][a-z0-9._@-]{2,31}$')
RATE_RE = re.compile(r'^\d+[kKmM]?(/\d+[kKmM]?)?$')
PASSWORD_MIN, PASSWORD_MAX = 4, 64

# Kept only so older imports do not crash. It is no longer a selectable/built-in
# plan and create_member() will never manufacture it.
FREE_PLAN_NAME = 'Member — free, unlimited'


class MemberError(ValueError):
    """A problem the person can fix; the message is shown as is."""


def clean_username(value):
    """Usernames are lower-case and cannot contain spaces."""
    return re.sub(r'\s+', '', str(value or '')).lower()


def check_password(password):
    pw = str(password or '')
    if pw != pw.strip() or re.search(r'\s', pw):
        raise MemberError('The password cannot contain spaces.')
    if not PASSWORD_MIN <= len(pw) <= PASSWORD_MAX:
        raise MemberError(
            f'The password must be {PASSWORD_MIN}–{PASSWORD_MAX} characters.'
        )
    if any(ord(ch) < 32 for ch in pw):
        raise MemberError(
            'The password contains a character that cannot be used.'
        )
    return pw


def username_taken(username, exclude=None):
    qs = Voucher.all_objects.filter(code__iexact=username)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    return (
        qs.exists()
        or VoucherCodeAlias.objects.filter(code__iexact=username).exists()
    )


def check_username(username, exclude=None):
    name = clean_username(username)
    if not USERNAME_RE.match(name):
        raise MemberError(
            'A username must be 3–32 characters: small letters, numbers, '
            'dot, dash, underscore or @, starting with a letter or number.'
        )
    if username_taken(name, exclude=exclude):
        raise MemberError(
            f'“{name}” is already used by another member or voucher code. '
            'Choose another username.'
        )
    return name


def stored_password(username, password, same):
    """What to keep in Voucher.password: '' when it equals the username."""
    if same or str(password or '') == username:
        return ''
    return check_password(password)


def _money(value, label='Price'):
    try:
        amount = Decimal(str(value or '0').replace(',', '').strip() or '0')
    except (InvalidOperation, ValueError):
        raise MemberError(f'{label} must be a number.')
    if amount < 0:
        raise MemberError(f'{label} cannot be negative.')
    if amount > Decimal('99999999.99'):
        raise MemberError(f'{label} is too large.')
    return amount.quantize(Decimal('0.01'))


def _devices(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise MemberError('Devices must be a whole number.')
    if not 1 <= n <= 20:
        raise MemberError('Devices must be between 1 and 20.')
    return n


def _speed(value):
    rate = str(value or '').strip()[:50]
    if rate and not RATE_RE.fullmatch(rate):
        raise MemberError(
            'Speed looks wrong — use a form like 5M/5M or 2M, '
            'or leave it empty for full speed.'
        )
    return rate


def _email(value):
    email = str(value or '').strip().lower()[:254]
    if not email:
        return ''
    try:
        validate_email(email)
    except ValidationError as exc:
        raise MemberError('Enter a valid member email address.') from exc
    return email


def plan_choice(business, value, *, active_only=True):
    """Return a business-owned MemberPlan from a submitted id."""
    raw = str(value or '')
    qs = business.member_plans.all()
    if active_only:
        qs = qs.filter(active=True)
    plan = qs.filter(pk=int(raw)).first() if raw.isdigit() else None
    if not plan:
        raise MemberError(
            'Choose one of your Member Plans. Create the Member Plan first '
            'if it is not listed.'
        )
    return plan


def plan_for_member(voucher):
    """The reusable MemberPlan assigned to this member, or None for legacy members."""
    try:
        link = voucher.member_plan_assignment
    except (MemberPlanAssignment.DoesNotExist, AttributeError):
        return None
    return link.plan


def notification_settings_for(voucher):
    """Return saved member email/reminder settings or None."""
    try:
        return voucher.member_notification_settings
    except (MemberNotificationSettings.DoesNotExist, AttributeError):
        return None


def save_member_notifications(voucher, data, *, user=None):
    """Validate and save the member's email and optional expiry reminders."""
    if not voucher.is_member:
        raise MemberError('Only members can have member reminder settings.')

    email = _email(data.get('customer_email', data.get('email', '')))
    reminders_enabled = bool(data.get('reminders_enabled'))
    current = notification_settings_for(voucher)
    if reminders_enabled:
        remind_7 = bool(data.get('remind_7_days'))
        remind_2 = bool(data.get('remind_2_days'))
        remind_1 = bool(data.get('remind_1_day'))
    else:
        # Disabled checkboxes are not submitted by browsers. Keep the member's
        # chosen thresholds ready for the next time reminders are enabled.
        remind_7 = current.remind_7_days if current else True
        remind_2 = current.remind_2_days if current else True
        remind_1 = current.remind_1_day if current else True

    if reminders_enabled and not email:
        raise MemberError(
            'Enter the member email address before enabling expiry reminders.'
        )
    if reminders_enabled and not any((remind_7, remind_2, remind_1)):
        raise MemberError(
            'Choose at least one reminder: 7 days, 2 days or 1 day before expiry.'
        )

    pref, _ = MemberNotificationSettings.objects.update_or_create(
        member=voucher,
        defaults={
            'email': email,
            'reminders_enabled': reminders_enabled,
            'remind_7_days': remind_7,
            'remind_2_days': remind_2,
            'remind_1_day': remind_1,
            'updated_by': (
                user if getattr(user, 'is_authenticated', False) else None
            ),
        },
    )
    return pref


def kind_of(voucher):
    """'free' when the assigned/current member price is zero, else 'paid'."""
    plan = plan_for_member(voucher)
    price = plan.price if plan is not None else voucher.price
    return 'free' if not price else 'paid'


def _plan_snapshot(plan):
    return {
        'plan_name': plan.name,
        'price': plan.price,
        'duration_minutes': plan.duration_minutes,
        'max_devices': plan.max_devices,
        'rate_limit': plan.speed_limit,
        'router_profile': plan.router_profile_name,
    }


@transaction.atomic
def save_member_plan(business, data, *, plan=None, user=None):
    """Create/update a reusable MemberPlan.

    Returns (plan, number_of_existing_members_refreshed).
    Existing members inherit name/price/devices/speed/profile immediately. A
    member that has already started its current period keeps its current expiry;
    the edited validity is used on its next renewal.
    """
    from .durations import to_minutes

    if plan is not None and plan.business_id != business.pk:
        raise MemberError('That Member Plan does not belong to this business.')

    name = str(data.get('name') or '').strip()[:80]
    if not name:
        raise MemberError('Give the Member Plan a name.')

    dup = business.member_plans.filter(name__iexact=name)
    if plan is not None:
        dup = dup.exclude(pk=plan.pk)
    if dup.exists():
        raise MemberError(
            f'A Member Plan named “{name}” already exists.'
        )

    price = _money(data.get('price'))
    devices = _devices(data.get('max_devices') or 1)
    rate = _speed(data.get('speed_limit'))

    unit = str(data.get('duration_unit') or 'months')
    try:
        minutes = to_minutes(
            data.get('duration_value') or 1,
            unit,
        )
    except ValueError as exc:
        raise MemberError(str(exc)) from exc

    if plan is None:
        plan = MemberPlan(
            business=business,
            created_by=user if getattr(user, 'is_authenticated', False) else None,
        )

    plan.name = name
    plan.price = price
    plan.duration_minutes = minutes
    plan.duration_unit = unit
    plan.max_devices = devices
    plan.speed_limit = rate
    plan.save()
    try:     # the plan's router-profile row on the Plans page follows the new price / validity at once
        from .member_plan_prices import sync_business
        sync_business(business)
    except Exception:
        pass

    # Refresh every linked member's reusable-plan facts. Current expiry is not
    # recalculated for members already inside a running period.
    refreshed = 0
    assignments = list(
        plan.assignments.select_related('member').all()
    )
    for assignment in assignments:
        member = assignment.member
        updates = {
            'plan_name': plan.name,
            'price': plan.price,
            'max_devices': plan.max_devices,
            'rate_limit': plan.speed_limit,
            'router_profile': plan.router_profile_name,
            'mikrotik_sync_status': 'Pending',
            'mikrotik_sync_error': '',
        }
        from .member_time import rebase
        updates.update(rebase(member, plan.duration_minutes))   # a new plan length: time already used still counts
        Voucher.objects.filter(pk=member.pk).update(**updates)
        refreshed += 1

    if refreshed:
        _push_after_commit([a.member_id for a in assignments], plan)
    return plan, refreshed


@transaction.atomic
def set_plan_active(plan, active):
    plan.active = bool(active)
    plan.save(update_fields=['active', 'updated_at'])
    return plan


@transaction.atomic
def assign_member_plan(voucher, plan, *, user=None):
    """Assign/change an existing member to one active MemberPlan.

    A member whose time has started keeps their clock: the days already used count in the new plan
    (core/member_time.py). The caller sends the member to the router afterwards (push_one).
    """
    if not voucher.is_member:
        raise MemberError('Only members can be assigned to a Member Plan.')
    if voucher.business_id != plan.business_id:
        raise MemberError(
            'The member and Member Plan belong to different businesses.'
        )
    if not plan.active:
        raise MemberError(
            'That Member Plan is inactive. Enable it before assigning members.'
        )

    MemberPlanAssignment.objects.update_or_create(
        member=voucher,
        defaults={
            'plan': plan,
            'assigned_by': (
                user if getattr(user, 'is_authenticated', False) else None
            ),
        },
    )

    from .member_time import describe_change, rebase
    values = _plan_snapshot(plan)
    clock = rebase(voucher, plan.duration_minutes)          # time already used counts in the new plan
    values.update(clock)
    values.update({
        'mikrotik_sync_status': 'Pending',
        'mikrotik_sync_error': '',
    })
    Voucher.objects.filter(pk=voucher.pk).update(**values)

    for key, value in values.items():
        if hasattr(voucher, key):
            setattr(voucher, key, value)

    from .voucher_history import record
    record(voucher, 'note', user=user, reason=f'Member Plan: {plan.name}', text=describe_change(voucher, clock))
    return voucher                      # the page sends the member to the router right after (views_members)


def _push_after_commit(ids, plan):
    """Send members to their routers once the change is saved, so the router has the plan's time and speed."""
    def go():
        from .views_agents import push_one
        for v in Voucher.objects.filter(pk__in=ids).exclude(router__isnull=True).select_related('router'):
            try:
                push_one(v, plan)
            except Exception:            # router offline: it stays Pending and the next sync sends it
                pass
    transaction.on_commit(go)


@transaction.atomic
def create_member(
    business,
    *,
    username,
    password='',
    same=False,
    plan_value='',
    router=None,
    agent=None,
    customer_name='',
    customer_phone='',
    customer_email='',
    reminders_enabled=False,
    remind_7_days=True,
    remind_2_days=True,
    remind_1_day=True,
    note='',
    paid=False,
    method='cash',
    reference='',
    user=None,
    # Accepted for source compatibility only. The values are intentionally ignored:
    # devices/speed must come from MemberPlan now.
    devices=None,
    rate_limit='',
):
    """Create one member from an existing reusable MemberPlan."""
    from .finance import record_sale
    from .serials import allocate

    name = check_username(username)
    pw = stored_password(name, password, same)
    plan = plan_choice(business, plan_value, active_only=True)

    # Validate email/reminder choices before the Voucher row is created. Because
    # this function is atomic, any later failure also rolls back the member.
    email = _email(customer_email)
    if reminders_enabled and not email:
        raise MemberError(
            'Enter the member email address before enabling expiry reminders.'
        )
    if reminders_enabled and not any(
        (remind_7_days, remind_2_days, remind_1_day)
    ):
        raise MemberError(
            'Choose at least one reminder: 7 days, 2 days or 1 day before expiry.'
        )

    v = Voucher.objects.create(
        business=business,
        router=router,
        code=name,
        password=pw,
        login_type='member',
        serial=allocate(business, 1, plan=plan.name)[0],
        plan_name=plan.name,
        price=plan.price,
        duration_minutes=plan.duration_minutes,
        max_devices=plan.max_devices,
        rate_limit=plan.speed_limit,
        router_profile=plan.router_profile_name,
        source='taptap',
        agent=agent,
        customer_name=(customer_name or '').strip()[:120],
        customer_phone=(customer_phone or '').strip()[:60],
        note=(note or '').strip()[:255],
        mikrotik_sync_status='Pending',
    )

    MemberPlanAssignment.objects.create(
        member=v,
        plan=plan,
        assigned_by=(
            user if getattr(user, 'is_authenticated', False) else None
        ),
    )

    if not reminders_enabled and not any(
        (remind_7_days, remind_2_days, remind_1_day)
    ):
        # Browsers omit disabled threshold checkboxes. Keep the useful default
        # so turning reminders on later starts with 7d / 2d / 1d selected.
        remind_7_days = remind_2_days = remind_1_day = True

    MemberNotificationSettings.objects.create(
        member=v,
        email=email,
        reminders_enabled=bool(reminders_enabled),
        remind_7_days=bool(remind_7_days),
        remind_2_days=bool(remind_2_days),
        remind_1_day=bool(remind_1_day),
        updated_by=(
            user if getattr(user, 'is_authenticated', False) else None
        ),
    )

    if paid and plan.price and plan.price > 0:
        record_sale(
            business,
            v,
            method=method,
            agent=agent,
            customer_name=v.customer_name,
            customer_phone=v.customer_phone,
            reference=(reference or '')[:120],
            user=user,
            notes=f'Member {name}: first payment · {plan.name}',
        )

    return v


def _push_password(voucher, user=None):
    """Send a changed password to the router. Returns (via, result, ok)."""
    from .voucher_history import channel

    router = voucher.router
    via = channel(router)
    if not router:
        return (
            via,
            'No router assigned — changed in TapTap only',
            True,
        )

    if via == 'TapTap Link':
        from .agent import push_pending_vouchers
        from .linkops import send

        Voucher.objects.filter(pk=voucher.pk).update(
            mikrotik_sync_status='Pending',
            mikrotik_sync_error='',
        )
        push_pending_vouchers(router)
        try:
            send(
                router,
                'disconnect',
                {'user': voucher.code},
                label=f'Sign out member {voucher.code}',
                user=user,
            )
        except ValueError:
            pass
        return (
            via,
            'Queued — the router applies it at its next check-in',
            True,
        )

    from .mikrotik import MikroTikService

    try:
        svc = MikroTikService(router).connect()
        try:
            if svc.set_user_password(
                voucher.code,
                voucher.login_password,
            ):
                return (
                    via,
                    'Password changed on the router; the member was signed out',
                    True,
                )
        finally:
            svc.close()
    except Exception as exc:
        Voucher.objects.filter(pk=voucher.pk).update(
            mikrotik_sync_status='Pending',
            mikrotik_sync_error=str(exc)[:500],
        )
        return (
            via,
            f'Router update failed: {exc}. '
            'TapTap sends it at the next sync.',
            False,
        )

    Voucher.objects.filter(pk=voucher.pk).update(
        mikrotik_sync_status='Pending',
        mikrotik_sync_error='',
    )
    return (
        via,
        'Not on the router yet — it is added with the new password at the next sync',
        True,
    )


def change_password(
    voucher,
    password='',
    same=False,
    user=None,
    reason='',
):
    """Give a member a new password (or make it equal to the username)."""
    from .voucher_history import record
    from .utils import log

    if not voucher.is_member:
        raise MemberError(
            'Only members have a password. A voucher logs in with its code.'
        )
    if voucher.deleted_at:
        raise MemberError('Members in the bin cannot be changed.')

    pw = stored_password(voucher.code, password, same)
    Voucher.objects.filter(pk=voucher.pk).update(password=pw)
    voucher.password = pw

    via, result, ok = _push_password(voucher, user=user)
    record(
        voucher,
        'password_changed',
        user=user,
        reason=(reason or '')[:255],
        via=via,
        router_result=result,
        status_before=voucher.status,
        status_after=voucher.status,
        text=(
            'Password is now the same as the username'
            if not pw
            else 'New password set'
        ),
    )
    log(voucher.business, 'Member Password Changed', voucher.code)
    return ok, result


@transaction.atomic
def renew_with_receipt(
    voucher,
    *,
    amount=None,
    method='cash',
    reference='',
    agent=None,
    user=None,
    require_amount=False,
):
    """Renew a member, book the payment in Finance and create a receipt row.

    ``amount`` is the actual money collected, not merely the plan list price.
    The Members page uses ``require_amount=True`` so the cashier must explicitly
    confirm what was collected. Programmatic callers may omit it to use the plan
    price, preserving the older renew() behaviour.
    """
    from .finance import record_sale
    from . import voucher_history as vh

    if not voucher.is_member:
        raise MemberError('Only members can be renewed.')

    plan = plan_for_member(voucher)
    if plan is None:
        raise MemberError(
            f'{voucher.code} is a legacy member with no Member Plan. '
            'Assign a Member Plan before renewing.'
        )

    minutes = int(plan.duration_minutes or 0)
    if not minutes:
        raise MemberError(
            f'{voucher.code} is on {plan.name}, which has no time limit, '
            'so there is nothing to renew.'
        )

    if require_amount and amount in (None, ''):
        raise MemberError('Enter the amount collected from the member.')

    collected = (
        _money(amount, 'Amount collected')
        if amount not in (None, '')
        else _money(plan.price, 'Amount collected')
    )
    if plan.price > 0 and collected <= 0:
        raise MemberError(
            'This is a paid Member Plan. Enter the amount actually collected.'
        )

    old_expiry = voucher.expires_at

    # A renewal is the point at which the current plan validity becomes the
    # member's new period. Price/devices/speed also refresh from the plan.
    Voucher.objects.filter(pk=voucher.pk).update(
        plan_name=plan.name,
        price=plan.price,
        duration_minutes=plan.duration_minutes,
        max_devices=plan.max_devices,
        rate_limit=plan.speed_limit,
        router_profile=plan.router_profile_name,
        mikrotik_sync_status='Pending',
        mikrotik_sync_error='',
    )
    voucher.plan_name = plan.name
    voucher.price = plan.price
    voucher.duration_minutes = plan.duration_minutes
    voucher.max_devices = plan.max_devices
    voucher.rate_limit = plan.speed_limit
    voucher.router_profile = plan.router_profile_name

    ok, result = vh.enable(
        voucher,
        user=user,
        reason=f'Member renewal · {plan.name}',
        add_minutes=minutes,
    )

    # vh.enable() may update expiry/status directly in the database.
    voucher.refresh_from_db()

    sale = None
    if collected > 0:
        sale = record_sale(
            voucher.business,
            None,
            plan_name=plan.name,
            amount=collected,
            method=method,
            agent=agent,
            customer_name=voucher.customer_name,
            customer_phone=voucher.customer_phone,
            reference=(reference or '')[:120],
            user=user,
            notes=(
                f'Member {voucher.code}: renewal · {plan.name} · '
                f'amount collected {voucher.business.currency}{collected:,.2f}'
            ),
        )
        VoucherSale.objects.filter(pk=sale.pk).update(
            voucher_code=voucher.code
        )
        sale.voucher_code = voucher.code

    pref = notification_settings_for(voucher)
    renewal = MemberRenewal.objects.create(
        business=voucher.business,
        member=voucher,
        plan=plan,
        sale=sale,
        member_username=voucher.code,
        customer_name=voucher.customer_name,
        plan_name=plan.name,
        plan_price=plan.price,
        amount_collected=collected,
        currency=voucher.business.currency,
        payment_method=method,
        reference=(reference or '')[:120],
        duration_minutes=plan.duration_minutes,
        max_devices=plan.max_devices,
        speed_limit=plan.speed_limit,
        old_expires_at=old_expiry,
        new_expires_at=voucher.expires_at,
        recorded_by=(
            user if getattr(user, 'is_authenticated', False) else None
        ),
        email_to=(pref.email if pref else ''),
    )

    return ok, result, sale, minutes, renewal


def renew(
    voucher,
    *,
    amount=None,
    method='cash',
    reference='',
    agent=None,
    user=None,
):
    """Backward-compatible renewal API returning the original four values."""
    ok, result, sale, minutes, _renewal = renew_with_receipt(
        voucher,
        amount=amount,
        method=method,
        reference=reference,
        agent=agent,
        user=user,
        require_amount=False,
    )
    return ok, result, sale, minutes


def member_stats(business):
    qs = business.vouchers.filter(login_type='member')
    total = qs.count()
    free = qs.filter(price=0).count()
    try:
        needs_plan = qs.filter(
            member_plan_assignment__isnull=True
        ).count()
    except Exception:
        needs_plan = 0

    return {
        'total': total,
        'free': free,
        'paid': total - free,
        'disabled': qs.filter(status='disabled').count(),
        'needs_plan': needs_plan,
        'online_ready': qs.filter(
            status='active',
            frozen_at__isnull=True,
        ).count(),
        'now': timezone.now(),
    }
