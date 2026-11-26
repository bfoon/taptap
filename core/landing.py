"""TapTap's public landing page (/) and the adverts on it, managed from the platform console (/platform/landing/).

Ad places on the page
---------------------
    strip       the announcement bar at the very top (one at a time, can be closed by the visitor)
    hero        a card beside the headline (one at a time)
    spotlight   the "Offers & news" carousel (up to 6, they slide by themselves)
    banner      a wide banner between two sections (one at a time)

Where several ads share a place, ``weight`` decides how often each is picked. Every shown ad counts a view
(not for bots or for the platform staff previewing it); a click goes through /go/ad/<id>/, which counts it and
forwards the visitor to the advertiser's link (http/https only). Pictures are stored small (WebP, at most
1600 × 900) and served from /landing/ad/<id>/image/ with a long cache, so the page itself stays light.

The page also shows live figures (businesses, routers online, vouchers sold in 30 days) once TapTap has at least
LANDING_STATS_MIN businesses — rounded down, cached for 10 minutes.
"""
from __future__ import annotations

import base64
import io
import logging
import random
import re
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.db.models import F, Q
from django.db.models.functions import Length
from django.http import Http404, HttpResponse, HttpResponseNotModified
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

from .models_landing import LandingAd

logger = logging.getLogger('taptap.landing')

MAX_UPLOAD = 6 * 1024 * 1024
MAX_SIDE = (1600, 900)
SPOTLIGHT_MAX = 6
BOTS = re.compile(r'bot|crawl|spider|slurp|preview|facebookexternalhit|whatsapp|curl|wget|python-requests', re.I)
DATA_RE = re.compile(r'^data:image/(webp|png|jpeg);base64,(.+)$', re.S)


# ─────────────────────────────── choosing ads ───────────────────────────────

def live_ads(now=None):
    now = now or timezone.now()
    return (LandingAd.objects.filter(active=True)
            .filter(Q(starts_at__isnull=True) | Q(starts_at__lte=now))
            .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now)))


def _pick(ads):
    """One ad, more often the heavier ones."""
    if not ads:
        return None
    return random.choices(ads, weights=[max(1, a.weight) for a in ads], k=1)[0]


def slots(preview=None):
    """{placement: ad or [ads]} for one page view. ``preview``: an ad the platform staff look at (shown even when not
    live, in its own place, first)."""
    ads = list(live_ads().defer('image').annotate(img_len=Length('image')))
    by = {p: [a for a in ads if a.placement == p] for p, _ in LandingAd.PLACEMENTS}
    if preview is not None:
        preview.img_len = len(preview.image or '')
        by[preview.placement] = [preview] + [a for a in by[preview.placement] if a.pk != preview.pk]
    pick = (lambda p: by[p][0]) if preview is not None else (lambda p: _pick(by[p]))
    spot = by['spotlight'] if preview is not None else sorted(by['spotlight'], key=lambda a: -a.weight - random.random())
    return {'strip': pick('strip') if by['strip'] else None, 'hero': pick('hero') if by['hero'] else None,
            'banner': pick('banner') if by['banner'] else None, 'spotlight': spot[:SPOTLIGHT_MAX]}


def _shown(s):
    out = [s[k] for k in ('strip', 'hero', 'banner') if s.get(k)]
    return out + list(s.get('spotlight') or [])


def count_views(ads):
    ids = [a.pk for a in ads if a.pk]
    if ids:
        LandingAd.objects.filter(pk__in=ids).update(views=F('views') + 1)


# ─────────────────────────────── the page ───────────────────────────────

def _nice(n):
    """Round down for a public figure: 7 → 7, 34 → 30+, 1234 → 1,200+."""
    if n < 10:
        return str(n)
    step = 10 if n < 100 else (100 if n < 10000 else 1000)
    return f'{(n // step) * step:,}+'


def public_stats():
    hit = cache.get('tt:landing:stats')
    if hit is not None:
        return hit
    from .models import Business, Router, VoucherSale
    stats = None
    try:
        businesses = Business.objects.filter(account_kind='wifi').count()     # agents and resellers are not Wi-Fi businesses
        if businesses >= int(getattr(settings, 'LANDING_STATS_MIN', 10)):
            since = timezone.now() - timedelta(days=30)
            stats = [
                {'value': _nice(businesses), 'label': 'Wi-Fi businesses', 'icon': 'bi-shop'},
                {'value': _nice(Router.objects.filter(status='Online').count()), 'label': 'routers online now', 'icon': 'bi-router'},
                {'value': _nice(VoucherSale.objects.filter(sold_at__gte=since).count()), 'label': 'vouchers sold in 30 days', 'icon': 'bi-ticket-perforated'},
                {'value': _nice(Business.objects.filter(account_kind='wifi').values('country').distinct().count()), 'label': 'countries', 'icon': 'bi-globe-europe-africa'},
            ]
    except Exception:
        logger.exception('landing stats')
    cache.set('tt:landing:stats', stats or [], 600)
    return stats or []


def plan_cards():
    """The four plans with a "from" price for the default country (core/plans.py)."""
    from . import countries, features as ft, plans
    out = []
    for key, (name, line, modules, extras) in ft.TIERS.items():
        price = None
        if key in ft.PAID_TIERS:
            try:
                rows = plans.prices(countries.DEFAULT_COUNTRY, key)
                price = min(rows, key=lambda r: r['per_month']) if rows else None
            except Exception:
                price = None
        out.append({'key': key, 'name': name, 'line': line, 'extras': extras, 'price': price,
                    'icon': plans.ICONS.get(key, 'bi-box')})
    return out


FEATURE_TOUR = [
    ('bi-ticket-perforated', 'Vouchers that sell themselves', 'Print beautiful cards, sell online with mobile money, let agents resell — every sale lands in your books.'),
    ('bi-router', 'MikroTik without the terminal', 'TapTap Link connects any router over HTTPS: no public IP, no port forwarding. Push changes, fix problems, see everything.'),
    ('bi-shield-check', 'Stops sharing and abuse', 'Sticky vouchers, sharing detection, fair usage and a security audit keep your Wi-Fi earning, not leaking.'),
    ('bi-hdd-network', 'Two internet lines, one network', 'Share the load, fail over automatically and let the Internet Doctor heal a line before customers notice.'),
    ('bi-palette', 'A login page with your brand', 'Design the portal and vouchers in a studio, show your adverts — and earn from local businesses that advertise.'),
    ('bi-robot', 'Fit, your AI technician', 'Ask in plain words: “why is the internet slow?” Fit checks the router, explains and fixes — with your permission.'),
]

STEPS = [
    ('bi-person-plus', 'Create your account', 'A free trial of everything. No card needed.'),
    ('bi-link-45deg', 'Connect your MikroTik', 'Paste one line into the router. TapTap Link does the rest.'),
    ('bi-cash-coin', 'Sell and relax', 'Make vouchers, watch sales and customers live — from your phone.'),
]

FAQ = [
    ('Do I need a public IP or a VPN?', 'No. TapTap Link makes the router call TapTap over HTTPS, from behind any NAT or 4G line.'),
    ('Which routers work?', 'MikroTik routers with RouterOS 6.49 or 7. Other access points (TP-Link, Omada…) can sit behind them.'),
    ('What happens when my trial ends?', 'You keep the Free plan: vouchers, batches, plans and routers keep working. Choose a plan any time to unlock the rest.'),
    ('Can customers pay with mobile money?', 'Yes. Online sales let customers buy a voucher with mobile money and log in straight away.'),
    ('No router yet — can I still try TapTap?', 'Yes. Download TikLab, our free MikroTik simulator for Windows, Linux and Raspberry Pi, and connect it to TapTap like a real router.'),
]


def _app(request):
    try:
        from .app_download import ctx
        return ctx(request)
    except Exception:            # the page never fails because of the app downloads
        logger.exception('app on the landing page')
        return None


def _tiklab():
    try:
        from .tiklab import landing_ctx
        return landing_ctx()
    except Exception:            # the page never fails because of the simulator downloads
        logger.exception('tiklab on the landing page')
        return None


def home(request):
    preview = None
    staff = request.user.is_authenticated and request.user.is_superuser
    if staff and str(request.GET.get('preview', '')).isdigit():
        preview = LandingAd.objects.filter(pk=int(request.GET['preview'])).first()
    try:
        s = slots(preview)
    except Exception:            # the page never fails because of an advert
        logger.exception('landing ads')
        s = {'strip': None, 'hero': None, 'banner': None, 'spotlight': []}
    if not staff and not BOTS.search(request.META.get('HTTP_USER_AGENT', '')):
        try:
            count_views(_shown(s))
        except Exception:
            logger.debug('landing views', exc_info=True)
    return render(request, 'core/landing.html', {
        'ads': s, 'preview': preview, 'stats': public_stats(), 'plans': plan_cards(), 'tour': FEATURE_TOUR,
        'steps': STEPS, 'faq': FAQ, 'app': _app(request), 'tiklab': _tiklab(), 'tiklab_page': request.build_absolute_uri('/tiklab/'), 'trial_days': getattr(settings, 'TRIAL_DAYS', 7), 'year': timezone.localdate().year,
    })


def ad_click(request, pk):
    ad = LandingAd.objects.filter(pk=pk).only('pk', 'link').first()
    if ad is None:
        return redirect('home')
    if not BOTS.search(request.META.get('HTTP_USER_AGENT', '')) and not (request.user.is_authenticated and request.user.is_superuser):
        LandingAd.objects.filter(pk=pk).update(clicks=F('clicks') + 1)
    link = ad.link or ''
    if not link.lower().startswith(('http://', 'https://')):
        return redirect('home')
    return redirect(link)


def ad_image(request, pk):
    ad = LandingAd.objects.filter(pk=pk).only('pk', 'image', 'updated_at').first()
    m = DATA_RE.match(ad.image or '') if ad else None
    if not m:
        raise Http404
    etag = f'"ad{ad.pk}-{int(ad.updated_at.timestamp())}"'
    if request.headers.get('If-None-Match') == etag:
        return HttpResponseNotModified()
    resp = HttpResponse(base64.b64decode(m.group(2)), content_type=f'image/{m.group(1)}')
    resp['ETag'] = etag
    resp['Cache-Control'] = 'public, max-age=86400'
    return resp


# ─────────────────────────────── the platform console ───────────────────────────────

RAW_MAX = 2 * 1024 * 1024        # without Pillow a picture is kept as it is, so it must already be small


def _sniff(head):
    """'png', 'jpeg' or 'webp' from a file's first bytes, else None."""
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if head.startswith(b'\xff\xd8\xff'):
        return 'jpeg'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'webp'
    return None


def _as_is(upload):
    """Keep a JPG, PNG or WebP exactly as uploaded (the server has no image library to shrink it)."""
    data = upload.read()
    kind = _sniff(data[:16])
    if not kind:
        raise ValueError('That file is not a picture TapTap can read (use JPG, PNG or WebP).')
    if len(data) > RAW_MAX:
        raise ValueError('This server cannot shrink pictures yet (Pillow is not installed), so the picture must be under 2 MB. '
                         'Upload a WebP or a smaller JPG — or rebuild the server image to install Pillow.')
    return f'data:image/{kind};base64,' + base64.b64encode(data).decode()


def picture(upload):
    """An uploaded picture as a small WebP data: URL. Raises ValueError with a readable reason."""
    if upload.size > MAX_UPLOAD:
        raise ValueError('The picture is larger than 6 MB.')
    try:
        from PIL import Image, ImageOps
    except ImportError:          # Pillow missing from the server image: keep the picture as it is
        logger.warning('Pillow is not installed: landing picture kept as uploaded')
        return _as_is(upload)
    try:
        img = Image.open(upload)
        img = ImageOps.exif_transpose(img)
        img.thumbnail(MAX_SIDE)
        if img.mode not in ('RGB', 'RGBA'):
            img = img.convert('RGBA' if 'A' in img.getbands() else 'RGB')
        out = io.BytesIO()
        img.save(out, 'WEBP', quality=82, method=4)
    except Exception:
        raise ValueError('That file is not a picture TapTap can read (use JPG, PNG or WebP).')
    return 'data:image/webp;base64,' + base64.b64encode(out.getvalue()).decode()


def _when(value):
    value = (value or '').strip()
    if not value:
        return None
    dt = parse_datetime(value.replace(' ', 'T'))
    if dt is None:
        raise ValueError(f'"{value}" is not a date and time.')
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


def save_ad(ad, data, files, user=None):
    """Fill an ad from the platform form. Raises ValueError with a readable reason."""
    title = (data.get('title') or '').strip()
    if not title:
        raise ValueError('Give the advert a title.')
    link = (data.get('link') or '').strip()
    if link and not link.lower().startswith(('http://', 'https://')):
        raise ValueError('The link must start with http:// or https://')
    ad.title, ad.text = title[:100], (data.get('text') or '').strip()[:260]
    ad.advertiser, ad.badge = (data.get('advertiser') or '').strip()[:100], (data.get('badge') or '').strip()[:24]
    ad.cta, ad.link = ((data.get('cta') or '').strip() or 'Learn more')[:30], link[:500]
    ad.placement = data.get('placement') if data.get('placement') in dict(LandingAd.PLACEMENTS) else ad.placement
    ad.style = data.get('style') if data.get('style') in dict(LandingAd.STYLES) else ad.style
    ad.starts_at, ad.ends_at = _when(data.get('starts_at')), _when(data.get('ends_at'))
    if ad.starts_at and ad.ends_at and ad.ends_at <= ad.starts_at:
        raise ValueError('The end must come after the start.')
    try:
        ad.weight = max(1, min(10, int(data.get('weight') or 1)))
    except (TypeError, ValueError):
        ad.weight = 1
    ad.active = str(data.get('active', '')).lower() in ('1', 'on', 'true', 'yes')
    if data.get('remove_image'):
        ad.image = ''
    if files and files.get('image'):
        ad.image = picture(files['image'])
    if ad.pk is None and user is not None:
        ad.created_by = user
    ad.save()
    return ad


def _superuser(view):
    from .views_platform import superuser_required
    return superuser_required(view)


def platform_page(request):
    now = timezone.now()
    ads = list(LandingAd.objects.defer('image').annotate(img_len=Length('image')).order_by('placement', '-active', '-weight', '-updated_at'))
    for a in ads:
        a.now_state = a.state(now)
    edit = None
    if str(request.GET.get('edit', '')).isdigit():
        edit = LandingAd.objects.filter(pk=int(request.GET['edit'])).first()
    totals = {'live': sum(1 for a in ads if a.now_state == 'live'), 'views': sum(a.views for a in ads),
              'clicks': sum(a.clicks for a in ads)}
    totals['ctr'] = round(totals['clicks'] * 100 / totals['views'], 1) if totals['views'] else 0
    return render(request, 'core/platform/landing.html', {
        'ads': ads, 'edit': edit, 'placements': LandingAd.PLACEMENTS, 'styles': LandingAd.STYLES, 'totals': totals,
    })


@require_POST
def platform_save(request):
    pk = request.POST.get('id')
    ad = get_object_or_404(LandingAd, pk=int(pk)) if str(pk or '').isdigit() else LandingAd()
    try:
        save_ad(ad, request.POST, request.FILES, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(f'/platform/landing/?edit={ad.pk}' if ad.pk else '/platform/landing/')
    except Exception as exc:             # platform staff see the real reason instead of "Server Error (500)"
        logger.exception('landing advert save failed')
        messages.error(request, f'The advert was not saved: {type(exc).__name__}: {exc}'[:300])
        return redirect(f'/platform/landing/?edit={ad.pk}' if ad.pk else '/platform/landing/')
    messages.success(request, f'“{ad.title}” saved — it is {ad.state()} on the landing page.')
    return redirect('platform_landing')


@require_POST
def platform_action(request, pk):
    ad = get_object_or_404(LandingAd, pk=pk)
    action = request.POST.get('action')
    if action in ('pause', 'resume'):
        ad.active = action == 'resume'
        ad.save(update_fields=['active', 'updated_at'])
        messages.success(request, f'“{ad.title}” {"is back on" if ad.active else "is paused"}.')
    elif action == 'duplicate':
        copy = LandingAd.objects.get(pk=ad.pk)
        copy.pk, copy.views, copy.clicks, copy.active, copy.created_by = None, 0, 0, False, request.user
        copy.title = f'{ad.title} (copy)'[:100]
        copy.save()
        messages.success(request, 'Copied — the copy is paused until you switch it on.')
        return redirect(f'/platform/landing/?edit={copy.pk}')
    elif action == 'reset':
        LandingAd.objects.filter(pk=ad.pk).update(views=0, clicks=0)
        messages.success(request, 'Views and clicks set back to zero.')
    elif action == 'delete':
        ad.delete()
        messages.success(request, 'Advert deleted.')
    return redirect('platform_landing')


URLS = [
    ('go/ad/<int:pk>/', ad_click, 'landing_ad_click', False),
    ('landing/ad/<int:pk>/image/', ad_image, 'landing_ad_image', False),
    ('platform/landing/', platform_page, 'platform_landing', True),
    ('platform/landing/save/', platform_save, 'platform_landing_save', True),
    ('platform/landing/<int:pk>/action/', platform_action, 'platform_landing_action', True),
]


def install():
    """URLs; the public ones open for everybody (signed in or not, any plan)."""
    from django.urls import path
    from . import middleware, permissions, team, urls
    names = {getattr(x, 'name', None) for x in urls.urlpatterns}
    for route, view, name, staff in URLS:
        if name not in names:
            urls.urlpatterns.append(path(route, _superuser(view) if staff else view, name=name))
        if not staff:
            permissions.MEMBER_ALLOWED.add(name)
            team.PLATFORM_OPEN.add(name)
            middleware.SubscriptionAccessMiddleware.ALLOWED.add(name)
