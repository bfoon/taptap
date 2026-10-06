"""Compatibility guard for safe TapTap Link commands already implemented in agent.command_body.

The current agent.py contains command builders and validators for these kinds,
and existing TapTap features queue them, but they are absent from SAFE_KINDS.
That makes queue() reject them with "Not an allowed command." after a successful
Quick Install.

This module does not enable free-form scripts. It only exposes the already
implemented, parameter-validated command kinds below.
"""
import logging

logger = logging.getLogger("taptap.link_safe_command_compat")

REQUIRED_IMPLEMENTED_SAFE_KINDS = {
    "hotspot_user_mac",   # Permanent Pin / one-device router MAC pin
    "hotspot_kick",       # kick one foreign MAC from one voucher
    "hotspot_sticky",     # sticky-session profile setting
}


def install():
    from . import agent

    missing = REQUIRED_IMPLEMENTED_SAFE_KINDS - set(agent.SAFE_KINDS)
    if missing:
        agent.SAFE_KINDS.update(missing)
        logger.warning(
            "TapTap Link safe-command catalogue repaired for implemented kinds: %s",
            ", ".join(sorted(missing)),
        )

    # Voucher commands should get voucher-priority delivery.
    current = list(agent.VOUCHER_KINDS)
    for kind in ("hotspot_user_mac", "hotspot_kick"):
        if kind not in current:
            current.append(kind)
    agent.VOUCHER_KINDS = tuple(current)
