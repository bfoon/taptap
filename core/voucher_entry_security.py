from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import timedelta

from django.db.models import Q
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.utils import timezone

from .models_voucher_entry import VoucherEntryDevice, VoucherEntryPolicy


MAC_RE = re.compile(r'^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$')


def _norm_mac(value):
    raw = ''.join(
        c
        for c in str(value or '').upper()
        if c in '0123456789ABCDEF'
    )

    if len(raw) != 12:
        return ''

    mac = ':'.join(
        raw[i:i + 2]
        for i in range(0, 12, 2)
    )

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
        try:
            return request.POST.dict()
        except Exception:
            return {}


def _identity(request, data):
    """
    Prefer the browser/device fingerprint.

    Phones can rotate their private Wi-Fi MAC. The portal fingerprint is more
    stable for the same phone/browser, while the current MAC remains stored for
    staff visibility.
    """
    mac = _norm_mac(data.get('mac', ''))
    fp = str(data.get('fp') or '').strip()
    ip = str(
        data.get('ip')
        or _client_ip(request)
    ).strip()[:64]

    if fp:
        digest = hashlib.sha256(
            fp.encode(
                'utf-8',
                'ignore',
            ),
        ).hexdigest()

        return (
            f'fp:{digest}',
            mac,
            digest,
            ip,
        )

    if mac:
        return (
            f'mac:{mac}',
            mac,
            '',
            ip,
        )

    # Only a hotspot (private) address the router reported identifies one device. The address the
    # request came from is the router's public IP — shared by EVERY customer — and counting on it
    # blocked everybody for one person's typos.
    hotspot_ip = str(data.get('ip') or '').strip()
    if hotspot_ip and _private_ip(hotspot_ip):
        digest = hashlib.sha256(hotspot_ip.encode('utf-8', 'ignore')).hexdigest()
        return (f'ip:{digest}', '', '', hotspot_ip)
    return ('', '', '', ip)


def _private_ip(value):
    import ipaddress
    try:
        a = ipaddress.ip_address(str(value).strip())
        return a.is_private or str(a).startswith('100.')
    except ValueError:
        return False


def _related_blocked(business, row):
    """A block on the same phone under another key (same device ID or same MAC) counts too —
    clearing the browser or switching to another browser must not get around it."""
    if row is None:
        return None
    q = Q()
    if row.fingerprint_hash:
        q |= Q(fingerprint_hash=row.fingerprint_hash)
    if row.mac_address:
        q |= Q(mac_address=row.mac_address)
    if not q:
        return None
    return (VoucherEntryDevice.objects.filter(business=business, blocked_until__gt=timezone.now())
            .filter(q).exclude(pk=row.pk).order_by('-blocked_until').first())


def _note(business, mode, row, code, result, left=None):
    """Keep the last 50 portal checks per business (shown on the Security page) — to see exactly what customers got."""
    from django.core.cache import cache
    key = f'tt:portal:log:{business.pk}'
    log = cache.get(key) or []
    c = str(code or '').upper()
    log.insert(0, {'at': timezone.now().isoformat(), 'mode': mode, 'mac': getattr(row, 'mac_address', '') or '',
                   'device': (getattr(row, 'device_key', '') or '')[:12], 'code': (c[:2] + '•' * max(0, len(c) - 4) + c[-2:]) if len(c) > 4 else c,
                   'result': result, 'left': left})
    cache.set(key, log[:50], 86400 * 7)


def portal_log(business):
    from django.core.cache import cache
    from django.utils.dateparse import parse_datetime
    out = []
    for e in cache.get(f'tt:portal:log:{business.pk}') or []:
        e = dict(e); e['at'] = parse_datetime(e['at']); out.append(e)
    return out


def _portal_seen(business):
    """When a router-served login page last reached TapTap (None = never seen)."""
    from django.core.cache import cache
    from django.utils.dateparse import parse_datetime
    v = cache.get(f'tt:portal:state:{business.pk}')
    return parse_datetime(v) if v else None


def _policy(business):
    obj, _created = (
        VoucherEntryPolicy.objects
        .get_or_create(
            business=business,
            defaults={
                'enabled': False,
                'max_attempts': 5,
                'warning_remaining': 2,
                'window_minutes': 10,
                'block_minutes': 30,
            },
        )
    )

    return obj


def _support_number(business):
    return (
        business.support_phone
        or business.phone
        or ''
    ).strip()


def _duration_text(seconds):
    seconds = max(
        0,
        int(
            math.ceil(
                float(seconds or 0)
            ),
        ),
    )

    minutes, seconds = divmod(
        seconds,
        60,
    )

    hours, minutes = divmod(
        minutes,
        60,
    )

    parts = []

    if hours:
        parts.append(
            f'{hours} hour'
            f'{"s" if hours != 1 else ""}',
        )

    if minutes:
        parts.append(
            f'{minutes} minute'
            f'{"s" if minutes != 1 else ""}',
        )

    if seconds or not parts:
        parts.append(
            f'{seconds} second'
            f'{"s" if seconds != 1 else ""}',
        )

    return ' '.join(parts[:2])


def _remaining_seconds(row, now=None):
    now = now or timezone.now()

    if not row.blocked_until:
        return 0

    return max(
        0,
        int(
            math.ceil(
                (
                    row.blocked_until
                    - now
                ).total_seconds(),
            ),
        ),
    )


def _blocked_message(
    policy,
    business,
    row,
    now=None,
):
    now = now or timezone.now()
    seconds = _remaining_seconds(
        row,
        now,
    )

    support = _support_number(
        business,
    )

    until = (
        timezone.localtime(
            row.blocked_until,
        )
        if row.blocked_until
        else None
    )

    until_text = (
        until.strftime('%H:%M:%S')
        if until
        else ''
    )

    custom = (
        policy.blocked_text
        or ''
    ).strip()

    if custom:
        base = custom
    else:
        base = (
            'This device has been temporarily blocked '
            'from entering voucher codes because too '
            'many incorrect attempts were made.'
        )

    if seconds:
        base += (
            f' Try again in '
            f'{_duration_text(seconds)}'
        )

        if until_text:
            base += (
                f' (at {until_text}).'
            )
        else:
            base += '.'

    if support:
        base += (
            f' If you need access sooner, '
            f'call {support} and ask us to '
            f'unblock your device.'
        )
    else:
        base += (
            ' If you need access sooner, '
            'contact staff and ask them to '
            'unblock your device.'
        )

    return base


def _warning_message(
    policy,
    remaining,
):
    custom = (
        policy.warning_text
        or ''
    ).strip()

    attempts = (
        f'{remaining} attempt'
        f'{"s" if remaining != 1 else ""} '
        'remaining.'
    )

    if custom:
        return (
            f'{custom} {attempts}'
        )

    return (
        'Warning: the voucher code or login '
        'details entered are incorrect. '
        'Please check carefully before trying '
        f'again. {attempts}'
    )


def _get_row(
    business,
    request,
    data,
):
    (
        device_key,
        mac,
        fp_hash,
        ip,
    ) = _identity(
        request,
        data,
    )

    if not device_key:
        return None                    # cannot tell this device apart: do not count (never block everyone)

    row, _created = (
        VoucherEntryDevice.objects
        .get_or_create(
            business=business,
            device_key=device_key,
            defaults={
                'mac_address': mac,
                'fingerprint_hash': fp_hash,
                'ip_address': ip,
            },
        )
    )

    changed = []

    if (
        mac
        and row.mac_address != mac
    ):
        row.mac_address = mac
        changed.append(
            'mac_address',
        )

    if (
        fp_hash
        and row.fingerprint_hash != fp_hash
    ):
        row.fingerprint_hash = fp_hash
        changed.append(
            'fingerprint_hash',
        )

    if (
        ip
        and row.ip_address != ip
    ):
        row.ip_address = ip
        changed.append(
            'ip_address',
        )

    if changed:
        row.save(
            update_fields=(
                changed
                + ['last_seen_at']
            ),
        )

    return row


def _clear_expired_block(
    row,
    now,
):
    if (
        row.blocked_until
        and row.blocked_until <= now
    ):
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


def _reset_if_window_expired(
    row,
    policy,
    now,
):
    if row.is_blocked:
        return

    if not row.window_started_at:
        return

    minutes = max(
        1,
        int(
            policy.window_minutes
            or 10
        ),
    )

    if (
        now
        - row.window_started_at
        > timedelta(
            minutes=minutes,
        )
    ):
        row.attempts = 0
        row.window_started_at = None

        row.save(
            update_fields=[
                'attempts',
                'window_started_at',
                'last_seen_at',
            ],
        )


def _prepare(
    business,
    request,
    data,
):
    policy = _policy(
        business,
    )

    if not policy.enabled:
        return (
            None,
            policy,
        )

    now = timezone.now()

    row = _get_row(
        business,
        request,
        data,
    )
    if row is None:
        return None, policy

    _clear_expired_block(
        row,
        now,
    )

    _reset_if_window_expired(
        row,
        policy,
        now,
    )

    row.refresh_from_db()
    other = _related_blocked(business, row)
    if other is not None and not row.is_blocked:
        # same phone, blocked under another key: carry the block over
        row.blocked_at, row.blocked_until, row.attempts = other.blocked_at, other.blocked_until, other.attempts
        row.save(update_fields=['blocked_at', 'blocked_until', 'attempts', 'last_seen_at'])

    return (
        row,
        policy,
    )


def _blocked_payload(
    policy,
    business,
    row,
    *,
    router_mode=False,
):
    """
    IMPORTANT:

    Do NOT return blocked=true here.

    portal-render.js already uses blocked=true for TapTap's voucher-freeze
    warning/paused screen. Feeding this security lockout into that path caused
    the wrong/error page reported by customers.

    For the MikroTik router-served portal we use its existing `wait` countdown.
    For hosted portal pages we return a normal success=false message.
    """
    now = timezone.now()

    seconds = _remaining_seconds(
        row,
        now,
    )

    payload = {
        'ok': False,
        'success': False,
        'security_block': True,
        'security_warning': False,
        'attempts_remaining': 0,
        'blocked_until': (
            row.blocked_until.isoformat()
            if row.blocked_until
            else ''
        ),
        'block_remaining_seconds': seconds,
        'support_phone': _support_number(
            business,
        ),
        'message': _blocked_message(
            policy,
            business,
            row,
            now,
        ),
    }

    # The portal shows this as a red box with a live countdown and keeps the code box locked until
    # it ends (security_block). It must not use `wait`: that one logs in when the countdown ends.
    return payload


def _warning_payload(
    policy,
    remaining,
    *,
    member=False,
    router_mode=False,
):
    prefix = (
        'Username or password is wrong. '
        'Check them and try again.'
        if member
        else (
            'That code was not recognised. '
            'Check the letters and try again.'
        )
    )

    message = (
        f'{prefix} '
        f'{_warning_message(policy, remaining)}'
    )

    payload = {
        'ok': False,
        'success': False,
        'invalid': True,
        'security_warning': True,
        'security_block': False,
        'attempts_remaining': remaining,
        'message': message,
    }

    return payload


def _record_failure(
    business,
    row,
    policy,
    *,
    member=False,
    router_mode=False,
):
    now = timezone.now()

    if not row.window_started_at:
        row.window_started_at = now
        row.attempts = 0

    row.attempts = min(
        65535,
        int(
            row.attempts
            or 0
        )
        + 1,
    )

    row.last_attempt_at = now

    max_attempts = (
        policy.safe_max_attempts
    )

    remaining = max(
        0,
        max_attempts
        - row.attempts,
    )

    if (
        row.attempts
        >= max_attempts
    ):
        block_minutes = max(
            1,
            int(
                policy.block_minutes
                or 30
            ),
        )

        row.blocked_at = now
        row.blocked_until = (
            now
            + timedelta(
                minutes=block_minutes,
            )
        )

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

        return _blocked_payload(
            policy,
            business,
            row,
            router_mode=router_mode,
        )

    row.save(
        update_fields=[
            'attempts',
            'window_started_at',
            'last_attempt_at',
            'last_seen_at',
        ],
    )

    warn = (
        remaining
        <= policy.safe_warning_remaining
    )

    if warn:
        return _warning_payload(
            policy,
            remaining,
            member=member,
            router_mode=router_mode,
        )

    return {
        'ok': False,
        'success': False,
        'invalid': True,
        'security_warning': False,
        'security_block': False,
        'attempts_used': row.attempts,
        'attempts_remaining': remaining,
        'message': (
            'Username or password is wrong. '
            'Check them and try again.'
            if member
            else (
                'That code was not recognised. '
                'Check the letters and try again.'
            )
        ),
    }


def _reset_on_valid_voucher(
    row,
):
    if not row:
        return

    if (
        row.attempts
        or row.window_started_at
    ):
        row.attempts = 0
        row.window_started_at = None

        row.save(
            update_fields=[
                'attempts',
                'window_started_at',
                'last_seen_at',
            ],
        )


def _response_json(
    response,
):
    try:
        return json.loads(
            response.content.decode(
                'utf-8',
            ),
        )
    except Exception:
        return {}


def _invalid_hosted_login(
    response,
):
    data = _response_json(
        response,
    )

    if data.get('success') is not False:
        return False

    message = str(
        data.get('message')
        or '',
    ).lower()

    return (
        'not recognised'
        in message
        or 'not recognized'
        in message
        or (
            'username or password'
            in message
            and 'wrong'
            in message
        )
    )


def _successful_hosted_login(
    response,
):
    data = _response_json(
        response,
    )

    return (
        getattr(
            response,
            'status_code',
            500,
        )
        < 400
        and data.get('success')
        is True
    )


def _public_json(
    payload,
):
    """
    Always return HTTP 200 for security warning/lockout messages.

    The captive portal is an application UI. 403/404/429 responses can be
    replaced/intercepted by hotspot/web-proxy infrastructure and become an
    HTML error page. The payload itself carries success=false/security_block.
    """
    return JsonResponse(
        payload,
        status=200,
    )


def install():
    """
    Protect BOTH portal paths:

    1. views_studio.portal_check
       Used by hosted TapTap portal pages.

    2. views_ads.portal_state
       Used by login.html served directly by MikroTik.

    The previous version only wrapped portal_check, which is why customers on
    the actual MikroTik captive portal did not see the warning/countdown.
    """
    from . import views
    from . import views_ads
    from . import views_studio

    if getattr(
        views_studio,
        '_voucher_entry_security_v2_installed',
        False,
    ):
        return

    original_portal_check = (
        views_studio.portal_check
    )

    original_portal_state = (
        views_ads.portal_state
    )

    original_security = (
        views.security
    )

    def protected_portal_check(
        request,
        slug,
        *args,
        **kwargs,
    ):
        page = (
            views_studio.PortalPage.objects
            .select_related(
                'business',
            )
            .filter(
                slug=slug,
            )
            .first()
        )

        if not page:
            return original_portal_check(
                request,
                slug,
                *args,
                **kwargs,
            )

        data = _body(
            request,
        )

        row, policy = _prepare(
            page.business,
            request,
            data,
        )

        if (
            policy.enabled
            and row
            and row.is_blocked
        ):
            return _public_json(
                _blocked_payload(
                    policy,
                    page.business,
                    row,
                    router_mode=False,
                ),
            )

        response = (
            original_portal_check(
                request,
                slug,
                *args,
                **kwargs,
            )
        )

        if (
            not policy.enabled
            or not row
        ):
            return response

        if _successful_hosted_login(
            response,
        ):
            _reset_on_valid_voucher(
                row,
            )
            return response

        if _invalid_hosted_login(
            response,
        ):
            payload = _record_failure(
                page.business,
                row,
                policy,
                member=bool(
                    data.get(
                        'member',
                    ),
                ),
                router_mode=False,
            )

            _note(page.business, 'hosted', row, data.get('code'), 'locked' if payload.get('security_block') else ('warning' if payload.get('security_warning') else 'wrong code'),
                  payload.get('attempts_remaining'))
            return _public_json(
                payload,
            )

        # Expired, disabled, frozen, sticky-device refusals, etc. are returned
        # exactly as TapTap already handles them and do not count as guessing.
        return response

    def protected_portal_state(
        request,
        slug,
        *args,
        **kwargs,
    ):
        """
        MikroTik router-served login page.

        This endpoint runs BEFORE RouterOS receives the username/password, so it
        is the only place where TapTap can show the configured countdown on the
        actual customer portal.
        """
        page = (
            views_ads.PortalPage.objects
            .select_related(
                'business',
            )
            .filter(
                slug=slug,
            )
            .first()
        )

        if not page:
            return original_portal_state(
                request,
                slug,
                *args,
                **kwargs,
            )

        data = (
            views_ads._portal_json(
                request,
            )
            or {}
        )
        from django.core.cache import cache as _cache
        _cache.set(f'tt:portal:state:{page.business_id}', timezone.now().isoformat(), 86400 * 30)   # the router page reached TapTap

        row, policy = _prepare(
            page.business,
            request,
            data,
        )

        if (
            policy.enabled
            and row
            and row.is_blocked
        ):
            _note(page.business, 'router', row, data.get('code'), 'still locked')
            return views_ads._cors(
                _public_json(
                    _blocked_payload(
                        policy,
                        page.business,
                        row,
                        router_mode=True,
                    ),
                ),
            )

        if not policy.enabled:
            return original_portal_state(
                request,
                slug,
                *args,
                **kwargs,
            )

        code = str(
            data.get(
                'code',
            )
            or '',
        ).replace(
            ' ',
            '',
        ).strip()

        # Empty submissions are handled by portal-render.js and do not count.
        if not code:
            return original_portal_state(
                request,
                slug,
                *args,
                **kwargs,
            )

        voucher = (
            page.business.vouchers
            .filter(
                code__iexact=code,
            )
            .first()
        )
        if not voucher:
            from .models import RouterHotspotUser
            known_on_router = RouterHotspotUser.objects.filter(business=page.business, username__iexact=code, is_present=True).exists()
            if known_on_router or row is None:
                # a user the router has (made in WinBox/Mikhmon, not in TapTap yet), or a device we cannot
                # tell apart: let the router decide, do not count it as guessing
                if row is not None and known_on_router:
                    _reset_on_valid_voucher(row)
                return original_portal_state(request, slug, *args, **kwargs)
            # Unknown to TapTap and to the router: it is wrong. Count it and answer on the page itself
            # ("not recognised — N tries left") instead of sending it to the router's own error page.
            payload = _record_failure(page.business, row, policy, member=False, router_mode=True)
            _note(page.business, 'router', row, code, 'locked' if payload.get('security_block') else ('warning' if payload.get('security_warning') else 'wrong code'),
                  payload.get('attempts_remaining'))
            return views_ads._cors(_public_json(payload))

        # A known voucher code is NOT guessing. Reset the wrong-code counter
        # before normal expired/disabled/frozen/sticky checks run.
        #
        # Member usernames are deliberately not reset here because this state
        # request does not contain the member password; hosted portal_check can
        # validate both fields.
        if not voucher.is_member:
            _reset_on_valid_voucher(
                row,
            )
        _note(page.business, 'router', row, code, 'known code → router')

        return original_portal_state(
            request,
            slug,
            *args,
            **kwargs,
        )

    def security_with_voucher_entry(
        request,
        *args,
        **kwargs,
    ):
        response = original_security(
            request,
            *args,
            **kwargs,
        )

        try:
            if (
                getattr(
                    response,
                    'status_code',
                    200,
                )
                != 200
            ):
                return response

            business = (
                request.user.business
            )

            policy = _policy(
                business,
            )

            now = timezone.now()

            # Clean only blocks whose configured blocked_until has actually
            # elapsed. This is what makes "Block device for 10 minutes" mean
            # exactly 10 minutes rather than depending on a page refresh/cache.
            expired = (
                VoucherEntryDevice.objects
                .filter(
                    business=business,
                    blocked_until__isnull=False,
                    blocked_until__lte=now,
                )
            )

            expired.update(
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
                .order_by(
                    'blocked_until',
                )[:200]
            )

            recent = list(
                VoucherEntryDevice.objects
                .filter(
                    business=business,
                    attempts__gt=0,
                    blocked_until__isnull=True,
                )
                .order_by(
                    '-last_attempt_at',
                )[:20]
            )

            card = render_to_string(
                'core/partials/'
                'voucher_entry_security.html',
                {
                    'voucher_entry_policy':
                        policy,
                    'voucher_entry_blocked':
                        blocked,
                    'voucher_entry_recent':
                        recent,
                    'voucher_entry_now':
                        now,
                    'voucher_entry_portal_seen':
                        _portal_seen(business),
                    'voucher_entry_log':
                        portal_log(business)[:20],
                    'voucher_entry_router_pages':
                        business.portal_pages.filter(is_published=True).exists() if hasattr(business, 'portal_pages') else False,
                    'current_business':
                        business,
                },
                request=request,
            )

            text = (
                response.content.decode(
                    response.charset
                    or 'utf-8',
                )
            )

            marker = (
                '<section class="panel mb-3" '
                'id="fair-usage">'
            )

            slot = '<div id="entry-protection-slot"></div>'   # Security › Access control tab
            if slot in text:
                text = text.replace(slot, slot + '\n' + card, 1)
            elif marker in text:
                text = text.replace(
                    marker,
                    card
                    + '\n'
                    + marker,
                    1,
                )
            else:
                # Security template structure changed: put the protection card
                # immediately after the main content container if possible.
                for fallback in (
                    '<div class="security-page">',
                    '<main',
                ):
                    pos = text.find(
                        fallback,
                    )

                    if pos >= 0:
                        if fallback == '<main':
                            end = text.find(
                                '>',
                                pos,
                            )

                            if end >= 0:
                                text = (
                                    text[:end + 1]
                                    + card
                                    + text[end + 1:]
                                )
                                break
                        else:
                            end = (
                                pos
                                + len(
                                    fallback,
                                )
                            )

                            text = (
                                text[:end]
                                + card
                                + text[end:]
                            )
                            break

            response.content = (
                text.encode(
                    response.charset
                    or 'utf-8',
                )
            )

            if response.has_header(
                'Content-Length',
            ):
                del response[
                    'Content-Length'
                ]

        except Exception:
            # Never make Security Center unavailable because this panel failed.
            pass

        return response

    # The originals are @csrf_exempt: the customer's login page is on the router (another origin) and has
    # no CSRF token. A plain wrapper lost that, so every check got "403 Forbidden" — the page then logged in
    # blind: no count, no warning, no lockout. Keep the wrappers exempt like the views they replace.
    from django.views.decorators.csrf import csrf_exempt
    protected_portal_check = csrf_exempt(protected_portal_check)
    protected_portal_state = csrf_exempt(protected_portal_state)

    views_studio.portal_check = (
        protected_portal_check
    )

    views_ads.portal_state = (
        protected_portal_state
    )

    views.security = (
        security_with_voucher_entry
    )

    views_studio._voucher_entry_security_v2_installed = True
