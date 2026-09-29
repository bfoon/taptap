"""Customer-facing expired voucher protection.

This module adds one consistent expiry check to both TapTap portal paths:

1. Hosted portal:
       POST /p/<slug>/check/

2. MikroTik-served portal:
       POST /p/<slug>/state/

An expired voucher returns the same ``blocked`` payload already understood by
TapTap's portal renderer, so the customer gets a full overlay/page instead of
RouterOS's generic authentication error.

No database model or migration is required.
"""
from __future__ import annotations

import json
import re
from functools import wraps

from django.http import JsonResponse
from django.utils import timezone


_INSTALLED = False


def _expired_payload(page, voucher):
    """Return the portal block shown when a voucher has run out of time."""
    from .voucher_history import ends_at

    ended = ends_at(voucher)
    expired_text = (
        timezone.localtime(ended).strftime("%d %b %Y at %H:%M")
        if ended
        else ""
    )

    return {
        "success": False,
        "blocked": True,
        "kind": "expired",
        "title": "Voucher expired",
        "code": voucher.code,
        "plan": voucher.plan_name,
        "message": (
            "The internet time on this voucher has finished. "
            "This voucher can no longer be used to connect."
        ),
        "keep": (
            f"Expired {expired_text}."
            if expired_text
            else "The voucher has reached its time limit."
        ),
        "contact": (
            page.business.support_phone
            or page.business.phone
            or ""
        ),
        "button": "Use another voucher",
    }


def _is_expired(voucher):
    from .voucher_history import time_is_up

    return bool(
        voucher
        and (
            voucher.status == "expired"
            or time_is_up(voucher)
        )
    )


def _request_data(request):
    """Read JSON/text/plain portal payload without raising."""
    if len(request.body or b"") > 8000:
        return {}
    try:
        return json.loads(request.body or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        try:
            return request.POST
        except Exception:
            return {}


def _clean_code(value):
    # Match the same customer-code normalization used by the current portal.
    return re.sub(r"[\s-]", "", str(value or "")).strip()


def _page_and_voucher(slug, code):
    from .models import PortalPage

    page = (
        PortalPage.objects
        .select_related("business")
        .filter(slug=slug)
        .first()
    )
    if not page:
        return None, None

    code = _clean_code(code)
    if not code:
        return page, None

    voucher = (
        page.business.vouchers
        .filter(code__iexact=code)
        .first()
    )
    return page, voucher


def _wrap_hosted(original):
    """Wrap views_studio.portal_check."""

    @wraps(original)
    def hosted(request, slug, *args, **kwargs):
        # Preserve normal method/rate-limit/not-found handling in the existing
        # view. We only intercept a real POST containing an expired voucher.
        if request.method == "POST":
            data = _request_data(request)
            page, voucher = _page_and_voucher(slug, data.get("code"))

            if page and _is_expired(voucher):
                return JsonResponse(
                    _expired_payload(page, voucher),
                    status=403,
                )

        return original(request, slug, *args, **kwargs)

    return hosted


def _wrap_router_state(original):
    """Wrap views_ads.portal_state for router-hosted login.html."""

    @wraps(original)
    def router_state(request, slug, *args, **kwargs):
        if request.method == "OPTIONS":
            return original(request, slug, *args, **kwargs)

        if request.method == "POST":
            data = _request_data(request)
            page, voucher = _page_and_voucher(slug, data.get("code"))

            if page and _is_expired(voucher):
                response = JsonResponse(
                    _expired_payload(page, voucher),
                    status=403,
                )

                # Router-served hotspot pages call TapTap from another origin.
                response["Access-Control-Allow-Origin"] = "*"
                response["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
                response["Access-Control-Allow-Headers"] = "Content-Type"
                response["Cache-Control"] = "no-store"
                return response

        return original(request, slug, *args, **kwargs)

    return router_state


def install():
    """Install the wrappers exactly once during Django app startup."""
    global _INSTALLED

    if _INSTALLED:
        return

    from . import views_ads
    from . import views_studio

    # Guard against Django autoreload importing this more than once.
    if not getattr(views_studio.portal_check, "_taptap_expiry_guard", False):
        wrapped = _wrap_hosted(views_studio.portal_check)
        wrapped._taptap_expiry_guard = True
        views_studio.portal_check = wrapped

    if not getattr(views_ads.portal_state, "_taptap_expiry_guard", False):
        wrapped = _wrap_router_state(views_ads.portal_state)
        wrapped._taptap_expiry_guard = True
        views_ads.portal_state = wrapped

    _INSTALLED = True
