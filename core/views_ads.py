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
    v = _portal_voucher(page, data.get('code'))
    if sig and v:
        from .shared_use import check_after_login
        check_after_login(page.business, v)
    return _cors(JsonResponse({'ok': bool(sig)}))


def _portal_voucher(page, code):
    code = re.sub(r'[\s-]', '', str(code or ''))
    return page.business.vouchers.filter(code__iexact=code).first() if code else None


def _portal_json(request):
    if len(request.body or b'') > 8000:
        return None
    try:
        return json.loads(request.body or '{}')
    except ValueError:
        return {}


@csrf_exempt
def portal_state(request, slug):
    """Router-served portal pages ask this before logging in: is the voucher frozen or warned?
    Also records the device (like device_beacon) and runs the shared-use check.
    Answers {"ok": true} when the router may go ahead."""
    from .shared_use import check_after_login
    from .voucher_freeze import portal_block
    if request.method == 'OPTIONS':
        return _cors(HttpResponse(status=204))
    if request.method != 'POST' or _throttle(f'pstate:{_client_ip(request)}', 30, 60):
        return _cors(JsonResponse({'ok': True}))          # never block a login because of this check
    page = PortalPage.objects.select_related('business').filter(slug=slug).first()
    data = _portal_json(request)
    if not page or data is None:
        return _cors(JsonResponse({'ok': True}))
    v = _portal_voucher(page, data.get('code'))
    if not v:
        return _cors(JsonResponse({'ok': True}))
    if data.get('fp') and not v.frozen_at:
        try:
            record_device(page.business, data.get('fp', ''), data.get('c') or {}, request.META.get('HTTP_USER_AGENT', ''),
                          mac=data.get('mac', ''), ip=data.get('ip') or _client_ip(request), code=v.code, portal=page)
            if check_after_login(page.business, v):
                v.refresh_from_db()
        except Exception:
            pass
    block = portal_block(v)
    if not block and v.status == 'active':
        from . import device_lock          # sticky vouchers: refuse devices the voucher is not locked to
        lock = device_lock.claim(v, mac=data.get('mac', ''), fp=data.get('fp', ''), source='portal', hints=device_lock.online_hints(v))
        if not lock.allowed:
            block = device_lock.portal_block(v, lock.message)
        else:
            # Its own old session (from before the phone left / changed MAC) would make the router refuse
            # the login ("no more sessions"): remove it first, and tell the page how long to wait.
            wait = device_lock.release_stale(v, data.get('mac', ''))
            if wait:
                return _cors(JsonResponse({'ok': True, 'wait': wait, 'message': 'Getting your connection ready…'}))
    # The real username (right upper/lower case) so router-served pages log members in exactly.
    return _cors(JsonResponse(block or {'ok': True, 'code': v.code}))


@csrf_exempt
def portal_accept(request, slug):
    """The customer pressed "I agree" on the warning page: internet and clock continue."""
    from .shared_use import customer_accepted
    from .voucher_history import channel
    if request.method == 'OPTIONS':
        return _cors(HttpResponse(status=204))
    if request.method != 'POST' or _throttle(f'paccept:{_client_ip(request)}', 10, 60):
        return _cors(JsonResponse({'success': False, 'message': 'Too many tries. Wait a minute.'}, status=429))
    page = PortalPage.objects.select_related('business').filter(slug=slug).first()
    data = _portal_json(request) or {}
    v = _portal_voucher(page, data.get('code')) if page else None
    if not v or not (v.frozen_at and v.freeze_kind == 'warning'):
        return _cors(JsonResponse({'success': False, 'message': 'There is no warning to accept for this code.'}, status=400))
    customer_accepted(v, str(data.get('fp') or ''))
    # TapTap Link routers apply the unfreeze at their next check-in, so the page waits a little before logging in.
    wait = 15 if channel(v.router) == 'TapTap Link' else 0
    return _cors(JsonResponse({'success': True, 'code': v.code, 'wait': wait,
                               'message': 'Thank you. Your internet continues from where it stopped.'}))


# ───────────────────────────── Devices page ─────────────────────────────
@login_required
def devices(request):
    business = _b(request)
    qs = business.device_signatures.select_related('router')
    q = request.GET.get('q', '').strip()
    view = request.GET.get('view', 'all')
    if view == 'flagged':
        qs = qs.filter(flagged=True)
    elif view == 'blacklist':
        qs = qs.filter(blacklisted_at__isnull=False)
    elif view == 'today':
        qs = qs.filter(last_seen__gte=timezone.now() - timedelta(hours=24))
    from . import device_search as ds
    search_info = None
    if q:
        items, search_info = ds.search(business, q, qs)
    else:
        items = list(qs[:300])
    if view == 'multimac':
        items = [d for d in items if len(d.macs or []) > 1]
    all_sigs = business.device_signatures
    stats = {
        'total': all_sigs.count(), 'today': all_sigs.filter(last_seen__gte=timezone.now() - timedelta(hours=24)).count(),
        'flagged': all_sigs.filter(flagged=True).count(),
        'blacklisted': all_sigs.filter(blacklisted_at__isnull=False).count(),
        'os': list(all_sigs.values('os').annotate(n=Count('id')).order_by('-n')[:6]),
    }
    multi = [d for d in all_sigs.only('id', 'macs')[:2000] if len(d.macs or []) > 1]
    stats['multimac'] = len(multi)
    from .shared_use import cases
    from .voucher_freeze import warning_text
    shared = cases(business, limit=100)
    stats['shared'] = sum(1 for c in shared if c['open'])
    collecting = business.portal_pages.filter(kind='login', is_published=True).exists()
    return render(request, 'core/devices.html', {'devices': items, 'stats': stats, 'shared': shared if view in {'all', 'shared'} else [],
                                                 'shared_open': [c for c in shared if c['open']], 'shared_waiting': [c for c in shared if c['waiting']],
                                                 'shared_done': [c for c in shared if not c['open'] and not c['waiting']],
                                                 'warning_text': warning_text(business), 'custom_warning': business.shared_warning_text,
                                                 'q': q, 'view': view, 'collecting': collecting, 'search': search_info,
                                                 'search_help': ds.HELP, 'search_suggest': ds.suggestions(business), 'search_sorts': ds.SORTS,
                                                 'quick': [('Online now', 'is:online'), ('Heavy users', 'used>2gb sort:data'), ('Slowed', 'is:slowed'),
                                                           ('Shared vouchers', 'is:shared'), ('Random MAC', 'is:random'), ('New today', 'is:new'),
                                                           ('Video watchers', 'app:youtube sort:data'), ('iPhones', 'os:ios')]})


@login_required
@require_POST
def shared_resolve(request, pk):
    """Decide a voucher used on too many devices: allow, warn, reset devices, freeze or disable."""
    from .shared_use import SharedError, resolve
    v = get_object_or_404(_b(request).vouchers.select_related('router'), pk=pk)
    try:
        for level, msg in resolve(v, request.POST.get('action', ''), request.user, request.POST.get('note', '')):
            getattr(messages, level)(request, msg)
    except SharedError as e:
        messages.error(request, str(e))
    except ValueError as e:
        messages.error(request, str(e))
    nxt = request.POST.get('next', '')
    return redirect(nxt if nxt.startswith('/') and not nxt.startswith('//') else 'devices')


@login_required
@require_POST
def shared_settings(request):
    business = _b(request)
    mode = request.POST.get('shared_warning_mode')
    business.shared_warning_mode = mode if mode in ('manual', 'auto') else 'manual'
    business.shared_warning_text = request.POST.get('shared_warning_text', '').strip()[:1500]
    business.save(update_fields=['shared_warning_mode', 'shared_warning_text'])
    messages.success(request, 'Automatic warnings are on: a voucher seen on too many devices is paused until the customer agrees.'
                     if business.shared_warning_mode == 'auto' else 'Manual warnings: shared vouchers are listed here for you to decide.')
    return redirect('/devices/?view=shared')


@login_required
@require_POST
def device_action(request, pk):
    sig = get_object_or_404(_b(request).device_signatures, pk=pk)
    action = request.POST.get('action')
    if action == 'label':
        sig.label = request.POST.get('label', '').strip()[:120]; sig.note = request.POST.get('note', '').strip()[:255]
        sig.save(update_fields=['label', 'note'])
    elif action == 'flag':
        from .blacklist import flag
        flag(sig, '' if sig.flagged else 'watch', user=request.user)
    elif action == 'flag_set':
        from .blacklist import flag
        flag(sig, request.POST.get('level', ''), request.POST.get('note', '').strip()[:255], request.POST.get('notify') == 'on', request.user)
        messages.success(request, 'Flag saved.' if sig.flagged else 'Flag removed.')
    elif action == 'blacklist':
        from .blacklist import blacklist
        res = blacklist(sig, request.user, request.POST.get('reason', ''))
        messages.success(request, f'Device blacklisted — it cannot use your Wi-Fi until you remove it. {res}')
    elif action == 'unblacklist':
        from .blacklist import unblacklist
        n = unblacklist(sig, request.user)
        messages.success(request, f'Removed from the blacklist — it can connect again ({n} router block(s) removed).')
    elif action == 'delete':
        sig.delete(); messages.success(request, 'Device forgotten.')
    return redirect(request.POST.get('next') or 'devices')


# ───────────────────────────── One device: what uses its data ─────────────────────────────
DEVICE_PERIODS = {'24h': ('Last 24 hours', 1), '7d': ('Last 7 days', 7), '30d': ('Last 30 days', 30)}


@login_required
def device_detail(request, pk):
    """Charts and top lists of the apps and sites that use this device's data."""
    from collections import defaultdict
    from django.db.models import Sum
    from .models import DeviceAppUsage, UsageRecord
    business = _b(request)
    d = get_object_or_404(business.device_signatures.select_related('router'), pk=pk)
    period = request.GET.get('period') if request.GET.get('period') in DEVICE_PERIODS else '7d'
    label, days = DEVICE_PERIODS[period]
    now = timezone.now()
    start = now - timedelta(days=days)
    macs = [str(m).upper() for m in (d.macs or []) if m] or ([d.last_mac.upper()] if d.last_mac else [])
    # MAC first (phones' random MACs are all listed on the device); vouchers only when no MAC is known
    match = Q(mac__in=macs) if macs else Q(username__in=[str(v) for v in (d.vouchers or [])] or ['-'])
    qs = DeviceAppUsage.objects.filter(business=business, hour__gte=start).filter(match)
    tot = qs.aggregate(dn=Sum('download'), up=Sum('upload'))
    down, up = tot['dn'] or 0, tot['up'] or 0
    total = down + up
    apps = list(qs.values('app', 'category').annotate(dn=Sum('download'), up=Sum('upload')).order_by())
    for a in apps:
        a['total'] = a['dn'] + a['up']
        a['pct'] = round(a['total'] * 100 / total, 1) if total else 0
    apps.sort(key=lambda a: -a['total'])
    sites = list(qs.exclude(domain='many sites').values('domain', 'app').annotate(dn=Sum('download'), up=Sum('upload')).order_by())
    for s in sites:
        s['total'] = s['dn'] + s['up']
        s['pct'] = round(s['total'] * 100 / total, 1) if total else 0
    sites.sort(key=lambda s: -s['total'])
    cats = defaultdict(int)
    for a in apps:
        cats[a['category']] += a['total']
    from .traffic import CATEGORY_ORDER
    categories = [{'name': c, 'total': cats[c]} for c in CATEGORY_ORDER if cats.get(c)] + \
                 [{'name': c, 'total': v} for c, v in cats.items() if c not in CATEGORY_ORDER and v]
    # over time: hours for a day, days otherwise; the 5 biggest apps + everything else
    top5 = [a['app'] for a in apps[:5]]
    step = 'hour' if days == 1 else 'day'
    tz = timezone.get_current_timezone()
    if step == 'hour':
        first = timezone.localtime(now).replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
        slots = [first + timedelta(hours=i) for i in range(24)]
        key = lambda h: timezone.localtime(h).replace(minute=0, second=0, microsecond=0)
        fmt = '%H:%M'
    else:
        first = timezone.localtime(now).date() - timedelta(days=days - 1)
        slots = [first + timedelta(days=i) for i in range(days)]
        key = lambda h: timezone.localtime(h).date()
        fmt = '%d %b'
    grid = defaultdict(lambda: defaultdict(int))
    for h, app, dn, u in qs.values_list('hour', 'app', 'download', 'upload'):
        grid[key(h)][app if app in top5 else 'Everything else'] += dn + u
    series_names = top5 + (['Everything else'] if any('Everything else' in grid[s] for s in grid) else [])
    timeline = {'labels': [s.strftime(fmt) for s in slots],
                'series': [{'name': n, 'data': [round(grid[s].get(n, 0) / 1024 ** 2, 1) for s in slots]} for n in series_names]}
    busiest = max(slots, key=lambda s: sum(grid[s].values())) if grid else None
    # everything the hotspot counted for these MACs (includes traffic between connection samples)
    counted = UsageRecord.objects.filter(business=business, hour__gte=start, mac_address__in=macs).aggregate(
        dn=Sum('download'), up=Sum('upload')) if macs else {'dn': 0, 'up': 0}
    counted_total = (counted['dn'] or 0) + (counted['up'] or 0)
    return render(request, 'core/device_detail.html', {
        'd': d, 'period': period, 'periods': DEVICE_PERIODS, 'period_label': label, 'macs': macs,
        'down': down, 'up': up, 'total': total, 'apps': apps[:15], 'sites': sites[:20], 'top_app': apps[0] if apps else None,
        'categories': categories, 'timeline': timeline, 'busiest': busiest.strftime('%d %b %H:00' if step == 'hour' else '%a %d %b') if busiest and grid[busiest] else '',
        'counted_total': counted_total, 'coverage': round(total * 100 / counted_total) if counted_total else None,
        'chart_data': {'timeline': timeline, 'categories': categories, 'step': step},
    })
