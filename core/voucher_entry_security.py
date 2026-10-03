from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta

from django.http import JsonResponse
from django.template.loader import render_to_string
from django.utils import timezone

from .models_voucher_entry import VoucherEntryDevice, VoucherEntryPolicy

MAC_RE = re.compile(r'^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$')


def _norm_mac(value):
    raw = ''.join(c for c in str(value or '').upper() if c in '0123456789ABCDEF')
    if len(raw) != 12:
        return ''
    mac = ':'.join(raw[i:i + 2] for i in range(0, 12, 2))
    return mac if MAC_RE.match(mac) else ''


def _client_ip(request):
    return (
        request.META.get('HTTP_X_FORWARDED_FOR')
        or request.META.get('REMOTE_ADDR')
        or ''
    ).split(',')[0].strip()[:64]


def _body(request):
    try:
        raw = request.body or b''
        return json.loads(raw.decode('utf-8')) if raw else {}
    except Exception:
        return request.POST.dict() if hasattr(request, 'POST') else {}


def _identity(request, data):
    mac = _norm_mac(data.get('mac', ''))
    fp = str(data.get('fp') or '').strip()
    ip = str(data.get('ip') or _client_ip(request)).strip()[:64]

    # Prefer the browser/device fingerprint when the portal sends one. This
    # keeps the counter attached to the same phone even if Android/iOS rotates
    # its private Wi-Fi MAC after reconnecting. Keep the current MAC on the row
    # for staff visibility.
    if fp:
        digest = hashlib.sha256(fp.encode('utf-8', 'ignore')).hexdigest()
        return f'fp:{digest}', mac, digest, ip

    if mac:
        return f'mac:{mac}', mac, '', ip

    digest = hashlib.sha256(ip.encode('utf-8', 'ignore')).hexdigest()
    return f'ip:{digest}', '', '', ip


def _policy(business):
    obj, _ = VoucherEntryPolicy.objects.get_or_create(
        business=business,
        defaults={
            'enabled': False,
            'max_attempts': 5,
            'warning_remaining': 2,
            'window_minutes': 10,
            'block_minutes': 30,
        },
    )
    return obj


def _support_number(business):
    return (business.support_phone or business.phone or '').strip()


def _blocked_message(policy, business, row):
    support = _support_number(business)
    until = timezone.localtime(row.blocked_until) if row.blocked_until else None
    when = until.strftime('%H:%M') if until else ''

    base = (
        policy.blocked_text.strip()
        if policy.blocked_text.strip()
        else (
            f'This device is temporarily blocked from entering voucher codes '
            f'because too many incorrect entries were made. '
            f'Try again after {when}.'
        )
    )

    if support:
        base += f' If you need access sooner, call {support} and ask staff to unblock your device.'
    else:
        base += ' If you need access sooner, contact staff and ask them to unblock your device.'

    return base


def _warning_message(policy, remaining):
    custom = policy.warning_text.strip()
    if custom:
        return f'{custom} {remaining} attempt{"s" if remaining != 1 else ""} remaining.'

    return (
        f'Warning: {remaining} incorrect voucher entr'
        f'{"ies" if remaining != 1 else "y"} remaining before this device is temporarily blocked.'
    )


def _get_row(business, request, data):
    device_key, mac, fp_hash, ip = _identity(request, data)

    row, _ = VoucherEntryDevice.objects.get_or_create(
        business=business,
        device_key=device_key,
        defaults={
            'mac_address': mac,
            'fingerprint_hash': fp_hash,
            'ip_address': ip,
        },
    )

    changed = []

    if mac and row.mac_address != mac:
        row.mac_address = mac
        changed.append('mac_address')

    if fp_hash and row.fingerprint_hash != fp_hash:
        row.fingerprint_hash = fp_hash
        changed.append('fingerprint_hash')

    if ip and row.ip_address != ip:
        row.ip_address = ip
        changed.append('ip_address')

    if changed:
        row.save(update_fields=changed + ['last_seen_at'])

    return row


def _reset_if_window_expired(row, policy, now):
    if (
        row.window_started_at
        and now - row.window_started_at > timedelta(minutes=max(1, int(policy.window_minutes or 10)))
        and not row.is_blocked
    ):
        row.attempts = 0
        row.window_started_at = None
        row.save(update_fields=['attempts', 'window_started_at', 'last_seen_at'])


def _clear_expired_block(row, now):
    if row.blocked_until and row.blocked_until <= now:
        row.attempts = 0
        row.window_started_at = None
        row.blocked_at = None
        row.blocked_until = None
        row.save(
            update_fields=[
                'attempts',
                'window_started_at',
                'blocked_at',
                'blocked_until',
                'last_seen_at',
            ],
        )


def _before_check(page, request, data):
    policy = _policy(page.business)

    if not policy.enabled:
        return None, None, policy

    now = timezone.now()
    row = _get_row(page.business, request, data)

    _clear_expired_block(row, now)
    _reset_if_window_expired(row, policy, now)
    row.refresh_from_db()

    if row.is_blocked:
        return row, JsonResponse(
            {
                'success': False,
                'blocked': True,
                'security_block': True,
                'attempts_remaining': 0,
                'blocked_until': row.blocked_until.isoformat(),
                'support_phone': _support_number(page.business),
                'message': _blocked_message(policy, page.business, row),
            },
            status=403,
        ), policy

    return row, None, policy


def _invalid_login_response(response):
    if getattr(response, 'status_code', 200) != 404:
        return False

    try:
        data = json.loads(response.content.decode('utf-8'))
    except Exception:
        return False

    if data.get('success') is not False:
        return False

    message = str(data.get('message') or '').lower()

    return (
        'not recognised' in message
        or 'not recognized' in message
        or 'username or password is wrong' in message
    )


def _success_response(response):
    if getattr(response, 'status_code', 500) >= 400:
        return False

    try:
        data = json.loads(response.content.decode('utf-8'))
    except Exception:
        return False

    return data.get('success') is True


def _record_failure(page, row, policy, member=False):
    now = timezone.now()
    max_attempts = policy.safe_max_attempts

    if not row.window_started_at:
        row.window_started_at = now
        row.attempts = 0

    row.attempts = min(65535, int(row.attempts or 0) + 1)
    row.last_attempt_at = now

    remaining = max(0, max_attempts - row.attempts)

    if row.attempts >= max_attempts:
        row.blocked_at = now
        row.blocked_until = now + timedelta(minutes=max(1, int(policy.block_minutes or 30)))
        row.save(
            update_fields=[
                'attempts',
                'window_started_at',
                'last_attempt_at',
                'blocked_at',
                'blocked_until',
                'last_seen_at',
            ],
        )

        return JsonResponse(
            {
                'success': False,
                'blocked': True,
                'security_block': True,
                'attempts_remaining': 0,
                'blocked_until': row.blocked_until.isoformat(),
                'support_phone': _support_number(page.business),
                'message': _blocked_message(policy, page.business, row),
            },
            status=403,
        )

    row.save(
        update_fields=[
            'attempts',
            'window_started_at',
            'last_attempt_at',
            'last_seen_at',
        ],
    )

    warn = remaining <= policy.safe_warning_remaining

    message = (
        'Username or password is wrong. Check them and try again.'
        if member
        else 'That code was not recognised. Check the letters and try again.'
    )

    if warn:
        message += ' ' + _warning_message(policy, remaining)

    return JsonResponse(
        {
            'success': False,
            'warning': warn,
            'security_warning': warn,
            'attempts_used': row.attempts,
            'attempts_remaining': remaining,
            'message': message,
        },
        status=404,
    )


def _reset_on_success(row):
    if not row:
        return

    if row.attempts or row.window_started_at:
        row.attempts = 0
        row.window_started_at = None
        row.save(
            update_fields=[
                'attempts',
                'window_started_at',
                'last_seen_at',
            ],
        )


def install():
    """
    Wrap the existing customer portal checker and Security view.

    This is deliberately installed before core.urls is imported, so the URLs
    bind to the wrapped functions without replacing the very large views files.
    """
    from . import views
    from . import views_studio

    if getattr(views_studio, '_voucher_entry_security_installed', False):
        return

    original_portal_check = views_studio.portal_check
    original_security = views.security

    def protected_portal_check(request, slug, *args, **kwargs):
        page = (
            views_studio.PortalPage.objects
            .select_related('business')
            .filter(slug=slug)
            .first()
        )

        if not page:
            return original_portal_check(request, slug, *args, **kwargs)

        data = _body(request)
        row, blocked_response, policy = _before_check(page, request, data)

        if blocked_response is not None:
            return blocked_response

        response = original_portal_check(request, slug, *args, **kwargs)

        if not policy.enabled:
            return response

        if _success_response(response):
            _reset_on_success(row)
            return response

        if _invalid_login_response(response):
            return _record_failure(
                page,
                row,
                policy,
                member=bool(data.get('member')),
            )

        return response

    def security_with_voucher_entry(request, *args, **kwargs):
        response = original_security(request, *args, **kwargs)

        try:
            if getattr(response, 'status_code', 200) != 200:
                return response

            business = request.user.business
            policy = _policy(business)
            now = timezone.now()

            VoucherEntryDevice.objects.filter(
                business=business,
                blocked_until__isnull=False,
                blocked_until__lte=now,
            ).update(
                attempts=0,
                window_started_at=None,
                blocked_at=None,
                blocked_until=None,
            )

            blocked = list(
                VoucherEntryDevice.objects
                .filter(
                    business=business,
                    blocked_until__gt=now,
                )
                .order_by('blocked_until')[:200]
            )

            recent = list(
                VoucherEntryDevice.objects
                .filter(
                    business=business,
                    attempts__gt=0,
                    blocked_until__isnull=True,
                )
                .order_by('-last_attempt_at')[:20]
            )

            card = render_to_string(
                'core/partials/voucher_entry_security.html',
                {
                    'voucher_entry_policy': policy,
                    'voucher_entry_blocked': blocked,
                    'voucher_entry_recent': recent,
                    'voucher_entry_now': now,
                    'current_business': business,
                },
                request=request,
            )

            text = response.content.decode(response.charset or 'utf-8')
            marker = '<section class="panel mb-3" id="fair-usage">'

            if marker in text:
                text = text.replace(marker, card + '\n' + marker, 1)
            else:
                text = text.replace('{% block content %}', '{% block content %}' + card, 1)

            response.content = text.encode(response.charset or 'utf-8')
            if response.has_header('Content-Length'):
                del response['Content-Length']

        except Exception:
            # Security Center must remain available even if this card has a
            # temporary database/schema problem during deployment.
            pass

        return response

    views_studio.portal_check = protected_portal_check
    views.security = security_with_voucher_entry

    views_studio._voucher_entry_security_installed = True
