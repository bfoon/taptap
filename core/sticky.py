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
OFF_KEEPALIVE = '20m'   # sticky sessions off: still long enough for a sleeping phone


def profile_values(business):
    """Values for a hotspot user profile (RouterOS names)."""
    if not getattr(business, 'sticky_sessions', True):
        # Not the RouterOS default of 2m: an iPhone (X, XR and others) asleep in a pocket stops answering the
        # router for several minutes at a time, and 2m logged it out every time the screen went dark.
        return {'add-mac-cookie': 'no', 'idle-timeout': 'none', 'keepalive-timeout': OFF_KEEPALIVE}
    return {'add-mac-cookie': 'yes', 'mac-cookie-timeout': COOKIE_TIMEOUT, 'idle-timeout': 'none',
            'keepalive-timeout': business.sticky_keepalive or '2h'}


MEMBER_PROFILE_PREFIXES = ('taptap-member-plan-', 'taptap-unlimited-member-plan-')


def is_member_profile(name):
    return str(name or '').lower().startswith(MEMBER_PROFILE_PREFIXES)


def member_profile_values(business):
    """Values for a Member Plan profile — always "stay signed in", whatever the voucher setting.

    Members type a username and password, so every forced sign-in costs them. A phone with its screen
    off stops answering the router for minutes at a time; with RouterOS defaults (keepalive 2m) it was
    signed out and had to type the password again. So member profiles always get:
      idle-timeout=none, keepalive-timeout=the business choice (at least the sticky default),
      a MAC cookie (a signed-out phone comes back by itself) and no session-timeout
      (the member's time is limit-uptime + TapTap's expiry, never the length of one session).
    """
    keep = getattr(business, 'sticky_keepalive', '') or '2h'
    if not getattr(business, 'sticky_sessions', True):
        keep = '2h'
    return {'add-mac-cookie': 'yes', 'mac-cookie-timeout': COOKIE_TIMEOUT, 'idle-timeout': 'none',
            'keepalive-timeout': keep, 'session-timeout': '0s'}


def server_login_script():
    """RouterOS: every hotspot server also accepts MAC-cookie and browser-cookie sign-in (keeps its others)."""
    return (':foreach p in=[/ip hotspot profile find] do={ :local have ""; :local out ""; '
            ':foreach x in=[/ip hotspot profile get $p login-by] do={ :set out ($out . $x . ","); :set have ($have . "|" . $x . "|") }; '
            ':if ([:typeof [:find $have "|mac-cookie|"]] = "nil") do={ :set out ($out . "mac-cookie,") }; '
            ':if ([:typeof [:find $have "|cookie|"]] = "nil") do={ :set out ($out . "cookie,") }; '
            '/ip hotspot profile set $p login-by=[:pick $out 0 ([:len $out] - 1)] }')


def member_profile_lines(names, business):
    """RouterOS lines making member profiles keep idle members signed in (TapTap Link)."""
    from .agent import rs
    vals = ' '.join(f'{k}={v}' for k, v in member_profile_values(business).items())
    names = [n for n in dict.fromkeys(names) if is_member_profile(n)]
    if not names:
        return []
    lines = [f':do {{ /ip hotspot user profile set [find name={rs(n)}] {vals} }} on-error={{}}' for n in names]
    lines.append(f':do {{ {server_login_script()} }} on-error={{}}')
    return lines


def apply_member_api(svc, business, names=None):
    """Direct API / Tunnel: same as member_profile_lines. Returns the number of member profiles updated."""
    vals = {k.replace('-', '_'): v for k, v in member_profile_values(business).items()}
    profiles = svc.resource('/ip/hotspot/user/profile')
    n = 0
    for row in profiles.get():
        if row.get('id') and is_member_profile(row.get('name')) and (names is None or row.get('name') in names):
            profiles.set(id=row['id'], **vals)
            n += 1
    if n:
        servers = svc.resource('/ip/hotspot/profile')
        for row in servers.get():
            have = [x for x in str(row.get('login-by', '')).split(',') if x]
            want = have + [x for x in ('mac-cookie', 'cookie') if x not in have]
            if row.get('id') and want != have:
                servers.set(id=row['id'], login_by=','.join(want))
    return n


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
