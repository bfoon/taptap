"""Owner/admin member and plan editor for TapTap.

Full separate management pages keep the original Members/Plans screens intact.
No schema migrations. Role permissions are registered when this module is
imported from config.urls before requests are served.
"""
from decimal import Decimal, InvalidOperation
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from . import members as mem
from . import voucher_history as vh
from .durations import to_minutes
from .models import Voucher
from .permissions import URL_PERMS
from .utils import log
from .views_agents import push_one

# TeamAccessMiddleware is fail-closed for unmapped URL names. Register these
# specific new routes and enforce granular permissions in each view as well.
URL_PERMS.update({
    'catalog_manage': ('vouchers.view', 'vouchers.create', 'vouchers.support', 'plans.manage'),
    'catalog_member_edit': ('vouchers.create', 'vouchers.support'),
    'catalog_plan_edit': 'plans.manage',
})

SPEED = re.compile(r'^\d+[kKmM]?(/\d+[kKmM]?)?$')


def _business(request):
    return getattr(request, 'tt_business', None)


def _has(request, *permissions):
    return bool(set(permissions).intersection(getattr(request, 'tt_perms', frozenset())))


def _money(raw):
    try:
        value = Decimal(str(raw).replace(',', '').strip())
    except (InvalidOperation, ValueError):
        raise ValueError('Enter a valid price.')
    if value < 0 or value > Decimal('99999999.99'):
        raise ValueError('Price must be between zero and 99,999,999.99.')
    return value


def _integer(raw, label, lower, upper):
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f'{label} must be a whole number.')
    if not lower <= n <= upper:
        raise ValueError(f'{label} must be from {lower} to {upper}.')
    return n


@login_required
def catalog_manage(request):
    business = _business(request)
    if not business or not _has(request, 'vouchers.view', 'vouchers.create', 'vouchers.support', 'plans.manage'):
        return HttpResponseForbidden('You cannot access business management.')
    q = (request.GET.get('q') or '').strip()[:80]
    members = business.vouchers.filter(login_type='member').select_related('router').order_by('-created_at')
    plans = business.plans.select_related('imported_from_router').order_by('name')
    if q:
        members = members.filter(Q(code__icontains=q) | Q(customer_name__icontains=q) | Q(customer_phone__icontains=q))
        plans = plans.filter(Q(name__icontains=q) | Q(mikrotik_profile_name__icontains=q))
    return render(request, 'core/catalog_manage.html', {
        'members': members[:150], 'plans': plans[:150], 'q': q,
        'member_count': business.vouchers.filter(login_type='member').count(),
        'plan_count': business.plans.count(),
    })


@login_required
@require_http_methods(['GET', 'POST'])
def catalog_member_edit(request, pk):
    business = _business(request)
    if not business or not _has(request, 'vouchers.create', 'vouchers.support'):
        return HttpResponseForbidden('You do not have permission to edit members.')
    v = get_object_or_404(business.vouchers.select_related('router'), pk=pk, login_type='member')
    plans = business.plans.filter(active=True).order_by('name')
    routers = business.routers.order_by('name')
    agents = business.agents.filter(active=True).order_by('name')
    fresh = not (v.used_at or v.sold_at or v.expires_at)
    current_plan = business.plans.filter(name=v.plan_name).first()
    if request.method == 'POST':
        data = request.POST
        action = data.get('action', 'save')
        if action == 'change_password':
            if not _has(request, 'vouchers.support'):
                return HttpResponseForbidden('Password changes require voucher support permission.')
            try:
                ok, msg = mem.change_password(
                    v, data.get('password', ''), same=bool(data.get('same')),
                    user=request.user, reason=(data.get('reason') or 'Member edit')[:255],
                )
                (messages.success if ok else messages.warning)(request, msg)
            except mem.MemberError as exc:
                messages.error(request, str(exc))
            return redirect('catalog_member_edit', pk=v.pk)
        if action == 'rename':
            if not _has(request, 'vouchers.manage'):
                return HttpResponseForbidden('Renaming member usernames requires voucher management permission.')
            from .voucher_codes import change_code, CodeChangeError
            try:
                new_code = mem.clean_username(data.get('new_username'))
                if not mem.USERNAME_RE.fullmatch(new_code):
                    raise mem.MemberError('Username must be 3–32 allowed characters.')
                ok, msg = change_code(v, new_code, user=request.user,
                                      reason=(data.get('reason') or 'Member username updated')[:255])
                (messages.success if ok else messages.warning)(request, f'Username updated. {msg}')
            except (CodeChangeError, mem.MemberError) as exc:
                messages.error(request, str(exc))
            return redirect('catalog_member_edit', pk=v.pk)
        if action == 'save':
            try:
                dev = _integer(data.get('devices', v.max_devices), 'Devices', 1, 20)
                if dev < v.device_bindings.count():
                    raise ValueError('Reset existing device bindings before reducing the device limit.')
                rate = (data.get('rate_limit') or '').strip()
                if len(rate) > 50 or (rate and not SPEED.fullmatch(rate)):
                    raise ValueError('Speed must look like 5M/5M or 2M, or remain empty.')
                new_router = business.routers.filter(pk=data.get('router') or 0).first()
                # Moving a live/synced member without removing the old HotSpot
                # entry risks granting the same login on multiple routers.
                if v.router_id and (not new_router or new_router.pk != v.router_id):
                    raise ValueError('Changing an existing router assignment needs a controlled transfer. Keep the current router; contact support to move the live account.')
                if not fresh and data.get('plan') != (str(current_plan.pk) if current_plan else 'free'):
                    raise ValueError('An activated or sold member cannot have its existing plan replaced here. Use Renew for new time or create a separate member.')
                plan = None
                if data.get('plan') != 'free':
                    plan = business.plans.filter(pk=data.get('plan'), active=True).first() if str(data.get('plan')).isdigit() else None
                    if not plan:
                        raise ValueError('Choose an active plan or Free unlimited.')
                price = plan.price if plan else Decimal('0')
                minutes = plan.duration_minutes if plan else 0
                name = plan.name if plan else mem.FREE_PLAN_NAME
                if fresh:
                    v.plan_name = name
                    v.price = price
                    v.duration_minutes = minutes
                v.customer_name = (data.get('customer_name') or '').strip()[:120]
                v.customer_phone = (data.get('customer_phone') or '').strip()[:60]
                v.note = (data.get('note') or '').strip()[:255]
                v.max_devices = dev
                # For a native plan the plan owns the speed, otherwise a free
                # unlimited member may have an individual rate limit.
                v.rate_limit = rate if not plan else ''
                if not v.router_id and new_router:
                    v.router = new_router
                if data.get('agent'):
                    agent = business.agents.filter(pk=data.get('agent'), active=True).first()
                    if not agent:
                        raise ValueError('Choose a valid agent.')
                    if v.sold_at:
                        raise ValueError('A sold member’s assigned agent cannot be changed here; historical sales must stay accurate.')
                    v.agent = agent
                elif not v.sold_at:
                    v.agent = None
                v.mikrotik_sync_status = 'Pending'
                v.mikrotik_sync_error = ''
                with transaction.atomic():
                    v.save(update_fields=[
                        'customer_name','customer_phone','note','max_devices','rate_limit','plan_name',
                        'price','duration_minutes','router','agent','mikrotik_sync_status','mikrotik_sync_error',
                    ])
                log(business, 'Member Edited', f'{v.code}: customer, plan/device/router details updated by {request.user}')
                if v.router_id:
                    result = push_one(v, plan)
                    if result is True:
                        messages.success(request, f'{v.code} saved and synchronized to {v.router.name}.')
                    else:
                        messages.warning(request, f'{v.code} saved. Router update pending: {str(result)[:180]}')
                else:
                    messages.success(request, f'{v.code} saved in TapTap. Assign a router to activate its login.')
                return redirect('catalog_member_edit', pk=v.pk)
            except ValueError as exc:
                messages.error(request, str(exc))
        else:
            messages.error(request, 'Unknown edit action.')
    return render(request, 'core/catalog_member_edit.html', {
        'v': v, 'plans': plans, 'routers': routers, 'agents': agents,
        'fresh': fresh, 'current_plan': current_plan,
    })


@login_required
@require_http_methods(['GET', 'POST'])
def catalog_plan_edit(request, pk):
    business = _business(request)
    if not business or not _has(request, 'plans.manage'):
        return HttpResponseForbidden('You cannot edit plans.')
    p = get_object_or_404(business.plans, pk=pk)
    voucher_count = business.vouchers.filter(plan_name=p.name).count()
    if request.method == 'POST':
        d = request.POST
        try:
            name = (d.get('name') or '').strip()
            if not name or len(name) > 80:
                raise ValueError('Enter a plan name up to 80 characters.')
            if name.casefold() != p.name.casefold():
                if p.source == 'mikrotik':
                    raise ValueError('MikroTik-imported plan names must be renamed on the router first, then synchronized.')
                if voucher_count:
                    raise ValueError('A plan with existing vouchers cannot be renamed safely here. Create another plan instead to keep existing sales and voucher history intact.')
                if business.plans.filter(name__iexact=name).exclude(pk=p.pk).exists():
                    raise ValueError('A plan with that name already exists.')
            free = d.get('is_free') == 'on'
            price = Decimal('0') if free else _money(d.get('price', ''))
            unit = d.get('duration_unit', 'days')
            if unit not in {'minutes','hours','days','months','unlimited'}:
                raise ValueError('Choose a valid duration unit.')
            minutes = to_minutes(d.get('duration_value') or '1', unit)
            devices = _integer(d.get('max_devices'), 'Device limit', 1, 50)
            rate = (d.get('speed_limit') or '').strip()
            if len(rate) > 50 or (rate and not SPEED.fullmatch(rate)):
                raise ValueError('Speed must look like 5M/5M or 2M, or remain empty.')
            raw_data = (d.get('data_limit_mb') or '').strip()
            data_mb = _integer(raw_data, 'Data limit', 1, 100_000_000) if raw_data else None
            old_name, old_minutes = p.name, p.duration_minutes
            p.name = name
            p.price = price
            p.is_free = free
            p.price_source = 'manual'
            p.duration_minutes = minutes
            p.duration_unit = unit
            p.max_devices = devices
            p.speed_limit = rate
            p.data_limit_mb = data_mb
            p.active = d.get('active') == 'on'
            with transaction.atomic():
                p.save()
                eligible = business.vouchers.filter(
                    plan_name=old_name, used_at__isnull=True, expires_at__isnull=True,
                    frozen_at__isnull=True, sold_at__isnull=True,
                )
                updated = 0
                if d.get('apply_to_unused') == 'on':
                    ids = list(eligible.values_list('pk', flat=True))
                    updated = eligible.update(
                        duration_minutes=minutes, max_devices=devices, price=price,
                        mikrotik_sync_status='Pending', mikrotik_sync_error='',
                    )
                    try:   # just these vouchers, not a full router sync
                        from .voucher_push import push_vouchers
                        push_vouchers(list(business.vouchers.filter(pk__in=ids).select_related('router')), request.user)
                    except Exception:
                        pass
                elif old_minutes != minutes:
                    # Leave outstanding voucher duration unchanged unless asked.
                    pass
            log(business, 'Plan Edited', f'{old_name} -> {name} by {request.user}')
            from .portal_deploy import schedule_redeploy
            schedule_redeploy(business)
            msg = f'Plan {name} saved.'
            if updated:
                msg += f' {updated} unused and unsold vouchers updated and being sent to their routers.'
            if p.source == 'mikrotik':
                msg += ' Note: profile-controlled duration/devices/speed may be restored from MikroTik on the next sync.'
            messages.success(request, msg)
            return redirect('catalog_plan_edit', pk=p.pk)
        except ValueError as exc:
            messages.error(request, str(exc))
    return render(request, 'core/catalog_plan_edit.html', {
        'p': p, 'voucher_count': voucher_count,
        'eligible_count': business.vouchers.filter(
            plan_name=p.name, used_at__isnull=True, sold_at__isnull=True,
            expires_at__isnull=True, frozen_at__isnull=True,
        ).count(),
    })
