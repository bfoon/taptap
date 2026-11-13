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


def reachable(hs):
    """The picked people who can be rung right now: TapTap open in a browser or the app, or a phone signed in to
    the app with notifications on. ("Ring only those with TapTap open" used to count a phone in a pocket as away,
    so whenever one person had TapTap open on a computer, nobody's phone rang.)"""
    try:
        from . import mobile_push
        phones = mobile_push.phone_users([u.pk for u in hs])
    except Exception:
        phones = set()
    return [u for u in hs if chat.online(u.pk) or u.pk in phones]


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
    free = [u for u in (reachable(hs) or hs) if not chat_call.busy(u.pk)]
    return {'open': bool(hs), 'online': len(online), 'busy': bool(hs) and not free}


# ─────────────────────────────── start ───────────────────────────────

def _clean(v, n):
    return ' '.join(str(v or '').split())[:n]


def _initials(name):
    parts = [p for p in name.replace('-', ' ').split() if p]
    return ((parts[0][0] + (parts[1][0] if len(parts) > 1 else '')) if parts else '☎').upper()


def _key(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def start(page, *, name='', phone='', ip='', device=None):
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
    targets = (reachable(hs) or hs) if settings_for(business).online_first else hs
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
    cust = remember(page, device, name=name, phone=phone, ip=ip, kind='call')
    if cust is not None:                       # who is calling: MAC address, voucher, why they may be calling
        call['guest'].update({'cust': cust.pk, 'mac': cust.mac})
        call['card'] = customer_card(business, cust)
        if call['card'].get('line'):
            call['info'] = call['card']['line']
    cache.set(chat_call._k(cid), call, chat_call.CALL_TTL)
    cache.set(f'call:user:{gid}', cid, chat_call.CALL_TTL)
    chat_call.heartbeat(call, gid)
    for u in free:
        chat_call._add_ring(u.pk, cid)
    cache.set(rk, n + 1, IP_WINDOW)
    cache.set(ringing_key, (live + [cid])[-10:], chat_call.RING_SECONDS + 30)
    return call, secret


def guest_call(cid, secret, mac=''):
    """The call, if this secret belongs to its customer; else None. A call TapTap staff started towards a customer
    (a call-back) is also the customer's when their phone has the MAC address it was started for."""
    call = chat_call.get(cid)
    g = (call or {}).get('guest')
    if not g:
        return None
    if secret and isinstance(secret, str) and g.get('key') and hmac.compare_digest(g['key'], _key(secret)):
        return call
    if call.get('dir') == 'out' and g.get('mac') and norm_mac(mac) == g['mac']:
        return call
    return None


def guest_uid(call):
    """The customer's side of the call: the caller, or — for a call-back — the one it rings."""
    return call['caller'] if is_guest(call['caller']) else call['targets'][0]


def guest_state(call):
    """What the customer's page needs on each poll (also keeps the customer's side alive)."""
    st = chat_call.public_state(call, guest_uid(call))
    out = {k: st.get(k) for k in ('id', 'state', 'reason', 'answer', 'reanswer', 'rask', 'offer', 'reoffer')}
    if st.get('peer'):
        out['peer'] = {'name': st['peer']['name'].split(' ')[0], 'initials': st['peer']['initials']}
    if st['state'] == 'ringing':
        out['ringing_on'] = sum(1 for u in call['targets'] if chat_call.ring_acked(call, u))   # phones that confirmed
        out['left'] = max(0, int(call.get('ring', chat_call.RING_SECONDS) - (time.time() - call['at'])))
    return out


# ─────────────────────────────── "call me back" (no microphone) ───────────────────────────────

CALLBACKS_PER_IP = 3              # per 10 minutes


def callback_request(page, *, name='', phone='', ip='', why='', device=None):
    """The customer's phone could not use the microphone (the small Wi-Fi sign-in window, a blocked permission):
    they leave their number instead. It lands exactly like a missed call — a note in the team room with the number
    (Customer calls in the app, with a call-back button) and a push to the people who answer."""
    business = page.business
    hs = handlers(business)
    if not hs:
        raise chat.ChatError('Nobody is set up to answer calls yet.')
    name, phone = _clean(name, 60), _clean(phone, 30)
    if sum(c.isdigit() for c in phone) < 6:
        raise chat.ChatError('Type your phone number so we can call you back.')
    rk = f'pcall:cb:{business.pk}:{ip}'
    n = cache.get(rk, 0)
    if n >= CALLBACKS_PER_IP:
        raise chat.ChatError('We already have your number — someone will call you back soon.')
    cache.set(rk, n + 1, IP_WINDOW)
    remember(page, device, name=name, phone=phone, ip=ip, kind='callback', outcome='waiting')
    who = name or 'a Wi-Fi customer'
    reason = {'mic': 'their phone could not use the microphone', 'captive': 'the Wi-Fi sign-in window cannot make calls'}.get(why, '')
    text = f'📶 Missed Wi-Fi call from {who} · {phone} — asked to be called back' + (f' ({reason})' if reason else '')
    where = f' ({page.name[:80]})' if page.name else ''
    from .models_chat import ChatMessage
    thread = chat.team_thread(business)
    m = ChatMessage.objects.create(thread=thread, sender=None, body=(text + where)[:4000], system=True)
    thread.last_at = m.created_at
    thread.save(update_fields=['last_at'])
    try:
        from . import mobile_push
        if mobile_push.configured():
            mobile_push.missed_call([u.pk for u in hs], text[2:].strip(), phone, name)
    except Exception:
        pass
    try:
        from .utils import log
        log(business, 'Wi-Fi Call', text[:200])
    except Exception:
        pass
    return text


VOICE_PER_IP = 3                  # voice messages per phone/IP every 10 minutes
VOICE_PER_BUSINESS_HOUR = 40      # from all customers together


def voice_message(page, data, *, name='', phone='', secs=0, ip='', device=None):
    """A customer on the Wi-Fi page leaves a voice message (up to 30 seconds) instead of calling.

    It lands in the team room like a chat voice message, with the customer's name and number above it, and the
    people who answer calls get a push. Like every voice message it is deleted for good 1 minute after someone
    first plays it (core/chat_media.py)."""
    from django.db import transaction
    from . import chat_media
    from .models_chat import ChatMessage
    business = page.business
    if not calls_enabled(business):
        raise chat.ChatError('Calls and voice messages are switched off for this Wi-Fi.')
    hs = handlers(business)
    if not hs:
        raise chat.ChatError('Nobody is set up to answer yet.')
    mime, secs = chat_media.read_voice(data or b'', secs)
    name, phone = _clean(name, 60), _clean(phone, 30)
    if phone and sum(c.isdigit() for c in phone) < 6:
        raise chat.ChatError('That phone number looks too short.')
    rk, bk = f'pcall:voice:{business.pk}:{ip}', f'pcall:voice:{business.pk}'
    n, nb = cache.get(rk, 0), cache.get(bk, 0)
    if n >= VOICE_PER_IP:
        raise chat.ChatError('We already have your voice messages — someone will get back to you soon.')
    if nb >= VOICE_PER_BUSINESS_HOUR:
        raise chat.ChatError('Too many voice messages right now. Please try again a little later.')
    q = chat_media.QUOTA_BYTES
    if q and chat_media.business_usage(business.pk) + len(data) > q:
        raise chat.ChatError('Voice messages cannot be received right now. Please call instead.')
    who = name or 'a Wi-Fi customer'
    text = f'🎙️ Voice message from {who}' + (f' · {phone}' if phone else '') + (f' ({page.name[:80]})' if page.name else '')
    thread = chat.team_thread(business)
    with transaction.atomic():
        m = ChatMessage.objects.create(thread=thread, sender=None, body=text[:4000], attachment='voice')
        chat_media.store_voice(m, business.pk, data, mime, secs)
        thread.last_at = m.created_at
        thread.save(update_fields=['last_at'])
    cache.set(rk, n + 1, IP_WINDOW)
    cache.set(bk, nb + 1, 3600)
    remember(page, device, name=name, phone=phone, ip=ip, kind='voice', outcome='waiting')
    try:
        from . import mobile_push
        if mobile_push.configured():
            mobile_push.portal_voice([u.pk for u in hs], m, f'{who} · {secs} s' + (f' · {phone}' if phone else ''))
    except Exception:
        pass
    try:
        from .utils import log
        log(business, 'Wi-Fi Call', text[:200])
    except Exception:
        pass
    return m


# ─────────────────────────────── the note in the team room ───────────────────────────────

def note(call, reason, live_at):
    """Leave one line in the team room for every customer call (called once, from chat_call.finish)."""
    g = call.get('guest') or {}
    who = g.get('name') or 'a Wi-Fi customer'
    phone = g.get('phone') or ''
    where = f' ({g["page"]})' if g.get('page') else ''
    answered_by = ''
    t = cache.get(chat_call._k(call['id'], 'taken'))
    out = call.get('dir') == 'out'
    try:
        if g.get('cust'):
            from .models_portal_call import PortalCaller
            PortalCaller.objects.filter(pk=g['cust']).update(
                last_kind='out' if out else 'call',
                last_outcome='answered' if live_at else reason if reason in ('missed', 'declined') else 'missed' if reason == 'cancelled' else 'failed')
    except Exception:
        pass
    if out:                                   # staff called the customer back on the Wi-Fi page
        from django.contrib.auth.models import User
        u = User.objects.filter(pk=call['caller']).first()
        by = chat.display_name(u).split(' ')[0] if u else 'Staff'
        tail = f' · {phone}' if phone else ''
        if live_at:
            secs = int(time.time() - live_at)
            text = f'📶 {by} called {who} back on the Wi-Fi page{tail} · {secs // 60}:{secs % 60:02d}'
        elif reason == 'declined':
            text = f'📶 {who} declined {by}’s call-back on the Wi-Fi page{tail}'
        else:
            text = f'📶 {by} tried to call {who} back on the Wi-Fi page{tail} — no answer (their page shows “we tried to call you”)'
        try:
            from .models_chat import ChatMessage, ChatThread
            thread = ChatThread.objects.get(pk=call['thread'])
            m = ChatMessage.objects.create(thread=thread, sender=None, body=text[:4000], system=True)
            thread.last_at = m.created_at
            thread.save(update_fields=['last_at'])
        except Exception:
            pass
        return
    if t and isinstance(t.get('uid'), int):
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
                mobile_push.missed_call(call.get('targets') or [], text[2:].strip(), phone, g.get('name') or '')
        from .utils import log
        log(thread.business, 'Wi-Fi Call', text[:200])
    except Exception:
        pass


# ─────────────────────────────── who is calling: the customer's record ───────────────────────────────

import re as _re

OUT_RING_SECONDS = 90             # a call-back rings longer: the customer has to notice the card on their page
TRIED_SECONDS = 15 * 60           # after a missed call-back their page says "we tried to call you" this long
SEEN_SECONDS = 40                 # "on the Wi-Fi page now": their page asked for calls this recently


def norm_mac(v):
    h = _re.sub(r'[^0-9A-Fa-f]', '', str(v or '')).upper()
    if len(h) != 12 or h == '000000000000':
        return ''
    return ':'.join(h[i:i + 2] for i in range(0, 12, 2))


def _device_key(device):
    dk = str((device or {}).get('dk') or '')
    return _key(dk) if 16 <= len(dk) <= 80 else ''


def _agent(ua):
    ua = str(ua or '')
    os_ = 'iPhone' if 'iPhone' in ua else 'iPad' if 'iPad' in ua else 'Android' if 'Android' in ua else \
        'Windows' if 'Windows' in ua else 'Mac' if 'Macintosh' in ua else ''
    br = 'Wi-Fi sign-in window' if 'CaptivePortal' in ua or '; wv)' in ua else 'Samsung Internet' if 'SamsungBrowser' in ua else \
        'Edge' if 'Edg' in ua else 'Opera' if 'OPR/' in ua else 'Firefox' if 'Firefox/' in ua or 'FxiOS' in ua else \
        'Chrome' if 'Chrome/' in ua or 'CriOS' in ua else 'Safari' if 'Safari/' in ua else ''
    return ' · '.join(x for x in (os_, br) if x)[:80]


def find_caller(business, device):
    """The record of this phone (by its page key, else by its MAC address), or None."""
    from .models_portal_call import PortalCaller
    kh, mac = _device_key(device), norm_mac((device or {}).get('mac'))
    qs = PortalCaller.objects.filter(business=business)
    c = qs.filter(key_hash=kh).first() if kh else None
    if c is None and mac:
        c = qs.filter(mac=mac).order_by('-last_seen').first()
    return c


def remember(page, device, *, name='', phone='', ip='', kind='', outcome=''):
    """Keep what the customer's page told us about this phone. Never raises: a call must not fail over a record."""
    try:
        from .models_portal_call import PortalCaller
        device = device if isinstance(device, dict) else {}
        kh, mac = _device_key(device), norm_mac(device.get('mac'))
        if not kh and not mac:
            return None
        business = page.business
        c = find_caller(business, device) or PortalCaller(business=business)
        c.key_hash = kh or c.key_hash
        c.mac = mac or c.mac
        c.ip = _clean(device.get('ip'), 64) or c.ip or _clean(ip, 64)
        c.name = _clean(name, 60) or c.name
        c.phone = _clean(phone, 30) or c.phone
        user = _clean(device.get('user'), 120)
        if user and _re.match(r'^[\w.@+\- ]+$', user):
            c.username = user
        c.page = page.name[:80]
        c.device = _agent(device.get('ua')) or c.device
        if kind:
            c.calls = (c.calls or 0) + 1
            c.last_kind, c.last_outcome = kind, outcome
        c.save()
        return c
    except Exception:
        import logging
        logging.getLogger(__name__).exception('portal caller record')
        return None


def _left_text(seconds):
    s = int(max(0, seconds))
    if s >= 172800:
        return f'{s // 86400} days left'
    if s >= 3600:
        return f'{s // 3600} h {s % 3600 // 60:02d} min left'
    return f'{max(1, s // 60)} min left'


def _voucher_of(business, cust):
    from .models import Voucher, VoucherDeviceBinding
    v = None
    if cust.username:
        v = Voucher.objects.filter(business=business, code__iexact=cust.username).select_related('router').first()
    if v is None and cust.mac:
        b = (VoucherDeviceBinding.objects.filter(business=business, current_mac__iexact=cust.mac)
             .select_related('voucher', 'voucher__router').order_by('-last_seen_at').first())
        v = b.voucher if b and not b.voucher.deleted_at else None
    return v


def customer_card(business, cust):
    """Everything staff should see while this customer's call rings: who, which phone (MAC address), which voucher,
    how much time it has, and the likely reason for the call. Small, plain values only — it travels with the call."""
    from django.utils import timezone
    card = {'id': cust.pk, 'name': cust.name, 'phone': cust.phone, 'mac': cust.mac, 'ip': cust.ip,
            'device': cust.device, 'page': cust.page, 'calls': cust.calls,
            'private_mac': bool(cust.mac) and int(cust.mac[1], 16) & 2 == 2}
    try:
        v = _voucher_of(business, cust)
        if v is not None:
            from .voucher_history import display_state, ends_at
            from . import voucher_unstick
            now = timezone.now()
            end = ends_at(v)
            key, label = display_state(v, now)
            vc = {'id': v.pk, 'code': v.code, 'plan': v.plan_name, 'state': label, 'state_key': key,
                  'member': v.is_member, 'devices': v.max_devices, 'router': v.router.name if v.router_id else ''}
            if end and end > now and key in ('used', 'warned'):
                vc['left'] = _left_text((end - now).total_seconds())
            elif not v.used_at and key not in ('expired', 'disabled', 'archived', 'frozen'):
                vc['left'] = 'not used yet'
            card['voucher'] = vc
            why = voucher_unstick.blocking(v, now)
            ref = voucher_unstick.last_refusal(v) or {}
            if why:
                card['why'] = f'Voucher {v.code}: {why}.'
            elif ref.get('error'):
                card['why'] = f'The router refused {v.code}: “{ref["error"][:120]}”'
                card['fix'] = True                # "Can't connect? → Fix it" will help
    except Exception:
        import logging
        logging.getLogger(__name__).exception('customer card')
    bits = []
    if card.get('voucher'):
        vc = card['voucher']
        bits.append(vc['code'] + (f' · {vc["left"]}' if vc.get('left') else f' · {vc["state"]}'))
    if card['mac']:
        bits.append('MAC ' + card['mac'])
    card['line'] = ' · '.join(bits)[:120]
    return card


def seen_key(business_id, cust_id):
    return f'pcall:seen:{business_id}:{cust_id}'


def caller_rows(business, limit=40):
    """The customers who called, left a number or a voice message — newest first, each with its card."""
    from .models_portal_call import PortalCaller
    rows = []
    for c in PortalCaller.objects.filter(business=business)[:limit]:
        card = customer_card(business, c)
        card.update({'at': c.last_seen.isoformat(), 'kind': c.last_kind, 'outcome': c.last_outcome,
                     'here': bool(cache.get(seen_key(business.pk, c.pk))), 'can_ring': bool(c.key_hash or c.mac)})
        rows.append(card)
    return rows


# ─────────────────────────────── call the customer back, on their Wi-Fi page ───────────────────────────────
# Staff press "Call back on the Wi-Fi page": the call rings in the customer's browser — the login / status page shows
# "… is calling you · Answer" (it asks TapTap every few seconds once that phone has called before), and the call
# page answers it. Same call system, with the roles swapped: staff are the caller, the customer's page the one that
# answers. Not on the page? For 15 minutes their page says "we tried to call you — call us now".

def call_back(user, business, cust_id):
    from .models_portal_call import PortalCaller
    if business is None or not chat.people_of(business).filter(pk=user.pk).exists():
        raise chat.ChatError('Only the team of this business can call its customers.')
    cust = PortalCaller.objects.filter(business=business, pk=cust_id).first()
    if cust is None or not (cust.key_hash or cust.mac):
        raise chat.ChatError('This customer cannot be reached on the Wi-Fi page. Use their phone number.')
    if chat_call.busy(user.pk):
        raise chat.ChatError('You are already in a call.')
    gid = GUEST + f'c{cust.pk}'
    if chat_call.busy(gid):
        raise chat.ChatError('Someone is already calling this customer.')
    who = cust.name or 'Wi-Fi customer'
    cid = secrets.token_urlsafe(12)
    thread = chat.team_thread(business)
    name = chat.display_name(user)
    card = customer_card(business, cust)
    call = {'id': cid, 'thread': thread.pk, 'kind': 'portal', 'dir': 'out', 'caller': user.pk, 'caller_name': name,
            'caller_initials': chat.initials(user), 'video': False, 'at': time.time(), 'targets': [gid],
            'ring': OUT_RING_SECONDS, 'label': business.business_name, 'card': card,
            'guest': {'name': cust.name, 'phone': cust.phone, 'business': business.pk, 'page': cust.page,
                      'key': cust.key_hash, 'mac': cust.mac, 'cust': cust.pk}}
    cache.set(chat_call._k(cid), call, chat_call.CALL_TTL)
    cache.set(f'call:user:{user.pk}', cid, chat_call.CALL_TTL)
    chat_call.heartbeat(call, user.pk)
    cache.set(f'pcall:out:{business.pk}:{cust.pk}', cid, OUT_RING_SECONDS + 30)
    cache.set(f'pcall:tried:{business.pk}:{cust.pk}', name.split(' ')[0], TRIED_SECONDS)
    return call, f'{who}' + (f' · {cust.phone}' if cust.phone else '')


def inbox(page, device):
    """The customer's page asks: is anybody calling me? (Also tells staff this customer is on the page now.)"""
    business = page.business
    cust = find_caller(business, device)
    if cust is None:
        return {}
    cache.set(seen_key(business.pk, cust.pk), 1, SEEN_SECONDS)
    cid = cache.get(f'pcall:out:{business.pk}:{cust.pk}')
    call = chat_call.get(cid) if cid else None
    if call and chat_call.state(call)[0] == 'ringing':
        return {'ring': {'id': cid, 'from': business.business_name,
                         'left': max(0, int(OUT_RING_SECONDS - (time.time() - call['at'])))}}
    tried = cache.get(f'pcall:tried:{business.pk}:{cust.pk}')
    return {'tried': {'by': tried}} if tried and not (call and chat_call.state(call)[0] != 'ended') else {}


def answered(call):
    """The customer picked up (or declined): their page no longer needs the "we tried to call you" card."""
    g = call.get('guest') or {}
    cache.delete(f'pcall:tried:{g.get("business")}:{g.get("cust")}')
