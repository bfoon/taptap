from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from .models import RouterDevice
from .models_apps import TrafficSpeedRule
from .traffic_speed import apply_business


def _business(request):
    return getattr(request, 'tt_business', None) or request.user.business


def _positive_decimal(value, label):
    try:
        number = Decimal(str(value or '').strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f'Enter a valid {label} speed.')

    if number <= 0:
        raise ValueError(f'{label.capitalize()} speed must be greater than zero.')

    if number > 100000:
        raise ValueError(f'{label.capitalize()} speed is too large.')

    return number.quantize(Decimal('0.01'))


def _auto_name(scope, plans=None, agents=None, devices=None):
    plans = plans or []
    agents = agents or []
    devices = devices or []

    if scope == 'all_plans':
        return 'All plans'

    if scope == 'plans':
        names = [p.name for p in plans]
        return 'Plans: ' + ', '.join(names[:3]) + (f' +{len(names)-3}' if len(names) > 3 else '')

    if scope == 'agents':
        names = [a.name for a in agents]
        return 'Agents: ' + ', '.join(names[:3]) + (f' +{len(names)-3}' if len(names) > 3 else '')

    names = [
        d.get('name') or d.get('mac') or d.get('ip') or 'Device'
        for d in devices
    ]
    return 'Devices: ' + ', '.join(names[:3]) + (f' +{len(names)-3}' if len(names) > 3 else '')


@login_required
@require_POST
def speed_rule_save(request):
    business = _business(request)
    post = request.POST
    scope = post.get('scope', '').strip()

    if scope not in {'all_plans', 'plans', 'agents', 'devices'}:
        messages.error(request, 'Choose what this speed limit applies to.')
        return redirect('traffic')

    try:
        down = _positive_decimal(post.get('down_mbps'), 'download')
        up = _positive_decimal(post.get('up_mbps'), 'upload')
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect('traffic')

    rule_id = (post.get('id') or '').strip()

    if rule_id.isdigit():
        rule = get_object_or_404(
            TrafficSpeedRule,
            pk=int(rule_id),
            business=business,
        )
    else:
        rule = TrafficSpeedRule(
            business=business,
            created_by=request.user,
        )

    selected_plans = []
    selected_agents = []
    selected_devices = []

    if scope == 'plans':
        ids = [x for x in post.getlist('plan_ids') if x.isdigit()]
        selected_plans = list(
            business.plans
            .filter(pk__in=ids, active=True)
            .exclude(name__startswith='*')
            .order_by('name')
        )

        if not selected_plans:
            messages.error(request, 'Select at least one plan.')
            return redirect('traffic')

    elif scope == 'agents':
        ids = [x for x in post.getlist('agent_ids') if x.isdigit()]
        selected_agents = list(
            business.agents
            .filter(pk__in=ids, active=True)
            .order_by('name')
        )

        if not selected_agents:
            messages.error(request, 'Select at least one agent.')
            return redirect('traffic')

    elif scope == 'devices':
        ids = [int(x) for x in post.getlist('device_ids') if x.isdigit()]
        devices = list(
            RouterDevice.objects
            .filter(
                pk__in=ids,
                router__business=business,
            )
            .select_related('router')
            .order_by('router__name', 'hostname', 'mac_address')
        )

        for device in devices:
            selected_devices.append({
                'router': device.router_id,
                'mac': (device.mac_address or '').upper(),
                'ip': device.ip_address or '',
                'name': (
                    device.hostname
                    or device.mac_address
                    or device.ip_address
                    or f'Device {device.pk}'
                ),
            })

        if not selected_devices:
            messages.error(request, 'Select at least one device.')
            return redirect('traffic')

    rule.scope = scope
    rule.down_mbps = down
    rule.up_mbps = up
    rule.enabled = True
    rule.devices = selected_devices if scope == 'devices' else []
    rule.name = (
        post.get('name', '').strip()[:160]
        or _auto_name(
            scope,
            selected_plans,
            selected_agents,
            selected_devices,
        )[:160]
    )
    rule.save()

    rule.plans.set(selected_plans if scope == 'plans' else [])
    rule.agents.set(selected_agents if scope == 'agents' else [])

    results = apply_business(business, force=True)
    failed = [router.name for router, ok, _result in results if not ok]

    messages.success(
        request,
        f'{rule.name}: {down:g} Mb/s download / {up:g} Mb/s upload saved.',
    )

    if failed:
        messages.warning(
            request,
            'Saved in TapTap, but these routers could not be updated immediately: '
            + ', '.join(failed[:5])
            + '. Live sync will retry.',
        )

    return redirect('traffic')


@login_required
@require_POST
def speed_rule_action(request, pk):
    business = _business(request)
    rule = get_object_or_404(
        TrafficSpeedRule,
        pk=pk,
        business=business,
    )
    action = request.POST.get('action', '')

    if action == 'delete':
        label = rule.name
        rule.delete()
        messages.success(request, f'{label} speed rule removed.')

    elif action == 'toggle':
        rule.enabled = not rule.enabled
        rule.save(update_fields=['enabled', 'updated_at'])

        messages.success(
            request,
            f'{rule.name} is now {"active" if rule.enabled else "off"}.',
        )

    else:
        messages.error(request, 'Unknown speed-rule action.')
        return redirect('traffic')

    results = apply_business(business, force=True)
    failed = [router.name for router, ok, _result in results if not ok]

    if failed:
        messages.warning(
            request,
            'The rule was saved, but these routers could not be refreshed immediately: '
            + ', '.join(failed[:5])
            + '. Live sync will retry.',
        )

    return redirect('traffic')
