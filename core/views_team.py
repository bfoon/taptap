"""Team accounts for a business, and the daily sales page used by view-only staff."""
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

from .models import TrustedDevice
from .models_team import TeamMember, UsageDaily
from .permissions import PERMISSIONS, ROLES, STAFF_ROLES
from .utils import log


def _b(request):
    return request.user.business


def _is_owner(request):
    return request.tt_role == 'owner'


def _roles_you_can_assign(request):
    return [r for r in STAFF_ROLES if _is_owner(request) or r[0] != 'admin']


def _extras_you_can_grant(request):
    """Owners may add any extra permission except billing; admins only what they have, minus team management."""
    # Deleting plans with used vouchers stays with Owner and Admin: it is never handed out as an extra.
    blocked = {'subscription.manage', 'plans.delete_used', 'vouchers.warn'} | (set() if _is_owner(request) else {'team.manage'})
    return [(k, v) for k, v in PERMISSIONS.items() if k not in blocked and k in request.tt_perms]


def _can_touch(request, member):
    if member.user_id == request.user.pk:
        return False
    return _is_owner(request) or member.role != 'admin'


def _new_password():
    return secrets.token_urlsafe(9)


@login_required
def team(request):
    business = _b(request)
    members = list(business.team.select_related('user').all())
    since = timezone.localdate() - timedelta(days=29)
    usage = {r['user']: r for r in UsageDaily.objects.filter(business=business, date__gte=since)
             .values('user').annotate(views=Sum('page_views'), logins=Sum('logins'), days=Count('id'))}
    for m in members:
        m.can_edit = _can_touch(request, m)
        m.usage = usage.get(m.user_id, {})
    roles = [(k, ROLES[k][0], ROLES[k][1], [PERMISSIONS[p] for p in sorted(ROLES[k][2])]) for k, _ in _roles_you_can_assign(request)]
    return render(request, 'core/team.html', {
        'members': members, 'roles': roles, 'extras': _extras_you_can_grant(request), 'permission_labels': PERMISSIONS,
        'owner': business.user, 'is_owner': _is_owner(request),
    })


@login_required
@require_POST
def team_member_save(request):
    business = _b(request)
    pk = request.POST.get('id')
    member = get_object_or_404(business.team.select_related('user'), pk=pk) if pk else None
    if member and not _can_touch(request, member):
        messages.error(request, 'You cannot change this account.')
        return redirect('team')
    role = request.POST.get('role', 'viewer')
    if role not in dict(_roles_you_can_assign(request)):
        messages.error(request, 'You cannot give that role.')
        return redirect('team')
    grantable = {k for k, _ in _extras_you_can_grant(request)}
    extras = sorted(set(request.POST.getlist('extra')) & grantable - set(ROLES[role][2]))
    first = request.POST.get('first_name', '').strip()[:150]
    last = request.POST.get('last_name', '').strip()[:150]
    phone = request.POST.get('phone', '').strip()[:60]

    if member:
        member.role, member.extra_permissions, member.phone = role, extras, phone
        member.save(update_fields=['role', 'extra_permissions', 'phone'])
        member.user.first_name, member.user.last_name = first or member.user.first_name, last
        member.user.save(update_fields=['first_name', 'last_name'])
        log(business, 'Team', f'{member.name} is now {member.role_label}')
        messages.success(request, f'{member.name} updated.')
        return redirect('team')

    email = request.POST.get('email', '').strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, 'Enter a valid email address.')
        return redirect('team')
    if User.objects.filter(username__iexact=email).exists() or User.objects.filter(email__iexact=email).exists():
        messages.error(request, 'That email already has a TapTap login. Each team member needs their own email.')
        return redirect('team')
    password = request.POST.get('password', '').strip()
    generated = not password
    password = password or _new_password()
    try:
        validate_password(password, User(username=email, email=email, first_name=first))
    except ValidationError as e:
        messages.error(request, 'Password: ' + ' '.join(e.messages))
        return redirect('team')
    with transaction.atomic():
        user = User.objects.create_user(username=email, email=email, password=password, first_name=first, last_name=last)
        member = TeamMember.objects.create(business=business, user=user, role=role, extra_permissions=extras, phone=phone,
                                           created_by=request.user, must_change_password=generated)
    log(business, 'Team', f'Added {member.name} as {member.role_label}')
    if generated:
        messages.success(request, f'{member.name} can now sign in with {email} and the temporary password {password} '
                                  '— share it privately; it is shown only once. They will be asked to choose their own password when they first sign in.')
    else:
        messages.success(request, f'{member.name} can now sign in with {email} and the password you set.')
    return redirect('team')


@login_required
@require_POST
def team_member_action(request, pk):
    business = _b(request)
    member = get_object_or_404(business.team.select_related('user'), pk=pk)
    if not _can_touch(request, member):
        messages.error(request, 'You cannot change this account.')
        return redirect('team')
    action = request.POST.get('action')
    if action == 'deactivate':
        member.is_active = False
        member.save(update_fields=['is_active'])
        TrustedDevice.objects.filter(user=member.user, revoked_at__isnull=True).update(revoked_at=timezone.now())
        log(business, 'Team', f'Switched off {member.name}')
        messages.success(request, f'{member.name} is switched off and signed out everywhere.')
    elif action == 'activate':
        member.is_active = True
        member.save(update_fields=['is_active'])
        log(business, 'Team', f'Switched on {member.name}')
        messages.success(request, f'{member.name} can sign in again.')
    elif action == 'reset_password':
        pw = _new_password()
        member.user.set_password(pw)
        member.user.save(update_fields=['password'])
        member.must_change_password = True
        member.save(update_fields=['must_change_password'])
        TrustedDevice.objects.filter(user=member.user, revoked_at__isnull=True).update(revoked_at=timezone.now())
        log(business, 'Team', f'Reset the password for {member.name}')
        messages.success(request, f'New temporary password for {member.name}: {pw} — share it privately; it is shown only once.')
    elif action == 'delete':
        name = member.name
        member.user.delete()  # cascades to the membership; their past sales/records keep working (SET_NULL)
        log(business, 'Team', f'Removed {name}')
        messages.success(request, f'{name} was removed.')
    return redirect('team')


# ─────────────────────────── daily sales ───────────────────────────
@login_required
def sales_daily(request):
    business = _b(request)
    try:
        day = datetime.strptime(request.GET.get('date', ''), '%Y-%m-%d').date()
    except ValueError:
        day = timezone.localdate()
    day = min(day, timezone.localdate())
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(day, datetime.min.time()), tz)
    end = start + timedelta(days=1)
    sales = business.sales.filter(sold_at__gte=start, sold_at__lt=end)
    total = sales.aggregate(v=Sum('amount'), n=Count('id'), disc=Sum('discount'))
    prev = business.sales.filter(sold_at__gte=start - timedelta(days=1), sold_at__lt=start).aggregate(v=Sum('amount'), n=Count('id'))
    week = []
    for i in range(6, -1, -1):
        s = start - timedelta(days=i)
        agg = business.sales.filter(sold_at__gte=s, sold_at__lt=s + timedelta(days=1)).aggregate(v=Sum('amount'), n=Count('id'))
        week.append({'day': s.date(), 'amount': agg['v'] or 0, 'count': agg['n']})
    peak = max((w['amount'] for w in week), default=0) or 1
    for w in week:
        w['pct'] = round(float(w['amount']) / float(peak) * 100)
    change = None
    if prev['v']:
        change = round((float(total['v'] or 0) - float(prev['v'])) / float(prev['v']) * 100, 1)
    return render(request, 'core/sales_daily.html', {
        'day': day, 'is_today': day == timezone.localdate(),
        'prev_day': day - timedelta(days=1), 'next_day': day + timedelta(days=1) if day < timezone.localdate() else None,
        'total': total['v'] or 0, 'count': total['n'], 'discounts': total['disc'] or 0, 'change': change,
        'avg': (total['v'] or 0) / total['n'] if total['n'] else 0,
        'by_plan': sales.values('plan_name').annotate(n=Count('id'), v=Sum('amount')).order_by('-v'),
        'by_method': sales.values('payment_method').annotate(n=Count('id'), v=Sum('amount')).order_by('-v'),
        'by_agent': sales.values('agent__name').annotate(n=Count('id'), v=Sum('amount')).order_by('-v'),
        'sales': sales.select_related('agent', 'router')[:200], 'week': week,
    })


# ─────────────────────────── own password ───────────────────────────
@login_required
def account_password(request):
    from django.contrib.auth import update_session_auth_hash
    from django.contrib.auth.forms import PasswordChangeForm
    form = PasswordChangeForm(request.user, request.POST or None)
    for f in form.fields.values():
        f.widget.attrs['class'] = 'form-control'
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        TeamMember.objects.filter(user=user).update(must_change_password=False)
        messages.success(request, 'Your password was changed.')
        return redirect('dashboard')
    forced = bool(getattr(request, 'tt_member', None) and request.tt_member.must_change_password)
    return render(request, 'core/account_password.html', {'form': form, 'forced': forced})
