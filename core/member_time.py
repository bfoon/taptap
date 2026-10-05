"""A member's clock: what changing their plan does to their end date, and what the router must say.

The rule (owner's words): "if someone already used 4 days and their plan changes to 1 month, those 4 days
count in the 1 month plan." So when a member's plan changes — moved to another plan, or the plan's length
edited — a member whose time has started keeps their start:

    new end = start + new plan length + any extra periods they already paid for (renewals / added time)

* start = first use (``used_at``), or for time bought before first use, end − old length.
* Moving FROM an unlimited plan starts the clock now: months on a free plan must not expire a member the moment
  they are put on a monthly plan.
* To an unlimited plan: no end, no router limit.
* Not started yet: the new plan's full length applies from the first login.

The router gets the same time: its ``limit-uptime`` is the member's whole period (start → end). The router counts
connected time, so time already used is inside that limit too; TapTap's strict expiry still ends the member on the
calendar date (core/expiry.py).
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from .durations import UNLIMITED_ROUTEROS, parse_routeros, routeros


def _minutes(td):
    return int(td.total_seconds() + 59) // 60


def start_of(v):
    """When the member's current time started (None = not started)."""
    if v.used_at:
        return v.used_at
    if v.expires_at and v.duration_minutes:
        return v.expires_at - timedelta(minutes=v.duration_minutes)
    return None


def rebase(v, new_minutes, now=None):
    """Field updates when the member's plan length becomes ``new_minutes`` (0 = unlimited)."""
    now = now or timezone.now()
    new_minutes = int(new_minutes or 0)
    if not new_minutes:
        return {'duration_minutes': 0, 'expires_at': None}
    was_unlimited = not v.duration_minutes and not v.expires_at
    start = start_of(v)
    if start is None and not v.used_at:
        return {'duration_minutes': new_minutes, 'expires_at': None}          # whole plan from the first login
    if was_unlimited:
        return {'duration_minutes': new_minutes, 'expires_at': now + timedelta(minutes=new_minutes)}
    extra = 0
    if v.expires_at and v.duration_minutes:
        extra = max(0, _minutes(v.expires_at - start) - int(v.duration_minutes))   # renewals already paid for
    return {'duration_minutes': new_minutes, 'expires_at': start + timedelta(minutes=new_minutes + extra)}


def expected_limit(v):
    """The limit-uptime the router should have for this member ('0s' = no limit)."""
    if not v.duration_minutes and not v.expires_at:
        return UNLIMITED_ROUTEROS
    start = start_of(v)
    if v.expires_at and start:
        return routeros(max(1, _minutes(v.expires_at - start)))
    return routeros(int(v.duration_minutes))


def router_matches(v, router_limit_text):
    """Does the router's limit-uptime equal the member's plan time? (within a minute)"""
    have = parse_routeros(router_limit_text or '', 0) or 0
    want = parse_routeros(expected_limit(v), 0) or 0
    return abs(have - want) <= 1


def describe_change(v, updates, now=None):
    """One line for the history and the message: what happens to the member's time."""
    now = now or timezone.now()
    end = updates.get('expires_at')
    if not updates.get('duration_minutes'):
        return 'no time limit any more'
    if end is None:
        return 'the plan time starts at the first login'
    if end <= now:
        return f'time already used is more than the new plan — the member’s time ended ({timezone.localtime(end):%d %b %H:%M})'
    return f'ends {timezone.localtime(end):%d %b %Y %H:%M} (time already used counts in the new plan)'
