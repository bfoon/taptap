"""Public "Buy Wi-Fi Online" storefront with verified mobile-money settlement.

Wave is built in, but stays OFF until server-side credentials and an explicit
ISO currency are configured. Other providers can be added as adapter classes
without changing the storefront or voucher-settlement logic.

Security rule: browser return/success URLs NEVER issue a voucher. Only a
provider adapter that verifies its webhook may call settle_provider_event().
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from functools import wraps
import hashlib
import hmac
import importlib
import json
import logging
import os
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import PAYMENT_METHODS, Voucher, VoucherPlan, VoucherSale
from .models_collaboration_buy import OnlineStorefront, OnlineVoucherPurchase

logger = logging.getLogger("taptap.online_buy")
_INSTALLED = False

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _business(request):
    return getattr(request, "tt_business", None) or request.user.business


def _actual_owner(request, business=None):
    business = business or _business(request)
    return bool(
        business
        and request.user.is_authenticated
        and business.user_id == request.user.pk
        and not getattr(request, "tt_view_as", None)
    )


def _site_url(request=None):
    value = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    if value:
        return value
    return request.build_absolute_uri("/").rstrip("/") if request else ""


def _as_decimal(value):
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Invalid payment amount.")


def _payment_method(provider_code):
    keys = {key for key, _ in PAYMENT_METHODS}
    return provider_code if provider_code in keys else "other"


class PaymentProvider:
    code = ""
    name = ""
    currencies = ()

    def configured_for(self, business):
        return False, "Provider is not configured."

    def create_checkout(self, purchase, request):
        raise NotImplementedError

    def verify_webhook(self, request):
        """Return normalized event:
        {reference, amount, currency, transaction_id, provider_session_id, paid, raw}
        """
        raise NotImplementedError


class WaveProvider(PaymentProvider):
    code = "wave"
    name = "Wave"

    @property
    def api_key(self):
        return os.environ.get("TAPTAP_WAVE_API_KEY", "").strip()

    @property
    def request_signing_secret(self):
        return os.environ.get("TAPTAP_WAVE_SIGNING_SECRET", "").strip()

    @property
    def webhook_secret(self):
        return os.environ.get("TAPTAP_WAVE_WEBHOOK_SECRET", "").strip()

    @property
    def iso_currency(self):
        # Deliberately explicit. Business.currency is a display symbol (e.g. D),
        # not an ISO-4217 code.
        return os.environ.get("TAPTAP_WAVE_CURRENCY", "").strip().upper()

    @property
    def currencies(self):
        return (self.iso_currency,) if self.iso_currency else ()

    def configured_for(self, business):
        if not self.api_key:
            return False, "Wave API key is not configured."
        if not self.webhook_secret:
            return False, "Wave webhook signing secret is not configured."
        if not self.iso_currency:
            return (
                False,
                "Wave ISO currency is not configured. Confirm the currency enabled on your Wave Business API account first.",
            )
        return True, ""

    @staticmethod
    def _wave_signature(body, secret):
        ts = str(int(time.time()))
        digest = hmac.new(
            secret.encode("utf-8"),
            ts.encode("ascii") + body,
            hashlib.sha256,
        ).hexdigest()
        return f"t={ts},v1={digest}"

    def create_checkout(self, purchase, request):
        ok, reason = self.configured_for(purchase.business)
        if not ok:
            raise ValueError(reason)

        base = _site_url(request)
        success_url = (
            base
            + reverse(
                "online_buy_order",
                args=[purchase.reference, purchase.access_token],
            )
        )
        error_url = (
            base
            + reverse(
                "online_buy_order",
                args=[purchase.reference, purchase.access_token],
            )
            + "?payment=error"
        )
        body_obj = {
            "amount": format(purchase.amount, ".2f").rstrip("0").rstrip("."),
            "currency": purchase.currency,
            "client_reference": purchase.reference,
            "success_url": success_url,
            "error_url": error_url,
        }

        # Optional payer locking. Wave requires E.164 for this parameter.
        if (
            os.environ.get("TAPTAP_WAVE_RESTRICT_PAYER", "").strip().lower()
            in {"1", "true", "yes", "on"}
            and purchase.customer_phone.startswith("+")
        ):
            body_obj["restrict_payer_mobile"] = purchase.customer_phone

        body = json.dumps(body_obj, separators=(",", ":")).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.request_signing_secret:
            headers["Wave-Signature"] = self._wave_signature(
                body,
                self.request_signing_secret,
            )

        req = Request(
            "https://api.wave.com/v1/checkout/sessions",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(req, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise ValueError(f"Wave checkout rejected the request: {detail or exc.code}")
        except (URLError, TimeoutError) as exc:
            raise ValueError(f"Wave checkout is temporarily unavailable: {exc}")
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"Wave returned an invalid checkout response: {exc}")

        launch = str(payload.get("wave_launch_url") or "").strip()
        session_id = str(payload.get("id") or "").strip()
        if not launch.startswith("https://") or not session_id:
            raise ValueError("Wave did not return a valid checkout session.")

        purchase.provider_session_id = session_id[:120]
        purchase.provider_payload = {
            "checkout_status": payload.get("checkout_status"),
            "payment_status": payload.get("payment_status"),
        }
        purchase.save(
            update_fields=[
                "provider_session_id",
                "provider_payload",
                "updated_at",
            ]
        )
        return launch

    def _verify_signature(self, raw, header):
        secret = self.webhook_secret
        if not secret or not header:
            return False

        timestamp = None
        signatures = []
        for part in str(header).split(","):
            part = part.strip()
            if part.startswith("t="):
                timestamp = part[2:]
            elif part.startswith("v1="):
                signatures.append(part[3:])

        if not timestamp or not signatures:
            return False
        try:
            ts = int(timestamp)
        except ValueError:
            return False

        now = int(time.time())
        # Wave recommends rejecting old signed webhook requests.
        if ts < now - 300 or ts > now + 30:
            return False

        expected = hmac.new(
            secret.encode("utf-8"),
            timestamp.encode("ascii") + raw,
            hashlib.sha256,
        ).hexdigest()
        return any(hmac.compare_digest(expected, sig) for sig in signatures)

    def verify_webhook(self, request):
        raw = request.body
        signature = request.headers.get("Wave-Signature", "")
        if not self._verify_signature(raw, signature):
            raise PermissionError("Invalid Wave webhook signature.")

        try:
            event = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("Invalid Wave webhook JSON.")

        data = event.get("data") or {}
        event_type = str(event.get("type") or "")
        paid = (
            event_type == "checkout.session.completed"
            and str(data.get("payment_status") or "").lower() == "succeeded"
            and str(data.get("checkout_status") or "").lower() == "complete"
        )

        return {
            "reference": str(data.get("client_reference") or "").strip(),
            "amount": str(data.get("amount") or ""),
            "currency": str(data.get("currency") or "").strip().upper(),
            "transaction_id": str(data.get("transaction_id") or "")[:120],
            "provider_session_id": str(data.get("id") or "")[:120],
            "paid": paid,
            "raw": {
                "event_id": event.get("id"),
                "type": event_type,
                "checkout_status": data.get("checkout_status"),
                "payment_status": data.get("payment_status"),
                "last_payment_error": data.get("last_payment_error"),
            },
        }


def _custom_provider_classes():
    configured = getattr(settings, "TAPTAP_ONLINE_PAYMENT_ADAPTERS", []) or []
    out = []
    for dotted in configured:
        try:
            module, cls = str(dotted).rsplit(".", 1)
            obj = getattr(importlib.import_module(module), cls)()
            if getattr(obj, "code", "") and callable(getattr(obj, "create_checkout", None)):
                out.append(obj)
        except Exception:
            logger.exception("Could not load online payment adapter %s", dotted)
    return out


def all_providers():
    providers = [WaveProvider()]
    providers.extend(_custom_provider_classes())
    # First code wins; prevents a custom adapter accidentally shadowing Wave.
    result = {}
    for provider in providers:
        result.setdefault(provider.code, provider)
    return list(result.values())


def provider_by_code(code):
    code = str(code or "").strip().lower()
    return next((p for p in all_providers() if p.code == code), None)


def active_providers(business):
    rows = []
    for provider in all_providers():
        try:
            ok, reason = provider.configured_for(business)
        except Exception as exc:
            ok, reason = False, str(exc)
        rows.append(
            {
                "provider": provider,
                "code": provider.code,
                "name": provider.name or provider.code.title(),
                "active": bool(ok),
                "reason": reason,
                "currencies": list(getattr(provider, "currencies", ()) or ()),
            }
        )
    return rows


def _store_plans(store):
    qs = (
        store.business.plans
        .filter(active=True, price__gt=0)
        .exclude(name__startswith="*")
        .order_by("price", "name")
    )
    ids = [int(x) for x in (store.plan_ids or []) if str(x).isdigit()]
    if ids:
        qs = qs.filter(pk__in=ids)
    return qs


def _new_voucher_code(business):
    from .voucher_codes import code_taken
    from .utils import portal_code_length

    try:
        length = max(4, min(12, int(portal_code_length(business))))
    except Exception:
        length = 8

    for _ in range(100):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))
        if not code_taken(code):
            return code
    raise RuntimeError("Could not allocate a unique voucher code.")


@transaction.atomic
def issue_paid_purchase(purchase):
    """Issue exactly one voucher and exactly one Finance sale."""
    row = (
        OnlineVoucherPurchase.objects
        .select_for_update()
        .select_related("business", "plan", "router", "storefront")
        .get(pk=purchase.pk)
    )

    if row.status == "issued" and row.voucher_id:
        return row.voucher
    if row.status != "paid":
        raise ValueError("This purchase is not in a paid state.")
    if not row.plan_id:
        row.status = "review"
        row.error = "The purchased plan was deleted."
        row.save(update_fields=["status", "error", "updated_at"])
        raise ValueError(row.error)
    if not row.router_id or row.router.business_id != row.business_id:
        row.status = "review"
        row.error = "The online shop router is missing or no longer belongs to this business."
        row.save(update_fields=["status", "error", "updated_at"])
        raise ValueError(row.error)

    # Re-check the immutable charged amount against the plan snapshot expected
    # for this purchase. A price change after checkout does not increase or
    # decrease what has already been paid.
    plan = row.plan
    code = _new_voucher_code(row.business)
    now = timezone.now()

    voucher = Voucher.objects.create(
        business=row.business,
        router=row.router,
        code=code,
        plan_name=plan.name,
        price=row.amount,
        duration_minutes=plan.duration_minutes,
        max_devices=plan.max_devices,
        source="taptap",
        status="active",
        sold_at=now,
        customer_name=row.customer_name[:120],
        customer_phone=row.customer_phone[:60],
    )

    VoucherSale.objects.create(
        business=row.business,
        voucher=voucher,
        router=row.router,
        plan_name=plan.name,
        voucher_code=voucher.code,
        amount=row.amount,
        payment_method=_payment_method(row.provider),
        customer_name=row.customer_name[:120],
        customer_phone=row.customer_phone[:60],
        reference=(row.provider_transaction_id or row.reference)[:120],
        notes=f"Online purchase {row.reference} via {row.provider}"[:255],
        sold_at=now,
    )

    row.voucher = voucher
    row.status = "issued"
    row.issued_at = now
    row.error = ""
    row.save(
        update_fields=[
            "voucher",
            "status",
            "issued_at",
            "error",
            "updated_at",
        ]
    )

    from .utils import log
    log(
        row.business,
        "Online Voucher Sale",
        (
            f"{voucher.code}: {plan.name} sold online for "
            f"{row.business.currency}{row.amount:,.2f} via {row.provider}"
        ),
    )

    transaction.on_commit(lambda: _after_issue(row.pk))
    return voucher


def _after_issue(purchase_id):
    purchase = (
        OnlineVoucherPurchase.objects
        .select_related("voucher", "router", "business", "plan")
        .filter(pk=purchase_id)
        .first()
    )
    if not purchase or not purchase.voucher_id:
        return

    # Router delivery is best-effort; the normal router sync remains the safety net.
    try:
        from .voucher_push import push_vouchers
        push_vouchers([purchase.voucher], None)
    except Exception as exc:
        logger.exception("Online voucher %s could not be pushed immediately", purchase.voucher.code)
        Voucher.objects.filter(pk=purchase.voucher_id).update(
            mikrotik_sync_status="Pending",
            mikrotik_sync_error=str(exc)[:255],
        )

    if purchase.customer_email:
        try:
            send_mail(
                f"[{purchase.business.business_name}] Your Wi-Fi voucher",
                (
                    f"Payment received.\n\n"
                    f"Plan: {purchase.plan.name if purchase.plan else purchase.voucher.plan_name}\n"
                    f"Voucher code: {purchase.voucher.code}\n"
                    f"Amount: {purchase.business.currency}{purchase.amount:,.2f}\n"
                    f"Reference: {purchase.reference}\n\n"
                    "Keep this voucher until your package has ended."
                ),
                getattr(settings, "DEFAULT_FROM_EMAIL", None),
                [purchase.customer_email],
                fail_silently=True,
            )
        except Exception:
            logger.exception("Could not email online voucher %s", purchase.voucher.code)


@transaction.atomic
def settle_provider_event(provider_code, event):
    reference = str(event.get("reference") or "").strip()
    if not reference:
        raise ValueError("Payment event has no client reference.")

    purchase = (
        OnlineVoucherPurchase.objects
        .select_for_update()
        .select_related("business", "plan", "router", "storefront")
        .filter(reference=reference, provider=provider_code)
        .first()
    )
    if not purchase:
        raise ValueError("Unknown online purchase reference.")

    # Idempotent webhook delivery: Wave/providers may retry.
    if purchase.status == "issued" and purchase.voucher_id:
        return purchase
    if purchase.status == "paid":
        issue_paid_purchase(purchase)
        purchase.refresh_from_db()
        return purchase

    if event.get("provider_session_id"):
        session_id = str(event["provider_session_id"])[:120]
        if purchase.provider_session_id and purchase.provider_session_id != session_id:
            purchase.status = "review"
            purchase.error = "Provider session does not match this purchase."
            purchase.save(update_fields=["status", "error", "updated_at"])
            raise ValueError(purchase.error)
        purchase.provider_session_id = session_id

    purchase.provider_payload = event.get("raw") or {}
    if not event.get("paid"):
        purchase.status = "failed"
        purchase.error = "Payment was not completed successfully."
        purchase.save(
            update_fields=[
                "status",
                "provider_session_id",
                "provider_payload",
                "error",
                "updated_at",
            ]
        )
        return purchase

    event_amount = _as_decimal(event.get("amount"))
    if event_amount != purchase.amount:
        purchase.status = "review"
        purchase.error = (
            f"Payment amount mismatch: expected {purchase.amount}, "
            f"provider reported {event_amount}."
        )
        purchase.save(
            update_fields=[
                "status",
                "provider_session_id",
                "provider_payload",
                "error",
                "updated_at",
            ]
        )
        raise ValueError(purchase.error)

    if str(event.get("currency") or "").upper() != purchase.currency.upper():
        purchase.status = "review"
        purchase.error = "Payment currency does not match this purchase."
        purchase.save(
            update_fields=[
                "status",
                "provider_session_id",
                "provider_payload",
                "error",
                "updated_at",
            ]
        )
        raise ValueError(purchase.error)

    purchase.status = "paid"
    purchase.paid_at = timezone.now()
    purchase.provider_transaction_id = str(event.get("transaction_id") or "")[:120]
    purchase.error = ""
    purchase.save(
        update_fields=[
            "status",
            "paid_at",
            "provider_session_id",
            "provider_transaction_id",
            "provider_payload",
            "error",
            "updated_at",
        ]
    )

    issue_paid_purchase(purchase)
    purchase.refresh_from_db()
    return purchase


@login_required
def online_buy_admin(request):
    business = _business(request)
    if not _actual_owner(request, business):
        messages.error(request, "Only the actual business owner can configure the public online shop.")
        return redirect("dashboard")

    store, _ = OnlineStorefront.objects.get_or_create(business=business)
    plans = (
        business.plans.filter(active=True, price__gt=0)
        .exclude(name__startswith="*")
        .order_by("price", "name")
    )
    providers = active_providers(business)

    if request.method == "POST":
        store.enabled = request.POST.get("enabled") == "1"
        router_id = str(request.POST.get("router") or "")
        store.router = business.routers.filter(pk=router_id).first() if router_id.isdigit() else None
        store.heading = (request.POST.get("heading") or "Buy Wi-Fi online").strip()[:120]
        store.note = (request.POST.get("note") or "").strip()[:255]
        store.require_phone = request.POST.get("require_phone") == "1"
        store.require_email = request.POST.get("require_email") == "1"
        store.plan_ids = [
            int(x)
            for x in request.POST.getlist("plan")
            if str(x).isdigit() and plans.filter(pk=int(x)).exists()
        ]

        if store.enabled and not store.router:
            messages.error(request, "Choose the MikroTik router that receives online vouchers before enabling the shop.")
        elif store.enabled and not any(p["active"] for p in providers):
            messages.error(
                request,
                "No verified mobile-money provider is active yet. The settings were saved, but the shop remains disabled.",
            )
            store.enabled = False
        else:
            messages.success(request, "Online shop settings saved.")

        store.save()
        return redirect("online_buy_admin")

    url = _site_url(request) + reverse("online_buy_store", args=[store.public_slug])
    return render(
        request,
        "core/online_buy_admin.html",
        {
            "business": business,
            "store": store,
            "plans": plans,
            "selected_plan_ids": {int(x) for x in (store.plan_ids or []) if str(x).isdigit()},
            "routers": business.routers.all().order_by("name"),
            "providers": providers,
            "public_url": url,
            "purchases": business.online_purchases.select_related("plan", "voucher")[:30],
        },
    )


def online_buy_store(request, slug):
    store = get_object_or_404(
        OnlineStorefront.objects.select_related("business", "router"),
        public_slug=slug,
    )
    business = store.business
    plans = _store_plans(store)
    providers = [p for p in active_providers(business) if p["active"]]

    if request.method == "POST":
        if not store.enabled:
            messages.error(request, "Online sales are not active for this business.")
            return redirect("online_buy_store", slug=slug)
        if not store.router_id:
            messages.error(request, "This online shop is not connected to a sales router yet.")
            return redirect("online_buy_store", slug=slug)

        plan = plans.filter(pk=request.POST.get("plan")).first()
        if not plan:
            messages.error(request, "Choose an available Wi-Fi plan.")
            return redirect("online_buy_store", slug=slug)

        provider = provider_by_code(request.POST.get("provider"))
        provider_row = next((p for p in providers if p["code"] == getattr(provider, "code", "")), None)
        if not provider or not provider_row:
            messages.error(request, "That payment provider is not currently available.")
            return redirect("online_buy_store", slug=slug)

        phone = (request.POST.get("phone") or "").strip()[:60]
        email = (request.POST.get("email") or "").strip().lower()[:254]
        name = (request.POST.get("name") or "").strip()[:120]
        if store.require_phone and not phone:
            messages.error(request, "Enter the mobile number used for the purchase.")
            return redirect("online_buy_store", slug=slug)
        if store.require_email and not email:
            messages.error(request, "Enter your email address.")
            return redirect("online_buy_store", slug=slug)

        # The provider adapter explicitly defines the ISO currency to charge.
        provider_currency = (
            provider.iso_currency
            if hasattr(provider, "iso_currency")
            else next(iter(getattr(provider, "currencies", ()) or ()), "")
        )
        if not provider_currency:
            messages.error(request, "The payment provider currency is not configured.")
            return redirect("online_buy_store", slug=slug)

        purchase = OnlineVoucherPurchase.objects.create(
            business=business,
            storefront=store,
            plan=plan,
            router=store.router,
            provider=provider.code,
            amount=plan.price,
            currency=provider_currency,
            customer_name=name,
            customer_phone=phone,
            customer_email=email,
        )
        try:
            launch_url = provider.create_checkout(purchase, request)
        except Exception as exc:
            purchase.status = "failed"
            purchase.error = str(exc)[:255]
            purchase.save(update_fields=["status", "error", "updated_at"])
            messages.error(request, str(exc))
            return redirect("online_buy_store", slug=slug)

        # Must be a normal browser redirect. Wave explicitly requires the
        # wave_launch_url to open in the browser rather than an embedded webview.
        return redirect(launch_url)

    return render(
        request,
        "core/online_buy_store.html",
        {
            "store": store,
            "business": business,
            "plans": plans,
            "providers": providers,
        },
    )


def online_buy_order(request, reference, token):
    purchase = get_object_or_404(
        OnlineVoucherPurchase.objects.select_related("business", "plan", "voucher"),
        reference=reference,
        access_token=token,
    )
    return render(
        request,
        "core/online_buy_order.html",
        {
            "purchase": purchase,
            "business": purchase.business,
        },
    )


@csrf_exempt
@require_POST
def online_buy_webhook(request, provider_code):
    provider = provider_by_code(provider_code)
    if not provider:
        return JsonResponse({"ok": False, "error": "unknown provider"}, status=404)
    try:
        event = provider.verify_webhook(request)
        purchase = settle_provider_event(provider.code, event)
    except PermissionError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=401)
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("Online payment webhook failed")
        return JsonResponse({"ok": False, "error": "payment processing failed"}, status=500)

    return JsonResponse(
        {
            "ok": True,
            "reference": purchase.reference,
            "status": purchase.status,
        }
    )


def _install_permissions():
    from . import permissions
    permissions.URL_PERMS["online_buy_admin"] = "settings.manage"


NAV_DESKTOP = '<a href="/online-sales/"><i class="bi bi-phone"></i> Online sales</a>'
NAV_MOBILE = '<a href="/online-sales/">Online sales</a>'


def _inject_nav(request, response):
    business = getattr(request, "tt_business", None)
    if (
        not business
        or not _actual_owner(request, business)
        or getattr(response, "streaming", False)
        or response.status_code != 200
        or "text/html" not in response.get("Content-Type", "").lower()
    ):
        return response

    body = response.content.decode(response.charset or "utf-8")
    if 'href="/online-sales/"' in body:
        return response

    finance_anchor = '<a href="/finance/"><i class="bi bi-cash-stack"></i> Finance</a>'
    support_anchor = '<a href="/support/"><i class="bi bi-headset"></i> Support</a>'
    if finance_anchor in body:
        body = body.replace(finance_anchor, finance_anchor + NAV_DESKTOP, 1)
    elif support_anchor in body:
        body = body.replace(support_anchor, NAV_DESKTOP + support_anchor, 1)

    mobile_finance = '<a href="/finance/">Finance</a>'
    mobile_support = '<a href="/support/">Support</a>'
    if mobile_finance in body:
        body = body.replace(mobile_finance, mobile_finance + NAV_MOBILE, 1)
    elif mobile_support in body:
        body = body.replace(mobile_support, NAV_MOBILE + mobile_support, 1)

    output = body.encode(response.charset or "utf-8")
    response.content = output
    if response.has_header("Content-Length"):
        response["Content-Length"] = str(len(output))
    return response


def _install_nav():
    from .team import TeamAccessMiddleware
    original = TeamAccessMiddleware.__call__
    if getattr(original, "_taptap_online_buy_nav", False):
        return

    @wraps(original)
    def wrapped(self, request):
        return _inject_nav(request, original(self, request))

    wrapped._taptap_online_buy_nav = True
    TeamAccessMiddleware.__call__ = wrapped


def _install_urls():
    from . import urls
    names = {getattr(p, "name", None) for p in urls.urlpatterns}
    routes = [
        ("online_buy_admin", "online-sales/", online_buy_admin),
        # /p/ is already excluded from TeamAccessMiddleware so this remains a
        # truly public storefront even if the visitor happens to be logged in.
        ("online_buy_store", "p/buy/<str:slug>/", online_buy_store),
        ("online_buy_order", "p/buy/order/<str:reference>/<str:token>/", online_buy_order),
        ("online_buy_webhook", "api/online-buy/webhook/<str:provider_code>/", online_buy_webhook),
    ]
    for name, route, view in routes:
        if name not in names:
            urls.urlpatterns.append(path(route, view, name=name))


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _install_permissions()
    _install_urls()
    _install_nav()
    _INSTALLED = True
