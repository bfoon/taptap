"""In-app calls from the Wi-Fi pages — a customer taps "Call" and TapTap rings the people the owner picked.

It is the same call system as chat (core/chat_call.py): the same ring card in TapTap, the same call window,
the same encrypted peer-to-peer audio (WebRTC, DTLS-SRTP), the same reconnect on a dropped line. The only
difference is who calls: a customer on the Wi-Fi page has no TapTap account, so the call carries a
*guest* caller:

    caller   'g:<random>'                 — never a user id, so nothing confuses it with a person
    guest    {name, phone, business, page, key}   key = sha256 of the secret only the customer's page holds

The customer's page (templates/core/portal_call.html + static/js/portal-call.js) talks to
views_portal_call.portal_call_api with the call id and that secret. Staff answer exactly like a chat call.

Who rings (PortalCallSettings): the people the owner picked (owner + active team members only); nobody picked =
the owner. With "online first", only those who have TapTap open ring; if none of them is, everyone picked rings.

Switched on per page in Portal Studio: a page offers in-app calls only while it has a visible "Call button" block
(calls_enabled). Every call — answered, missed or declined — leaves a note in the team room with the customer's
name and number, so a missed call can be returned.

Abuse limits: a few calls per phone/IP every 10 minutes, a few customers ringing one business at once, short
name/phone fields, and calls end by themselves after the ring time or when the customer's page goes quiet.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from django.core.cache import cache

from . import chat, chat_call

GUEST = 'g:'
MAX_RINGING_PER_BUSINESS = 3
STARTS_PER_IP = 5                 # per 10 minutes
IP_WINDOW = 600


class Guest:
    """Stands in for request.user when the customer's page calls chat_call functions (they only read .pk)."""
    is_authenticated = False

    def __init__(self, pk):
        self.pk = pk


def is_guest(uid):
    return isinstance(uid, str) and uid.startswith(GUEST)


# ─────────────────────────────── who answers ───────────────────────────────

def settings_for(business):
    from .models_portal_call import PortalCallSettings
    s, _ = PortalCallSettings.objects.get_or_create(business=business)
    return s


def candidates(business):
    """Everyone who may be picked: the owner and active team members."""
    return list(chat.people_of(business).order_by('first_name', 'username'))


def handlers(business):
    """The people whose TapTap rings for this business (picked ones still on the team; else the owner)."""
    allowed = {u.pk: u for u in candidates(business)}
    s = settings_for(business)
    picked = [allowed[pk] for pk in s.handlers.values_list('pk', flat=True) if pk in allowed]
    if picked:
        return picked
    owner = allowed.get(business.user_id)
    return [owner] if owner else []


def calls_enabled(business):
    """True while one of the business's pages has a visible Call button block (Portal Studio switches it)."""
    for cfg in business.portal_pages.values_list('config', flat=True):
        for b in (cfg or {}).get('blocks') or []:
            if b.get('type') == 'call' and not b.get('hidden'):
                return True
    return False


def status(business):
    """What the customer's page shows before calling: can it call, and is anybody there right now."""
    if not calls_enabled(business):
        return {'open': False, 'online': 0}
    hs = handlers(business)
    online = [u for u in hs if chat.online(u.pk)]
    free = [u for u in (online or hs) if not chat_call.busy(u.pk)]
    return {'open': bool(hs), 'online': len(online), 'busy': bool(hs) and not free}


# ─────────────────────────────── start ───────────────────────────────

def _clean(v, n):
    return ' '.join(str(v or '').split())[:n]


def _initials(name):
    parts = [p for p in name.replace('-', ' ').split() if p]
    return ((parts[0][0] + (parts[1][0] if len(parts) > 1 else '')) if parts else '☎').upper()


def _key(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def start(page, *, name='', phone='', ip=''):
    """Ring the business for a customer on `page`. Returns (call, secret) — the secret goes only to that page."""
    business = page.business
    if not calls_enabled(business):
        raise chat.ChatError('Calls are switched off for this Wi-Fi.')
    rk = f'pcall:ip:{business.pk}:{ip}'
    n = cache.get(rk, 0)
    if n >= STARTS_PER_IP:
        raise chat.ChatError('You have called several times just now. Wait a few minutes and try again.')
    ringing_key = f'pcall:ringing:{business.pk}'
    live = [c for c in (cache.get(ringing_key) or []) if (lambda x: x and chat_call.state(x)[0] == 'ringing')(chat_call.get(c))]
    if len(live) >= MAX_RINGING_PER_BUSINESS:
        raise chat.ChatError('Our staff are answering other customers. Try again in a minute.')

    hs = handlers(business)
    if not hs:
        raise chat.ChatError('Nobody is set up to answer calls yet.')
    online = [u for u in hs if chat.online(u.pk)]
    targets = (online or hs) if settings_for(business).online_first else hs
    free = [u for u in targets if u.is_active and not chat_call.busy(u.pk)][:12]
    if not free:
        raise chat.ChatError('Everyone is on another call. Try again in a minute.')

    name, phone = _clean(name, 60), _clean(phone, 30)
    who = name or 'Wi-Fi customer'
    secret = secrets.token_urlsafe(18)
    cid = secrets.token_urlsafe(12)
    gid = GUEST + secrets.token_hex(8)
    thread = chat.team_thread(business)
    call = {'id': cid, 'thread': thread.pk, 'kind': 'portal', 'caller': gid, 'caller_name': who,
            'caller_initials': _initials(who), 'video': False, 'at': time.time(), 'targets': [u.pk for u in free],
            'label': f'{who}' + (f' · {phone}' if phone else '') + ' · Wi-Fi',
            'guest': {'name': name, 'phone': phone, 'business': business.pk, 'page': page.name[:80], 'key': _key(secret)}}
    cache.set(chat_call._k(cid), call, chat_call.CALL_TTL)
    cache.set(f'call:user:{gid}', cid, chat_call.CALL_TTL)
    chat_call.heartbeat(call, gid)
    for u in free:
        chat_call._add_ring(u.pk, cid)
    cache.set(rk, n + 1, IP_WINDOW)
    cache.set(ringing_key, (live + [cid])[-10:], chat_call.RING_SECONDS + 30)
    return call, secret


def guest_call(cid, secret):
    """The call, if this secret belongs to its customer; else None."""
    call = chat_call.get(cid)
    g = (call or {}).get('guest')
    if not g or not secret or not isinstance(secret, str):
        return None
    return call if hmac.compare_digest(g['key'], _key(secret)) else None


def guest_state(call):
    """What the customer's page needs on each poll (also keeps the customer's side alive)."""
    st = chat_call.public_state(call, call['caller'])
    out = {k: st.get(k) for k in ('id', 'state', 'reason', 'answer', 'reanswer', 'rask')}
    if st.get('peer'):
        out['peer'] = {'name': st['peer']['name'].split(' ')[0], 'initials': st['peer']['initials']}
    if st['state'] == 'ringing':
        out['left'] = max(0, int(chat_call.RING_SECONDS - (time.time() - call['at'])))
    return out


# ─────────────────────────────── the note in the team room ───────────────────────────────

def note(call, reason, live_at):
    """Leave one line in the team room for every customer call (called once, from chat_call.finish)."""
    g = call.get('guest') or {}
    who = g.get('name') or 'a Wi-Fi customer'
    phone = g.get('phone') or ''
    where = f' ({g["page"]})' if g.get('page') else ''
    answered_by = ''
    t = cache.get(chat_call._k(call['id'], 'taken'))
    if t:
        from django.contrib.auth.models import User
        u = User.objects.filter(pk=t['uid']).first()
        answered_by = chat.display_name(u).split(' ')[0] if u else ''
    if live_at:
        secs = int(time.time() - live_at)
        text = f'📶 Wi-Fi call from {who}' + (f' · {phone}' if phone else '') + f' · {secs // 60}:{secs % 60:02d}' + \
               (f' · answered by {answered_by}' if answered_by else '')
    elif reason == 'declined':
        text = f'📶 Wi-Fi call from {who}' + (f' · {phone}' if phone else '') + ' was declined'
    elif reason in ('missed', 'cancelled'):
        text = f'📶 Missed Wi-Fi call from {who}' + (f' · {phone} — call them back' if phone else ' (no number left)')
    else:
        text = f'📶 Wi-Fi call from {who}' + (f' · {phone}' if phone else '') + ' could not connect'
    try:
        from .models_chat import ChatMessage, ChatThread
        thread = ChatThread.objects.get(pk=call['thread'])
        m = ChatMessage.objects.create(thread=thread, sender=None, body=(text + where)[:4000], system=True)
        thread.last_at = m.created_at
        thread.save(update_fields=['last_at'])
        if not live_at and reason in ('missed', 'cancelled') and phone:
            from . import mobile_push                  # a missed customer call: tell the people who answer, with the number
            if mobile_push.configured():
                mobile_push.send(call.get('targets') or [], {
                    'notification': {'title': '📶 Missed customer call', 'body': text[2:].strip()[:180]},
                    'data': {'type': 'missed_call', 'phone': phone, 'name': g.get('name') or ''},
                    'android': {'priority': 'HIGH', 'notification': {'channel_id': 'tt_calls_missed'}},
                    'apns': {'payload': {'aps': {'sound': 'default'}}}})
        from .utils import log
        log(thread.business, 'Wi-Fi Call', text[:200])
    except Exception:
        pass
