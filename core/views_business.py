"""Finance & Reports views."""
import csv
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Sum, Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST

from .finance import (
    resolve_period, finance_summary, finance_charts, report_data, record_sale, sell_from_stock, AUTO,
    commission_for, PRESETS, CATEGORY_LABELS, METHOD_LABELS, d,
)
from .models import Agent, VoucherSale, Expense, CashCollection, EXPENSE_CATEGORIES, PAYMENT_METHODS
from .utils import log

MANUAL_METHODS = [m for m in PAYMENT_METHODS if m[0] != 'auto']


def _b(request): return request.user.business


def _dec(value, default='0'):
    try: return max(Decimal('0'), Decimal(str(value or default).replace(',', '').strip() or default))
    except (InvalidOperation, ValueError): return Decimal(default)


def _when(value):
    """Parse a yyyy-mm-dd (or datetime-local) form value; keep 'now' for today so ordering is natural."""
    if not value: return timezone.now()
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%d'):
        try:
            dt = datetime.strptime(value, fmt)
            if fmt == '%Y-%m-%d':
                if dt.date() == timezone.localdate(): return timezone.now()
                dt = dt.replace(hour=12)
            return timezone.make_aware(dt)
        except ValueError:
            continue
    return timezone.now()


def _back(request, tab):
    q = request.POST.get('return_query', '')
    base = f"/finance/?tab={tab}"
    return redirect(base + ('&' + q if q else ''))


# ───────────────────────────── Finance ─────────────────────────────
@login_required
def finance(request):
    business = _b(request)
    period = resolve_period(request.GET, 'month')
    tab = request.GET.get('tab', 'overview')
    summary = finance_summary(business, period)
    charts = finance_charts(business, period)

    sales = business.sales.select_related('agent', 'router', 'voucher').filter(sold_at__gte=period.start, sold_at__lt=period.end)
    expenses = business.expenses.select_related('router').filter(paid_at__gte=period.start, paid_at__lt=period.end)
    sq = request.GET.get('q', '').strip()
    if sq:
        sales = sales.filter(Q(voucher_code__icontains=sq) | Q(customer_name__icontains=sq) | Q(customer_phone__icontains=sq) | Q(reference__icontains=sq) | Q(plan_name__icontains=sq))
        expenses = expenses.filter(Q(description__icontains=sq) | Q(vendor__icontains=sq) | Q(reference__icontains=sq))
    if request.GET.get('method'):
        sales = sales.filter(payment_method=request.GET['method'])
    if request.GET.get('agent'):
        sales = sales.filter(agent_id=request.GET['agent'])
    if request.GET.get('category'):
        expenses = expenses.filter(category=request.GET['category'])

    stock = list(business.vouchers.filter(status='active', sold_at__isnull=True, used_at__isnull=True)
                 .values('plan_name').annotate(n=Count('id'), price=Sum('price')).order_by('plan_name'))
    plans = list(business.plans.filter(active=True).order_by('price'))
    stock_map = {s['plan_name']: s['n'] for s in stock}
    for p in plans: p.stock = stock_map.get(p.name, 0)

    pl_rows = [
        ('Voucher sales', summary['revenue'] + summary['discounts'], 'in'),
        ('Discounts given', -summary['discounts'], 'out'),
        ('Net sales', summary['revenue'], 'sub'),
        ('Agent commission', -summary['commission'], 'out'),
    ]
    by_cat = {r['category']: r['v'] for r in expenses.values('category').annotate(v=Sum('amount'))}
    for key, label in EXPENSE_CATEGORIES:
        if by_cat.get(key): pl_rows.append((label, -by_cat[key], 'out'))
    pl_rows.append(('Net profit', summary['profit'], 'total'))

    ctx = {
        'tab': tab, 'period': period, 'presets': PRESETS, 'summary': summary, 'charts': charts,
        'sales_page': Paginator(sales, 40).get_page(request.GET.get('sp')),
        'expenses_page': Paginator(expenses, 40).get_page(request.GET.get('ep')),
        'collections': business.collections.select_related('agent')[:30],
        'agents_all': business.agents.all(), 'plans': plans, 'stock': stock,
        'routers': business.routers.all().order_by('name'),
        'methods': MANUAL_METHODS, 'all_methods': PAYMENT_METHODS, 'categories': EXPENSE_CATEGORIES,
        'pl_rows': pl_rows, 'subs': business.subscriptions.order_by('-created_at')[:12],
        'today': timezone.localdate().isoformat(), 'q': sq, 'query': request.GET.urlencode(),
        'filters': {'method': request.GET.get('method', ''), 'agent': request.GET.get('agent', ''), 'category': request.GET.get('category', '')},
    }
    return render(request, 'core/finance.html', ctx)


@login_required
@require_POST
def finance_sale_add(request):
    business = _b(request)
    mode = request.POST.get('mode', 'stock')
    # '' = whoever holds the voucher (agent stock → that agent, shop stock → the shop); 'shop' = sold by the shop itself
    raw_agent = request.POST.get('agent', '')
    agent = None if raw_agent == 'shop' else (business.agents.filter(pk=raw_agent).first() if raw_agent.isdigit() else AUTO)
    common = dict(method=request.POST.get('method', 'cash'), agent=agent, customer_name=request.POST.get('customer_name', '')[:120],
                  customer_phone=request.POST.get('customer_phone', '')[:60], reference=request.POST.get('reference', '')[:120],
                  notes=request.POST.get('notes', '')[:255], discount=_dec(request.POST.get('discount')), user=request.user,
                  when=_when(request.POST.get('sold_at')))
    if common['method'] not in dict(PAYMENT_METHODS): common['method'] = 'cash'
    sales = []
    if mode == 'code':
        codes = [c.strip() for c in request.POST.get('codes', '').replace(',', '\n').splitlines() if c.strip()]
        missing = []
        for c in codes:
            v = business.vouchers.filter(code__iexact=c).first()
            if not v: missing.append(c); continue
            s = record_sale(business, v, **common)
            if s: sales.append(s)
            else: messages.warning(request, f'{v.code} was already sold.')
        if missing: messages.error(request, f'Not found: {", ".join(missing[:10])}')
    elif mode == 'manual':
        amount = _dec(request.POST.get('amount'))
        if amount <= 0:
            messages.error(request, 'Enter an amount above zero.'); return _back(request, 'sales')
        if common['agent'] is AUTO: common['agent'] = None
        sales = [record_sale(business, None, plan_name=request.POST.get('plan_name', 'Walk-in')[:120] or 'Walk-in', amount=amount, **common)]
    else:
        plan = business.plans.filter(pk=request.POST.get('plan') or 0).first()
        qty = max(1, min(500, int(request.POST.get('quantity') or 1)))
        if not plan:
            messages.error(request, 'Choose a plan to sell.'); return _back(request, 'sales')
        sales = sell_from_stock(business, plan.name, qty, **common)
        if len(sales) < qty:
            messages.warning(request, f'Only {len(sales)} unsold {plan.name} voucher(s) were in stock. Generate more to sell the rest.')
    if sales:
        total = sum((s.amount for s in sales), Decimal('0'))
        log(business, 'Sale Recorded', f'{len(sales)} voucher(s) · {business.currency}{total}')
        codes = ', '.join(s.voucher_code for s in sales if s.voucher_code)[:200]
        messages.success(request, f'Recorded {len(sales)} sale(s) worth {business.currency}{total:,.2f}.' + (f' Codes: {codes}' if codes else ''))
        if request.POST.get('print') and any(s.voucher_id for s in sales):
            ids = ','.join(str(s.voucher_id) for s in sales if s.voucher_id)
            return redirect(f'/studio/vouchers/print/?ids={ids}')
    nxt = request.POST.get('next')
    return redirect(nxt) if nxt and nxt.startswith('/') else _back(request, 'sales')


@login_required
@require_POST
def finance_sale_delete(request, pk):
    business = _b(request); s = get_object_or_404(business.sales, pk=pk)
    if s.voucher_id: business.vouchers.filter(pk=s.voucher_id).update(sold_at=None)
    log(business, 'Sale Voided', f'{s.plan_name} {s.voucher_code} {business.currency}{s.amount}')
    s.delete(); messages.success(request, 'Sale voided. The voucher is back in stock.')
    return _back(request, 'sales')


@login_required
@require_POST
def finance_expense_add(request):
    business = _b(request)
    amount = _dec(request.POST.get('amount'))
    desc = request.POST.get('description', '').strip()[:200]
    if amount <= 0 or not desc:
        messages.error(request, 'An expense needs a description and an amount above zero.'); return _back(request, 'expenses')
    cat = request.POST.get('category', 'other'); cat = cat if cat in dict(EXPENSE_CATEGORIES) else 'other'
    Expense.objects.create(business=business, category=cat, description=desc, vendor=request.POST.get('vendor', '')[:120], amount=amount,
                           payment_method=request.POST.get('method', 'cash'), router=business.routers.filter(pk=request.POST.get('router') or 0).first(),
                           reference=request.POST.get('reference', '')[:120], recurring=bool(request.POST.get('recurring')),
                           paid_at=_when(request.POST.get('paid_at')), recorded_by=request.user)
    log(business, 'Expense Recorded', f'{CATEGORY_LABELS.get(cat)}: {desc} {business.currency}{amount}')
    messages.success(request, f'Expense of {business.currency}{amount:,.2f} recorded.')
    return _back(request, 'expenses')


@login_required
@require_POST
def finance_expense_delete(request, pk):
    get_object_or_404(_b(request).expenses, pk=pk).delete(); messages.success(request, 'Expense deleted.')
    return _back(request, 'expenses')


@login_required
@require_POST
def finance_expense_repeat(request):
    """Copy last month's recurring expenses into this month in one click."""
    business = _b(request); today = timezone.localdate(); m0 = today.replace(day=1); lm = (m0 - timedelta(days=1)).replace(day=1)
    start = timezone.make_aware(datetime.combine(lm, datetime.min.time())); end = timezone.make_aware(datetime.combine(m0, datetime.min.time()))
    made = 0
    for e in business.expenses.filter(recurring=True, paid_at__gte=start, paid_at__lt=end):
        if business.expenses.filter(recurring=True, description=e.description, paid_at__gte=end).exists(): continue
        Expense.objects.create(business=business, category=e.category, description=e.description, vendor=e.vendor, amount=e.amount,
                               payment_method=e.payment_method, router=e.router, recurring=True, paid_at=timezone.now(), recorded_by=request.user)
        made += 1
    messages.success(request, f'{made} recurring expense(s) copied into {m0:%B}.' if made else 'Nothing to copy — this month already has last month’s recurring expenses.')
    return _back(request, 'expenses')


@login_required
@require_POST
def finance_agent_save(request):
    business = _b(request)
    agent = business.agents.filter(pk=request.POST.get('id') or 0).first() or Agent(business=business)
    name = request.POST.get('name', '').strip()
    if not name:
        messages.error(request, 'Give the agent a name.'); return _back(request, 'agents')
    agent.name = name[:120]; agent.phone = request.POST.get('phone', '')[:60]; agent.location = request.POST.get('location', '')[:160]
    agent.commission_percent = min(Decimal('100'), _dec(request.POST.get('commission_percent'), '10'))
    agent.active = request.POST.get('active', '1') == '1'; agent.save()
    messages.success(request, f'Agent {agent.name} saved.')
    return _back(request, 'agents')


@login_required
@require_POST
def finance_collection_add(request):
    business = _b(request); agent = get_object_or_404(business.agents, pk=request.POST.get('agent'))
    amount = _dec(request.POST.get('amount'))
    if amount <= 0:
        messages.error(request, 'Enter the amount collected.'); return _back(request, 'agents')
    CashCollection.objects.create(business=business, agent=agent, amount=amount, payment_method=request.POST.get('method', 'cash'),
                                  reference=request.POST.get('reference', '')[:120], note=request.POST.get('note', '')[:255],
                                  collected_at=_when(request.POST.get('collected_at')), recorded_by=request.user)
    log(business, 'Cash Collected', f'{agent.name}: {business.currency}{amount}')
    messages.success(request, f'Collected {business.currency}{amount:,.2f} from {agent.name}.')
    nxt = request.POST.get('next', '')
    return redirect(nxt) if nxt.startswith('/') else _back(request, 'agents')


@login_required
@require_POST
def finance_settings(request):
    business = _b(request)
    business.monthly_revenue_target = _dec(request.POST.get('monthly_revenue_target'))
    business.auto_record_sales = bool(request.POST.get('auto_record_sales'))
    business.save(update_fields=['monthly_revenue_target', 'auto_record_sales'])
    messages.success(request, 'Finance settings saved.')
    return _back(request, 'overview')


@login_required
def finance_export(request):
    business = _b(request); period = resolve_period(request.GET, 'month'); kind = request.GET.get('type', 'sales')
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = f'attachment; filename="taptap-{kind}-{period.as_dict()["start"]}-to-{period.as_dict()["end"]}.csv"'
    w = csv.writer(resp)
    if kind == 'expenses':
        w.writerow(['Date', 'Category', 'Description', 'Vendor', 'Site', 'Method', 'Reference', 'Recurring', 'Amount'])
        for e in business.expenses.select_related('router').filter(paid_at__gte=period.start, paid_at__lt=period.end):
            w.writerow([timezone.localtime(e.paid_at).strftime('%Y-%m-%d %H:%M'), e.get_category_display(), e.description, e.vendor,
                        e.router.name if e.router else '', e.get_payment_method_display(), e.reference, 'yes' if e.recurring else '', e.amount])
    elif kind == 'pnl':
        s = finance_summary(business, period)
        w.writerow(['TapTap profit & loss', business.business_name, period.label])
        w.writerow(['Net sales', s['revenue']]); w.writerow(['Discounts', s['discounts']]); w.writerow(['Agent commission', s['commission']])
        for r in business.expenses.filter(paid_at__gte=period.start, paid_at__lt=period.end).values('category').annotate(v=Sum('amount')):
            w.writerow([CATEGORY_LABELS.get(r['category']), r['v']])
        w.writerow(['Total expenses', s['expenses']]); w.writerow(['Net profit', s['profit']]); w.writerow(['Margin %', s['margin']])
    elif kind == 'agents':
        from .finance import agent_balances
        w.writerow(['Agent', 'Phone', 'Commission %', 'Vouchers sold', 'Gross sales', 'Commission', 'Owed', 'Collected', 'Outstanding'])
        for r in agent_balances(business):
            a = r['agent']; w.writerow([a.name, a.phone, a.commission_percent, r['sold'], r['gross'], r['commission'], r['owed'], r['collected'], r['outstanding']])
    else:
        w.writerow(['Date', 'Voucher', 'Plan', 'Site', 'Agent', 'Customer', 'Phone', 'Method', 'Reference', 'Discount', 'Commission', 'Amount'])
        for s in business.sales.select_related('agent', 'router').filter(sold_at__gte=period.start, sold_at__lt=period.end):
            w.writerow([timezone.localtime(s.sold_at).strftime('%Y-%m-%d %H:%M'), s.voucher_code, s.plan_name, s.router.name if s.router else '',
                        s.agent.name if s.agent else '', s.customer_name, s.customer_phone, s.get_payment_method_display(), s.reference,
                        s.discount, s.commission, s.amount])
    return resp


# ───────────────────────────── Reports ─────────────────────────────
@login_required
def reports(request):
    business = _b(request)
    return render(request, 'core/reports.html', {
        'presets': PRESETS, 'routers': business.routers.all().order_by('name'),
        'plans': business.vouchers.values_list('plan_name', flat=True).distinct().order_by('plan_name'),
        'initial': {'range': request.GET.get('range', '30d'), 'start': request.GET.get('start', ''), 'end': request.GET.get('end', ''),
                    'router': request.GET.get('router', ''), 'plan': request.GET.get('plan', '')},
    })


@login_required
def reports_data(request):
    business = _b(request); period = resolve_period(request.GET, '30d')
    router_id = request.GET.get('router') or None
    if router_id and not business.routers.filter(pk=router_id).exists(): router_id = None
    return JsonResponse(report_data(business, period, router_id, request.GET.get('plan') or None))


@login_required
def reports_export(request):
    business = _b(request); period = resolve_period(request.GET, '30d')
    data = report_data(business, period, request.GET.get('router') or None, request.GET.get('plan') or None)
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = f'attachment; filename="taptap-report-{data["period"]["start"]}-to-{data["period"]["end"]}.csv"'
    w = csv.writer(resp)
    w.writerow(['TapTap report', business.business_name, period.label]); w.writerow([])
    w.writerow(['Metric', 'Value', 'Change vs previous period %'])
    for k, label in [('revenue', 'Revenue'), ('sold', 'Vouchers sold'), ('generated', 'Vouchers generated'), ('activated', 'Vouchers activated'), ('avg_ticket', 'Average sale'), ('sell_through', 'Sell-through %')]:
        w.writerow([label, round(data['kpis'][k]['value'], 2), data['kpis'][k]['change'] if data['kpis'][k]['change'] is not None else '']); 
    w.writerow([]); w.writerow(['Plan', 'Generated', 'Sold', 'Activated', 'Revenue', 'Share %'])
    for p in data['plans']: w.writerow([p['plan'], p['generated'], p['sold'], p['activated'], p['revenue'], p['share']])
    w.writerow([]); w.writerow(['Period', 'Revenue', 'Sold', 'Generated', 'Activated'])
    t = data['trend']
    for i, lbl in enumerate(t['labels']): w.writerow([lbl, t['revenue'][i], int(t['sold'][i]), int(t['generated'][i]), int(t['activated'][i])])
    return resp
