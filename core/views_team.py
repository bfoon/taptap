"""Team accounts for a business, business switching, and daily sales."""
import secrets
from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import PAYMENT_METHODS, TrustedDevice
from .models_team import TeamMember, UsageDaily
from .permissions import PERMISSIONS, ROLES, STAFF_ROLES, landing_for
from .team import ACTIVE_BUSINESS_KEY, business_access_for_user, owned_business
from .utils import log


def _b(request):
    return request.user.business


def _is_owner(request):
    return request.tt_role == 'owner'


def _roles_you_can_assign(request):
    return [
        role
        for role in STAFF_ROLES
        if _is_owner(request) or role[0] != 'admin'
    ]


def _extras_you_can_grant(request):
    """Owners may add extras; admins only permissions they already hold."""
    blocked = {
        'subscription.manage',
        'plans.delete_used',
        'vouchers.warn',
        'vouchers.rollback',
    } | (set() if _is_owner(request) else {'team.manage', 'payroll.manage'})

    return [
        (key, value)
        for key, value in PERMISSIONS.items()
        if key not in blocked and key in request.tt_perms
    ]


def _can_touch(request, member):
    if member.user_id == request.user.pk:
        return False
    return _is_owner(request) or member.role != 'admin'


def _new_password():
    return secrets.token_urlsafe(9)


def _user_has_other_business_access(user, excluding_business_id):
    """True when changing this login would affect another TapTap business."""
    owned = owned_business(user)
    if owned and owned.pk != excluding_business_id:
        return True

    return (
        TeamMember.objects
        .filter(user=user, is_active=True)
        .exclude(business_id=excluding_business_id)
        .exists()
    )


@login_required
def team(request):
    business = _b(request)

    members = list(
        business.team
        .select_related('user', 'pay', 'pay__site')
        .all()
    )

    since = timezone.localdate() - timedelta(days=29)
    usage = {
        row['user']: row
        for row in (
            UsageDaily.objects
            .filter(business=business, date__gte=since)
            .values('user')
            .annotate(
                views=Sum('page_views'),
                logins=Sum('logins'),
                days=Count('id'),
            )
        )
    }

    for member in members:
        member.can_edit = _can_touch(request, member)
        member.usage = usage.get(member.user_id, {})
        member.shared_login = _user_has_other_business_access(
            member.user,
            business.pk,
        )

    roles = [
        (
            key,
            ROLES[key][0],
            ROLES[key][1],
            [PERMISSIONS[p] for p in sorted(ROLES[key][2])],
        )
        for key, _ in _roles_you_can_assign(request)
    ]

    # Staff pay is sensitive: only people with payroll.manage see or change it.
    payroll = None
    pay_preview = None
    if 'payroll.manage' in request.tt_perms:
        from .payroll import describe, payroll_month, preview_figures

        payroll = payroll_month(business, timezone.localdate())
        rows = {r['member'].pk: r for r in payroll['rows'] if r['member']}
        for member in members:
            member.pay_row = rows.get(member.pk)
            member.pay_terms = getattr(member, 'pay', None)
            member.pay_text = describe(member.pay_terms, business.currency) if member.pay_terms else ''
            # Same rules as paying: not your own pay, and only the owner touches an admin's pay.
            member.can_pay = (
                member.user_id != request.user.pk
                and (_is_owner(request) or member.role != 'admin')
            )
        pay_preview = preview_figures(business)

    return render(
        request,
        'core/team.html',
        {
            'members': members,
            'roles': roles,
            'extras': _extras_you_can_grant(request),
            'permission_labels': PERMISSIONS,
            'owner': business.user,
            'is_owner': _is_owner(request),
            'payroll': payroll,
            'pay_preview': pay_preview,
            'routers': business.routers.all().order_by('name') if payroll is not None else [],
            'pay_methods': [m for m in PAYMENT_METHODS if m[0] != 'auto'],
            'today': timezone.localdate().isoformat(),
        },
    )


@login_required
@require_POST
def team_member_save(request):
    business = _b(request)
    pk = request.POST.get('id')

    member = (
        get_object_or_404(
            business.team.select_related('user'),
            pk=pk,
        )
        if pk
        else None
    )

    if member and not _can_touch(request, member):
        messages.error(request, 'You cannot change this account.')
        return redirect('team')

    role = request.POST.get('role', 'viewer')
    if role not in dict(_roles_you_can_assign(request)):
        messages.error(request, 'You cannot give that role.')
        return redirect('team')

    grantable = {key for key, _ in _extras_you_can_grant(request)}
    extras = sorted(
        set(request.POST.getlist('extra'))
        & grantable
        - set(ROLES[role][2])
    )

    first = request.POST.get('first_name', '').strip()[:150]
    last = request.POST.get('last_name', '').strip()[:150]
    phone = request.POST.get('phone', '').strip()[:60]

    if member:
        member.role = role
        member.extra_permissions = extras
        member.phone = phone
        member.save(
            update_fields=['role', 'extra_permissions', 'phone']
        )

        # Name belongs to the login itself, so changing it also changes the
        # display name in every business using this same account.
        member.user.first_name = first or member.user.first_name
        member.user.last_name = last
        member.user.save(update_fields=['first_name', 'last_name'])

        log(
            business,
            'Team',
            f'{member.name} is now {member.role_label}',
        )
        messages.success(request, f'{member.name} updated.')
        return redirect('team')

    email = request.POST.get('email', '').strip().lower()

    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, 'Enter a valid email address.')
        return redirect('team')

    # The important multi-business change:
    # if this email already belongs to a TapTap user, reuse that login and add
    # a membership for this business instead of rejecting the email.
    user = (
        User.objects.filter(email__iexact=email).first()
        or User.objects.filter(username__iexact=email).first()
    )

    if user:
        if business.user_id == user.pk:
            messages.error(
                request,
                'That user is already the owner of this business.',
            )
            return redirect('team')

        if TeamMember.objects.filter(
            business=business,
            user=user,
        ).exists():
            messages.error(
                request,
                'That person already has access to this business.',
            )
            return redirect('team')

        with transaction.atomic():
            member = TeamMember.objects.create(
                business=business,
                user=user,
                role=role,
                extra_permissions=extras,
                phone=phone,
                created_by=request.user,
                must_change_password=False,
            )

            changed = []
            if first and not user.first_name:
                user.first_name = first
                changed.append('first_name')
            if last and not user.last_name:
                user.last_name = last
                changed.append('last_name')
            if changed:
                user.save(update_fields=changed)

        log(
            business,
            'Team',
            f'Added existing TapTap user {member.name} as {member.role_label}',
        )
        messages.success(
            request,
            f'{member.name} already has a TapTap login. '
            f'{business.business_name} is now available in their business '
            f'switcher as {member.role_label}.',
        )
        return redirect('team')

    password = request.POST.get('password', '').strip()
    generated = not password
    password = password or _new_password()

    try:
        validate_password(
            password,
            User(
                username=email,
                email=email,
                first_name=first,
            ),
        )
    except ValidationError as exc:
        messages.error(
            request,
            'Password: ' + ' '.join(exc.messages),
        )
        return redirect('team')

    with transaction.atomic():
        user = User.objects.create_user(
            username=email,
            email=email,
            password=password,
            first_name=first,
            last_name=last,
        )
        member = TeamMember.objects.create(
            business=business,
            user=user,
            role=role,
            extra_permissions=extras,
            phone=phone,
            created_by=request.user,
            must_change_password=generated,
        )

    log(
        business,
        'Team',
        f'Added {member.name} as {member.role_label}',
    )

    if generated:
        messages.success(
            request,
            f'{member.name} can now sign in with {email} and the temporary '
            f'password {password} — share it privately; it is shown only '
            'once. They will be asked to choose their own password when '
            'they first sign in.',
        )
    else:
        messages.success(
            request,
            f'{member.name} can now sign in with {email} and the password '
            'you set.',
        )

    return redirect('team')


@login_required
@require_POST
def team_member_action(request, pk):
    business = _b(request)
    member = get_object_or_404(
        business.team.select_related('user'),
        pk=pk,
    )

    if not _can_touch(request, member):
        messages.error(request, 'You cannot change this account.')
        return redirect('team')

    action = request.POST.get('action')

    if action == 'deactivate':
        member.is_active = False
        member.save(update_fields=['is_active'])

        # Do not revoke the user's trusted devices globally. The same login may
        # still be valid in another business.
        log(
            business,
            'Team',
            f'Switched off {member.name}',
        )
        messages.success(
            request,
            f'{member.name} no longer has access to '
            f'{business.business_name}.',
        )

    elif action == 'activate':
        member.is_active = True
        member.save(update_fields=['is_active'])

        log(
            business,
            'Team',
            f'Switched on {member.name}',
        )
        messages.success(
            request,
            f'{member.name} can access {business.business_name} again.',
        )

    elif action == 'reset_password':
        # A business must not reset a shared login and accidentally lock that
        # person out of another business.
        if _user_has_other_business_access(
            member.user,
            business.pk,
        ):
            messages.error(
                request,
                'This is a shared TapTap login used by another business. '
                'For security, the business cannot reset its global password. '
                'The user must change their own password from My password.',
            )
            return redirect('team')

        pw = _new_password()
        member.user.set_password(pw)
        member.user.save(update_fields=['password'])

        member.must_change_password = True
        member.save(update_fields=['must_change_password'])

        TrustedDevice.objects.filter(
            user=member.user,
            revoked_at__isnull=True,
        ).update(revoked_at=timezone.now())

        log(
            business,
            'Team',
            f'Reset the password for {member.name}',
        )
        messages.success(
            request,
            f'New temporary password for {member.name}: {pw} — share it '
            'privately; it is shown only once.',
        )

    elif action == 'delete':
        name = member.name

        # Delete only this business membership. Never delete the shared User.
        member.delete()

        log(
            business,
            'Team',
            f'Removed {name}',
        )
        messages.success(
            request,
            f'{name} was removed from {business.business_name}. '
            'Their TapTap login and access to other businesses were kept.',
        )

    return redirect('team')


@login_required
@require_POST
def business_switch(request, business_id):
    """Switch the current session to another authorised TapTap business."""
    business, member = business_access_for_user(
        request.user,
        business_id,
    )

    if business is None:
        messages.error(
            request,
            'You do not have access to that business.',
        )
        return redirect('dashboard')

    request.session[ACTIVE_BUSINESS_KEY] = business.pk
    request.session.modified = True

    if member is None:
        role = 'owner'
        role_label = 'Owner'
        perms = frozenset(PERMISSIONS)
    else:
        role = member.role
        role_label = member.role_label
        perms = member.permissions

    messages.success(
        request,
        f'You are now working in {business.business_name} as {role_label}.',
    )

    return redirect(landing_for(perms, role))


# ─────────────────────────── daily sales ───────────────────────────
@login_required
def sales_daily(request):
    business = _b(request)

    try:
        day = datetime.strptime(
            request.GET.get('date', ''),
            '%Y-%m-%d',
        ).date()
    except ValueError:
        day = timezone.localdate()

    day = min(day, timezone.localdate())

    tz = timezone.get_current_timezone()
    start = timezone.make_aware(
        datetime.combine(day, datetime.min.time()),
        tz,
    )
    end = start + timedelta(days=1)

    sales = business.sales.filter(
        sold_at__gte=start,
        sold_at__lt=end,
    )

    total = sales.aggregate(
        v=Sum('amount'),
        n=Count('id'),
        disc=Sum('discount'),
    )

    prev = business.sales.filter(
        sold_at__gte=start - timedelta(days=1),
        sold_at__lt=start,
    ).aggregate(
        v=Sum('amount'),
        n=Count('id'),
    )

    week = []
    for i in range(6, -1, -1):
        period_start = start - timedelta(days=i)
        agg = business.sales.filter(
            sold_at__gte=period_start,
            sold_at__lt=period_start + timedelta(days=1),
        ).aggregate(
            v=Sum('amount'),
            n=Count('id'),
        )
        week.append(
            {
                'day': period_start.date(),
                'amount': agg['v'] or 0,
                'count': agg['n'],
            }
        )

    peak = max(
        (row['amount'] for row in week),
        default=0,
    ) or 1

    for row in week:
        row['pct'] = round(
            float(row['amount']) / float(peak) * 100
        )

    change = None
    if prev['v']:
        change = round(
            (
                float(total['v'] or 0)
                - float(prev['v'])
            )
            / float(prev['v'])
            * 100,
            1,
        )

    return render(
        request,
        'core/sales_daily.html',
        {
            'day': day,
            'is_today': day == timezone.localdate(),
            'prev_day': day - timedelta(days=1),
            'next_day': (
                day + timedelta(days=1)
                if day < timezone.localdate()
                else None
            ),
            'total': total['v'] or 0,
            'count': total['n'],
            'discounts': total['disc'] or 0,
            'change': change,
            'avg': (
                (total['v'] or 0) / total['n']
                if total['n']
                else 0
            ),
            'by_plan': (
                sales.values('plan_name')
                .annotate(n=Count('id'), v=Sum('amount'))
                .order_by('-v')
            ),
            'by_method': (
                sales.values('payment_method')
                .annotate(n=Count('id'), v=Sum('amount'))
                .order_by('-v')
            ),
            'by_agent': (
                sales.values('agent__name')
                .annotate(n=Count('id'), v=Sum('amount'))
                .order_by('-v')
            ),
            'sales': sales.select_related('agent', 'router')[:200],
            'week': week,
        },
    )


# ─────────────────────────── own password ───────────────────────────
@login_required
def account_password(request):
    from django.contrib.auth import update_session_auth_hash
    from django.contrib.auth.forms import PasswordChangeForm

    form = PasswordChangeForm(
        request.user,
        request.POST or None,
    )

    for field in form.fields.values():
        field.widget.attrs['class'] = 'form-control'

    if request.method == 'POST' and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)

        TeamMember.objects.filter(
            user=user,
        ).update(
            must_change_password=False,
        )

        messages.success(
            request,
            'Your password was changed.',
        )
        return redirect('dashboard')

    forced = bool(
        getattr(request, 'tt_member', None)
        and request.tt_member.must_change_password
    )

    return render(
        request,
        'core/account_password.html',
        {
            'form': form,
            'forced': forced,
        },
    )
