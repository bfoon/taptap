"""TapTap must never be blocked by the protection it installed on a router.

What people saw
---------------
"It still says router offline", while the router was fine. TapTap's router protection (Security › Protection)
counts new connections to the router's management ports: the fourth one within a few minutes puts the source on the
block list for an hour. TapTap itself is exempt only through the ``taptap-trusted`` address list — filled with the
address SITE_URL points at. That is where routers *reach* TapTap; it is not always where TapTap's own connections
*come from* (Cloudflare or another proxy in front, NAT, a second network card). Then TapTap's live checks counted as
password guessing, TapTap was blocked for an hour, and the router looked offline. The 10-second connection check
(core/router_pulse.py) made it happen within seconds.

What this does
--------------
* After every successful API connection (at most every 10 minutes per router) TapTap asks the router which address
  its own session comes from (``/user/active``, via=api, TapTap's user) and makes sure exactly that address is on
  ``taptap-trusted`` — and on none of the protection's stage or block lists.
* Those learned addresses are also added whenever protection is switched on (``protection.trusted_addresses``).
* ``state(router)`` tells the 10-second connection check whether knocking is safe: "none" (no TapTap protection on
  this router) or "trusted" (TapTap's address confirmed trusted). Until then it does not knock, so it can never be
  what gets TapTap blocked.
"""
from __future__ import annotations

import logging

from django.core.cache import cache

logger = logging.getLogger('taptap.selftrust')

EVERY = 600
STAGE_LISTS = ('taptap-bf-1', 'taptap-bf-2', 'taptap-bf-3', 'taptap-ips-blocked', 'taptap-ddos-blocked')
COMMENT = 'TapTap: never inspected or blocked (its own address, as this router sees it)'


def state(router_id):
    """'none' | 'trusted' | 'unknown' | None (not checked yet)."""
    return cache.get(f'tt:selftrust:state:{router_id}')


def learned():
    return list(cache.get('tt:egress:addrs') or [])


def _remember(addr):
    addrs = [a for a in learned() if a != addr] + [addr]
    cache.set('tt:egress:addrs', addrs[-5:], 30 * 86400)


def ensure(svc, router, force=False):
    """Make sure the router's TapTap protection never blocks TapTap. Returns the state."""
    if not force and cache.get(f'tt:selftrust:{router.pk}'):
        return state(router.pk)
    st = 'unknown'
    try:
        filt = svc.resource('/ip/firewall/filter')
        protected = any(filt.get(chain=c) for c in ('taptap-ips', 'taptap-ddos'))
        addr = ''
        try:
            mine = [u for u in svc.resource('/user/active').get()
                    if str(u.get('via', '')).lower() == 'api' and str(u.get('name', '')) == str(router.username)]
            if mine:
                addr = str(mine[-1].get('address') or '').strip()       # the newest API session: this one
        except Exception:
            addr = ''
        if addr:
            _remember(addr)
        if not protected:
            st = 'none'
        elif addr:
            alist = svc.resource('/ip/firewall/address-list')
            rows = alist.get()
            if not any(r.get('list') == 'taptap-trusted' and str(r.get('address')) == addr for r in rows):
                alist.add(list='taptap-trusted', address=addr, comment=COMMENT)
                logger.warning('%s: TapTap’s own address %s was not trusted by its protection — added', router, addr)
            for r in rows:
                if r.get('list') in STAGE_LISTS and str(r.get('address')) == addr:
                    alist.remove(id=r['id'])
            st = 'trusted'
    except Exception as exc:                                   # noqa: BLE001 — read-only users, old RouterOS…
        logger.info('self-trust %s: %s', router, exc)
    cache.set(f'tt:selftrust:state:{router.pk}', st, 3600)
    cache.set(f'tt:selftrust:{router.pk}', 1, EVERY)
    return st
