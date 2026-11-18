"""How hard TapTap leans on a Direct API / TapTap Tunnel router — and leaning less, so vouchers always get through.

Every live pass (every LIVE_WATCH_SECONDS, 15 s) used to read the router's WHOLE hotspot user table: every voucher
ever put on it, sold or not, thousands of rows on a big site — four times a minute, day and night. On a busy
MikroTik that print alone kept the CPU up and made the API slow to answer, and a voucher sent at that moment
waited behind it ("router busy", sometimes "offline" for a moment).

A pass now reads the full table only when it is useful, and the rest of the time works from what it already has:

* **Light pass** (most passes): the active sessions and IP bindings only — that is where everything urgent shows:
  a voucher logging in for the first time (activated from its session), a voucher whose time is up still online
  (switched off at once), fair usage, device locks. Expired vouchers are switched off from TapTap's own copy of the
  router's table (``light_rows``), refreshed by every full read and by every switch-off TapTap makes.
* **Full read**: the whole table — at the router's own pace (``full_every``): every pass on a small router, every
  1–3 minutes on a big one, and further apart while the router answers slowly (it is busy: give it room). At once
  when the sessions show a code TapTap does not know (a voucher made on the router, to import), or when someone
  asks (force / Sync).

Also: the hotspot host list (every connected device) is read only when the router has bypassed IP bindings — the
only thing it was read for.
"""
from __future__ import annotations

import time

from django.core.cache import cache

# (most users on the router, seconds between full reads of its user table)
FULL_EVERY = ((300, 0), (1500, 60), (5000, 120), (None, 180))
SLOW_READ_S = 3.0          # a full read slower than this: the router is busy — read half as often
VERY_SLOW_READ_S = 8.0     # … slower than this: a quarter as often
MAX_EVERY = 300            # never fewer than one full read every 5 minutes
STATE_TTL = 6 * 3600


def _k(router_id, what):
    return f'tt:load:{what}:{router_id}'


def table_size(router):
    """Users on the router at the last full read (TapTap's mirror when not known yet)."""
    n = cache.get(_k(router.pk, 'size'))
    if n is not None:
        return int(n)
    try:
        from .models import RouterHotspotUser
        n = RouterHotspotUser.objects.filter(router=router, is_present=True).count()
    except Exception:
        n = 0
    cache.set(_k(router.pk, 'size'), n, 600)
    return n


def full_every(router):
    """Seconds between full reads of this router's user table: by its size, stretched while it answers slowly."""
    n = table_size(router)
    every = next(s for limit, s in FULL_EVERY if limit is None or n <= limit)
    took = cache.get(_k(router.pk, 'took'))
    if took is not None and every:
        if took >= VERY_SLOW_READ_S:
            every *= 4
        elif took >= SLOW_READ_S:
            every *= 2
    elif took is not None and took >= SLOW_READ_S:
        every = 60 if took < VERY_SLOW_READ_S else 120     # a small table that still reads slowly: a busy router
    return min(MAX_EVERY, every)


def full_due(router, *, force=False, unknown_codes=False, now=None):
    """Read the whole user table on this pass?"""
    if force or unknown_codes:
        return True
    last = cache.get(_k(router.pk, 'full'))
    if last is None:
        return True
    now = now or time.time()
    return now - float(last) >= full_every(router)


def note_full_read(router, seconds, rows):
    """A full read happened: when, how long it took, how many users."""
    cache.set_many({_k(router.pk, 'full'): time.time(), _k(router.pk, 'took'): round(float(seconds), 2),
                    _k(router.pk, 'size'): int(rows)}, STATE_TTL)


def note_light_pass(router):
    key = _k(router.pk, 'light')
    if not cache.add(key, 1, 86400):
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, 86400)


def light_rows(router):
    """TapTap's copy of the router's user table, shaped like router rows (name, disabled, uptime, limit-uptime) —
    what a light pass switches expired vouchers off from."""
    from .models import RouterHotspotUser
    out = []
    for name, disabled, uptime, limit in (RouterHotspotUser.objects.filter(router=router, is_present=True)
                                          .values_list('username', 'disabled', 'uptime', 'limit_uptime')):
        out.append({'name': name, 'disabled': 'true' if disabled else 'false', 'uptime': uptime or '',
                    'limit-uptime': limit or ''})
    return out


def needs_hosts(bindings):
    """Read the hotspot host list only when there are bypassed IP bindings (the only devices it is read for)."""
    for b in bindings or []:
        if str(b.get('type', '')).strip() == 'bypassed' and str(b.get('disabled', 'false')).lower() not in ('true', 'yes'):
            return True
    return False


def summary(router):
    """For the Routers page: how TapTap reads this router now, and today's voucher-time decisions."""
    try:
        from .router_time import tally
        t = tally(router.pk)
    except Exception:
        t = {'urgent': 0, 'checkpoint': 0, 'wait': 0}
    out = {'time': t, 'link': getattr(router, 'connection_mode', 'api') == 'agent'}
    if not out['link']:
        took = cache.get(_k(router.pk, 'took'))
        out.update({'every': full_every(router), 'size': table_size(router),
                    'took': took, 'slow': took is not None and took >= SLOW_READ_S})
    return out
