"""Network › Other routers & APs: TP-Link and other routers behind your MikroTik, and the Omada controller."""
import ssl
import urllib.error
import urllib.request

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import netdev
from .models_netdev import NetDevice, OmadaController, RemoteSession


def _b(request):
    return request.user.business


def _ip(request):
    return (request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR', ''))


@login_required
def netdev_list(request):
    from .device_block import norm
    from .models import RouterDevice
    from .net_vendors import brand_of
    business = _b(request)
    items = list(business.net_devices.select_related('router'))
    seen = {}
    for rd in RouterDevice.objects.filter(router__business=business).only('mac_address', 'ip_address', 'hostname', 'last_seen_at', 'is_online', 'router'):
        if rd.mac_address:
            seen[norm(rd.mac_address)] = rd
        if rd.ip_address:
            seen.setdefault(rd.ip_address, rd)
    for d in items:
        rd = seen.get(norm(d.mac)) or seen.get(d.ip)
        d.online = bool(rd and rd.is_online)
        d.seen = rd.last_seen_at if rd else None
        d.reach_mode, d.reach_note = netdev.reach(d.router)
    known = {norm(d.mac) for d in items} | {d.ip for d in items}
    suggestions = []
    for rd in RouterDevice.objects.filter(router__business=business).select_related('router').order_by('-last_seen_at')[:2000]:
        brand = brand_of(rd.mac_address)
        if brand and norm(rd.mac_address) not in known and rd.ip_address not in known:
            suggestions.append({'brand': brand, 'mac': norm(rd.mac_address), 'ip': rd.ip_address, 'name': rd.hostname, 'router': rd.router})
            known.add(norm(rd.mac_address))
    omada = OmadaController.objects.filter(business=business).first()
    ap, cl, omada_err = [], [], ''
    if omada and omada.site_id:
        from . import omada as om
        try:
            ap = [{'name': x.get('name') or x.get('mac', ''), 'model': x.get('model', ''), 'ip': x.get('ip', ''), 'mac': x.get('mac', ''),
                   'online': x.get('status') in (1, 14) or x.get('statusCategory') == 1, 'status': x.get('status', ''), 'clients': x.get('clientNum')}
                  for x in om.devices(omada)]
            cl = [{'name': x.get('name') or x.get('hostName') or '', 'ip': x.get('ip', ''), 'mac': x.get('mac', ''), 'ssid': x.get('ssid', ''),
                   'ap': x.get('apName', ''), 'blocked': bool(x.get('blocked'))} for x in om.clients(omada)]
        except om.OmadaError as exc:
            omada_err = str(exc)
    sessions = RemoteSession.objects.filter(device__business=business, closed_at__isnull=True, expires_at__gt=timezone.now()).select_related('device', 'user')
    return render(request, 'core/netdev.html', {'items': items, 'suggestions': suggestions[:30], 'routers': business.routers.order_by('name'),
                                                'kinds': NetDevice.KINDS, 'omada': omada, 'omada_aps': ap, 'omada_clients': cl[:300],
                                                'omada_err': omada_err, 'sessions': sessions})


@login_required
@require_POST
def netdev_save(request):
    business = _b(request)
    p = request.POST
    d = get_object_or_404(business.net_devices, pk=p['id']) if p.get('id') else NetDevice(business=business, created_by=request.user)
    try:
        import ipaddress
        ipaddress.ip_address(p.get('ip', '').strip())
    except ValueError:
        messages.error(request, 'Type the device’s address on your network, e.g. 192.168.88.20.')
        return redirect('netdev_list')
    d.name = p.get('name', '').strip()[:120] or p.get('brand', '').strip() or p['ip']
    d.ip, d.mac = p['ip'].strip(), p.get('mac', '').strip().upper()[:32]
    d.brand, d.model, d.notes = p.get('brand', '').strip()[:60], p.get('model', '').strip()[:80], p.get('notes', '').strip()[:255]
    d.kind = p.get('kind') if p.get('kind') in dict(NetDevice.KINDS) else 'router'
    d.router = business.routers.filter(pk=p.get('router') or 0).first()
    d.web_port = int(p['web_port']) if str(p.get('web_port', '')).isdigit() and 0 < int(p['web_port']) < 65536 else 80
    d.web_https = p.get('web_https') == 'on'
    d.username = p.get('username', '').strip()[:80]
    if p.get('password'):
        d.set_password(p['password'])
    d.omada_mac = p.get('omada_mac', '').strip()[:32]
    d.save()
    from .utils import log
    log(business, 'Network Device Saved', f'{d.name} ({d.ip}) behind {d.router.name if d.router else "—"}')
    messages.success(request, f'{d.name} saved.' + ('' if d.password_enc else ' Add its admin password to log in faster.'))
    return redirect('netdev_list')


@login_required
@require_POST
def netdev_action(request, pk):
    business = _b(request)
    d = get_object_or_404(business.net_devices.select_related('router'), pk=pk)
    act = request.POST.get('action')
    from .utils import log
    if act == 'delete':
        log(business, 'Network Device Removed', f'{d.name} ({d.ip})'); d.delete()
        messages.info(request, 'Device removed from TapTap (nothing was changed on it).')
        return redirect('netdev_list')
    if act == 'reveal':
        log(business, 'Network Device Password Viewed', f'{d.name} by {request.user.get_full_name() or request.user.username}')
        return JsonResponse({'username': d.username, 'password': d.get_password()})
    if act == 'open':
        try:
            s = netdev.open_session(d, request.user, _ip(request))
        except netdev.RemoteError as exc:
            messages.error(request, str(exc)); return redirect('netdev_list')
        return redirect('netdev_session', token=s.token)
    raise Http404


@login_required
def netdev_session(request, token):
    s = get_object_or_404(RemoteSession.objects.select_related('device__router'), token=token, user=request.user)
    if request.method == 'POST' and request.POST.get('action') == 'close':
        netdev.close_session(s); messages.info(request, 'Remote admin closed — the path on the router is removed.')
        return redirect('netdev_list')
    live = not s.closed_at and s.expires_at > timezone.now()
    url = f'/remote/{s.token}/' if s.mode == 'proxy' else netdev.direct_url(s)
    return render(request, 'core/netdev_session.html', {'s': s, 'd': s.device, 'live': live, 'url': url,
                                                        'queued': netdev._channel(s.device.router) == 'TapTap Link'})


HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailers', 'transfer-encoding', 'upgrade',
       'content-encoding', 'content-length'}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


@csrf_exempt
@login_required
def remote_proxy(request, token, path=''):
    """The device's own admin page, shown through TapTap (proxy sessions only)."""
    s = RemoteSession.objects.select_related('device').filter(token=token, user=request.user, mode='proxy', closed_at__isnull=True,
                                                               expires_at__gt=timezone.now()).first()
    if not s:
        return HttpResponse('This remote admin session has ended. Open it again from Network › Other routers & APs.', status=410)
    d = s.device
    prefix = f'/remote/{s.token}/'
    url = f'{"https" if d.web_https else "http"}://{s.target_host}:{s.port}/{path}' + (f'?{request.META["QUERY_STRING"]}' if request.META.get('QUERY_STRING') else '')
    req = urllib.request.Request(url, data=request.body if request.method not in ('GET', 'HEAD') else None, method=request.method)
    for k, v in request.headers.items():
        lk = k.lower()
        if lk in HOP or lk in ('host', 'cookie', 'accept-encoding', 'origin', 'referer'):
            continue
        req.add_header(k, v)
    keep = '; '.join(f'{k}={v}' for k, v in request.COOKIES.items() if k not in ('sessionid', 'csrftoken'))
    if keep:
        req.add_header('Cookie', keep)
    base = f'{"https" if d.web_https else "http"}://{d.ip}' + ('' if d.web_port in (80, 443) else f':{d.web_port}')
    req.add_header('Referer', base + '/' + path); req.add_header('Origin', base)
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    try:
        r = opener.open(req, timeout=20)
        status, headers, body = r.status, r.headers, r.read()
    except urllib.error.HTTPError as exc:
        status, headers, body = exc.code, exc.headers, exc.read()
    except Exception as exc:
        return HttpResponse(f'{d.name} did not answer through {d.router.name if d.router else "the router"}: {exc}. '
                            f'If you just opened the session on a TapTap Link router, wait a few seconds and reload.', status=502)
    ctype = headers.get('Content-Type', 'application/octet-stream')
    if any(t in ctype for t in ('text/html', 'javascript', 'text/css')):
        body = netdev.rewrite(body, ctype, prefix)
    resp = HttpResponse(body, status=status, content_type=ctype)
    for k, v in headers.items():
        lk = k.lower()
        if lk in HOP or lk in ('content-type', 'set-cookie', 'x-frame-options', 'content-security-policy', 'strict-transport-security'):
            continue
        if lk == 'location':
            for origin in (f'http://{d.ip}', f'https://{d.ip}', f'http://{s.target_host}:{s.port}', f'https://{s.target_host}:{s.port}'):
                v = v.replace(origin + (f':{d.web_port}' if origin.endswith(d.ip) and d.web_port not in (80, 443) else ''), '')
            if v.startswith('/') and not v.startswith(prefix):
                v = prefix + v.lstrip('/')
        resp[k] = v
    for c in headers.get_all('Set-Cookie') or []:
        parts = [x for x in c.split(';') if not x.strip().lower().startswith(('domain=', 'path=', 'secure'))]
        name, _, val = parts[0].partition('=')
        resp.set_cookie(name.strip(), val.strip(), path=prefix, httponly='httponly' in c.lower(), samesite='Lax')
    return resp


@login_required
@require_POST
def omada_save(request):
    from . import omada as om
    business = _b(request)
    c = OmadaController.objects.filter(business=business).first() or OmadaController(business=business)
    p = request.POST
    if p.get('action') == 'remove':
        if c.pk:
            c.delete()
        messages.info(request, 'Omada controller removed from TapTap.')
        return redirect('netdev_list')
    c.base_url = p.get('base_url', '').strip()[:200]
    c.omadac_id, c.client_id = p.get('omadac_id', '').strip()[:80], p.get('client_id', '').strip()[:120]
    c.set_secret(p.get('client_secret', '').strip())
    c.verify_ssl = p.get('verify_ssl') == 'on'
    if p.get('site_id'):
        c.site_id = p['site_id'][:80]
    c.save()
    try:
        sites = om.test(c)
        messages.success(request, f'Connected to Omada — site “{c.site_name or c.site_id}” ({len(sites)} site(s) found).')
    except om.OmadaError as exc:
        c.last_error = str(exc)[:255]; c.save(update_fields=['last_error'])
        messages.error(request, str(exc))
    return redirect('netdev_list')


@login_required
@require_POST
def omada_action(request):
    from . import omada as om
    from .utils import log
    business = _b(request)
    c = om.for_business(business)
    if not c:
        raise Http404
    act, mac = request.POST.get('action'), request.POST.get('mac', '')
    try:
        if act in ('block', 'unblock'):
            om.block_client(c, mac, act == 'block')
            messages.success(request, f'{mac} {"blocked" if act == "block" else "unblocked"} on the Omada Wi-Fi.')
        elif act == 'reboot':
            om.reboot_device(c, mac)
            messages.success(request, f'Access point {mac} is restarting.')
        log(business, 'Omada Action', f'{act} {mac}')
    except om.OmadaError as exc:
        messages.error(request, str(exc))
    return redirect('/network/devices/#omada')
