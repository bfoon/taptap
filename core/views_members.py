"""Members and reusable Member Plans."""
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import members as mem
from . import voucher_history as vh
from .durations import split
from .models_member_plans import MemberRenewal
from .utils import log
from .views_agents import MANUAL_METHODS, push_one


def _b(request):
    return request.user.business


def _safe_next(value, fallback='/members/'):
    value = str(value or '')
    return value if value.startswith('/') and not value.startswith('//') else fallback


def _back(request, fallback='members'):
    nxt = request.POST.get('next', '')
    return redirect(
        nxt
        if nxt.startswith('/') and not nxt.startswith('//')
        else fallback
    )


def _can(request, permission):
    perms = getattr(request, 'tt_perms', None)
    return perms is None or permission in perms


def _plan_rows(business):
    rows = []
    plans = (
        business.member_plans
        .annotate(assigned_count=Count('assignments'))
        .order_by('-active', 'price', 'name')
    )
    for plan in plans:
        value, unit = split(
            plan.duration_minutes,
            plan.duration_unit,
        )
        rows.append({
            'plan': plan,
            'duration_value': value,
            'duration_unit': unit,
            'duration_text': plan.duration_text,
            'assigned_count': plan.assigned_count,
        })
    return rows


@login_required
def members(request):
    business = _b(request)

    # Printable renewal receipt without adding another URL/permission surface.
    # Example: /members/?receipt=15
    receipt_id = str(request.GET.get('receipt') or '')
    if request.method == 'GET' and receipt_id.isdigit():
        renewal = get_object_or_404(
            MemberRenewal.objects.select_related(
                'business',
                'member',
                'plan',
                'sale',
                'recorded_by',
            ),
            business=business,
            pk=int(receipt_id),
        )
        return render(
            request,
            'core/member_renewal_receipt.html',
            {
                'renewal': renewal,
                'next_url': _safe_next(
                    request.GET.get('next'),
                    reverse('members'),
                ),
                'autoprint': request.GET.get('autoprint') == '1',
                'fmt': (
                    'thermal'
                    if request.GET.get('format') == 'thermal'
                    else 'a4'
                ),
            },
        )

    active_plans = business.member_plans.filter(
        active=True
    ).order_by('price', 'name')

    if request.method == 'POST':
        f = request.POST
        action = f.get('action') or 'create_member'

        # ── Member Plan catalogue ──────────────────────────────────────
        if action == 'save_plan':
            if not _can(request, 'plans.manage'):
                messages.error(
                    request,
                    'Your role cannot create or edit Member Plans.',
                )
                return redirect('members')

            plan = None
            plan_id = str(f.get('plan_id') or '')
            if plan_id.isdigit():
                plan = business.member_plans.filter(
                    pk=int(plan_id)
                ).first()
                if plan is None:
                    messages.error(
                        request,
                        'That Member Plan no longer exists.',
                    )
                    return redirect('members')

            try:
                plan, refreshed = mem.save_member_plan(
                    business,
                    f,
                    plan=plan,
                    user=request.user,
                )
            except mem.MemberError as exc:
                messages.error(request, str(exc))
                ctx = _ctx(
                    request,
                    business,
                    active_plans,
                    plan_form=f,
                )
                return render(
                    request,
                    'core/members.html',
                    ctx,
                    status=400,
                )

            verb = 'updated' if plan_id else 'created'
            msg = (
                f'Member Plan “{plan.name}” {verb}: '
                f'{business.currency}{plan.price:,.2f}, '
                f'{plan.max_devices} device'
                f'{"s" if plan.max_devices != 1 else ""}, '
                f'{plan.speed_limit or "full speed"}, '
                f'{plan.duration_text}.'
            )
            if refreshed:
                msg += (
                    f' {refreshed} assigned member'
                    f'{"s" if refreshed != 1 else ""} refreshed from the plan '
                    'and marked for router sync.'
                )
            messages.success(request, msg)
            log(
                business,
                'Member Plan',
                f'{plan.name} {verb}',
            )
            return redirect('/members/#member-plans')

        if action == 'toggle_plan':
            if not _can(request, 'plans.manage'):
                messages.error(
                    request,
                    'Your role cannot change Member Plans.',
                )
                return redirect('members')

            plan = get_object_or_404(
                business.member_plans,
                pk=f.get('plan_id') or 0,
            )
            active = not plan.active
            mem.set_plan_active(plan, active)
            messages.success(
                request,
                f'{plan.name} is now '
                f'{"available for new members" if active else "inactive for new members"}. '
                'Existing members stay on the plan.',
            )
            log(
                business,
                'Member Plan',
                f'{plan.name}: {"enabled" if active else "disabled"}',
            )
            return redirect('/members/#member-plans')

        if action == 'assign_plan':
            if not (
                _can(request, 'vouchers.create')
                or _can(request, 'plans.manage')
            ):
                messages.error(
                    request,
                    'Your role cannot assign Member Plans.',
                )
                return redirect('members')

            member = get_object_or_404(
                business.vouchers.filter(login_type='member'),
                pk=f.get('member_id') or 0,
            )
            try:
                plan = mem.plan_choice(
                    business,
                    f.get('plan'),
                    active_only=True,
                )
                mem.assign_member_plan(
                    member,
                    plan,
                    user=request.user,
                )
            except mem.MemberError as exc:
                messages.error(request, str(exc))
                return _back(request)

            result = None
            if member.router:
                result = push_one(member, plan)

            text = (
                f'{member.code} is now on Member Plan “{plan.name}”. '
                f'Devices: {plan.max_devices}; '
                f'speed: {plan.speed_limit or "full speed"}; '
                f'price: {business.currency}{plan.price:,.2f}.'
            )
            if member.router:
                text += (
                    ' Router updated.'
                    if result is True
                    else f' Router update is pending ({result}).'
                )
            messages.success(request, text)
            log(
                business,
                'Member Plan Assigned',
                f'{member.code} → {plan.name}',
            )
            return _back(request)

        # ── Member email + reminders ───────────────────────────────────
        if action == 'save_notifications':
            if not (
                _can(request, 'vouchers.create')
                or _can(request, 'vouchers.support')
            ):
                messages.error(
                    request,
                    'Your role cannot change member contact settings.',
                )
                return redirect('members')

            member = get_object_or_404(
                business.vouchers.filter(login_type='member'),
                pk=f.get('member_id') or 0,
            )
            try:
                pref = mem.save_member_notifications(
                    member,
                    f,
                    user=request.user,
                )
            except mem.MemberError as exc:
                messages.error(request, str(exc))
                return _back(request)

            if pref.email:
                if pref.reminders_enabled:
                    selected = []
                    if pref.remind_7_days:
                        selected.append('7 days')
                    if pref.remind_2_days:
                        selected.append('2 days')
                    if pref.remind_1_day:
                        selected.append('1 day')
                    messages.success(
                        request,
                        f'{member.code}: email saved as {pref.email}; '
                        f'expiry reminders enabled at {", ".join(selected)}.',
                    )
                else:
                    messages.success(
                        request,
                        f'{member.code}: email saved as {pref.email}. '
                        'Renewal receipts will be emailed; expiry reminders are off.',
                    )
            else:
                messages.success(
                    request,
                    f'{member.code}: member email cleared and expiry reminders are off.',
                )
            log(
                business,
                'Member Notifications',
                f'{member.code}: {pref.email or "no email"}; reminders '
                f'{"on" if pref.reminders_enabled else "off"}',
            )
            return _back(request)

        # ── New member ────────────────────────────────────────────────
        if not _can(request, 'vouchers.create'):
            messages.error(
                request,
                'Your role cannot create members.',
            )
            return redirect('members')

        router = business.routers.filter(
            pk=f.get('router') or 0
        ).first()
        agent = business.agents.filter(
            pk=f.get('agent') or 0
        ).first()
        method = (
            f.get('method')
            if f.get('method') in dict(MANUAL_METHODS)
            else 'cash'
        )

        try:
            v = mem.create_member(
                business,
                username=f.get('username'),
                password=f.get('password', ''),
                same=bool(f.get('same')),
                plan_value=f.get('plan'),
                router=router,
                agent=agent,
                customer_name=f.get('customer_name'),
                customer_phone=f.get('customer_phone'),
                customer_email=f.get('customer_email'),
                reminders_enabled=bool(f.get('reminders_enabled')),
                remind_7_days=bool(f.get('remind_7_days')),
                remind_2_days=bool(f.get('remind_2_days')),
                remind_1_day=bool(f.get('remind_1_day')),
                note=f.get('note'),
                paid=bool(f.get('paid')),
                method=method,
                reference=f.get('reference'),
                user=request.user,
            )
        except mem.MemberError as exc:
            messages.error(request, str(exc))
            return render(
                request,
                'core/members.html',
                _ctx(
                    request,
                    business,
                    active_plans,
                    form=f,
                ),
                status=400,
            )

        plan = mem.plan_for_member(v)
        log(
            business,
            'Member Created',
            f'{v.code} ({plan.name if plan else v.plan_name})'
            + (
                f' for {v.customer_name}'
                if v.customer_name
                else ''
            ),
        )

        if router:
            pushed = push_one(v, plan)
            if pushed is True:
                messages.success(
                    request,
                    f'Member {v.code} is live on {router.name} on '
                    f'“{plan.name}” — they can log in now.',
                )
            else:
                messages.warning(
                    request,
                    f'Member {v.code} saved on “{plan.name}”, but '
                    f'{router.name} could not be updated right now '
                    f'({pushed}). TapTap sends it at the next router sync.',
                )
        else:
            messages.success(
                request,
                f'Member {v.code} created on “{plan.name}”. '
                'Choose a router so they can log in.',
            )

        return redirect(
            f'{request.path}?created={v.pk}'
        )

    return render(
        request,
        'core/members.html',
        _ctx(request, business, active_plans),
    )


def _ctx(
    request,
    business,
    plans,
    form=None,
    plan_form=None,
):
    q = (request.GET.get('q') or '').strip()
    kind = request.GET.get('kind', '')

    qs = (
        business.vouchers
        .filter(login_type='member')
        .select_related(
            'router',
            'agent',
            'member_plan_assignment__plan',
            'member_notification_settings',
        )
        .order_by('-created_at')
    )

    if q:
        qs = qs.filter(
            Q(code__icontains=q)
            | Q(customer_name__icontains=q)
            | Q(customer_phone__icontains=q)
            | Q(plan_name__icontains=q)
            | Q(member_notification_settings__email__icontains=q)
        )

    if kind == 'free':
        qs = qs.filter(price=0)
    elif kind == 'paid':
        qs = qs.filter(price__gt=0)
    elif kind == 'unassigned':
        qs = qs.filter(
            member_plan_assignment__isnull=True
        )

    page = Paginator(qs, 50).get_page(
        request.GET.get('p')
    )
    now = timezone.now()
    on_router = router_view(business, list(page))
    rows = []

    for v in page:
        key, label = vh.display_state(v, now)
        label = {
            'stock': 'Not logged in yet',
            'sold': 'Paid, not used yet',
        }.get(key, label)
        end = vh.ends_at(v)
        plan = mem.plan_for_member(v)
        pref = mem.notification_settings_for(v)

        rows.append({
            'v': v,
            'plan': plan,
            'notify': pref,
            'needs_plan': plan is None,
            'key': key,
            'label': label,
            'ends_at': end,
            'kind': mem.kind_of(v),
            'left': (
                (end - now)
                if end and end > now
                else None
            ),
            'router_view': on_router.get(v.pk),
        })

    created = (
        business.vouchers
        .filter(
            login_type='member',
            pk=request.GET.get('created') or 0,
        )
        .first()
        if str(
            request.GET.get('created') or ''
        ).isdigit()
        else None
    )

    plans = list(plans)
    default_plan = str(plans[0].pk) if plans else ''

    default_form = {
        'plan': default_plan,
        'same': '',
        'remind_7_days': '1',
        'remind_2_days': '1',
        'remind_1_day': '1',
    }

    return {
        'plans': plans,
        'plan_rows': _plan_rows(business),
        'routers': business.routers.all(),
        'agents': business.agents.filter(active=True),
        'methods': MANUAL_METHODS,
        'rows': rows,
        'page': page,
        'q': q,
        'kind': kind,
        'stats': mem.member_stats(business),
        'form': form or default_form,
        'plan_form': plan_form or {},
        'created': created,
        'created_plan': (
            mem.plan_for_member(created)
            if created
            else None
        ),
        'created_notify': (
            mem.notification_settings_for(created)
            if created
            else None
        ),
        'default_router': business.routers.first(),
    }


def router_view(business, vouchers):
    """What each member's router reports from TapTap's latest mirrored state."""
    from .durations import parse_routeros
    from .models import (
        RouterHotspotProfile,
        RouterHotspotUser,
    )
    from .sync import parse_mikhmon
    from .durations import text as mtext

    vs = [v for v in vouchers if v.router_id]
    if not vs:
        return {}

    users = {
        (u.router_id, u.username.lower()): u
        for u in RouterHotspotUser.objects.filter(
            router_id__in={
                v.router_id
                for v in vs
            },
            username__in=[
                v.code
                for v in vs
            ],
            is_present=True,
        )
    }
    profs = {
        (p.router_id, p.name): p
        for p in RouterHotspotProfile.objects.filter(
            router_id__in={
                v.router_id
                for v in vs
            },
            is_present=True,
        )
    }

    out = {}
    for v in vs:
        u = users.get(
            (v.router_id, v.code.lower())
        )
        if not u:
            out[v.pk] = {
                'text': 'Not seen on the router yet',
                'mismatch': False,
                'missing': True,
            }
            continue

        limits = []
        lim = parse_routeros(
            u.limit_uptime,
            0,
        ) or 0
        if lim:
            limits.append(
                f'{mtext(lim)} (user limit)'
            )

        pr = profs.get(
            (v.router_id, u.profile)
        )
        if pr:
            st = parse_routeros(
                pr.session_timeout,
                0,
            ) or 0
            if st:
                limits.append(
                    f'{mtext(st)} per session '
                    f'(profile {pr.name})'
                )
            _, validity = parse_mikhmon(
                (pr.raw_data or {}).get(
                    'on-login',
                    '',
                )
            )
            if validity:
                limits.append(
                    f'{mtext(validity)} validity '
                    f'(Mikhmon script on {pr.name})'
                )

        unlimited = (
            not v.duration_minutes
            and not v.expires_at
        )
        plan = mem.plan_for_member(v)
        expected_profile = (
            plan.router_profile_name
            if plan
            else v.router_profile
        )
        profile_mismatch = bool(
            expected_profile
            and u.profile
            and u.profile != expected_profile
        )

        out[v.pk] = {
            'text': (
                ', '.join(limits)
                if limits
                else 'No time limit'
            ),
            'profile': u.profile,
            'expected_profile': expected_profile,
            'mismatch': (
                bool(limits)
                if unlimited
                else False
            ) or profile_mismatch,
            'profile_mismatch': profile_mismatch,
            'missing': False,
        }

    return out


@login_required
@require_POST
def member_push(request, pk):
    """Send one member to its router again using its assigned MemberPlan."""
    business = _b(request)
    v = get_object_or_404(
        business.vouchers.filter(
            login_type='member'
        ),
        pk=pk,
    )

    if not v.router:
        messages.error(
            request,
            f'{v.code} has no router. Choose one first.',
        )
        return _back(request)

    plan = mem.plan_for_member(v)
    if plan is None:
        messages.warning(
            request,
            f'{v.code} has no Member Plan yet. '
            'Assign a plan first; the current legacy settings were sent as-is.',
        )

    res = push_one(v, plan)
    if res is True:
        messages.success(
            request,
            f'{v.code} sent to {v.router.name} again'
            + (
                f' using {plan.name}.'
                if plan
                else '.'
            ),
        )
    else:
        messages.warning(
            request,
            f'{v.router.name} could not be updated right now '
            f'({res}). TapTap retries at the next sync.',
        )

    log(
        business,
        'Member Updated',
        f'{v.code} sent to {v.router.name} again',
    )
    return _back(request)


@login_required
@require_POST
def member_password(request, pk):
    business = _b(request)
    v = get_object_or_404(
        business.vouchers.filter(
            login_type='member'
        ),
        pk=pk,
    )
    try:
        ok, result = mem.change_password(
            v,
            request.POST.get('password', ''),
            same=bool(request.POST.get('same')),
            user=request.user,
            reason=request.POST.get(
                'reason',
                '',
            ),
        )
    except mem.MemberError as exc:
        messages.error(request, str(exc))
        return _back(request)

    (
        messages.success
        if ok
        else messages.warning
    )(
        request,
        f'Password of {v.code} changed. {result}.',
    )
    return _back(request)


@login_required
@require_POST
def member_renew(request, pk):
    business = _b(request)
    v = get_object_or_404(
        business.vouchers.filter(
            login_type='member'
        ),
        pk=pk,
    )

    method = (
        request.POST.get('method')
        if request.POST.get('method')
        in dict(MANUAL_METHODS)
        else 'cash'
    )
    agent = business.agents.filter(
        pk=request.POST.get('agent') or 0
    ).first()

    try:
        ok, result, sale, minutes, renewal = mem.renew_with_receipt(
            v,
            amount=request.POST.get('amount'),
            method=method,
            reference=request.POST.get(
                'reference',
                '',
            ),
            agent=agent,
            user=request.user,
            require_amount=True,
        )
    except (
        mem.MemberError,
        vh.VoucherActionError,
    ) as exc:
        messages.error(request, str(exc))
        return _back(request)

    from .durations import text
    from .member_notifications import send_renewal_receipt

    email_message = ''
    if renewal.email_to:
        sent, email_message = send_renewal_receipt(renewal)
        renewal.refresh_from_db()
        if not sent:
            messages.warning(
                request,
                f'The renewal was successful, but {email_message}',
            )

    paid = (
        f' {business.currency}{sale.amount:,.2f} was posted to Finance.'
        if sale
        else ' No payment was required for this plan.'
    )
    if renewal.email_to and renewal.email_sent_at:
        paid += f' Receipt emailed to {renewal.email_to}.'

    (
        messages.success
        if ok
        else messages.warning
    )(
        request,
        f'{v.code} renewed: +{text(minutes)}. '
        f'Amount collected: {business.currency}'
        f'{renewal.amount_collected:,.2f}.{paid} {result}.',
    )
    log(
        business,
        'Member Renewed',
        f'{v.code}: {renewal.receipt_number} · '
        f'{business.currency}{renewal.amount_collected:,.2f}',
    )

    nxt = _safe_next(
        request.POST.get('next'),
        reverse('members'),
    )
    query = urlencode({
        'receipt': renewal.pk,
        'next': nxt,
    })
    return redirect(f'{reverse("members")}?{query}')
