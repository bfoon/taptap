"""Voucher QR extensions and stable hosted customer-portal resolver."""
from __future__ import annotations

import re
from functools import wraps
from urllib.parse import urlencode

from django.shortcuts import redirect, render
from django.urls import path, reverse

from .models import Voucher

_INSTALLED = False


def customer_portal(request):
    """Resolve a printed voucher to the business's current published login portal."""
    code = re.sub(r"\s+", "", str(request.GET.get("v") or ""))[:120]
    voucher = (
        Voucher.objects.select_related("business").filter(code__iexact=code).first()
        if code else None
    )
    if not voucher:
        return render(request, "core/customer_portal_unavailable.html", {"business": None}, status=404)

    business = voucher.business
    page = (
        business.portal_pages.filter(kind="login", is_published=True, is_default=True).first()
        or business.portal_pages.filter(kind="login", is_published=True).order_by("-updated_at").first()
    )
    if not page:
        return render(
            request,
            "core/customer_portal_unavailable.html",
            {"business": business, "voucher": voucher},
            status=404,
        )

    target = reverse("portal_public", args=[page.slug])
    return redirect(target + "?" + urlencode({"username": voucher.code, "voucher": voucher.code}))


def _install_urls():
    from . import urls
    if any(getattr(p, "name", None) == "voucher_customer_portal" for p in urls.urlpatterns):
        return
    urls.urlpatterns.append(path("c/", customer_portal, name="voucher_customer_portal"))


RUNTIME_TAG = '<script src="/static/studio/voucher-qr-runtime.js"></script>'
EDITOR_TAG = '<script src="/static/studio/voucher-qr-extra.js"></script>'


def _inject_scripts(request, response):
    value = request.path.rstrip("/")
    is_editor = bool(re.fullmatch(r"/studio/vouchers/\d+", value))
    is_print = value == "/studio/vouchers/print"
    if not (is_editor or is_print):
        return response
    if getattr(response, "streaming", False) or response.status_code != 200:
        return response
    if "text/html" not in response.get("Content-Type", "").lower():
        return response

    body = response.content.decode(response.charset or "utf-8")
    if "voucher-qr-runtime.js" not in body:
        needle = "studio/voucher-render.js"
        pos = body.find(needle)
        if pos >= 0:
            end = body.find("</script>", pos)
            if end >= 0:
                end += len("</script>")
                body = body[:end] + RUNTIME_TAG + body[end:]

    if is_editor and "voucher-qr-extra.js" not in body:
        needle = "studio/voucher-editor.js"
        pos = body.find(needle)
        if pos >= 0:
            end = body.find("</script>", pos)
            if end >= 0:
                end += len("</script>")
                body = body[:end] + EDITOR_TAG + body[end:]

    output = body.encode(response.charset or "utf-8")
    response.content = output
    if response.has_header("Content-Length"):
        response["Content-Length"] = str(len(output))
    return response


def _install_ui():
    from .team import TeamAccessMiddleware
    original = TeamAccessMiddleware.__call__
    if getattr(original, "_voucher_qr_installed", False):
        return

    @wraps(original)
    def with_voucher_qr(self, request):
        return _inject_scripts(request, original(self, request))

    with_voucher_qr._voucher_qr_installed = True
    TeamAccessMiddleware.__call__ = with_voucher_qr


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _install_urls()
    _install_ui()
    _INSTALLED = True
