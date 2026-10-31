"""Ready to browse — a customer who types a valid code gets internet, not a router error.

What people saw
---------------
"The voucher shows on TapTap with the phone's MAC address, but the customer still cannot browse."

The login page asks TapTap first (``/p/<slug>/state/`` on router-served pages, ``/p/<slug>/check/`` on hosted
pages). TapTap checked the code against its OWN database, locked the phone to the voucher (that is the MAC shown
on TapTap) and said "go ahead". The page then logged in on the router — and the router refused, because it did
not have that voucher yet (still Pending, a TapTap Link batch waiting for the next check-in, a push that failed
while the router was briefly unreachable), or had it switched off, or its time counter used up, or an old session
in the way. TapTap looked "connected"; the customer had no internet.

What this module does
---------------------
``make_ready()``    Login guarantee. Before the page logs in, TapTap makes sure the router really has the voucher,
                    switched on: Direct API / Tunnel routers get it at once (the page waits ~2 s at most);
                    TapTap Link routers get it as a top-priority command and check in faster (turbo) while the
                    customer waits. Vouchers that are already on the router cost two database reads, nothing more.
``/p/<slug>/ready/``  The page polls this and logs in the moment the router has the voucher — not after a fixed
                    countdown. Old pages already on routers still work: they wait the ``wait`` seconds sent back.
``/p/<slug>/heal/``  Self-healing login. If the router still refuses, the page sends TapTap the router's own error
                    ("invalid username or password", "no more sessions are allowed", "invalid mac address", "has
                    reached uptime limit"…). TapTap fixes that cause — re-sends the exact user, clears a stale
                    session or an old MAC pin — and the page tries once more by itself.
``sweep()``         Vouchers that could not be sent (router briefly unreachable) used to wait for the next full
                    sync, which on Direct API routers only runs when someone presses Sync. They are now retried
                    every few seconds, per router, with backoff.
``refresh_portals()``  Router-served login pages carry the page code inside them: routers with an outdated copy
                    get the current one automatically (needs SITE_URL).

Nothing here weakens a refusal: frozen, expired, disabled, wrong-device and wrong-code answers are decided before
this runs and are returned unchanged. Tokens handed to the page are signed and short-lived, so the ready/heal
addresses cannot be used to test codes.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from functools import wraps

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger('taptap.ready')

SALT = 'taptap-ready'
TOKEN_AGE = 20 * 60                 # a login page stays open a while; the token only says "this voucher"
API_WAIT = float(getattr(settings, 'READY_API_WAIT', 2.0))   # the router page gives TapTap 4 s in total
MAX_WAIT = 45                       # the page never waits longer before trying the router anyway
CONFIRM_TTL = 6 * 3600
LINK_MIRROR_TRUST = 40 * 60         # a Link inventory older than this says nothing about the router now
NOTE_EVERY = 600                    # one history note per voucher per 10 minutes at most
PENDING_STATES = ('Pending', 'Error', '')
SWEEP_BATCH = 100
RUN_INLINE = False                  # tests / no worker: push in the request thread instead of a helper thread
ANY_MAC = '00:00:00:00:00:00'


# ─────────────────────────────── small helpers ───────────────────────────────

def _via(router):
    from .voucher_history import channel
    return channel(router)


def _ts(dt):
    return dt.timestamp() if dt else 0.0


def confirm(ids):
    """The router confirmed these vouchers in their current state (push OK / Link ack OK)."""
    ids = [i for i in ids or [] if i]
    if ids:
        now = time.time()
        try:
            cache.set_many({f'tt:ready:ok:{pk}': now for pk in ids}, CONFIRM_TTL)
        except Exception:
            pass


def unconfirm(ids):
    ids = [i for i in ids or [] if i]
    if ids:
        try:
            cache.delete_many([f'tt:ready:ok:{pk}' for pk in ids])
        except Exception:
            pass


def _confirmed_at(pk):
    try:
        return float(cache.get(f'tt:ready:ok:{pk}') or 0)
    except Exception:
        return 0.0


def token_for(voucher):
    return signing.dumps({'v': voucher.pk, 'b': voucher.business_id}, salt=SALT, compress=True)


def voucher_from_token(token, page):
    from .models import Voucher
    try:
        data = signing.loads(str(token or ''), salt=SALT, max_age=TOKEN_AGE)
    except signing.BadSignature:
        return None
    if not page or data.get('b') != page.business_id:
        return None
    return Voucher.objects.select_related('router', 'business').filter(pk=data.get('v'), business_id=page.business_id).first()


def _mirror(v):
    from .models import RouterHotspotUser
    return (RouterHotspotUser.objects.filter(router_id=v.router_id, username=v.code).first()
            or RouterHotspotUser.objects.filter(router_id=v.router_id, username__iexact=v.code).first())


def _secs(text):
    from .router_time import secs
    return secs(text)


def _poll_seconds(router):
    try:
        return max(10, int(router.agent.poll_seconds or 10))
    except Exception:
        return 10


def _note(v, text, via=''):
    """One history line so staff can see what happened at login (rate-limited per voucher)."""
    if not cache.add(f'tt:ready:note:{v.pk}', 1, NOTE_EVERY):
        return
    try:
        from .voucher_history import record
        record(v, 'note', source='auto', via=via, reason='Ready to browse', text=text[:250])
    except Exception:
        logger.debug('ready note for %s', v.code, exc_info=True)


# ─────────────────────────────── does the router have it? ───────────────────────────────

def needs_send(v, via=None):
    """(send?, why, state) — state: skip | ok | queued | missing | disabled | limit."""
    from .voucher_fastlane import can_be_online
    if not v.router_id or v.source != 'taptap' or v.deleted_at:
        return False, 'the router decides (not a TapTap voucher on a router)', 'skip'
    if not can_be_online(v):
        return False, 'not allowed online right now', 'skip'
    via = via or _via(v.router)
    state = v.mikrotik_sync_status or 'Pending'
    if state == 'Queued':
        return True, 'on its way to the router', 'queued'
    if state != 'Synced':
        return True, 'not on the router yet', 'missing'
    m = _mirror(v)
    if m is None:
        return False, 'on the router', 'ok'
    seen = max(_ts(m.last_seen_at), _ts(m.updated_at))
    ok_at = _confirmed_at(v.pk)
    if ok_at and seen <= ok_at:
        return False, 'on the router (confirmed)', 'ok'           # the router's own answer is newer than the copy
    if via == 'TapTap Link' and seen < time.time() - LINK_MIRROR_TRUST:
        return False, 'on the router', 'ok'                       # an old Link inventory: it says nothing now
    if not m.is_present:
        return True, 'missing on the router', 'missing'
    if m.disabled:
        return True, 'switched off on the router', 'disabled'
    used, limit = _secs(m.uptime), _secs(m.limit_uptime)
    if limit and used >= limit:
        return True, "the router's time counter is used up", 'limit'
    return False, 'on the router', 'ok'


def make_ready(v, force=False, unpin=False):
    """Make sure the router has this voucher, switched on, before the customer logs in.
    ``force``: send it even if TapTap believes the router has it (the router just refused it).
    ``unpin``: also clear an old MAC pin on the router user (Direct API / Tunnel; Link sends its own command).
    Returns {'ready': bool, 'wait': seconds, 'why': text, 'via': channel}."""
    need, why, state = needs_send(v)
    via = _via(v.router) if v.router_id else 'TapTap only'
    if state == 'skip' or (not need and not force):
        return {'ready': True, 'wait': 0, 'why': why, 'via': via}
    if force and not need:
        why = 'the router refused it'
    if via == 'TapTap Link':
        return _ready_link(v, why, state, force)
    return _ready_api(v, why, via, unpin=unpin)


def _ready_link(v, why, state, force):
    from .linklive import link_state
    from .voucher_fastlane import release_stuck, sync_now
    from . import agent as link
    router = v.router
    online, reason = link_state(router)
    if not online:
        # The router is not checking in (no internet itself?). Making the customer wait cannot help.
        return {'ready': True, 'wait': 0, 'why': f'{why}; {reason}', 'via': 'TapTap Link'}
    if state == 'queued' and not force:
        try:
            release_stuck(router, force=True)          # a batch lost from the queue goes back to Pending
        except Exception:
            logger.debug('release stuck on %s', router, exc_info=True)
        v.refresh_from_db(fields=['mikrotik_sync_status'])
    if force or v.mikrotik_sync_status != 'Queued':
        if v.is_member:
            cache.delete(f'tt:mpush:{v.pk}')           # member sender: never skip a re-send the router needs
        sync_now([v.pk], force=True)
    link.mark_urgent(router)
    return {'ready': False, 'wait': min(MAX_WAIT, _poll_seconds(router) + 8), 'why': why, 'via': 'TapTap Link'}


def push_exact(pk, unpin=False):
    """Direct API / Tunnel: write this one voucher exactly as TapTap wants it. True when the router took it."""
    from .models import Voucher
    v = Voucher.objects.select_related('router', 'business').get(pk=pk)
    if not v.router_id:
        return False
    if unpin:
        _api_unpin(v)
    if v.is_member:
        from . import views_agents
        cache.delete(f'tt:mpush:{v.pk}')
        ok = views_agents.push_one(v) is True
    else:
        from .voucher_push import push_now
        sent, _failed = push_now(v.router, [v.pk])
        ok = sent == 1
    if ok:
        confirm([v.pk])
        try:
            from .voucher_fastlane import claim
            claim(v)                                    # the fast lane need not send the same state again
        except Exception:
            pass
    return ok


def _ready_api(v, why, via, unpin=False):
    busy = f'tt:ready:busy:{v.pk}'
    if not cache.add(busy, 1, 30):
        return {'ready': False, 'wait': 6, 'why': f'{why} — being sent', 'via': via}
    result = {}
    inline = RUN_INLINE or getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False)

    def run():
        try:
            result['ok'] = push_exact(v.pk, unpin=unpin)
        except Exception as exc:                         # noqa: BLE001 — router API errors come in many types
            result['err'] = str(exc)[:200]
            logger.info('ready push of %s: %s', v.code, exc)
        finally:
            cache.delete(busy)
            if not inline:
                connection.close()

    if inline:
        run()
        finished = True
    else:
        t = threading.Thread(target=run, name=f'tt-ready-{v.pk}', daemon=True)
        t.start()
        t.join(API_WAIT)
        finished = not t.is_alive()
    if finished and result.get('ok'):
        return {'ready': True, 'wait': 0, 'why': f'{why} — sent just now', 'via': via}
    if finished:
        # TapTap cannot reach the router right now: the sweeper keeps trying; the router decides this login.
        return {'ready': True, 'wait': 0, 'why': f'{why}; router not reachable ({result.get("err") or "push failed"})', 'via': via}
    return {'ready': False, 'wait': 8, 'why': f'{why} — being sent', 'via': via}


def check_ready(v):
    """For the page's poll: is it on the router now? Keeps it moving when it is not."""
    v.refresh_from_db()
    need, why, state = needs_send(v)
    if not need or state == 'skip':
        return {'ready': True, 'wait': 0}
    if cache.add(f'tt:ready:nudge:{v.pk}', 1, 6):
        r = make_ready(v)
        return {'ready': r['ready'], 'wait': r['wait']}
    return {'ready': False, 'wait': 4}


# ─────────────────────────────── self-healing login ───────────────────────────────

def _permanent(v):
    try:
        from .models_permanent_device import PermanentVoucherDevice
        return PermanentVoucherDevice.objects.filter(binding__voucher=v).exists()
    except Exception:
        return False


def _api_unpin(v):
    from .mikrotik import MikroTikService
    svc = MikroTikService(v.router, timeout=getattr(settings, 'MIKROTIK_LIVE_TIMEOUT', 5)).connect()
    try:
        users = svc.resource('/ip/hotspot/user')
        rows = users.get(name=v.code)
        if rows:
            users.set(id=rows[0]['id'], mac_address=ANY_MAC)
    finally:
        svc.close()


def classify(error):
    t = str(error or '').lower()
    if 'radius' in t:
        return 'radius'
    if 'already authorizing' in t or 'retry later' in t:
        return 'busy'
    if 'session' in t:
        return 'sessions'
    if 'mac' in t:
        return 'mac'
    return 'user'          # invalid username or password / disabled / uptime or traffic limit / anything else


def heal(v, error, mac=''):
    """The router refused a login TapTap allowed. Fix the cause; tell the page whether to try again."""
    from .voucher_fastlane import can_be_online
    if not can_be_online(v):
        return {'retry': False, 'message': 'This voucher cannot go online right now. Ask staff for help.'}
    if not v.router_id or v.source != 'taptap':
        return {'retry': False, 'message': ''}
    kind = classify(error)
    via = _via(v.router)
    if kind == 'radius':
        return {'retry': False, 'message': ''}
    if kind == 'busy':
        return {'retry': True, 'wait': 3, 'message': 'The Wi-Fi was busy — trying again…'}
    did, wait = [], 0
    if kind == 'sessions':
        from . import device_lock
        wait = device_lock.release_stale(v, mac) or 0
        did.append('cleared an old session of this voucher')
    if kind == 'mac':
        if _permanent(v):
            return {'retry': False, 'message': 'This voucher is locked to another phone. Ask staff for help.'}
        if via == 'TapTap Link':
            try:
                from .linkops import send
                send(v.router, 'hotspot_user_mac', {'name': v.code, 'mac': ANY_MAC},
                     label=f'Login fix {v.code}: any device', minutes=30)
            except ValueError:
                pass
        did.append('removed an old device pin on the router')
    cache.delete(f'tt:ready:busy:{v.pk}')
    unconfirm([v.pk])
    r = make_ready(v, force=True, unpin=(kind == 'mac'))
    if via != 'TapTap Link' and r['ready'] and 'not reachable' in r.get('why', ''):
        return {'retry': False, 'message': 'We could not reach the Wi-Fi router. Try again in a minute.'}
    did.append('sent the voucher to the router again')
    _note(v, f'Router refused the login ("{str(error)[:80]}") — TapTap {", ".join(did)}; the page tried again', via)
    return {'retry': True, 'wait': max(int(wait or 0), int(r.get('wait') or 0), 1), 'ready': bool(r.get('ready')),
            'message': 'Fixing your connection…'}


# ─────────────────────────────── portal endpoints ───────────────────────────────

def _cors(resp):
    from .views_ads import _cors as cors
    return cors(resp)


def _client_ip(request):
    return request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', '')).split(',')[0].strip()


def _throttled(key, limit, seconds):
    try:
        if cache.add(key, 1, seconds):
            return False
        return cache.incr(key) > limit
    except Exception:
        return False


def _json(request):
    if len(request.body or b'') > 4000:
        return None
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _page(slug):
    from .models import PortalPage
    return PortalPage.objects.select_related('business').filter(slug=slug).first()


@csrf_exempt
def portal_ready(request, slug):
    """The login page polls this while the voucher travels to the router."""
    if request.method == 'OPTIONS':
        return _cors(HttpResponse(status=204))
    if request.method != 'POST' or _throttled(f'tt:ready:ip:{_client_ip(request)}', 90, 60):
        return _cors(JsonResponse({'ready': True, 'wait': 0}))       # never hold a customer back because of this
    page, data = _page(slug), _json(request)
    v = voucher_from_token((data or {}).get('t'), page) if data is not None else None
    if not v:
        return _cors(JsonResponse({'ready': True, 'wait': 0}))
    try:
        return _cors(JsonResponse(check_ready(v)))
    except Exception:
        logger.exception('ready check for %s', v.code)
        return _cors(JsonResponse({'ready': True, 'wait': 0}))


@csrf_exempt
def portal_heal(request, slug):
    """The router refused a login TapTap allowed: fix the cause, tell the page to try again."""
    if request.method == 'OPTIONS':
        return _cors(HttpResponse(status=204))
    if request.method != 'POST' or _throttled(f'tt:heal:ip:{_client_ip(request)}', 10, 60):
        return _cors(JsonResponse({'retry': False}))
    page, data = _page(slug), _json(request)
    v = voucher_from_token((data or {}).get('t'), page) if data is not None else None
    if not v or _throttled(f'tt:heal:v:{v.pk}', 4, 300):
        return _cors(JsonResponse({'retry': False}))
    try:
        out = heal(v, str(data.get('error') or '')[:200], str(data.get('mac') or '')[:32])
    except Exception:
        logger.exception('login heal for %s', v.code)
        out = {'retry': False}
    out['code'] = v.code if out.get('retry') else ''
    return _cors(JsonResponse(out))


# ─────────────────────────────── wrapping the portal checks ───────────────────────────────

def _req_code(request):
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        return ''
    return re.sub(r'[\s-]', '', str((data or {}).get('code') or '')) if isinstance(data, dict) else ''


def _augment(request, slug, resp, hosted):
    if request.method != 'POST' or resp.status_code != 200 or not resp.get('Content-Type', '').startswith('application/json'):
        return resp
    try:
        data = json.loads(resp.content or b'{}')
    except ValueError:
        return resp
    if not isinstance(data, dict):
        return resp
    allowed = data.get('success') is True if hosted else (data.get('ok') is True and not data.get('blocked'))
    if not allowed:
        return resp
    code = str(data.get('code') or '') or _req_code(request)
    page = _page(slug)
    if not page or not code:
        return resp
    from .models import Voucher
    v = Voucher.objects.select_related('router', 'business').filter(business=page.business, code__iexact=code).first()
    if not v:
        return resp
    r = make_ready(v)
    data['ready_token'] = token_for(v)
    data.setdefault('code', v.code)               # old wait replies did not carry the real code
    if not r['ready']:
        data['ready'] = False
        data['wait'] = max(int(data.get('wait') or 0), int(r['wait']))
        data['message'] = 'Activating your voucher on the Wi-Fi…'
        _note(v, f'Login check: {r["why"]} — TapTap sent it to {v.router.name} first; the page waits for the router', r.get('via', ''))
    elif 'sent just now' in r.get('why', ''):
        _note(v, f'Login check: {r["why"].replace(" — sent just now", "")} — TapTap sent it to {v.router.name} before the login', r.get('via', ''))
    resp.content = json.dumps(data)
    return resp


def _wrap(original, hosted):
    @wraps(original)
    def view(request, slug, *args, **kwargs):
        resp = original(request, slug, *args, **kwargs)
        try:
            return _augment(request, slug, resp, hosted)
        except Exception:
            logger.exception('login guarantee on %s', slug)
            return resp
    view._taptap_ready = True
    return csrf_exempt(view)


# ─────────────────────────────── retries in the background ───────────────────────────────

def sweep():
    """Send vouchers whose push failed (router briefly unreachable) — Direct API / Tunnel routers.
    TapTap Link routers pick theirs up at every check-in (agent.push_pending_vouchers)."""
    from .models import Router, Voucher
    every = max(5, int(getattr(settings, 'VOUCHER_SWEEP_SECONDS', 10)))
    if not cache.add('tt:vsweep:pass', 1, every - 1):
        return 0
    qs = (Voucher.objects.filter(source='taptap', router__isnull=False, deleted_at__isnull=True,
                                 mikrotik_sync_status__in=PENDING_STATES)
          .exclude(status__in=['expired', 'archived']))
    per_router = {}
    for pk, rid in qs.order_by('-pk').values_list('pk', 'router_id')[:5000]:
        per_router.setdefault(rid, []).append(pk)
    if not per_router:
        return 0
    queued = 0
    from .tasks import push_vouchers_task
    for router in Router.objects.filter(pk__in=list(per_router)).select_related('business'):
        try:
            if not router.business.has_access or _via(router) == 'TapTap Link':
                continue
        except Exception:
            continue
        if cache.get(f'tt:vsweep:wait:{router.pk}'):
            continue                                   # a push is running, or the router failed a moment ago
        ids = [pk for pk in per_router[router.pk]
               if not cache.get(f'tt:vfast:{pk}') and not cache.get(f'tt:vsweep:cool:{pk}')][:SWEEP_BATCH]
        if not ids:
            continue
        cache.set(f'tt:vsweep:wait:{router.pk}', 1, 60)
        try:
            push_vouchers_task.delay(router.pk, ids, True)
            queued += len(ids)
        except Exception:
            cache.delete(f'tt:vsweep:wait:{router.pk}')
            logger.exception('voucher sweep: could not queue %s voucher(s) for %s', len(ids), router)
    return queued


def after_sweep_push(router_id, ids, sent, failed):
    """Backoff bookkeeping after a sweeper push: routers that are down are tried less often, and a voucher the
    router keeps refusing does not hold its turn for ever."""
    from .models import Voucher
    wait_key, back_key = f'tt:vsweep:wait:{router_id}', f'tt:vsweep:back:{router_id}'
    if failed and not sent:
        back = min(300, int(cache.get(back_key) or 7) * 2)
        cache.set(back_key, back, 3600)
        cache.set(wait_key, 1, back)
        return
    cache.delete_many([wait_key, back_key])
    if failed:
        bad = Voucher.objects.filter(pk__in=list(ids)).exclude(mikrotik_sync_status='Synced').values_list('pk', flat=True)
        for pk in bad:
            n = int(cache.get(f'tt:vsweep:n:{pk}') or 0) + 1
            cache.set(f'tt:vsweep:n:{pk}', n, 3600)
            cache.set(f'tt:vsweep:cool:{pk}', 1, min(600, 15 * 2 ** n))


# ─────────────────────────────── router-served pages stay current ───────────────────────────────

def refresh_portals():
    """Routers whose installed login page is older than the current pages/renderer get the current one."""
    if not getattr(settings, 'SITE_URL', '') or not getattr(settings, 'PORTAL_AUTO_REFRESH', True):
        return 0
    from .models import PortalDeployment
    from .portal_deploy import deploy, version_for
    versions, n = {}, 0
    for dep in PortalDeployment.objects.filter(status='installed').select_related('router__business'):
        business = dep.router.business
        try:
            if not business.has_access:
                continue
            current = versions.get(business.pk) or versions.setdefault(business.pk, version_for(business))
        except Exception:
            continue
        if dep.version != current and cache.add(f'tt:portal:auto:{dep.router_id}', 1, 6 * 3600):
            try:
                ok, msg = deploy(dep.router)
                n += 1 if ok else 0
                logger.info('portal refresh %s: %s', dep.router, msg)
            except Exception:
                logger.exception('portal refresh %s', dep.router)
    return n


# ─────────────────────────────── install ───────────────────────────────

def install():
    from . import views_ads, views_studio
    if not getattr(views_ads.portal_state, '_taptap_ready', False):
        views_ads.portal_state = _wrap(views_ads.portal_state, hosted=False)
    if not getattr(views_studio.portal_check, '_taptap_ready', False):
        views_studio.portal_check = _wrap(views_studio.portal_check, hosted=True)
    logger.info('Ready to browse enabled: vouchers are on the router before the login, refused logins heal themselves.')
