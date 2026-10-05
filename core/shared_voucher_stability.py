"""Keep multi-device/shared vouchers stable.

The bug
-------
TapTap gives an N-device voucher N persistent device bindings.  The original
fallback in core.device_lock._same_phone() is useful for a *single-device*
voucher: after a phone changes to a new private/random MAC, TapTap may move the
one offline binding to that new MAC.

That inference is unsafe on a shared voucher such as Family / 10 devices.
Several legitimate family phones are normally offline at any moment, so there
is no safe way to decide which offline person's slot an unknown MAC belongs to.
Moving one by guess can make the original device an "extra" device.  RouterOS
may then auto-login it from its HotSpot cookie, TapTap may disconnect it, and
the customer can see a repeated disconnect/reconnect cycle.

Safe policy
-----------
For vouchers with more than one device slot:
* Never reassign a full slot from hostname/private-MAC heuristics.
* Existing current MACs and known previous MACs still work.
* A retained portal device token still moves the SAME binding to a new MAC;
  core.device_lock.claim() handles that before _same_phone() is called.
* A genuinely new device can take a normal free slot while capacity remains.
* Once capacity is full, another unknown device is denied rather than replacing
  an offline family member.
* Staff can deliberately use "Reset devices" when a family device has really
  been replaced.

For a one-device voucher the original TapTap matcher is kept unchanged, so its
existing private-MAC convenience remains.

No router profile, shared-users value, finance logic, or database schema is
changed by this module.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("taptap.shared_voucher_stability")

_ORIGINAL = None


def safe_same_phone(voucher, rows, mac, hints):
    """Safe replacement for core.device_lock._same_phone()."""
    from . import device_lock

    # On a shared voucher an unknown MAC is not enough evidence to identify
    # which offline person's permanent slot should move.  Positive identity
    # (device token/current MAC/previous MAC) is already handled earlier by
    # device_lock.claim(), before this fallback is reached.
    if device_lock.slots(voucher) > 1:
        return None, ""

    # Preserve existing one-device behaviour exactly.
    if _ORIGINAL is None:
        return None, ""
    return _ORIGINAL(voucher, rows, mac, hints)


def install():
    """Install once during Django CoreConfig.ready()."""
    global _ORIGINAL

    from . import device_lock

    if getattr(device_lock._same_phone, "_taptap_shared_stability", False):
        return

    _ORIGINAL = device_lock._same_phone
    safe_same_phone._taptap_shared_stability = True
    device_lock._same_phone = safe_same_phone

    logger.info(
        "Shared voucher stability enabled: heuristic MAC slot moves are "
        "disabled for multi-device vouchers."
    )
