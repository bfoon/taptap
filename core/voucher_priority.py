"""Vouchers first — "sync this voucher now" means now, and TapTap keeps at it until the router has it.

What people saw
---------------
The TapTap Link page showed "Traffic sample (apps & sites) — Sent to the router" while "Disconnect AFE3R",
"Switch off 1 voucher whose time ran out" and "Remove 2 deleted vouchers" had been "Waiting for the router" for
minutes. And asking Fit to "put this voucher first / sync it now" only marked it Pending: when another voucher batch
was already waiting, it went behind it (sometimes behind hundreds of stock vouchers), and nobody checked that it
arrived.

What changed (core/agent.build_response / push_pending_vouchers)
---------------------------------------------------------------
* Voucher commands ride first in EVERY check-in. On the other work's turn they keep at most half the batch, so a
  sync still gets through during a long backlog; before, the other work went first and vouchers waited.
* Background samples (apps & sites, sharing lists) are not taken nor sent while a voucher change waits.
* Waiting vouchers go in the order a customer feels: asked-for-first, in use, sold, then the newest stock.
* While the router is still busy with the last voucher batch and more wait, it is asked to check in again in 2 s.

This module: priority vouchers
------------------------------
``prioritize(vouchers)``  sends them at once (TapTap Link: their own small command, leading the next batch;
                          Direct API / Tunnel: pushed right now), turbo check-ins, and WATCHES them:
``tick(router)``          every TapTap Link check-in: confirmed by the router → done (history + live feed); lost,
                          refused or expired on the way → sent again (every 20 s at most); still on its way → its
                          command stays at the front. Gives up after 15 minutes and says so.
``tick_api()``            the same for Direct API / Tunnel routers, from the voucher sweeper (every few seconds).
"""
from __future__ import annotations

import logging
import time

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('taptap.vprio')

WATCH_SECONDS = 15 * 60     # keep trying this long
RESEND_EVERY = 20           # seconds between re-sends of one voucher
MAX_VOUCHERS = 50           # "first" means a handful; a batch or a plan uses the normal sync
TTL = WATCH_SECONDS + 300


def _wkey(router_id):
    return f'tt:vprio:{router_id}'


def _ckey(router_id):
    return f'tt:vprio:cmds:{router_id}'


ROUTERS_KEY = 'tt:vprio:routers'


def watching(router_id):
    try:
        return cache.get(_wkey(router_id)) or {}
    except Exception:
        return {}


def _save(router_id, w):
    try:
        if w:
            cache.set(_wkey(router_id), w, TTL)
            rs = set(cache.get(ROUTERS_KEY) or [])
            if router_id not in rs:
                rs.add(router_id)
                cache.set(ROUTERS_KEY, sorted(rs), TTL)
        else:
            cache.delete_many([_wkey(router_id), _ckey(router_id)])
    except Exception:
        logger.debug('priority watch save', exc_info=True)


def command_ids(router):
    """Queued Link commands that carry a priority voucher: build_response puts them at the front."""
    try:
        return set(cache.get(_ckey(router.pk)) or [])
    except Exception:
        return set()


def _carriers(router, vouchers):
    """Live (queued/sent) Link commands about these vouchers → (queued pks, carried voucher pks)."""
    from .agent import VOUCHER_KINDS
    from .models import AgentCommand
    ids = {v.pk for v in vouchers}
    codes = {v.code.upper(): v.pk for v in vouchers}
    queued, carried = set(), set()
    for c in AgentCommand.objects.filter(router=router, status__in=['queued', 'sent'], kind__in=list(VOUCHER_KINDS)).only(
            'pk', 'kind', 'params', 'status'):
        p = c.params or {}
        hit = set(i for i in (p.get('ids') or []) if i in ids)
        names = [p.get('name'), p.get('user'), p.get('member_upsert')] + list(p.get('names') or []) + \
            [u.get('n') for u in (p.get('users') or []) if isinstance(u, dict)]
        hit |= {codes[str(n).upper()] for n in names if n is not None and str(n).upper() in codes}
        if isinstance(p.get('member_upsert'), int) and p['member_upsert'] in ids:
            hit.add(p['member_upsert'])
        if hit:
            carried |= hit
            if c.status == 'queued':
                queued.add(c.pk)
    return queued, carried


def _allowed(v):
    from .router_time import platform_owned
    if not v.router_id or v.deleted_at or v.status in ('expired', 'archived'):
        return 'no router' if not v.router_id else 'its time is over / it is deleted — nothing to send'
    if not platform_owned(v):
        return 'made on the router — TapTap reads it from the router instead'
    return ''


def _send(router, vouchers, user=None):
    """One push of these vouchers, at the front of the line. Returns a short text."""
    from .voucher_history import channel
    if channel(router) == 'TapTap Link':
        from . import agent as link
        from .voucher_fastlane import sync_now
        out = sync_now([v.pk for v in vouchers], user=user, force=True)     # its own small command (members: exact push)
        link.mark_urgent(router, 180)                                     # the router checks in every few seconds
        queued, _ = _carriers(router, vouchers)
        try:
            cache.set(_ckey(router.pk), sorted(queued), TTL)
        except Exception:
            pass
        return '; '.join(out.values()) or 'queued'
    from .voucher_ready import push_exact
    ok = 0
    for v in vouchers:
        try:
            ok += 1 if push_exact(v.pk) else 0
        except Exception as exc:                  # noqa: BLE001 — router API errors come in many types
            logger.info('priority push %s: %s', v.code, exc)
    return f'{ok} of {len(vouchers)} on {router.name} now' + ('' if ok == len(vouchers) else ' — TapTap keeps trying')


def prioritize(vouchers, user=None, reason=''):
    """Put these vouchers on their routers first, and keep trying until each router confirms. Returns report lines."""
    now = time.time()
    lines, by_router, skipped = [], {}, []
    for v in list(vouchers)[:MAX_VOUCHERS]:
        why = _allowed(v)
        if why:
            skipped.append(f'{v.code}: {why}')
            continue
        by_router.setdefault(v.router, []).append(v)
    for router, vs in by_router.items():
        w = watching(router.pk)
        for v in vs:
            w[str(v.pk)] = {'code': v.code, 'since': now, 'until': now + WATCH_SECONDS, 'last': now, 'tries': 1,
                            'by': getattr(user, 'pk', None), 'why': (reason or '')[:120]}
        _save(router.pk, w)
        text = _send(router, vs, user)
        codes = ', '.join(v.code for v in vs[:6]) + ('…' if len(vs) > 6 else '')
        lines.append(f'{codes} → {router.name}: sent first ({text}); TapTap keeps trying until the router confirms (up to 15 min)')
        for v in vs:
            _note(v, 'Sent to the router first (priority) — TapTap keeps trying until the router confirms', user)
        if router.pk and _via_api(router):
            tick_router(router)                    # Direct API: confirmed straight away when the push worked
    if len(vouchers) > MAX_VOUCHERS:
        lines.append(f'{len(vouchers) - MAX_VOUCHERS} more go with the normal sync (priority is for a few vouchers at a time)')
    lines += skipped
    return lines


def _via_api(router):
    from .voucher_history import channel
    return channel(router) != 'TapTap Link'


def _note(v, text, user=None, via=''):
    try:
        from .voucher_history import channel, record
        record(v, 'note', user=user, source='user' if user else 'auto', via=via or channel(v.router), reason='Priority sync', text=text[:250])
    except Exception:
        logger.debug('priority note for %s', v.code, exc_info=True)


def _event(router, text, level='info'):
    try:
        from .live import push_event
        push_event(router.business_id, text, level)
    except Exception:
        pass


def tick_router(router):
    """Check every priority voucher of this router once: done, re-send, or give up. Cheap when none."""
    from .models import Voucher
    from .voucher_ready import _confirmed_at
    w = watching(router.pk)
    if not w:
        return 0
    now = time.time()
    vs = {v.pk: v for v in Voucher.objects.filter(pk__in=[int(k) for k in w]).select_related('router', 'business')}
    on_link = not _via_api(router)
    queued, carried = _carriers(router, list(vs.values())) if on_link else (set(), set())
    resend = []
    for key, e in list(w.items()):
        v = vs.get(int(key))
        if v is None or v.router_id != router.pk or _allowed(v):
            w.pop(key, None)
            continue
        confirmed = (_confirmed_at(v.pk) or 0) >= e['since'] - 1
        if not confirmed and v.is_member:
            confirmed = v.mikrotik_sync_status == 'Synced' and v.pk not in carried and now - e['since'] > 5
        if confirmed:
            w.pop(key, None)
            secs = int(now - e['since'])
            _note(v, f'On the router — confirmed {secs} s after it was sent first')
            _event(router, f'{router.name}: {v.code} is on the router (sent first, confirmed in {secs} s)', 'fix')
            continue
        if now > e['until']:
            w.pop(key, None)
            _note(v, 'Priority sync: the router did not confirm it within 15 minutes — see the TapTap Link page')
            _event(router, f'{router.name}: {v.code} — no confirmation from the router after 15 minutes', 'bad')
            continue
        stuck = v.pk not in carried if on_link else True       # nothing on its way any more (lost / refused / expired)
        if stuck and now - e.get('last', 0) >= RESEND_EVERY:
            e['last'], e['tries'] = now, int(e.get('tries') or 1) + 1
            resend.append(v)
    if resend:
        try:
            _send(router, resend)
        except Exception:
            logger.exception('priority re-send on %s', router)
        if on_link:
            queued, _ = _carriers(router, list(vs.values()))
    if on_link:
        try:
            cache.set(_ckey(router.pk), sorted(queued), TTL)
            if w:
                from .agent import mark_urgent
                mark_urgent(router, 120)
        except Exception:
            pass
    _save(router.pk, w)
    return len(w)


def tick(router):
    """TapTap Link: called at every check-in, before the reply is built."""
    if not watching(router.pk):
        return 0
    return tick_router(router)


def tick_api():
    """Direct API / Tunnel routers: from the voucher sweeper every few seconds."""
    from .models import Router
    try:
        ids = list(cache.get(ROUTERS_KEY) or [])
    except Exception:
        return 0
    left, n = [], 0
    for r in Router.objects.filter(pk__in=ids).select_related('business'):
        if not watching(r.pk):
            continue
        left.append(r.pk)
        if _via_api(r) and cache.add(f'tt:vprio:api:{r.pk}', 1, 5):
            try:
                n += tick_router(r)
            except Exception:
                logger.exception('priority tick on %s', r)
    try:
        cache.set(ROUTERS_KEY, left, TTL) if left else cache.delete(ROUTERS_KEY)
    except Exception:
        pass
    return n


def status(business):
    """{router name: [codes still being pushed]} — for Fit and the routers page."""
    out = {}
    for r in business.routers.all():
        w = watching(r.pk)
        if w:
            out[r.name] = [e.get('code') for e in w.values()]
    return out
