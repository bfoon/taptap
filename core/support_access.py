"""Owner-authorized temporary support access.

The platform console used to let a superuser immediately set ``tt_view_as`` and
enter a customer's business. This module changes that to:

    support requests access
        -> owner is notified
        -> owner approves or denies from Support
        -> the same support user may open the portal
        -> approval expires automatically
        -> owner may revoke access at any time

No extra database table is needed. The existing PlatformAudit table is the
durable, append-only authorization/audit record.

Security properties:
* Only the actual Business.user (the owner login) can approve/deny/revoke.
* A request token is random and is tied to one business and one support user.
* Pending requests expire after 24 hours.
* Approved access expires after 1 hour.
* Middleware validates the grant on every support-view request.
* A revoked/denied/expired approval immediately stops working.
* The old direct ``action=view_as`` cannot bypass approval.
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.core.mail import send_mail
from django.http import HttpResponseForbidden, HttpResponseNotAllowed
from django.shortcuts import redirect
from django.utils import timezone

from .models import Business
from .models_team import PlatformAudit
from .team import VIEW_AS_KEY, _ip

logger = logging.getLogger("taptap.support_access")

GRANT_KEY = "tt_view_as_grant"

REQUEST_TTL = timedelta(hours=24)
APPROVAL_TTL = timedelta(hours=1)

ACTION_REQUESTED = "Support access requested"
ACTION_APPROVED = "Support access approved"
ACTION_DENIED = "Support access denied"
ACTION_REVOKED = "Support access revoked"
ACTION_STARTED = "Support access started"
ACTION_STOPPED = "Support access stopped"

MAX_REASON = 180


def _dump(data):
    """Compact JSON that always fits PlatformAudit.details."""
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=True)
    return raw[:500]


def _load(details):
    try:
        value = json.loads(details or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _dt(value):
    if not value:
        return None
    try:
        d = timezone.datetime.fromisoformat(str(value))
        if timezone.is_naive(d):
            d = timezone.make_aware(d, timezone.get_current_timezone())
        return d
    except Exception:
        return None


def _support_name(user):
    return (user.get_full_name() or user.email or user.username or "TapTap Support").strip()


def _audit(*, actor, business, action, data=None, request=None):
    return PlatformAudit.objects.create(
        actor=actor,
        business=business,
        action=action[:80],
        details=_dump(data or {}),
        ip=_ip(request) if request else None,
    )


def _records(business, token=None, limit=100):
    rows = PlatformAudit.objects.filter(
        business=business,
        action__in=[
            ACTION_REQUESTED,
            ACTION_APPROVED,
            ACTION_DENIED,
            ACTION_REVOKED,
            ACTION_STARTED,
            ACTION_STOPPED,
        ],
    ).select_related("actor").order_by("-created_at")[:limit]
    out = []
    for row in rows:
        data = _load(row.details)
        if token and data.get("token") != token:
            continue
        out.append((row, data))
    return out


def state_for_token(business, token):
    """Return the current authorization state for one request token."""
    if not token:
        return None

    rows = _records(business, token=token, limit=80)
    requested = next(((row, data) for row, data in rows if row.action == ACTION_REQUESTED), None)
    if not requested:
        return None

    req_row, req = requested
    now = timezone.now()
    request_expires = _dt(req.get("request_expires"))
    requester_id = int(req.get("support_user_id") or 0)

    # Decisions are append-only. The newest relevant decision wins.
    decision = next(
        (
            (row, data)
            for row, data in rows
            if row.action in (ACTION_APPROVED, ACTION_DENIED, ACTION_REVOKED)
        ),
        None,
    )

    status = "pending"
    grant_expires = None
    decision_row = None
    decision_data = {}

    if request_expires and request_expires <= now:
        status = "expired"

    if decision:
        decision_row, decision_data = decision
        if decision_row.action == ACTION_APPROVED:
            grant_expires = _dt(decision_data.get("grant_expires"))
            status = "approved" if grant_expires and grant_expires > now else "expired"
        elif decision_row.action == ACTION_DENIED:
            status = "denied"
        elif decision_row.action == ACTION_REVOKED:
            status = "revoked"

    return {
        "token": token,
        "status": status,
        "requester_id": requester_id,
        "requester_name": req.get("support_name") or (
            _support_name(req_row.actor) if req_row.actor else "TapTap Support"
        ),
        "requester_email": req.get("support_email") or (
            req_row.actor.email if req_row.actor else ""
        ),
        "reason": req.get("reason") or "",
        "requested_at": req_row.created_at,
        "request_expires": request_expires,
        "grant_expires": grant_expires,
        "decision_at": decision_row.created_at if decision_row else None,
        "request_row": req_row,
        "decision_row": decision_row,
    }


def owner_requests(business, limit=20):
    """Recent support-access requests for the owner's Support page."""
    if not business:
        return []

    reqs = PlatformAudit.objects.filter(
        business=business,
        action=ACTION_REQUESTED,
    ).select_related("actor").order_by("-created_at")[: max(limit * 2, 30)]

    out = []
    seen = set()
    for row in reqs:
        data = _load(row.details)
        token = data.get("token")
        if not token or token in seen:
            continue
        seen.add(token)
        state = state_for_token(business, token)
        if not state:
            continue
        out.append(state)
        if len(out) >= limit:
            break
    return out


def latest_for_support(business, support_user):
    """Latest request belonging to this support user for this business."""
    rows = PlatformAudit.objects.filter(
        business=business,
        actor=support_user,
        action=ACTION_REQUESTED,
    ).order_by("-created_at")[:10]
    for row in rows:
        token = _load(row.details).get("token")
        state = state_for_token(business, token)
        if state and state["requester_id"] == support_user.pk:
            return state
    return None


def _notify_owner_email(business, support_user, reason):
    owner = business.user
    email = (owner.email or "").strip()
    if not email:
        return

    site = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    support_url = f"{site}/support/" if site else "/support/"
    body = (
        f"Hello {business.owner_name or owner.get_full_name() or 'Business Owner'},\n\n"
        f"{_support_name(support_user)} from TapTap Support is requesting temporary "
        f"permission to open your TapTap business portal for support.\n\n"
        f"Business: {business.business_name}\n"
        f"Reason: {reason or 'Support assistance'}\n\n"
        f"Support CANNOT enter your portal until you approve this request.\n"
        f"If you approve it, access is limited to 1 hour and you can revoke it at any time.\n\n"
        f"Sign in to TapTap yourself and open Support to Approve or Deny:\n"
        f"{support_url}\n\n"
        f"Do not share your password or verification code with support.\n\n"
        f"TapTap"
    )
    try:
        send_mail(
            subject=f"TapTap Support access request — {business.business_name}",
            message=body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=[email],
            fail_silently=True,
        )
    except Exception:
        logger.exception("Could not email support-access request to %s", email)


def _chat_notice(business, sender, body):
    """Put the request/decision in the normal TapTap Support conversation too."""
    try:
        from .chat import post, support_thread
        post(support_thread(business), sender, body, system=True)
    except Exception:
        logger.info("Support-access chat notice could not be posted", exc_info=True)


def create_request(request, business, reason=""):
    """Create or reuse a pending request for this support user."""
    support_user = request.user
    current = latest_for_support(business, support_user)

    if current and current["status"] == "pending":
        return current, False
    if current and current["status"] == "approved":
        return current, False

    token = secrets.token_urlsafe(24)
    now = timezone.now()
    reason = (reason or "").strip()[:MAX_REASON]

    _audit(
        actor=support_user,
        business=business,
        action=ACTION_REQUESTED,
        data={
            "token": token,
            "support_user_id": support_user.pk,
            "support_name": _support_name(support_user),
            "support_email": support_user.email or support_user.username,
            "reason": reason,
            "request_expires": (now + REQUEST_TTL).isoformat(),
        },
        request=request,
    )

    _notify_owner_email(business, support_user, reason)
    _chat_notice(
        business,
        support_user,
        (
            f"Support access requested by {_support_name(support_user)}. "
            f"Reason: {reason or 'Support assistance'}. "
            f"The business owner must approve this from Support before we can enter the portal."
        ),
    )
    return state_for_token(business, token), True


def owner_decide(request, business, token, action):
    """Owner approves, denies or revokes one request."""
    if not business or request.user.pk != business.user_id:
        raise PermissionError("Only the business owner can approve support access.")

    state = state_for_token(business, token)
    if not state:
        raise ValueError("This support access request was not found.")

    now = timezone.now()

    if action == "approve":
        if state["status"] != "pending":
            raise ValueError(f"This request is already {state['status']}.")
        grant_expires = now + APPROVAL_TTL
        _audit(
            actor=request.user,
            business=business,
            action=ACTION_APPROVED,
            data={
                "token": token,
                "support_user_id": state["requester_id"],
                "grant_expires": grant_expires.isoformat(),
            },
            request=request,
        )
        _chat_notice(
            business,
            request.user,
            (
                f"The owner approved temporary support access for "
                f"{state['requester_name']}. Access expires at "
                f"{timezone.localtime(grant_expires):%H:%M}."
            ),
        )
        return "approved", grant_expires

    if action == "deny":
        if state["status"] not in ("pending", "approved"):
            raise ValueError(f"This request is already {state['status']}.")
        _audit(
            actor=request.user,
            business=business,
            action=ACTION_DENIED,
            data={"token": token, "support_user_id": state["requester_id"]},
            request=request,
        )
        _chat_notice(
            business,
            request.user,
            f"The owner denied the support access request from {state['requester_name']}.",
        )
        return "denied", None

    if action == "revoke":
        if state["status"] != "approved":
            raise ValueError("There is no active approval to revoke.")
        _audit(
            actor=request.user,
            business=business,
            action=ACTION_REVOKED,
            data={"token": token, "support_user_id": state["requester_id"]},
            request=request,
        )
        _chat_notice(
            business,
            request.user,
            f"The owner revoked support access for {state['requester_name']}.",
        )
        return "revoked", None

    raise ValueError("Unknown support-access action.")


def _approved_for_open(business, support_user):
    state = latest_for_support(business, support_user)
    if not state:
        return None
    if state["status"] != "approved":
        return None
    if state["requester_id"] != support_user.pk:
        return None
    if not state["grant_expires"] or state["grant_expires"] <= timezone.now():
        return None
    return state


def start_view_as(request, business):
    """Start support view only when owner approval is active."""
    state = _approved_for_open(business, request.user)
    if not state:
        return False, None

    grant = {
        "business_id": business.pk,
        "token": state["token"],
        "support_user_id": request.user.pk,
        "expires_at": state["grant_expires"].isoformat(),
    }

    request.session[VIEW_AS_KEY] = business.pk
    request.session[GRANT_KEY] = grant
    request.session.modified = True

    _audit(
        actor=request.user,
        business=business,
        action=ACTION_STARTED,
        data={
            "token": state["token"],
            "support_user_id": request.user.pk,
            "expires_at": state["grant_expires"].isoformat(),
        },
        request=request,
    )
    return True, state


def validate_session_grant(user, business_id, grant):
    """Middleware validation performed on every request while support is viewing."""
    if not user or not user.is_authenticated or not user.is_superuser:
        return False
    if not isinstance(grant, dict):
        return False

    try:
        gid = int(grant.get("business_id") or 0)
        uid = int(grant.get("support_user_id") or 0)
    except (TypeError, ValueError):
        return False

    if gid != int(business_id or 0) or uid != user.pk:
        return False

    expires = _dt(grant.get("expires_at"))
    if not expires or expires <= timezone.now():
        return False

    business = Business.objects.filter(pk=gid).first()
    if not business:
        return False

    state = state_for_token(business, grant.get("token"))
    if not state:
        return False
    if state["status"] != "approved":
        return False
    if state["requester_id"] != user.pk:
        return False
    if not state["grant_expires"] or state["grant_expires"] <= timezone.now():
        return False

    return True


def _platform_business_action(original):
    @wraps(original)
    def wrapped(request, pk, *args, **kwargs):
        # Let the original protected view handle every other platform action.
        if request.method != "POST" or request.POST.get("action") != "view_as":
            return original(request, pk, *args, **kwargs)

        if not request.user.is_authenticated or not request.user.is_superuser:
            return HttpResponseForbidden("Platform staff only.")

        business = Business.objects.select_related("user").filter(pk=pk).first()
        if not business:
            return original(request, pk, *args, **kwargs)

        reason = (request.POST.get("note") or "").strip()[:MAX_REASON]

        # If the owner has already approved this exact support user, open it.
        ok, state = start_view_as(request, business)
        if ok:
            messages.info(
                request,
                (
                    f"Owner-approved support access started for {business.business_name}. "
                    f"Approval expires at {timezone.localtime(state['grant_expires']):%H:%M}. "
                    f"Every change is logged."
                ),
            )
            return redirect("dashboard")

        # Otherwise this button means REQUEST ACCESS, never direct access.
        state, created = create_request(request, business, reason)
        if created:
            messages.success(
                request,
                (
                    f"Access request sent to {business.owner_name or 'the owner'}. "
                    f"They were notified by email and in TapTap Support. "
                    f"You cannot enter the portal until the owner approves."
                ),
            )
        elif state and state["status"] == "pending":
            messages.info(
                request,
                (
                    f"Owner approval is still pending for {business.business_name}. "
                    f"You cannot enter the portal yet."
                ),
            )
        elif state and state["status"] in ("denied", "revoked", "expired"):
            # A fresh request is required after denial/revocation/expiry.
            state, created = create_request(request, business, reason)
            if created:
                messages.success(
                    request,
                    "A new access request was sent to the owner. Approval is required before access.",
                )
            else:
                messages.warning(request, f"Support access is {state['status']}.")
        else:
            messages.warning(request, "Owner approval is required before support can enter this portal.")

        return redirect("platform_business", pk=business.pk)

    return wrapped


def _platform_view_as_stop(original):
    @wraps(original)
    def wrapped(request, *args, **kwargs):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not request.user.is_authenticated or not request.user.is_superuser:
            return HttpResponseForbidden()

        business_id = request.session.get(VIEW_AS_KEY)
        grant = request.session.get(GRANT_KEY)
        token = grant.get("token") if isinstance(grant, dict) else ""

        request.session.pop(VIEW_AS_KEY, None)
        request.session.pop(GRANT_KEY, None)
        request.session.modified = True

        business = Business.objects.filter(pk=business_id).first() if business_id else None
        if business:
            _audit(
                actor=request.user,
                business=business,
                action=ACTION_STOPPED,
                data={"token": token, "support_user_id": request.user.pk},
                request=request,
            )
            return redirect("platform_business", pk=business.pk)
        return redirect("platform_overview")

    return wrapped


def _secure_identify(original):
    """Replace TeamAccessMiddleware._identify with grant validation in front."""
    @wraps(original)
    def wrapped(self, request):
        user = request.user

        if user.is_authenticated and user.is_superuser:
            business_id = request.session.get(VIEW_AS_KEY)
            if business_id:
                grant = request.session.get(GRANT_KEY)

                if validate_session_grant(user, business_id, grant):
                    business = Business.objects.filter(pk=business_id).first()
                    if business:
                        from .team import attach_business
                        from .permissions import ALL_PERMISSIONS

                        attach_business(user, business)
                        request.tt_business = business
                        request.tt_view_as = business
                        request.tt_role = "owner"
                        request.tt_perms = ALL_PERMISSIONS
                        return None

                # Old / forged / expired / denied / revoked support-view session.
                request.session.pop(VIEW_AS_KEY, None)
                request.session.pop(GRANT_KEY, None)
                request.session.modified = True
                messages.warning(
                    request,
                    "Support view was not opened because there is no active owner approval.",
                )
            else:
                # Do not let an old grant survive after Exit support view.
                request.session.pop(GRANT_KEY, None)

        return original(self, request)

    return wrapped


def install():
    """Install authorization guards once during app startup."""
    from . import views_platform
    from .team import TeamAccessMiddleware

    if not getattr(views_platform.business_action, "_owner_approval_guard", False):
        wrapped = _platform_business_action(views_platform.business_action)
        wrapped._owner_approval_guard = True
        views_platform.business_action = wrapped

    if not getattr(views_platform.view_as_stop, "_owner_approval_guard", False):
        wrapped = _platform_view_as_stop(views_platform.view_as_stop)
        wrapped._owner_approval_guard = True
        views_platform.view_as_stop = wrapped

    if not getattr(TeamAccessMiddleware._identify, "_owner_approval_guard", False):
        wrapped = _secure_identify(TeamAccessMiddleware._identify)
        wrapped._owner_approval_guard = True
        TeamAccessMiddleware._identify = wrapped
