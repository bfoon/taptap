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
        'params': t.params, 'at': timezone.localtime(t.created_at).strftime('%d %b %H:%M'), 'ts': int(t.created_at.timestamp()),
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
    if kind == 'whoami':                      # TapTap's own addresses come from the server, never the browser
        from .pathtrace import taptap_ips
        data['host'], data['ips'] = taptap_ips()
    try:
        params = nt.clean_params(kind, data)
        nt.limit_check(b, router, kind)
    except ValueError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)}, status=400)
    via = channel(router)
    stored = dict(params)
    if kind == 'whoami':
        hint = str(data.get('lip') or '').strip()
        stored['hint'] = hint if nt.is_ip(hint) else ''
    t = NetTest.objects.create(business=b, router=router, kind=kind, target=params.get('target', ''), params=stored,
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
        if kind == 'whoami':
            from .pathtrace import enrich_whoami
            res = enrich_whoami(router, res, stored.get('hint', ''))
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


# ─────────────────────────────── My connection (core/pathtrace.py) ───────────────────────────────

def _router(request, value):
    v = str(value or '')
    return _b(request).routers.filter(pk=v).first() if v.isdigit() else None


@login_required
@require_GET
def network_tools_path(request):
    """The way from a device to the MikroTik: every box with its IP, MAC and maker, nearest the MikroTik first."""
    from . import pathtrace
    router = _router(request, request.GET.get('router'))
    if not router:
        raise Http404
    ip = request.GET.get('ip', '').strip()
    mac = request.GET.get('mac', '').strip()
    try:
        ip = nt.clean_host(ip) if ip else ''
        mac = pathtrace.clean_mac(mac) if mac else ''
    except ValueError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)}, status=400)
    if ip and not nt.is_ip(ip):
        return JsonResponse({'ok': False, 'message': 'Give the device’s IP address.'}, status=400)
    path = pathtrace.build_path(router, ip, mac)
    path['measure'] = pathtrace.measured_ips(path)
    return JsonResponse({'ok': True, 'path': path})


@login_required
@require_GET
def network_tools_devices(request):
    """Online devices on a router, to trace a customer's device (or your own when TapTap cannot spot it)."""
    router = _router(request, request.GET.get('router'))
    if not router:
        raise Http404
    q = request.GET.get('q', '').strip().lower()[:40]
    out, seen = [], set()
    for d in router.devices.filter(is_online=True).exclude(ip_address='').order_by('-last_seen_at')[:600]:
        ip = (d.ip_address or '').split(',')[0].strip()
        if not nt.is_ip(ip) or ip in seen:
            continue
        name = d.hostname or ''
        if q and q not in name.lower() and q not in ip and q not in (d.mac_address or '').lower():
            continue
        seen.add(ip)
        out.append({'ip': ip, 'mac': (d.mac_address or '').upper(), 'name': name, 'type': d.connection_type})
        if len(out) >= 60:
            break
    return JsonResponse({'ok': True, 'devices': out})


@login_required
@require_POST
def network_tools_verdict(request):
    """Put the measurements together: which link is slow, and the record kept in Recent tests."""
    from . import pathtrace
    b = _b(request)
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        data = {}
    router = _router(request, data.get('router'))
    if not router:
        return JsonResponse({'ok': False, 'message': 'Choose a router first.'}, status=400)
    ip, mac = str(data.get('ip') or ''), str(data.get('mac') or '')
    try:
        ip = nt.clean_host(ip) if ip else ''
        mac = pathtrace.clean_mac(mac) if mac else ''
    except ValueError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)}, status=400)

    def test(pk, kind):
        t = b.net_tests.filter(pk=pk, router=router, kind=kind, status='done').first() if str(pk or '').isdigit() else None
        return t.result if t else None
    idle, load = test(data.get('idle'), 'hops'), test(data.get('load'), 'hops')
    if not idle:
        return JsonResponse({'ok': False, 'message': 'The path check has not finished.'}, status=400)
    speed = test(data.get('speed'), 'speed')
    router_speed = speed.get('mbps') if speed and speed.get('ok') else None
    dev = {}
    for k in ('down', 'up', 'rtt'):
        try:
            v = float((data.get('device') or {}).get(k))
            if 0 <= v < 100000:
                dev[k] = round(v, 1)
        except (TypeError, ValueError):
            pass
    path = pathtrace.build_path(router, ip, mac)
    res = pathtrace.analyze(path, idle, load, dev, router_speed)
    res['path_note'] = path['note']
    res['port'], res['router'] = path['port'], path['router']
    res['guessed'] = path['guessed']
    if data.get('preview'):                     # the quiet check drawn while the rest still runs: not a record yet
        return JsonResponse({'ok': True, 'test': {'result': res}})
    t = NetTest.objects.create(business=b, router=router, kind='mypath', target=path['device']['name'] or ip,
                               params={'ip': ip, 'mac': mac}, via=data.get('via', '')[:20] if isinstance(data.get('via'), str) else '',
                               created_by=request.user, status='done', result=res, finished_at=timezone.now())
    return JsonResponse({'ok': True, 'test': as_json(t)})


@login_required
@require_POST
def network_tools_save_chain(request):
    """Save the order the check found (MikroTik outwards) as the chain on Topology, so future checks use it."""
    from .models import SiteRouter
    from .site_routers import collect
    b = _b(request)
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        data = {}
    router = _router(request, data.get('router'))
    keys = data.get('keys') or []
    port = str(data.get('port') or '').strip()[:64]
    if not router or not isinstance(keys, list) or not 1 <= len(keys) <= 14:
        return JsonResponse({'ok': False, 'message': 'Nothing to save.'}, status=400)
    entries = {e['key']: e for e in collect(b)}
    if any(k not in entries for k in keys):
        return JsonResponse({'ok': False, 'message': 'Some of these boxes are no longer known — run the check again.'}, status=400)
    prev = None
    for i, k in enumerate(keys):
        e = entries[k]
        s = SiteRouter.objects.filter(business=b, pk=e['id']).first() if e['id'] else None
        if not s and e['mac']:
            s = SiteRouter.objects.filter(business=b, mac_address=e['mac']).first()
        if not s:
            s = SiteRouter(business=b, mac_address=e['mac'], ip_address=e['ip'], name=e['name'][:120], brand=e['brand'][:40],
                           role='repeater' if i else 'ap', source='auto', created_by=request.user)
        s.status = 'confirmed'
        s.router = router
        s.port = port if i == 0 else ''
        s.parent = prev
        s.save()
        prev = s
    from .utils import log
    log(b, 'Topology', f'Chain saved from My connection on {router.name}: {len(keys)} boxes')
    return JsonResponse({'ok': True, 'saved': len(keys)})


BLOB = None


@login_required
@require_GET
def network_tools_blob(request):
    """Bytes for the device speed test when the public test server cannot be reached (and the latency probe)."""
    global BLOB
    from django.http import StreamingHttpResponse
    import os
    try:
        n = max(0, min(30_000_000, int(request.GET.get('bytes', '0'))))
    except ValueError:
        n = 0
    if BLOB is None:
        BLOB = os.urandom(256 * 1024)

    def gen():
        left = n
        while left > 0:
            chunk = BLOB[:min(len(BLOB), left)]
            left -= len(chunk)
            yield chunk
    resp = StreamingHttpResponse(gen(), content_type='application/octet-stream')
    resp['Cache-Control'] = 'no-store'
    resp['Content-Encoding'] = 'identity'
    resp['Content-Length'] = str(n)
    return resp


@login_required
@require_POST
def network_tools_sink(request):
    """Upload target for the device speed test fallback: reads and throws away."""
    total = 0
    while True:
        chunk = request.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > 2_000_000:
            break
    return JsonResponse({'ok': True, 'bytes': total})
