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
   After a voucher expires, is disabled, or its devices are reset, that can
   prevent the captive portal from opening cleanly for the next voucher. An ordinary
   disconnect of a VALID voucher instead keeps the cookie to allow seamless return.

This module fixes both without changing the normal sticky experience for a valid
voucher. Cookies are removed for disable/expiry/reset, not ordinary checkout of valid vouchers.

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
            # Ordinary checkout of a valid voucher is NOT a logout from sticky
            # authorization. Retain MAC-cookie so this phone can rejoin without
            # typing its still-valid voucher. Expiry/disable is handled by the
            # separate hotspot_user_set disabled command below.
            from .agent import rs
            user = rs(p['user'])
            return f':do {{ /ip hotspot active remove [find user={user}] }} on-error={{}}'

        if kind == 'hotspot_user_set' and p.get('disabled'):
            # Disable/expiry is terminal for the current login. Clear remembered
            # auth at the same moment so reconnecting becomes an unauthenticated
            # HotSpot client and RouterOS can present the login page again — and
            # free the device (host + Wi-Fi) so an iPhone, which only looks for a
            # login page when it joins the Wi-Fi, reconnects and shows it.
            from .agent import DROP_DEVICE, free_login, rs
            name = rs(p['name'])
            return DROP_DEVICE + f'/ip hotspot user set [find name={name}] disabled=yes; ' + free_login(name)

        if kind == 'hotspot_users_disable' and p.get('disabled') and p.get('reason') == 'expired':
            return original(cmd)       # strict expiry: core.agent frees the devices too (cookies included)

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


def _forget_cookies(service, username='', mac=''):
    try:
        cookies = service.resource('/ip/hotspot/cookie')
        rows = cookies.get(user=username) if username else cookies.get(mac_address=mac)
        for row in rows:
            if row.get('id'):
                cookies.remove(id=row['id'])
    except Exception:
        logger.debug('Could not remove HotSpot cookies', exc_info=True)


def _is_valid(service, username):
    """Keep cookies on an ordinary checkout only for still-valid vouchers."""
    try:
        from .models import Voucher
        from .voucher_history import time_is_up
        voucher = Voucher.objects.filter(router=service.router, code__iexact=username).first()
        return bool(voucher and voucher.status == 'active' and not voucher.frozen_at and not time_is_up(voucher))
    except Exception:
        logger.exception('Unable to determine voucher validity during checkout')
        return False


def _reset_active_by_name(original):
    """Direct API/Tunnel: session reset retains cookies only for valid vouchers.

    Expiry and disable explicitly forget cookies; an ordinary session reset should
    not force the same valid phone to re-enter a voucher.
    """
    @wraps(original)
    def wrapped(self, code):
        valid = _is_valid(self, code)
        result = original(self, code)
        if not valid:
            _forget_cookies(self, username=code)
        return result
    return wrapped


def _disable_voucher(original):
    @wraps(original)
    def wrapped(self, code):
        result = original(self, code)
        _forget_cookies(self, username=code)
        return result
    return wrapped


def _disconnect_by_id(original):
    """An intentional checkout retains valid MAC-cookie for sticky reconnection."""
    @wraps(original)
    def wrapped(self, item_id):
        username, mac = '', ''
        try:
            row = self.resource('/ip/hotspot/active').get(id=item_id)
            if row:
                username = str(row[0].get('user') or '')
                mac = str(row[0].get('mac-address') or '')
        except Exception:
            logger.debug('Could not inspect session before checkout', exc_info=True)
        valid = _is_valid(self, username) if username else False
        result = original(self, item_id)
        if not valid and (username or mac):
            _forget_cookies(self, username=username, mac=mac)
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

    if not getattr(MikroTikService.disable_voucher, '_taptap_hotspot_recovery', False):
        fn = _disable_voucher(MikroTikService.disable_voucher)
        fn._taptap_hotspot_recovery = True
        MikroTikService.disable_voucher = fn

    _INSTALLED = True
