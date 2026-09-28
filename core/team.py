"""Who is signed in, which business they work in, and what they may do.

TeamAccessMiddleware runs right after Django's authentication middleware:

* Owners keep working exactly as before (request.user.business is their business).
* Team members get their employer's business attached to request.user, so every
  existing view that reads request.user.business keeps working unchanged, and
  every request is checked against core.permissions.URL_PERMS.
* The platform owner (a Django superuser) lands on /platform/ and can "view as"
  a business to help its owner; everything changed while viewing is audited.
* Page views and sign-ins are counted per business, user and day for analytics.
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
SKIP_PREFIXES = ('/admin/', '/static/', '/api/', '/p/', '/n/off/', '/media/')
PLATFORM_OPEN = {'logout', 'login', 'home', 'verify_code', 'resend_code', 'cancel_verification', 'account_password',
                 'trusted_devices', 'trusted_device_remove', 'trusted_devices_remove_all'}
_BUSINESS_REL = User._meta.get_field('business')
_actor = ContextVar('tt_actor', default='')


def current_actor():
    """Label of whoever is making the current request (used in the activity log)."""
    return _actor.get()


def attach_business(user, business):
    """Make user.business return `business` for this request without touching the database row."""
    _BUSINESS_REL.set_cached_value(user, business)


def owned_business(user):
    try:
        return user.business
    except Exception:  # RelatedObjectDoesNotExist
        return None


def business_for_user(user):
    """(business, membership) for any user; membership is None for owners."""
    biz = owned_business(user)
    if biz:
        return biz, None
    from .models_team import TeamMember
    m = TeamMember.objects.select_related('business').filter(user=user).first()
    return (m.business, m) if m else (None, None)


def _wants_json(request):
    accept = request.headers.get('Accept', '')
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'text/html' not in accept


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
        if request.user.is_authenticated:
            early = self._identify(request)
            if early is not None:
                return early
            token = _actor.set(self._actor_label(request))
            early = self._gate(request)
            if early is not None:
                _actor.reset(token)
                return early
        try:
            response = self.get_response(request)
        finally:
            if token is not None:
                _actor.reset(token)
        self._after(request, response)
        return response

    # ── who is this? ──
    def _identify(self, request):
        user = request.user
        if user.is_superuser and request.session.get(VIEW_AS_KEY):
            from .models import Business
            biz = Business.objects.filter(pk=request.session[VIEW_AS_KEY]).first()
            if biz:
                attach_business(user, biz)
                request.tt_business, request.tt_view_as = biz, biz
                request.tt_role, request.tt_perms = 'owner', ALL_PERMISSIONS
                return None
            request.session.pop(VIEW_AS_KEY, None)
        biz = owned_business(user)
        if biz:
            request.tt_business, request.tt_role, request.tt_perms = biz, 'owner', ALL_PERMISSIONS
            return None
        from .models_team import TeamMember
        member = TeamMember.objects.select_related('business').filter(user=user).first()
        if member:
            if not member.is_active:
                logout(request)
                messages.error(request, 'Your team account has been switched off. Ask the business owner to turn it back on.')
                return redirect('login')
            attach_business(user, member.business)
            request.tt_business, request.tt_member = member.business, member
            request.tt_role, request.tt_perms = member.role, member.permissions
            return None
        if user.is_superuser:
            request.tt_role = 'platform'
        return None

    @staticmethod
    def _actor_label(request):
        u = request.user
        name = u.get_full_name() or u.email or u.username
        if request.tt_view_as:
            return f'TapTap support ({u.email or u.username})'
        if request.tt_member:
            return f'{name} ({request.tt_member.role_label})'
        return name

    # ── may they open this page? ──
    def _gate(self, request):
        if any(request.path.startswith(p) for p in SKIP_PREFIXES):
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
        if role == 'owner':
            return None
        if role == 'platform':
            return None if name in PLATFORM_OPEN else redirect('platform_overview')
        if role is None:
            if name in PLATFORM_OPEN:
                return None
            logout(request)
            messages.error(request, 'This login is not linked to a TapTap business.')
            return redirect('login')
        member = request.tt_member
        if member and member.must_change_password and name not in ('account_password', 'logout') and request.method == 'GET':
            messages.info(request, 'Please choose your own password to finish setting up your account.')
            return redirect('account_password')
        ok = allowed(request.tt_perms, name)
        if ok:
            return None
        if ok is None:
            logger.warning('Team member %s blocked from unmapped url %s', request.user.pk, name)
        return self._deny(request, name)

    def _deny(self, request, name):
        landing = landing_for(request.tt_perms, request.tt_role)
        if _wants_json(request):
            return JsonResponse({'ok': False, 'error': 'Your role does not allow this.'}, status=403)
        if name == 'dashboard' or name == landing:
            return redirect(landing if name != landing else 'support')
        if request.method != 'GET':
            messages.error(request, 'Your role does not allow that action. Ask the business owner if you need it.')
            return redirect(landing)
        role = ROLES.get(request.tt_role or '', ('Staff',))[0]
        return render(request, 'core/access_denied.html', {'role_label': role, 'landing': landing}, status=403)

    # ── analytics & audit ──
    def _after(self, request, response):
        biz = getattr(request, 'tt_business', None)
        if not biz or not request.user.is_authenticated:
            return
        try:
            if request.tt_view_as:
                if request.method == 'POST':
                    from .models_team import PlatformAudit
                    PlatformAudit.objects.create(actor=request.user, business=biz, action='Change while viewing as owner',
                                                 details=f'POST {request.path}'[:500], ip=_ip(request))
                return
            if request.method == 'GET' and response.status_code == 200 and 'text/html' in response.get('Content-Type', ''):
                section = request.path.strip('/').split('/')[0] or 'home'
                record_usage(biz, request.user, section=section)
        except Exception:  # analytics must never break a page
            logger.exception('usage tracking failed')


def _ip(request):
    return (request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')) or None


def record_usage(business, user, section=None, login=False):
    from .models_team import UsageDaily
    with transaction.atomic():
        row, _ = UsageDaily.objects.select_for_update().get_or_create(business=business, user=user, date=timezone.localdate())
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
        biz, _ = business_for_user(user)
        if biz:
            record_usage(biz, user, login=True)
    except Exception:
        logger.exception('login tracking failed')
