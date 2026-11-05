"""Push notifications to the TapTap mobile app (Firebase Cloud Messaging, HTTP v1).

Optional: set FCM_SERVICE_ACCOUNT to the Firebase service-account JSON (the file path, or the JSON itself).
Without it nothing is sent and the app still works while it is open (it polls, like the website).

What is pushed (each phone can switch every kind off on the app's Notifications screen — KINDS below)
    call         a call is ringing for this person — data only, high priority, so the app shows the full-screen
                 incoming-call screen even when it is closed (customer calls from the Wi-Fi pages included)
    call_end     the caller hung up / someone else answered — stops that ringing screen (always sent)
    chat         a new message in a conversation (not to the sender, not to someone who has it muted)
    complaint    a customer sent a voucher report from the Wi-Fi page (to people who can see reports)
    missed_call  a customer's call from the Wi-Fi page was not answered (core/portal_call.py)
    alert        a new bell alert: business alerts (stock low, router offline, agent debt…) and tracking
                 (core/business_alerts.py, core/tracking.py) — sent when the alert asks for a pop-up
    vouchers     someone else on the team made vouchers (to the owner and people who manage vouchers)
    test         "Send a test notification" / "Check delivery" on the app's Notifications screen

Android shows each kind in its own notification channel (made by the app, tool/configure_platforms.py),
all at high importance, so they pop up on the screen; people can still tune each one in the phone's settings.

Delivery, and what happens when it fails
    * Every message goes through _post(): a fresh Google sign-in when the cached one is refused (401), and up to
      three tries with back-off when Google is busy (429 / 5xx, honouring Retry-After) or the line drops.
    * Google's answer is read properly (verdict()): a phone address is forgotten only when Google says it is dead
      (UNREGISTERED, or an invalid token) — never because of a mistake in the message itself.
    * The answer is kept per phone (MobileDevice.push_health) and shown on the app's Notifications screen, in
      `python manage.py push_doctor`, and in the log (WARNING for every failure).
    * FCM reserves some data keys ("from", "message_type", anything starting with "google"/"gcm") and refuses a
      message that uses them — _data() renames them, so that mistake can never stop a push again.

No extra library: the OAuth token is a JWT signed with `cryptography` (already installed) and the messages go
out with urllib on a background thread, so a slow Google never slows a call or a chat message. The test from the
app is sent at once instead, so the phone hears exactly what Google said.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)
SCOPE = 'https://www.googleapis.com/auth/firebase.messaging'
TOKEN_URI = 'https://oauth2.googleapis.com/token'
TRIES = 3                                     # Google busy / line dropped: try this many times in all
RESERVED_KEYS = {'from', 'message_type', 'collapse_key', 'notification'}
RESERVED_PREFIXES = ('google', 'gcm')
_sa_cache = {}


def _service_account():
    raw = os.getenv('FCM_SERVICE_ACCOUNT', '').strip()
    if not raw:
        return None
    if raw in _sa_cache:
        return _sa_cache[raw]
    try:
        if raw.startswith('{'):
            data = json.loads(raw)
        else:
            with open(raw) as f:
                data = json.load(f)
        sa = data if data.get('private_key') and data.get('client_email') and data.get('project_id') else None
        if sa is None:
            logger.warning('FCM_SERVICE_ACCOUNT is not a Firebase service-account key (private_key, client_email, project_id)')
    except Exception as exc:
        logger.warning('FCM_SERVICE_ACCOUNT unreadable: %s', exc)
        sa = None
    _sa_cache[raw] = sa
    return sa


def configured():
    return _service_account() is not None


def server_project():
    """The Firebase project of the server's key ('' when push is not set up)."""
    sa = _service_account()
    return (sa or {}).get('project_id', '')


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode()


class PushError(Exception):
    """Google could not be asked at all (the sign-in failed, or the network): carries a ready verdict."""

    def __init__(self, verdict):
        super().__init__(verdict.get('reason', 'push failed'))
        self.verdict = verdict


def _token_key(sa):
    return f'fcm:token:{sa["client_email"]}'


def _access_token(sa, fresh=False):
    """Google's OAuth token for the server key (cached ~1 hour). Raises PushError with a plain explanation."""
    key = _token_key(sa)
    if not fresh:
        tok = cache.get(key)
        if tok:
            return tok
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    uri = sa.get('token_uri') or TOKEN_URI
    now = int(time.time())
    head = _b64(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode())
    claim = _b64(json.dumps({'iss': sa['client_email'], 'scope': SCOPE, 'aud': uri, 'iat': now, 'exp': now + 3600}).encode())
    try:
        pk = serialization.load_pem_private_key(sa['private_key'].encode(), password=None)
    except Exception as exc:
        raise PushError(_v(False, 'BAD_KEY', f'The server key cannot be read ({exc}).',
                           'Download a new key: Firebase → Project settings → Service accounts → Generate new private key.'))
    sig = _b64(pk.sign(f'{head}.{claim}'.encode(), padding.PKCS1v15(), hashes.SHA256()))
    body = f'grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer&assertion={head}.{claim}.{sig}'.encode()
    req = urllib.request.Request(uri, data=body, headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise PushError(_oauth_verdict(e.code, e.read()[:600]))
    except Exception as exc:
        raise PushError(_v(False, 'NETWORK', f'The server could not reach Google to sign in ({exc}).',
                           'Check that this server can open https://oauth2.googleapis.com (firewall, DNS, internet).'))
    tok = d['access_token']
    cache.set(key, tok, max(60, int(d.get('expires_in', 3600)) - 120))
    return tok


def _oauth_verdict(status, body):
    text = body.decode('utf-8', 'replace') if isinstance(body, bytes) else str(body)
    if 'short-lived' in text or 'reasonable timeframe' in text:
        return _v(False, 'CLOCK', 'Google refused the server key because this server\'s clock is wrong.',
                  'Set the server time right (timedatectl set-ntp true), then try again.', detail=text[:300])
    if 'invalid_grant' in text or 'Signature' in text:
        return _v(False, 'KEY_REVOKED', 'Google no longer accepts the server key (it was deleted or replaced in Firebase).',
                  'Make a new key in Firebase → Project settings → Service accounts, put it in FCM_SERVICE_ACCOUNT and restart.',
                  detail=text[:300])
    return _v(False, 'OAUTH', f'Google refused the server\'s sign-in (HTTP {status}).',
              'Check FCM_SERVICE_ACCOUNT: it must be the service-account key of your Firebase project.', detail=text[:300])


def _v(ok, code='', reason='', fix='', drop=False, detail=''):
    return {'ok': ok, 'code': code, 'reason': reason, 'fix': fix, 'drop': drop, 'detail': detail}


def verdict(status, body):
    """Google's answer to one message, in plain words: ok, a code, why, what to do, and whether the phone's push
    address is dead (drop=True — the only case where TapTap forgets it)."""
    if 200 <= status < 300:
        return _v(True, 'OK', 'Google accepted it.')
    text = body.decode('utf-8', 'replace') if isinstance(body, bytes) else str(body or '')
    try:
        err = (json.loads(text) or {}).get('error') or {}
    except ValueError:
        err = {}
    msg = str(err.get('message') or text[:200])
    code, fields = '', []
    for d in err.get('details') or []:
        if 'FcmError' in str(d.get('@type', '')):
            code = d.get('errorCode') or code
        fields += [str(f.get('field', '')) for f in d.get('fieldViolations') or []]
    code = code or str(err.get('status') or f'HTTP_{status}')
    if code == 'UNREGISTERED':
        return _v(False, code, 'This phone\'s push address has expired (the app was removed, reinstalled or its data cleared).',
                  'Open TapTap on that phone once — it registers again by itself.', drop=True, detail=msg)
    if code == 'INVALID_ARGUMENT' and (any(f.endswith('.token') or f == 'message.token' for f in fields)
                                       or 'registration token' in msg.lower()):
        return _v(False, 'BAD_TOKEN', 'Google says this phone\'s push address is not valid.',
                  'Open TapTap on that phone once — it registers again by itself.', drop=True, detail=msg)
    if code == 'SENDER_ID_MISMATCH':
        return _v(False, code, 'The app on this phone belongs to a different Firebase project than the server\'s key.',
                  'Use google-services.json (the app\'s GOOGLE_SERVICES_JSON) and FCM_SERVICE_ACCOUNT from the same Firebase '
                  'project, rebuild or restart, then open the app once.', detail=msg)
    if code == 'THIRD_PARTY_AUTH_ERROR':
        return _v(False, code, 'Apple refused it: the iPhone push key (APNs) is missing or wrong in Firebase.',
                  'Firebase → Project settings → Cloud Messaging → Apple app: upload your APNs key.', detail=msg)
    if code in ('PERMISSION_DENIED',) or status == 403:
        return _v(False, code or 'PERMISSION_DENIED', 'The server key may not send messages for this Firebase project.',
                  'In Google Cloud for this project, enable "Firebase Cloud Messaging API" and give the service account the '
                  'Firebase Admin role (or make a new key in Firebase → Service accounts).', detail=msg)
    if code in ('UNAUTHENTICATED',) or status == 401:
        return _v(False, 'UNAUTHENTICATED', 'Google did not accept the server\'s sign-in.',
                  'Check FCM_SERVICE_ACCOUNT and the server clock, then restart web and worker.', detail=msg)
    if code == 'NOT_FOUND' or status == 404:
        return _v(False, 'NOT_FOUND', 'Google does not know the Firebase project in the server key.',
                  'Check project_id in FCM_SERVICE_ACCOUNT — it must be your Firebase project.', detail=msg)
    if code == 'QUOTA_EXCEEDED' or status == 429:
        return _v(False, 'QUOTA_EXCEEDED', 'Too many pushes to this phone at once — Google asked to slow down.',
                  'Nothing to do: the next ones go through.', detail=msg)
    if status >= 500 or code in ('UNAVAILABLE', 'INTERNAL'):
        return _v(False, code or 'UNAVAILABLE', 'Google was busy and did not take it, even after trying again.',
                  'Nothing to do: try again in a minute.', detail=msg)
    if code == 'INVALID_ARGUMENT':
        return _v(False, code, f'Google refused the message itself: {msg}',
                  'This is a TapTap problem, not your phone — send this text to TapTap support.', detail=msg)
    return _v(False, code, f'Google refused it ({code}): {msg}', 'Send this text to TapTap support.', detail=msg)


def _retry_after(headers, attempt):
    try:
        s = float((headers or {}).get('Retry-After') or 0)
    except (TypeError, ValueError):
        s = 0
    return min(max(s, (1, 3, 6)[min(attempt, 2)]), 10)


def _post(sa, body, sleep=time.sleep):
    """POST one message to FCM: (http status, response bytes). A refused sign-in is renewed once; Google busy or the
    line dropping is tried again (TRIES in all). Raises PushError when Google cannot be asked at all."""
    url = f'https://fcm.googleapis.com/v1/projects/{sa["project_id"]}/messages:send'
    data = json.dumps(body).encode()
    fresh = False
    last = None
    for attempt in range(TRIES + 1):
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json',
                                                              'Authorization': f'Bearer {_access_token(sa, fresh=fresh)}'})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            status, payload, headers = e.code, e.read()[:2000], e.headers
            if status == 401 and not fresh:          # the cached sign-in expired or was revoked: one fresh try
                cache.delete(_token_key(sa))
                fresh = True
                continue
            if (status == 429 or status >= 500) and attempt < TRIES - 1:
                sleep(_retry_after(headers, attempt))
                continue
            return status, payload
        except Exception as exc:                     # timeout, connection reset, DNS…
            last = exc
            if attempt < TRIES - 1:
                sleep((1, 3, 6)[min(attempt, 2)])
                continue
            break
    raise PushError(_v(False, 'NETWORK', f'The server could not reach Google ({last}).',
                       'Check that this server can open https://fcm.googleapis.com (firewall, DNS, internet).'))


def _tok_id(token):
    return hashlib.sha1(token.encode()).hexdigest()[:10]


def record(token, v, **extra):
    """Keep Google's last word on this phone (MobileDevice.push_health); forget a dead address."""
    from .models_mobile import MobileDevice
    try:
        now = timezone.now().isoformat()
        for dev in MobileDevice.objects.filter(push_token=token).only('pk', 'push_health'):
            h = dict(dev.push_health or {})
            h.update({'at': now, 'ok': v['ok'], 'code': v.get('code', ''), 'reason': v.get('reason', ''),
                      'fix': v.get('fix', '')}, **extra)
            if v['ok']:
                h['last_ok'] = now
            fields = {'push_health': h}
            if v.get('drop'):
                fields['push_token'] = ''
            MobileDevice.objects.filter(pk=dev.pk).update(**fields)
    except Exception:
        logger.exception('push health not saved')


def deliver(sa, token, message, validate_only=False, sleep=time.sleep):
    """Send one message to one phone now and return Google's verdict (never raises)."""
    body = {'message': {'token': token, **message}}
    if validate_only:
        body['validate_only'] = True
    try:
        status, payload = _post(sa, body, sleep=sleep)
        v = verdict(status, payload)
    except PushError as e:
        v = e.verdict
    except Exception as exc:
        logger.exception('FCM send crashed')
        v = _v(False, 'ERROR', f'The server could not send it ({exc}).', 'Send this text to TapTap support.')
    kind = (message.get('data') or {}).get('type', '?')
    if v['ok']:
        logger.debug('FCM %s → …%s ok', kind, _tok_id(token))
    else:
        logger.warning('FCM %s → …%s refused: %s %s %s', kind, _tok_id(token), v['code'], v['reason'], v.get('detail', '')[:200])
    if not validate_only or v.get('drop'):
        record(token, v)
    return v


def _send_one(sa, token, message):
    return deliver(sa, token, message)


# kind → (label, what it is, Android channel). The app's Notifications screen lists these; the channels are
# the ones tool/configure_platforms.py makes on Android.
KINDS = {
    'calls': ('Calls', 'Ring this phone for team calls and customer calls, even when TapTap is closed.', ''),
    'chat': ('Chat messages', 'New messages in the team room and your direct conversations.', 'tt_chat'),
    'complaints': ('Customer complaints', 'A customer reported a voucher from your Wi-Fi page.', 'tt_complaints'),
    'missed_calls': ('Missed customer calls', 'Nobody answered a customer who called from the Wi-Fi page.', 'tt_calls_missed'),
    'alerts': ('Business alerts', 'Stock low, router offline, agents and the vouchers, batches and plans you track.', 'tt_alerts'),
    'vouchers': ('Vouchers made by the team', 'Someone else on the team generated vouchers.', 'tt_vouchers'),
}


def _tokens(user_ids, kind=None):
    """Push tokens of these users' signed-in phones that have not switched `kind` off."""
    from .models_mobile import MobileDevice
    rows = MobileDevice.objects.filter(user_id__in=list(user_ids), revoked_at__isnull=True).exclude(push_token='')
    return [d.push_token for d in rows.only('push_token', 'notify') if d.wants(kind)]


def send(user_ids, message, kind=None):
    """Queue one FCM message to every signed-in phone of these users that wants this kind (background thread)."""
    sa = _service_account()
    if not sa or not user_ids:
        return 0
    toks = _tokens(user_ids, kind)
    return _dispatch(sa, toks, message) if toks else 0


def send_to_device(device, message):
    """One message to one phone, in the background."""
    sa = _service_account()
    if not sa or not device.push_token:
        return 0
    return _dispatch(sa, [device.push_token], message)


def _dispatch(sa, toks, message):
    """Send in a background thread. One phone failing (or Google refusing the sign-in) never stops the others."""
    if not toks:
        return 0

    def run():
        from django.db import close_old_connections
        try:
            for t in toks:
                try:
                    _send_one(sa, t, message)
                except Exception:
                    logger.exception('FCM push to …%s', _tok_id(t))
        finally:
            close_old_connections()
    threading.Thread(target=run, daemon=True, name='fcm-push').start()
    return len(toks)


def _data(d):
    """FCM data: strings only, and never a key FCM keeps for itself ("from", "google.*"…) — Google refuses the
    whole message for one of those, which is how calls stopped ringing closed phones before."""
    out = {}
    for k, v in d.items():
        if v is None:
            continue
        k = str(k)
        if k in RESERVED_KEYS or k.lower().startswith(RESERVED_PREFIXES):
            k = f'tt_{k}'
        out[k] = str(v)
    return out


def _shown(kind, title, body, data, tag=None, thread=None):
    """A notification people see: high priority, its own Android channel (pops up), sound on both platforms."""
    android = {'channel_id': KINDS[kind][2], 'notification_priority': 'PRIORITY_HIGH', 'default_sound': True}
    if tag:
        android['tag'] = tag
    aps = {'sound': 'default'}
    if thread:
        aps['thread-id'] = thread
    return {'notification': {'title': title[:120], 'body': body[:180]}, 'data': _data(data),
            'android': {'priority': 'HIGH', 'notification': android}, 'apns': {'payload': {'aps': aps}}}


# ─────────────────────────────── calls, chat, complaints ───────────────────────────────

def call_ringing(uid, call):
    """Wake the phone with the incoming-call screen (data only: the app draws it, also when closed)."""
    portal = call.get('kind') == 'portal'
    # "caller", not "from": FCM keeps "from" for itself and refuses a message that uses it
    data = _data({'type': 'call', 'id': call['id'], 'caller': call.get('label') or call.get('caller_name') or 'Call',
                  'initials': call.get('caller_initials') or '', 'kind': call.get('kind') or 'direct',
                  'video': '1' if call.get('video') else '0', 'at': int(call.get('at') or time.time())})
    return send([uid], {
        'data': data,
        'android': {'priority': 'HIGH', 'ttl': '40s'},
        'apns': {'headers': {'apns-priority': '10', 'apns-push-type': 'alert', 'apns-expiration': str(int(time.time()) + 40)},
                 'payload': {'aps': {'alert': {'title': ('📶 Customer call · ' if portal else 'Call · ') + data['caller'],
                                               'body': 'Tap to answer in TapTap'},
                                     'sound': 'default', 'content-available': 1, 'category': 'TT_CALL'}}},
    }, kind='calls')


def call_ended(uids, call_id):
    return send(uids, {'data': _data({'type': 'call_end', 'id': call_id}), 'android': {'priority': 'HIGH', 'ttl': '60s'},
                       'apns': {'headers': {'apns-priority': '5', 'apns-push-type': 'background'},
                                'payload': {'aps': {'content-available': 1}}}})


def chat_message(message):
    """A new chat message: everyone in the conversation except the sender and those who muted it."""
    from . import chat
    from .models_chat import ChatRead
    t = message.thread
    sender_id = message.sender_id
    people = [u.pk for u in chat.participants(t) if u and u.pk != sender_id]
    if not people:
        return 0
    muted = set(ChatRead.objects.filter(thread=t, user_id__in=people, muted=True).values_list('user_id', flat=True))
    people = [p for p in people if p not in muted]
    title = (chat.display_name(message.sender) if message.sender else 'TapTap')
    if t.kind == 'team':
        title += ' · Team'
    body = chat.preview_text(message)[:180]
    return send(people, _shown('chat', title, body, {'type': 'chat', 'thread': t.pk, 'message': message.pk},
                               tag=f'chat-{t.pk}', thread=f'chat-{t.pk}'), kind='chat')


def complaint(report):
    """A customer sent a voucher report: tell the people who can see customer reports."""
    from . import chat
    from .models_team import TeamMember
    b = report.business
    who = [b.user_id]
    for m in TeamMember.objects.filter(business=b, is_active=True):
        if {'vouchers.view', 'vouchers.support'} & set(m.permissions or ()):
            who.append(m.user_id)
    name = report.name or 'A customer'
    return send(set(who), _shown('complaints', f'⚠️ {name} reported voucher {report.code}',
                                 report.message or report.shown_error or 'Their voucher does not work.',
                                 {'type': 'complaint', 'id': report.pk}), kind='complaints')


def missed_call(user_ids, text, phone, name=''):
    """Nobody answered a customer who called from the Wi-Fi page (core/portal_call.py)."""
    return send(user_ids, _shown('missed_calls', '📶 Missed customer call', text,
                                 {'type': 'missed_call', 'phone': phone, 'name': name}), kind='missed_calls')


# ─────────────────────────────── bell alerts → phones ───────────────────────────────

# Who hears about a business alert: the people whose role covers it (owners always do).
ALERT_PERMS = {
    'stock_low': {'vouchers.view', 'vouchers.create'}, 'stock_restocked': {'vouchers.view', 'vouchers.create'},
    'members_expiring': {'vouchers.view', 'vouchers.support'},
    'no_sales': {'sales.daily', 'reports.view'}, 'daily_revenue': {'sales.daily', 'reports.view'},
    'router_offline': {'network.manage'}, 'router_schedule': {'network.manage'}, 'online_high': {'network.manage'}, 'fup_slowed': {'network.manage', 'vouchers.support'},
    'agent_debt': {'finance.agents', 'agents.view', 'finance.collect'}, 'agent_stock_low': {'finance.agents', 'agents.view', 'vouchers.create'},
    'agent_collection_due': {'finance.agents', 'finance.collect'}, 'agent_collected': {'finance.agents', 'finance.collect'},
}


def _alert_people(a):
    from .models_team import TeamMember
    if a.user_id:                                   # tracking: only the person who tracks the item
        return {a.user_id}
    b = a.business
    need = ALERT_PERMS.get(a.kind)
    who = {b.user_id}
    for m in TeamMember.objects.filter(business=b, is_active=True):
        if need is None or need & set(m.permissions or ()):
            who.add(m.user_id)
    return who


def alert(a):
    """A new bell alert (core/models_events.EventAlert). Sent when it asks for a pop-up — quiet hours and rules
    without a pop-up stay quiet on the phone too."""
    if not a.desktop:
        return 0
    icon = {'danger': '🚨', 'warning': '⚠️'}.get(a.level, '🔔')
    title = a.title if a.title[:1] and not a.title[0].isalnum() else f'{icon} {a.title}'
    return send(_alert_people(a), _shown('alerts', title, a.body or a.business.business_name,
                                         {'type': 'alert', 'id': a.pk, 'link': a.link, 'kind': a.kind}, tag=f'alert-{a.pk}'),
                kind='alerts')


def _alert_saved(sender, instance, created, **kwargs):
    if not created or not configured():
        return
    from django.db import transaction
    pk = instance.pk

    def go():
        from .models_events import EventAlert
        a = EventAlert.objects.select_related('business').filter(pk=pk).first()
        if a is not None:
            try:
                alert(a)
            except Exception:
                logger.exception('alert push %s', pk)
    transaction.on_commit(go)


def install():
    """Every new bell alert also reaches the phones (core/apps.py)."""
    from django.db.models.signals import post_save
    from .models_events import EventAlert
    post_save.connect(_alert_saved, sender=EventAlert, dispatch_uid='mobile_push_alert')


# ─────────────────────────────── vouchers made by the team ───────────────────────────────

def vouchers_made(business, actor, title, body, batch=None, voucher=None):
    """Someone made vouchers: tell the owner and the people who manage vouchers — not the person who did it."""
    if not configured():
        return 0
    from .models_team import TeamMember
    who = {business.user_id} | {m.user_id for m in TeamMember.objects.filter(business=business, is_active=True)
                                if 'vouchers.manage' in set(m.permissions or ())}
    me = getattr(actor, 'pk', None)
    who.discard(me)
    if not who:
        return 0
    name = ''
    if actor is not None and getattr(actor, 'is_authenticated', False):
        from . import chat
        name = chat.display_name(actor)
    return send(who, _shown('vouchers', f'🎟️ {name or "Someone"} made {title}', body,
                            {'type': 'vouchers', 'batch': getattr(batch, 'pk', None), 'voucher': getattr(voucher, 'pk', None)}),
                kind='vouchers')


def test_message(nonce=''):
    return _shown('alerts', '✅ TapTap notifications work', 'This is how calls, complaints and alerts reach this phone.',
                  {'type': 'test', 'nonce': nonce or None, 'sent': f'{time.time():.3f}'})


def test(device, nonce=''):
    """The test from the app's Notifications screen — to this one phone, sent now, so the phone hears exactly what
    Google said. With a nonce the app recognises it when it comes back (the round trip on "Check delivery")."""
    sa = _service_account()
    if not sa:
        return _v(False, 'NO_KEY', 'The server has no Firebase key.', 'Add FCM_SERVICE_ACCOUNT to .env, then restart web and worker.')
    if not device.push_token:
        return _v(False, 'NO_TOKEN', 'This phone has not given TapTap its push address yet.',
                  'Allow notifications, then open the app again.')
    return deliver(sa, device.push_token, test_message(nonce))


def project_check(device):
    """Does the app on this phone belong to the server key's Firebase project? True / False / None (not known)."""
    mine, theirs = server_project(), (device.push_project or '').strip()
    if not mine or not theirs:
        return None
    return mine == theirs
