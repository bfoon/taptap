"""The TapTap mobile app's API — /api/mobile/v1/ (the Flutter app in mobile/).

Sign-in hands the phone a token (core/mobile_auth.py). After that the app uses:

    these endpoints      me · business · push · notifications · home · sales · complaints · customer calls · devices
                         vouchers · batches (core/views_mobile_vouchers.py)
    the website's own    /chat/*  (conversations, photos, calls: /chat/call/poll/ + /chat/call/act/)
                         /fit/*   (Fit, the AI: state · ask · action · new)

Using the website's own chat, call and Fit endpoints is deliberate: Fit can do on the phone exactly what it can
do for this person on the web — same tools, same permissions, same "Allowed" groups in Fit settings, same
typed confirmations — because it is the same code.
"""
import json
from datetime import date, datetime
from decimal import Decimal
from functools import wraps

from django.contrib.auth import authenticate
from django.core.cache import cache
from django.db import models as djm
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from . import mobile_auth

API_VERSION = 1
LOGIN_TRIES = 8                      # per IP + email, per 15 minutes
TICKET_TTL = 900


# ─────────────────────────────── helpers ───────────────────────────────

def _body(request):
    try:
        d = json.loads(request.body or b'{}')
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _err(msg, status=400, **extra):
    return JsonResponse({'ok': False, 'message': msg, **extra}, status=status)


def _signed_in(view):
    @wraps(view)
    def wrap(request, *a, **kw):
        if not request.user.is_authenticated:
            return _err('Signed out — please sign in again.', 401, auth=False)
        return view(request, *a, **kw)
    return csrf_exempt(wrap)


def _business(request):
    return getattr(request, 'tt_business', None)


def _perms(request):
    return frozenset(getattr(request, 'tt_perms', None) or ())


def _feature(b, key):
    try:
        from .features import enabled
        return enabled(b, key)
    except Exception:
        return True


def clean(v):
    """Make builder output (Decimals, dates, model rows) JSON-safe."""
    from .models import Agent, VoucherSale
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return timezone.localtime(v).isoformat() if timezone.is_aware(v) else v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [clean(x) for x in v]
    if isinstance(v, VoucherSale):
        return {'id': v.pk, 'at': clean(v.sold_at), 'plan': v.plan_name, 'code': v.voucher_code, 'amount': float(v.amount),
                'discount': float(v.discount), 'commission': float(v.commission), 'method': v.get_payment_method_display(),
                'seller': v.agent.name if v.agent_id and v.agent else 'Shop', 'site': v.router.name if v.router_id and v.router else '',
                'customer': v.customer_name or v.customer_phone}
    if isinstance(v, Agent):
        return {'id': v.pk, 'name': v.name}
    if isinstance(v, djm.Model):
        if hasattr(v, 'price') and hasattr(v, 'name'):
            return {'id': v.pk, 'name': v.name, 'price': float(v.price or 0)}
        if hasattr(v, 'amount') and hasattr(v, 'collected_at'):
            return {'id': v.pk, 'amount': float(v.amount), 'at': clean(v.collected_at)}
        return {'id': v.pk, 'name': str(v)}
    return v


# ─────────────────────────────── sign in ───────────────────────────────

def _device_fields(d):
    return {'name': str(d.get('device') or '')[:80], 'platform': str(d.get('platform') or ''),
            'app_version': str(d.get('app_version') or '')[:20]}


def _issue(request, user, fields):
    from .auth_security import client_ip
    dev, token = mobile_auth.create_device(user, ip=client_ip(request), **fields)
    if user.email:                            # the same "new sign-in" security email the website sends
        from types import SimpleNamespace
        from .auth_security import notify_new_device
        notify_new_device(user, SimpleNamespace(label=f'TapTap app · {dev.name or dev.get_platform_display()}',
                                                ip_address=dev.last_ip, created_at=dev.created_at))
    return JsonResponse({'ok': True, 'token': token, 'device': dev.pk})


@csrf_exempt
def auth_login(request):
    if request.method != 'POST':
        return _err('POST only.', 405)
    from .auth_security import client_ip, send_code
    d = _body(request)
    email = str(d.get('email') or '').strip().lower()
    key = f'mob:login:{client_ip(request)}:{email}'
    tries = cache.get(key, 0)
    if tries >= LOGIN_TRIES:
        return _err('Too many tries. Wait 15 minutes and try again.', 429)
    user = authenticate(request, username=email, password=str(d.get('password') or ''))
    if not user or not user.is_active:
        cache.set(key, tries + 1, 900)
        return _err('Wrong email or password.', 400)
    cache.delete(key)
    from .views_auth import _otp_on
    if not _otp_on():
        return _issue(request, user, _device_fields(d))
    import secrets
    ticket = secrets.token_urlsafe(24)
    ok, msg = send_code(user.email or user.username, 'login', user=user, ip=client_ip(request),
                        business_name=getattr(getattr(user, 'business', None), 'business_name', ''))
    if not ok and 'wait' not in msg:
        return _err(msg)
    cache.set(f'mob:ticket:{ticket}', {'user': user.pk, 'email': user.email or user.username, 'fields': _device_fields(d),
                                       'tries': 0}, TICKET_TTL)
    return JsonResponse({'ok': True, 'otp': True, 'ticket': ticket,
                         'message': msg if ok else 'A code was sent a moment ago — check your inbox.',
                         'email': _mask(user.email or user.username)})


def _mask(email):
    name, _, dom = email.partition('@')
    return (name[:2] + '•' * max(1, len(name) - 2) + '@' + dom) if dom else email


@csrf_exempt
def auth_verify(request):
    if request.method != 'POST':
        return _err('POST only.', 405)
    from django.contrib.auth.models import User
    from .auth_security import check_code
    d = _body(request)
    k = f'mob:ticket:{str(d.get("ticket") or "")[:64]}'
    t = cache.get(k)
    if not t:
        return _err('This sign-in has expired. Start again.', 400, restart=True)
    if t['tries'] >= 6:
        cache.delete(k)
        return _err('Too many wrong codes. Start again.', 400, restart=True)
    ok, msg = check_code(t['email'], 'login', str(d.get('code') or '').strip())
    if not ok:
        t['tries'] += 1
        cache.set(k, t, TICKET_TTL)
        return _err(msg)
    cache.delete(k)
    user = User.objects.filter(pk=t['user'], is_active=True).first()
    if not user:
        return _err('This account is not active.', 400, restart=True)
    return _issue(request, user, t['fields'])


@csrf_exempt
def auth_resend(request):
    from .auth_security import client_ip, send_code
    d = _body(request)
    t = cache.get(f'mob:ticket:{str(d.get("ticket") or "")[:64]}')
    if not t:
        return _err('This sign-in has expired. Start again.', 400, restart=True)
    ok, msg = send_code(t['email'], 'login', ip=client_ip(request))
    return JsonResponse({'ok': ok, 'message': msg})


@_signed_in
def auth_logout(request):
    dev = getattr(request, 'tt_mobile', None)
    if dev is not None:
        request.session.flush()                # this request's own session: ended here, so Django does not re-save it
        mobile_auth.revoke(dev)
    return JsonResponse({'ok': True})


# ─────────────────────────────── who am I ───────────────────────────────

@_signed_in
def me(request):
    from . import chat
    from .permissions import ROLES
    from .team import accessible_businesses
    user, b = request.user, _business(request)
    perms = _perms(request)
    role = getattr(request, 'tt_role', None)
    out = {'ok': True, 'api': API_VERSION,
           'user': {'id': user.pk, 'name': chat.display_name(user), 'first': user.first_name or chat.display_name(user).split(' ')[0],
                    'initials': chat.initials(user), 'email': user.email},
           'businesses': [{'id': x.pk, 'name': x.business_name, 'role': ROLES.get(m.role, ('Staff',))[0] if m else 'Owner'}
                          for x, m in accessible_businesses(user)],
           'business': None}
    if b is None:
        return JsonResponse(out)
    from . import portal_call
    answers = any(u.pk == user.pk for u in portal_call.handlers(b))
    out['business'] = {
        'id': b.pk, 'name': b.business_name, 'brand': b.brand_color or '#1769e0', 'currency': b.currency or 'D',
        'logo': b.logo_data or '', 'phone': b.support_phone or b.phone,
        'role': ROLES.get(role, ('Staff',))[0] if role in ROLES else 'Owner', 'role_key': role or 'owner',
        'perms': sorted(perms),
    }
    out['can'] = {
        'sales': 'sales.daily' in perms and _feature(b, 'daily_sales'),
        'complaints': bool({'vouchers.view', 'vouchers.support'} & perms) and _feature(b, 'customer_reports'),
        'fit': _feature(b, 'fit'),
        'chat': True,
        'customer_calls': answers,                   # the owner picked this person to answer calls from the Wi-Fi pages
        'money': bool({'finance.view', 'finance.agents', 'agents.view'} & perms),
        # vouchers (core/views_mobile_vouchers.py): the same four rights as on the website
        'vouchers': 'vouchers.view' in perms,
        'vouchers_create': 'vouchers.create' in perms,
        'vouchers_support': 'vouchers.support' in perms,
        'vouchers_manage': 'vouchers.manage' in perms,
        # routers (core/views_mobile_routers.py): each MikroTik on its own card; sync, test, kick, reboot, move
        'routers': bool({'network.manage', 'vouchers.support'} & perms),
        'routers_manage': 'network.manage' in perms,
    }
    from .mobile_push import configured
    out['push'] = configured()
    dev = getattr(request, 'tt_mobile', None)
    if dev is not None:              # the app re-sends its push address when TapTap has lost it (self-healing)
        out['push_registered'] = bool(dev.push_token)
    return JsonResponse(out)


@_signed_in
def business(request):
    """Switch the phone to another business this person may open (same rule as the website's switcher)."""
    from .team import ACTIVE_BUSINESS_KEY, business_access_for_user
    bid = _body(request).get('id')
    b, _m = business_access_for_user(request.user, bid)
    if b is None:
        return _err('You do not have access to that business.', 403)
    request.session[ACTIVE_BUSINESS_KEY] = b.pk
    request.session.modified = True
    return JsonResponse({'ok': True})


@_signed_in
def push(request):
    """Register this phone's push token (Firebase Cloud Messaging) for calls, messages and customer reports.

    The app also says which Firebase project it belongs to (`project`) and whether the phone lets it show
    notifications (`perm`): TapTap answers whether that project matches the server's key, so a mismatch shows up
    on the phone at once instead of every push silently failing."""
    from . import mobile_push
    from .models_mobile import MobileDevice
    dev = getattr(request, 'tt_mobile', None)
    if dev is None:
        return _err('Only the mobile app can register for notifications.', 400)
    d = _body(request)
    tok = str(d.get('token') or '').strip()[:300]
    if tok:
        MobileDevice.objects.filter(push_token=tok).exclude(pk=dev.pk).update(push_token='')   # one phone, one owner
    dev = MobileDevice.objects.get(pk=dev.pk)
    fields = {'push_token': tok}
    project = str(d.get('project') or '').strip()[:100]
    if project:
        fields['push_project'] = project
    perm = str(d.get('perm') or '').strip()[:12]
    if perm:
        fields['push_health'] = {**(dev.push_health or {}), 'perm': perm, 'perm_at': timezone.now().isoformat()}
    MobileDevice.objects.filter(pk=dev.pk).update(**fields)
    dev.push_project = fields.get('push_project', dev.push_project)
    return JsonResponse({'ok': True, 'server': mobile_push.configured(), 'server_project': mobile_push.server_project(),
                         'project_match': mobile_push.project_check(dev)})


def _kinds_for(request):
    """The notification kinds that can reach this person (complaints only for people who see them, and so on)."""
    from .mobile_push import KINDS
    b, perms = _business(request), _perms(request)
    show = {'calls', 'chat', 'alerts'}
    if b is not None:
        from . import portal_call
        if {'vouchers.view', 'vouchers.support'} & perms and _feature(b, 'customer_reports'):
            show.add('complaints')
        if any(u.pk == request.user.pk for u in portal_call.handlers(b)):
            show.add('missed_calls')
        if 'vouchers.manage' in perms:
            show.add('vouchers')
    return [(k, v) for k, v in KINDS.items() if k in show]


@_signed_in
def notifications(request):
    """This phone's notification settings: which kinds it gets, whether TapTap can send at all, and a test."""
    from . import mobile_push
    from .models_mobile import MobileDevice
    dev = getattr(request, 'tt_mobile', None)
    if dev is None:
        return _err('Only the mobile app has notification settings.', 400)
    dev = MobileDevice.objects.get(pk=dev.pk)
    if request.method == 'POST':
        d = _body(request)
        if d.get('test'):
            if not mobile_push.configured():
                return _err('TapTap cannot send notifications yet: the server has no Firebase key (FCM_SERVICE_ACCOUNT).',
                            delivery={'ok': False, 'code': 'NO_KEY'})
            if not dev.push_token:
                return _err('This phone has not given TapTap a notification address yet. Allow notifications, then open the app again.',
                            delivery={'ok': False, 'code': 'NO_TOKEN'})
            v = mobile_push.test(dev, nonce=str(d.get('nonce') or '')[:40])     # sent now: Google's own answer
            delivery = {k: v.get(k) for k in ('ok', 'code', 'reason', 'fix')}
            if not v['ok']:
                return _err(f"{v['reason']} {v['fix']}".strip(), delivery=delivery)
            return JsonResponse({'ok': True, 'message': 'Google accepted it — it should pop up within a few seconds.',
                                 'delivery': delivery})
        prefs = d.get('prefs')
        if isinstance(prefs, dict):
            allowed = dict(_kinds_for(request))
            dev.notify = {**(dev.notify or {}), **{k: bool(v) for k, v in prefs.items() if k in allowed}}
            dev.save(update_fields=['notify'])
    dev.refresh_from_db(fields=['push_token', 'push_project', 'push_health'])
    h = dev.push_health or {}
    return JsonResponse({'ok': True, 'server': mobile_push.configured(), 'registered': bool(dev.push_token),
                         'server_project': mobile_push.server_project(), 'app_project': dev.push_project,
                         'project_match': mobile_push.project_check(dev),
                         # Google's last word on a push to this phone (None before the first one)
                         'last': {k: h.get(k) for k in ('at', 'ok', 'code', 'reason', 'fix', 'last_ok')} if h.get('at') else None,
                         'kinds': [{'key': k, 'label': label, 'text': text, 'on': dev.wants(k)} for k, (label, text, _ch) in _kinds_for(request)]})


# ─────────────────────────────── home: the day at a glance ───────────────────────────────

@_signed_in
def home(request):
    from . import chat, chat_call
    from .models_chat import ChatMessage
    b = _business(request)
    if b is None:
        return _err('This login is not linked to a TapTap business.', 403)
    perms = _perms(request)
    out = {'ok': True, 'greeting': _greeting(), 'now': timezone.localtime().isoformat()}
    if 'sales.daily' in perms and _feature(b, 'daily_sales'):
        from . import sales_daily as sd
        ctx = sd.build(b, {}, perms)
        out['sales'] = {'total': ctx['total'], 'count': ctx['count'], 'change': ctx['change'], 'avg': ctx['avg'], 'net': ctx['net'],
                        'commission': ctx['commission'], 'fp': ctx['live_fp'], 'week': ctx['week'], 'hours': ctx['hours'],
                        'busiest': ctx['busiest'], 'plans': ctx['by_plan'][:5], 'methods': ctx['by_method'][:4]}
    pulse = {'unread': 0, 'complaints': None, 'ringing': len(chat_call.rings_for(request.user))}
    try:
        threads = list(chat.accessible_threads(request.user, b))
        pulse['unread'] = sum(n for n, muted in chat.unread_map(request.user, threads).values() if not muted)
    except Exception:
        pass
    if {'vouchers.view', 'vouchers.support'} & perms and _feature(b, 'customer_reports'):
        from .models_voucher_reports import VoucherReport
        from .report_cases import norm_code
        pulse['complaints'] = len({norm_code(c) for c in VoucherReport.objects.filter(business=b, status='open').values_list('code', flat=True)})
    out['pulse'] = pulse
    out['calls'] = _customer_calls(b, limit=4, today=True)
    return JsonResponse(clean(out))


def _greeting():
    h = timezone.localtime().hour
    return 'Good morning' if h < 12 else ('Good afternoon' if h < 17 else 'Good evening')


def _customer_calls(b, limit=30, today=False):
    """Calls from the Wi-Fi pages, from the notes core/portal_call.py leaves in the team chat."""
    from . import chat
    from .models_chat import ChatMessage
    qs = ChatMessage.objects.filter(thread=chat.team_thread(b), system=True, body__startswith='📶')
    if today:
        start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
        qs = qs.filter(created_at__gte=start)
    rows = []
    import re
    for m in qs.order_by('-id')[:limit]:
        phone = re.search(r'·\s*([+\d][\d\s-]{4,})', m.body)
        rows.append({'id': m.pk, 'text': m.body, 'at': clean(m.created_at), 'missed': m.body.startswith('📶 Missed'),
                     'phone': phone.group(1).strip() if phone else ''})
    return rows


@_signed_in
def customer_calls(request):
    b = _business(request)
    if b is None:
        return _err('No business.', 403)
    return JsonResponse({'ok': True, 'calls': _customer_calls(b)})


# ─────────────────────────────── daily sales ───────────────────────────────

@_signed_in
def sales(request):
    """One day of sales — the website's Daily sales page as data (core/sales_daily.py)."""
    from . import sales_daily as sd
    b = _business(request)
    perms = _perms(request)
    if b is None or 'sales.daily' not in perms or not _feature(b, 'daily_sales'):
        return _err('Your role cannot see daily sales.', 403)
    fp = sd.fingerprint(b, request.GET)
    if request.GET.get('fp') and request.GET.get('fp') == fp:
        return JsonResponse({'ok': True, 'changed': False, 'fp': fp})
    ctx = sd.build(b, request.GET, perms)
    keep = ('day', 'is_today', 'prev_day', 'next_day', 'view', 'sel_plan', 'sel_seller', 'can_money', 'total', 'count', 'discounts',
            'commission', 'net', 'change', 'avg', 'week', 'by_plan', 'by_method', 'by_seller', 'hours', 'busiest', 'plan_rows',
            'seller_rows', 'plan_detail', 'seller_detail')
    out = {k: ctx.get(k) for k in keep if k in ctx}
    if 'sales' in ctx:
        out['sales'] = list(ctx['sales'][:100])
    for detail in ('plan_detail', 'seller_detail'):
        if out.get(detail):
            out[detail] = {k: v for k, v in out[detail].items() if k != 'sales'} | {'sales': list(out[detail]['sales'][:100])}
            if detail == 'seller_detail' and out[detail].get('balance'):
                bal = out[detail]['balance']
                out[detail]['balance'] = {k: v for k, v in bal.items() if k != 'agent'}
    for k in ('by_method',):
        for r in out.get(k) or []:
            r['label'] = dict(_methods()).get(r.get('payment_method'), r.get('payment_method'))
    return JsonResponse(clean({'ok': True, 'changed': True, 'fp': ctx['live_fp'], 'currency': b.currency or 'D', **out}))


def _methods():
    from .models import PAYMENT_METHODS
    return PAYMENT_METHODS


# ─────────────────────────────── customer complaints ───────────────────────────────

def _complaint_rights(request):
    b = _business(request)
    return b is not None and bool({'vouchers.view', 'vouchers.support'} & _perms(request)) and _feature(b, 'customer_reports')


@_signed_in
def complaints(request):
    """Customer reports from the Wi-Fi pages, one case per voucher (same as the website's Customer reports)."""
    from . import report_cases, report_fix
    from .fit_actions import norm_mac
    from .models_voucher_reports import VoucherReport
    from .online_portal import _device_pages, diagnose
    if not _complaint_rights(request):
        return _err('Your role cannot see customer reports.', 403)
    b = _business(request)
    show_all = request.GET.get('show') == 'all'
    qs = VoucherReport.objects.filter(business=b).select_related('voucher', 'resolved_by', 'business')
    reports = list((qs if show_all else qs.filter(status='open'))[:200])
    perms = _perms(request)
    settings = report_fix._settings(b)
    devices = _device_pages(b, [r.mac for r in reports])
    diag = {}
    for r in reports:
        k = report_cases.norm_code(r.code)
        if k not in diag:
            diag[k] = diagnose(b, r.code)
        r.now = diag[k]
        r.device_id = devices.get(norm_mac(r.mac))
        r.mac_norm = norm_mac(r.mac) or r.mac
    out = []
    from . import chat
    for c in report_cases.build(reports):
        fix = None
        if c.status == 'open':
            fix = report_fix.plan(c.latest, c.now, macs=[r.mac for r in c.reports])
            fix['can'], fix['hint'] = report_fix.availability(fix, b, perms, settings)
        out.append({
            'id': c.id, 'code': c.code, 'status': c.status, 'count': c.count, 'heat': c.heat, 'waiting': c.waiting,
            'last_seen': c.last_seen, 'phones': c.phones, 'macs': [m['mac'] for m in c.macs],
            'allowed_devices': c.allowed_devices, 'too_many_devices': c.more_devices_than_allowed,
            'now': {'state': c.now.get('state'), 'title': c.now.get('title'), 'checks': c.now.get('checks') or []},
            'messages': [{'at': r.created_at, 'name': r.name, 'phone': r.phone, 'message': r.message, 'shown_error': r.shown_error,
                          'portal': r.portal_slug} for r in c.reports],
            'fix': {'kind': fix.get('kind'), 'label': fix.get('label') or '', 'why': fix.get('why') or '', 'can': fix['can'],
                    'hint': fix['hint']} if fix else None,
            'resolved_by': chat.display_name(c.resolved_by) if c.resolved_by else '', 'resolved_at': c.resolved_at,
            'note': c.staff_note,
        })
    open_n = len({report_cases.norm_code(x) for x in qs.filter(status='open').values_list('code', flat=True)})
    return JsonResponse(clean({'ok': True, 'cases': out, 'open': open_n, 'show_all': show_all}))


@_signed_in
def complaint_act(request, pk):
    """fix · close (with a note) · reopen — exactly what the website's buttons do."""
    from . import report_cases, report_fix
    from .models_voucher_reports import VoucherReport
    if request.method != 'POST':
        return _err('POST only.', 405)
    if not _complaint_rights(request):
        return _err('Your role cannot change customer reports.', 403)
    b = _business(request)
    r = VoucherReport.objects.select_related('business').filter(business=b, pk=pk).first()
    if r is None:
        return _err('That report is gone.', 404)
    case = report_cases.case_reports(r)
    d = _body(request)
    act = d.get('action')
    if act == 'fix':
        ok, msg = report_fix.fix(case, request.user, _perms(request))
        return JsonResponse({'ok': ok, 'message': msg}, status=200 if ok else 400)
    if act == 'reopen':
        VoucherReport.objects.filter(pk__in=[x.pk for x in case]).update(status='open', resolved_at=None, resolved_by=None)
        return JsonResponse({'ok': True, 'message': 'Reopened.'})
    if act == 'close':
        report_fix._close([x for x in case if x.status == 'open'], request.user, str(d.get('note') or '').strip()[:500])
        return JsonResponse({'ok': True, 'message': 'Closed.'})
    return _err('Choose fix, close or reopen.')


# ─────────────────────────────── signed-in phones ───────────────────────────────

@_signed_in
def devices(request):
    from .models_mobile import MobileDevice
    here = getattr(request, 'tt_mobile', None)
    if request.method == 'POST':
        d = _body(request)
        dev = MobileDevice.objects.filter(user=request.user, pk=d.get('id') or 0, revoked_at__isnull=True).first()
        if dev is None:
            return _err('That phone is already signed out.', 404)
        if here and here.pk == dev.pk:
            request.session.flush()
        mobile_auth.revoke(dev)
        return JsonResponse({'ok': True})
    rows = MobileDevice.objects.filter(user=request.user, revoked_at__isnull=True)
    return JsonResponse(clean({'ok': True, 'devices': [
        {'id': x.pk, 'name': x.name or x.get_platform_display(), 'platform': x.platform, 'version': x.app_version,
         'since': x.created_at, 'seen': x.last_seen_at, 'this': bool(here and here.pk == x.pk)} for x in rows]}))
