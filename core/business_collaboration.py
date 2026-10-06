"""Owner-to-owner business collaboration built on TapTap's existing multi-business TeamMember model."""
from __future__ import annotations

from functools import wraps
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Business
from .models_collaboration_buy import BusinessCollaboration
from .models_team import TeamMember

logger = logging.getLogger("taptap.collaboration")

# Deliberately excludes team/settings/subscription and destructive/warning
# controls. The business owner chooses exactly which of these capabilities a
# partner owner receives.
COLLAB_PERMISSION_CHOICES = [
    ("dashboard.view", "Overview"),
    ("sales.daily", "Daily sales"),
    ("vouchers.view", "View vouchers and members"),
    ("vouchers.create", "Generate vouchers"),
    ("vouchers.support", "Voucher support"),
    ("plans.manage", "Manage plans"),
    ("reports.view", "Reports"),
    ("finance.view", "View finance"),
    ("finance.agents", "Agents and cash"),
    ("finance.collect", "Collect agent cash"),
    ("network.manage", "Network / MikroTik management"),
    ("studio.manage", "Portal, voucher and advert studio"),
]
SAFE_PERMS = {code for code, _ in COLLAB_PERMISSION_CHOICES}
DEFAULT_GRANTS = ["dashboard.view", "sales.daily", "vouchers.view", "reports.view"]

_INSTALLED = False


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


def _clean_grants(values):
    return sorted({v for v in (values or []) if v in SAFE_PERMS})


def _partner_by_email(email):
    value = (email or "").strip().lower()
    if not value:
        return None
    return (
        Business.objects
        .select_related("user")
        .filter(Q(user__email__iexact=value) | Q(user__username__iexact=value))
        .first()
    )


def _conflicting_membership(business, user):
    return TeamMember.objects.filter(business=business, user=user).first()


def _activate(collab, accepted_by, reverse_grants):
    if collab.status != "pending":
        raise ValueError("This collaboration invitation is no longer pending.")

    source = collab.source_business
    target = collab.target_business

    if source.pk == target.pk:
        raise ValueError("A business cannot collaborate with itself.")

    # Never take ownership of, overwrite, or later delete a membership that was
    # created independently through Team Management.
    conflict_a = _conflicting_membership(source, target.user)
    conflict_b = _conflicting_membership(target, source.user)
    if conflict_a or conflict_b:
        raise ValueError(
            "One of these business owners already has separate Team access to the other business. "
            "Remove that separate Team membership first so collaboration access can be managed safely."
        )

    source_grants = _clean_grants(collab.source_grants)
    target_grants = _clean_grants(reverse_grants)

    with transaction.atomic():
        a = TeamMember.objects.create(
            business=source,
            user=target.user,
            role="collaborator",
            extra_permissions=source_grants,
            is_active=True,
            must_change_password=False,
            created_by=collab.invited_by,
        )
        b = TeamMember.objects.create(
            business=target,
            user=source.user,
            role="collaborator",
            extra_permissions=target_grants,
            is_active=True,
            must_change_password=False,
            created_by=accepted_by,
        )
        collab.source_membership = a
        collab.target_membership = b
        collab.target_grants = target_grants
        collab.status = "active"
        collab.accepted_by = accepted_by
        collab.accepted_at = timezone.now()
        collab.save(
            update_fields=[
                "source_membership",
                "target_membership",
                "target_grants",
                "status",
                "accepted_by",
                "accepted_at",
            ]
        )

    from .utils import log
    log(source, "Business Collaboration", f"Collaboration activated with {target.business_name}.")
    log(target, "Business Collaboration", f"Collaboration activated with {source.business_name}.")
    return collab


def _end(collab, actor):
    if collab.status not in {"pending", "active"}:
        return False

    source = collab.source_business
    target = collab.target_business
    with transaction.atomic():
        # These memberships were only ever created by this collaboration.
        if collab.source_membership_id:
            TeamMember.objects.filter(pk=collab.source_membership_id).delete()
        if collab.target_membership_id:
            TeamMember.objects.filter(pk=collab.target_membership_id).delete()
        collab.source_membership = None
        collab.target_membership = None
        collab.status = "ended"
        collab.ended_at = timezone.now()
        collab.save(
            update_fields=[
                "source_membership",
                "target_membership",
                "status",
                "ended_at",
            ]
        )
    from .utils import log
    log(source, "Business Collaboration", f"Collaboration ended with {target.business_name}.")
    log(target, "Business Collaboration", f"Collaboration ended with {source.business_name}.")
    return True


def _send_invite(collab):
    recipient = collab.target_business.user.email or collab.target_business.user.username
    if not recipient or "@" not in recipient:
        return
    base = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    url = (base + reverse("business_collaboration")) if base else reverse("business_collaboration")
    try:
        send_mail(
            f"[TapTap] {collab.source_business.business_name} invited you to collaborate",
            (
                f"{collab.source_business.business_name} invited your TapTap business "
                f"({collab.target_business.business_name}) to collaborate.\n\n"
                f"Open TapTap to review the permissions and accept or reject:\n{url}\n\n"
                "Each business remains separate. Only explicitly granted capabilities are shared."
            ),
            getattr(settings, "DEFAULT_FROM_EMAIL", None),
            [recipient],
            fail_silently=True,
        )
    except Exception:
        logger.exception("Could not email collaboration invitation")


@login_required
def collaboration(request):
    business = _business(request)
    if not _actual_owner(request, business):
        messages.error(request, "Only the actual business owner can create or manage business collaborations.")
        return redirect("dashboard")

    sent = (
        BusinessCollaboration.objects
        .filter(source_business=business)
        .select_related("source_business", "target_business", "target_business__user")
    )
    received = (
        BusinessCollaboration.objects
        .filter(target_business=business)
        .select_related("source_business", "source_business__user", "target_business")
    )

    return render(
        request,
        "core/collaboration.html",
        {
            "business": business,
            "sent": sent,
            "received": received,
            "permission_choices": COLLAB_PERMISSION_CHOICES,
            "default_grants": DEFAULT_GRANTS,
        },
    )


@login_required
@require_POST
def collaboration_action(request):
    business = _business(request)
    if not _actual_owner(request, business):
        messages.error(request, "Only the actual business owner can manage collaboration.")
        return redirect("dashboard")

    action = str(request.POST.get("action") or "").strip()
    try:
        if action == "invite":
            target = _partner_by_email(request.POST.get("email"))
            if not target:
                raise ValueError(
                    "No TapTap business owner was found with that email. "
                    "The other business must already have a TapTap account."
                )
            if target.pk == business.pk:
                raise ValueError("You cannot invite your own business.")

            existing = (
                BusinessCollaboration.objects
                .filter(
                    Q(source_business=business, target_business=target)
                    | Q(source_business=target, target_business=business),
                    status__in=["pending", "active"],
                )
                .first()
            )
            if existing:
                raise ValueError("There is already a pending or active collaboration between these businesses.")

            grants = _clean_grants(request.POST.getlist("grant"))
            if not grants:
                raise ValueError("Choose at least one capability to share with the partner.")

            collab = BusinessCollaboration.objects.create(
                source_business=business,
                target_business=target,
                source_grants=grants,
                message=(request.POST.get("message") or "").strip()[:255],
                invited_by=request.user,
            )
            _send_invite(collab)
            messages.success(
                request,
                f"Collaboration invitation sent to {target.business_name}. "
                "Nothing is shared until their owner accepts.",
            )

        elif action in {"accept", "reject"}:
            collab = (
                BusinessCollaboration.objects
                .select_related("source_business", "target_business", "source_business__user", "target_business__user")
                .filter(pk=request.POST.get("id"), target_business=business, status="pending")
                .first()
            )
            if not collab:
                raise ValueError("That pending collaboration invitation was not found.")

            if action == "reject":
                collab.status = "rejected"
                collab.ended_at = timezone.now()
                collab.save(update_fields=["status", "ended_at"])
                messages.success(request, f"Collaboration with {collab.source_business.business_name} rejected.")
            else:
                reverse_grants = _clean_grants(request.POST.getlist("grant"))
                if not reverse_grants:
                    raise ValueError(
                        "Choose at least one capability that your business will share with the other owner."
                    )
                _activate(collab, request.user, reverse_grants)
                messages.success(
                    request,
                    f"Collaboration with {collab.source_business.business_name} is active. "
                    "Use the business switcher to move between the two businesses.",
                )

        elif action == "end":
            collab = (
                BusinessCollaboration.objects
                .select_related("source_business", "target_business")
                .filter(
                    Q(source_business=business) | Q(target_business=business),
                    pk=request.POST.get("id"),
                )
                .first()
            )
            if not collab:
                raise ValueError("Collaboration not found.")
            _end(collab, request.user)
            messages.success(request, "Collaboration ended. Cross-business access has been removed.")
        else:
            raise ValueError("Unsupported collaboration action.")

    except ValueError as exc:
        messages.error(request, str(exc))

    return redirect("business_collaboration")


def _install_role_and_permissions():
    from . import permissions

    # Runtime extension keeps Team UI from offering this role manually while
    # making TeamMember.permissions and the existing business switcher understand it.
    permissions.ROLES["collaborator"] = (
        "Collaborator",
        "Business-to-business access. Only explicitly approved collaboration permissions apply.",
        set(),
        "dashboard",
    )
    permissions.URL_PERMS["business_collaboration"] = "team.manage"
    permissions.URL_PERMS["business_collaboration_action"] = "team.manage"


NAV_DESKTOP = (
    '<a href="/collaboration/"><i class="bi bi-buildings"></i> Collaboration</a>'
)
NAV_MOBILE = '<a href="/collaboration/">Collaboration</a>'


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
    if 'href="/collaboration/"' in body:
        return response

    # Desktop: place beside Team when present; otherwise before Support.
    team_anchor = '<a href="/team/"><i class="bi bi-person-badge"></i> Team</a>'
    support_anchor = '<a href="/support/"><i class="bi bi-headset"></i> Support</a>'
    if team_anchor in body:
        body = body.replace(team_anchor, team_anchor + NAV_DESKTOP, 1)
    elif support_anchor in body:
        body = body.replace(support_anchor, NAV_DESKTOP + support_anchor, 1)

    # Mobile occurrence uses plain anchor text.
    mobile_team = '<a href="/team/">Team</a>'
    mobile_support = '<a href="/support/">Support</a>'
    if mobile_team in body:
        body = body.replace(mobile_team, mobile_team + NAV_MOBILE, 1)
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
    if getattr(original, "_taptap_collaboration_nav", False):
        return

    @wraps(original)
    def wrapped(self, request):
        return _inject_nav(request, original(self, request))

    wrapped._taptap_collaboration_nav = True
    TeamAccessMiddleware.__call__ = wrapped


def _install_urls():
    from . import urls
    names = {getattr(p, "name", None) for p in urls.urlpatterns}
    if "business_collaboration" not in names:
        urls.urlpatterns.append(path("collaboration/", collaboration, name="business_collaboration"))
    if "business_collaboration_action" not in names:
        urls.urlpatterns.append(
            path("collaboration/action/", collaboration_action, name="business_collaboration_action")
        )


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _install_role_and_permissions()
    _install_urls()
    _install_nav()
    _INSTALLED = True
