"""Email verification codes (OTP) and 30-day trusted devices.

* Email addresses must be well-formed, on a domain that accepts mail, and not a
  throwaway-mailbox service. Owning the address is then proved with a code.
* Codes: 6 digits, valid 10 minutes, 5 wrong tries max, only an HMAC is stored,
  60 s between resends, at most 6 codes per address per hour.
* Trusted devices: after a verified code, the browser gets a random token in an
  HttpOnly cookie (only its SHA-256 is stored). For 30 days that browser signs in
  with the password alone; after that, or on any other device, a new code is asked.
"""
import hashlib
import hmac
import logging
import re
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from .models import EmailOTP, TrustedDevice

logger = logging.getLogger('taptap.auth')
CODE_MINUTES = 10
MAX_ATTEMPTS = 5
RESEND_SECONDS = 60
MAX_PER_HOUR = 6
DEVICE_DAYS = int(getattr(settings, 'TRUSTED_DEVICE_DAYS', 30))
DEVICE_COOKIE = 'tt_device'
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
DISPOSABLE = {
    'mailinator.com', 'guerrillamail.com', 'guerrillamail.net', 'sharklasers.com', '10minutemail.com', 'tempmail.com', 'temp-mail.org',
    'yopmail.com', 'trashmail.com', 'getnada.com', 'dispostable.com', 'maildrop.cc', 'mintemail.com', 'throwawaymail.com',
    'fakeinbox.com', 'mohmal.com', 'emailondeck.com', 'tempail.com', 'mailnesia.com', 'spamgourmet.com', 'moakt.com', 'tmpmail.org',
}
TYPOS = {'gmial.com': 'gmail.com', 'gmai.com': 'gmail.com', 'gmail.co': 'gmail.com', 'gamil.com': 'gmail.com', 'yaho.com': 'yahoo.com',
         'yahoo.co': 'yahoo.com', 'hotmial.com': 'hotmail.com', 'hotmail.co': 'hotmail.com', 'outlok.com': 'outlook.com'}


# ─────────────────────────── email validation ───────────────────────────
def domain_accepts_mail(domain):
    """True / False from DNS (MX, then A/AAAA as RFC 5321 allows); None when DNS cannot be reached."""
    try:
        import dns.exception
        import dns.resolver
    except ImportError:
        return None
    resolver = dns.resolver.Resolver()
    resolver.lifetime = resolver.timeout = 4
    try:
        answers = resolver.resolve(domain, 'MX')
        # A "null MX" (RFC 7505: preference 0, exchange ".") means the domain accepts no mail.
        return any(str(r.exchange).strip('.') for r in answers)
    except (dns.resolver.NXDOMAIN,):
        return False
    except (dns.resolver.NoAnswer,):
        for rtype in ('A', 'AAAA'):
            try:
                resolver.resolve(domain, rtype)
                return True
            except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
                continue
            except dns.exception.DNSException:
                return None
        return False
    except dns.exception.DNSException:
        return None  # no DNS reachable from the server: do not block; the code still proves the address


def validate_email_address(email):
    """Return (clean_email, error_message_or_None)."""
    email = (email or '').strip().lower()
    if not email or len(email) > 254 or not EMAIL_RE.match(email):
        return email, 'Enter a valid email address, like name@example.com.'
    local, domain = email.rsplit('@', 1)
    if len(local) > 64 or '..' in email:
        return email, 'Enter a valid email address, like name@example.com.'
    if domain in TYPOS:
        return email, f'Did you mean {local}@{TYPOS[domain]}?'
    if domain in DISPOSABLE:
        return email, 'Temporary / disposable email addresses cannot be used. Please use your real email.'
    if getattr(settings, 'EMAIL_CHECK_DOMAIN', True):
        ok = domain_accepts_mail(domain)
        if ok is False:
            return email, f'The domain "{domain}" cannot receive email. Check the address for typos.'
    return email, None


# ─────────────────────────── codes ───────────────────────────
def _hash(email, purpose, code):
    return hmac.new(settings.SECRET_KEY.encode(), f'otp:{email.lower()}:{purpose}:{code}'.encode(), hashlib.sha256).hexdigest()


def mask(email):
    local, _, domain = (email or '').partition('@')
    shown = local[:2] if len(local) > 3 else local[:1]
    return f'{shown}{"•" * max(2, len(local) - len(shown))}@{domain}'


def seconds_until_resend(email, purpose):
    last = EmailOTP.objects.filter(email__iexact=email, purpose=purpose).order_by('-created_at').first()
    if not last:
        return 0
    left = RESEND_SECONDS - (timezone.now() - last.created_at).total_seconds()
    return max(0, int(left))


def send_code(email, purpose, user=None, ip='', business_name=''):
    """Create and email a code. Returns (ok, message)."""
    email = email.strip().lower()
    wait = seconds_until_resend(email, purpose)
    if wait:
        return False, f'Please wait {wait} s before asking for another code.'
    if EmailOTP.objects.filter(email__iexact=email, created_at__gte=timezone.now() - timedelta(hours=1)).count() >= MAX_PER_HOUR:
        return False, 'Too many codes were requested for this address. Try again in an hour.'
    code = f'{secrets.randbelow(10 ** 6):06d}'
    EmailOTP.objects.filter(email__iexact=email, purpose=purpose, consumed_at__isnull=True).update(consumed_at=timezone.now())  # older codes stop working
    EmailOTP.objects.create(user=user, email=email, purpose=purpose, code_hash=_hash(email, purpose, code), ip_address=ip[:64],
                            expires_at=timezone.now() + timedelta(minutes=CODE_MINUTES))
    what = {'register': 'finish creating your TapTap account', 'login': 'sign in on this device', 'email_change': 'confirm your new business email'}[purpose]
    ctx = {'code': code, 'what': what, 'minutes': CODE_MINUTES, 'business_name': business_name, 'ip': ip}
    try:
        msg = EmailMultiAlternatives(subject=f'{code} is your TapTap verification code', body=render_to_string('core/email/otp.txt', ctx),
                                     from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None), to=[email])
        msg.attach_alternative(render_to_string('core/email/otp.html', ctx), 'text/html')
        msg.send()
    except Exception as exc:
        logger.warning('OTP email to %s failed: %s', email, exc)
        EmailOTP.objects.filter(email__iexact=email, purpose=purpose, consumed_at__isnull=True).update(consumed_at=timezone.now())
        return False, 'We could not send the email right now. Please try again in a minute.'
    if settings.DEBUG and 'console' in getattr(settings, 'EMAIL_BACKEND', ''):
        logger.warning('DEBUG: TapTap code for %s is %s', email, code)
    return True, f'We sent a 6-digit code to {mask(email)}.'


def check_code(email, purpose, code):
    """Return (ok, message). Consumes the code on success; counts wrong tries."""
    code = re.sub(r'\D', '', str(code or ''))
    otp = EmailOTP.objects.filter(email__iexact=email, purpose=purpose, consumed_at__isnull=True).order_by('-created_at').first()
    if not otp:
        return False, 'That code is no longer valid. Ask for a new one.'
    if otp.expires_at < timezone.now():
        return False, 'That code has expired. Ask for a new one.'
    if otp.attempts >= MAX_ATTEMPTS:
        return False, 'Too many wrong tries. Ask for a new code.'
    if len(code) != 6 or not hmac.compare_digest(otp.code_hash, _hash(email, purpose, code)):
        otp.attempts += 1
        otp.save(update_fields=['attempts'])
        left = MAX_ATTEMPTS - otp.attempts
        return False, (f'That code is not right. {left} tr{"y" if left == 1 else "ies"} left.' if left else 'Too many wrong tries. Ask for a new code.')
    otp.consumed_at = timezone.now()
    otp.save(update_fields=['consumed_at'])
    return True, ''


# ─────────────────────────── trusted devices ───────────────────────────
def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def device_label(ua):
    ua = ua or ''
    browser = next((n for k, n in (('Edg/', 'Edge'), ('OPR/', 'Opera'), ('Chrome/', 'Chrome'), ('Firefox/', 'Firefox'), ('Safari/', 'Safari')) if k in ua), 'Browser')
    system = next((n for k, n in (('Android', 'Android'), ('iPhone', 'iPhone'), ('iPad', 'iPad'), ('Windows', 'Windows'), ('Mac OS', 'Mac'), ('Linux', 'Linux')) if k in ua), 'device')
    return f'{browser} on {system}'


def trusted_device(request, user):
    """The active TrustedDevice for this browser and user, or None."""
    token = request.COOKIES.get(DEVICE_COOKIE, '')
    if not token or len(token) > 100:
        return None
    dev = TrustedDevice.objects.filter(user=user, token_hash=_token_hash(token), revoked_at__isnull=True, expires_at__gt=timezone.now()).first()
    if dev:
        TrustedDevice.objects.filter(pk=dev.pk).update(last_used_at=timezone.now(), ip_address=client_ip(request)[:64])
    return dev


def trust_device(request, response, user):
    """Remember this browser for DEVICE_DAYS days (sets the cookie on the response)."""
    token = secrets.token_urlsafe(32)
    ua = request.META.get('HTTP_USER_AGENT', '')[:300]
    dev = TrustedDevice.objects.create(user=user, token_hash=_token_hash(token), label=device_label(ua), user_agent=ua,
                                       ip_address=client_ip(request)[:64], expires_at=timezone.now() + timedelta(days=DEVICE_DAYS))
    response.set_cookie(DEVICE_COOKIE, token, max_age=DEVICE_DAYS * 86400, httponly=True, samesite='Lax',
                        secure=getattr(settings, 'SESSION_COOKIE_SECURE', False))
    return dev


def client_ip(request):
    return (request.META.get('HTTP_X_FORWARDED_FOR', '') or request.META.get('REMOTE_ADDR', '')).split(',')[0].strip()


def notify_new_device(user, dev):
    """Security email: a new device was trusted."""
    try:
        ctx = {'user': user, 'dev': dev, 'days': DEVICE_DAYS, 'site': (getattr(settings, 'SITE_URL', '') or '').rstrip('/')}
        msg = EmailMultiAlternatives(subject='New sign-in to your TapTap account', body=render_to_string('core/email/new_device.txt', ctx),
                                     from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None), to=[user.email])
        msg.send()
    except Exception as exc:
        logger.info('new-device email failed: %s', exc)
