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

def online_page(request, slug, auto=None):
    """The online sign-in page customers open from the QR code."""
    page = _page(slug)
    b = page.business
    login_host = (getattr(b, 'hotspot_dns_name', '') or 'login.wifi').strip()
    code = re.sub(r'[^A-Za-z0-9._@-]', '', request.GET.get('code', '') or (auto or {}).get('user', ''))[:40]
    return render(request, 'core/portal_online.html', {
        'page': page, 'business': b, 'login_host': login_host,
        'after': (getattr(b, 'portal_redirect_url', '') or 'http://neverssl.com/'),
        'prefill': code, 'auto': bool(auto and code), 'member': bool(auto and auto.get('member')),
        'prefill_pw': (auto or {}).get('pw', '') if auto and auto.get('member') else '',
    })


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


@csrf_exempt
@require_POST
def online_report(request, slug):
    """A customer sends a voucher to staff."""
    page = _page(slug)
    ip = _client_ip(request)
    key = f'tt:vreport:{page.pk}:{ip}'
    if cache.get(key, 0) >= 5:
        return JsonResponse({'ok': False, 'message': 'You have sent several already — staff will look at them soon.'}, status=429)
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        data = {}
    code = re.sub(r'\s', '', str(data.get('code') or ''))[:80]
    if not code:
        return JsonResponse({'ok': False, 'message': 'Type the voucher code.'}, status=400)
    phone = re.sub(r'[^0-9+ ]', '', str(data.get('phone') or ''))[:40].strip()
    if not phone:
        return JsonResponse({'ok': False, 'message': 'Add a phone number so staff can reach you.'}, status=400)
    d = diagnose(page.business, code)
    mac = str(data.get('mac') or '').upper()[:20]
    report = VoucherReport.objects.create(
        business=page.business, voucher_id=d.get('voucher_id'), code=code, name=str(data.get('name') or '')[:80].strip(),
        phone=phone, message=str(data.get('message') or '')[:500].strip(), shown_error=str(data.get('error') or '')[:300],
        diagnosis=d, mac=mac if re.fullmatch(r'([0-9A-F]{2}:){5}[0-9A-F]{2}', mac) else '', ip=ip,
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:300], portal_slug=page.slug)
    cache.set(key, cache.get(key, 0) + 1, 600)
    from django.urls import reverse
    from .notify import notify
    notify(page.business, 'voucher_report', f'Voucher {code} sent to staff — {d["title"]}',
           f'{report.name or "A customer"} ({phone}) says: {report.message or "the voucher does not work"}.\nTapTap found: {d["title"]}.',
           link=reverse('voucher_reports'), key=f'vreport:{report.pk}')
    return JsonResponse({'ok': True, 'title': d['title'], 'message': d['customer']})


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
    return render(request, 'core/portal_qr.html', {'page': page, 'url': f'{base}/p/{page.slug}/go/', 'business': page.business})
