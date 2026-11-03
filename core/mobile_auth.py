"""Sign-in for the TapTap mobile app.

The app sends ``Authorization: Bearer <token>``. Each token belongs to one MobileDevice, which owns an ordinary
Django session. This middleware (placed BEFORE SessionMiddleware) hands that session to Django as if it had come
in a cookie, so every existing view — chat, calls, Fit, the team permission gate, the active-business switch —
runs unchanged and checks exactly what it checks on the website. The session cookie Django would send back is
stripped: the app never learns the session key, only its own token.

    * token: 32 random bytes, shown to the app once; TapTap stores only its sha256
    * a changed password ends the session (Django's session auth hash), so the app signs out
    * sliding expiry: 60 days after the phone was last used
    * CSRF does not apply to bearer requests (no cookies are involved, so there is nothing to forge)
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.http import JsonResponse
from django.utils import timezone

SESSION_DAYS = 60
SEEN_EVERY = timedelta(minutes=5)


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _store():
    from importlib import import_module
    return import_module(settings.SESSION_ENGINE).SessionStore


def create_device(user, *, name='', platform='other', app_version='', ip=''):
    """Sign a phone in. Returns (device, token) — the token is never stored and cannot be shown again."""
    from .models_mobile import MobileDevice
    s = _store()()
    s[SESSION_KEY] = str(user.pk)
    s[BACKEND_SESSION_KEY] = 'django.contrib.auth.backends.ModelBackend'
    s[HASH_SESSION_KEY] = user.get_session_auth_hash()
    s['tt_mobile'] = True
    s.set_expiry(SESSION_DAYS * 86400)
    s.create()
    token = 'ttm_' + secrets.token_urlsafe(32)
    dev = MobileDevice.objects.create(user=user, token_hash=_hash(token), session_key=s.session_key, name=str(name or '')[:80],
                                      platform=platform if platform in ('android', 'ios') else 'other',
                                      app_version=str(app_version or '')[:20], last_seen_at=timezone.now(), last_ip=ip[:64])
    return dev, token


def revoke(device):
    """Sign a phone out: the token stops working and its session is deleted."""
    if device.revoked_at is None:
        device.revoked_at = timezone.now()
        device.push_token = ''
        device.save(update_fields=['revoked_at', 'push_token'])
    try:
        _store()().delete(device.session_key)
    except Exception:
        pass


def bearer(request):
    h = request.META.get('HTTP_AUTHORIZATION', '')
    return h[7:].strip() if h[:7].lower() == 'bearer ' else ''


class MobileTokenMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = bearer(request)
        if not token or not token.startswith('ttm_'):
            return self.get_response(request)
        from .models_mobile import MobileDevice
        dev = MobileDevice.objects.filter(token_hash=_hash(token), revoked_at__isnull=True).select_related('user').first()
        if dev is None or not dev.user.is_active:
            return JsonResponse({'ok': False, 'auth': False, 'message': 'Signed out — please sign in again.'}, status=401)
        name = settings.SESSION_COOKIE_NAME
        request.COOKIES = {**request.COOKIES, name: dev.session_key}   # Django loads the device's session
        request._dont_enforce_csrf_checks = True
        request.tt_mobile = dev
        response = self.get_response(request)
        if name in response.cookies:                                    # never hand the session key to the app
            del response.cookies[name]
        if getattr(request, 'user', None) is not None and not request.user.is_authenticated:
            # password changed / session expired: tell the app plainly instead of a redirect to the login page
            if response.status_code in (301, 302):
                return JsonResponse({'ok': False, 'auth': False, 'message': 'Signed out — please sign in again.'}, status=401)
        elif response.status_code in (301, 302):
            # e.g. the subscription ran out, or the password must be changed: the website would redirect — say so
            return JsonResponse({'ok': False, 'redirect': response.get('Location', ''),
                                 'message': 'TapTap needs your attention on the website first (subscription or password).'},
                                status=409)
        now = timezone.now()
        if not dev.last_seen_at or now - dev.last_seen_at > SEEN_EVERY:
            from .auth_security import client_ip
            MobileDevice.objects.filter(pk=dev.pk).update(last_seen_at=now, last_ip=client_ip(request)[:64])
            try:                                                        # sliding expiry
                s = getattr(request, 'session', None)
                if s is not None and s.session_key == dev.session_key and request.user.is_authenticated:
                    s.set_expiry(SESSION_DAYS * 86400)
                    s.save()
            except Exception:
                pass
        return response
