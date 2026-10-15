"""Members stay signed in while idle — fix member profiles already on the routers.

New and repaired member profiles get the "stay signed in" values from every command that touches
them (core/agent.command_body, MikroTikService.ensure_hotspot_profile). This module fixes the member
profiles that are already on a router with RouterOS defaults (keepalive 2m, a session-timeout…):
after each inventory TapTap compares its mirror of /ip/hotspot/user/profile with what members need
and sends one correction when they differ (at most every 30 minutes per router).
"""
from __future__ import annotations

import logging

from django.core.cache import cache

logger = logging.getLogger('taptap.member_keepalive')

RECHECK_SECONDS = 1800


def _truthy(value):
    return str(value or '').strip().lower() in ('true', 'yes')


def _secs(value):
    from .router_time import secs
    return secs(value)


def needs_fix(row, business):
    """Does this mirrored member profile sign idle members out (or cut their sessions)?"""
    from .sticky import member_profile_values
    want = member_profile_values(business)
    raw = row.raw_data or {}
    idle = str(row.idle_timeout or raw.get('idle-timeout') or '').strip().lower()
    if idle not in ('', 'none', '0s'):
        return True
    keep, want_keep = str(row.keepalive_timeout or raw.get('keepalive-timeout') or '').strip().lower(), want['keepalive-timeout']
    if want_keep == 'none':
        if keep not in ('none', ''):
            return True
    elif keep in ('', ) or (keep != 'none' and _secs(keep) < _secs(want_keep)):
        return True
    session = str(row.session_timeout or raw.get('session-timeout') or '').strip().lower()
    if session not in ('', '0s', 'none') and _secs(session) > 0:
        return True
    if 'add-mac-cookie' in raw and not _truthy(raw.get('add-mac-cookie')):
        return True
    return False


def ensure_member_profiles(router, force=False):
    """Queue (Link) or apply (API) one correction for member profiles that sign idle members out.
    Returns the number of member profiles corrected or queued."""
    from .models import RouterHotspotProfile
    from .sticky import is_member_profile
    if not force and not cache.add(f'tt:member-keepalive:{router.pk}', 1, RECHECK_SECONDS):
        return 0
    business = router.business
    rows = [r for r in RouterHotspotProfile.objects.filter(router=router, is_present=True) if is_member_profile(r.name)]
    names = [r.name for r in rows if force or needs_fix(r, business)]
    if not names:
        return 0
    from .voucher_history import channel
    try:
        if channel(router) == 'TapTap Link':
            from .linkops import send
            send(router, 'member_profiles', {'names': names}, label=f'Members stay signed in ({len(names)} plan profile(s))', minutes=24 * 60)
            return len(names)
        from .mikrotik import MikroTikService
        from .sticky import apply_member_api
        svc = MikroTikService(router).connect()
        try:
            return apply_member_api(svc, business, set(names))
        finally:
            svc.close()
    except Exception as exc:
        cache.delete(f'tt:member-keepalive:{router.pk}')       # try again at the next inventory
        logger.info('member profiles on %s: %s', router, exc)
        return 0
