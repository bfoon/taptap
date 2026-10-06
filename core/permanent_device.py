"""Permanent device ownership for single-device vouchers.

A normal TapTap sticky voucher learns a device and can deliberately be reset.
For one-device vouchers staff may now mark the existing binding as PERMANENT.

While permanent:
* only that binding's stable portal device ID or its known current/previous MAC
  may use the voucher;
* an unknown/private MAC is never allowed to steal the slot through the normal
  one-device MAC-rotation heuristic;
* the shared-use warning detector ignores failed attempts against this voucher,
  so a stranger typing the code cannot warn/freeze the legitimate customer;
* the HotSpot user is pinned to the approved current MAC when one is known;
* Reset devices is protected until staff explicitly remove permanence.

Removing permanence keeps the normal sticky binding. Staff may then use the
existing Reset devices / Remove device controls if the customer really changed
phone.
"""
from __future__ import annotations

from functools import wraps
from html import escape
import logging
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.views.decorators.http import require_POST

from .models_permanent_device import PermanentVoucherDevice

logger = logging.getLogger("taptap.permanent_device")

_INSTALLED = False
_ORIGINAL_CLAIM = None
_ORIGINAL_SHARED_CHECK = None
_ORIGINAL_SHARED_CASES = None
_ORIGINAL_RESET = None
_ORIGINAL_RELEASE_ALL = None


def _business(request):
    return getattr(request, "tt_business", None) or request.user.business


def _allowed(request):
    business = _business(request)
    if request.user.is_superuser or getattr(business, "user_id", None) == request.user.id:
        return True
    return "vouchers.support" in getattr(request, "tt_perms", frozenset())


def permanent_binding(voucher):
    """Return the protected binding for a one-device voucher, or None."""
    if int(getattr(voucher, "max_devices", 1) or 1) != 1:
        return None
    from .models import VoucherDeviceBinding

    return (
        VoucherDeviceBinding.objects
        .filter(voucher=voucher, permanent_lock__isnull=False)
        .select_related("permanent_lock")
        .first()
    )


def is_permanent(voucher):
    return permanent_binding(voucher) is not None


def _norm_fp(value):
    return str(value or "").strip()[:128]


def _binding_matches(binding, mac="", fp=""):
    """Positive identity only: stable browser ID first, otherwise known MAC."""
    from . import device_lock

    token = _norm_fp(fp)
    if token and binding.device_token_hash and token == binding.device_token_hash:
        return True

    normalized = device_lock.norm_mac(mac)
    if not normalized:
        return False
    return normalized in {
        device_lock.norm_mac(binding.current_mac),
        device_lock.norm_mac(binding.previous_mac),
    } - {""}


def _signature_matches(binding, signature):
    """Whether a DeviceSignature belongs to the permanent device."""
    from . import device_lock

    fp = _norm_fp(getattr(signature, "fingerprint", ""))
    if fp and binding.device_token_hash and fp == binding.device_token_hash:
        return True

    known = {
        device_lock.norm_mac(binding.current_mac),
        device_lock.norm_mac(binding.previous_mac),
    } - {""}
    if not known:
        return False

    sig_macs = set()
    last_mac = device_lock.norm_mac(getattr(signature, "last_mac", ""))
    if last_mac:
        sig_macs.add(last_mac)
    for value in getattr(signature, "macs", None) or []:
        m = device_lock.norm_mac(value)
        if m:
            sig_macs.add(m)
    return bool(known & sig_macs)


def _clean_attempt_signatures(voucher, binding):
    """Forget failed strangers for shared-use counting, but keep the real phone.

    Portal telemetry may record the device signature before device_lock.claim()
    rejects it. While permanent those attempts must never become a later
    shared-use case. We therefore strip this voucher code from every signature
    that is not the permanent device.
    """
    code = str(voucher.code or "").upper()
    if not code:
        return 0

    changed = 0
    for sig in voucher.business.device_signatures.exclude(vouchers=[]).iterator():
        codes = list(sig.vouchers or [])
        if not any(str(c).upper() == code for c in codes):
            continue
        if _signature_matches(binding, sig):
            continue
        sig.vouchers = [c for c in codes if str(c).upper() != code]
        sig.save(update_fields=["vouchers"])
        changed += 1
    return changed


def _pin(voucher, binding):
    """Use the existing safe TapTap Link/API RouterOS MAC operation."""
    from . import device_lock

    mac = device_lock.norm_mac(binding.current_mac)
    if mac:
        device_lock._router_mac(voucher, mac)
        return mac
    return ""


def make_permanent(voucher, user=None):
    """Protect the voucher's one current binding until staff remove the flag."""
    if int(voucher.max_devices or 1) != 1:
        raise ValueError("Permanent device mode is only for single-device vouchers.")

    from .models import Voucher, VoucherDeviceBinding

    who = user if getattr(user, "is_authenticated", False) else None
    with transaction.atomic():
        # Serialize against device reset/claim while permanence is being set.
        Voucher.objects.select_for_update().filter(pk=voucher.pk).first()
        bindings = list(
            VoucherDeviceBinding.objects
            .select_for_update()
            .filter(voucher=voucher)
            .order_by("slot_no", "pk")[:2]
        )
        if not bindings:
            raise ValueError(
                "This voucher has no locked device yet. Let the customer's device connect first, then make it permanent."
            )
        if len(bindings) != 1:
            raise ValueError(
                "A single-device voucher must have exactly one locked device before it can be made permanent."
            )

        binding = bindings[0]
        if not binding.current_mac and not binding.device_token_hash:
            raise ValueError(
                "TapTap does not yet have enough identity information for this device. Let it connect through the portal first."
            )

        lock, created = PermanentVoucherDevice.objects.get_or_create(
            binding=binding,
            defaults={"created_by": who},
        )
        if not created and lock.created_by_id is None and who is not None:
            lock.created_by = who
            lock.save(update_fields=["created_by"])

    removed = _clean_attempt_signatures(voucher, binding)
    mac = _pin(voucher, binding)

    from .voucher_history import record, channel

    label = binding.label or mac or "Device"
    record(
        voucher,
        "device",
        user=user,
        source="user",
        reason="Permanent voucher device",
        via=channel(voucher.router),
        status_before=voucher.status,
        status_after=voucher.status,
        kind="permanent_device",
        binding_id=binding.pk,
        mac=mac or None,
        device_token=(binding.device_token_hash[:12] if binding.device_token_hash else None),
        rejected_signatures_cleared=removed or None,
        text=(
            f"{label} made permanent on this single-device voucher. "
            "Only this device may use the voucher until staff remove the permanent lock."
        ),
    )
    return binding, created


def remove_permanent(voucher, user=None):
    """Remove permanence but keep the ordinary sticky device binding."""
    from .models import Voucher, VoucherDeviceBinding

    with transaction.atomic():
        Voucher.objects.select_for_update().filter(pk=voucher.pk).first()
        binding = (
            VoucherDeviceBinding.objects
            .select_for_update()
            .filter(voucher=voucher, permanent_lock__isnull=False)
            .select_related("permanent_lock")
            .first()
        )
        if binding is None:
            return None, False

        # Clean failed attempts before deleting the permanent marker. Otherwise
        # an old rejected phone could immediately appear as a fresh sharing case.
        removed = _clean_attempt_signatures(voucher, binding)
        try:
            lock = binding.permanent_lock
        except PermanentVoucherDevice.DoesNotExist:
            return binding, False
        lock.delete()

    from . import device_lock
    from .voucher_history import record, channel

    device_lock.unlock_on_router(voucher)

    label = binding.label or binding.current_mac or "Device"
    record(
        voucher,
        "device",
        user=user,
        source="user",
        reason="Permanent device removed",
        via=channel(voucher.router),
        status_before=voucher.status,
        status_after=voucher.status,
        kind="permanent_device_removed",
        binding_id=binding.pk,
        rejected_signatures_cleared=removed or None,
        text=(
            f"Permanent protection removed from {label}. "
            "The device remains normally locked to the voucher until staff reset/remove it."
        ),
    )
    return binding, True


def _install_claim_guard():
    """Reject unknown devices before the normal one-device heuristic can move a slot."""
    global _ORIGINAL_CLAIM

    from . import device_lock

    if getattr(device_lock.claim, "_taptap_permanent_device", False):
        return

    _ORIGINAL_CLAIM = device_lock.claim

    @wraps(_ORIGINAL_CLAIM)
    def claim(voucher, mac="", fp="", source="portal", label="", hints=None):
        binding = permanent_binding(voucher)
        if binding is None:
            return _ORIGINAL_CLAIM(
                voucher,
                mac=mac,
                fp=fp,
                source=source,
                label=label,
                hints=hints,
            )

        # Permanent means positive identity only. Do NOT let the normal
        # private-MAC/hostname heuristic decide that an unknown phone is the
        # old one merely because the approved phone happens to be offline.
        if not _binding_matches(binding, mac=mac, fp=fp):
            return device_lock.Outcome(
                "denied",
                message=(
                    "This voucher is permanently assigned to another device. "
                    "This device cannot use it. Ask staff to remove the permanent device lock "
                    "only if the customer has actually changed phone."
                ),
            )

        old_mac = device_lock.norm_mac(binding.current_mac)
        out = _ORIGINAL_CLAIM(
            voucher,
            mac=mac,
            fp=fp,
            source=source,
            label=label,
            hints=hints,
        )

        # A matching stable portal device ID is allowed to follow the same
        # phone when it rotates its private Wi-Fi MAC. Re-pin RouterOS before
        # the login continues, so the new MAC becomes the only accepted one.
        if out.allowed and out.binding is not None:
            new_mac = device_lock.norm_mac(out.binding.current_mac)
            if new_mac and new_mac != old_mac:
                device_lock._router_mac(voucher, new_mac)
        return out

    claim._taptap_permanent_device = True
    device_lock.claim = claim


def _install_shared_use_guard():
    """A rejected device attempt must not create an auto warning/freeze."""
    global _ORIGINAL_SHARED_CHECK, _ORIGINAL_SHARED_CASES

    from . import shared_use

    if not getattr(shared_use.check_after_login, "_taptap_permanent_device", False):
        _ORIGINAL_SHARED_CHECK = shared_use.check_after_login

        @wraps(_ORIGINAL_SHARED_CHECK)
        def check_after_login(business, voucher):
            if is_permanent(voucher):
                # record_device() may already have seen the attempted phone.
                # It is deliberately ignored here; claim() will reject it.
                return False
            return _ORIGINAL_SHARED_CHECK(business, voucher)

        check_after_login._taptap_permanent_device = True
        shared_use.check_after_login = check_after_login

    if not getattr(shared_use.cases, "_taptap_permanent_device", False):
        _ORIGINAL_SHARED_CASES = shared_use.cases

        @wraps(_ORIGINAL_SHARED_CASES)
        def cases(business, include_resolved=True, limit=200):
            # Ask the original for enough rows, then omit vouchers whose
            # device ownership is deliberate and permanent.
            rows = _ORIGINAL_SHARED_CASES(
                business,
                include_resolved=include_resolved,
                limit=max(int(limit or 200), 1000),
            )
            out = [row for row in rows if not is_permanent(row["voucher"])]
            return out[:limit]

        cases._taptap_permanent_device = True
        shared_use.cases = cases


def _install_reset_guard():
    """Reset/remove cannot silently destroy a permanent device."""
    global _ORIGINAL_RESET, _ORIGINAL_RELEASE_ALL

    from . import device_lock
    from . import voucher_history as vh

    if not getattr(vh.reset_devices, "_taptap_permanent_device", False):
        _ORIGINAL_RESET = vh.reset_devices

        @wraps(_ORIGINAL_RESET)
        def reset_devices(voucher, user=None, reason=""):
            if is_permanent(voucher):
                return (
                    False,
                    "This voucher has a permanent device. Remove the permanent lock first; "
                    "the approved device was not changed.",
                )
            return _ORIGINAL_RESET(voucher, user=user, reason=reason)

        reset_devices._taptap_permanent_device = True
        vh.reset_devices = reset_devices

    # Defence in depth for any code path that calls release_all directly.
    if not getattr(device_lock.release_all, "_taptap_permanent_device", False):
        _ORIGINAL_RELEASE_ALL = device_lock.release_all

        @wraps(_ORIGINAL_RELEASE_ALL)
        def release_all(voucher):
            if is_permanent(voucher):
                logger.warning(
                    "Ignored release_all for permanent voucher %s; remove permanence first",
                    voucher.code,
                )
                return []
            return _ORIGINAL_RELEASE_ALL(voucher)

        release_all._taptap_permanent_device = True
        device_lock.release_all = release_all


@login_required
@require_POST
def permanent_device_action(request, pk):
    if not _allowed(request):
        raise PermissionDenied

    business = _business(request)
    voucher = get_object_or_404(
        business.vouchers,
        pk=pk,
        deleted_at__isnull=True,
    )
    action = str(request.POST.get("action") or "make").strip().lower()

    try:
        if action == "remove":
            binding, changed = remove_permanent(voucher, request.user)
            if changed:
                messages.success(
                    request,
                    (
                        f"{voucher.code}: permanent protection removed. "
                        "The same device is still normally locked to the voucher; "
                        "use Reset devices only if you want to replace it."
                    ),
                )
            else:
                messages.info(request, f"{voucher.code} does not have a permanent device.")
        else:
            binding, created = make_permanent(voucher, request.user)
            label = binding.label or binding.current_mac or "the locked device"
            if created:
                messages.success(
                    request,
                    (
                        f"{voucher.code}: {label} is now permanent. "
                        "Other devices are refused and their attempts will not warn or freeze this voucher."
                    ),
                )
            else:
                messages.info(
                    request,
                    f"{voucher.code}: {label} is already the permanent device.",
                )
    except ValueError as exc:
        messages.error(request, str(exc))

    return redirect("voucher_detail", pk=voucher.pk)


def _install_urls_and_permission():
    from . import urls
    from . import permissions

    # TeamAccessMiddleware fails closed for an unmapped URL, so explicitly use
    # the existing voucher-support permission for this support action.
    permissions.URL_PERMS["permanent_device_action"] = "vouchers.support"

    if not any(getattr(item, "name", None) == "permanent_device_action" for item in urls.urlpatterns):
        urls.urlpatterns.append(
            path(
                "vouchers/<int:pk>/permanent-device/",
                permanent_device_action,
                name="permanent_device_action",
            )
        )


def _voucher_from_request(request):
    match = getattr(request, "resolver_match", None)
    if not match or match.url_name != "voucher_detail":
        return None
    pk = (match.kwargs or {}).get("pk")
    if not pk:
        return None
    business = getattr(request, "tt_business", None)
    if business is None:
        return None
    return business.vouchers.filter(pk=pk).first()


def _device_panel_html(request, voucher, binding, permanent):
    token = escape(get_token(request), quote=True)
    action_url = escape(reverse("permanent_device_action", args=[voucher.pk]), quote=True)

    if binding is None:
        return """
        <div class="alert alert-light border small mt-2 mb-2 py-2">
          <i class="bi bi-pin-angle"></i>
          <b>Permanent device:</b> connect the customer's device first. Once it appears below,
          you can make that exact device permanent on this voucher.
        </div>
        """

    label = escape(binding.label or "Device")
    mac = escape(binding.current_mac or "")
    identity = f" · <code>{mac}</code>" if mac else ""
    csrf = f'<input type="hidden" name="csrfmiddlewaretoken" value="{token}">'

    if permanent:
        return f"""
        <div class="alert alert-success small mt-2 mb-2 py-2">
          <div class="d-flex flex-wrap gap-2 align-items-center justify-content-between">
            <div>
              <span class="badge text-bg-success me-1"><i class="bi bi-pin-angle-fill"></i> Permanent device</span>
              <b>{label}</b>{identity}
              <div class="mt-1">
                Only this device can use the voucher. Other devices are rejected without
                creating the normal shared-device warning or freezing this voucher.
              </div>
            </div>
            <form method="post" action="{action_url}" class="m-0"
                  onsubmit="return confirm('Remove permanent protection from this voucher? The current device will remain normally locked until you reset it.');">
              {csrf}
              <input type="hidden" name="action" value="remove">
              <button class="btn btn-sm btn-outline-danger" type="submit">
                <i class="bi bi-pin-angle"></i> Remove permanent
              </button>
            </form>
          </div>
        </div>
        <script>
        document.addEventListener('DOMContentLoaded', function(){{
          document.querySelectorAll('[data-bs-target="#resetModal"]').forEach(function(btn){{
            btn.disabled = true;
            btn.title = 'Remove the permanent device lock first';
          }});
        }});
        </script>
        """

    return f"""
    <div class="alert alert-light border small mt-2 mb-2 py-2">
      <div class="d-flex flex-wrap gap-2 align-items-center justify-content-between">
        <div>
          <i class="bi bi-pin-angle"></i>
          <b>Make this device permanent?</b> {label}{identity}
          <div class="text-secondary mt-1">
            For this single-device voucher, no other device will be allowed to take its place
            or trigger a shared-device warning/freeze until permanence is removed.
          </div>
        </div>
        <form method="post" action="{action_url}" class="m-0"
              onsubmit="return confirm('Make this the permanent device for voucher {escape(voucher.code)}? Other devices will be refused until you remove the permanent lock.');">
          {csrf}
          <input type="hidden" name="action" value="make">
          <button class="btn btn-sm btn-primary" type="submit">
            <i class="bi bi-pin-angle-fill"></i> Make permanent
          </button>
        </form>
      </div>
    </div>
    """


def _inject_voucher_ui(request, response):
    if (
        getattr(response, "streaming", False)
        or response.status_code != 200
        or "text/html" not in response.get("Content-Type", "").lower()
        or not getattr(request.user, "is_authenticated", False)
    ):
        return response

    voucher = _voucher_from_request(request)
    if voucher is None or int(voucher.max_devices or 1) != 1 or voucher.deleted_at:
        return response
    if not _allowed(request):
        return response

    body = response.content.decode(response.charset or "utf-8")
    if "tt-permanent-device-control" in body:
        return response

    marker = '<div class="panel-head"><div><h3><i class="bi bi-phone-vibrate"></i> Locked devices</h3>'
    if marker not in body:
        return response

    binding = voucher.device_bindings.order_by("slot_no", "pk").first()
    permanent = bool(binding and PermanentVoucherDevice.objects.filter(binding=binding).exists())
    block = (
        '<div id="tt-permanent-device-control">'
        + _device_panel_html(request, voucher, binding, permanent)
        + "</div>"
    )
    body = body.replace(marker, marker + block, 1)

    output = body.encode(response.charset or "utf-8")
    response.content = output
    if response.has_header("Content-Length"):
        response["Content-Length"] = str(len(output))
    return response


def _install_ui():
    from .team import TeamAccessMiddleware

    original = TeamAccessMiddleware.__call__
    if getattr(original, "_taptap_permanent_device_ui", False):
        return

    @wraps(original)
    def with_permanent_device_ui(self, request):
        response = original(self, request)
        try:
            return _inject_voucher_ui(request, response)
        except Exception:
            logger.exception("Permanent-device voucher UI injection failed")
            return response

    with_permanent_device_ui._taptap_permanent_device_ui = True
    TeamAccessMiddleware.__call__ = with_permanent_device_ui


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _install_urls_and_permission()
    _install_claim_guard()
    _install_shared_use_guard()
    _install_reset_guard()
    _install_ui()
    _INSTALLED = True
    logger.info(
        "Permanent single-device voucher protection enabled: positive identity only, "
        "shared-use warnings suppressed for rejected attempts, reset protected."
    )
