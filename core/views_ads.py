"""Ads manager, public ad tracking, device signatures."""
import json
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .ads import count, record_device, shared_vouchers
from .models import AD_PLACEMENTS, Advert, AdStat, DeviceSignature, PortalPage
from .utils import log

GIF = b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;'
IMG_RE = re.compile(r'^data:image/(png|jpeg|webp|gif);base64,[A-Za-z0-9+/=]+$')
MAX_IMAGE = 450_000


def _b(request):
    return request.user.business


def _client_ip(request):
    return request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', '')).split(',')[0].strip()


def _throttle(key, limit, seconds):
    try:
        n = cache.get(key, 0)
        if n >= limit:
            return True
        cache.set(key, n + 1, seconds)
    except Exception:
        pass
    return False


def _cors(resp):
    # Portal pages served from the router (offline export) call these from another origin.
    resp['Access-Control-Allow-Origin'] = '*'
    resp['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    resp['Access-Control-Allow-Headers'] = 'Content-Type'
    resp['Cache-Control'] = 'no-store'
    return resp


# ───────────────────────────── Ads manager ─────────────────────────────
@login_required
def ads(request):
    business = _b(request)
    today = timezone.localdate()
    items = list(business.adverts.all())
    since = today - timedelta(days=29)
    stats = AdStat.objects.filter(advert__business=business, day__gte=since)
    daily = {s['day']: s for s in stats.values('day').annotate(i=Sum('impressions'), c=Sum('clicks'))}
    days = [since + timedelta(days=i) for i in range(30)]
    chart = {'labels': [d.strftime('%d %b') for d in days],
             'impressions': [daily.get(d, {}).get('i') or 0 for d in days], 'clicks': [daily.get(d, {}).get('c') or 0 for d in days]}
    totals = {
        'live': sum(1 for a in items if a.is_live(today)), 'impressions': sum(a.impressions for a in items), 'clicks': sum(a.clicks for a in items),
        'income': sum((a.price for a in items if a.paid), Decimal('0')), 'owed': sum((a.price for a in items if not a.paid and a.price), Decimal('0')),
    }
    totals['ctr'] = round(totals['clicks'] * 100 / totals['impressions'], 1) if totals['impressions'] else 0
    for a in items:
        a.live = a.is_live(today)
        a.ctr = round(a.clicks * 100 / a.impressions, 1) if a.impressions else 0
        a.days_left = (a.ends_on - today).days if a.ends_on else None
        a.json = json.dumps({'id': a.pk, 'name': a.name, 'advertiser': a.advertiser, 'advertiser_phone': a.advertiser_phone, 'headline': a.headline,
                             'body': a.body, 'cta': a.cta, 'link': a.link, 'image': a.image, 'theme': a.theme or {}, 'placements': a.placements or [],
                             'weight': a.weight, 'starts_on': a.starts_on.isoformat() if a.starts_on else '', 'ends_on': a.ends_on.isoformat() if a.ends_on else '',
                             'price': str(a.price), 'paid': a.paid, 'active': a.active})
    return render(request, 'core/ads.html', {'ads': items, 'totals': totals, 'chart': chart, 'placements': AD_PLACEMENTS})


def _date(v):
    try:
        return timezone.datetime.strptime(v, '%Y-%m-%d').date() if v else None
    except ValueError:
        return None


@login_required
@require_POST
def ad_save(request):
    business = _b(request)
    ad = get_object_or_404(business.adverts, pk=request.POST['id']) if request.POST.get('id') else Advert(business=business)
    p = request.POST
    ad.name = (p.get('name') or p.get('headline') or 'Untitled advert').strip()[:120]
    for f, n in (('advertiser', 120), ('advertiser_phone', 60), ('headline', 120), ('body', 280), ('cta', 40)):
        setattr(ad, f, p.get(f, '').strip()[:n])
    link = p.get('link', '').strip()
    ad.link = link if re.match(r'^https?://', link) else (('https://' + link) if link and '.' in link and ' ' not in link else '')
    image = p.get('image', '').strip()
    if image == 'clear':
        ad.image = ''
    elif image:
        if not IMG_RE.match(image) or len(image) > MAX_IMAGE:
            messages.error(request, 'The image is too large or not a picture. Use a JPG or PNG under 300 KB.')
            return redirect('ads')
        ad.image = image
    ad.placements = [x for x in p.getlist('placements') if x in dict(AD_PLACEMENTS)] or ['login']
    try:
        ad.weight = max(1, min(10, int(p.get('weight') or 1)))
    except ValueError:
        ad.weight = 1
    ad.starts_on, ad.ends_on = _date(p.get('starts_on')), _date(p.get('ends_on'))
    if ad.starts_on and ad.ends_on and ad.ends_on < ad.starts_on:
        ad.starts_on, ad.ends_on = ad.ends_on, ad.starts_on
    try:
        ad.price = max(Decimal('0'), Decimal(str(p.get('price') or '0').replace(',', '')))
    except InvalidOperation:
        ad.price = Decimal('0')
    ad.paid = p.get('paid') == 'on'
    ad.active = p.get('active', 'on') == 'on'
    ad.theme = {'bg': p.get('theme_bg', '#102033')[:9], 'fg': p.get('theme_fg', '#ffffff')[:9]}
    ad.save()
    log(business, 'Advert Saved', ad.name)
    messages.success(request, f'Advert “{ad.name}” saved. Portal pages pick it up straight away; re-export router files to include it offline.')
    return redirect('ads')


@login_required
@require_POST
def ad_action(request, pk):
    business = _b(request)
    ad = get_object_or_404(business.adverts, pk=pk)
    action = request.POST.get('action')
    if action == 'delete':
        ad.delete(); messages.success(request, 'Advert deleted.')
    elif action == 'toggle':
        ad.active = not ad.active; ad.save(update_fields=['active']); messages.success(request, f'{ad.name} {"resumed" if ad.active else "paused"}.')
    elif action == 'paid':
        ad.paid = True; ad.save(update_fields=['paid']); messages.success(request, f'{ad.name} marked as paid.')
    elif action == 'duplicate':
        ad.pk = None; ad.name = f'{ad.name} (copy)'[:120]; ad.impressions = ad.clicks = 0; ad.paid = False; ad.save()
        messages.success(request, 'Advert duplicated.')
    return redirect('ads')


# ───────────────────────────── Public tracking ─────────────────────────────
def ad_seen(request, pk):
    ip = _client_ip(request)
    if not _throttle(f'ad-seen:{pk}:{ip}', 1, 600) and Advert.objects.filter(pk=pk).exists():
        count(pk, 'impressions')
    return _cors(HttpResponse(GIF, content_type='image/gif'))


def ad_go(request, pk):
    ad = Advert.objects.filter(pk=pk).only('link').first()
    if not ad or not re.match(r'^https?://', ad.link or ''):
        return HttpResponse('This advert has no link.', status=404)
    if not _throttle(f'ad-go:{pk}:{_client_ip(request)}', 3, 600):
        count(pk, 'clicks')
    return HttpResponseRedirect(ad.link)


@csrf_exempt
def device_beacon(request, slug):
    """Portal pages post the browser signature here (hosted and router-served pages)."""
    if request.method == 'OPTIONS':
        return _cors(HttpResponse(status=204))
    if request.method != 'POST':
        return _cors(JsonResponse({'ok': False}, status=405))
    if len(request.body or b'') > 8000 or _throttle(f'dev:{_client_ip(request)}', 30, 60):
        return _cors(JsonResponse({'ok': False}, status=429))
    page = PortalPage.objects.select_related('business').filter(slug=slug).first()
    if not page:
        return _cors(JsonResponse({'ok': False}, status=404))
    try:
        data = json.loads(request.body or '{}')
    except ValueError:
        data = {}
    sig = record_device(page.business, data.get('fp', ''), data.get('c') or {}, request.META.get('HTTP_USER_AGENT', ''),
                        mac=data.get('mac', ''), ip=data.get('ip') or _client_ip(request), code=data.get('code', ''), portal=page)
    return _cors(JsonResponse({'ok': bool(sig)}))


# ───────────────────────────── Devices page ─────────────────────────────
@login_required
def devices(request):
    business = _b(request)
    qs = business.device_signatures.select_related('router')
    q = request.GET.get('q', '').strip()
    view = request.GET.get('view', 'all')
    if q:
        qs = qs.filter(Q(label__icontains=q) | Q(model__icontains=q) | Q(os__icontains=q) | Q(macs__icontains=q.upper())
                       | Q(vouchers__icontains=q.upper()) | Q(ips__icontains=q) | Q(fingerprint__startswith=q.lower()))
    if view == 'flagged':
        qs = qs.filter(flagged=True)
    elif view == 'today':
        qs = qs.filter(last_seen__gte=timezone.now() - timedelta(hours=24))
    items = list(qs[:300])
    if view == 'multimac':
        items = [d for d in items if len(d.macs or []) > 1]
    all_sigs = business.device_signatures
    stats = {
        'total': all_sigs.count(), 'today': all_sigs.filter(last_seen__gte=timezone.now() - timedelta(hours=24)).count(),
        'flagged': all_sigs.filter(flagged=True).count(),
        'os': list(all_sigs.values('os').annotate(n=Count('id')).order_by('-n')[:6]),
    }
    multi = [d for d in all_sigs.only('id', 'macs')[:2000] if len(d.macs or []) > 1]
    stats['multimac'] = len(multi)
    shared = shared_vouchers(business, limit=50)
    stats['shared'] = len(shared)
    collecting = business.portal_pages.filter(kind='login', is_published=True).exists()
    return render(request, 'core/devices.html', {'devices': items, 'stats': stats, 'shared': shared if view in {'all', 'shared'} else [],
                                                 'q': q, 'view': view, 'collecting': collecting})


@login_required
@require_POST
def device_action(request, pk):
    sig = get_object_or_404(_b(request).device_signatures, pk=pk)
    action = request.POST.get('action')
    if action == 'label':
        sig.label = request.POST.get('label', '').strip()[:120]; sig.note = request.POST.get('note', '').strip()[:255]
        sig.save(update_fields=['label', 'note'])
    elif action == 'flag':
        sig.flagged = not sig.flagged; sig.save(update_fields=['flagged'])
    elif action == 'delete':
        sig.delete(); messages.success(request, 'Device forgotten.')
    return redirect(request.POST.get('next') or 'devices')
