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

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('taptap.router_time')

TOLERANCE_S = 120          # headroom may be this much short before it counts as drift
REPAIR_EVERY_S = 600       # one repair per voucher per 10 minutes, at most
UPTIME_TTL = 3 * 86400


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


def slots(v):
    return max(1, int(getattr(v, 'max_devices', 1) or 1))


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
    if router_id and code:
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


def assumed_uptime(v, now=None):
    """The most the router can have counted: time since the period started × device slots."""
    now = now or timezone.now()
    start = v.used_at
    if v.expires_at and v.duration_minutes and not start:
        from datetime import timedelta
        start = v.expires_at - timedelta(minutes=int(v.duration_minutes))
    if not start:
        return 0
    return max(0, int((now - start).total_seconds())) * slots(v)


# ─────────────────────────────── the rule ───────────────────────────────

def limit_seconds(v, plan=None, uptime=None, now=None):
    rem = remaining_s(v, plan, now)
    if rem is None:
        return 0
    if uptime is None:
        uptime = uptime_s(v)
    up = assumed_uptime(v, now) if uptime is None else int(uptime or 0)
    if rem <= 0:
        return max(60, up)                       # time is up: the router refuses it as well
    return up + rem * slots(v)


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
    if uptime_text is None:
        up = uptime_s(v)
        up = assumed_uptime(v, now) if up is None else up
    else:
        up = secs(uptime_text)
    head = have - up
    need = rem * slots(v)
    return need - TOLERANCE_S <= head <= need + max(need, 86400) + TOLERANCE_S


def why(v, have_text, uptime_text=None, plan=None, now=None):
    """Plain words for the history when the router disagreed."""
    rem = remaining_s(v, plan, now)
    have, up = secs(have_text), (uptime_s(v) if uptime_text is None else secs(uptime_text))
    up = assumed_uptime(v, now) if up is None else up
    if rem is None:
        return f'router had limit {have_text or "0s"} for a voucher without an end'
    if have <= 0:
        return 'router had no time limit'
    left = max(0, have - up)
    return (f'router allowed {routeros_text(left) if left else "nothing"} more (uptime {routeros_text(up) if up else "0s"} of '
            f'{have_text}), TapTap has {routeros_text(rem)} left × {slots(v)} device{"s" if slots(v) != 1 else ""}')


# ─────────────────────────────── repairs ───────────────────────────────

def _once(router_id, code):
    return cache.add(f'tt:rt:fix:{router_id}:{str(code).upper()}', 1, REPAIR_EVERY_S)


def _record(v, before, after, reason, via):
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
        want = limit_for(v, plan, secs(row.get('uptime', '')), now)
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
    for v in Voucher.objects.filter(business=router.business, router=router, status='active', frozen_at__isnull=True,
                                    deleted_at__isnull=True):
        if not platform_owned(v):
            continue
        m = mirrors.get(v.code.upper())
        if m is None:
            continue
        note_uptime(router.pk, v.code, m.uptime)
        plan = plans.get(v.plan_name)
        if acceptable(v, m.limit_uptime, m.uptime, plan, now) or not _once(router.pk, v.code):
            continue
        want = limit_for(v, plan, secs(m.uptime), now)
        users.append({'n': v.code, 'lim': want})
        notes.append((v, m.limit_uptime, want, why(v, m.limit_uptime, m.uptime, plan, now)))
    for i in range(0, len(users), 60):
        part = users[i:i + 60]
        queue(router, 'hotspot_users_limit', {'users': part, 'profiles': [], 'router_time': True},
              label=f'Match router time to TapTap ({len(part)} voucher{"s" if len(part) != 1 else ""})', minutes=60)
    for v, before, after, reason in notes:
        RouterHotspotUser.objects.filter(router=router, username__iexact=v.code).update(limit_uptime=after)
        _record(v, before, after, reason, 'TapTap Link')
    return len(users)
