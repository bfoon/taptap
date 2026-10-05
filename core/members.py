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
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from .models import Voucher, VoucherCodeAlias, VoucherSale
from .models_member_plans import MemberPlan, MemberPlanAssignment


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


def _money(value):
    try:
        amount = Decimal(str(value or '0').replace(',', '').strip() or '0')
    except (InvalidOperation, ValueError):
        raise MemberError('Price must be a number.')
    if amount < 0:
        raise MemberError('Price cannot be negative.')
    if amount > Decimal('99999999.99'):
        raise MemberError('Price is too large.')
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
        if not member.used_at and not member.expires_at:
            updates['duration_minutes'] = plan.duration_minutes
        Voucher.objects.filter(pk=member.pk).update(**updates)
        refreshed += 1

    return plan, refreshed


@transaction.atomic
def set_plan_active(plan, active):
    plan.active = bool(active)
    plan.save(update_fields=['active', 'updated_at'])
    return plan


@transaction.atomic
def assign_member_plan(voucher, plan, *, user=None):
    """Assign/change an existing member to one active MemberPlan.

    If the member has already started a validity period, keep its current expiry.
    The new plan's validity is used on the next renewal.
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

    values = _plan_snapshot(plan)
    if voucher.used_at or voucher.expires_at:
        values.pop('duration_minutes', None)
    values.update({
        'mikrotik_sync_status': 'Pending',
        'mikrotik_sync_error': '',
    })
    Voucher.objects.filter(pk=voucher.pk).update(**values)

    for key, value in values.items():
        if hasattr(voucher, key):
            setattr(voucher, key, value)

    return voucher


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


def renew(
    voucher,
    *,
    amount=None,
    method='cash',
    reference='',
    agent=None,
    user=None,
):
    """Renew a member using the CURRENT values of its assigned MemberPlan."""
    from .finance import d, record_sale
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

    price = (
        d(amount)
        if amount not in (None, '')
        else d(plan.price)
    )
    if price < 0:
        raise MemberError('The amount cannot be negative.')

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

    sale = None
    if price > 0:
        sale = record_sale(
            voucher.business,
            None,
            plan_name=plan.name,
            amount=price,
            method=method,
            agent=agent,
            customer_name=voucher.customer_name,
            customer_phone=voucher.customer_phone,
            reference=(reference or '')[:120],
            user=user,
            notes=f'Member {voucher.code}: renewal · {plan.name}',
        )
        VoucherSale.objects.filter(pk=sale.pk).update(
            voucher_code=voucher.code
        )
        sale.voucher_code = voucher.code

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
