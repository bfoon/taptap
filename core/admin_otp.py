"""Second-factor protection for Django Admin using an emailed OTP."""
from __future__ import annotations

import secrets
import time
from urllib.parse import urlparse

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.core.signing import salted_hmac
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods

OTP_CODE_KEY = "admin_otp_code_hash"
OTP_EXPIRES_KEY = "admin_otp_expires_at"
OTP_LAST_SEND_KEY = "admin_otp_last_send_at"
OTP_ATTEMPTS_KEY = "admin_otp_attempts"
OTP_VERIFIED_USER_KEY = "admin_otp_verified_user"
OTP_VERIFIED_AT_KEY = "admin_otp_verified_at"
OTP_NEXT_KEY = "admin_otp_next"

OTP_TTL_SECONDS = int(getattr(settings, "ADMIN_OTP_TTL_SECONDS", 600))
OTP_RESEND_SECONDS = int(getattr(settings, "ADMIN_OTP_RESEND_SECONDS", 60))
OTP_MAX_ATTEMPTS = int(getattr(settings, "ADMIN_OTP_MAX_ATTEMPTS", 6))
OTP_SESSION_SECONDS = int(getattr(settings, "ADMIN_OTP_SESSION_SECONDS", 43200))


def _now():
    return int(time.time())


def _code_hash(user_id, code):
    return salted_hmac(
        "taptap.admin.otp",
        f"{user_id}:{code}",
        secret=settings.SECRET_KEY,
        algorithm="sha256",
    ).hexdigest()


def _clear_pending(session):
    for key in (OTP_CODE_KEY, OTP_EXPIRES_KEY, OTP_ATTEMPTS_KEY):
        session.pop(key, None)


def clear_admin_otp(session):
    _clear_pending(session)
    for key in (OTP_LAST_SEND_KEY, OTP_VERIFIED_USER_KEY, OTP_VERIFIED_AT_KEY, OTP_NEXT_KEY):
        session.pop(key, None)


def admin_otp_verified(request):
    if not request.user.is_authenticated or not request.user.is_staff:
        return False
    if request.session.get(OTP_VERIFIED_USER_KEY) != request.user.pk:
        return False
    verified_at = int(request.session.get(OTP_VERIFIED_AT_KEY) or 0)
    if not verified_at:
        return False
    if _now() - verified_at > OTP_SESSION_SECONDS:
        clear_admin_otp(request.session)
        return False
    return True


def _safe_next(request, candidate):
    fallback = reverse("admin:index")
    candidate = (candidate or "").strip()
    if not candidate:
        return fallback
    if not url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return fallback
    parsed = urlparse(candidate)
    if not parsed.path.startswith("/admin/") or parsed.path.startswith("/admin/otp/"):
        return fallback
    return candidate


def _masked_email(email):
    email = (email or "").strip()
    if "@" not in email:
        return email or "your administrator email"
    name, domain = email.split("@", 1)
    masked = (name[:2] if len(name) > 2 else name[:1]) + "***"
    return f"{masked}@{domain}"


def _send_code(request, force=False):
    user = request.user
    email = (user.email or "").strip()
    if not email:
        return False, "This administrator account has no email address. Add one before using Admin OTP."

    now = _now()
    last_send = int(request.session.get(OTP_LAST_SEND_KEY) or 0)
    if not force and last_send and now - last_send < OTP_RESEND_SECONDS:
        wait = OTP_RESEND_SECONDS - (now - last_send)
        return False, f"Please wait {wait} seconds before requesting another code."

    code = f"{secrets.randbelow(1_000_000):06d}"
    request.session[OTP_CODE_KEY] = _code_hash(user.pk, code)
    request.session[OTP_EXPIRES_KEY] = now + OTP_TTL_SECONDS
    request.session[OTP_LAST_SEND_KEY] = now
    request.session[OTP_ATTEMPTS_KEY] = 0
    request.session.modified = True

    subject = getattr(settings, "ADMIN_OTP_EMAIL_SUBJECT", "Your TapTap Django Admin security code")
    body = (
        f"Your TapTap Django Admin one-time security code is: {code}\n\n"
        f"This code expires in {max(1, OTP_TTL_SECONDS // 60)} minutes.\n"
        "If you did not try to sign in to Django Admin, change your password immediately.\n"
    )
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [email], fail_silently=False)
    return True, f"A security code was sent to {_masked_email(email)}."


@login_required
@require_http_methods(["GET", "POST"])
def admin_otp(request):
    if not request.user.is_staff:
        return HttpResponseForbidden("Django Admin access is restricted to staff users.")

    next_url = _safe_next(
        request,
        request.GET.get("next") or request.POST.get("next") or request.session.get(OTP_NEXT_KEY),
    )
    request.session[OTP_NEXT_KEY] = next_url

    if admin_otp_verified(request):
        return redirect(next_url)

    if request.method == "GET" and not request.session.get(OTP_CODE_KEY):
        try:
            sent, note = _send_code(request, force=True)
            (messages.success if sent else messages.error)(request, note)
        except Exception:
            messages.error(request, "The Admin OTP email could not be sent. Check SMTP/email settings.")

    if request.method == "POST":
        action = request.POST.get("action", "verify")

        if action == "cancel":
            clear_admin_otp(request.session)
            logout(request)
            messages.info(request, "Administrator sign-in cancelled.")
            return redirect(reverse("admin:login"))

        if action == "resend":
            try:
                sent, note = _send_code(request)
                (messages.success if sent else messages.warning)(request, note)
            except Exception:
                messages.error(request, "The Admin OTP email could not be sent. Check SMTP/email settings.")
            return redirect(f"{reverse('admin_otp')}?next={next_url}")

        code = "".join(ch for ch in request.POST.get("code", "") if ch.isdigit())
        expires_at = int(request.session.get(OTP_EXPIRES_KEY) or 0)
        attempts = int(request.session.get(OTP_ATTEMPTS_KEY) or 0)

        if not request.session.get(OTP_CODE_KEY):
            messages.error(request, "Request a new security code first.")
        elif _now() > expires_at:
            _clear_pending(request.session)
            messages.error(request, "That security code has expired. Request a new one.")
        elif attempts >= OTP_MAX_ATTEMPTS:
            _clear_pending(request.session)
            messages.error(request, "Too many incorrect attempts. Request a new security code.")
        elif len(code) != 6:
            messages.error(request, "Enter the 6-digit security code.")
        elif secrets.compare_digest(request.session.get(OTP_CODE_KEY, ""), _code_hash(request.user.pk, code)):
            _clear_pending(request.session)
            request.session[OTP_VERIFIED_USER_KEY] = request.user.pk
            request.session[OTP_VERIFIED_AT_KEY] = _now()
            request.session.pop(OTP_NEXT_KEY, None)
            request.session.cycle_key()
            request.session.modified = True
            messages.success(request, "Django Admin security check completed.")
            return redirect(next_url)
        else:
            attempts += 1
            request.session[OTP_ATTEMPTS_KEY] = attempts
            left = max(0, OTP_MAX_ATTEMPTS - attempts)
            messages.error(request, f"Incorrect security code. {left} attempt{'s' if left != 1 else ''} remaining.")

    return render(request, "admin/admin_otp.html", {
        "title": "Administrator security verification",
        "next": next_url,
        "masked_email": _masked_email(request.user.email),
        "otp_minutes": max(1, OTP_TTL_SECONDS // 60),
    })


class AdminOTPMiddleware:
    """Block authenticated staff from all /admin/ pages until OTP passes."""

    ALLOWED_ADMIN_PATHS = (
        "/admin/login/",
        "/admin/logout/",
        "/admin/otp/",
        "/admin/jsi18n/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info or ""
        if not path.startswith("/admin/"):
            return self.get_response(request)

        if path.startswith(self.ALLOWED_ADMIN_PATHS):
            if path.startswith("/admin/logout/"):
                clear_admin_otp(request.session)
            return self.get_response(request)

        if not request.user.is_authenticated or not request.user.is_staff:
            return self.get_response(request)

        if admin_otp_verified(request):
            return self.get_response(request)

        request.session[OTP_NEXT_KEY] = request.get_full_path()
        request.session.modified = True
        return redirect(f"{reverse('admin_otp')}?next={request.get_full_path()}")
