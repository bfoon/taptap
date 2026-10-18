"""One rule for time on the router: TapTap's clock decides, the router only follows.

Why vouchers were cut off early ("user has reached uptime limit")
-----------------------------------------------------------------
TapTap measures a voucher on the wall clock: first login → ``expires_at``. RouterOS measures
``limit-uptime`` against the user's ``uptime`` counter, which is the SUM of all its sessions and is never
reset. With 3 phones online for 1 hour a family voucher burns 3 hours on the router. Old code set
``limit-uptime`` to the wall-clock span (expires_at − first login), so multi-device vouchers and members hit
the router's limit long before TapTap's end, and the expiry pass then marked them expired in TapTap
*because the router said so*. Free slot in TapTap, refused by the router.

The rule
--------
    limit-uptime = uptime already counted on the router + time left in TapTap × device slots

"Device slots" is the larger of the voucher's own devices and the shared-users of the profile it is on
ON THE ROUTER. The router counts every concurrent session, so a 1-device voucher on a profile that allows
3 sessions (an older/Mikhmon profile, a hand-made one, "unlimited", or sticky ghosts of a phone that changed
its MAC) burns 3 hours per hour: a 24 h voucher was cut after 8 h with 16 h still left in TapTap.

When the router's counter is not known yet, the largest value it could have is assumed (time since first
login × device slots), so an unknown counter can never cause an early cut either. For a one-device voucher
this gives exactly the old value (its whole period), so nothing changes on routers for those; only
multi-device vouchers and members get the extra room they always needed.

However the devices take turns, the router can never cut a customer before
TapTap's end. TapTap's strict expiry (core/expiry.py) still switches the voucher off at exactly its end,
and if a router loses contact with TapTap it still stops the customer by itself, at most "slots × time left"
later. A voucher with no end is unlimited (0s). A voucher whose time is up keeps limit = its uptime, so
the router refuses it too.

``acceptable()`` is the drift test every sync path uses: the router is fine while its headroom
(limit − uptime) covers time left × slots and is not wildly larger. Anything else is re-sent.

Used by durations.router_limit (all pushes), member_time.expected_limit / router_matches (members),
the expiry pass (API routers, live) and the Link inventory check (TapTap Link routers).
"""
from __future__ import annotations

import logging
import math
import zlib

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('taptap.router_time')

UNLIMITED_SHARED = 10      # shared-users=unlimited on the router: assume this many sessions at once
SHARED_TTL = 300
TOLERANCE_S = 120          # headroom may be this much short before it counts as drift
REPAIR_EVERY_S = 600       # one repair per voucher per 10 minutes, at most
UPTIME_TTL = 3 * 86400
SESSION_TTL = 300          # live-session uptimes from the last Link check-in / API pass
NOTE_EVERY_S = 6 * 3600    # one "Router time corrected" history note per voucher per reason per 6 hours
STUBBORN_AFTER = 3         # the same voucher losing its limit this many times in a day → tell the owner once


def secs(value):
    from .sync import _routeros_seconds
    try:
        return int(_routeros_seconds(value) or 0)
    except Exception:
        return 0


def routeros_text(seconds):
    from .durations import UNLIMITED_ROUTEROS, routeros
    seconds = int(seconds or 0)
    return UNLIMITED_ROUTEROS if seconds <= 0 else routeros(max(1, math.ceil(seconds / 60)))


def _profile_shared(row):
    """shared-users of a mirrored router profile (unlimited → UNLIMITED_SHARED)."""
    raw = str((row.raw_data or {}).get('shared-users', (row.raw_data or {}).get('shared_users', '')) or '').strip().lower()
    if raw in ('unlimited', 'none'):
        return UNLIMITED_SHARED
    try:
        n = int(raw) if raw else int(row.shared_users or 1)
    except (TypeError, ValueError):
        n = int(row.shared_users or 1)
    return max(1, min(n, UNLIMITED_SHARED))


def router_shared(v, plan=None):
    """How many sessions the ROUTER lets this voucher run at once (its profile's shared-users). None = unknown."""
    router_id = getattr(v, 'router_id', None)
    code = str(getattr(v, 'code', '') or '').upper()
    if not router_id or not code:
        return None
    key = f'tt:rt:sh:{router_id}:{code}'
    hit = cache.get(key)
    if hit is not None:
        return int(hit) or None
    found = 0
    try:
        from .models import RouterHotspotProfile, RouterHotspotUser
        name = (RouterHotspotUser.objects.filter(router_id=router_id, username__iexact=code, is_present=True)
                .values_list('profile', flat=True).first())
        if not name:
            from .utils import voucher_profile
            name = voucher_profile(v, plan)[0]
        row = RouterHotspotProfile.objects.filter(router_id=router_id, name=name, is_present=True).first() if name else None
        if row is not None:
            found = _profile_shared(row)
    except Exception:
        logger.debug('router shared-users for %s', code, exc_info=True)
    cache.set(key, found, SHARED_TTL)
    return found or None


def slots(v, plan=None):
    """Sessions that can burn the router's uptime counter at the same time: the voucher's devices, or more
    when the router profile it is on allows more (the router counts what IT allows, not what TapTap sold)."""
    own = max(1, int(getattr(v, 'max_devices', 1) or 1))
    return max(own, router_shared(v, plan) or 1)


def platform_owned(v):
    """TapTap's clock is the authority for this voucher (it made it, or it is a member)."""
    return getattr(v, 'source', 'taptap') == 'taptap' or getattr(v, 'login_type', '') == 'member'


def remaining_s(v, plan=None, now=None):
    """Seconds of TapTap time left. None = no limit at all."""
    now = now or timezone.now()
    if v.expires_at:
        return max(0, int((v.expires_at - now).total_seconds()))
    minutes = int(v.duration_minutes or 0)
    if plan is not None and not v.used_at and getattr(v, 'source', 'taptap') == 'taptap' and getattr(v, 'login_type', '') != 'member':
        minutes = int(getattr(plan, 'duration_minutes', 0) or 0)      # unused vouchers follow the current plan
    if not minutes:
        return None
    if v.used_at:                                                    # started, end not stored yet
        return max(0, int((v.used_at.timestamp() + minutes * 60) - now.timestamp()))
    return minutes * 60


# ─────────────────────────────── the router's uptime counter ───────────────────────────────

def _key(router_id, code):
    return f'tt:rt:up:{router_id}:{str(code).upper()}'


def note_uptime(router_id, code, uptime_text):
    """Remember the freshest uptime the router reported for a user (live API rows, inventory rows)."""
    if router_id and code and str(uptime_text or '').strip():     # blank = not reported: never cache it as 0
        cache.set(_key(router_id, code), secs(uptime_text), UPTIME_TTL)


def uptime_s(v):
    """The router's uptime counter for this voucher (live cache, else inventory mirror). None = unknown."""
    if not getattr(v, 'router_id', None):
        return None
    hit = cache.get(_key(v.router_id, v.code))
    if hit is not None:                 # refreshed by every live pass / inventory: freshest, and no query
        return int(hit)
    try:
        from .models import RouterHotspotUser
        text = RouterHotspotUser.objects.filter(router_id=v.router_id, username__iexact=v.code).values_list('uptime', flat=True).first()
    except Exception:
        text = None
    return secs(text) if str(text or '').strip() else None      # blank = the router did not report it


def _skey(router_id, code):
    return f'tt:rt:sess:{router_id}:{str(code).upper()}'


def note_sessions(router_id, rows):
    """Remember how long each user's CURRENT sessions have run (Link check-in rows / API active rows).

    RouterOS adds a session's time to /ip/hotspot/user ``uptime`` only when the session ENDS, and fixes the
    session's own time left at LOGIN (limit-uptime − uptime). A limit written while a customer is online is
    therefore judged at the next login against an uptime that has grown by the whole session in between.
    Without this, the repair set ``limit = uptime(0) + time left`` and the customer was cut that much early
    at the next login — and the next inventory "corrected" the voucher again, every login, for every voucher."""
    if not router_id:
        return
    total = {}
    for r in rows or []:
        name = str(r.get('user', '') or r.get('name', '')).strip().upper()
        if name and not name.startswith('T-'):
            total[name] = total.get(name, 0) + secs(r.get('uptime', ''))
    cache.set_many({_skey(router_id, n): t for n, t in total.items()}, SESSION_TTL)
    cache.set(f'tt:rt:sess-seen:{router_id}', 1, SESSION_TTL)


def session_s(v):
    """Seconds the voucher's live sessions have run (not yet in the router's uptime counter). 0 = none/unknown."""
    router_id = getattr(v, 'router_id', None)
    if not router_id:
        return 0
    hit = cache.get(_skey(router_id, v.code))
    if hit is not None:
        return int(hit)
    if cache.get(f'tt:rt:sess-seen:{router_id}') is None:
        try:                                    # TapTap Link routers: the last check-in's session list
            from .linklive import sessions
            rows = sessions(v.router) if getattr(v, 'router', None) is not None else []
            if rows:
                note_sessions(router_id, rows)
                return int(cache.get(_skey(router_id, v.code)) or 0)
        except Exception:
            logger.debug('live sessions for %s', v.code, exc_info=True)
    return 0


def counted(v, uptime_text):
    """What the router will have counted by the next login: its uptime counter + the sessions still running.
    None when the router did not report the counter (blank) — the caller then assumes the most it could be,
    as the module rule says. Never 0 for "unknown": that is what let a repair cut customers early."""
    if uptime_text is None or not str(uptime_text).strip():
        return None
    return secs(uptime_text) + session_s(v)


def assumed_uptime(v, now=None, plan=None):
    """The most the router can have counted: time since the period started × device slots."""
    now = now or timezone.now()
    start = v.used_at
    if v.expires_at and v.duration_minutes and not start:
        from datetime import timedelta
        start = v.expires_at - timedelta(minutes=int(v.duration_minutes))
    if not start:
        return 0
    return max(0, int((now - start).total_seconds())) * slots(v, plan)


# ─────────────────────────────── the rule ───────────────────────────────

def limit_seconds(v, plan=None, uptime=None, now=None):
    rem = remaining_s(v, plan, now)
    if rem is None:
        return 0
    if uptime is None:
        uptime = uptime_s(v)
        if uptime is not None:
            uptime += session_s(v)          # sessions still running are not in the router's counter yet
    up = assumed_uptime(v, now, plan) if uptime is None else int(uptime or 0)
    if rem <= 0:
        return max(60, up)                       # time is up: the router refuses it as well
    return up + rem * slots(v, plan)


def limit_for(v, plan=None, uptime=None, now=None):
    """The limit-uptime text the router must have for this voucher right now."""
    return routeros_text(limit_seconds(v, plan, uptime, now))


def acceptable(v, have_text, uptime_text=None, plan=None, now=None):
    """Does the router's limit-uptime agree with TapTap? (the drift test)"""
    rem = remaining_s(v, plan, now)
    have = secs(have_text)
    if rem is None:
        return have == 0
    if rem <= 0:
        return True                              # expired: the expiry pass disables it; the limit no longer matters
    if have <= 0:
        return False                             # router says "no limit" for a voucher that has an end
    up = counted(v, uptime_text) if uptime_text is not None else None
    if up is None:
        up = uptime_s(v)
        up = assumed_uptime(v, now, plan) if up is None else up + session_s(v)
    head = have - up
    need = rem * slots(v, plan)
    return need - TOLERANCE_S <= head <= need + max(need, 86400) + TOLERANCE_S


def why(v, have_text, uptime_text=None, plan=None, now=None):
    """Plain words for the history when the router disagreed."""
    rem = remaining_s(v, plan, now)
    have = secs(have_text)
    up = counted(v, uptime_text) if uptime_text is not None else None
    if up is None:
        up = uptime_s(v)
        up = assumed_uptime(v, now, plan) if up is None else up + session_s(v)
    if rem is None:
        return f'router had limit {have_text or "0s"} for a voucher without an end'
    if have <= 0:
        return 'router had no time limit'
    left = max(0, have - up)
    return (f'router allowed {routeros_text(left) if left else "nothing"} more (uptime {routeros_text(up) if up else "0s"} of '
            f'{have_text}), TapTap has {routeros_text(rem)} left × {slots(v, plan)} session{"s" if slots(v, plan) != 1 else ""}')


# ─────────────────────────────── repairs ───────────────────────────────

def _once(router_id, code):
    return cache.add(f'tt:rt:fix:{router_id}:{str(code).upper()}', 1, REPAIR_EVERY_S)


def _stubborn(v, reason):
    """Count "router had no time limit" repairs per voucher per day. The rule only ever WRITES a limit, so a
    voucher that keeps coming back at 0s means something on the router keeps removing it (a scheduler or
    on-login script, Mikhmon, another app, a reset/restore). Tell the owner once a day per router."""
    if reason != 'router had no time limit' or not getattr(v, 'router_id', None):
        return
    key = f'tt:rt:zero:{v.router_id}:{v.code.upper()}'
    cache.add(key, 0, 86400)
    try:
        n = cache.incr(key)
    except ValueError:
        cache.set(key, 1, 86400)
        n = 1
    if n == STUBBORN_AFTER and cache.add(f'tt:rt:zero-told:{v.router_id}', 1, 86400):
        try:
            from .notify import notify
            notify(v.business, 'router_time', f'{v.router.name} keeps removing voucher time limits',
                   f'{v.code} (and maybe others) lost its limit-uptime on the router {n} times today, although '
                   'TapTap only ever sets one. Something on the router clears it: check System › Scheduler and '
                   'Scripts, the on-login scripts of the hotspot user profiles, Mikhmon or another app using the '
                   'same router, or a backup being restored. TapTap still ends every voucher on time.',
                   severity='warning', link='/routers/', key=f'router:{v.router_id}:time-cleared')
        except Exception:
            logger.exception('stubborn router time notice for %s', v.code)


def _record(v, before, after, reason, via):
    """One history note per voucher and reason every NOTE_EVERY_S: the repair itself still runs every time,
    but a voucher's history no longer fills up with the same line after every sync."""
    _stubborn(v, reason)
    if not cache.add(f'tt:rt:note:{v.pk}:{zlib.crc32(reason[:40].encode())}', 1, NOTE_EVERY_S):
        logger.info('router time %s: %s → %s (%s) — note already written', v.code, before or '0s', after, reason)
        return
    try:
        from .voucher_history import record
        record(v, 'note', source='auto', via=via, router_result=f'limit-uptime {before or "0s"} → {after}',
               text=f'Router time corrected to match TapTap ({reason})')
    except Exception:
        logger.exception('history for %s', v.code)


def _plans(business):
    return {p.name: p for p in business.plans.all()}


def repair_api(router, svc, rows, now=None):
    """API / Tunnel routers, from the fresh /ip/hotspot/user rows of a live pass. Returns repairs made.
    Only active TapTap vouchers/members are touched; the router's own users are left to the router."""
    from .models import Voucher
    from .voucher_history import channel
    now = now or timezone.now()
    by_name = {}
    for row in rows or []:
        name = str(row.get('name', '')).strip()
        if name:
            by_name[name.upper()] = row
            note_uptime(router.pk, name, row.get('uptime', ''))
    if not by_name:
        return 0
    vouchers = [v for v in Voucher.objects.filter(business=router.business, router=router, status='active', frozen_at__isnull=True,
                                                  deleted_at__isnull=True).select_related('router')
                if platform_owned(v) and v.code.upper() in by_name]
    plans, fixed, via = _plans(router.business), 0, channel(router)
    res = svc.resource('/ip/hotspot/user')
    for v in vouchers:
        row = by_name[v.code.upper()]
        have = str(row.get('limit-uptime', row.get('limit_uptime', '')) or '')
        plan = plans.get(v.plan_name)
        if acceptable(v, have, row.get('uptime', ''), plan, now) or not row.get('id') or not _once(router.pk, v.code):
            continue
        want = limit_for(v, plan, counted(v, row.get('uptime', '')), now)
        try:
            res.set(id=row['id'], limit_uptime=want)
            fixed += 1
            _record(v, have, want, why(v, have, row.get('uptime', ''), plan, now), via)
        except Exception as exc:
            logger.warning('router time fix %s on %s: %s', v.code, router.name, exc)
    return fixed


def repair_link(router, now=None):
    """TapTap Link routers, right after a real /ip/hotspot/user inventory was stored. Queues one compact
    limit-only command for every drifted voucher. Returns how many were queued."""
    from .agent import queue
    from .models import RouterHotspotUser, Voucher
    now = now or timezone.now()
    mirrors = {m.username.upper(): m for m in RouterHotspotUser.objects.filter(router=router, is_present=True)}
    if not mirrors:
        return 0
    plans, users, notes = _plans(router.business), [], []
    from .models import AgentCommand
    waiting = {str(u.get('n', '')).upper()                      # a correction still on its way: do not queue another
               for c in AgentCommand.objects.filter(router=router, kind='hotspot_users_limit', status__in=['queued', 'sent'])
               if (c.params or {}).get('router_time') for u in (c.params or {}).get('users') or []}
    for v in Voucher.objects.filter(business=router.business, router=router, status='active', frozen_at__isnull=True,
                                    deleted_at__isnull=True):
        if not platform_owned(v):
            continue
        m = mirrors.get(v.code.upper())
        if m is None or v.code.upper() in waiting:
            continue
        note_uptime(router.pk, v.code, m.uptime)
        plan = plans.get(v.plan_name)
        if acceptable(v, m.limit_uptime, m.uptime, plan, now) or not _once(router.pk, v.code):
            continue
        want = limit_for(v, plan, counted(v, m.uptime), now)
        users.append({'n': m.username, 'lim': want})     # the router's own spelling: [find name=] is case-sensitive
        notes.append((v, m.limit_uptime, want, why(v, m.limit_uptime, m.uptime, plan, now)))
    for i in range(0, len(users), 60):
        part = users[i:i + 60]
        queue(router, 'hotspot_users_limit', {'users': part, 'profiles': [], 'router_time': True},
              label=f'Match router time to TapTap ({len(part)} voucher{"s" if len(part) != 1 else ""})', minutes=60)
    for v, before, after, reason in notes:
        RouterHotspotUser.objects.filter(router=router, username__iexact=v.code).update(limit_uptime=after)
        _record(v, before, after, reason, 'TapTap Link')
    return len(users)
