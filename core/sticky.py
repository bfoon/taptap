"""Sticky sessions on the router.

With sticky sessions on (Settings → Voucher devices), every hotspot user profile gets
  add-mac-cookie=yes, mac-cookie-timeout=30d  → a device that comes back is logged in by itself,
                                                no portal, straight away
  idle-timeout=none                            → a quiet device is never logged out for being idle
  keepalive-timeout=<your choice>              → how long an unreachable device stays in the list
and every hotspot server profile logs in by MAC cookie and browser cookie too (keeping
its other login methods). Turning it off puts the router defaults back.
"""
from __future__ import annotations

import logging

logger = logging.getLogger('taptap.sticky')

COOKIE_TIMEOUT = '30d'


def profile_values(business):
    """Values for a hotspot user profile (RouterOS names)."""
    if not getattr(business, 'sticky_sessions', True):
        return {'add-mac-cookie': 'no', 'idle-timeout': 'none', 'keepalive-timeout': '2m'}
    return {'add-mac-cookie': 'yes', 'mac-cookie-timeout': COOKIE_TIMEOUT, 'idle-timeout': 'none',
            'keepalive-timeout': business.sticky_keepalive or '2h'}


def api_values(business):
    return {k.replace('-', '_'): v for k, v in profile_values(business).items()}


def script(business):
    """RouterOS script used by TapTap Link."""
    vals = ' '.join(f'{k}={v}' for k, v in profile_values(business).items())
    lines = [f'/ip hotspot user profile set [find] {vals}']
    if getattr(business, 'sticky_sessions', True):
        lines.append(':foreach p in=[/ip hotspot profile find] do={ :local have ""; :local out ""; '
                     ':foreach x in=[/ip hotspot profile get $p login-by] do={ :set out ($out . $x . ","); :set have ($have . "|" . $x . "|") }; '
                     ':if ([:typeof [:find $have "|mac-cookie|"]] = "nil") do={ :set out ($out . "mac-cookie,") }; '
                     ':if ([:typeof [:find $have "|cookie|"]] = "nil") do={ :set out ($out . "cookie,") }; '
                     '/ip hotspot profile set $p login-by=[:pick $out 0 ([:len $out] - 1)] http-cookie-lifetime=' + COOKIE_TIMEOUT + ' }')
    return '; '.join(lines)


def apply_api(svc, business):
    profiles = svc.resource('/ip/hotspot/user/profile')
    vals = api_values(business)
    n = 0
    for row in profiles.get():
        if row.get('id'):
            profiles.set(id=row['id'], **vals); n += 1
    if getattr(business, 'sticky_sessions', True):
        servers = svc.resource('/ip/hotspot/profile')
        for row in servers.get():
            have = [x for x in str(row.get('login-by', '')).split(',') if x]
            want = have + [x for x in ('mac-cookie', 'cookie') if x not in have]
            if row.get('id'):
                servers.set(id=row['id'], login_by=','.join(want), http_cookie_lifetime=COOKIE_TIMEOUT)
    return n


def apply(router, user=None):
    """Push the business's sticky settings to one router. Returns (ok, message)."""
    from .voucher_history import channel
    business = router.business
    if channel(router) == 'TapTap Link':
        from .linkops import send
        try:
            send(router, 'hotspot_sticky', {'on': bool(business.sticky_sessions), 'keepalive': business.sticky_keepalive or '2h'},
                 label='Sticky sessions ' + ('on' if business.sticky_sessions else 'off'), user=user, minutes=60 * 24)
        except ValueError as exc:
            return False, f'{router.name}: not sent ({exc})'
        return True, f'{router.name}: queued'
    from .mikrotik import MikroTikService
    try:
        svc = MikroTikService(router).connect()
        try:
            n = apply_api(svc, business)
        finally:
            svc.close()
    except Exception as exc:
        return False, f'{router.name}: {exc}'
    return True, f'{router.name}: {n} profile(s) updated'


def apply_all(business, user=None):
    return [apply(r, user) for r in business.routers.all()]
