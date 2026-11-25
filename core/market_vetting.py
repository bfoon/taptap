"""Vetting Market sellers — TapTap decides who may list batches.

The way in
----------
1. **Seller applies** (Market › Selling): who they are, where, since when, how customers buy today, how buyers'
   customers get help, a photo of the installation and, if they have one, a registration document. They accept the
   seller rules (keep sold vouchers working until finished, never touch a buyer's vouchers, answer support).
2. **TapTap measures** the network itself — ten automatic checks on real data, nothing typed by the seller
   (``run_checks``): account age, verified email, a router online with history, uptime, steady sales, devices
   connected, complaints, a wallet to be paid on, a plan that includes selling, and their record on the Market.
   Four are required; together they give a score out of 100. The seller sees the same checklist live.
3. **Platform staff decide** (Platform console › Market sellers): approve at a level (Starter, Trusted, Partner — each
   with its own limits on open listings and batch size, which can be set by hand), ask for more information, reject
   (with a date from which they may apply again) — and later suspend or reinstate. Every step is logged and the
   seller is told.

After approval
--------------
* Only approved sellers' listings are shown, carry the "Vetted by TapTap" badge and can be bought.
* A suspended seller's open listings disappear from the Market at once (nothing is deleted). Batches already sold
  stay the buyers' and keep working: a suspension never takes anything from a buyer.
* Once a day TapTap re-runs the checks for every approved seller and puts a seller "on watch" for the platform when
  uptime, complaints or disputes slip — a person decides, nothing is suspended automatically.
* Businesses that were already selling when vetting started were approved as Starter sellers (migration 0135).
"""
from __future__ import annotations

import base64
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.mail import send_mail
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models_market import MarketDeal, MarketListing, MarketSeller, MarketVettingEvent

logger = logging.getLogger('taptap.market')

LIMITS = {'starter': (2, 300), 'trusted': (6, 2000), 'partner': (None, None)}      # open listings, vouchers per batch
REAPPLY_DAYS = 30
MAX_FILE = 3 * 1024 * 1024
FILE_TYPES = ('image/jpeg', 'image/png', 'image/webp', 'application/pdf')
RULES = [
    'Every voucher I sell on the Market keeps working on my network until it is finished.',
    'I never delete, pause or change a voucher a buyer owns; I help its customers like my own.',
    'The figures TapTap shows come from my network; I do not try to inflate them.',
    'TapTap may suspend my listings if buyers are let down.',
]


class VettingError(ValueError):
    pass


# ─────────────────────────────── the automatic checks ───────────────────────────────

def _uptime(router, now, days=30):
    from . import uptime_guard
    from .models_market import RouterOutage
    start = max(now - timedelta(days=days), router.created_at or now)
    span = max(1.0, (now - start).total_seconds())
    down = sum((min(o.ended_at, now) - max(o.started_at, start)).total_seconds()
               for o in RouterOutage.objects.filter(router=router, ended_at__gt=start, started_at__lt=now))
    since = uptime_guard.down_since(router.pk)
    if since:
        down += max(0, (now - max(since, start)).total_seconds())
    return round(100 - down / span * 100, 2)


def run_checks(business, now=None):
    """Ten checks on what TapTap itself recorded. Returns (checks, score, required_ok)."""
    from .models import UsageRecord, Voucher
    from .models_voucher_reports import VoucherReport
    from .market import wallets
    now = now or timezone.now()
    age = (now - business.created_at).days if business.created_at else 0
    routers = list(business.routers.all())
    seasoned = [r for r in routers if r.created_at and r.created_at <= now - timedelta(days=30)]
    online = [r for r in routers if str(r.status).lower() == 'online']
    best_up = max([_uptime(r, now) for r in seasoned] or [None], key=lambda x: -1 if x is None else x)
    since30 = now - timedelta(days=30)
    sales = business.sales.filter(sold_at__gte=since30).exclude(reference__startswith='MKT-')
    sale_days = sales.annotate(d=TruncDate('sold_at')).values('d').distinct().count()
    n_sales = sales.count()
    week = now - timedelta(days=7)
    devices = UsageRecord.objects.filter(business=business, hour__gte=week).exclude(mac_address='').values('mac_address').distinct().count()
    if not devices:
        devices = Voucher.all_objects.filter(business=business, used_at__gte=week).count()
    complaints = VoucherReport.objects.filter(business=business, created_at__gte=since30).count()
    per100 = round(complaints / n_sales * 100, 1) if n_sales else 0
    disputes = MarketDeal.objects.filter(seller=business, status='rejected', claimed_at__isnull=False,
                                         created_at__gte=now - timedelta(days=90)).count()
    from . import features
    plan_ok = features.uses(business, 'market')
    rows = [
        ('age', 'On TapTap for 30 days or more', age >= 30, f'{age} days', 8, False),
        ('email', 'Email address verified', bool(business.email_verified_at), 'verified' if business.email_verified_at else 'not verified', 6, True),
        ('router', 'A router online, watched by TapTap for 30+ days', bool(online) and bool(seasoned),
         f'{len(online)} online now, {len(seasoned)} with 30+ days of history', 14, True),
        ('uptime', 'Uptime of 97% or more (30 days)', best_up is not None and best_up >= 97,
         f'{best_up}% on the best router' if best_up is not None else 'not enough history', 16, False),
        ('sales', 'Sales on 15 of the last 30 days', sale_days >= 15, f'sales on {sale_days} days', 14, False),
        ('devices', 'At least 10 different devices in the last 7 days', devices >= 10, f'{devices} devices', 12, False),
        ('complaints', 'Fewer than 3 complaints per 100 sales', per100 < 3, f'{per100} per 100 sales', 10, False),
        ('wallet', 'A mobile-money wallet to be paid on', wallets(business).exists(),
         ', '.join(w.get_provider_display() for w in wallets(business)) or 'none yet', 6, True),
        ('plan', 'Plan includes selling on the Market', plan_ok, business.tier_name if hasattr(business, 'tier_name') else '', 6, True),
        ('record', 'No buyer left unpaid-for on the Market (90 days)', disputes == 0,
         'clean' if disputes == 0 else f'{disputes} payment{"s" if disputes != 1 else ""} a buyer said they made were refused', 8, False),
    ]
    checks = [{'key': k, 'label': lbl, 'ok': bool(ok), 'detail': det, 'weight': w, 'required': req} for k, lbl, ok, det, w, req in rows]
    score = sum(c['weight'] for c in checks if c['ok'])
    return checks, score, all(c['ok'] for c in checks if c['required'])


# ─────────────────────────────── rules used by the Market ───────────────────────────────

def seller_of(business):
    return MarketSeller.objects.filter(business=business).first()


def is_vetted(business):
    return MarketSeller.objects.filter(business=business, status='approved').exists()


def limits(seller):
    lo, lb = LIMITS.get(seller.level, LIMITS['starter'])
    return (seller.max_open_listings if seller.max_open_listings is not None else lo,
            seller.max_batch_vouchers if seller.max_batch_vouchers is not None else lb)


def require_vetted(business, quantity):
    """Raise (as a MarketError) unless this business may list a batch of ``quantity`` vouchers now."""
    from .market import MarketError
    s = seller_of(business)
    if not s or s.status != 'approved':
        state = s.get_status_display().lower() if s else 'not applied yet'
        raise MarketError(f'Only sellers vetted by TapTap can list batches (you: {state}). Apply under Market › Selling.')
    open_max, batch_max = limits(s)
    if batch_max and quantity > batch_max:
        raise MarketError(f'As a {s.get_level_display().lower()} you can list up to {batch_max} vouchers in one batch.')
    if open_max is not None:
        n = MarketListing.objects.filter(business=business, status__in=('open', 'reserved')).count()
        if n >= open_max:
            raise MarketError(f'As a {s.get_level_display().lower()} you can have {open_max} listings on the market at a time.')


def vetted_q(prefix='business__'):
    return Q(**{f'{prefix}market_seller__status': 'approved'})


# ─────────────────────────────── applying ───────────────────────────────

def _file(f):
    if not f:
        return ''
    if f.size > MAX_FILE:
        raise VettingError('Files must be under 3 MB.')
    ctype = (getattr(f, 'content_type', '') or '').lower()
    if ctype not in FILE_TYPES:
        raise VettingError('Upload a photo (JPG, PNG, WebP) or a PDF.')
    return f'data:{ctype};base64,' + base64.b64encode(f.read()).decode()


def apply(business, user, data, files):
    s = seller_of(business)
    today = timezone.localdate()
    if s and s.status == 'approved':
        raise VettingError('You are already a vetted seller.')
    if s and s.status == 'suspended':
        raise VettingError('Your selling is suspended. TapTap will contact you.')
    if s and s.status == 'rejected' and s.reapply_after and s.reapply_after > today:
        raise VettingError(f'You can apply again from {s.reapply_after:%d %B %Y}.')
    if data.get('agree') != '1':
        raise VettingError('Accept the Market seller rules to apply.')
    need = {'contact_name': 'your name', 'contact_phone': 'a phone number', 'town': 'the town or area',
            'operating_since': 'since when the network runs', 'how_sell': 'how customers buy today',
            'support_plan': 'how customers get help'}
    clean = {}
    for f, label in need.items():
        v = str(data.get(f) or '').strip()
        if len(v) < 2:
            raise VettingError(f'Tell TapTap {label}.')
        clean[f] = v[:400] if f in ('how_sell', 'support_plan') else v[:120]
    try:
        sites = max(1, min(999, int(data.get('sites') or 1)))
    except ValueError:
        sites = 1
    photo, doc = _file(files.get('site_photo')), _file(files.get('document'))
    if not photo and not (s and s.site_photo):
        raise VettingError('Add a photo of your installation (the router or an access point in place).')
    checks, score, _ = run_checks(business)
    first = s is None
    s = s or MarketSeller(business=business)
    for k, v in clean.items():
        setattr(s, k, v)
    s.sites, s.status, s.checks, s.score = sites, 'applied', checks, score
    s.agreed_at = s.submitted_at = timezone.now()
    if photo:
        s.site_photo = photo
    if doc:
        s.document = doc
    s.save()
    MarketVettingEvent.objects.create(seller=s, actor=user, action='applied' if first else 'resubmitted',
                                      note=f'Score {score}/100')
    _tell_platform(f'Market seller to vet: {business.business_name}',
                   f'{business.business_name} ({clean["town"]}) applied to sell on the TapTap Market. Automatic score {score}/100.',
                   reverse('platform_market_seller', args=[s.pk]))
    return s


def _tell_platform(subject, body, link):
    emails = list(get_user_model().objects.filter(is_superuser=True, is_active=True).exclude(email='').values_list('email', flat=True))
    if not emails:
        return
    site = (getattr(settings, 'SITE_URL', '') or '').rstrip('/')
    try:
        send_mail(f'[TapTap] {subject}', f'{body}\n\n{site}{link}', getattr(settings, 'DEFAULT_FROM_EMAIL', None), emails, fail_silently=True)
    except Exception:
        logger.exception('platform email')


def _tell_seller(s, subject, body):
    from .market import _tell
    _tell(s.business, subject, body, reverse('market') + '?tab=selling')


# ─────────────────────────────── decisions ───────────────────────────────

def decide(seller, user, action, note='', level=None, max_open=None, max_batch=None, days=REAPPLY_DAYS):
    now = timezone.now()
    note = str(note or '').strip()[:500]
    s = seller
    if action == 'approve':
        if s.status not in ('applied', 'info', 'rejected', 'suspended'):
            raise VettingError('Already approved.')
        s.status, s.approved_at = 'approved', s.approved_at or now
        s.level = level if level in dict(MarketSeller.LEVELS) else s.level
        s.max_open_listings, s.max_batch_vouchers = max_open, max_batch
        s.watch, s.watch_note, s.reapply_after = False, '', None
        lo, lb = limits(s)
        _tell_seller(s, 'Market: you are a vetted seller', f'TapTap approved {s.business.business_name} as a {s.get_level_display().lower()}: '
                     f'up to {lo if lo is not None else "unlimited"} listings at a time, {lb if lb else "any number of"} vouchers per batch.'
                     + (f' {note}' if note else ''))
    elif action == 'info':
        if not note:
            raise VettingError('Say what TapTap needs.')
        s.status = 'info'
        _tell_seller(s, 'Market: TapTap needs a little more', note)
    elif action == 'reject':
        if not note:
            raise VettingError('Give the reason — the seller sees it.')
        s.status, s.reapply_after = 'rejected', timezone.localdate() + timedelta(days=max(0, int(days or 0)))
        _tell_seller(s, 'Market: application not approved', f'{note} You can apply again from {s.reapply_after:%d %B %Y}.')
    elif action == 'suspend':
        if s.status != 'approved':
            raise VettingError('Only an approved seller can be suspended.')
        if not note:
            raise VettingError('Give the reason — the seller sees it.')
        s.status = 'suspended'
        _tell_seller(s, 'Market: your listings are suspended', f'{note} Batches you already sold keep working for their buyers.')
    elif action == 'reinstate':
        if s.status != 'suspended':
            raise VettingError('Only a suspended seller can be reinstated.')
        s.status = 'approved'
        _tell_seller(s, 'Market: your listings are back', note or 'TapTap reinstated your selling on the Market.')
    elif action == 'level':
        if s.status != 'approved':
            raise VettingError('Approve the seller first.')
        s.level = level if level in dict(MarketSeller.LEVELS) else s.level
        s.max_open_listings, s.max_batch_vouchers = max_open, max_batch
        _tell_seller(s, f'Market: you are now a {s.get_level_display().lower()}', note or 'Your selling limits changed.')
    elif action == 'clear_watch':
        s.watch, s.watch_note = False, ''
    else:
        raise VettingError('Unknown action.')
    if action in ('approve', 'info', 'reject', 'suspend', 'reinstate'):
        s.note = note
    s.reviewed_by, s.reviewed_at = user, now
    s.save()
    MarketVettingEvent.objects.create(seller=s, actor=user, action=action, note=note or (s.get_level_display() if action in ('approve', 'level') else ''))
    return s


def watch_sellers(now=None):
    """Once a day: re-run the checks for vetted sellers and put slipping ones on the platform's watch list."""
    if not cache.add('tt:mkt:vet-watch', 1, 86400):
        return 0
    n = 0
    for s in MarketSeller.objects.filter(status='approved').select_related('business'):
        checks, score, _ = run_checks(s.business, now)
        bad = [c['label'] for c in checks if c['key'] in ('uptime', 'complaints', 'record') and not c['ok']]
        MarketSeller.objects.filter(pk=s.pk).update(checks=checks, score=score)
        if bad and not s.watch:
            MarketSeller.objects.filter(pk=s.pk).update(watch=True, watch_note=('Slipped: ' + '; '.join(bad))[:300])
            MarketVettingEvent.objects.create(seller=s, action='watch', note=('Slipped: ' + '; '.join(bad))[:500])
            _tell_platform(f'Market seller on watch: {s.business.business_name}', 'Daily check: ' + '; '.join(bad),
                           reverse('platform_market_seller', args=[s.pk]))
            n += 1
    return n


# ─────────────────────────────── pages: the seller ───────────────────────────────

@require_POST
def seller_apply(request):
    from .market import _business, _is_owner
    if not request.user.is_authenticated:
        return redirect('login')
    if not _is_owner(request):
        messages.error(request, 'Only the business owner can apply to sell on the Market.')
        return redirect(reverse('market') + '?tab=selling')
    try:
        apply(_business(request), request.user, request.POST, request.FILES)
    except VettingError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, 'Thank you — TapTap will look at your application and tell you here and by email.')
    return redirect(reverse('market') + '?tab=selling#vetting')


def seller_context(business):
    """What the Selling tab shows about vetting."""
    s = seller_of(business)
    checks, score, req = run_checks(business)
    lo = lb = None
    if s and s.status == 'approved':
        lo, lb = limits(s)
    steps = ['Apply', 'TapTap checks', 'Decision', 'Selling']
    at = 0 if not s else {'applied': 1, 'info': 1, 'rejected': 2, 'approved': 3, 'suspended': 2}[s.status]
    return {'seller': s, 'checks': checks, 'score': score, 'required_ok': req, 'rules': RULES, 'limit_open': lo, 'limit_batch': lb,
            'can_apply': not s or s.status in ('info',) or (s.status == 'rejected' and (not s.reapply_after or s.reapply_after <= timezone.localdate())),
            'steps': [{'label': x, 'done': i < at or (s and s.status == 'approved'), 'now': i == at} for i, x in enumerate(steps)]}


# ─────────────────────────────── pages: the platform ───────────────────────────────

def _superuser(view):
    from .views_platform import superuser_required
    return superuser_required(view)


def platform_list(request):
    tab = request.GET.get('tab') or 'waiting'
    qs = MarketSeller.objects.select_related('business').annotate(listings=Count('business__market_listings', distinct=True))
    filt = {'waiting': Q(status='applied'), 'info': Q(status='info'), 'approved': Q(status='approved'),
            'watch': Q(watch=True), 'closed': Q(status__in=('rejected', 'suspended'))}.get(tab, Q(status='applied'))
    counts = {k: MarketSeller.objects.filter(v).count() for k, v in
              {'waiting': Q(status='applied'), 'info': Q(status='info'), 'approved': Q(status='approved'), 'watch': Q(watch=True),
               'closed': Q(status__in=('rejected', 'suspended'))}.items()}
    rows = list(qs.filter(filt).order_by('submitted_at' if tab == 'waiting' else '-updated_at')[:200])
    for r in rows:
        r.failed_required = [c['label'] for c in (r.checks or []) if c.get('required') and not c.get('ok')]
        r.waiting_days = (timezone.now() - r.submitted_at).days
    return render(request, 'core/platform/market.html', {'tab': tab, 'rows': rows, 'counts': counts,
                                                          'deals': MarketDeal.objects.filter(status__in=MarketDeal.OWNED).count(),
                                                          'open_listings': MarketListing.objects.filter(status='open').filter(vetted_q()).count()})


def platform_seller(request, pk):
    s = get_object_or_404(MarketSeller.objects.select_related('business', 'reviewed_by'), pk=pk)
    checks, score, req = run_checks(s.business)
    b = s.business
    preview = None
    router = next(iter(sorted(b.routers.all(), key=lambda r: (str(r.status).lower() != 'online', r.pk))), None)
    plan = b.plans.filter(active=True, price__gt=0).order_by('price').first()
    if router and plan:
        from .market_diligence import build
        probe = MarketListing(business=b, router=router, plan=plan, plan_name=plan.name, face_price=plan.price,
                              min_retail=plan.price, quantity=100, price=plan.price * 80, area='—', precision='exact')
        try:
            preview = build(probe)
        except Exception:
            logger.exception('vetting preview for %s', b)
    deals = MarketDeal.objects.filter(seller=b).select_related('buyer', 'listing').order_by('-created_at')[:20]
    rated = [d.rating for d in deals if d.rating]
    lo, lb = limits(s)
    return render(request, 'core/platform/market_seller.html', {
        's': s, 'b': b, 'checks': checks, 'score': score, 'required_ok': req, 'dd': preview, 'router': router, 'plan': plan,
        'deals': deals, 'rating': round(sum(rated) / len(rated), 1) if rated else None, 'events': s.events.select_related('actor')[:40],
        'listings': b.market_listings.order_by('-created_at')[:20], 'levels': MarketSeller.LEVELS, 'limit_open': lo, 'limit_batch': lb,
        'level_limits': LIMITS, 'rules': RULES, 'photo_is_image': s.site_photo.startswith('data:image/'),
        'doc_is_image': s.document.startswith('data:image/')})


def _int_or_none(v):
    v = str(v or '').strip()
    return int(v) if v.isdigit() else None


@require_POST
def platform_action(request, pk):
    s = get_object_or_404(MarketSeller.objects.select_related('business'), pk=pk)
    try:
        decide(s, request.user, request.POST.get('action'), request.POST.get('note'), request.POST.get('level'),
               _int_or_none(request.POST.get('max_open')), _int_or_none(request.POST.get('max_batch')),
               _int_or_none(request.POST.get('days')) if request.POST.get('days') else REAPPLY_DAYS)
    except VettingError as e:
        messages.error(request, str(e))
    else:
        try:
            from .views_platform import audit
            audit(request, s.business, f'Market seller: {request.POST.get("action")}', request.POST.get('note') or '')
        except Exception:
            logger.exception('platform audit')
        messages.success(request, f'{s.business.business_name}: {s.get_status_display()}.')
    return redirect('platform_market_seller', pk=pk)


def platform_file(request, pk, which):
    s = get_object_or_404(MarketSeller, pk=pk)
    data = s.site_photo if which == 'photo' else s.document if which == 'document' else ''
    if not data.startswith('data:') or ';base64,' not in data:
        raise Http404
    head, b64 = data.split(';base64,', 1)
    ctype = head[5:]
    if ctype not in FILE_TYPES:
        raise Http404
    resp = HttpResponse(base64.b64decode(b64), content_type=ctype)
    resp['Content-Disposition'] = f'inline; filename="{which}-{s.pk}.{ctype.split("/")[-1]}"'
    resp['X-Content-Type-Options'] = 'nosniff'
    resp['Cache-Control'] = 'private, no-store'
    return resp


URLS = [
    ('market/apply/', seller_apply, 'market_apply', False),
    ('platform/market/', platform_list, 'platform_market', True),
    ('platform/market/<int:pk>/', platform_seller, 'platform_market_seller', True),
    ('platform/market/<int:pk>/action/', platform_action, 'platform_market_action', True),
    ('platform/market/<int:pk>/file/<str:which>/', platform_file, 'platform_market_file', True),
]


def install():
    from django.urls import path
    from . import permissions, urls
    names = {getattr(x, 'name', None) for x in urls.urlpatterns}
    for route, view, name, staff in URLS:
        if name not in names:
            urls.urlpatterns.append(path(route, _superuser(view) if staff else view, name=name))
        if not staff:
            permissions.URL_PERMS[name] = ('finance.view',)
