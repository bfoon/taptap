"""Member shield — TapTap never signs out a member whose time is not finished.

The rule (owner's decision): members pay for a period; while that period runs and nobody switched the member off,
nothing TapTap does on its own may disconnect them. Automatic checks still run (device slots, sharing, old MACs,
session enforcement…), but when one of them is about to send a "disconnect" or "kick" for a member who still has
time, the command is stopped here and written down instead:

    * the command is kept as *Cancelled* with the reason, so the TapTap Link page shows what was held back
    * a line goes to the Activity log ("Member shield: kept X online — …")
    * recent stops are counted per member (shield_stats) for the Members page

What still disconnects a member (on purpose):
    * a person doing it (any command queued with a user: the Disconnect button, a password change, Fit with
      confirmation)
    * the member's time is over, the member is disabled / frozen / deleted in TapTap
    * a command that asks for it explicitly (params ``force=True``)

Covers TapTap Link commands (``core/agent.queue``). Direct API routers take a different road (``core/live.py``
enforcement only signs out expired / disabled vouchers, which the shield allows anyway).
"""
from __future__ import annotations

import logging
from functools import wraps

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('taptap.member_shield')

GUARDED = ('disconnect', 'hotspot_kick')
STATS_TTL = 7 * 86400


def protected_member(business, code, now=None):
    """The member behind this code when TapTap must keep them online, else None."""
    from .models import Voucher
    code = str(code or '').strip()
    if not business or not code:
        return None
    now = now or timezone.now()
    v = (Voucher.all_objects if hasattr(Voucher, 'all_objects') else Voucher.objects).filter(
        business=business, code__iexact=code, login_type='member').first()
    if v is None or getattr(v, 'deleted_at', None) or getattr(v, 'frozen_at', None):
        return None
    if v.status != 'active':
        return None
    try:   # the same clock the device lock uses (core/device_lock.protected_member)
        from .voucher_history import time_is_up
        if time_is_up(v):
            return None
    except Exception:
        if v.expires_at and v.expires_at <= now:
            return None
    return v


def _note(business, v, kind, label):
    key = f'tt:shield:{business.pk}:{v.pk}'
    try:
        n = cache.get(key) or {}
        n = {'count': int(n.get('count') or 0) + 1, 'last': timezone.now().isoformat(), 'what': str(label)[:120]}
        cache.set(key, n, STATS_TTL)
    except Exception:
        pass
    try:
        from .utils import log
        # one Activity line per member per 10 minutes (an enforcement loop can retry every check-in)
        if cache.add(f'tt:shield:log:{business.pk}:{v.pk}', 1, 600):
            log(business, 'Member shield', f'Kept member {v.code} online — TapTap stopped an automatic '
                f'{"sign-out" if kind == "disconnect" else "device kick"} ({label}). Members with time left are never disconnected.')
    except Exception:
        pass


def shield_stats(business, voucher_ids):
    """{voucher_id: {'count', 'last', 'what'}} for members TapTap kept online in the last 7 days."""
    out = {}
    for pk in voucher_ids:
        try:
            d = cache.get(f'tt:shield:{business.pk}:{pk}')
        except Exception:
            d = None
        if d:
            out[pk] = d
    return out


def _guard(original):
    @wraps(original)
    def queue(router, kind, params=None, label='', user=None, minutes=None):
        p = params or {}
        if kind in GUARDED and user is None and not p.get('force'):
            try:
                v = protected_member(router.business, p.get('user'))
            except Exception:
                v = None
            if v is not None:
                from datetime import timedelta
                from .models import AgentCommand
                _note(router.business, v, kind, label or kind)
                logger.info('member shield: kept %s online on %s (%s)', v.code, router, label or kind)
                return AgentCommand.objects.create(
                    router=router, kind=kind, params=p, label=(f'Held back: {label or kind}')[:200], status='cancelled',
                    result='Member shield: this member still has time, so TapTap does not disconnect them automatically.',
                    created_by=None, expires_at=timezone.now() + timedelta(minutes=1), done_at=timezone.now())
        return original(router, kind, params, label=label, user=user, minutes=minutes)
    queue._taptap_member_shield = True
    return queue


def install():
    from . import agent
    if getattr(agent.queue, '_taptap_member_shield', False):
        return
    agent.queue = _guard(agent.queue)
