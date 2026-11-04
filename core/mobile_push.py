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
    test         "Send a test notification" on the app's Notifications screen

Android shows each kind in its own notification channel (made by the app, tool/configure_platforms.py),
all at high importance, so they pop up on the screen; people can still tune each one in the phone's settings.

No extra library: the OAuth token is a JWT signed with `cryptography` (already installed) and the messages go
out with urllib on a background thread, so a slow Google never slows a call or a chat message.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.request

from django.core.cache import cache

logger = logging.getLogger(__name__)
SCOPE = 'https://www.googleapis.com/auth/firebase.messaging'
_lock = threading.Lock()
_sa_cache = {}


def _service_account():
    raw = os.getenv('FCM_SERVICE_ACCOUNT', '').strip()
    if not raw:
        return None
    if raw in _sa_cache:
        return _sa_cache[raw]
    try:
        data = json.loads(raw) if raw.startswith('{') else json.load(open(raw))
        sa = data if data.get('private_key') and data.get('client_email') else None
    except Exception as exc:
        logger.warning('FCM_SERVICE_ACCOUNT unreadable: %s', exc)
        sa = None
    _sa_cache[raw] = sa
    return sa


def configured():
    return _service_account() is not None


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode()


def _access_token(sa):
    key = f'fcm:token:{sa["client_email"]}'
    tok = cache.get(key)
    if tok:
        return tok
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    now = int(time.time())
    head = _b64(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode())
    claim = _b64(json.dumps({'iss': sa['client_email'], 'scope': SCOPE, 'aud': sa.get('token_uri') or 'https://oauth2.googleapis.com/token',
                             'iat': now, 'exp': now + 3600}).encode())
    pk = serialization.load_pem_private_key(sa['private_key'].encode(), password=None)
    sig = _b64(pk.sign(f'{head}.{claim}'.encode(), padding.PKCS1v15(), hashes.SHA256()))
    body = f'grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer&assertion={head}.{claim}.{sig}'.encode()
    req = urllib.request.Request(sa.get('token_uri') or 'https://oauth2.googleapis.com/token', data=body,
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.loads(r.read())
    tok = d['access_token']
    cache.set(key, tok, max(60, int(d.get('expires_in', 3600)) - 120))
    return tok


def _send_one(sa, token, message):
    from .models_mobile import MobileDevice
    url = f'https://fcm.googleapis.com/v1/projects/{sa["project_id"]}/messages:send'
    body = json.dumps({'message': {'token': token, **message}}).encode()
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json',
                                                          'Authorization': f'Bearer {_access_token(sa)}'})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:300]
        if e.code in (404, 400) and (b'UNREGISTERED' in detail or b'registration-token-not-registered' in detail
                                     or b'INVALID_ARGUMENT' in detail and b'token' in detail):
            MobileDevice.objects.filter(push_token=token).update(push_token='')   # the app was removed: forget it
        else:
            logger.info('FCM %s: %s', e.code, detail)
    except Exception as exc:
        logger.info('FCM send failed: %s', exc)


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
    """One message to one phone (the test notification)."""
    sa = _service_account()
    if not sa or not device.push_token:
        return 0
    return _dispatch(sa, [device.push_token], message)


def _dispatch(sa, toks, message):
    if not toks:
        return 0

    def run():
        from django.db import close_old_connections
        try:
            for t in toks:
                _send_one(sa, t, message)
        finally:
            close_old_connections()
    threading.Thread(target=run, daemon=True).start()
    return len(toks)


def _data(d):
    return {k: str(v) for k, v in d.items() if v is not None}


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
    data = _data({'type': 'call', 'id': call['id'], 'from': call.get('label') or call.get('caller_name') or 'Call',
                  'initials': call.get('caller_initials') or '', 'kind': call.get('kind') or 'direct',
                  'video': '1' if call.get('video') else '0', 'at': int(call.get('at') or time.time())})
    return send([uid], {
        'data': data,
        'android': {'priority': 'HIGH', 'ttl': '40s'},
        'apns': {'headers': {'apns-priority': '10', 'apns-push-type': 'alert', 'apns-expiration': str(int(time.time()) + 40)},
                 'payload': {'aps': {'alert': {'title': ('📶 Customer call · ' if portal else 'Call · ') + data['from'],
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
    'router_offline': {'network.manage'}, 'online_high': {'network.manage'}, 'fup_slowed': {'network.manage', 'vouchers.support'},
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


def test(device):
    """The test notification from the app's Notifications screen — to this one phone only."""
    return send_to_device(device, _shown('alerts', '✅ TapTap notifications work',
                                         'This is how calls, complaints and alerts reach this phone.', {'type': 'test'}))
