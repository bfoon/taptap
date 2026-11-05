"""Offline-first sync for the TapTap mobile app.

The phone keeps everything it has seen in its own database (mobile/lib/data/) and shows it at once; this module
lets it find out cheaply what is new, so it only downloads what changed.

    MobileETagMiddleware   every GET the app makes to /api/mobile/v1/, /chat/t/ and /fit/state/ gets an ETag (a
                           hash of the answer). The phone sends it back as If-None-Match; when nothing changed it
                           gets an empty 304 instead of the whole answer — on a slow mobile line that is the
                           difference between instant and waiting. Only requests from the app (X-Requested-With:
                           TapTapApp) are touched, so the website behaves exactly as before.

    pulse                  /api/mobile/v1/sync/ — a few tiny "stamps" (sales today, customer reports, Wi-Fi page
                           calls, vouchers & batches, who-am-I). While the app is open it asks every few seconds
                           and pulls only the pages whose stamp moved.

Nothing here writes to the database.
"""
from __future__ import annotations

import hashlib
import json

from django.core.cache import cache
from django.http import HttpResponseNotModified, JsonResponse

APP_HEADER = 'TapTapApp'
PREFIXES = ('/api/mobile/v1/', '/chat/t/', '/fit/state/')
SKIP = ('/api/mobile/v1/auth/',)
VOLATILE = ('now',)              # top-level keys that change on every answer without meaning anything changed
MAX_BYTES = 4 * 1024 * 1024
PULSE_CACHE_SECONDS = 15         # several phones of one business asking at once share one set of queries
SYNC_VERSION = 1


# ─────────────────────────────── ETag / 304 ───────────────────────────────

def _tag(body: bytes) -> str:
    raw = body
    try:
        data = json.loads(body)
        if isinstance(data, dict) and any(k in data for k in VOLATILE):
            data = {k: v for k, v in data.items() if k not in VOLATILE}
            raw = json.dumps(data, sort_keys=True, separators=(',', ':'), default=str).encode()
    except (ValueError, TypeError, UnicodeDecodeError):
        pass
    return 'm1-' + hashlib.sha1(raw).hexdigest()[:32]


def _sent_tags(request):
    """The tags in If-None-Match, without quotes or the W/ a proxy may add when it compresses."""
    out = set()
    for part in (request.headers.get('If-None-Match') or '').split(','):
        t = part.strip()
        if t.startswith('W/'):
            t = t[2:]
        t = t.strip('"')
        if t:
            out.add(t)
    return out


def _applies(request, response) -> bool:
    if request.method != 'GET' or request.headers.get('X-Requested-With') != APP_HEADER:
        return False
    path = request.path
    if not path.startswith(PREFIXES) or path.startswith(SKIP):
        return False
    if response.status_code != 200 or getattr(response, 'streaming', False) or response.has_header('ETag'):
        return False
    return (response.get('Content-Type') or '').startswith('application/json')


def conditional(request, response):
    if not _applies(request, response):
        return response
    body = response.content
    if len(body) > MAX_BYTES:
        return response
    tag = _tag(body)
    response['ETag'] = f'"{tag}"'
    if not response.has_header('Cache-Control'):
        response['Cache-Control'] = 'private, no-cache'     # the phone may keep it, but must ask before reusing it
    if tag in _sent_tags(request):
        nm = HttpResponseNotModified()
        nm['ETag'] = response['ETag']
        nm['Cache-Control'] = response['Cache-Control']
        if response.has_header('Vary'):
            nm['Vary'] = response['Vary']
        return nm
    return response


class MobileETagMiddleware:
    """Last in MIDDLEWARE, so it sees exactly what the view answered."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            return conditional(request, response)
        except Exception:                      # never let the shortcut break an answer
            return response


# ─────────────────────────────── the pulse ───────────────────────────────

def _h(*parts) -> str:
    return hashlib.sha1('|'.join(str(p) for p in parts).encode()).hexdigest()[:16]


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None                            # unknown: the phone refreshes that page on its own timer


def _stamp_sales(b, perms, feature):
    if 'sales.daily' not in perms or not feature(b, 'daily_sales'):
        return None
    from . import sales_daily as sd
    return sd.fingerprint(b, {})


def _stamp_complaints(b, perms, feature):
    if not ({'vouchers.view', 'vouchers.support'} & perms) or not feature(b, 'customer_reports'):
        return None
    from django.db.models import Count, Max, Q
    from .models_voucher_reports import VoucherReport
    a = VoucherReport.objects.filter(business=b).aggregate(
        n=Count('id'), m=Max('id'), r=Max('resolved_at'), o=Count('id', filter=Q(status='open')))
    return _h(a['n'], a['m'], a['r'], a['o'])


def _stamp_calls(b):
    from django.db.models import Max
    from .models_chat import ChatMessage
    m = ChatMessage.objects.filter(thread__business=b, thread__kind='team', system=True,
                                   body__startswith='📶').aggregate(m=Max('id'))['m']
    return _h(m or 0)


def _stamp_vouchers(b, perms):
    if 'vouchers.view' not in perms:
        return None
    from django.db.models import Count, Max, Q
    v = b.vouchers.exclude(login_type='member').aggregate(
        n=Count('id'), m=Max('id'), s=Max('sold_at'), u=Max('used_at'), f=Count('id', filter=Q(frozen_at__isnull=False)),
        d=Count('id', filter=~Q(status='active')))
    k = b.batches.aggregate(n=Count('id'), m=Max('id'))
    return _h(v['n'], v['m'], v['s'], v['u'], v['f'], v['d'], k['n'], k['m'])


def _business_stamps(b, perms, feature):
    key = f'tt:mobile-pulse:{b.pk}:{_h(*sorted(perms))}'
    try:
        hit = cache.get(key)
    except Exception:
        hit = None
    if isinstance(hit, dict):
        return hit
    out = {
        'sales': _safe(lambda: _stamp_sales(b, perms, feature)),
        'complaints': _safe(lambda: _stamp_complaints(b, perms, feature)),
        'calls': _safe(lambda: _stamp_calls(b)),
        'vouchers': _safe(lambda: _stamp_vouchers(b, perms)),
    }
    try:
        cache.set(key, out, PULSE_CACHE_SECONDS)
    except Exception:
        pass
    return out


def pulse(request):
    """What changed since the phone last looked — stamps only, no data."""
    from .views_mobile import _business, _feature, _perms
    if not request.user.is_authenticated:
        return JsonResponse({'ok': False, 'auth': False, 'message': 'Signed out — please sign in again.'}, status=401)
    b, perms = _business(request), _perms(request)
    role = getattr(request, 'tt_role', None)
    stamps = {}
    if b is not None:
        stamps.update(_business_stamps(b, perms, _feature))
        features = [_feature(b, k) for k in ('daily_sales', 'customer_reports', 'fit')]
    else:
        features = []

    def answers():
        from . import portal_call
        return any(u.pk == request.user.pk for u in portal_call.handlers(b)) if b is not None else False

    stamps['me'] = _h(b.pk if b else 0, role, *sorted(perms), *features, _safe(answers))
    return JsonResponse({'ok': True, 'v': SYNC_VERSION, 'stamps': stamps})
