"""Online sign-in and "send this voucher to staff".

* **Online sign-in** (/p/<slug>/go/) — a page hosted by TapTap that customers open from a QR code when the router's own
  login page does not open or the router is busy. It checks the voucher on TapTap first (the same rules as the router
  page: paused, disabled, expired, device slots), then sends the phone to the router's login address
  (``http://<login address>/login?username=…&password=…``) and the router signs it in. TapTap's address is always in
  the walled garden (core/free_access.py) so the page opens before the customer is logged in.
* **Voucher diagnosis** — every reason a voucher can fail, in plain words, for the customer and for staff.
* **Send this voucher to staff** — the customer sends the code (with name, phone and what happened); staff see it with
  the diagnosis under Vouchers → Customer reports, and get a notification.
"""
from __future__ import annotations

import json
import re

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models_voucher_reports import VoucherReport


def _client_ip(request):
    return request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', '')).split(',')[0].strip()[:64]


def _page(slug):
    from .models import PortalPage
    page = PortalPage.objects.select_related('business').filter(slug=slug).first()
    if not page:
        raise Http404
    return page


def _when(dt):
    return timezone.localtime(dt).strftime('%d %b %Y at %H:%M') if dt else ''


# ─────────────────────────────── diagnosis ───────────────────────────────

def diagnose(business, code):
    """Why a voucher works or not. Returns {'found', 'state', 'title', 'customer', 'checks': [[level, text]], 'voucher_id'}."""
    from .models import Voucher
    code = str(code or '').replace(' ', '').strip()
    v = Voucher.all_objects.filter(business=business, code__iexact=code).select_related('router').first() if code else None
    if not v:
        return {'found': False, 'state': 'unknown', 'title': 'Code not recognised',
                'customer': 'That code was not recognised. Check each letter and number (0 and O, 1 and I look alike).',
                'checks': [['bad', f'No voucher or member with the code “{code}” exists in this business.']], 'voucher_id': None}
    now = timezone.now()
    checks = [['info', f'{"Member" if v.is_member else "Voucher"} {v.code} · plan {v.plan_name or "—"} · '
                       f'{"on " + v.router.name if v.router_id else "no router assigned"}']]
    state, title, customer = 'ok', 'This voucher looks fine', ('This voucher is valid. If it still does not connect, forget the '
                                                              'Wi-Fi network on your phone, join again and open the login page.')
    if v.deleted_at:
        state, title = 'deleted', 'Deleted'
        customer = 'This voucher was cancelled. Ask staff for help.'
        checks.append(['bad', f'Moved to the bin on {_when(v.deleted_at)}.'])
    elif v.frozen_at:
        state, title = 'paused', 'Paused by staff'
        customer = 'This voucher is paused by staff' + (f': {v.freeze_reason}' if v.freeze_reason else '.') + ' Ask staff to unpause it.'
        checks.append(['warn', f'Paused since {_when(v.frozen_at)}' + (f' — {v.freeze_reason}' if v.freeze_reason else '') + '.'])
    elif v.status == 'disabled':
        state, title = 'disabled', 'Disabled'
        customer = 'This voucher has been disabled. Ask staff for help.'
        checks.append(['bad', 'Disabled by staff (see the voucher history for why).'])
    elif v.expires_at and v.expires_at <= now:
        state, title = 'expired', 'Expired'
        customer = f'This voucher expired on {_when(v.expires_at)}. Buy a new voucher to keep browsing.'
        checks.append(['bad', f'Expired on {_when(v.expires_at)} (first used {_when(v.used_at) or "—"}).'])
    else:
        if v.expires_at:
            left = v.expires_at - now
            checks.append(['ok', f'Valid until {_when(v.expires_at)} ({_left(left)} left).'])
        elif v.used_at is None:
            checks.append(['ok', 'Never used yet — the time starts at the first login.'])
        else:
            checks.append(['ok', 'No end date (unlimited).'])
        if not v.router_id:
            checks.append(['warn', 'Not assigned to a router: it only works on routers that accept vouchers from any router.'])
    # Device slots (sticky vouchers) and devices online now
    try:
        from . import device_lock
        if device_lock.enabled(v):
            n, slots = v.device_bindings.count(), max(1, v.max_devices or 1)
            online = set(device_lock.online_hints(v) or [])
            labels = ', '.join(f'{b.label or b.current_mac}' + (' (online now)' if b.current_mac in online else '')
                               for b in v.device_bindings.order_by('slot_no')[:5])
            if n >= slots:
                checks.append(['warn' if state == 'ok' else 'info',
                               f'All {slots} device slot{"s are" if slots != 1 else " is"} taken: {labels or "—"}. A new device is refused.'])
                if state == 'ok':
                    state, title = 'slots_full', 'Device slots are full'
                    customer = (f'This voucher is already used on {n} device{"s" if n != 1 else ""} — the most it allows. '
                                f'Use it on the same phone, or ask staff to free a slot.')
            else:
                checks.append(['ok', f'{n} of {slots} device slot{"s" if slots != 1 else ""} taken{": " + labels if labels else ""}.'])
            if online and state == 'ok':
                checks.append(['info', f'Online right now on {len(online)} device{"s" if len(online) != 1 else ""}.'])
    except Exception:
        pass
    return {'found': True, 'state': state, 'title': title, 'customer': customer, 'checks': checks, 'voucher_id': v.pk,
            'code': v.code, 'member': v.is_member}


def _left(delta):
    secs = max(0, int(delta.total_seconds()))
    d, h, m = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
    return f'{d}d {h}h' if d else (f'{h}h {m}m' if h else f'{m}m')


# ─────────────────────────────── public pages ───────────────────────────────

# ─────────────────────────────── where the router's login page is ───────────────────────────────

def hotspot_ips(router):
    """The address(es) the router's hotspot login answers on, from its stored configuration.

    An IP needs no name lookup, so it works even when the phone uses Private DNS / Secure DNS (common on Android and in
    Chrome) or when the router never got the easy name (login.wifi) — both make "http://login.wifi" unreachable."""
    import ipaddress
    try:
        sections = router.config_snapshot.sections or {}
    except Exception:
        return []
    rows = lambda label: ((sections.get(label) or {}).get('rows') or [])
    get = lambda row, *keys: next((str(row.get(k)) for k in keys if row.get(k) not in (None, '')), '')
    profiles = {get(p, 'name'): p for p in rows('HotSpot server profiles')}
    addrs = rows('IP addresses')
    out = []
    for srv in rows('HotSpot servers'):
        if get(srv, 'disabled').lower() in ('true', 'yes'):
            continue
        prof = profiles.get(get(srv, 'profile'), {})
        ip = get(prof, 'hotspot-address', 'hotspot_address')
        if not ip:
            iface = get(srv, 'interface')
            hit = next((a for a in addrs if get(a, 'interface') == iface and get(a, 'disabled').lower() not in ('true', 'yes')), None)
            ip = get(hit, 'address').split('/')[0] if hit else ''
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        if ip not in out:
            out.append(ip)
    return out


def login_host(request, business):
    """Where to send the phone to sign in: the router's hotspot IP when TapTap can tell, else the easy name."""
    from . import geomap
    routers = []
    rid = str(request.GET.get('r') or '')
    if rid.isdigit():
        routers = list(business.routers.filter(pk=rid))
    if not routers:
        try:
            routers = geomap.sites_from_ip(business, _client_ip(request))
        except Exception:
            routers = []
    if not routers:                          # not matched: every router — fine when they share one hotspot IP
        routers = list(business.routers.all()[:20])
    ips = []
    for r in routers:
        for ip in hotspot_ips(r):
            if ip not in ips:
                ips.append(ip)
    if len(ips) == 1:                        # one answer for every router that could be serving this phone
        return ips[0]
    return (getattr(business, 'hotspot_dns_name', '') or 'login.wifi').strip()


def online_page(request, slug, auto=None):
    """Online sign-in: the login page designed in Portal Studio, served by TapTap, signing in through the router.

    The design gets the router's login address (link-login-only) when the router did not pass one — the hotspot IP
    when TapTap can tell, else the easy name — and, from a voucher QR / quick-login link, the code to sign in with."""
    from .views_studio import _public_ctx, _safe_json
    page = _page(slug)
    owner_preview = request.user.is_authenticated and getattr(request.user, 'business', None) == page.business
    if not page.is_published and not owner_preview:
        raise Http404
    ctx = _public_ctx(request, page, 'hosted')
    if not ctx['mt'].get('linkLoginOnly'):
        ctx['mt']['linkLoginOnly'] = f'http://{login_host(request, page.business)}/login'
    code = re.sub(r'[^A-Za-z0-9._@-]', '', request.GET.get('code', '') or (auto or {}).get('user', ''))[:40]
    if code:
        ctx['autoLogin'] = {'code': code, 'member': bool(auto and auto.get('member')),
                            'password': (auto or {}).get('pw', '') if auto and auto.get('member') else ''}
    return render(request, 'core/studio/portal_public.html', {'page': page, 'config_json': _safe_json(page.config),
                                                              'ctx_json': _safe_json(ctx), 'draft': not page.is_published})


def online_login(request, slug):
    """/p/<slug>/login?username=CODE&password=CODE — the "Quick login" link and the QR printed on vouchers.

    That address is the MikroTik login format; when a business's login address is its online portal the link points
    here. TapTap opens the online sign-in page with the code filled in and signs in straight away (vouchers: the code
    is username and password; members: their own password)."""
    user = str(request.GET.get('username') or '').strip()
    pw = str(request.GET.get('password') or '')
    if not user:
        return online_page(request, slug)
    member = bool(pw) and pw != user
    return online_page(request, slug, auto={'user': user, 'pw': pw[:64], 'member': member})


def _cors(resp):
    resp['Access-Control-Allow-Origin'] = '*'
    return resp


@csrf_exempt
def online_report(request, slug):
    """A customer sends a voucher to staff — from the online page or the router's own page (cross-site, text/plain)."""
    if request.method == 'OPTIONS':
        resp = JsonResponse({'ok': True})
        resp['Access-Control-Allow-Origin'] = '*'
        resp['Access-Control-Allow-Methods'] = 'POST'
        resp['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp
    if request.method != 'POST':
        return JsonResponse({'ok': False}, status=405)
    page = _page(slug)
    ip = _client_ip(request)
    key = f'tt:vreport:{page.pk}:{ip}'
    try:
        sent = cache.get(key, 0)
    except Exception:                        # cache down: still take the report
        sent = 0
    if sent >= 5:
        return _cors(JsonResponse({'ok': False, 'message': 'You have sent several already — staff will look at them soon.'}, status=429))
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        data = {}
    code = re.sub(r'\s', '', str(data.get('code') or ''))[:80]
    if not code:
        return _cors(JsonResponse({'ok': False, 'message': 'Type the voucher code.'}, status=400))
    phone = re.sub(r'[^0-9+ ]', '', str(data.get('phone') or ''))[:40].strip()
    if not phone:
        return _cors(JsonResponse({'ok': False, 'message': 'Add a phone number so staff can reach you.'}, status=400))
    d = diagnose(page.business, code)
    mac = str(data.get('mac') or '').upper()[:20]
    report = VoucherReport.objects.create(
        business=page.business, voucher_id=d.get('voucher_id'), code=code, name=str(data.get('name') or '')[:80].strip(),
        phone=phone, message=str(data.get('message') or '')[:500].strip(), shown_error=str(data.get('error') or '')[:300],
        diagnosis=d, mac=mac if re.fullmatch(r'([0-9A-F]{2}:){5}[0-9A-F]{2}', mac) else '', ip=ip,
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:300], portal_slug=page.slug)
    try:
        cache.set(key, sent + 1, 600)
    except Exception:
        pass
    from django.urls import reverse
    try:                                     # the report is saved either way; a notification hiccup must not lose it
        from .notify import notify
        notify(page.business, 'voucher_report', f'Voucher {code} sent to staff — {d["title"]}',
               f'{report.name or "A customer"} ({phone}) says: {report.message or "the voucher does not work"}.\nTapTap found: {d["title"]}.',
               link=reverse('voucher_reports'), key=f'vreport:{report.pk}')
    except Exception:
        import logging
        logging.getLogger('taptap.portal').exception('voucher report %s: notification failed', report.pk)
    resp = JsonResponse({'ok': True, 'title': d['title'], 'message': d['customer']})
    resp['Access-Control-Allow-Origin'] = '*'
    return resp


# ─────────────────────────────── staff ───────────────────────────────

@login_required
def voucher_reports(request):
    b = request.user.business
    if request.method == 'POST':
        r = get_object_or_404(VoucherReport, business=b, pk=request.POST.get('report') or 0)
        if request.POST.get('action') == 'reopen':
            r.status, r.resolved_at, r.resolved_by = 'open', None, None
        else:
            r.status, r.resolved_at, r.resolved_by = 'resolved', timezone.now(), request.user
            r.staff_note = request.POST.get('note', '').strip()[:500]
        r.save()
        return redirect(request.path + ('?show=all' if request.GET.get('show') == 'all' else ''))
    show_all = request.GET.get('show') == 'all'
    qs = VoucherReport.objects.filter(business=b).select_related('voucher', 'resolved_by')
    reports = list((qs if show_all else qs.filter(status='open'))[:200])
    for r in reports:                                        # the voucher today, not only when it was sent
        r.now = diagnose(b, r.code)
    return render(request, 'core/voucher_reports.html', {'reports': reports, 'show_all': show_all,
                                                         'open_count': qs.filter(status='open').count()})


@login_required
def portal_qr(request, pk):
    """Printable QR card for online sign-in."""
    from .models import PortalPage
    page = get_object_or_404(PortalPage, business=request.user.business, pk=pk)
    from django.conf import settings
    base = (getattr(settings, 'SITE_URL', '') or request.build_absolute_uri('/')).rstrip('/')
    routers = list(page.business.routers.order_by('name'))
    rid = str(request.GET.get('r') or '')
    chosen = next((r for r in routers if str(r.pk) == rid), None)
    url = f'{base}/p/{page.slug}/go/' + (f'?r={chosen.pk}' if chosen else '')
    return render(request, 'core/portal_qr.html', {'page': page, 'url': url, 'business': page.business, 'routers': routers,
                                                   'chosen': chosen,
                                                   'ips': {r.pk: ', '.join(hotspot_ips(r)) or 'unknown' for r in routers}})


# ─────────────────────────────── prepare routers for online sign-in ───────────────────────────────

SETUP_COMMENT = 'TapTap online sign-in'


def setup_script(host):
    """RouterOS: let phones reach TapTap over HTTPS before login, and accept the plain-password login the online page
    uses (http-pap is ADDED to each hotspot profile's login methods; nothing is removed)."""
    from .agent import rs
    h, c = rs(host), rs(SETUP_COMMENT)
    return (f':do {{ /ip hotspot walled-garden ip remove [find where comment={c}] }} on-error={{}}; '
            f':do {{ /ip hotspot walled-garden ip add dst-host={h} action=accept comment={c} }} on-error={{}}; '
            f':do {{ /ip hotspot walled-garden remove [find where comment={c}] }} on-error={{}}; '
            f':do {{ /ip hotspot walled-garden add dst-host={h} action=allow comment={c} }} on-error={{}}; '
            ':foreach p in=[/ip hotspot profile find] do={ :local s ""; '
            ':foreach x in=[/ip hotspot profile get $p login-by] do={ :set s ($s . $x . ",") }; '
            ':if ([:typeof [:find $s "http-pap"]] = "nil") do={ /ip hotspot profile set $p login-by=($s . "http-pap") } }')


def link_body(cmd, url, check, nonce_value):
    return '{ ' + setup_script(cmd.params['host']) + ' }'


@login_required
@require_POST
def prepare_routers(request):
    """Send every router the online sign-in setup."""
    from urllib.parse import urlsplit
    from django.conf import settings
    from .voucher_history import channel
    host = urlsplit(getattr(settings, 'SITE_URL', '') or request.build_absolute_uri('/')).hostname or ''
    if not re.fullmatch(r'[A-Za-z0-9.-]{3,253}', host):
        return JsonResponse({'ok': False, 'message': 'SITE_URL is not set.'}, status=400)
    results = []
    for r in request.user.business.routers.order_by('name'):
        try:
            if channel(r) == 'TapTap Link':
                from .linkops import send
                send(r, 'online_signin', {'host': host}, label='Prepare online sign-in', user=request.user, minutes=60 * 24)
                results.append({'router': r.name, 'ok': True, 'text': 'sent — applied at the next check-in'})
            else:
                from .mikrotik import MikroTikService
                from .portctl import schedule_on_router
                svc = MikroTikService(r, timeout=20).connect()
                try:
                    schedule_on_router(svc, 'taptap-online-signin', 3, setup_script(host))
                finally:
                    svc.close()
                results.append({'router': r.name, 'ok': True, 'text': 'done'})
        except Exception as exc:  # noqa: BLE001
            results.append({'router': r.name, 'ok': False, 'text': str(exc)[:160]})
    return JsonResponse({'ok': True, 'results': results})
