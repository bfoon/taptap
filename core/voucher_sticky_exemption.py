"""Per-voucher sticky HotSpot exemption.

Business Settings still controls normal sticky sessions. This module lets one
voucher opt out without weakening its TapTap device lock or max-device rule.

An exempt voucher keeps its current active session, but TapTap removes its
RouterOS HotSpot MAC cookies. The next reconnect therefore shows the portal.
"""
from __future__ import annotations

from datetime import timedelta
from functools import wraps
import logging
import re

from django.contrib import messages
from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import post_save
from django.http import HttpResponseRedirect
from django.utils import timezone
from django.utils.html import escape

from .models import DeviceSignature
from .models_voucher_sticky import VoucherStickyExemption

logger = logging.getLogger("taptap.voucher_sticky_exemption")

_INSTALLED = False
PATH_RE = re.compile(r"^/vouchers/(?P<pk>\d+)/?$")
ACTION_FIELD = "voucher_sticky_action"
DURATIONS = {
    "exempt_1h": timedelta(hours=1),
    "exempt_24h": timedelta(hours=24),
    "exempt_7d": timedelta(days=7),
}


def exemption_for(voucher, now=None):
    now = now or timezone.now()
    try:
        row = voucher.sticky_exemption
    except VoucherStickyExemption.DoesNotExist:
        return None
    if row.expires_at and row.expires_at <= now:
        return None
    return row


def is_exempt(voucher, now=None):
    return exemption_for(voucher, now=now) is not None


def _history(voucher, user, text):
    try:
        from .voucher_history import record
        record(
            voucher,
            "note",
            user=user,
            source="user" if user else "auto",
            kind="sticky_policy",
            text=text,
        )
    except Exception:
        logger.exception("Could not record sticky policy for %s", voucher.code)


def set_exemption(voucher, *, user=None, duration=None, reason=""):
    expires_at = timezone.now() + duration if duration else None
    actor = user if getattr(user, "is_authenticated", False) else None
    row, created = VoucherStickyExemption.objects.update_or_create(
        voucher=voucher,
        defaults={
            "expires_at": expires_at,
            "reason": str(reason or "").strip()[:255],
            "updated_by": actor,
        },
    )
    if created and actor and not row.created_by_id:
        row.created_by = actor
        row.save(update_fields=["created_by", "updated_at"])

    if expires_at:
        text = (
            "Sticky auto-login exempted until "
            f"{timezone.localtime(expires_at):%d %b %Y %H:%M}"
        )
    else:
        text = "Sticky auto-login exempted until manually restored"
    if row.reason:
        text += f" — {row.reason}"
    _history(voucher, actor, text)
    return row


def restore_normal(voucher, *, user=None, reason=""):
    deleted, _ = VoucherStickyExemption.objects.filter(voucher=voucher).delete()
    text = "Sticky auto-login restored to the business setting"
    reason = str(reason or "").strip()[:255]
    if reason:
        text += f" — {reason}"
    _history(voucher, user, text)
    return bool(deleted)


def _install_agent_command():
    """Safe TapTap Link command: remove cookies, never the active session."""
    from . import agent

    kind = "hotspot_user_cookies_remove"
    agent.SAFE_KINDS.add(kind)
    agent.DEFAULT_EXPIRY[kind] = 10
    if kind not in agent.VOUCHER_KINDS:
        agent.VOUCHER_KINDS = agent.VOUCHER_KINDS + (kind,)

    original = agent.command_body
    if getattr(original, "_voucher_sticky_exemption_installed", False):
        return

    @wraps(original)
    def command_body(cmd):
        if cmd.kind == kind:
            username = str((cmd.params or {}).get("user") or "").strip()
            if not username or len(username) > 120 or not agent.NAME_RE.match(username):
                raise ValueError("Invalid voucher/member username.")
            u = agent.rs(username)
            return (
                f":local u {u}; "
                ':do { /ip hotspot cookie remove [find where user=$u] } '
                "on-error={}"
            )
        return original(cmd)

    command_body._voucher_sticky_exemption_installed = True
    agent.command_body = command_body


def clear_all_cookies(voucher, *, user=None):
    """Forget sticky login without disconnecting current HotSpot sessions."""
    if not voucher.router_id:
        return True, "No router assigned; TapTap policy saved."

    from .voucher_history import channel
    try:
        if channel(voucher.router) == "TapTap Link":
            from .linkops import send
            send(
                voucher.router,
                "hotspot_user_cookies_remove",
                {"user": voucher.code},
                label=f"Forget sticky login for {voucher.code}",
                user=user,
                minutes=10,
            )
            return True, f"Cookie removal queued for {voucher.router.name}."

        from .mikrotik import MikroTikService
        svc = MikroTikService(voucher.router).connect()
        try:
            cookies = svc.resource("/ip/hotspot/cookie")
            removed = 0
            for row in cookies.get(user=voucher.code):
                item_id = row.get("id") or row.get(".id")
                if item_id:
                    cookies.remove(id=item_id)
                    removed += 1
        finally:
            svc.close()
        return True, f"Removed {removed} sticky cookie(s) from {voucher.router.name}."
    except Exception as exc:
        logger.exception("Could not clear sticky cookies for %s", voucher.code)
        return False, str(exc)[:250]


def _signature_macs(signature):
    from .device_lock import norm_mac
    out = []
    for value in [signature.last_mac] + list(signature.macs or []):
        mac = norm_mac(value)
        if mac and mac not in out:
            out.append(mac)
    return out[:4]


def _signature_codes(signature):
    out, seen = [], set()
    for value in signature.vouchers or []:
        code = str(value or "").strip()
        key = code.lower()
        if code and key not in seen:
            out.append(code)
            seen.add(key)
        if len(out) >= 30:
            break
    return out


def schedule_after_login(signature):
    """Remove freshly-created RouterOS MAC cookies for exempt vouchers."""
    codes = _signature_codes(signature)
    macs = _signature_macs(signature)
    if not codes or not macs:
        return 0

    vouchers = list(
        signature.business.vouchers
        .filter(code__in=codes, router__isnull=False)
        .select_related("router")
    )
    jobs = []
    for voucher in vouchers:
        if not is_exempt(voucher):
            continue
        for mac in macs:
            key = f"voucher-sticky:{voucher.pk}:{mac}"
            try:
                if not cache.add(key, 1, 50):
                    continue
            except Exception:
                pass
            jobs.append((voucher.pk, mac))

    if not jobs:
        return 0

    def queue():
        from .sticky_exclusions import clear_sticky_cookie
        for voucher_id, mac in jobs:
            for seconds in (5, 18, 45):
                try:
                    clear_sticky_cookie.apply_async(
                        args=[voucher_id, mac],
                        countdown=seconds,
                    )
                except Exception:
                    logger.exception(
                        "Could not queue sticky-cookie clear for voucher %s",
                        voucher_id,
                    )
                    break

    transaction.on_commit(queue)
    return len(jobs)


def _signature_saved(sender, instance, **kwargs):
    key = f"voucher-sticky-sig:{instance.pk}"
    try:
        if not cache.add(key, 1, 8):
            return
    except Exception:
        pass
    try:
        schedule_after_login(instance)
    except Exception:
        logger.exception(
            "Voucher sticky exemption failed for signature %s",
            instance.pk,
        )


def _can_manage(request):
    perms = set(getattr(request, "tt_perms", ()) or ())
    return bool(
        getattr(request, "tt_role", "") == "owner"
        or "vouchers.support" in perms
        or "vouchers.manage" in perms
    )


def _voucher_for_request(request, pk):
    business = getattr(request, "tt_business", None)
    if not business:
        return None
    return (
        business.vouchers
        .filter(pk=pk)
        .select_related("router")
        .first()
    )


def _handle_action(request, voucher):
    action = str(request.POST.get(ACTION_FIELD) or "").strip()
    reason = str(request.POST.get("sticky_reason") or "").strip()[:255]
    actor = request.user if getattr(request.user, "is_authenticated", False) else None

    if action == "normal":
        restore_normal(voucher, user=actor, reason=reason)
        messages.success(
            request,
            f"{voucher.code} now follows the normal business sticky setting.",
        )
        return

    if action in DURATIONS or action == "exempt_forever":
        row = set_exemption(
            voucher,
            user=actor,
            duration=DURATIONS.get(action),
            reason=reason,
        )
        ok, router_msg = clear_all_cookies(voucher, user=actor)
        if row.expires_at:
            until = timezone.localtime(row.expires_at).strftime("%d %b %Y %H:%M")
            headline = f"{voucher.code} is exempt from sticky login until {until}."
        else:
            headline = f"{voucher.code} is exempt until you restore normal sticky."

        if ok:
            messages.success(
                request,
                headline
                + " Current Internet sessions stay online; device locking is unchanged. "
                + router_msg,
            )
        else:
            messages.warning(
                request,
                headline
                + " The policy is saved, but the router cookie could not be cleared yet: "
                + router_msg,
            )
        return

    if action == "clear_once":
        ok, router_msg = clear_all_cookies(voucher, user=actor)
        _history(
            voucher,
            actor,
            "Sticky login forgotten once; saved sticky policy was not changed"
            + (f" — {reason}" if reason else ""),
        )
        if ok:
            messages.success(
                request,
                f"{voucher.code}: sticky login forgotten once. "
                "Current Internet stays online; the portal should appear on the "
                f"next reconnect. {router_msg}",
            )
        else:
            messages.error(
                request,
                f"Could not forget sticky login for {voucher.code}: {router_msg}",
            )
        return

    messages.error(request, "Choose a valid sticky-voucher action.")


def _status(voucher, business):
    now = timezone.now()
    row = exemption_for(voucher, now=now)
    global_on = bool(getattr(business, "sticky_sessions", True))

    if row:
        if row.expires_at:
            until = timezone.localtime(row.expires_at)
            return (
                "exempt",
                f"Exempt until {until:%d %b %Y %H:%M}",
                "No HotSpot MAC cookie is kept until then. After that this voucher automatically returns to the business setting.",
                row,
            )
        return (
            "exempt",
            "Exempt until you restore it",
            "This voucher does not keep HotSpot MAC cookies even while other vouchers stay sticky.",
            row,
        )

    try:
        old = voucher.sticky_exemption
    except VoucherStickyExemption.DoesNotExist:
        old = None

    if not global_on:
        return (
            "global-off",
            "Sticky login is off for the whole business",
            "You can still save an exemption now; it will remain exempt if sticky sessions are enabled later.",
            old,
        )

    if old and old.expires_at and old.expires_at <= now:
        ended = timezone.localtime(old.expires_at)
        return (
            "normal",
            "Normal sticky behaviour",
            f"The temporary exemption ended {ended:%d %b %Y %H:%M}; this voucher follows Settings again.",
            old,
        )

    return (
        "normal",
        "Normal sticky behaviour",
        "This voucher follows Settings → Voucher devices and may auto-login by HotSpot MAC cookie.",
        old,
    )


def _card(voucher, business, can_manage):
    state, title, detail, row = _status(voucher, business)
    badge_cls, icon, badge_text = {
        "exempt": ("warning", "bi-pin-angle-fill", "Sticky exempt"),
        "global-off": ("secondary", "bi-toggle-off", "Sticky globally off"),
        "normal": ("success", "bi-pin-angle", "Normal sticky"),
    }[state]
    reason = escape((row.reason if row else "") or "")

    controls = ""
    if can_manage and not voucher.deleted_at:
        options = [
            ("normal", "Use normal business sticky setting"),
            ("exempt_1h", "No sticky for 1 hour"),
            ("exempt_24h", "No sticky for 24 hours"),
            ("exempt_7d", "No sticky for 7 days"),
            ("exempt_forever", "No sticky until I turn it back on"),
        ]
        opts = '<option value="" selected disabled>Choose a change…</option>' + "".join(
            f'<option value="{value}">{escape(label)}</option>'
            for value, label in options
        )
        controls = f"""
        <form method="post" class="mt-3">
          <input type="hidden" name="csrfmiddlewaretoken" value="__CSRF__">
          <div class="row g-2 align-items-end">
            <div class="col-lg-5">
              <label class="form-label small fw-bold mb-1">Voucher sticky policy</label>
              <select class="form-select" name="{ACTION_FIELD}" required>{opts}</select>
            </div>
            <div class="col-lg-5">
              <label class="form-label small fw-bold mb-1">Reason <span class="text-secondary fw-normal">(optional)</span></label>
              <input class="form-control" name="sticky_reason" maxlength="255" value="{reason}"
                     placeholder="e.g. guest voucher, support test, always show portal">
            </div>
            <div class="col-lg-2 d-grid">
              <button class="btn btn-primary"><i class="bi bi-check2"></i> Apply</button>
            </div>
          </div>
        </form>
        <form method="post" class="mt-2">
          <input type="hidden" name="csrfmiddlewaretoken" value="__CSRF__">
          <input type="hidden" name="{ACTION_FIELD}" value="clear_once">
          <button class="btn btn-sm btn-outline-secondary">
            <i class="bi bi-arrow-counterclockwise"></i> Forget sticky login once
          </button>
          <small class="text-secondary ms-2">Does not change the saved policy.</small>
        </form>"""

    return f"""
    <section class="panel vd-card tt-voucher-sticky" id="voucher-sticky-policy">
      <div class="hd">
        <h3><i class="bi bi-pin-angle"></i> Sticky voucher</h3>
        <span class="badge text-bg-{badge_cls}"><i class="bi {icon}"></i> {badge_text}</span>
      </div>
      <div class="d-flex gap-2 align-items-start">
        <i class="bi bi-shield-check fs-5 text-primary mt-1"></i>
        <div class="small">
          <b>{escape(title)}</b>
          <div class="text-secondary mt-1">{escape(detail)}</div>
          <div class="mt-2">
            <span class="badge text-bg-light border me-1"><i class="bi bi-wifi"></i> Current session stays online</span>
            <span class="badge text-bg-light border"><i class="bi bi-phone-lock"></i> Device lock unchanged</span>
          </div>
        </div>
      </div>
      {controls}
    </section>"""


def _csrf_from_html(body):
    match = re.search(
        r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)["\']',
        body,
        re.I,
    )
    return match.group(1) if match else ""


def _inject_card(request, response, voucher):
    if getattr(response, "streaming", False) or response.status_code != 200:
        return response
    if "text/html" not in response.get("Content-Type", "").lower():
        return response

    body = response.content.decode(response.charset or "utf-8")
    if 'id="voucher-sticky-policy"' in body:
        return response

    marker = '<section class="panel vd-card vd-locked">'
    if marker not in body:
        return response

    business = getattr(request, "tt_business", None)
    if not business:
        return response

    card = _card(voucher, business, _can_manage(request))
    card = card.replace("__CSRF__", escape(_csrf_from_html(body)))
    body = body.replace(marker, card + marker, 1)

    output = body.encode(response.charset or "utf-8")
    response.content = output
    if response.has_header("Content-Length"):
        response["Content-Length"] = str(len(output))
    return response


def _install_voucher_detail_ui():
    """Use the same optional middleware-extension style as sticky_exclusions."""
    from .team import TeamAccessMiddleware

    original = TeamAccessMiddleware.__call__
    if getattr(original, "_voucher_sticky_exemption_installed", False):
        return

    @wraps(original)
    def with_voucher_sticky(self, request):
        response = original(self, request)

        match = PATH_RE.match(request.path)
        if not match:
            return response

        voucher = _voucher_for_request(request, int(match.group("pk")))
        if voucher is None:
            return response

        if request.method == "POST" and ACTION_FIELD in request.POST:
            if not _can_manage(request):
                messages.error(
                    request,
                    "Your role does not allow changing sticky voucher behaviour.",
                )
            else:
                try:
                    _handle_action(request, voucher)
                except Exception as exc:
                    logger.exception(
                        "Could not change sticky policy for %s",
                        voucher.code,
                    )
                    messages.error(
                        request,
                        f"Sticky voucher setting could not be changed: {str(exc)[:180]}",
                    )
            return HttpResponseRedirect(request.path)

        if request.method == "GET":
            return _inject_card(request, response, voucher)

        return response

    with_voucher_sticky._voucher_sticky_exemption_installed = True
    TeamAccessMiddleware.__call__ = with_voucher_sticky


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _install_agent_command()
    _install_voucher_detail_ui()
    post_save.connect(
        _signature_saved,
        sender=DeviceSignature,
        dispatch_uid="taptap-voucher-sticky-exemption-signature",
        weak=False,
    )
    _INSTALLED = True
