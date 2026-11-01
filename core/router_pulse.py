"""Router pulse — TapTap checks the connection to every router every few seconds.

What people saw
---------------
The header pill turned orange — "Live · 1m ago", then "1 router offline" — although the router was fine. The only
connection check was the full live sync: a heavy pass (sessions, users, bindings, device scan…) that ran over the
businesses one after another and waited for its slowest router. One router answering slowly, or a voucher push
occupying the live worker, held up the next check of every router, so their "last checked" time aged past a minute.
And a router marked offline was retried only once a minute, so "offline" lingered after it was reachable again.

What this does
--------------
* Every ``ROUTER_PULSE_SECONDS`` (10) every Direct API / Tunnel router gets a quick knock on its API port — all
  routers in parallel, ``ROUTER_PULSE_TIMEOUT`` (4 s) each, no login (so the router's log does not fill with
  "logged in via api" lines). TapTap Link routers need none: their check-in every few seconds is their pulse.
* A knock that fails counts toward "offline" exactly like a failed live check (several in a row over 40 s+,
  core/live.note_failure); a knock that answers on a router marked offline starts a full check at once, so it is
  back on the dashboard within seconds instead of up to a minute.
* ``seen()`` — when TapTap last confirmed a router reachable (pulse, live check or Link check-in) — is what the
  header pill shows as "Live · …", so it reflects the connection, not the speed of the slowest full sync.
"""
from __future__ import annotations

import logging
import socket
from concurrent.futures import ThreadPoolExecutor, wait

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('taptap.pulse')


def every():
    return max(5, int(getattr(settings, 'ROUTER_PULSE_SECONDS', 10)))


def timeout():
    return max(1.0, float(getattr(settings, 'ROUTER_PULSE_TIMEOUT', 4)))


def target(router):
    """(host, port) of the router's API as TapTap reaches it, or None (TapTap Link: check-ins are the pulse)."""
    if getattr(router, 'connection_mode', 'api') == 'agent':
        return None
    host = str(router.ip_address or '').strip()
    port = int(router.api_port or (8729 if router.use_ssl else 8728))
    if host.count(':') == 1 and not host.startswith('['):        # "host:port" typed into the address field
        host, _, maybe = host.partition(':')
        if maybe.isdigit():
            port = int(maybe)
    host = host.strip('[]').strip()
    return (host, port) if host else None


def knock(host, port, secs=None):
    """Can TapTap open a TCP connection to the router's API? (No login: nothing is written to the router's log.)"""
    with socket.create_connection((host, port), timeout=secs or timeout()):
        return True


def seen(router, agent=None):
    """When TapTap last confirmed this router reachable (pulse, full live check or TapTap Link check-in)."""
    times = [router.last_watch_at]
    if getattr(router, 'connection_mode', 'api') == 'agent':
        try:
            times.append((agent or router.agent).last_seen_at)
        except Exception:
            pass
    else:
        ok = cache.get(f'tt:pulse:ok:{router.pk}')
        if ok:
            try:
                times.append(timezone.datetime.fromisoformat(ok))
            except ValueError:
                pass
    times = [t for t in times if t]
    return max(times) if times else None


def _routers(business=None):
    from .models import Router
    qs = Router.objects.select_related('business').exclude(connection_mode='agent')
    if business is not None:
        return [r for r in qs.filter(business=business)]
    return [r for r in qs.filter(business__live_sync=True) if r.business.has_access]


def _safe_to_knock(router, now):
    """Knock only where it can never hurt: TapTap's protection on the router must have TapTap's own address
    trusted (each knock is a new connection to the API port — protection counts those as password guessing),
    and a router the full live check confirmed a moment ago needs no extra connection."""
    from .self_trust import state
    if state(router.pk) not in ('none', 'trusted'):
        return False
    return not (router.status == 'Online' and router.last_watch_at and (now - router.last_watch_at).total_seconds() < 20)


def pulse(business=None):
    """Knock on every router (or one business's) in parallel and apply the results. Returns {router_id: ok}."""
    now = timezone.now()
    routers = [(r, target(r)) for r in _routers(business) if _safe_to_knock(r, now)]
    routers = [(r, t) for r, t in routers if t]
    if not routers:
        return {}
    results = {}
    pool = ThreadPoolExecutor(max_workers=min(32, len(routers)), thread_name_prefix='tt-pulse')
    futs = {pool.submit(knock, t[0], t[1]): r for r, t in routers}
    done, _late = wait(futs, timeout=timeout() + 2)
    pool.shutdown(wait=False)
    for f, r in futs.items():
        exc = None
        if f not in done:
            exc = TimeoutError(f'no answer from {r.name} within {timeout():g} s')
        else:
            try:
                f.result()
            except Exception as e:                       # noqa: BLE001 — refused, unreachable, DNS…
                exc = e
        results[r.pk] = exc is None
        try:
            _apply(r, exc)
        except Exception:
            logger.exception('pulse result for %s', r)
    return results


def _apply(router, exc):
    from . import live
    now = timezone.now()
    if exc is not None:
        live.note_failure(router, exc, now)              # counts toward offline (several in a row over 40 s+)
        return
    cache.set(f'tt:pulse:ok:{router.pk}', now.isoformat(), 3600)
    if router.status == 'Offline':
        # It answers again: a full check right away (it confirms the API and turns the router back online),
        # instead of waiting for the once-a-minute retry of offline routers.
        cache.delete(f'tt:watch:retry:{router.pk}')
        if cache.add(f'tt:pulse:recheck:{router.pk}', 1, every()):
            from .tasks import watch_router_task
            try:
                watch_router_task.delay(router.pk)
            except Exception:
                logger.exception('could not queue a recheck of %s', router)
