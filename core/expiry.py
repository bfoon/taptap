"""Strict expiry: when a voucher's time runs out it is switched off at once — made in TapTap or on the router.

A voucher's time has run out when either:

* **calendar** — its end (``expires_at``, or first use + duration) has passed. This is TapTap's rule for every
  voucher and member, the same one the voucher pages show as "Time ran out"; or
* **router uptime** — the router shows it has used all of its ``limit-uptime``.

Unlimited vouchers (no duration) never run out; frozen vouchers are skipped (their clock stands still).

What happens, every live pass (``core.live.watch_all``, every ``LIVE_WATCH_SECONDS``, default 15 s) and for
**every** business — not only those with live sync switched on:

1. the voucher is marked ``expired`` in TapTap and a "Time ran out" entry is added to its history;
2. it is **disabled on the router and its sessions are dropped** — over the RouterOS API (direct or TapTap
   Tunnel) or, for TapTap Link routers, with one batch command per 100 vouchers;
3. **nothing escapes**: any voucher that is expired in TapTap but still enabled on its router is pushed
   again until the router confirms it — after the router was offline, after a failed command, or after
   someone re-enabled it by hand in WinBox. To give more time, use "Add time" in TapTap: it re-enables
   the voucher properly;
4. a session of an expired voucher is disconnected immediately — no grace period, whatever the
   auto-enforce setting (see ``live.watch_router`` and ``agent.ingest_sessions``).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger('taptap.expiry')
LINK_REQUEUE_SECONDS = 600      # a queued Link disable is sent again if the router has not confirmed it by then
API_RETRY_SECONDS = 60          # routers not live-watched: at most one connection attempt a minute
LINK_BATCH = 100


def _seconds(value):
    from .sync import _routeros_seconds
    return _routeros_seconds(value)


def uptime_used_up(row):
    """True when a RouterOS hotspot user row has used all of its limit-uptime."""
    limit = _seconds((row or {}).get('limit-uptime', ''))
    return limit > 0 and _seconds((row or {}).get('uptime', '')) >= limit


def fill_missing_ends(qs):
    """Give used vouchers without a stored end one (first use + duration), so the sweep sees them."""
    from .models import Voucher
    n = 0
    for v in qs.filter(expires_at__isnull=True, used_at__isnull=False, duration_minutes__gt=0).only('pk', 'used_at', 'duration_minutes')[:5000]:
        n += Voucher.objects.filter(pk=v.pk, expires_at__isnull=True).update(expires_at=v.used_at + timedelta(minutes=v.duration_minutes))
    return n


def _running(qs):
    return qs.filter(status='active', frozen_at__isnull=True)


def mark_expired(vouchers, now, why, via, detail=''):
    """Set status 'expired' (only if still active) and record it. Returns the vouchers that changed."""
    from .live import push_event
    from .models import Voucher
    from .voucher_history import record
    changed = []
    for v in vouchers:
        if not Voucher.objects.filter(pk=v.pk, status='active', frozen_at__isnull=True).update(status='expired'):
            continue
        before, v.status = 'active', 'expired'
        record(v, 'time_up', source='auto', via=via, reason=why, status_before=before, status_after='expired',
               router_result='Switching off on the router' if v.router_id else 'No router — marked expired in TapTap', text=detail)
        changed.append(v)
    if changed:
        biz = changed[0].business_id
        names = ', '.join(v.code for v in changed[:5]) + (f' and {len(changed) - 5} more' if len(changed) > 5 else '')
        push_event(biz, f'Time ran out: {names} — switched off', 'fix')
    return changed


# ─────────────────────────── inside the live pass (API routers, live rows at hand) ───────────────────────────
def enforce_on_router(router, svc, users, active, now):
    """Called by ``live.watch_router`` with the rows it just read. Returns the codes switched off / kicked."""
    from .models import RouterHotspotUser, Voucher
    from .voucher_history import channel
    via = channel(router)
    rows = {str(r.get('name', '')).strip().upper(): r for r in users if r.get('name')}
    online = {str(s.get('user', '')).strip().upper() for s in active if s.get('user')}
    base = Voucher.objects.filter(business=router.business)
    fill_missing_ends(base.filter(router=router))
    # 1. calendar: the end has passed
    due = list(_running(base.filter(router=router, expires_at__lte=now)))
    newly = mark_expired(due, now, 'Time ran out', via, f'on {router.name}')
    # 2. router uptime: the router shows the whole limit-uptime used
    used_up = [n for n, r in rows.items() if uptime_used_up(r)]
    if used_up:
        codes = {c.upper(): c for c in used_up}
        cands = [v for v in _running(base.filter(Q(router=router) | Q(router__isnull=True)))
                 .filter(code__in=list(codes) + [n.lower() for n in codes]) if v.code.upper() in rows]
        newly += mark_expired(cands, now, 'Used all its time on the router (uptime limit reached)', via, f'on {router.name}')
    # 3. every expired voucher on this router: disabled there, and no session left — retried each pass until true
    switched = set()
    for v in base.filter(router=router, status='expired', frozen_at__isnull=True).only('pk', 'code'):
        key = v.code.upper()
        row = rows.get(key)
        enabled = row is not None and str(row.get('disabled', 'false')).lower() not in ('true', 'yes')
        if not enabled and key not in online:
            continue
        try:
            if enabled:
                svc.disable_voucher(v.code)
                RouterHotspotUser.objects.filter(router=router, username__iexact=v.code).update(disabled=True)
            if key in online:
                svc.reset_active_by_name(v.code)
            switched.add(key)
        except Exception as exc:     # try again next pass
            logger.warning('expiry: could not switch off %s on %s: %s', v.code, router.name, exc)
    return switched, len(newly)


# ─────────────────────────── the sweep (every business, every pass) ───────────────────────────
def _confirmed_key(router_id, code):
    return f'tt:exp:done:{router_id}:{code.upper()}'


def _needs_router_action(v, mirror):
    """Still enabled on its router, as far as TapTap's copy of the router knows."""
    m = mirror.get((v.router_id, v.code.upper()))
    if m is not None:
        return m.is_present and not m.disabled
    return not cache.get(_confirmed_key(v.router_id, v.code))   # not in the copy: push once, then trust the router's answer


def sweep(business=None, now=None, watched=(), recheck=False):
    """Expire every voucher whose time is up and make sure its router switched it off.

    ``watched``: router ids the live pass already handled with fresh rows this round (skipped here).
    ``recheck``: re-check every expired voucher now (otherwise that full check runs once a minute)."""
    from .linkops import uses_link
    from .models import Router, RouterHotspotUser, Voucher
    from .voucher_history import channel
    now = now or timezone.now()
    qs = Voucher.objects.all() if business is None else Voucher.objects.filter(business=business)
    fill_missing_ends(qs)
    summary = {'expired': 0, 'pushed': 0, 'queued': 0}
    # 1. mark every voucher whose calendar time is up — with or without a router, online or not
    due = list(_running(qs.filter(expires_at__lte=now)).select_related('router', 'business')[:5000])
    by_router = {}
    for v in due:
        by_router.setdefault(v.router_id, []).append(v)
    for rid, vs in by_router.items():
        via = channel(vs[0].router) if rid else 'TapTap only'
        summary['expired'] += len(mark_expired(vs, now, 'Time ran out', via))
    # 2. every expired voucher still enabled on its router (per TapTap's copy of the router): switch it off.
    #    Newly expired ones go out now; the full re-check of older ones runs once a minute (it can be large).
    pending = qs.filter(status='expired', frozen_at__isnull=True, router__isnull=False).exclude(router_id__in=list(watched))
    gate = f'tt:exp:recheck:{business.pk if business else "all"}'
    if not recheck and not cache.add(gate, 1, 60):
        pending = pending.filter(pk__in=[v.pk for v in due])
    mirror = {(m.router_id, m.username.upper()): m for m in RouterHotspotUser.objects.filter(
        router_id__in=pending.values('router_id'), username__in=pending.values('code'))}
    todo = {}
    for v in pending.only('pk', 'code', 'router_id')[:20000]:
        if _needs_router_action(v, mirror):
            todo.setdefault(v.router_id, []).append(v)
    for router in Router.objects.filter(pk__in=list(todo)):
        vs = todo[router.pk]
        if uses_link(router):
            summary['queued'] += _queue_link(router, vs)
        else:
            summary['pushed'] += _push_api(router, vs)
    return summary


def _queue_link(router, vouchers):
    """TapTap Link: batch-disable (also drops sessions). Re-sent only if not confirmed after 10 minutes."""
    from .linkops import send
    names = [v.code for v in vouchers if cache.add(f'tt:exp:link:{router.pk}:{v.code.upper()}', 1, LINK_REQUEUE_SECONDS)]
    queued = 0
    for i in range(0, len(names), LINK_BATCH):
        part = names[i:i + LINK_BATCH]
        try:
            send(router, 'hotspot_users_disable', {'names': part, 'disabled': True, 'reason': 'expired'},
                 label=f'Switch off {len(part)} voucher{"s" if len(part) != 1 else ""} whose time ran out')
            queued += len(part)
        except ValueError as exc:     # Link offline: allow a new try at the next pass
            logger.info('expiry (link) %s: %s', router.name, exc)
            for n in part:
                cache.delete(f'tt:exp:link:{router.pk}:{n.upper()}')
            break
    return queued


def _push_api(router, vouchers):
    """Direct API routers not covered by the live pass: one connection, disable + drop sessions."""
    from .mikrotik import MikroTikService
    from .models import RouterHotspotUser
    if not cache.add(f'tt:exp:api:{router.pk}', 1, API_RETRY_SECONDS):
        return 0
    done = 0
    try:
        svc = MikroTikService(router, timeout=getattr(settings, 'MIKROTIK_LIVE_TIMEOUT', 5)).connect()
    except Exception as exc:          # offline: the next pass (a minute later) tries again
        logger.info('expiry (api) %s offline: %s', router.name, exc)
        return 0
    try:
        for v in vouchers:
            try:
                svc.disable_voucher(v.code)
                svc.reset_active_by_name(v.code)
                RouterHotspotUser.objects.filter(router=router, username__iexact=v.code).update(disabled=True)
                cache.set(_confirmed_key(router.pk, v.code), 1, 86400)
                done += 1
            except Exception as exc:
                logger.warning('expiry: could not switch off %s on %s: %s', v.code, router.name, exc)
    finally:
        svc.close()
    return done


def link_ack(cmd, ok):
    """TapTap Link confirmed (or refused) a batch disable sent for expired vouchers."""
    from .models import RouterHotspotUser
    names = [str(n) for n in cmd.params.get('names', [])]
    if ok and names:
        for n in names:
            RouterHotspotUser.objects.filter(router=cmd.router, username__iexact=n).update(disabled=True)
            cache.set(_confirmed_key(cmd.router_id, n), 1, 86400)
    elif not ok:
        for n in names:   # refused: allow an immediate new try
            cache.delete(f'tt:exp:link:{cmd.router_id}:{n.upper()}')
