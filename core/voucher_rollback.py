"""Rolling a voucher back to its full time.

While less than half of a voucher's time is used, an Owner or Admin can roll it back: the clock
restarts now with the voucher's full time. Once half or more is used, rollback is no longer possible.

* "Full time" is the voucher's whole current period: first login (or the last rollback) → its end.
  Time added with "Add time" is part of that period, so it is given back too.
* Exactly half used already counts as "half or more": the rule is *less than* 50%.
* The period start moves to the moment of the rollback (``rolled_back_at``), so the 50% rule and the
  time bar always measure the current period. ``used_at`` (first login) is never changed, so reports
  and sales keep their real dates.
* On the router the uptime limit is raised by the full period on top of what it already used, the same
  way "Add time" does it, so the router's own ``limit-uptime`` cannot cut the customer off early.
* Rollback is never a sale and never touches finance. Every rollback is in the voucher history.
"""
from __future__ import annotations

import math
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import Voucher

LIMIT_PERCENT = 50


class RollbackError(ValueError):
    pass


def period_start(voucher):
    """When the voucher's current period began: the last rollback, else the first login."""
    return voucher.rolled_back_at or voucher.used_at


def check(voucher, now=None):
    """Can this voucher be rolled back right now?

    Returns a dict: ``allowed`` (bool), ``why`` (reason when not allowed), and when the clock is running
    ``start``, ``end``, ``period`` and ``elapsed`` (timedeltas), ``pct`` (0–100, whole number) and
    ``deadline`` (the moment half of the period is used — rollback closes then)."""
    from .voucher_history import ends_at
    now = now or timezone.now()
    out = {'allowed': False, 'why': '', 'start': None, 'end': None, 'period': None, 'elapsed': None,
           'pct': None, 'deadline': None}
    if voucher.deleted_at:
        out['why'] = 'it is in the bin'; return out
    if voucher.is_member:
        out['why'] = 'members are renewed, not rolled back'; return out
    if voucher.frozen_at:
        out['why'] = 'it is frozen — unfreeze it first'; return out
    if voucher.status == 'archived':
        out['why'] = 'it is archived'; return out
    if voucher.is_unlimited:
        out['why'] = 'it has no time limit'; return out
    start, end = period_start(voucher), ends_at(voucher)
    if not start or not end:
        out['why'] = 'it has not been used yet, so it still has its full time'; return out
    if end <= start:
        out['why'] = 'its time period is not valid'; return out
    period, elapsed = end - start, max(timedelta(0), now - start)
    out.update(start=start, end=end, period=period, elapsed=elapsed, deadline=start + period * LIMIT_PERCENT / 100,
               pct=max(0, min(100, math.floor(elapsed.total_seconds() * 100 / period.total_seconds()))))
    if end <= now or voucher.status == 'expired':
        out['why'] = 'its time has run out'; return out
    if voucher.status != 'active':
        out['why'] = 'it is disabled — enable it first'; return out
    # Strictly less than half: compare in seconds without rounding, so exactly 50% is refused.
    if elapsed.total_seconds() * 100 >= period.total_seconds() * LIMIT_PERCENT:
        out['why'] = f'{LIMIT_PERCENT}% or more of its time is already used'; return out
    out['allowed'] = True
    return out


def rollback(voucher, user=None, reason=''):
    """Restart the voucher's clock at its full time. Returns (ok, router_result). Raises RollbackError."""
    from .durations import text as mtext
    from .utils import log
    from .voucher_history import _router_apply, record
    reason = (reason or '').strip()[:255]
    if not reason:
        raise RollbackError('Give a reason — it is kept in the voucher history.')
    now = timezone.now()
    with transaction.atomic():
        # Lock the row and check again here: a page opened earlier must not roll back a voucher that
        # has since crossed the 50% line (or been frozen, disabled or rolled back by someone else).
        # of=('self',): PostgreSQL cannot lock the nullable side of the router join, so lock only the voucher row.
        v = Voucher.all_objects.select_for_update(of=('self',)).select_related('router', 'business').get(pk=voucher.pk)
        state = check(v, now)
        if not state['allowed']:
            raise RollbackError(f'{v.code} cannot be rolled back: {state["why"]}.')
        old_end, period, pct = state['end'], state['period'], state['pct']
        v.rolled_back_at = now
        v.expires_at = now + period
        v.save(update_fields=['rolled_back_at', 'expires_at'])
    # Router: the uptime limit becomes what it already used + the full period (like "Add time").
    minutes = max(1, math.ceil(period.total_seconds() / 60))
    via, result, ok = _router_apply(v, 'enable', user=user, minutes=minutes, total=False)
    record(v, 'rolled_back', user=user, reason=reason, via=via, router_result=result,
           status_before=v.status, status_after=v.status,
           text=f'Was {pct}% used · full {mtext(minutes)} again · now ends {timezone.localtime(v.expires_at):%d %b %Y %H:%M}',
           used_percent=pct, period_minutes=minutes,
           ends_before=old_end.isoformat(), ends_after=v.expires_at.isoformat())
    log(v.business, 'Voucher Rolled Back', f'{v.code} — {pct}% used, back to {mtext(minutes)}' + (f' — {reason}' if reason else ''))
    voucher.rolled_back_at, voucher.expires_at = v.rolled_back_at, v.expires_at
    return ok, result
