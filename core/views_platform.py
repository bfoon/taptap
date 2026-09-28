"""TapTap platform console for the app owner (Django superusers only).

Software usage analytics, subscription counts and revenue, and support tools to
help business owners with their accounts. Every change made here is written to
PlatformAudit.
"""
import csv
import secrets
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db.models import Count, F, Max, Q, Sum
from django.db.models.functions import Coalesce, TruncDate, TruncMonth
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Business, Router, Subscription, TrustedDevice, Voucher, VoucherSale
from .models_team import PlatformAudit, TeamMember, UsageDaily
from .subscriptions import PLAN_DAYS, activate_subscription, extend_business, record_paid_subscription
from .team import VIEW_AS_KEY, _ip

SECTION_LABELS = {'dashboard': 'Overview', 'vouchers': 'Vouchers', 'batches': 'Batches', 'plans': 'Plans', 'routers': 'Routers',
                  'active-users': 'Active users', 'ip-bindings': 'IP binding', 'topology': 'Topology', 'traffic': 'Traffic',
                  'devices': 'Devices', 'alerts': 'Alerts', 'security': 'Security', 'studio': 'Studios', 'ads': 'Adverts',
                  'reports': 'Reports', 'finance': 'Finance', 'sales': 'Daily sales', 'subscription': 'Subscription',
                  'settings': 'Settings', 'support': 'Support', 'team': 'Team', 'notifications': 'Email alerts', 'account': 'Account'}


STATUS_OPTIONS = [('paid', 'Paid & active'), ('trial', 'On trial'), ('expired', 'Expired'), ('unlimited', 'Unlimited'), ('suspended', 'Suspended')]
SORT_OPTIONS = [('-created_at', 'Newest first'), ('created_at', 'Oldest first'), ('business_name', 'Name A–Z'), ('-last_seen', 'Recently active'),
                ('-paid_total', 'Most paid'), ('subscription_expires_at', 'Expiring first')]


def superuser_required(view):
    @login_required
    @wraps(view)
    def inner(request, *args, **kwargs):
        if not request.user.is_superuser:
            return HttpResponseForbidden('Platform staff only.')
        return view(request, *args, **kwargs)
    return inner


def audit(request, business, action, details=''):
    PlatformAudit.objects.create(actor=request.user, business=business, action=action[:80], details=str(details)[:500], ip=_ip(request))


def _paid():
    return Subscription.objects.filter(payment_status='Paid').annotate(paid_on=Coalesce('starts_at', 'created_at'))


def _month_start(d):
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _status_filters(now):
    unlimited = Q(is_unlimited=True)
    paid = Q(is_unlimited=False, subscription_status='active', subscription_expires_at__gt=now)
    trial = Q(is_unlimited=False, trial_ends_at__gt=now) & ~Q(subscription_status='active')
    return {'unlimited': unlimited, 'paid': paid, 'trial': trial, 'expired': ~(unlimited | paid | trial)}


def _dec(v, default='0'):
    try:
        return max(Decimal('0'), Decimal(str(v or default).replace(',', '').strip() or default))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def _int(v, default=0, lo=0, hi=3650):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


# ─────────────────────────── overview ───────────────────────────
@superuser_required
def overview(request):
    now = timezone.now()
    today = timezone.localdate()
    this_month = _month_start(timezone.localtime())
    last_month = _month_start(this_month - timedelta(days=1))
    year_start = this_month.replace(month=1)
    st = _status_filters(now)
    biz = Business.objects.all()
    counts = {k: biz.filter(q).count() for k, q in st.items()}
    counts['total'] = biz.count()
    counts['new_30'] = biz.filter(created_at__gte=now - timedelta(days=30)).count()
    counts['new_month'] = biz.filter(created_at__gte=this_month).count()
    counts['team'] = TeamMember.objects.filter(is_active=True).count()
    counts['paying_ever'] = biz.filter(subscriptions__payment_status='Paid').distinct().count()
    counts['conversion'] = round(counts['paying_ever'] / counts['total'] * 100, 1) if counts['total'] else 0

    paid = _paid()
    rev = {
        'month': paid.filter(paid_on__gte=this_month).aggregate(v=Sum('amount'))['v'] or 0,
        'last_month': paid.filter(paid_on__gte=last_month, paid_on__lt=this_month).aggregate(v=Sum('amount'))['v'] or 0,
        'ytd': paid.filter(paid_on__gte=year_start).aggregate(v=Sum('amount'))['v'] or 0,
        'all': paid.aggregate(v=Sum('amount'))['v'] or 0,
        'paid_count': paid.count(),
    }
    pending = Subscription.objects.filter(payment_status='Pending')
    rev['pending_count'] = pending.count()
    rev['pending_amount'] = pending.aggregate(v=Sum('amount'))['v'] or 0
    rev['change'] = round((float(rev['month']) - float(rev['last_month'])) / float(rev['last_month']) * 100, 1) if rev['last_month'] else None
    rev['arpa'] = (rev['all'] / counts['paying_ever']) if counts['paying_ever'] else 0

    # 12-month revenue + subscription counts
    start12 = _month_start((this_month - timedelta(days=330)))
    monthly = {r['m'].date() if hasattr(r['m'], 'date') else r['m']: r for r in paid.filter(paid_on__gte=start12)
               .annotate(m=TruncMonth('paid_on')).values('m').annotate(v=Sum('amount'), n=Count('id'))}
    months, m = [], start12
    for _ in range(12):
        key = m.date()
        r = monthly.get(key, {})
        months.append({'label': m.strftime('%b %y'), 'amount': float(r.get('v') or 0), 'count': r.get('n') or 0})
        m = _month_start(m + timedelta(days=32))
    by_plan = list(paid.values('plan').annotate(n=Count('id'), v=Sum('amount')).order_by('-v'))

    # usage
    d30 = today - timedelta(days=29)
    usage = UsageDaily.objects.filter(date__gte=d30)
    active = {
        'dau': usage.filter(date=today).values('business').distinct().count(),
        'wau': usage.filter(date__gte=today - timedelta(days=6)).values('business').distinct().count(),
        'mau': usage.values('business').distinct().count(),
        'users_30': usage.values('user').distinct().count(),
    }
    active['stickiness'] = round(active['dau'] / active['mau'] * 100) if active['mau'] else 0
    daily = {r['date']: r for r in usage.values('date').annotate(b=Count('business', distinct=True), v=Sum('page_views'), l=Sum('logins'))}
    days = []
    for i in range(30):
        d = d30 + timedelta(days=i)
        r = daily.get(d, {})
        days.append({'label': d.strftime('%d %b'), 'businesses': r.get('b') or 0, 'views': r.get('v') or 0, 'logins': r.get('l') or 0})
    totals = usage.aggregate(v=Sum('page_views'), l=Sum('logins'))
    sections = Counter()
    for s in usage.values_list('sections', flat=True):
        sections.update(s or {})
    features = [{'label': SECTION_LABELS.get(k, k.replace('-', ' ').title()), 'views': v} for k, v in sections.most_common(12)]
    top_biz = list(usage.values('business', 'business__business_name').annotate(v=Sum('page_views'), l=Sum('logins'), d=Count('date', distinct=True))
                   .order_by('-v')[:10])

    seen = set(UsageDaily.objects.filter(date__gte=today - timedelta(days=13)).values_list('business', flat=True).distinct())
    live_q = st['paid'] | st['trial'] | st['unlimited']
    dormant = [b for b in biz.filter(live_q).order_by('-created_at')[:500] if b.pk not in seen][:10]
    expiring = biz.filter(st['paid'], subscription_expires_at__lte=now + timedelta(days=7)).order_by('subscription_expires_at')[:10]
    trials_ending = biz.filter(st['trial'], trial_ends_at__lte=now + timedelta(days=3)).order_by('trial_ends_at')[:10]

    platform = {
        'routers': Router.objects.count(), 'online': Router.objects.filter(status='Online').count(),
        'vouchers_30': Voucher.objects.filter(created_at__gte=now - timedelta(days=30)).count(),
        'sales_30': VoucherSale.objects.filter(sold_at__gte=now - timedelta(days=30)).aggregate(v=Sum('amount'), n=Count('id')),
    }
    signups = {r['d']: r['n'] for r in biz.filter(created_at__date__gte=d30).annotate(d=TruncDate('created_at')).values('d').annotate(n=Count('id'))}
    for i, row in enumerate(days):
        row['signups'] = signups.get(d30 + timedelta(days=i), 0)

    return render(request, 'core/platform/overview.html', {
        'counts': counts, 'rev': rev, 'months': months, 'by_plan': by_plan, 'active': active, 'days': days,
        'usage_totals': totals, 'features': features, 'top_biz': top_biz, 'dormant': dormant, 'expiring': expiring,
        'trials_ending': trials_ending, 'platform': platform, 'pending': pending.select_related('business').order_by('-created_at')[:8],
    })


# ─────────────────────────── businesses ───────────────────────────
@superuser_required
def businesses(request):
    now = timezone.now()
    qs = Business.objects.select_related('user').annotate(
        last_seen=Max('usage_days__last_seen_at'), team_count=Count('team', distinct=True),
        paid_total=Sum('subscriptions__amount', filter=Q(subscriptions__payment_status='Paid')),
    )
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(business_name__icontains=q) | Q(owner_name__icontains=q) | Q(user__email__icontains=q)
                       | Q(phone__icontains=q) | Q(email__icontains=q) | Q(team__user__email__icontains=q)).distinct()
    status = request.GET.get('status', '')
    filters = _status_filters(now)
    if status in filters:
        qs = qs.filter(filters[status])
    elif status == 'suspended':
        qs = qs.filter(user__is_active=False)
    sort = request.GET.get('sort', '-created_at')
    if sort not in dict(SORT_OPTIONS):
        sort = '-created_at'
    if sort.startswith('-') and sort[1:] in ('last_seen', 'paid_total'):
        qs = qs.order_by(F(sort[1:]).desc(nulls_last=True), '-created_at')
    else:
        qs = qs.order_by(sort)
    if request.GET.get('export') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = 'attachment; filename="taptap-businesses.csv"'
        w = csv.writer(resp)
        w.writerow(['Business', 'Owner', 'Email', 'Phone', 'Status', 'Access until', 'Joined', 'Last seen', 'Team', 'Paid total'])
        for b in qs:
            w.writerow([b.business_name, b.owner_name, b.user.email, b.phone, _status_of(b, now),
                        'unlimited' if b.is_unlimited else (b.access_expires_at() or ''), b.created_at, b.last_seen or '', b.team_count, b.paid_total or 0])
        return resp
    page = Paginator(qs, 50).get_page(request.GET.get('page'))
    for b in page:
        b.status_label = _status_of(b, now)
    params = request.GET.copy()
    params.pop('page', None)
    return render(request, 'core/platform/businesses.html', {'page_obj': page, 'q': q, 'status': status, 'sort': sort,
                                                             'params': params.urlencode(), 'status_options': STATUS_OPTIONS,
                                                             'sort_options': SORT_OPTIONS})


def _status_of(b, now):
    if not b.user.is_active:
        return 'suspended'
    if b.is_unlimited:
        return 'unlimited'
    if b.subscription_status == 'active' and b.subscription_expires_at and b.subscription_expires_at > now:
        return 'paid'
    if b.subscription_status != 'active' and b.trial_ends_at > now:
        return 'trial'
    return 'expired'


@superuser_required
def business_detail(request, pk):
    b = get_object_or_404(Business.objects.select_related('user'), pk=pk)
    now = timezone.now()
    today = timezone.localdate()
    d30 = today - timedelta(days=29)
    usage = UsageDaily.objects.filter(business=b, date__gte=d30)
    per_day = {r['date']: r for r in usage.values('date').annotate(v=Sum('page_views'), l=Sum('logins'))}
    days = [{'label': (d30 + timedelta(days=i)).strftime('%d %b'), 'views': (per_day.get(d30 + timedelta(days=i)) or {}).get('v') or 0}
            for i in range(30)]
    peak = max((d['views'] for d in days), default=0) or 1
    for d in days:
        d['pct'] = max(3, round(d['views'] / peak * 100))
    per_user = list(usage.values('user__email', 'user__first_name').annotate(v=Sum('page_views'), l=Sum('logins'), last=Max('last_seen_at')).order_by('-v'))
    sections = Counter()
    for s in usage.values_list('sections', flat=True):
        sections.update(s or {})
    stats = {
        'routers': b.routers.count(), 'online': b.routers.filter(status='Online').count(), 'vouchers': b.vouchers.count(),
        'sales_30': b.sales.filter(sold_at__gte=now - timedelta(days=30)).aggregate(v=Sum('amount'), n=Count('id')),
        'paid_total': b.subscriptions.filter(payment_status='Paid').aggregate(v=Sum('amount'))['v'] or 0,
        'views_30': sum(d['views'] for d in days), 'logins_30': usage.aggregate(l=Sum('logins'))['l'] or 0,
        'last_seen': usage.aggregate(m=Max('last_seen_at'))['m'],
    }
    from .views import SUBSCRIPTION_PACKAGES
    return render(request, 'core/platform/business_detail.html', {
        'b': b, 'status': _status_of(b, now), 'stats': stats, 'days': days, 'per_user': per_user,
        'features': [(SECTION_LABELS.get(k, k), v) for k, v in sections.most_common(8)],
        'subs': b.subscriptions.order_by('-created_at'), 'team': b.team.select_related('user'),
        'activities': b.activities.order_by('-created_at')[:15], 'audit': b.platform_audit.select_related('actor')[:15],
        'packages': SUBSCRIPTION_PACKAGES, 'devices': TrustedDevice.objects.filter(user=b.user, revoked_at__isnull=True, expires_at__gt=now).count(),
    })


@superuser_required
@require_POST
def business_action(request, pk):
    b = get_object_or_404(Business.objects.select_related('user'), pk=pk)
    action = request.POST.get('action')
    note = request.POST.get('note', '').strip()[:200]
    back = redirect('platform_business', pk=b.pk)
    now = timezone.now()

    if action == 'extend_paid':
        days = _int(request.POST.get('days'), 0, 1)
        if not days:
            messages.error(request, 'Enter the number of days.')
            return back
        _, until = extend_business(b, days)
        audit(request, b, 'Extended subscription', f'+{days} days → {until:%d %b %Y}. {note}')
        messages.success(request, f'Paid access extended by {days} days (until {timezone.localtime(until):%d %b %Y}).')
    elif action == 'extend_trial':
        days = _int(request.POST.get('days'), 0, 1)
        b.trial_ends_at = max(now, b.trial_ends_at) + timedelta(days=days)
        b.save(update_fields=['trial_ends_at'])
        audit(request, b, 'Extended trial', f'+{days} days → {b.trial_ends_at:%d %b %Y}. {note}')
        messages.success(request, f'Trial extended by {days} days.')
    elif action == 'record_payment':
        from .views import SUBSCRIPTION_PACKAGES
        code = request.POST.get('package')
        if code in SUBSCRIPTION_PACKAGES:
            plan, amount, days = SUBSCRIPTION_PACKAGES[code]
        else:
            plan, days = request.POST.get('plan', 'Custom').strip() or 'Custom', _int(request.POST.get('days'), 0, 1)
            amount = _dec(request.POST.get('amount'))
        if request.POST.get('amount'):
            amount = _dec(request.POST.get('amount'))
        if not days:
            messages.error(request, 'Choose a package or enter the days for a custom payment.')
            return back
        sub = record_paid_subscription(b, plan, amount, days, method=request.POST.get('method', 'Manual'), reference=request.POST.get('reference', ''))
        audit(request, b, 'Recorded payment', f'{plan} D{amount} ({days} days) ref {sub.transaction_id}. {note}')
        messages.success(request, f'Payment recorded: {plan} for D{amount}. Access until {timezone.localtime(sub.expires_at):%d %b %Y}.')
    elif action in ('confirm_sub', 'reject_sub'):
        return sub_action(request, request.POST.get('sub'))
    elif action == 'toggle_unlimited':
        b.is_unlimited = not b.is_unlimited
        b.save(update_fields=['is_unlimited'])
        audit(request, b, 'Unlimited on' if b.is_unlimited else 'Unlimited off', note)
        messages.success(request, f'Unlimited access turned {"on" if b.is_unlimited else "off"}.')
    elif action in ('suspend', 'reactivate'):
        active = action == 'reactivate'
        ids = [b.user_id] + list(b.team.values_list('user_id', flat=True))
        User.objects.filter(pk__in=ids).exclude(is_superuser=True).update(is_active=active)
        if not active:
            TrustedDevice.objects.filter(user_id__in=ids, revoked_at__isnull=True).update(revoked_at=now)
        audit(request, b, 'Suspended account' if not active else 'Reactivated account', note)
        messages.success(request, f'{b.business_name} {"reactivated" if active else "suspended — owner and team are signed out"}.')
    elif action == 'reset_owner_password':
        pw = secrets.token_urlsafe(9)
        b.user.set_password(pw)
        b.user.save(update_fields=['password'])
        TrustedDevice.objects.filter(user=b.user, revoked_at__isnull=True).update(revoked_at=now)
        audit(request, b, 'Reset owner password', note)
        messages.success(request, f'Temporary password for {b.user.email}: {pw} — give it to the owner privately and ask them to change it '
                                  'under Settings → Change password. It is shown only once.')
    elif action == 'change_owner_email':
        from django.core.exceptions import ValidationError
        from django.core.validators import validate_email
        email = request.POST.get('email', '').strip().lower()
        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, 'Enter a valid email.')
            return back
        if User.objects.filter(Q(username__iexact=email) | Q(email__iexact=email)).exclude(pk=b.user_id).exists():
            messages.error(request, 'That email already belongs to another login.')
            return back
        old = b.user.email
        b.user.username = b.user.email = email
        b.user.save(update_fields=['username', 'email'])
        audit(request, b, 'Changed owner login email', f'{old} → {email}. {note}')
        messages.success(request, f'Owner now signs in with {email}.')
    elif action == 'revoke_devices':
        n = TrustedDevice.objects.filter(user=b.user, revoked_at__isnull=True).update(revoked_at=now)
        audit(request, b, 'Revoked trusted devices', f'{n} device(s). {note}')
        messages.success(request, f'{n} trusted device(s) removed; the owner will get an emailed code next sign-in.')
    elif action == 'note':
        if note:
            audit(request, b, 'Support note', note)
            messages.success(request, 'Note saved.')
    elif action == 'view_as':
        request.session[VIEW_AS_KEY] = b.pk
        audit(request, b, 'Started viewing as owner', note)
        messages.info(request, f'You are viewing TapTap as {b.business_name}. Every change you make is logged.')
        return redirect('dashboard')
    else:
        messages.error(request, 'Unknown action.')
    return back


@login_required
@require_POST
def view_as_stop(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden()
    pk = request.session.pop(VIEW_AS_KEY, None)
    b = Business.objects.filter(pk=pk).first()
    if b:
        audit(request, b, 'Stopped viewing as owner')
        return redirect('platform_business', pk=b.pk)
    return redirect('platform_overview')


# ─────────────────────────── subscriptions ───────────────────────────
@superuser_required
def subscriptions(request):
    qs = Subscription.objects.select_related('business').annotate(paid_on=Coalesce('starts_at', 'created_at')).order_by('-created_at')
    status = request.GET.get('status', '')
    if status:
        qs = qs.filter(payment_status=status)
    plan = request.GET.get('plan', '')
    if plan:
        qs = qs.filter(plan=plan)
    start, end = request.GET.get('start', ''), request.GET.get('end', '')
    try:
        if start:
            qs = qs.filter(created_at__date__gte=datetime.strptime(start, '%Y-%m-%d').date())
        if end:
            qs = qs.filter(created_at__date__lte=datetime.strptime(end, '%Y-%m-%d').date())
    except ValueError:
        pass
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(business__business_name__icontains=q) | Q(transaction_id__icontains=q) | Q(business__user__email__icontains=q))
    totals = qs.aggregate(paid=Sum('amount', filter=Q(payment_status='Paid')), n_paid=Count('id', filter=Q(payment_status='Paid')),
                          pending=Sum('amount', filter=Q(payment_status='Pending')), n_pending=Count('id', filter=Q(payment_status='Pending')))
    if request.GET.get('export') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = 'attachment; filename="taptap-subscriptions.csv"'
        w = csv.writer(resp)
        w.writerow(['Requested', 'Business', 'Plan', 'Amount', 'Method', 'Status', 'Reference', 'Starts', 'Expires'])
        for s in qs:
            w.writerow([s.created_at, s.business.business_name, s.plan, s.amount, s.payment_method, s.payment_status, s.transaction_id,
                        s.starts_at or '', s.expires_at or ''])
        return resp
    params = request.GET.copy()
    params.pop('page', None)
    return render(request, 'core/platform/subscriptions.html', {
        'page_obj': Paginator(qs, 50).get_page(request.GET.get('page')), 'totals': totals, 'status': status, 'plan': plan,
        'plans': Subscription.objects.values_list('plan', flat=True).distinct().order_by('plan'), 'start': start, 'end': end, 'q': q,
        'params': params.urlencode(), 'plan_days': PLAN_DAYS, 'statuses': ['Pending', 'Paid', 'Rejected'],
    })


@superuser_required
@require_POST
def sub_action(request, pk):
    sub = get_object_or_404(Subscription.objects.select_related('business'), pk=pk)
    action = request.POST.get('action')
    nxt = request.POST.get('next', '')
    back = redirect(nxt) if nxt.startswith('/') and not nxt.startswith('//') else redirect('platform_subscriptions')
    if action == 'confirm_sub':
        days = _int(request.POST.get('days'), 0, 1) or None
        if activate_subscription(sub, days=days, method=request.POST.get('method') or None, reference=request.POST.get('reference') or None):
            audit(request, sub.business, 'Confirmed payment', f'{sub.plan} D{sub.amount} → access until {sub.expires_at:%d %b %Y}')
            messages.success(request, f'{sub.business.business_name}: {sub.plan} confirmed. Access until {timezone.localtime(sub.expires_at):%d %b %Y}.')
        else:
            messages.error(request, 'Already paid, or the plan length is unknown — enter the number of days.')
    elif action == 'reject_sub' and sub.payment_status == 'Pending':
        sub.payment_status = 'Rejected'
        sub.save(update_fields=['payment_status'])
        audit(request, sub.business, 'Rejected payment request', f'{sub.plan} D{sub.amount}')
        messages.success(request, 'Payment request rejected.')
    return back


# ─────────────────────────── audit log ───────────────────────────
@superuser_required
def audit_log(request):
    qs = PlatformAudit.objects.select_related('actor', 'business')
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(business__business_name__icontains=q) | Q(action__icontains=q) | Q(details__icontains=q) | Q(actor__email__icontains=q))
    return render(request, 'core/platform/audit.html', {'page_obj': Paginator(qs, 60).get_page(request.GET.get('page')), 'q': q})
