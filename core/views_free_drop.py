"""Bonanza › Free Wi-Fi: free hours, happy hours, lucky draws and surprise gifts — no wheel."""
from __future__ import annotations

import json
import secrets
from datetime import datetime

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import free_drop as fd
from .models_free_drop import FreeDrop

COOKIE = 'ttfd'

# Ready-made ideas: one click fills the form.
RECIPES = [
    {'key': 'first50', 'icon': 'bi-lightning-charge', 'title': 'Free hour — first 50', 'text': 'The first 50 phones get 1 free hour. Print the QR at the shop.',
     'set': {'kind': 'claim', 'name': 'Free hour for the first 50', 'headline': 'Free Wi-Fi hour — first 50 only!', 'minutes': 60, 'pick': 'first', 'limit': 50, 'audience': 'everyone'}},
    {'key': 'happy', 'icon': 'bi-cup-hot', 'title': 'Happy hour for everyone', 'text': 'Every day 18:00–19:00 anyone can get free Wi-Fi until the hour ends.',
     'set': {'kind': 'claim', 'name': 'Happy hour', 'headline': 'Happy hour — Wi-Fi is on us', 'minutes': 60, 'pick': 'all', 'audience': 'everyone',
             'daily_from': '18:00', 'daily_to': '19:00', 'end_with_window': True}},
    {'key': 'thanks', 'icon': 'bi-heart', 'title': 'Thank-you hours for my customers', 'text': 'Only people who bought from you: they type their voucher code and get 2 free hours.',
     'set': {'kind': 'claim', 'name': 'Thank-you hours', 'headline': 'Thank you for buying with us — 2 hours on us', 'minutes': 120, 'pick': 'first', 'audience': 'customers'}},
    {'key': 'draw', 'icon': 'bi-shuffle', 'title': 'Lucky draw — 20 winners', 'text': 'People enter all day; 20 random phones win 3 free hours at the draw.',
     'set': {'kind': 'claim', 'name': 'Lucky draw', 'headline': 'Enter now — 20 lucky phones win 3 hours', 'minutes': 180, 'pick': 'draw', 'limit': 20, 'audience': 'everyone'}},
    {'key': 'surprise', 'icon': 'bi-gift', 'title': 'Surprise for 10 online now', 'text': '10 random customers online right now get +1 hour on their own voucher.',
     'set': {'kind': 'gift', 'name': 'Surprise hour', 'minutes': 60, 'target': 'online', 'random_only': True, 'limit': 10}},
    {'key': 'weekend', 'icon': 'bi-calendar-heart', 'title': 'Weekend gift to my buyers', 'text': 'Everyone who bought this week gets +2 hours, every Saturday at 10:00.',
     'set': {'kind': 'gift', 'name': 'Weekend thank-you', 'minutes': 120, 'target': 'recent', 'recent_days': 7, 'daily_from': '10:00', 'daily_to': '11:00', 'days': '5'}},
]


def _b(request):
    return request.user.business


def _int(v, default=None, lo=0, hi=10 ** 9):
    try:
        return max(lo, min(hi, int(str(v).strip())))
    except (TypeError, ValueError):
        return default


def _dt(v):
    v = (v or '').strip()
    if not v:
        return None
    try:
        return timezone.make_aware(datetime.strptime(v, '%Y-%m-%dT%H:%M'))
    except ValueError:
        return None


def _time(v):
    v = (v or '').strip()
    try:
        return datetime.strptime(v, '%H:%M').time() if v else None
    except ValueError:
        return None


def _slug():
    while True:
        s = secrets.token_urlsafe(6).replace('-', '').replace('_', '').lower()[:8]
        if len(s) == 8 and not FreeDrop.objects.filter(slug=s).exists():
            return s


def _base(request):
    return (getattr(settings, 'SITE_URL', '') or request.build_absolute_uri('/')).rstrip('/')


# ─────────────────────────────── owner ───────────────────────────────

@login_required
def drop_list(request):
    business = _b(request)
    try:
        fd.tick()            # a draw or gift that is due happens now too, not only in the background
    except Exception:
        pass
    items = list(business.free_drops.all())
    for d in items:
        d.state_key, d.state_text = fd.state(d)
        d.sched = fd.schedule_text(d)
        d.st = fd.stats(d)
        d.left_now = fd.left(d)
        d.time_text = fd.minutes_text(d.minutes)
    return render(request, 'core/bonanza/free_list.html', {'items': items, 'recipes': RECIPES})


@login_required
def drop_edit(request, pk=None):
    business = _b(request)
    d = get_object_or_404(business.free_drops, pk=pk) if pk else None
    if request.method == 'POST':
        P = request.POST
        name = P.get('name', '').strip()[:120]
        if not name:
            messages.error(request, 'Give it a name.')
            return redirect(request.get_full_path())
        if d is None:
            d = FreeDrop(business=business, slug=_slug(), created_by=request.user)
        d.name = name
        d.kind = P.get('kind') if P.get('kind') in dict(FreeDrop.KINDS) else 'claim'
        d.headline = P.get('headline', '').strip()[:160]
        d.message = P.get('message', '').strip()[:2000]
        d.theme_color = (P.get('theme_color') or '#18a66a')[:20]
        hours, mins = _int(P.get('hours'), 0, 0, 8784), _int(P.get('mins'), 0, 0, 59)
        d.minutes = max(1, min(fd.MAX_MINUTES, hours * 60 + mins)) if (hours or mins) else 60
        d.max_devices = _int(P.get('max_devices'), 1, 1, 20)
        d.router = business.routers.filter(pk=_int(P.get('router'), 0)).first()
        d.audience = P.get('audience') if P.get('audience') in dict(FreeDrop.AUDIENCE) else 'everyone'
        d.pick = P.get('pick') if P.get('pick') in dict(FreeDrop.PICK) else 'first'
        d.target = P.get('target') if P.get('target') in dict(FreeDrop.TARGET) else 'online'
        d.recent_days = _int(P.get('recent_days'), 7, 1, 365)
        d.random_only = bool(P.get('random_only'))
        lim = (P.get('limit') or '').strip()
        d.limit = _int(lim, None, 1, 1000000) if lim else None
        d.one_per_device = bool(P.get('one_per_device'))
        d.ask_phone = bool(P.get('ask_phone'))
        d.end_with_window = bool(P.get('end_with_window'))
        d.starts_at, d.ends_at = _dt(P.get('starts_at')), _dt(P.get('ends_at'))
        d.draw_at = _dt(P.get('draw_at'))
        if P.get('when') == 'daily':
            d.daily_from, d.daily_to = _time(P.get('daily_from')), _time(P.get('daily_to'))
            d.days = ','.join(x for x in P.getlist('days') if x.isdigit() and 0 <= int(x) <= 6)
        else:
            d.daily_from = d.daily_to = None
            d.days = ''
        if P.get('status') in dict(FreeDrop.STATUS):
            d.status = P.get('status')
        d.save()
        warn = []
        if d.kind == 'claim' and not business.routers.exists():
            warn.append('Add a router first — free codes need a router to work on.')
        if d.kind == 'claim' and d.pick == 'draw' and not (d.draw_at or d.ends_at):
            warn.append('Set when the draw happens (or an end) — or press “Draw now” yourself.')
        if d.kind == 'claim' and d.pick == 'draw' and d.daily_from:
            warn.append('A lucky draw happens once: the happy-hour times are not used for it.')
        if d.kind == 'gift' and d.random_only and d.limit is None:
            warn.append('Set how many random customers get the gift.')
        if P.get('when') == 'daily' and not (d.daily_from and d.daily_to):
            warn.append('Set the happy-hour times (from / to).')
        for w in warn:
            messages.warning(request, w)
        messages.success(request, f'{d.name} saved.' + (' It is live.' if d.status == 'live' else ''))
        return redirect('free_drop_edit', pk=d.pk)
    preset = {}
    if d is None:
        r = next((x for x in RECIPES if x['key'] == request.GET.get('recipe')), None)
        preset = dict(r['set']) if r else {}
    return render(request, 'core/bonanza/free_edit.html', {
        'd': d, 'preset': preset, 'routers': business.routers.order_by('name'), 'kinds': FreeDrop.KINDS, 'audiences': FreeDrop.AUDIENCE,
        'picks': FreeDrop.PICK, 'targets': FreeDrop.TARGET, 'statuses': FreeDrop.STATUS, 'days': list(enumerate(fd.DAYS)),
        'sel_days': set(int(x) for x in (d.days if d else str(preset.get('days', ''))).split(',') if x.strip().isdigit()),
        'public_url': f'{_base(request)}/f/{d.slug}/' if d else '', 'state': fd.state(d) if d else None,
        'stats': fd.stats(d) if d else None, 'left': fd.left(d) if d else None,
        'preview_count': len(fd.gift_targets(d)) if d and d.kind == 'gift' else None,
        'hours': (d.minutes if d else int(preset.get('minutes', 60))) // 60, 'mins': (d.minutes if d else int(preset.get('minutes', 60))) % 60,
    })


@login_required
@require_POST
def drop_action(request, pk):
    d = get_object_or_404(_b(request).free_drops, pk=pk)
    act = request.POST.get('action')
    if act in dict(FreeDrop.STATUS):
        d.status = act
        d.save(update_fields=['status'])
        messages.success(request, f'{d.name} is now {d.get_status_display().lower()}.')
        if act == 'live':
            try:
                fd.tick()
            except Exception:
                pass
    elif act == 'draw':
        n = fd.draw(d, user=request.user)
        messages.success(request, f'Drawn: {n} winner(s). They see their code on the page.' if n or d.pick == 'draw' else 'Nothing to draw.')
    elif act == 'gift':
        if d.kind != 'gift':
            messages.error(request, 'This one gives free codes, not gifts.')
        else:
            if request.POST.get('again') and d.gifted_at:
                FreeDrop.objects.filter(pk=d.pk).update(gifted_at=None)
                d.refresh_from_db()
            n = fd.gift(d, user=request.user)
            messages.success(request, f'{fd.minutes_text(d.minutes)} added to {n} customer voucher(s).' if n or not d.gifted_at else 'Already given — press “Give again” to give it once more.')
    elif act == 'delete':
        name = d.name
        d.delete()
        messages.success(request, f'{name} deleted.')
        return redirect('free_drops')
    nxt = request.POST.get('next', '')
    if nxt.startswith('/') and not nxt.startswith('//'):
        return redirect(nxt)
    return redirect('free_drop_edit', pk=d.pk)


@login_required
@require_GET
def drop_people(request, pk):
    d = get_object_or_404(_b(request).free_drops, pk=pk)
    qs = d.claims.select_related('voucher', 'proof')
    view = request.GET.get('view', 'all')
    if view in dict(d.claims.model.STATUS):
        qs = qs.filter(status=view)
    return render(request, 'core/bonanza/free_people.html', {'d': d, 'page_obj': Paginator(qs, 50).get_page(request.GET.get('page')),
                                                              'view': view, 'stats': fd.stats(d)})


@login_required
@require_GET
def drop_poster(request, pk):
    d = get_object_or_404(_b(request).free_drops, pk=pk)
    return render(request, 'core/bonanza/free_poster.html', {'d': d, 'url': f'{_base(request)}/f/{d.slug}/', 'business': d.business,
                                                              'sched': fd.schedule_text(d), 'minutes_text': fd.minutes_text(d.minutes)})


# ─────────────────────────────── public ───────────────────────────────

def _ip(request):
    return (request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR', ''))[:64]


def _throttled(key, limit, seconds):
    n = cache.get(key, 0)
    if n >= limit:
        return True
    cache.set(key, n + 1, seconds)
    return False


def _drop(slug, request=None):
    d = FreeDrop.objects.select_related('business').filter(slug=slug, kind='claim').first()
    if d is None:
        raise Http404
    if d.status == 'draft' and not (request and request.user.is_authenticated and getattr(request.user, 'business', None) == d.business):
        raise Http404
    return d


def _result(c):
    d = c.drop
    out = {'status': c.status}
    if c.status == 'won' and c.voucher_id:
        out.update({'code': c.voucher.code, 'time': fd.minutes_text(c.minutes),
                    'message': f'Your free {fd.minutes_text(c.minutes)} — enjoy!'})
    elif c.status == 'entered':
        when = d.draw_at or d.ends_at
        out['message'] = "You're in the draw! " + (f'Winners are picked {timezone.localtime(when):%d %b at %H:%M}' if when else 'Winners are picked soon') + \
                         ' — open this page again then.'
    elif c.status == 'lost':
        out['message'] = 'Not this time — thank you for taking part. Watch out for the next one!'
    else:
        out['message'] = c.get_status_display()
    return out


def drop_public(request, slug):
    d = _drop(slug, request)
    key, text = fd.state(d)
    cookie = request.COOKIES.get(COOKIE) or secrets.token_urlsafe(18)
    mine = None
    tok = request.COOKIES.get(f'fdt{d.pk}')
    if tok:
        c = d.claims.select_related('voucher').filter(token=tok).first()
        mine = json.dumps(_result(c)) if c else None
    total = d.limit if d.pick in ('first', 'all') else None
    resp = render(request, 'core/bonanza/free_public.html', {
        'd': d, 'business': d.business, 'state_key': key, 'state_text': text, 'left': fd.left(d), 'total': total,
        'time_text': fd.minutes_text(d.minutes), 'sched': fd.schedule_text(d), 'mine': mine, 'preview': d.status == 'draft',
        'mac': fd.clean_mac(request.GET.get('mac')), 'window_end': (fd.current_window(d) or (None, None))[1],
        'next_at': fd.next_window(d)})
    resp.set_cookie(COOKIE, cookie, max_age=400 * 86400, samesite='Lax', secure=request.is_secure(), httponly=True)
    return resp


@csrf_exempt
@require_POST
def drop_claim(request, slug):
    d = _drop(slug)
    if _throttled(f'fdc:{_ip(request)}', 12, 60):
        return JsonResponse({'ok': False, 'message': 'Too many tries. Wait a minute.'}, status=429)
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        data = {}
    try:
        c = fd.claim(d, device=fd.device_key(request, request.COOKIES.get(COOKIE, '')), ip=_ip(request),
                     mac=data.get('mac'), phone=data.get('phone'), code=data.get('code'))
    except fd.FreeDropError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)})
    resp = JsonResponse({'ok': True, 'result': _result(c), 'left': fd.left(d)})
    resp.set_cookie(f'fdt{d.pk}', c.token, max_age=60 * 86400, samesite='Lax', secure=request.is_secure(), httponly=True)
    return resp


@require_GET
def drop_mine(request, slug):
    d = _drop(slug)
    tok = request.COOKIES.get(f'fdt{d.pk}')
    c = d.claims.select_related('voucher').filter(token=tok).first() if tok else None
    if c is None:
        return JsonResponse({'ok': False})
    if c.status == 'entered':
        try:
            fd.tick()
            c.refresh_from_db()
        except Exception:
            pass
    return JsonResponse({'ok': True, 'result': _result(c), 'left': fd.left(d)})
