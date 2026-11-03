"""Push notifications to the TapTap mobile app (Firebase Cloud Messaging, HTTP v1).

Optional: set FCM_SERVICE_ACCOUNT to the Firebase service-account JSON (the file path, or the JSON itself).
Without it nothing is sent and the app still works while it is open (it polls, like the website).

What is pushed
    call       a call is ringing for this person — data only, high priority, so the app shows the full-screen
               incoming-call screen even when it is closed (customer calls from the Wi-Fi pages included)
    call_end   the caller hung up / someone else answered — stops that ringing screen
    chat       a new message in a conversation (not to the sender, not to someone who has it muted)
    complaint  a customer sent a voucher report from the Wi-Fi page (to people who can see reports)

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


def _tokens(user_ids):
    from .models_mobile import MobileDevice
    return list(MobileDevice.objects.filter(user_id__in=list(user_ids), revoked_at__isnull=True)
                .exclude(push_token='').values_list('push_token', flat=True))


def send(user_ids, message):
    """Queue one FCM message to every signed-in phone of these users (background thread)."""
    sa = _service_account()
    if not sa or not user_ids:
        return 0
    toks = _tokens(user_ids)
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


# ─────────────────────────────── the four kinds ───────────────────────────────

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
    })


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
    return send(people, {
        'notification': {'title': title, 'body': body},
        'data': _data({'type': 'chat', 'thread': t.pk, 'message': message.pk}),
        'android': {'priority': 'HIGH', 'notification': {'channel_id': 'tt_chat', 'tag': f'chat-{t.pk}'}},
        'apns': {'payload': {'aps': {'sound': 'default', 'thread-id': f'chat-{t.pk}'}}},
    })


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
    return send(set(who), {
        'notification': {'title': f'⚠️ {name} reported voucher {report.code}',
                         'body': (report.message or report.shown_error or 'Their voucher does not work.')[:180]},
        'data': _data({'type': 'complaint', 'id': report.pk}),
        'android': {'priority': 'HIGH', 'notification': {'channel_id': 'tt_complaints'}},
        'apns': {'payload': {'aps': {'sound': 'default'}}},
    })
