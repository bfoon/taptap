"""Bonanza pages: the admin list, editor and winners, and the public spin page."""
import json
import secrets
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import bonanza as engine
from .models import Bonanza, BonanzaPrize, BonanzaSpin


def _b(request): return request.user.business


def _int(v, default=0, lo=0, hi=10 ** 9):
    try: return max(lo, min(hi, int(str(v).strip())))
    except (TypeError, ValueError): return default


def _dec(v):
    try: return max(Decimal('0'), Decimal(str(v).strip() or '0'))
    except (InvalidOperation, ValueError): return Decimal('0')


def _dt(v):
    v = (v or '').strip()
    if not v: return None
    try: return timezone.make_aware(datetime.strptime(v, '%Y-%m-%dT%H:%M'))
    except ValueError: return None


def _slug():
    while True:
        s = secrets.token_urlsafe(6).replace('-', '').replace('_', '').lower()[:8]
        if len(s) == 8 and not Bonanza.objects.filter(slug=s).exists():
            return s


# ─────────────────────────────── admin ───────────────────────────────

@login_required
def bonanza_list(request):
    business = _b(request)
    items = list(business.bonanzas.annotate(n_spins=Count('spins'), n_pending=Count('spins', filter=Q(spins__payout__in=['pending', 'failed']))))
    for x in items:
        x.open_now = engine.is_open(x)
    return render(request, 'core/bonanza/list.html', {'items': items})


@login_required
def bonanza_edit(request, pk=None):
    business = _b(request)
    bz = get_object_or_404(business.bonanzas, pk=pk) if pk else None
    if request.method == 'POST':
        P = request.POST
        name = P.get('name', '').strip()[:120]
        if not name:
            messages.error(request, 'Give the Bonanza a name.')
            return redirect(request.path)
        if bz is None:
            bz = Bonanza(business=business, slug=_slug(), created_by=request.user)
        bz.name = name
        bz.headline = P.get('headline', '').strip()[:160]
        bz.description = P.get('description', '').strip()[:3000]
        bz.status = P.get('status') if P.get('status') in dict(Bonanza.STATUS) else 'draft'
        bz.starts_at, bz.ends_at = _dt(P.get('starts_at')), _dt(P.get('ends_at'))
        bz.spins_per_voucher = _int(P.get('spins_per_voucher'), 1, 1, 20)
        bz.require_sold = bool(P.get('require_sold'))
        bz.theme_color = (P.get('theme_color') or '#f59e0b')[:20]
        bz.save()
        bz.plans.set(business.plans.filter(pk__in=P.getlist('plans')))
        bz.batches.set(business.batches.filter(pk__in=P.getlist('batches')))
        bz.agents.set(business.agents.filter(pk__in=P.getlist('agents')))
        _save_prizes(request, bz)
        warn = []
        if not bz.plans.exists() and not bz.batches.exists():
            warn.append('Choose at least one plan or batch — until then no voucher can spin.')
        if not any(p.weight > 0 and p.kind != 'none' for p in bz.prizes.filter(active=True)):
            warn.append('Add at least one prize with a chance above 0.')
        for w in warn:
            messages.warning(request, w)
        messages.success(request, f'{bz.name} saved.')
        return redirect('bonanza_edit', pk=bz.pk)
    plans = business.plans.filter(active=True).order_by('price')
    ctx = {'bz': bz, 'plans': plans, 'batches': business.batches.order_by('-created_at')[:200], 'agents': business.agents.filter(active=True),
           'prizes': list(bz.prizes.all()) if bz else [], 'kinds': BonanzaPrize.KINDS, 'when_out': BonanzaPrize.WHEN_OUT,
           'palette': engine.PALETTE, 'statuses': Bonanza.STATUS,
           'sel_plans': set(bz.plans.values_list('pk', flat=True)) if bz else set(),
           'sel_batches': set(bz.batches.values_list('pk', flat=True)) if bz else set(),
           'sel_agents': set(bz.agents.values_list('pk', flat=True)) if bz else set(),
           'stats': engine.stats(bz) if bz else None,
           'public_url': request.build_absolute_uri(f'/b/{bz.slug}/') if bz else ''}
    return render(request, 'core/bonanza/editor.html', ctx)


def _save_prizes(request, bz):
    P = request.POST
    ids, labels = P.getlist('p_id'), P.getlist('p_label')
    business = bz.business
    keep = []
    for i, label in enumerate(labels):
        label = (label or '').strip()[:40]
        get = lambda k, d='': (P.getlist(k)[i] if i < len(P.getlist(k)) else d)
        pid = _int(get('p_id'), 0)
        if get('p_delete') == '1' or not label:
            continue
        p = bz.prizes.filter(pk=pid).first() if pid else None
        p = p or BonanzaPrize(bonanza=bz)
        p.label = label
        p.kind = get('p_kind') if get('p_kind') in dict(BonanzaPrize.KINDS) else 'none'
        p.plan = business.plans.filter(pk=_int(get('p_plan'), 0)).first() if p.kind == 'voucher' else None
        p.minutes = _int(get('p_minutes'), 0, 0, 527040) if p.kind == 'time' else 0
        p.amount = _dec(get('p_amount')) if p.kind == 'cash' else Decimal('0')
        p.details = get('p_details').strip()[:160]
        p.weight = _int(get('p_weight'), 0, 0, 1000000)
        q = get('p_quantity').strip()
        p.quantity = _int(q, None, 0) if q else None
        p.when_out = get('p_when_out') if get('p_when_out') in dict(BonanzaPrize.WHEN_OUT) else 'remove'
        p.color = get('p_color')[:20]
        p.active = get('p_active', '1') == '1'
        p.position = i
        p.save()
        keep.append(p.pk)
    # Prizes already won are kept (spins point to them) but switched off; unwon ones are removed.
    for p in bz.prizes.exclude(pk__in=keep):
        if p.won:
            p.active = False; p.save(update_fields=['active'])
        else:
            p.delete()


@login_required
@require_POST
def bonanza_status(request, pk):
    bz = get_object_or_404(_b(request).bonanzas, pk=pk)
    st = request.POST.get('status')
    if st in dict(Bonanza.STATUS):
        bz.status = st; bz.save(update_fields=['status'])
        messages.success(request, f'{bz.name} is now {bz.get_status_display().lower()}.')
    return redirect(request.POST.get('next') or 'bonanza_list')


@login_required
def bonanza_spins(request, pk):
    bz = get_object_or_404(_b(request).bonanzas, pk=pk)
    qs = bz.spins.select_related('voucher', 'reward_voucher', 'via_agent', 'paid_by', 'paid_by_agent')
    view = request.GET.get('view', 'all')
    q = request.GET.get('q', '').strip()
    if view == 'pending': qs = qs.filter(payout__in=['pending', 'failed'])
    elif view == 'winners': qs = qs.exclude(prize_kind='none')
    if q: qs = qs.filter(Q(voucher_code__icontains=q) | Q(claim_code__iexact=q.upper()) | Q(prize_label__icontains=q))
    return render(request, 'core/bonanza/spins.html', {'bz': bz, 'page_obj': Paginator(qs, 50).get_page(request.GET.get('page')),
                                                        'view': view, 'q': q, 'stats': engine.stats(bz), 'agents': bz.business.agents.filter(active=True)})


@login_required
@require_POST
def bonanza_payout(request, pk):
    s = get_object_or_404(BonanzaSpin.objects.select_related('bonanza'), pk=pk, bonanza__business=_b(request))
    agent = s.bonanza.business.agents.filter(pk=_int(request.POST.get('agent'), 0)).first()
    try:
        engine.mark_paid(s, request.user, agent, request.POST.get('note', ''))
        messages.success(request, f'Payout recorded: {s.prize_label} for {s.voucher_code}.')
    except engine.BonanzaError as e:
        messages.error(request, str(e))
    nxt = request.POST.get('next', '')
    if nxt.startswith('/') and not nxt.startswith('//'):
        return redirect(nxt)
    return redirect('bonanza_spins', pk=s.bonanza_id)


# ─────────────────────────────── public ───────────────────────────────

def _ip(request):
    return (request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR', ''))[:64]


def _throttled(key, limit, seconds):
    n = cache.get(key, 0)
    if n >= limit:
        return True
    cache.set(key, n + 1, seconds)
    return False


def bonanza_public(request, slug):
    bz = Bonanza.objects.select_related('business').filter(slug=slug).first()
    if not bz or bz.status == 'draft' and not (request.user.is_authenticated and getattr(request.user, 'business', None) == bz.business):
        raise Http404
    agent = bz.business.agents.filter(pk=_int(request.GET.get('a'), 0)).first()
    ctx = {'bz': bz, 'business': bz.business, 'wheel': json.dumps(engine.wheel_json(bz)), 'open': engine.is_open(bz),
           'closed_reason': engine.closed_reason(bz), 'agent': agent, 'chances': engine.chances(bz),
           'preview': bz.status == 'draft'}
    return render(request, 'core/bonanza/public.html', ctx)


@csrf_exempt
@require_POST
def bonanza_check(request, slug):
    bz = get_object_or_404(Bonanza, slug=slug)
    if _throttled(f'bzc:{_ip(request)}', 20, 60):
        return JsonResponse({'ok': False, 'message': 'Too many tries. Wait a minute.'}, status=429)
    try: data = json.loads(request.body or '{}')
    except ValueError: data = {}
    el = engine.eligibility(bz, data.get('code'))
    if not el.ok:
        return JsonResponse({'ok': False, 'message': el.reason})
    return JsonResponse({'ok': True, 'spins_left': el.spins_left, 'code': el.voucher.code})


@csrf_exempt
@require_POST
def bonanza_spin(request, slug):
    bz = get_object_or_404(Bonanza, slug=slug)
    if _throttled(f'bzs:{_ip(request)}', 10, 60):
        return JsonResponse({'ok': False, 'message': 'Too many spins. Wait a minute.'}, status=429)
    try: data = json.loads(request.body or '{}')
    except ValueError: data = {}
    agent = bz.business.agents.filter(pk=_int(data.get('a'), 0)).first()
    try:
        s = engine.spin(bz, data.get('code'), ip=_ip(request), via_agent=agent)
    except engine.BonanzaError as e:
        return JsonResponse({'ok': False, 'message': str(e)})
    s.refresh_from_db()
    el = engine.eligibility(bz, s.voucher_code)
    return JsonResponse({'ok': True, 'result': engine.result_json(s), 'spins_left': el.spins_left, 'wheel': engine.wheel_json(bz)})
