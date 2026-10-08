"""Network tools page: ping, traceroute, DNS, website, speed test and the one-click Internet check (core/nettools.py)."""
import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import nettools as nt
from .models_nettools import NetTest

logger = logging.getLogger('taptap.nettools')


def _b(request):
    return request.user.business


def as_json(t):
    return {
        'id': t.pk, 'kind': t.kind, 'kind_label': t.get_kind_display(), 'target': t.target, 'status': t.status,
        'via': t.via, 'result': t.result or {}, 'summary': nt.summary(t), 'router': t.router.name, 'router_id': t.router_id,
        'params': t.params, 'at': timezone.localtime(t.created_at).strftime('%d %b %H:%M'),
        'secs': round((t.finished_at - t.created_at).total_seconds(), 1) if t.finished_at else None,
    }


@login_required
def network_tools(request):
    from .voucher_history import channel
    b = _b(request)
    routers = list(b.routers.order_by('name'))
    sel = None
    want = request.GET.get('router')
    if want and str(want).isdigit():
        sel = next((r for r in routers if r.pk == int(want)), None)
    sel = sel or next((r for r in routers if r.status == 'Online'), None) or (routers[0] if routers else None)
    cards = [{'r': r, 'via': channel(r)} for r in routers]
    devices = []
    if sel:
        seen = set()
        for d in sel.devices.filter(is_online=True).exclude(ip_address='').order_by('-last_seen_at')[:200]:
            ip = (d.ip_address or '').split(',')[0].strip()
            if ip and ip not in seen and nt.is_ip(ip):
                seen.add(ip)
                devices.append({'ip': ip, 'name': d.hostname or d.mac_address})
            if len(devices) >= 40:
                break
    history = [as_json(t) for t in b.net_tests.select_related('router')[:40]]
    return render(request, 'core/network_tools.html', {
        'routers': cards, 'sel': sel, 'sel_via': channel(sel) if sel else '', 'devices': devices,
        'history': history, 'kinds': nt.KINDS,
    })


@login_required
@require_POST
def network_tools_run(request):
    from .voucher_history import channel
    b = _b(request)
    try:
        data = json.loads(request.body or b'{}') if request.content_type == 'application/json' else request.POST.dict()
    except ValueError:
        data = {}
    router = b.routers.filter(pk=str(data.get('router') or '0')).first() if str(data.get('router') or '').isdigit() else None
    if not router:
        return JsonResponse({'ok': False, 'message': 'Choose a router first.'}, status=400)
    kind = str(data.get('kind') or '')
    try:
        params = nt.clean_params(kind, data)
        nt.limit_check(b, router, kind)
    except ValueError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)}, status=400)
    via = channel(router)
    t = NetTest.objects.create(business=b, router=router, kind=kind, target=params.get('target', ''), params=params,
                               via=via, created_by=request.user, status='waiting' if via == 'TapTap Link' else 'running')
    if via == 'TapTap Link':
        from .linkops import send
        try:
            cmd = send(router, 'nettest', {'test_id': t.pk, 'kind': kind, **params},
                       label=f'{t.get_kind_display()} {params.get("target", "")}'.strip(), user=request.user, minutes=6)
            t.command_id = cmd.pk
            t.save(update_fields=['command_id'])
        except ValueError as exc:
            t.status, t.result, t.finished_at = 'failed', {'error': f'{router.name} cannot take commands right now: {exc}'}, timezone.now()
            t.save(update_fields=['status', 'result', 'finished_at'])
        return JsonResponse({'ok': True, 'test': as_json(t)})
    try:
        res = nt.run_api(t)
        t.status, t.result = 'done', res
    except Exception as exc:  # noqa: BLE001 - routeros_api raises many types
        logger.info('network test %s on router %s failed: %s', kind, router.pk, exc)
        t.status, t.result = 'failed', {'error': nt._ros_error(exc)}
    t.finished_at = timezone.now()
    t.save(update_fields=['status', 'result', 'finished_at'])
    return JsonResponse({'ok': True, 'test': as_json(t)})


@login_required
@require_GET
def network_tools_test(request, pk):
    t = _b(request).net_tests.select_related('router').filter(pk=pk).first()
    if not t:
        raise Http404
    nt.expire_waiting(t)
    return JsonResponse({'ok': True, 'test': as_json(t)})
