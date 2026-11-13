"""Voice / video calls and screen sharing in chat — the signaling side.

The media never touches this server. Browsers connect peer-to-peer with WebRTC, which always encrypts
audio, video and screen with DTLS-SRTP (a TURN relay, if you set one up, only forwards encrypted packets).
This module only passes the two connection descriptions (offer and answer) between the browsers and keeps
track of who is ringing whom. It runs on the shared Redis cache — no WebSocket server and no new tables.

Every piece of a call is its own cache key, written once, so concurrent requests never overwrite each other:

    call:<id>          who, where, voice/video, ring targets           (written at start, never changed)
    call:<id>:offer    caller's connection description
    call:<id>:taken    {uid, at}  — the first person to answer (cache.add = atomic, "first answer wins")
    call:<id>:answer   callee's connection description               → the call is live
    call:<id>:end      why it ended                                   (cache.add: logged exactly once)
    call:<id>:dec:<u>  this person declined
    call:<id>:hb:<u>   last heartbeat from each side (a closed laptop ends the call)
    call:<id>:ro       {gen, desc}  caller's ICE-restart offer (the line dropped: new network paths)
    call:<id>:ra       {gen, desc}  callee's ICE-restart answer for that generation
    call:<id>:rask     time the callee asked the caller to restart (only the caller sends restart offers)
    call:ring:<uid>    calls ringing for a person (checked by the cheap poll)
    call:user:<uid>    the call a person is in (busy signal)

Who can be called
    portal   a customer on a Wi-Fi page calls the business: rings the people the owner picked (core/portal_call.py)
    direct   the other person
    support  business side → the assigned agent, or every active agent (first to answer takes it);
             agent → everyone in that business (first to answer).
    team     not called directly — tap a person to start a direct call.

In-call tools (all peer-to-peer over the call's encrypted data channel, see static/js/chat-call.js):
camera flip / back camera, drag-to-capture an area into the chat, short screen recordings (stored as chat
video clips, deleted with every chat file after a week) and remote control of a shared TapTap tab — which
the sharer must allow, can take back at any moment (Esc), and which leaves a note in the conversation.

Reconnecting: when the line drops (Wi-Fi hop, dual-WAN failover, phone switching networks) the call is not
hung up. The caller sends a fresh ICE-restart offer through this module, the callee answers, and the same call
carries on over the new path — audio, camera, screen share and the data channel all survive. Only after
RECONNECT_GIVE_UP seconds in the browser (or HEARTBEAT_LOST seconds of silence here) does the call end as "lost".

Privacy: CHAT_TURN_ONLY=1 sends every call through your TURN relay, so neither side ever learns the
other's IP address (useful when TapTap Support calls customers). The browser verifies the other side
with "safety emoji" derived from both DTLS fingerprints — see static/js/chat-call.js.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

from django.core.cache import cache

from . import chat

RING_SECONDS = 40
CALL_TTL = 4 * 3600
# A call is ended as "lost" only after this long without a poll from the other side. 40 s was too short: a phone
# that briefly backgrounds the call, or a router failing over to its second internet link, went silent for that
# long while the media itself was fine or about to come back. The browser gives up by itself after 60 s of no
# media (static/js/chat-call.js), so this is only the backstop for a closed laptop or a killed tab.
HEARTBEAT_LOST = 90
# Answered but the connection details never arrived (the answering phone lost its network, the app was closed, the
# microphone failed): the call used to stay "connecting" for CALL_TTL — and its people "busy", so no customer call
# rang them for up to 4 hours. Both call windows give up long before this.
CONNECT_SECONDS = 75
# A phone that has not confirmed it is ringing gets the push again at these ages of the call (seconds): one data
# push can be late or lost on a phone that is asleep.
REPUSH_AT = (7, 15, 25)
MAX_RESTART_GEN = 500
MAX_SDP = 24000


# ─────────────────────────────── ICE / TURN ───────────────────────────────

def ice_config(user):
    """RTCPeerConnection settings for this person. TURN credentials are short-lived (coturn REST style)."""
    stun = [u.strip() for u in os.getenv('CHAT_STUN_URLS', 'stun:stun.l.google.com:19302,stun:stun.cloudflare.com:3478').split(',') if u.strip()]
    servers = [{'urls': stun}] if stun else []
    turn = [u.strip() for u in os.getenv('CHAT_TURN_URLS', '').split(',') if u.strip()]
    if turn:
        secret = os.getenv('CHAT_TURN_SECRET', '')
        if secret:                              # coturn: use-auth-secret + static-auth-secret
            username = f'{int(time.time()) + 6 * 3600}:tt{user.pk}'
            cred = base64.b64encode(hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()).decode()
            servers.append({'urls': turn, 'username': username, 'credential': cred})
        else:
            servers.append({'urls': turn, 'username': os.getenv('CHAT_TURN_USERNAME', ''),
                            'credential': os.getenv('CHAT_TURN_PASSWORD', '')})
    relay_only = bool(turn) and os.getenv('CHAT_TURN_ONLY', '0') in ('1', 'true', 'yes')
    return {'iceServers': servers, 'iceTransportPolicy': 'relay' if relay_only else 'all', 'relayOnly': relay_only}


# ─────────────────────────────── helpers ───────────────────────────────

def _k(cid, part=''):
    return f'call:{cid}' + (f':{part}' if part else '')


def _side(thread, uid, biz_ids=None):
    if thread.kind != 'support':
        return 'p'
    biz_ids = biz_ids if biz_ids is not None else set(chat.people_of(thread.business).values_list('id', flat=True))
    return 'biz' if uid in biz_ids else 'agent'


def get(cid):
    if not cid or not isinstance(cid, str) or len(cid) > 40:
        return None
    return cache.get(_k(cid))


def _taken(cid):
    return cache.get(_k(cid, 'taken'))


def state(call):
    cid = call['id']
    end = cache.get(_k(cid, 'end'))
    if end:
        return 'ended', end
    now = time.time()
    t = _taken(cid)
    if cache.get(_k(cid, 'answer')):
        # both sides gone (two closed apps, a crashed tab): nobody is left to notice, so notice here
        beats = [cache.get(_k(cid, f'hb:{u}')) for u in (call['caller'], (t or {}).get('uid')) if u is not None]
        if beats and all(b is None or now - b > HEARTBEAT_LOST for b in beats) \
                and now - (cache.get(_k(cid, 'live_at')) or now) > HEARTBEAT_LOST:
            finish(call, 'lost')
            return 'ended', cache.get(_k(cid, 'end'))
        return 'active', None
    if t:
        if now - (t.get('at') or now) > CONNECT_SECONDS:
            finish(call, 'failed')
            return 'ended', cache.get(_k(cid, 'end'))
        return 'connecting', None
    if time.time() - call['at'] > call.get('ring', RING_SECONDS):
        finish(call, 'missed')
        return 'ended', cache.get(_k(cid, 'end'))
    return 'ringing', None


def busy(uid):
    cid = cache.get(f'call:user:{uid}')
    call = get(cid) if cid else None
    return bool(call and state(call)[0] != 'ended')


def _add_ring(uid, cid):
    key = f'call:ring:{uid}'
    ids = [c for c in (cache.get(key) or []) if c != cid][-4:] + [cid]
    cache.set(key, ids, RING_SECONDS + 20)
    _push(lambda p: p.call_ringing(uid, get(cid) or {'id': cid}))      # the mobile app's incoming-call screen


def _push(fn):
    """Mobile app push (core/mobile_push.py) — never lets a push problem touch the call itself."""
    try:
        from . import mobile_push
        if mobile_push.configured():
            fn(mobile_push)
    except Exception:
        import logging
        logging.getLogger(__name__).exception('call push')


def ring_ack(call, uid):
    """This person's phone says it is ringing (the app got the push, or its own check saw the call)."""
    cid = call['id']
    if uid in call.get('targets', ()) and cache.add(_k(cid, f'ack:{uid}'), time.time(), RING_SECONDS + 60):
        cache.set(_k(cid, f'ackat:{uid}'), round(time.time() - call['at'], 1), 600)


def ring_acked(call, uid):
    return cache.get(_k(call['id'], f'ack:{uid}')) is not None


def reping(call):
    """While it rings: push again to every phone that has not confirmed it is ringing. Driven by the caller's own
    poll (about once a second), so it needs no timer and stops with the call."""
    cid = call['id']
    age = time.time() - call['at']
    if age > RING_SECONDS or call.get('kind') == 'test':
        return 0
    n = 0
    for i, at in enumerate(REPUSH_AT):
        if age < at:
            break
        if not cache.add(_k(cid, f'rp:{i}'), 1, RING_SECONDS + 20):
            continue
        for uid in call['targets']:
            if not isinstance(uid, int) or ring_acked(call, uid) or cache.get(_k(cid, f'dec:{uid}')):
                continue
            _push(lambda p, uid=uid: p.call_ringing(uid, call, attempt=i + 2))
            n += 1
        break
    return n


def _drop_ring(uid, cid):
    key = f'call:ring:{uid}'
    ids = [c for c in (cache.get(key) or []) if c != cid]
    cache.set(key, ids, RING_SECONDS + 20) if ids else cache.delete(key)


def is_member(call, uid):
    if uid == call['caller']:
        return True
    t = _taken(call['id'])
    if t:
        return t['uid'] == uid
    return uid in call['targets']


def other_uid(call, uid):
    if uid == call['caller']:
        t = _taken(call['id'])
        return t['uid'] if t else None
    return call['caller']


# ─────────────────────────────── start / ring ───────────────────────────────

def start(thread, user, video=False):
    if thread.kind not in ('direct', 'support'):
        raise chat.ChatError('Tap a person to call them directly.')
    if busy(user.pk):
        raise chat.ChatError('You are already in a call.')
    biz_ids = set(chat.people_of(thread.business).values_list('id', flat=True))
    mine = _side(thread, user.pk, biz_ids)
    targets = [u for u in chat.participants(thread) if u.pk != user.pk and (thread.kind != 'support' or _side(thread, u.pk, biz_ids) != mine)]
    if thread.kind == 'support' and mine == 'biz' and not thread.assigned_to_id:
        targets = [u for u in targets if chat.online(u.pk)] or targets   # ring the agents who are on now
    targets = [u for u in targets if u.is_active][:12]
    if not targets:
        raise chat.ChatError('There is nobody to call here yet.')
    free = [u for u in targets if not busy(u.pk)]
    if not free:
        who = chat.display_name(targets[0]).split(' ')[0] if len(targets) == 1 else 'Everyone'
        raise chat.ChatError(f'{who} is on another call. Try again in a moment.')
    cid = secrets.token_urlsafe(12)
    name = chat.display_name(user)
    if thread.kind == 'support' and mine == 'agent':
        label_for = {'biz': f'TapTap Support · {name.split(" ")[0]}'}
    elif thread.kind == 'support':
        label_for = {'agent': f'{name} · {thread.business.business_name}'}
    else:
        label_for = {}
    call = {'id': cid, 'thread': thread.pk, 'kind': thread.kind, 'caller': user.pk, 'caller_name': name,
            'caller_initials': chat.initials(user), 'video': bool(video), 'at': time.time(),
            'targets': [u.pk for u in free], 'label': label_for.get('biz' if mine == 'agent' else 'agent', name)}
    cache.set(_k(cid), call, CALL_TTL)
    cache.set(f'call:user:{user.pk}', cid, CALL_TTL)
    heartbeat(call, user.pk)
    for u in free:
        _add_ring(u.pk, cid)
    return call


def rings_for(user):
    """Calls ringing for this person right now (the cheap poll — cache only, no database)."""
    out = []
    for cid in cache.get(f'call:ring:{user.pk}') or []:
        call = get(cid)
        if not call or user.pk not in call['targets'] or cache.get(_k(cid, f'dec:{user.pk}')):
            continue
        st, _ = state(call)
        if st != 'ringing':
            continue
        ring = {'id': cid, 'thread': call['thread'], 'from': call['label'], 'initials': call['caller_initials'],
                'video': call['video'], 'kind': call['kind'], 'left': max(0, int(RING_SECONDS - (time.time() - call['at'])))}
        if call.get('card'):                      # a customer on the Wi-Fi page: who they are (core/portal_call.py)
            ring['customer'] = call['card']
        out.append(ring)
    return out


# ─────────────────────────────── signaling ───────────────────────────────

def _sdp(desc, kind):
    if not isinstance(desc, dict) or desc.get('type') != kind:
        raise chat.ChatError('Bad connection details.')
    sdp = desc.get('sdp') or ''
    if not isinstance(sdp, str) or not sdp.startswith('v=0') or len(sdp) > MAX_SDP:
        raise chat.ChatError('Bad connection details.')
    return {'type': kind, 'sdp': sdp}


def post_offer(call, user, desc):
    if user.pk != call['caller']:
        raise chat.ChatError('Only the caller sends the offer.')
    cache.set(_k(call['id'], 'offer'), _sdp(desc, 'offer'), CALL_TTL)


def accept(call, user):
    cid = call['id']
    if user.pk not in call['targets']:
        raise chat.ChatError('This call is not for you.')
    st, _ = state(call)
    if st == 'ended':
        raise chat.ChatError('The call has ended.')
    if busy(user.pk) and cache.get(f'call:user:{user.pk}') != cid:
        raise chat.ChatError('Hang up your other call first.')
    if not cache.add(_k(cid, 'taken'), {'uid': user.pk, 'at': time.time()}, CALL_TTL):
        t = _taken(cid)
        if not t or t['uid'] != user.pk:
            raise chat.ChatError('Someone else already answered.')
    cache.set(f'call:user:{user.pk}', cid, CALL_TTL)
    for uid in call['targets']:
        _drop_ring(uid, cid)
    _push(lambda p: p.call_ended([u for u in call['targets'] if u != user.pk], cid))   # stop it ringing on the others' phones
    heartbeat(call, user.pk)
    return cache.get(_k(cid, 'offer'))


def post_answer(call, user, desc):
    t = _taken(call['id'])
    if not t or t['uid'] != user.pk:
        raise chat.ChatError('Answer the call first.')
    cache.set(_k(call['id'], 'answer'), _sdp(desc, 'answer'), CALL_TTL)
    cache.set(_k(call['id'], 'live_at'), time.time(), CALL_TTL)


# ─────────────────────────────── reconnect (ICE restart) ───────────────────────────────

def _gen(v):
    try:
        g = int(v)
    except (TypeError, ValueError):
        raise chat.ChatError('Bad connection details.')
    if not 1 <= g <= MAX_RESTART_GEN:
        raise chat.ChatError('Bad connection details.')
    return g


def _live_for(call, user):
    t = _taken(call['id'])
    if not t:
        raise chat.ChatError('The call is not connected yet.')
    if cache.get(_k(call['id'], 'end')):
        raise chat.ChatError('The call has ended.')
    return t


def post_reoffer(call, user, desc, gen):
    """Caller: a new offer with fresh ICE credentials, after the line dropped. Newer generations replace older."""
    if user.pk != call['caller']:
        raise chat.ChatError('Only the caller restarts the connection.')
    _live_for(call, user)
    g = _gen(gen)
    cur = cache.get(_k(call['id'], 'ro'))
    if cur and cur.get('gen', 0) > g:
        return
    cache.set(_k(call['id'], 'ro'), {'gen': g, 'desc': _sdp(desc, 'offer')}, CALL_TTL)
    heartbeat(call, user.pk)


def post_reanswer(call, user, desc, gen):
    """Callee: the answer to restart generation `gen`."""
    t = _live_for(call, user)
    if t['uid'] != user.pk:
        raise chat.ChatError('Answer the call first.')
    g = _gen(gen)
    cur = cache.get(_k(call['id'], 'ro'))
    if not cur or cur.get('gen') != g:
        return                                   # a newer restart is already on its way; this answer is stale
    cache.set(_k(call['id'], 'ra'), {'gen': g, 'desc': _sdp(desc, 'answer')}, CALL_TTL)
    heartbeat(call, user.pk)


def ask_restart(call, user):
    """Callee: my side lost the line — caller, please send a restart offer."""
    t = _live_for(call, user)
    if t['uid'] != user.pk:
        raise chat.ChatError('Answer the call first.')
    cache.set(_k(call['id'], 'rask'), time.time(), CALL_TTL)
    heartbeat(call, user.pk)


def decline(call, user):
    cid = call['id']
    cache.set(_k(cid, f'dec:{user.pk}'), 1, RING_SECONDS + 20)
    _drop_ring(user.pk, cid)
    if all(cache.get(_k(cid, f'dec:{u}')) for u in call['targets']) and not _taken(cid):
        finish(call, 'declined')


def heartbeat(call, uid):
    cache.set(_k(call['id'], f'hb:{uid}'), time.time(), CALL_TTL)


def finish(call, reason='ended'):
    """End the call once; posts the call note in the conversation (missed / declined / duration)."""
    cid = call['id']
    reason = reason if reason in ('ended', 'missed', 'declined', 'cancelled', 'lost', 'failed') else 'ended'
    if not cache.add(_k(cid, 'end'), reason, CALL_TTL):
        return
    for uid in call['targets']:
        _drop_ring(uid, cid)
    _push(lambda p: p.call_ended([u for u in [call['caller']] + call['targets'] if isinstance(u, int)], cid))
    for uid in [call['caller']] + call['targets']:
        if cache.get(f'call:user:{uid}') == cid:
            cache.delete(f'call:user:{uid}')
    live_at = cache.get(_k(cid, 'live_at'))
    if call.get('guest'):                     # a customer calling from a Wi-Fi page (core/portal_call.py)
        from .portal_call import note
        note(call, reason, live_at)
        return
    what = 'video call' if call['video'] else 'voice call'
    first = call['caller_name'].split(' ')[0]
    if live_at:
        secs = int(time.time() - live_at)
        text = f'📞 {what.capitalize()} · {secs // 60}:{secs % 60:02d}'
    elif reason == 'declined':
        text = f'📞 {first}’s {what} was declined'
    elif reason in ('missed', 'cancelled'):
        text = f'📞 Missed {what} from {first}'
    else:
        text = f'📞 {first}’s {what} could not connect'
    try:
        from django.contrib.auth.models import User
        from .models_chat import ChatThread
        thread = ChatThread.objects.select_related('business').get(pk=call['thread'])
        caller = User.objects.get(pk=call['caller'])
        chat.post(thread, caller, text, system=True)
    except Exception:
        pass


def control_summary(summary):
    """The sharer's window sends what happened during control (counts, pages, last actions). Clean it before it
    goes into the chat: only numbers, short labels and page paths — never anything typed."""
    if not isinstance(summary, dict):
        return ''
    def num(v):
        try:
            return max(0, min(int(v or 0), 9999))
        except (TypeError, ValueError):
            return 0
    def text(v, n=60):
        return ' '.join(str(v or '').split())[:n]
    acts, blocked = num(summary.get('actions')), num(summary.get('blocked'))
    pages = [text(p, 60) for p in (summary.get('pages') or [])[:6] if isinstance(p, str) and p.startswith('/')]
    last = [text(x) for x in (summary.get('last') or [])[:6] if isinstance(x, str) and text(x)]
    parts = []
    if acts or blocked:
        parts.append(f'{acts} action{"" if acts == 1 else "s"}' + (f', {blocked} blocked' if blocked else ''))
    if pages:
        parts.append('pages: ' + ', '.join(pages))
    if last:
        parts.append('last: ' + ' · '.join(last))
    return '\n'.join(parts)


def log_control(call, user, secs, summary=None):
    """The sharer's call window reports that a remote-control session ended: leave an audit note in the chat
    (how long, how many actions, which pages, the last few things clicked)."""
    other = other_uid(call, user.pk)
    if not other:
        return
    key = _k(call['id'], f'ctl:{int(time.time()) // 5}')
    if not cache.add(key, 1, 30):                # one note per session even if the beacon fires twice
        return
    secs = max(0, min(int(secs or 0), CALL_TTL))
    try:
        from django.contrib.auth.models import User
        from .models_chat import ChatThread
        u = User.objects.filter(pk=other).first()
        thread = ChatThread.objects.select_related('business').get(pk=call['thread'])
        who = chat.display_name(u).split(' ')[0] if u else 'Someone'
        me = chat.display_name(user).split(' ')[0]
        detail = control_summary(summary)
        chat.post(thread, user, f'🖱 {who} had remote control of {me}’s TapTap tab · {secs // 60}:{secs % 60:02d}' + (f'\n{detail}' if detail else ''), system=True)
    except Exception:
        pass


def public_state(call, uid):
    """What a call member's browser needs on each poll."""
    cid = call['id']
    st, reason = state(call)
    out = {'id': cid, 'state': st, 'reason': reason, 'video': call['video'], 'thread': call['thread'],
           'caller': call['caller'] == uid}
    if st == 'ringing' and call['caller'] == uid:
        try:
            reping(call)
        except Exception:
            pass
    if st in ('connecting', 'active'):
        heartbeat(call, uid)
        other = other_uid(call, uid)
        hb = cache.get(_k(cid, f'hb:{other}')) if other else None
        if st == 'active' and hb and time.time() - hb > HEARTBEAT_LOST:
            finish(call, 'lost')
            out['state'], out['reason'] = 'ended', 'lost'
            return out
        if other and call.get('guest') and isinstance(other, str):     # the customer on the Wi-Fi page
            g = call['guest']
            out['peer'] = {'name': (g.get('name') or 'Wi-Fi customer') + (f' · {g["phone"]}' if g.get('phone') else ''),
                           'initials': call.get('caller_initials') or '☎'}
        elif other:
            from django.contrib.auth.models import User
            u = User.objects.filter(pk=other).first()
            out['peer'] = {'name': chat.display_name(u), 'initials': chat.initials(u)} if u else None
    if call.get('card') and isinstance(uid, int):     # staff see the customer's record during the call
        out['customer'] = call['card']
    if call['caller'] == uid and st in ('connecting', 'active'):
        out['answer'] = cache.get(_k(cid, 'answer'))
        out['reanswer'] = cache.get(_k(cid, 'ra'))
        out['rask'] = cache.get(_k(cid, 'rask'))
    if call['caller'] != uid and st in ('connecting', 'active'):
        out['offer'] = cache.get(_k(cid, 'offer'))
        out['reoffer'] = cache.get(_k(cid, 'ro'))
    return out
