"""Agent-owned batches, agent statements, and one-off vouchers for individual customers."""
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .finance import agent_balances, assign_batch, record_sale, PAYMENT_METHODS, d
from .mikrotik import MikroTikService
from .models import Voucher, VoucherBatch
from .durations import router_limit, to_minutes
from .utils import generate_code, log, voucher_profile, code_format_ctx, code_format_from_post, CodeFormatError
from .views_studio import duration_text, business_ctx, _safe_json, voucher_print_rows


def _on_link(router):
    """True when this router must be handled through TapTap Link right now
    (enrolled in Link and its TapTap Tunnel is not healthy)."""
    from .linkops import uses_link
    return uses_link(router)


CODE_RE = re.compile(r'^[A-Z0-9]{4,20}$')
MANUAL_METHODS = [m for m in PAYMENT_METHODS if m[0] != 'auto']


def _b(request):
    return request.user.business


def _dec(v, default='0'):
    try:
        return max(Decimal('0'), Decimal(str(v or default).replace(',', '').strip() or default))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def wa_number(phone):
    """Digits for a wa.me link. Gambian local numbers (7 digits) get the 220 country code."""
    digits = re.sub(r'\D', '', phone or '')
    if len(digits) == 7:
        digits = '220' + digits
    return digits


# ───────────────────────────── batches ↔ agents ─────────────────────────────
@login_required
@require_POST
def batch_assign(request, pk):
    business = _b(request); batch = get_object_or_404(business.batches, pk=pk)
    agent = business.agents.filter(pk=request.POST.get('agent') or 0).first()
    settlement = request.POST.get('settlement') if request.POST.get('settlement') in {'credit', 'prepaid'} else 'credit'
    method = request.POST.get('method', 'cash') if request.POST.get('method') in dict(MANUAL_METHODS) else 'cash'
    previous = batch.agent
    moved, sales = assign_batch(batch, agent, settlement, method, request.user, request.POST.get('reference', '')[:120])
    if agent:
        msg = f'{batch.name}: {moved} unsold voucher{"s" if moved != 1 else ""} now held by {agent.name}'
        if sales:
            total = sum((s.amount for s in sales), Decimal('0')); comm = sum((s.commission for s in sales), Decimal('0'))
            msg += f'. Bought upfront: {business.currency}{total:,.2f} in sales, {business.currency}{comm:,.2f} commission, payment recorded.'
        messages.success(request, msg)
        log(business, 'Batch Issued', f'{batch.name} → {agent.name} ({batch.get_settlement_display()})')
    else:
        messages.success(request, f'{batch.name}: {moved} unsold voucher{"s" if moved != 1 else ""} returned to the shop' + (f' from {previous.name}' if previous else '') + '.')
        log(business, 'Batch Returned', f'{batch.name} ← {previous.name if previous else "shop"}')
    nxt = request.POST.get('next', '')
    return redirect(nxt if nxt.startswith('/') else 'batches')


@login_required
def agent_detail(request, pk):
    business = _b(request); agent = get_object_or_404(business.agents, pk=pk)
    bal = next((r for r in agent_balances(business) if r['agent'].id == agent.id), None)
    batches = (agent.batches.select_related('plan').annotate(
        total=Count('vouchers',filter=Q(vouchers__deleted_at__isnull=True)), sold=Count('vouchers',filter=Q(vouchers__sold_at__isnull=False,vouchers__deleted_at__isnull=True)),
        used=Count('vouchers',filter=Q(vouchers__used_at__isnull=False,vouchers__deleted_at__isnull=True)),
        left=Count('vouchers',filter=Q(vouchers__sold_at__isnull=True, vouchers__used_at__isnull=True, vouchers__status='active',vouchers__deleted_at__isnull=True)))
        .order_by('-issued_at', '-created_at'))
    holding = (business.vouchers.filter(agent=agent, status='active', sold_at__isnull=True, used_at__isnull=True)
               .values('plan_name').annotate(n=Count('id'), v=Sum('price')).order_by('plan_name'))
    agent.ensure_portal_token()
    portal_url = request.build_absolute_uri(f'/ag/{agent.portal_token}/')
    return render(request, 'core/agent_detail.html', {
        'portal_url': portal_url,
        'all_plans': business.plans.filter(active=True).exclude(name__startswith='*').order_by('price', 'name'),
        'order_plan_ids': set(agent.order_plans.values_list('pk', flat=True)),
        'orders': agent.orders.select_related('plan', 'batch', 'done_by')[:30],
        'helps': agent.help_requests.select_related('voucher', 'handled_by')[:30],
        'checks': agent.checks.select_related('voucher')[:60],
        'open_orders': agent.orders.filter(status='new').count(), 'open_helps': agent.help_requests.filter(handled_at__isnull=True).count(),
        'agent': agent, 'bal': bal, 'batches': batches, 'holding': holding,
        'sales': agent.sales.select_related('router')[:40], 'collections': agent.collections.all()[:30],
        'shop_batches': business.batches.filter(agent__isnull=True).select_related('plan').annotate(
            left=Count('vouchers',filter=Q(vouchers__sold_at__isnull=True, vouchers__used_at__isnull=True, vouchers__status='active',vouchers__deleted_at__isnull=True))).filter(left__gt=0).order_by('-created_at')[:30],
        'methods': MANUAL_METHODS, 'today': timezone.localdate().isoformat(), 'now': timezone.now(),
    })


# ───────────────────────────── one voucher for one person ─────────────────────────────
@login_required
def single_voucher(request):
    business = _b(request)
    plans = business.plans.filter(active=True).order_by('price')
    ctx = {'plans': plans, 'routers': business.routers.all(), 'agents': business.agents.filter(active=True), 'methods': MANUAL_METHODS}
    ctx['recent'] = business.vouchers.filter(batch__isnull=True, source='taptap').filter(Q(customer_name__gt='') | Q(customer_phone__gt='')).order_by('-created_at')[:8]
    if request.method != 'POST':
        ctx['form'] = {'plan': request.GET.get('plan', ''), 'mode': 'plan', 'paid': '1'}
        ctx['cf'] = code_format_ctx(business); ctx['code_mode'] = 'random'
        return render(request, 'core/single_voucher.html', ctx)
    f = request.POST; errors = []
    mode = 'custom' if f.get('mode') == 'custom' else 'plan'
    plan = None
    if mode == 'plan':
        plan = plans.filter(pk=f.get('plan') or 0).first()
        if not plan: errors.append('Choose a plan, or switch to a custom voucher.')
        price = _dec(f.get('price'), str(plan.price if plan else 0)) if f.get('price') not in (None, '') else (plan.price if plan else Decimal('0'))
        minutes = plan.duration_minutes if plan else 1440; devices = plan.max_devices if plan else 1; plan_name = plan.name if plan else ''; rate = ''
    else:
        # Old forms sent h/d/w; new ones send minutes/hours/days/months.
        unit = {'h': 'hours', 'd': 'days', 'w': 'weeks'}.get(f.get('duration_unit'), f.get('duration_unit') or 'days')
        try:
            if unit == 'weeks':
                minutes = to_minutes(int(f.get('duration_value') or 1) * 7, 'days')
            else:
                minutes = to_minutes(f.get('duration_value') or 1, unit)
        except ValueError as exc:
            minutes = 1440; errors.append(str(exc))
        try: devices = max(1, min(20, int(f.get('devices') or 1)))
        except ValueError: devices = 1
        price = _dec(f.get('price')); rate = (f.get('rate_limit') or '').strip()[:50]
        if rate and not re.fullmatch(r'\d+[kKmM]?(/\d+[kKmM]?)?', rate):
            errors.append('Speed looks wrong — use a form like 5M/5M or 2M.')
        plan_name = (f.get('custom_name') or '').strip()[:80] or f'Custom {duration_text(minutes)}'
    code_mode = f.get('code_mode') or ('own' if (f.get('code') or '').strip() else 'random')
    code = re.sub(r'[\s-]', '', (f.get('code') or '')).upper() if code_mode == 'own' else ''
    fmt = None
    if code_mode == 'own' and not code:
        errors.append('Type your own code, or switch to a random code.')
    elif code_mode == 'random':
        try: fmt = code_format_from_post(f, business)
        except CodeFormatError as exc: errors.append(str(exc))
    if code:
        if not CODE_RE.fullmatch(code): errors.append('A custom code must be 4–20 letters or numbers.')
        elif Voucher.all_objects.filter(code__iexact=code).exists() or business.voucher_code_aliases.model.objects.filter(code__iexact=code).exists(): errors.append(f'The code {code} is already taken (or was used by a deleted voucher). Try another, or leave it blank for a random one.')
    name = (f.get('customer_name') or '').strip()[:120]; phone = (f.get('customer_phone') or '').strip()[:60]
    router = business.routers.filter(pk=f.get('router') or 0).first()
    agent = business.agents.filter(pk=f.get('agent') or 0).first()
    if errors:
        for e in errors: messages.error(request, e)
        ctx['form'] = f; ctx['cf'] = code_format_ctx(business, f); ctx['code_mode'] = code_mode
        return render(request, 'core/single_voucher.html', ctx, status=400)
    with transaction.atomic():
        from .serials import allocate as _serials
        v = Voucher.objects.create(business=business, router=router, serial=_serials(business, 1, plan=plan_name)[0], code=code or generate_code(fmt['length'], fmt['charset'], fmt['prefix'], fmt['suffix'], business), plan_name=plan_name, price=price,
                                   duration_minutes=minutes, max_devices=devices, rate_limit=rate, source='taptap', agent=agent,
                                   customer_name=name, customer_phone=phone, note=(f.get('note') or '')[:255])
        if f.get('paid'):
            method = f.get('method') if f.get('method') in dict(MANUAL_METHODS) else 'cash'
            record_sale(business, v, method=method, agent=agent, customer_name=name, customer_phone=phone,
                        reference=(f.get('reference') or '')[:120], user=request.user, notes='Single voucher')
    log(business, 'Voucher Created', f'{v.code} for {name or phone or "a customer"} ({plan_name})')
    pushed = None
    if router:
        pushed = push_one(v, plan)
        if pushed is True:
            messages.success(request, f'{v.code} is live on {router.name} — the customer can connect now.')
        else:
            messages.warning(request, f'Voucher saved, but {router.name} could not be updated right now ({pushed}). It will be sent on the next router sync.')
    else:
        messages.success(request, f'Voucher {v.code} created.')
    return redirect('voucher_card', pk=v.pk)


def push_one(voucher, plan=None):
    """Put one voucher on its router straight away instead of waiting for a full sync."""
    if voucher.router and _on_link(voucher.router):
        from .agent import push_pending_vouchers
        Voucher.objects.filter(pk=voucher.pk).update(mikrotik_sync_status='Pending', mikrotik_sync_error='')
        push_pending_vouchers(voucher.router)
        return True  # delivered at the router's next check-in (a few seconds)
    try:
        svc = MikroTikService(voucher.router).connect()
        try:
            profile, shared, rate = voucher_profile(voucher, plan)
            svc.ensure_hotspot_profile(profile, shared, rate)
            kind = 'member' if voucher.is_member else 'voucher'
            _, item_id = svc.upsert_voucher(voucher.code, profile, limit_uptime=router_limit(voucher), password=voucher.login_password,
                                            disabled=(voucher.status != 'active'),
                                            comment=f'TapTap {kind} {voucher.code}' + (f' · {voucher.customer_name}' if voucher.customer_name else ''))
        finally:
            svc.close()
        Voucher.objects.filter(pk=voucher.pk).update(mikrotik_id=str(item_id or ''), mikrotik_sync_status='Synced', mikrotik_sync_error='')
        return True
    except Exception as exc:
        Voucher.objects.filter(pk=voucher.pk).update(mikrotik_sync_status='Pending', mikrotik_sync_error=str(exc)[:500])
        return str(exc)[:160]


@login_required
def voucher_card(request, pk):
    business = _b(request); v = get_object_or_404(business.vouchers.select_related('router', 'agent', 'batch'), pk=pk)
    biz = business_ctx(business)
    # Only split long random codes into readable groups; a code the owner chose (e.g. AWA2026) stays whole.
    grouped = ' '.join(v.code[i:i + 4] for i in range(0, len(v.code), 4)) if len(v.code) % 4 == 0 and len(v.code) >= 8 else v.code
    brand = biz['name'] if re.search(r'wi-?fi', biz['name'], re.I) else f'{biz["name"]} Wi-Fi'
    lines = [f'Hi {v.customer_name.split()[0]},' if v.customer_name else 'Hello,',
             f'your {brand} voucher is ready.', '', f'Code: {grouped}',
             f'Valid for: {duration_text(v.duration_minutes)} · {v.max_devices} device{"s" if v.max_devices != 1 else ""}',
             f'Connect to the Wi-Fi “{biz["ssid"]}”, open any website and type the code.']
    if biz['login_url']:
        u = biz['login_url'] if biz['login_url'].startswith('http') else 'http://' + biz['login_url']
        u = u.rstrip('/') + ('' if u.rstrip('/').endswith('/login') else '/login')
        lines.append(f'Quick login: {u}?username={v.code}&password={v.code}')
    if biz['phone']:
        lines.append(f'Help: {biz["phone"]}')
    text = '\n'.join(lines)
    design = business.voucher_designs.filter(is_default=True).first()
    from .studio_presets import voucher_template
    return render(request, 'core/voucher_card.html', {
        'v': v, 'grouped': grouped, 'share_text': text, 'wa': wa_number(v.customer_phone),
        'wa_text': quote(text), 'sms_text': quote(text), 'duration': duration_text(v.duration_minutes),
        'sale': v.sale if hasattr(v, 'sale') else None,
        'config_json': _safe_json(design.config if design else voucher_template('classic')),
        'row_json': _safe_json(voucher_print_rows(business, business.vouchers.filter(pk=v.pk))[0]), 'business_json': _safe_json(biz),
        'methods': MANUAL_METHODS, 'agents': business.agents.filter(active=True),
    })


# ───────────────────────────── agent cash-flow statement ─────────────────────────────
@login_required
def agent_statement(request, pk):
    """Printable cash-flow statement of one agent for a period (default: this month)."""
    from .finance import PRESETS, agent_statement as build, resolve_period
    business = _b(request); agent = get_object_or_404(business.agents, pk=pk)
    period = resolve_period(request.GET, default='month')
    st = build(business, agent, period)
    by = request.user.get_full_name() or request.user.email or request.user.username
    return render(request, 'core/agent_statement.html', {
        'agent': agent, 'st': st, 'period': period, 'presets': PRESETS, 'generated_at': timezone.now(), 'generated_by': by,
    })


# ───────────────────────────── batch receipt ─────────────────────────────
@login_required
def batch_receipt(request, pk):
    """Printable receipt for a generated batch: A4 (office copy + agent stub) or 80 mm thermal."""
    from django.urls import reverse
    from .finance import batch_receipt as build
    business = _b(request)
    batch = get_object_or_404(business.batches.select_related('plan', 'agent'), pk=pk)
    rc = build(batch)
    perms = getattr(request, 'tt_perms', None)
    can_codes = perms is None or 'vouchers.create' in perms
    codes = list(batch.vouchers.order_by('id').values_list('serial', 'code')) if can_codes and request.GET.get('codes') == '1' else []
    nxt = request.GET.get('next', '')
    return render(request, 'core/batch_receipt.html', {
        'batch': batch, 'rc': rc, 'codes': codes, 'can_codes': can_codes,
        'fmt': 'thermal' if request.GET.get('format') == 'thermal' else 'a4',
        'autoprint': request.GET.get('autoprint') == '1',
        'verify_url': request.build_absolute_uri(reverse('batch_detail', args=[batch.pk])),
        'next_url': nxt if nxt.startswith('/') and not nxt.startswith('//') else '',
        'printed_by': request.user.get_full_name() or request.user.email or request.user.username,
    })



@login_required
@require_POST
def agent_portal_rotate(request, pk):
    """New secret link / QR for an agent (the old QR stops working)."""
    business = _b(request); agent = get_object_or_404(business.agents, pk=pk)
    agent.ensure_portal_token(rotate=True)
    log(business, 'Agent QR Renewed', f'{agent.name}: new voucher-checker link; the old QR no longer works')
    messages.success(request, f'New QR code for {agent.name}. The old one no longer works — print and give them the new one.')
    return redirect('agent_detail', pk=pk)



@login_required
@require_POST
def agent_portal_phone(request, pk):
    """The number the voucher checker tells customers to call for this agent (empty = customer help line)."""
    business = _b(request); agent = get_object_or_404(business.agents, pk=pk)
    agent.customer_phone = re.sub(r'[^0-9+ ()-]', '', request.POST.get('customer_phone', ''))[:60].strip()
    agent.save(update_fields=['customer_phone'])
    messages.success(request, f'Customers checked by {agent.name} are told to call {agent.help_number() or "— (no number set)"}.')
    return redirect('agent_detail', pk=pk)



@login_required
@require_POST
def agent_log_action(request, pk):
    """Decline an agent's order, or mark a help request as handled."""
    business = _b(request); agent = get_object_or_404(business.agents, pk=pk)
    act, oid = request.POST.get('action'), request.POST.get('id', '')
    if act == 'order_decline' and oid.isdigit():
        agent.orders.filter(pk=int(oid), status='new').update(status='rejected', done_at=timezone.now(), done_by=request.user)
        messages.info(request, 'Order declined — the agent sees it on their phone.')
    elif act == 'help_done' and oid.isdigit():
        agent.help_requests.filter(pk=int(oid), handled_at__isnull=True).update(handled_at=timezone.now(), handled_by=request.user,
                                                                               note=request.POST.get('note', '')[:255])
        messages.success(request, 'Help request marked as handled.')
    from django.urls import reverse
    return redirect(f"{reverse('agent_detail', args=[pk])}#agent-logs")



@login_required
@require_POST
def agent_order_plans(request, pk):
    """Which plans this agent may order on the voucher checker (none ticked = every active plan)."""
    business = _b(request); agent = get_object_or_404(business.agents, pk=pk)
    ids = [int(x) for x in request.POST.getlist('plans') if str(x).isdigit()]
    agent.order_plans.set(business.plans.filter(pk__in=ids))
    q = request.POST.get('order_quantity', '')
    if q.isdigit():
        agent.order_quantity = max(1, min(500, int(q)))
    agent.order_quantity_fixed = request.POST.get('order_quantity_fixed') == 'on'
    agent.save(update_fields=['order_quantity', 'order_quantity_fixed'])
    names = list(agent.order_plans.values_list('name', flat=True))
    messages.success(request, f'{agent.name} can order: ' + (', '.join(names) if names else 'every active plan')
                     + f' — {agent.order_quantity} voucher(s) per order' + (' (fixed).' if agent.order_quantity_fixed else ' to start with.'))
    return redirect('agent_detail', pk=pk)
