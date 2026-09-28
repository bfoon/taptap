"""Sign-up, sign-in and email changes with emailed codes and 30-day trusted devices."""
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .auth_security import (DEVICE_COOKIE, DEVICE_DAYS, check_code, client_ip, mask, notify_new_device, seconds_until_resend,
                            send_code, trust_device, trusted_device, validate_email_address)
from .forms import RegisterForm
from .models import Business, TrustedDevice, VoucherPlan
from .utils import log

PENDING = 'auth_pending'
PENDING_MINUTES = 30


def _otp_on():
    return getattr(settings, 'AUTH_EMAIL_OTP', True)


def _set_pending(request, **data):
    request.session[PENDING] = {**data, 'started': timezone.now().isoformat()}


def _pending(request):
    p = request.session.get(PENDING)
    if not p:
        return None
    try:
        if timezone.now() - timezone.datetime.fromisoformat(p['started']) > timedelta(minutes=PENDING_MINUTES):
            request.session.pop(PENDING, None)
            return None
    except (KeyError, ValueError):
        return None
    return p


def _safe_next(request, default='dashboard'):
    nxt = request.POST.get('next') or request.GET.get('next') or ''
    return nxt if nxt.startswith('/') and not nxt.startswith('//') else default


# ─────────────────────────── sign-up ───────────────────────────
def register(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    form = RegisterForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        email, err = validate_email_address(form.cleaned_data['email'])
        if err:
            form.add_error('email', err)
        elif User.objects.filter(username=email).exists():
            form.add_error('email', 'An account with this email already exists. Sign in instead.')
        else:
            data = {k: form.cleaned_data[k] for k in ('business_name', 'owner_name', 'phone')}
            data['password'] = make_password(form.cleaned_data['password'])  # never keep the plain password
            if not _otp_on():
                return _create_account(request, email, data)
            ok, msg = send_code(email, 'register', ip=client_ip(request), business_name=data['business_name'])
            if ok:
                _set_pending(request, purpose='register', email=email, data=data)
                messages.success(request, msg)
                return redirect('verify_code')
            form.add_error(None, msg)
    return render(request, 'core/register.html', {'form': form})


def _create_account(request, email, data, remember=True):
    from .durations import best_unit
    from .views import DEFAULT_PLANS
    with transaction.atomic():
        user = User(username=email, email=email, first_name=data['owner_name'])
        user.password = data['password']
        user.save()
        business = Business.objects.create(user=user, business_name=data['business_name'], owner_name=data['owner_name'], phone=data['phone'],
                                           trial_ends_at=timezone.now() + timedelta(days=settings.TRIAL_DAYS),
                                           email_verified_at=timezone.now() if _otp_on() else None)
        for n, p, h, d in DEFAULT_PLANS:
            VoucherPlan.objects.create(business=business, name=n, price=p, duration_minutes=h * 60, duration_unit=best_unit(h * 60), max_devices=d)
    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    request.session.pop(PENDING, None)
    messages.success(request, f'Welcome to TapTap. Your email is verified and your {settings.TRIAL_DAYS}-day trial is active.')
    response = redirect('dashboard')
    if remember and _otp_on():
        trust_device(request, response, user)
    return response


# ─────────────────────────── sign-in ───────────────────────────
def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()
        user = authenticate(request, username=email, password=request.POST.get('password', ''))
        if not user:
            messages.error(request, 'Invalid email or password.')
            return render(request, 'core/login.html', {'email': email})
        if not _otp_on() or trusted_device(request, user):
            login(request, user)
            return redirect(_safe_next(request))
        ok, msg = send_code(user.email or user.username, 'login', user=user, ip=client_ip(request),
                            business_name=getattr(getattr(user, 'business', None), 'business_name', ''))
        if not ok and 'wait' not in msg:
            messages.error(request, msg)
            return render(request, 'core/login.html', {'email': email})
        _set_pending(request, purpose='login', email=user.email or user.username, user_id=user.pk, next=_safe_next(request))
        messages.info(request, msg if ok else 'A code was sent a moment ago — check your inbox.')
        return redirect('verify_code')
    return render(request, 'core/login.html')


# ─────────────────────────── verification ───────────────────────────
def verify_code(request):
    p = _pending(request)
    if not p:
        messages.info(request, 'Your verification session ended. Please start again.')
        return redirect('dashboard' if request.user.is_authenticated else 'login')
    purpose, email = p['purpose'], p['email']
    if request.method == 'POST':
        remember = request.POST.get('remember') == 'on'
        ok, msg = check_code(email, purpose, request.POST.get('code'))
        if not ok:
            messages.error(request, msg)
        elif purpose == 'register':
            if User.objects.filter(username=email).exists():
                request.session.pop(PENDING, None)
                messages.error(request, 'This email was registered in the meantime. Please sign in.')
                return redirect('login')
            return _create_account(request, email, p['data'], remember)
        elif purpose == 'login':
            user = get_object_or_404(User, pk=p['user_id'], is_active=True)
            login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            request.session.pop(PENDING, None)
            biz = getattr(user, 'business', None)
            if biz and not biz.email_verified_at:
                Business.objects.filter(pk=biz.pk).update(email_verified_at=timezone.now())
            response = redirect(p.get('next') or 'dashboard')
            if remember:
                dev = trust_device(request, response, user)
                notify_new_device(user, dev)
                messages.success(request, f'Signed in. This device is trusted for {DEVICE_DAYS} days.')
            return response
        elif purpose == 'email_change':
            if not request.user.is_authenticated or request.user.pk != p.get('user_id'):
                request.session.pop(PENDING, None)
                return redirect('login')
            biz = request.user.business
            biz.email = email
            biz.save(update_fields=['email'])
            request.session.pop(PENDING, None)
            log(biz, 'Business Email', f'Business email changed to {email} (verified)')
            messages.success(request, f'Business email changed to {email}.')
            return redirect(p.get('next') or 'settings')
    labels = {'register': ('Verify your email', 'Enter the code we emailed to finish creating your account.'),
              'login': ('Check your email', 'This device is new or has not been used for a while. Enter the code we emailed you.'),
              'email_change': ('Confirm your new email', 'Enter the code we sent to your new business email.')}
    title, intro = labels[purpose]
    return render(request, 'core/verify_code.html', {'title': title, 'intro': intro, 'masked': mask(email), 'purpose': purpose,
                                                     'wait': seconds_until_resend(email, purpose), 'days': DEVICE_DAYS})


@require_POST
def resend_code(request):
    p = _pending(request)
    if not p:
        return redirect('login')
    user = User.objects.filter(pk=p.get('user_id')).first() if p.get('user_id') else None
    ok, msg = send_code(p['email'], p['purpose'], user=user, ip=client_ip(request),
                        business_name=(p.get('data') or {}).get('business_name', ''))
    (messages.success if ok else messages.error)(request, msg)
    return redirect('verify_code')


def cancel_verification(request):
    request.session.pop(PENDING, None)
    return redirect('dashboard' if request.user.is_authenticated else 'login')


# ─────────────────────────── changing the business email ───────────────────────────
def start_email_change(request, new_email, next_url='/settings/'):
    """Validate, email a code to the NEW address and send the user to the code page.
    Returns a redirect response, or None with an error message already added."""
    email, err = validate_email_address(new_email)
    if err:
        messages.error(request, err)
        return None
    biz = request.user.business
    if email == (biz.email or '').lower():
        return None
    if not _otp_on():
        biz.email = email; biz.save(update_fields=['email'])
        return None
    ok, msg = send_code(email, 'email_change', user=request.user, ip=client_ip(request), business_name=biz.business_name)
    if not ok:
        messages.error(request, msg)
        return None
    _set_pending(request, purpose='email_change', email=email, user_id=request.user.pk, next=next_url)
    messages.info(request, msg + ' Your business email changes once you enter it.')
    return redirect('verify_code')


# ─────────────────────────── trusted devices ───────────────────────────
@login_required
def trusted_devices(request):
    current = trusted_device(request, request.user)
    devices = TrustedDevice.objects.filter(user=request.user, revoked_at__isnull=True, expires_at__gt=timezone.now())
    return render(request, 'core/trusted_devices.html', {'devices': devices, 'current_id': current.pk if current else None, 'days': DEVICE_DAYS})


@login_required
@require_POST
def trusted_device_remove(request, pk=None):
    qs = TrustedDevice.objects.filter(user=request.user, revoked_at__isnull=True)
    if pk:
        qs = qs.filter(pk=pk)
    n = qs.update(revoked_at=timezone.now())
    messages.success(request, f'{n} device{"s" if n != 1 else ""} removed. They will need a new emailed code to sign in.')
    response = redirect('trusted_devices')
    current = request.COOKIES.get(DEVICE_COOKIE)
    if current and not TrustedDevice.objects.filter(user=request.user, revoked_at__isnull=True).exists():
        response.delete_cookie(DEVICE_COOKIE)
    return response
