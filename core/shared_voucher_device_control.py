"""Remove one locked device from a voucher without resetting the others.

This deliberately reuses the existing ``reset_mac`` URL and permission.  The
voucher-detail page already protects that action with ``vouchers.support`` and
TeamAccessMiddleware enforces the same permission server-side.

The device-list button submits a private marker through the existing ``reason``
field.  This module wraps ``voucher_history.reset_devices()`` (which the normal
reset view looks up dynamically) and turns only that marker into a surgical
one-binding removal.  Ordinary Reset devices continues to call TapTap's
original all-device reset unchanged.

Why not remove the whole HotSpot user/session?
----------------------------------------------
For a Family / 10-device voucher that would interrupt the other nine people.
Instead this code:
* deletes only the selected VoucherDeviceBinding;
* keeps its slot number empty (claim() already reuses the first free slot);
* removes only that binding's current/previous MAC active session + cookie;
* protects MACs that are still referenced by another allowed binding;
* preserves the router profile and shared-users value;
* records exactly which slot/device was removed in voucher history;
* temporarily refuses the just-removed MAC/device ID for five minutes so a
  stale RouterOS session cannot race the cleanup and instantly take its slot
  back. A normal full Reset devices clears that temporary guard.
"""
from __future__ import annotations

from functools import wraps
import hashlib
import logging

from django.core.cache import cache
from django.db import transaction

logger = logging.getLogger("taptap.shared_voucher_device_control")

MARKER = "__taptap_remove_device_binding__:"
REMOVED_COOLDOWN = 300
_ORIGINAL_RESET = None
_ORIGINAL_CLAIM = None


def removal_reason(binding_id):
    """Opaque POST value used by the device-list Remove button."""
    return f"{MARKER}{int(binding_id)}"


def _binding_id(reason):
    text = str(reason or "").strip()
    if not text.startswith(MARKER):
        return None
    raw = text[len(MARKER):].strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _unique_macs(device_lock, values):
    result = []
    seen = set()
    for value in values:
        mac = device_lock.norm_mac(value)
        if mac and mac not in seen:
            seen.add(mac)
            result.append(mac)
    return result


def _mac_key(voucher_id, mac):
    return f"lock:removed:mac:{voucher_id}:{mac}"


def _fp_key(voucher_id, fp):
    digest = hashlib.sha256(str(fp or "").encode("utf-8")).hexdigest()[:32]
    return f"lock:removed:fp:{voucher_id}:{digest}"


def _index_key(voucher_id):
    return f"lock:removed:index:{voucher_id}"


def _temporarily_block(voucher, macs, fp=""):
    keys = []
    for mac in macs:
        key = _mac_key(voucher.pk, mac)
        cache.set(key, 1, REMOVED_COOLDOWN)
        keys.append(key)
    if fp:
        key = _fp_key(voucher.pk, fp)
        cache.set(key, 1, REMOVED_COOLDOWN)
        keys.append(key)

    if keys:
        old = list(cache.get(_index_key(voucher.pk)) or [])
        merged = list(dict.fromkeys(old + keys))
        cache.set(_index_key(voucher.pk), merged, REMOVED_COOLDOWN)


def _clear_removed_blocks(voucher):
    keys = list(cache.get(_index_key(voucher.pk)) or [])
    if keys:
        cache.delete_many(keys)
    cache.delete(_index_key(voucher.pk))


def _is_temporarily_removed(voucher, mac="", fp=""):
    from . import device_lock

    normalized = device_lock.norm_mac(mac)
    if normalized and cache.get(_mac_key(voucher.pk, normalized)):
        return True
    token = str(fp or "").strip()[:128]
    return bool(token and cache.get(_fp_key(voucher.pk, token)))


def remove_one(voucher, binding_id, user=None):
    """Free exactly one device slot and forget only that device on RouterOS."""
    from . import device_lock
    from .models import Voucher, VoucherDeviceBinding
    from . import voucher_history as vh

    with transaction.atomic():
        # claim() also locks the voucher row, so live sync cannot allocate the
        # same slot in the middle of this database change.
        Voucher.objects.select_for_update().filter(pk=voucher.pk).first()
        binding = (
            VoucherDeviceBinding.objects
            .select_for_update()
            .filter(pk=binding_id, voucher=voucher, business=voucher.business)
            .first()
        )
        if binding is None:
            # The existing reset_mac view does not catch VoucherActionError.
            # Treat a stale/double-click safely as an idempotent no-op instead
            # of turning it into a 500 response.
            return True, "No device was removed because that slot is already free; all other devices were left unchanged"

        slot_no = int(binding.slot_no or 1)
        label = (binding.label or "").strip()
        fingerprint = (binding.device_token_hash or "").strip()[:128]
        candidates = _unique_macs(
            device_lock,
            (binding.current_mac, binding.previous_mac),
        )

        # Put the guard in cache before releasing the voucher-row lock.  If the
        # router still reports this stale session for another live pass, claim()
        # refuses it rather than recreating the binding we just removed.
        _temporarily_block(voucher, candidates, fingerprint)
        binding.delete()

        # Be extra defensive: if an old/previous MAC is still referenced by a
        # different legitimate slot, never disconnect that other device.
        remaining_values = []
        for current_mac, previous_mac in (
            VoucherDeviceBinding.objects
            .filter(voucher=voucher)
            .values_list("current_mac", "previous_mac")
        ):
            remaining_values.extend((current_mac, previous_mac))
        protected = set(_unique_macs(device_lock, remaining_values))
        macs = [mac for mac in candidates if mac not in protected]

    router_id = getattr(voucher, "router_id", None)
    for mac in macs:
        if router_id:
            cache.delete(f"lock:act:{router_id}:{mac}")
            cache.delete(f"lock:strike:{voucher.pk}:{mac}")
            cache.delete(f"lock:kick:{router_id}:{voucher.pk}:{mac}")
        # Deliberately per MAC: remove only this phone's active HotSpot
        # session/cookie on Direct/Tunnel or queue hotspot_kick on TapTap Link.
        device_lock._forget_mac(voucher, mac)

    # A one-device voucher may still carry a legacy RouterOS per-user MAC lock.
    # Clearing it is safe here because no other binding exists on that voucher.
    if device_lock.slots(voucher) == 1:
        device_lock.unlock_on_router(voucher)

    via = vh.channel(voucher.router)
    who = label or (macs[0] if macs else f"device {slot_no}")
    result = (
        f"Only device slot {slot_no} was freed"
        + (f" and {len(macs)} device MAC{'s' if len(macs) != 1 else ''} forgotten on/for the router" if macs else "")
        + "; all other locked devices were left unchanged"
    )

    vh.record(
        voucher,
        "device",
        user=user,
        source="user",
        reason="One locked device removed",
        via=via,
        router_result=result,
        status_before=voucher.status,
        status_after=voucher.status,
        kind="binding_removed",
        slot_no=slot_no,
        label=label or None,
        devices_removed=macs or None,
        text=f"Removed {who} from device slot {slot_no}; that slot is free again",
    )

    logger.info(
        "Removed binding %s (slot %s) from voucher %s; kept all other bindings",
        binding_id,
        slot_no,
        voucher.code,
    )
    return True, result


def _wrapped_claim(original):
    @wraps(original)
    def wrapped(voucher, mac="", fp="", source="portal", label="", hints=None):
        if _is_temporarily_removed(voucher, mac=mac, fp=fp):
            from . import device_lock
            return device_lock.Outcome(
                "denied",
                message=(
                    "This device was just removed from this voucher by staff. "
                    "Its old session is being cleared; try again in a few minutes "
                    "or ask staff if it should be added back."
                ),
            )
        return original(
            voucher,
            mac=mac,
            fp=fp,
            source=source,
            label=label,
            hints=hints,
        )

    wrapped._taptap_single_device_remove_guard = True
    return wrapped


def _wrapped_reset(original):
    @wraps(original)
    def wrapped(voucher, user=None, reason=""):
        binding_id = _binding_id(reason)
        if binding_id is None:
            # A deliberate full Reset devices means all devices are allowed to
            # learn slots again immediately, so clear any short anti-reclaim
            # guard left by a previous one-device removal.
            _clear_removed_blocks(voucher)
            return original(voucher, user=user, reason=reason)
        if not binding_id:
            return True, "No device was removed because the device selection was invalid; refresh the voucher and try again"
        return remove_one(voucher, binding_id, user=user)

    wrapped._taptap_single_device_remove = True
    return wrapped


def install():
    """Install the per-device reset handler and short anti-reclaim guard once."""
    global _ORIGINAL_RESET, _ORIGINAL_CLAIM

    from . import voucher_history as vh
    from . import device_lock

    if not getattr(vh.reset_devices, "_taptap_single_device_remove", False):
        _ORIGINAL_RESET = vh.reset_devices
        vh.reset_devices = _wrapped_reset(_ORIGINAL_RESET)

    if not getattr(device_lock.claim, "_taptap_single_device_remove_guard", False):
        _ORIGINAL_CLAIM = device_lock.claim
        device_lock.claim = _wrapped_claim(_ORIGINAL_CLAIM)

    logger.info("Single locked-device removal enabled on voucher details.")
