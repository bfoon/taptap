"""HotSpot session recovery for sticky vouchers and expired voucher re-login.

Why this exists
===============
TapTap intentionally enables RouterOS MAC-cookie/browser-cookie login when sticky
sessions are on. That gives customers the desired behaviour while a voucher is
valid: they can leave Wi-Fi and come back without typing the code again.

There are two edge cases that need explicit cleanup:

1. The current TapTap Link command catalogue implements ``hotspot_sticky``,
   ``hotspot_user_mac`` and ``hotspot_kick`` but the current safe-command allowlist
   does not include them. The queue therefore rejects those actions even though
   ``command_body`` knows how to execute them.

2. Removing only ``/ip hotspot active`` does NOT remove the remembered HotSpot
   cookie/MAC-cookie. Some phones then reconnect with that stale remembered login.
   After a voucher expires, is disabled, is manually disconnected, or its devices
   are reset, that can prevent the captive portal from opening cleanly for the next
   voucher.

This module fixes both without changing the normal sticky experience for a valid
voucher. Cookies are removed only on an explicit disconnect/disable/expiry/reset.

No database migration is required.
"""
from __future__ import annotations

import logging
from functools import wraps

logger = logging.getLogger('taptap.hotspot_recovery')
_INSTALLED = False


# Commands already implemented by core.agent.command_body but missing from the
# current SAFE_KINDS set in the repository.
STICKY_SAFE_KINDS = {
    'hotspot_sticky',
    'hotspot_user_mac',
    'hotspot_kick',
}


def _link_command_body(original):
    """Add cookie cleanup to Link commands that end a HotSpot login.

    We deliberately keep the existing command implementation for every other
    command. This also means future unrelated command changes continue to come
    from core.agent.
    """
    @wraps(original)
    def wrapped(cmd):
        p = cmd.params or {}
        kind = cmd.kind

        if kind == 'disconnect':
            # A deliberate checkout must forget both the live session and the
            # remembered login. Otherwise MAC-cookie can immediately put the
            # same stale voucher back on the phone and hide the portal.
            from .agent import rs
            user = rs(p['user'])
            return (
                f':do {{ /ip hotspot active remove [find user={user}] }} on-error={{}}; '
                f':do {{ /ip hotspot cookie remove [find user={user}] }} on-error={{}}'
            )

        if kind == 'hotspot_user_set' and p.get('disabled'):
            # Disable/expiry is terminal for the current login. Clear remembered
            # auth at the same moment so reconnecting becomes an unauthenticated
            # HotSpot client and RouterOS can present the login page again.
            from .agent import rs
            name = rs(p['name'])
            return (
                f'/ip hotspot user set [find name={name}] disabled=yes; '
                f':do {{ /ip hotspot active remove [find user={name}] }} on-error={{}}; '
                f':do {{ /ip hotspot cookie remove [find user={name}] }} on-error={{}}'
            )

        if kind == 'hotspot_users_disable' and p.get('disabled'):
            from .agent import rs
            names = ';'.join(rs(n) for n in p.get('names', []))
            return (
                f':foreach n in={{{names}}} do={{ '
                f'/ip hotspot user set [find name=$n] disabled=yes; '
                f':do {{ /ip hotspot active remove [find user=$n] }} on-error={{}}; '
                f':do {{ /ip hotspot cookie remove [find user=$n] }} on-error={{}} }}'
            )

        return original(cmd)

    return wrapped


def _reset_active_by_name(original):
    """Direct API/Tunnel: disconnect a username and forget its HotSpot cookies."""
    @wraps(original)
    def wrapped(self, code):
        # Run the repository's current active-session cleanup first.
        result = original(self, code)
        try:
            cookies = self.resource('/ip/hotspot/cookie')
            for row in cookies.get(user=code):
                try:
                    cookies.remove(id=row['id'])
                except Exception:
                    logger.debug('Could not remove HotSpot cookie %s for %s', row.get('id'), code, exc_info=True)
        except Exception:
            # Older/unusual RouterOS builds should never break a disconnect just
            # because the cookie table is unavailable.
            logger.debug('Could not clear HotSpot cookies for %s', code, exc_info=True)
        return result
    return wrapped


def _disconnect_by_id(original):
    """Direct API/Tunnel manual checkout: clear the matching remembered login too."""
    @wraps(original)
    def wrapped(self, item_id):
        user = ''
        mac = ''
        try:
            active = self.resource('/ip/hotspot/active')
            rows = active.get(id=item_id)
            if rows:
                user = str(rows[0].get('user', '') or '')
                mac = str(rows[0].get('mac-address', rows[0].get('mac_address', '')) or '').upper()
        except Exception:
            rows = []

        result = original(self, item_id)

        # Remove the remembered login for this exact session. Prefer username;
        # if it is unavailable fall back to MAC address.
        try:
            cookies = self.resource('/ip/hotspot/cookie')
            if user:
                remembered = cookies.get(user=user)
            elif mac:
                remembered = cookies.get(mac_address=mac)
            else:
                remembered = []
            for row in remembered:
                try:
                    cookies.remove(id=row['id'])
                except Exception:
                    logger.debug('Could not remove checkout cookie', exc_info=True)
        except Exception:
            logger.debug('Could not clear checkout HotSpot cookie', exc_info=True)
        return result
    return wrapped


def install():
    """Install the recovery behaviour once during Django startup."""
    global _INSTALLED
    if _INSTALLED:
        return

    from . import agent
    from .mikrotik import MikroTikService

    # Fix the concrete allowlist bug first. These command bodies and validators
    # are already present in the latest repository; only their allowlist entries
    # are missing.
    agent.SAFE_KINDS.update(STICKY_SAFE_KINDS)

    if not getattr(agent.command_body, '_taptap_hotspot_recovery', False):
        fn = _link_command_body(agent.command_body)
        fn._taptap_hotspot_recovery = True
        agent.command_body = fn

    if not getattr(MikroTikService.reset_active_by_name, '_taptap_hotspot_recovery', False):
        fn = _reset_active_by_name(MikroTikService.reset_active_by_name)
        fn._taptap_hotspot_recovery = True
        MikroTikService.reset_active_by_name = fn

    if not getattr(MikroTikService.disconnect, '_taptap_hotspot_recovery', False):
        fn = _disconnect_by_id(MikroTikService.disconnect)
        fn._taptap_hotspot_recovery = True
        MikroTikService.disconnect = fn

    _INSTALLED = True
