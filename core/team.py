"""Who is signed in, which business they work in, and what they may do.

TeamAccessMiddleware runs right after Django's authentication middleware:

* An owner can keep their own business and may also be a team member of other businesses.
* A team member may belong to more than one business.
* The selected business is stored in the session and request.user.business is
  pointed at that business for the current request so existing views continue
  to work without mixing business data.
* Each business membership keeps its own role and permission set.
* The platform owner (a Django superuser) can still "view as" a business.
* Page views and sign-ins are counted per business, user and day.
"""
import logging
from contextvars import ContextVar

from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.models import User
from django.contrib.auth.signals import user_logged_in
from django.db import transaction
from django.dispatch import receiver
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import Resolver404, resolve
from django.utils import timezone

from .permissions import ALL_PERMISSIONS, ROLES, allowed, landing_for

logger = logging.getLogger('taptap')

VIEW_AS_KEY = 'tt_view_as'
ACTIVE_BUSINESS_KEY = 'tt_business'
SKIP_PREFIXES = ('/admin/', '/static/', '/api/', '/p/', '/b/', '/n/off/', '/n/me/', '/media/', '/ag/')
PLATFORM_OPEN = {
    'logout', 'login', 'home', 'verify_code', 'resend_code', 'cancel_verification',
    'account_password', 'trusted_devices', 'trusted_device_remove',
    'trusted_devices_remove_all', 'chat_page', 'chat_state', 'chat_history',
    'chat_send', 'chat_read', 'chat_direct', 'chat_settings', 'chat_support_action',
}
_BUSINESS_REL = User._meta.get_field('business')
_actor = ContextVar('tt_actor', default='')
_actor_user = ContextVar('tt_actor_user', default=None)


def current_actor():
    """Label of whoever is making the current request (used in the activity log)."""
    return _actor.get()


def current_user():
    """The logged-in User making the current request, or None."""
    return _actor_user.get()


def attach_business(user, business):
    """Make user.business return `business` for this request only."""
    _BUSINESS_REL.set_cached_value(user, business)


def owned_business(user):
    """Return the business this user owns, if any."""
    try:
        return user.business
    except Exception:
        return None


def business_for_user(user):
    """Return the default active business and membership for a user."""
    biz = owned_business(user)
    if biz:
        return biz, None

    from .models_team import TeamMember
    member = (
        TeamMember.objects
        .select_related('business')
        .filter(user=user, is_active=True)
        .order_by('business__business_name', 'pk')
        .first()
    )
    return (member.business, member) if member else (None, None)


def business_access_for_user(user, business_id):
    """Return (business, membership) only when `user` can access business_id."""
    if not business_id:
        return None, None

    try:
        business_id = int(business_id)
    except (TypeError, ValueError):
        return None, None

    owned = owned_business(user)
    if owned and owned.pk == business_id:
        return owned, None

    from .models_team import TeamMember
    member = (
        TeamMember.objects
        .select_related('business')
        .filter(user=user, business_id=business_id, is_active=True)
        .first()
    )
    return (member.business, member) if member else (None, None)


def accessible_businesses(user):
    """Return every business this user may switch to."""
    result = []
    seen = set()

    owned = owned_business(user)
    if owned:
        result.append((owned, None))
        seen.add(owned.pk)

    from .models_team import TeamMember
    memberships = (
        TeamMember.objects
        .select_related('business')
        .filter(user=user, is_active=True)
        .order_by('business__business_name', 'pk')
    )
    for member in memberships:
        if member.business_id in seen:
            continue
        result.append((member.business, member))
        seen.add(member.business_id)

    return result


def _wants_json(request):
    accept = request.headers.get('Accept', '')
    return (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or 'text/html' not in accept
    )


class TeamAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.tt_role = None
        request.tt_perms = frozenset()
        request.tt_member = None
        request.tt_business = None
        request.tt_view_as = None

        token = None
        utoken = None

        if request.user.is_authenticated:
            early = self._identify(request)
            if early is not None:
                return early

            token = _actor.set(self._actor_label(request))
            utoken = _actor_user.set(request.user)

            early = self._gate(request)
            if early is not None:
                _actor.reset(token)
                _actor_user.reset(utoken)
                return early

        try:
            response = self.get_response(request)
        finally:
            if token is not None:
                _actor.reset(token)
            if utoken is not None:
                _actor_user.reset(utoken)

        self._after(request, response)
        return response

    # ── identify active business and role ──
    def _identify(self, request):
        user = request.user

        # Platform support "view as" continues to take priority.
        if user.is_superuser and request.session.get(VIEW_AS_KEY):
            from .models import Business

            biz = Business.objects.filter(pk=request.session[VIEW_AS_KEY]).first()
            if biz:
                attach_business(user, biz)
                request.tt_business = biz
                request.tt_view_as = biz
                request.tt_role = 'owner'
                request.tt_perms = ALL_PERMISSIONS
                return None

            request.session.pop(VIEW_AS_KEY, None)

        # Try the user's selected business first.
        selected_id = request.session.get(ACTIVE_BUSINESS_KEY)
        selected_business, selected_member = business_access_for_user(
            user, selected_id
        )

        # If the selected business is gone, disabled or invalid, choose a valid
        # default business and store it.
        if selected_business is None:
            selected_business, selected_member = business_for_user(user)
            if selected_business:
                request.session[ACTIVE_BUSINESS_KEY] = selected_business.pk
                request.session.modified = True
            else:
                request.session.pop(ACTIVE_BUSINESS_KEY, None)

        if selected_business:
            attach_business(user, selected_business)
            request.tt_business = selected_business
            request.tt_member = selected_member

            if selected_member is None:
                request.tt_role = 'owner'
                request.tt_perms = ALL_PERMISSIONS
            else:
                request.tt_role = selected_member.role
                request.tt_perms = selected_member.permissions

            return None

        if user.is_superuser:
            request.tt_role = 'platform'

        return None

    @staticmethod
    def _actor_label(request):
        user = request.user
        name = user.get_full_name() or user.email or user.username

        if request.tt_view_as:
            return f'TapTap support ({user.email or user.username})'

        if request.tt_member:
            return f'{name} ({request.tt_member.role_label})'

        return name

    # ── permission gate ──
    def _gate(self, request):
        if any(request.path.startswith(prefix) for prefix in SKIP_PREFIXES):
            return None

        try:
            name = resolve(request.path_info).url_name
        except Resolver404:
            return None

        if name and name.startswith('platform_'):
            if not request.user.is_superuser:
                return self._deny(request, name)
            return None

        role = request.tt_role

        # Every authenticated business user must be able to switch between
        # businesses they are authorised to access.
        if name == 'business_switch' and role in ROLES:
            return None

        if role == 'owner':
            return None

        if role == 'platform':
            return None if name in PLATFORM_OPEN else redirect('platform_overview')

        if role is None:
            if name in PLATFORM_OPEN:
                return None

            from .chat import is_agent
            if is_agent(request.user):
                return redirect('chat_page')

            logout(request)
            messages.error(
                request,
                'This login is not linked to a TapTap business.'
            )
            return redirect('login')

        member = request.tt_member
        if (
            member
            and member.must_change_password
            and name not in ('account_password', 'logout', 'business_switch')
            and request.method == 'GET'
        ):
            messages.info(
                request,
                'Please choose your own password to finish setting up your account.'
            )
            return redirect('account_password')

        ok = allowed(request.tt_perms, name)
        if ok:
            return None

        if ok is None:
            logger.warning(
                'Team member %s blocked from unmapped url %s',
                request.user.pk,
                name,
            )

        return self._deny(request, name)

    def _deny(self, request, name):
        landing = landing_for(request.tt_perms, request.tt_role)

        if _wants_json(request):
            return JsonResponse(
                {'ok': False, 'error': 'Your role does not allow this.'},
                status=403,
            )

        if name == 'dashboard' or name == landing:
            return redirect(landing if name != landing else 'support')

        if request.method != 'GET':
            messages.error(
                request,
                'Your role does not allow that action. Ask the business owner if you need it.'
            )
            return redirect(landing)

        role_label = ROLES.get(request.tt_role or '', ('Staff',))[0]
        return render(
            request,
            'core/access_denied.html',
            {'role_label': role_label, 'landing': landing},
            status=403,
        )

    # ── analytics & audit ──
    def _after(self, request, response):
        biz = getattr(request, 'tt_business', None)
        if not biz or not request.user.is_authenticated:
            return

        try:
            if request.tt_view_as:
                if request.method == 'POST':
                    from .models_team import PlatformAudit

                    PlatformAudit.objects.create(
                        actor=request.user,
                        business=biz,
                        action='Change while viewing as owner',
                        details=f'POST {request.path}'[:500],
                        ip=_ip(request),
                    )
                return

            if (
                request.method == 'GET'
                and response.status_code == 200
                and 'text/html' in response.get('Content-Type', '')
            ):
                section = request.path.strip('/').split('/')[0] or 'home'
                record_usage(biz, request.user, section=section)

        except Exception:
            logger.exception('usage tracking failed')


def _ip(request):
    return (
        request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
        or request.META.get('REMOTE_ADDR')
    ) or None


def record_usage(business, user, section=None, login=False):
    from .models_team import UsageDaily

    with transaction.atomic():
        row, _ = UsageDaily.objects.select_for_update().get_or_create(
            business=business,
            user=user,
            date=timezone.localdate(),
        )

        if login:
            row.logins += 1

        if section:
            row.page_views += 1
            sections = dict(row.sections or {})
            sections[section[:40]] = sections.get(section[:40], 0) + 1
            row.sections = sections

        row.last_seen_at = timezone.now()
        row.save()


@receiver(user_logged_in)
def _count_login(sender, request, user, **kwargs):
    try:
        business, _ = business_for_user(user)
        if business:
            record_usage(business, user, login=True)
    except Exception:
        logger.exception('login tracking failed')
