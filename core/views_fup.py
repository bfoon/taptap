"""Fair usage (data speed steps) — managed from the Security Center."""
import json
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import fair_usage as fu
from .models_fup import COUNTS, PERIODS, FairUsagePolicy

PRESETS = [
    ('Gentle daily', 'day', [{'gb': 3, 'down': 5, 'up': 2}, {'gb': 6, 'down': 2, 'up': 1}]),
    ('Strict daily', 'day', [{'gb': 1, 'down': 3, 'up': 1}, {'gb': 2, 'down': 1, 'up': .5}, {'gb': 4, 'down': .256, 'up': .128}]),
    ('Weekly bundle', 'week', [{'gb': 10, 'down': 4, 'up': 2}, {'gb': 20, 'down': 1, 'up': .5}]),
    ('Per voucher', 'voucher', [{'gb': 5, 'down': 3, 'up': 1}, {'gb': 10, 'down': 1, 'up': .5}]),
]


def _b(request):
    return request.user.business


def _hour(v):
    v = str(v or '').strip()
    return int(v) if v.isdigit() and 0 <= int(v) <= 23 else None


@login_required
def fup_edit(request, pk=None):
    business = _b(request)
    pol = get_object_or_404(business.fair_usage_policies, pk=pk) if pk else None
    if request.method == 'POST':
        try:
            tiers = fu.clean_tiers(json.loads(request.POST.get('tiers_json') or '[]'))
        except ValueError:
            tiers = []
        name = request.POST.get('name', '').strip()[:80]
        if not name or not tiers:
            messages.error(request, 'Give the policy a name and at least one step (after how many GB, and the speed).')
        else:
            pol = pol or FairUsagePolicy(business=business, created_by=request.user)
            pol.name, pol.tiers = name, tiers
            pol.active = request.POST.get('active') == '1'
            pol.period = request.POST.get('period') if request.POST.get('period') in dict(PERIODS) else 'day'
            pol.counts = request.POST.get('counts') if request.POST.get('counts') in dict(COUNTS) else 'total'
            if request.POST.get('free_hours') == '1':
                pol.free_from, pol.free_to = _hour(request.POST.get('free_from')), _hour(request.POST.get('free_to'))
            else:
                pol.free_from = pol.free_to = None
            pol.save()
            pol.plans.set(business.plans.filter(pk__in=[int(x) for x in request.POST.getlist('plans') if x.isdigit()]))
            from .utils import log
            log(business, 'Fair Usage', f'Policy {pol.name} saved ({len(tiers)} step(s), {pol.get_period_display().lower()})')
            messages.success(request, f'{pol.name} saved. It applies from the next live sync (within a minute or two).')
            return redirect('/security/#fair-usage')
    taken = {}
    for other in business.fair_usage_policies.exclude(pk=pol.pk if pol else None).prefetch_related('plans'):
        for p in other.plans.all():
            taken[p.pk] = other.name
    plans = list(business.plans.order_by('name'))
    for p in plans:
        p.taken_by = taken.get(p.pk, '')
    return render(request, 'core/fair_usage_edit.html', {
        'pol': pol, 'plans': plans, 'chosen': set(pol.plans.values_list('pk', flat=True)) if pol else set(),
        'taken': taken, 'periods': PERIODS, 'counts': COUNTS, 'hours': range(24),
        'tiers_json': json.dumps(pol.tiers if pol else PRESETS[0][2]),
        'presets_json': json.dumps([{'name': n, 'period': per, 'tiers': t} for n, per, t in PRESETS]),
    })


@login_required
@require_POST
def fup_action(request, pk):
    business = _b(request)
    pol = get_object_or_404(business.fair_usage_policies, pk=pk)
    action = request.POST.get('action')
    if action == 'toggle':
        pol.active = not pol.active
        pol.save(update_fields=['active'])
        messages.success(request, f'{pol.name} is {"on" if pol.active else "off — customers go back to full speed at the next sync"}.')
    elif action == 'delete':
        name = pol.name
        pol.delete()
        messages.success(request, f'{name} deleted. Customers it slowed go back to full speed at the next sync.')
    return redirect('/security/#fair-usage')


@login_required
@require_POST
def fup_lift(request, pk):
    """Full speed for one voucher until the period resets — or put the limits back."""
    v = get_object_or_404(_b(request).vouchers, pk=pk)
    if request.POST.get('action') == 'restore':
        fu.unlift(v, request.user)
        messages.success(request, f'Fair usage limits are back on for {v.code}.')
    else:
        until = fu.lift(v, request.user)
        if until:
            messages.success(request, f'{v.code} has full speed until {timezone.localtime(until):%d %b %H:%M} (applied at the next sync).')
        else:
            messages.info(request, 'No fair usage policy covers this voucher.')
    return redirect('voucher_detail', pk=v.pk)


def security_context(business):
    """Cards for the Security Center."""
    recent = timezone.now() - timedelta(minutes=30)
    pols = list(business.fair_usage_policies.prefetch_related('plans').annotate(
        slowed=Count('states', filter=Q(states__tier__gt=0, states__updated_at__gte=recent), distinct=True)))
    for p in pols:
        p.steps = [f'{t["gb"]:g} GB → {fu.speed_text(t["down"])}' for t in p.tiers]
    return {'fup_policies': pols}
