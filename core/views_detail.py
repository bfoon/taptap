"""Batch detail and plan detail pages.

Both pages show a summary and the vouchers split into:
  remaining  - not used yet and still valid (in stock, or sold but not used)
  in_use     - clock started and time still left
  expired    - time ran out (or marked expired)
  other      - disabled by someone, or frozen / warned
The same rules as the voucher page's state badge (voucher_history.display_state),
written as database filters so each list can be paged and searched.
Lists load 100 rows at a time; scrolling to the bottom fetches the next page.
"""
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, F, Max, Min, Q, Sum
from django.http import JsonResponse
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.template.loader import render_to_string
from django.utils import timezone

from . import voucher_history as vh

PAGE_SIZE = 100
TABS = [('remaining', 'Remaining'), ('in_use', 'In use'), ('expired', 'Expired'), ('other', 'Disabled / frozen')]


def state_filters(qs, now):
    """Q filters for each tab. Vouchers without a fixed expiry end at first use + their duration;
    a batch or plan only has a few different durations, so each gets its own simple condition."""
    ran_out = Q(status='expired') | Q(expires_at__lte=now)
    durations = (qs.filter(expires_at__isnull=True, used_at__isnull=False)
                 .values_list('duration_minutes', flat=True).distinct())
    for d in durations:
        if d:
            ran_out |= Q(expires_at__isnull=True, used_at__isnull=False, duration_minutes=d,
                         used_at__lte=now - timedelta(minutes=d))
    live = Q(frozen_at__isnull=True) & ~Q(status='disabled')
    return {
        'remaining': live & Q(used_at__isnull=True) & ~ran_out,
        'in_use': live & Q(used_at__isnull=False) & ~ran_out,
        'expired': live & ran_out,
        'other': Q(frozen_at__isnull=False) | Q(status='disabled'),
    }


def _search(qs, q):
    q = (q or '').strip()
    if not q:
        return qs
    return qs.filter(Q(code__icontains=q) | Q(customer_name__icontains=q) | Q(customer_phone__icontains=q)
                     | Q(agent__name__icontains=q) | Q(code_aliases__code__icontains=q)).distinct()


def _order(tab):
    # In-stock first, then sold-not-used; newest use first for the others.
    return {'remaining': [F('sold_at').asc(nulls_first=True), 'code'], 'in_use': [F('used_at').desc(nulls_last=True)],
            'expired': [F('used_at').desc(nulls_last=True), F('expires_at').desc(nulls_last=True)],
            'other': [F('frozen_at').desc(nulls_last=True), 'code']}[tab]


def _short(seconds):
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f'{d}d {h}h'
    if h:
        return f'{h}h {m}m'
    return f'{max(m, 1)}m'


def _rows(qs, tab, page, now):
    page_obj = Paginator(qs.select_related('agent', 'router').order_by(*_order(tab), 'pk'), PAGE_SIZE).get_page(page)
    rows = []
    for v in page_obj:
        key, label = vh.display_state(v, now)
        end = vh.ends_at(v)
        left = int((end - now).total_seconds()) if end and end > now else 0
        if v.frozen_at and v.frozen_left:
            left = v.frozen_left
        rows.append({'v': v, 'key': key, 'label': label, 'ends_at': end, 'left': _short(left) if left else ''})
    return page_obj, rows


def _voucher_lists(request, qs, context, template):
    """Full page, or (partial=1) the next page of one tab as JSON for the scroll loader."""
    now = timezone.now()
    filters = state_filters(qs, now)
    tab = request.GET.get('tab') if request.GET.get('tab') in filters else None
    q = request.GET.get('q', '')
    if request.GET.get('partial') and tab:
        page_obj, rows = _rows(_search(qs.filter(filters[tab]), q), tab, request.GET.get('page'), now)
        html = render_to_string('core/partials/voucher_rows.html', {'rows': rows, 'tab': tab}, request=request)
        return JsonResponse({'html': html, 'next': page_obj.next_page_number() if page_obj.has_next() else None,
                             'count': page_obj.paginator.count})
    counts = qs.aggregate(**{k: Count('pk', filter=f) for k, f in filters.items()}, total=Count('pk'),
                          sold=Count('pk', filter=Q(sold_at__isnull=False)),
                          sold_unused=Count('pk', filter=filters['remaining'] & Q(sold_at__isnull=False)),
                          value=Sum('price'), first_use=Min('used_at'), last_use=Max('used_at'))
    lists = []
    for key, label in TABS:
        if key == 'other' and not counts[key]:
            continue
        page_obj, rows = _rows(qs.filter(filters[key]), key, 1, now)
        lists.append({'key': key, 'label': label, 'count': counts[key], 'rows': rows,
                      'next': page_obj.next_page_number() if page_obj.has_next() else None})
    total = counts['total'] or 0
    context.update({'counts': counts, 'lists': lists, 'active_tab': tab or 'remaining',
                    'pct': {k: round(counts[k] * 100 / total, 1) if total else 0 for k, _ in TABS}})
    return render(request, template, context)


@login_required
def batch_detail(request, pk):
    business = request.user.business
    batch = get_object_or_404(business.batches.select_related('plan', 'agent'), pk=pk)
    qs = business.vouchers.filter(batch=batch, deleted_at__isnull=True)
    sales = business.sales.filter(voucher__batch=batch).aggregate(n=Count('pk'), v=Sum('amount'), c=Sum('commission'))
    routers = list(qs.exclude(router__isnull=True).values('router__name').annotate(n=Count('pk')).order_by('-n'))
    open_report = business.missing_voucher_reports.filter(batch=batch).exclude(status='resolved').order_by('-reported_at').first()
    from . import batch_health
    from .models import RouterHotspotProfile
    health = batch_health.check(batch, live=request.GET.get('check') == 'live')
    router_ids = {x['v'].router_id for x in health['vouchers'] if x['v'].router_id}
    profile_choices = sorted(set(RouterHotspotProfile.objects.filter(router_id__in=router_ids, is_present=True).values_list('name', flat=True)))
    return _voucher_lists(request, qs, {
        'batch': batch, 'sales': sales, 'routers': routers, 'open_report': open_report,
        'designs': business.voucher_designs.all(), 'health': health, 'profile_choices': profile_choices,
        'other_plans': business.plans.exclude(pk=batch.plan_id or 0).order_by('name'),
    }, 'core/batch_detail.html')


@login_required
@require_POST
def batch_health_action(request, pk):
    """Batch page: repair unused vouchers, or swap their profile (core/batch_health.py)."""
    from . import batch_health
    batch = get_object_or_404(request.user.business.batches, pk=pk)
    try:
        if request.POST.get('action') == 'swap':
            messages.success(request, batch_health.swap(batch, request.POST.get('target', ''), request.user))
        else:
            messages.success(request, batch_health.repair(batch, request.user))
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect('batch_detail', pk=pk)


@login_required
def plan_detail(request, pk):
    business = request.user.business
    plan = get_object_or_404(business.plans.select_related('imported_from_router'), pk=pk)
    qs = business.vouchers.filter(plan_name=plan.name, deleted_at__isnull=True)
    since = timezone.now() - timedelta(days=30)
    plan_sales = business.sales.filter(plan_name=plan.name)
    sales = plan_sales.aggregate(n=Count('pk'), v=Sum('amount'))
    sales_30 = plan_sales.filter(sold_at__gte=since).aggregate(n=Count('pk'), v=Sum('amount'))
    batches = list(business.batches.filter(plan=plan).select_related('agent').annotate(
        actual=Count('vouchers', filter=Q(vouchers__deleted_at__isnull=True)),
        unused=Count('vouchers', filter=Q(vouchers__deleted_at__isnull=True, vouchers__used_at__isnull=True, vouchers__status='active')),
        used=Count('vouchers', filter=Q(vouchers__deleted_at__isnull=True, vouchers__used_at__isnull=False)),
    ).order_by('-created_at'))
    return _voucher_lists(request, qs, {
        'plan': plan, 'sales': sales, 'sales_30': sales_30, 'batches': batches,
        'individual': qs.filter(batch__isnull=True).count(),
    }, 'core/plan_detail.html')
