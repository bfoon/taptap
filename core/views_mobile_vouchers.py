"""Vouchers in the TapTap mobile app — /api/mobile/v1/vouchers/… and /batches/.

Same rules as the website, through the same code, so the phone can never do more than the person may do on the web:

    GET  vouchers/                 list: state tabs, search (* wildcards), plan, batch      vouchers.view
    GET  vouchers/<id>/            one voucher: time, devices, sale, history, what you may do  vouchers.view
    POST vouchers/<id>/            disable · enable · add_time · reset_devices                vouchers.support
                                   delete (to the bin, with a reason)                         vouchers.manage
    GET  vouchers/new/             what the form needs: plans, routers, agents, code format   vouchers.create
    POST vouchers/new/             kind=batch  → core/voucher_make.make_batch (= website Generate)
                                   kind=single → one voucher for one customer (= website Single voucher)
    GET  batches/                  batches with how many are left                             vouchers.view

Actions go through core/voucher_history.py and core/voucher_bin.py — router updates, history and tracking alerts
happen exactly as when the buttons are pressed on the website. Members (username + password logins) are left out:
they have their own page on the website.
"""
import re

from django.db import transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.utils import timezone

from .views_mobile import _body, _business, _err, _perms, _signed_in, clean

PAGE = 40
CODES_SHOWN = 500
# The state tabs of the website's Vouchers page (core/views.py vouchers), in its order.
STATES = [('', 'All'), ('unsold', 'In stock'), ('sold', 'Sold'), ('used', 'Used'), ('frozen', 'Frozen'),
          ('disabled', 'Disabled / expired'), ('archived', 'Archived')]
EXTEND = [('30 min', 30), ('1 hour', 60), ('3 hours', 180), ('12 hours', 720), ('1 day', 1440), ('3 days', 4320),
          ('1 week', 10080), ('30 days', 43200)]


def _need(request, *perms):
    """The business, or a JSON refusal when the person's role has none of these permissions."""
    b = _business(request)
    if b is None:
        return None, _err('This login is not linked to a TapTap business.', 403)
    if not set(perms) & _perms(request):
        return None, _err('Your role does not allow this.', 403)
    return b, None


def _vouchers(b):
    return b.vouchers.exclude(login_type='member')


def _state_filter(qs, state):
    if state == 'unsold':
        return qs.filter(status='active', sold_at__isnull=True, used_at__isnull=True)
    if state == 'sold':
        return qs.filter(sold_at__isnull=False, used_at__isnull=True)
    if state == 'used':
        return qs.filter(used_at__isnull=False)
    if state == 'disabled':
        return qs.exclude(status__in=['active', 'archived']).filter(frozen_at__isnull=True)
    if state == 'archived':
        return qs.filter(status='archived')
    if state == 'frozen':
        return qs.filter(frozen_at__isnull=False)
    return qs


def _counts(qs):
    c = qs.aggregate(
        all=Count('id'), unsold=Count('id', filter=Q(status='active', sold_at__isnull=True, used_at__isnull=True)),
        sold=Count('id', filter=Q(sold_at__isnull=False, used_at__isnull=True)), used=Count('id', filter=Q(used_at__isnull=False)),
        frozen=Count('id', filter=Q(frozen_at__isnull=False)),
        disabled=Count('id', filter=~Q(status__in=['active', 'archived']) & Q(frozen_at__isnull=True)),
        archived=Count('id', filter=Q(status='archived')))
    return [{'key': k, 'label': label, 'count': c['all' if k == '' else k]} for k, label in STATES
            if k != 'archived' or c['archived']]


def _row(v, now):
    from . import voucher_history as vh
    key, label = vh.display_state(v, now)
    return {'id': v.pk, 'code': v.code, 'serial': v.serial, 'plan': v.plan_name, 'price': v.price, 'state': key,
            'state_label': label, 'ends_at': vh.ends_at(v), 'used_at': v.used_at, 'sold_at': v.sold_at, 'created_at': v.created_at,
            'batch': v.batch.name if v.batch_id and v.batch else '', 'batch_id': v.batch_id,
            'holder': v.agent.name if v.agent_id and v.agent else 'Shop', 'customer': v.customer_name or v.customer_phone,
            'router': v.router.name if v.router_id and v.router else ''}


@_signed_in
def vouchers(request):
    from .utils import code_search_q
    b, refused = _need(request, 'vouchers.view')
    if refused:
        return refused
    g = request.GET
    base = _vouchers(b)
    batch = None
    if (g.get('batch') or '').isdigit():
        batch = b.batches.filter(pk=int(g['batch'])).select_related('plan', 'agent').first()
        if batch is None:
            return _err('That batch is gone.', 404)
        base = base.filter(batch=batch)
    q = (g.get('q') or '').strip()[:60]
    if q:
        base = base.filter(code_search_q(q, 'code', ('batch__name', 'customer_name', 'customer_phone', 'serial'),
                                         also_codes=('code_aliases__code',))).distinct()
    plan = (g.get('plan') or '').strip()
    if plan:
        base = base.filter(plan_name=plan)
    state = g.get('state', '') if g.get('state', '') in dict(STATES) else ''
    qs = _state_filter(base, state).select_related('router', 'batch', 'agent').order_by('-created_at', '-id')
    try:
        page = max(1, int(g.get('page') or 1))
    except ValueError:
        page = 1
    rows = list(qs[(page - 1) * PAGE: page * PAGE + 1])
    now = timezone.now()
    out = {'ok': True, 'vouchers': [_row(v, now) for v in rows[:PAGE]], 'page': page, 'more': len(rows) > PAGE,
           'tabs': _counts(base), 'state': state, 'q': q, 'plan': plan,
           'plans': list(_vouchers(b).exclude(plan_name='').exclude(plan_name__startswith='*')
                         .values_list('plan_name', flat=True).distinct().order_by('plan_name')),
           'batch': _batch_row(b, batch) if batch else None}
    return JsonResponse(clean(out))


# ─────────────────────────────── one voucher ───────────────────────────────

def _detail(request, b, v):
    from . import voucher_history as vh
    from .models import VoucherSale
    perms = _perms(request)
    now = timezone.now()
    end = vh.ends_at(v)
    key, label = vh.display_state(v, now)
    support, manage = 'vouchers.support' in perms, 'vouchers.manage' in perms
    live = v.deleted_at is None and v.status != 'archived'
    still = live and not v.frozen_at
    expired = vh.time_is_up(v, now)
    sale = VoucherSale.objects.filter(voucher=v).select_related('agent').first()
    bindings = list(v.device_bindings.order_by('slot_no'))
    started = v.rolled_back_at or v.used_at
    pct = None
    if started and end and end > started:
        pct = max(0, min(100, round((now - started).total_seconds() * 100 / (end - started).total_seconds())))
    who = b.business_name
    share = (f'{who} Wi-Fi voucher: {v.code}\n{v.plan_name} · '
             + ('no time limit' if v.is_unlimited else f'{v.duration_text}, starts when you first connect')
             + (f' · {v.max_devices} devices' if (v.max_devices or 1) > 1 else ''))
    timeline = []
    for it in vh.timeline(v, now)[:40]:
        timeline.append({k: it.get(k) for k in ('kind', 'at', 'title', 'text', 'who', 'via', 'result', 'reason', 'tone')})
    return {
        **_row(v, now),
        'duration': v.duration_text, 'unlimited': v.is_unlimited, 'max_devices': max(1, v.max_devices or 1),
        'time_left': int((end - now).total_seconds()) if end and end > now else 0, 'time_pct': pct, 'expired': expired,
        'frozen': bool(v.frozen_at), 'freeze_reason': v.freeze_reason, 'note': v.note,
        'customer_name': v.customer_name, 'customer_phone': v.customer_phone,
        'router_sync': v.mikrotik_sync_status if v.router_id else '', 'router_error': v.mikrotik_sync_error[:200] if v.router_id else '',
        'channel': vh.channel(v.router),
        'devices': [{'slot': x.slot_no, 'mac': x.current_mac, 'label': x.label, 'since': x.first_bound_at, 'seen': x.last_seen_at}
                    for x in bindings],
        'sale': {'amount': sale.amount, 'method': sale.get_payment_method_display(), 'at': sale.sold_at,
                 'seller': sale.agent.name if sale.agent_id and sale.agent else 'Shop',
                 'customer': sale.customer_name or sale.customer_phone} if sale else None,
        'deleted': v.deleted_at is not None,
        'can': {
            'disable': support and still and v.status == 'active' and not expired,
            'enable': support and still and v.status != 'active' and not expired,
            'add_time': support and still and not v.is_unlimited,
            'reset_devices': support and live and bool(bindings),
            'delete': manage and v.deleted_at is None and not v.used_at,
        },
        'extend': [{'label': lb, 'minutes': m} for lb, m in EXTEND],
        'share': share,
        'timeline': timeline,
    }


def _voucher(b, pk):
    return _vouchers(b).select_related('router', 'batch', 'agent').filter(pk=pk).first()


@_signed_in
def voucher(request, pk):
    b, refused = _need(request, 'vouchers.view')
    if refused:
        return refused
    v = _voucher(b, pk)
    if v is None:
        return _err('That voucher is gone (deleted, or in another business).', 404)
    if request.method == 'POST':
        return _act(request, b, v)
    return JsonResponse(clean({'ok': True, 'voucher': _detail(request, b, v)}))


def _act(request, b, v):
    from . import voucher_history as vh
    from .utils import log
    d = _body(request)
    act = d.get('action')
    reason = str(d.get('reason') or '').strip()[:255]
    perms = _perms(request)
    user = request.user
    if act == 'delete':
        if 'vouchers.manage' not in perms:
            return _err('Your role cannot delete vouchers.', 403)
        from .voucher_bin import BinError, delete_vouchers
        try:
            s = delete_vouchers(b, [v], user=user, reason=reason)
        except BinError as e:
            return _err(str(e))
        if not s['deleted']:
            why = s['skipped'][0][1] if s['skipped'] else 'nothing changed'
            return _err(f'{v.code} was not deleted: {why}.')
        msg = f'{v.code} moved to the bin.'
        if s['sales_removed']:
            msg += f' Its sale ({b.currency}{s["sales_amount"]:,.2f}) was taken out of finance.'
        return JsonResponse({'ok': True, 'deleted': True, 'message': msg,
                             'notes': [text for _n, _ok, text in s['router']]})
    if act not in ('disable', 'enable', 'add_time', 'reset_devices'):
        return _err('Choose disable, enable, add_time, reset_devices or delete.')
    if 'vouchers.support' not in perms:
        return _err('Your role cannot change vouchers (voucher support is needed).', 403)
    try:
        if act == 'disable':
            ok, result = vh.disable(v, user, reason)
            log(b, 'Voucher Disabled', v.code)
            done = 'disabled'
        elif act == 'reset_devices':
            ok, result = vh.reset_devices(v, user, reason)
            log(b, 'Voucher MAC Reset', v.code)
            done = 'device binding reset'
        else:
            minutes = vh.parse_added_time(d.get('days'), d.get('hours'), d.get('minutes'))
            if act == 'add_time' and not minutes:
                return _err('Choose how much time to add.')
            ok, result = vh.enable(v, user, reason, None, add_minutes=minutes or None)
            log(b, 'Voucher Time Added' if minutes else 'Voucher Enabled', v.code + (f' +{vh._mtext(minutes)}' if minutes else ''))
            done = f'given {vh._mtext(minutes)} more' if minutes else 'enabled'
    except vh.VoucherActionError as e:
        return _err(str(e))
    if ok:
        msg = f'{v.code} {done}. {result}.' if result else f'{v.code} {done}.'
    else:
        msg = f'{v.code} {done} in TapTap, but the router was not updated: {result}'
    v = _voucher(b, v.pk)
    return JsonResponse(clean({'ok': True, 'message': msg, 'warning': not ok, 'voucher': _detail(request, b, v)}))


# ─────────────────────────────── batches ───────────────────────────────

def _batch_qs(b):
    return b.batches.select_related('plan', 'agent').annotate(
        actual=Count('vouchers', filter=Q(vouchers__deleted_at__isnull=True)),
        left=Count('vouchers', filter=Q(vouchers__sold_at__isnull=True, vouchers__used_at__isnull=True,
                                         vouchers__status='active', vouchers__deleted_at__isnull=True)),
        used=Count('vouchers', filter=Q(vouchers__used_at__isnull=False, vouchers__deleted_at__isnull=True)))


def _batch_row(b, x):
    if not hasattr(x, 'actual'):
        x = _batch_qs(b).filter(pk=x.pk).first()
    return {'id': x.pk, 'name': x.name, 'plan': x.plan.name if x.plan_id and x.plan else '', 'price': x.plan.price if x.plan_id and x.plan else 0,
            'count': x.actual, 'left': x.left, 'used': x.used, 'holder': x.agent.name if x.agent_id and x.agent else 'Shop',
            'settlement': x.get_settlement_display() if x.agent_id else '', 'created_at': x.created_at, 'by': x.created_by_label,
            'note': x.note, 'print_url': f'/studio/vouchers/print/?batch={x.pk}'}


@_signed_in
def batches(request):
    b, refused = _need(request, 'vouchers.view')
    if refused:
        return refused
    qs = _batch_qs(b).order_by('-created_at', '-id')
    q = (request.GET.get('q') or '').strip()[:60]
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(plan__name__icontains=q) | Q(agent__name__icontains=q))
    try:
        page = max(1, int(request.GET.get('page') or 1))
    except ValueError:
        page = 1
    rows = list(qs[(page - 1) * PAGE: page * PAGE + 1])
    return JsonResponse(clean({'ok': True, 'batches': [_batch_row(b, x) for x in rows[:PAGE]], 'page': page, 'more': len(rows) > PAGE}))


# ─────────────────────────────── making vouchers ───────────────────────────────

def _plans(b):
    return b.plans.filter(active=True).exclude(name__startswith='*').order_by('price', 'name')


def _options(b):
    from .utils import CODE_AFFIX_MAX, CODE_FORMATS, CODE_LENGTH_MAX, CODE_LENGTH_MIN, CODE_RANDOM_MIN, portal_code_length
    from .views import voucher_batch_max, voucher_confirm_above
    from .views_agents import MANUAL_METHODS
    return {
        'plans': [{'id': p.pk, 'name': p.name, 'price': p.price, 'free': p.is_free, 'duration': p.duration_text,
                   'devices': max(1, p.max_devices or 1), 'speed': p.speed_limit} for p in _plans(b)],
        'routers': [{'id': r.pk, 'name': r.name, 'status': r.status} for r in b.routers.all().order_by('name')],
        'agents': [{'id': a.pk, 'name': a.name} for a in b.agents.filter(active=True).order_by('name')],
        'methods': [{'key': k, 'label': v} for k, v in MANUAL_METHODS],
        'code': {'length': portal_code_length(b), 'min': CODE_LENGTH_MIN, 'max': CODE_LENGTH_MAX, 'affix_max': CODE_AFFIX_MAX,
                 'random_min': CODE_RANDOM_MIN, 'charsets': [{'key': k, 'label': v} for k, v in CODE_FORMATS]},
        'limits': {'confirm_above': voucher_confirm_above(), 'batch_max': voucher_batch_max(), 'shown': CODES_SHOWN},
        'currency': b.currency or 'D',
    }


def _fmt_input(d):
    """The code format fields from the app (capital letters only, like the website's default)."""
    return {'code_length': d.get('code_length') or None, 'code_charset': d.get('code_charset') or 'mixed',
            'code_prefix': str(d.get('code_prefix') or '').strip(), 'code_suffix': str(d.get('code_suffix') or '').strip(),
            'code_case': 'upper'}


@_signed_in
def new(request):
    b, refused = _need(request, 'vouchers.create')
    if refused:
        return refused
    if request.method != 'POST':
        return JsonResponse(clean({'ok': True, **_options(b)}))
    d = _body(request)
    plan = _plans(b).filter(pk=d.get('plan') or 0).first()
    if plan is None:
        return _err('Choose a plan.')
    router = b.routers.filter(pk=d.get('router') or 0).first() if d.get('router') else None
    agent = b.agents.filter(active=True, pk=d.get('agent') or 0).first() if d.get('agent') else None
    if d.get('kind') == 'single':
        return _single(request, b, plan, router, agent, d)
    return _batch(request, b, plan, router, agent, d)


def _batch(request, b, plan, router, agent, d):
    from .serials import SerialError
    from .utils import CodeFormatError, code_format_from_post
    from .views import voucher_batch_max, voucher_confirm_above
    from .voucher_make import make_batch
    try:
        qty = int(d.get('quantity') or 0)
    except (TypeError, ValueError):
        qty = 0
    if qty < 1:
        return _err('Type how many vouchers to make.')
    batch_max, confirm_above = voucher_batch_max(), voucher_confirm_above()
    if qty > batch_max:
        return _err(f'{qty:,} vouchers is more than one batch can hold ({batch_max:,}). Make it in several batches.')
    if qty > confirm_above and str(d.get('confirm_qty') or '').strip() != str(qty):
        value = plan.price * qty
        return JsonResponse(clean({'ok': False, 'confirm': True, 'quantity': qty, 'value': value,
                                   'message': f'You are about to create {qty:,} vouchers of {plan.name}, worth '
                                              f'{b.currency or "D"}{value:,.2f}. Confirm the amount to create them.'}), status=409)
    try:
        fmt = code_format_from_post(_fmt_input(d), b)
        batch, notes = make_batch(b, plan, qty, fmt, router=router, name=str(d.get('name') or ''), note=str(d.get('note') or ''),
                                  agent=agent, settlement=d.get('settlement'), pay_method=d.get('pay_method') or 'cash',
                                  reference=str(d.get('reference') or ''), user=request.user)
    except (CodeFormatError, SerialError) as e:
        return _err(str(e))
    codes = list(batch.vouchers.order_by('id').values('id', 'code', 'serial')[:CODES_SHOWN])
    return JsonResponse(clean({'ok': True, 'kind': 'batch', 'message': f'{qty} voucher(s) created.',
                               'notes': [{'level': lv, 'text': t} for lv, t in notes],
                               'batch': _batch_row(b, batch), 'codes': codes, 'more': max(0, qty - len(codes))}))


def _single(request, b, plan, router, agent, d):
    from . import serials
    from .finance import record_sale
    from .models import Voucher, VoucherCodeAlias
    from .utils import CodeFormatError, code_format_from_post, generate_code, log
    from .views_agents import CODE_RE, MANUAL_METHODS, push_one
    own = re.sub(r'[\s-]', '', str(d.get('code') or '')).upper()
    fmt = None
    if own:
        if not CODE_RE.fullmatch(own):
            return _err('Your own code must be 4–20 letters or numbers.')
        if Voucher.all_objects.filter(code__iexact=own).exists() or VoucherCodeAlias.objects.filter(code__iexact=own).exists():
            return _err(f'The code {own} is already taken (or was used by a deleted voucher). Try another, or leave it empty.')
    else:
        try:
            fmt = code_format_from_post(_fmt_input(d), b)
        except CodeFormatError as e:
            return _err(str(e))
    name = str(d.get('customer_name') or '').strip()[:120]
    phone = str(d.get('customer_phone') or '').strip()[:60]
    paid = bool(d.get('paid'))
    try:
        with transaction.atomic():
            code = own or generate_code(fmt['length'], fmt['charset'], fmt['prefix'], fmt['suffix'], b, fmt['case'])
            v = Voucher.objects.create(business=b, router=router, serial=serials.allocate(b, 1, plan=plan.name)[0], code=code,
                                       plan_name=plan.name, price=plan.price, duration_minutes=plan.duration_minutes,
                                       max_devices=plan.max_devices, source='taptap', agent=agent, customer_name=name,
                                       customer_phone=phone, note=str(d.get('note') or '')[:255])
            if paid:
                method = d.get('method') if d.get('method') in dict(MANUAL_METHODS) else 'cash'
                record_sale(b, v, method=method, agent=agent, customer_name=name, customer_phone=phone,
                            reference=str(d.get('reference') or '')[:120], user=request.user, notes='Single voucher (app)')
    except (CodeFormatError, serials.SerialError) as e:
        return _err(str(e))
    log(b, 'Voucher Created', f'{v.code} for {name or phone or "a customer"} ({plan.name}) · TapTap app')
    notes = []
    if router:
        pushed = push_one(v, plan)
        if pushed is True:
            notes.append({'level': 'info', 'text': f'{v.code} is live on {router.name} — the customer can connect now.'})
        else:
            notes.append({'level': 'warning', 'text': f'Saved, but {router.name} could not be updated right now ({pushed}). '
                                                      'It goes with the next router sync.'})
    try:
        from . import mobile_push
        mobile_push.vouchers_made(b, request.user, f'a voucher of {plan.name}',
                                  v.code + (f' · for {name or phone}' if (name or phone) else '') + (' · paid' if paid else ''), voucher=v)
    except Exception:
        pass
    v = _voucher(b, v.pk)
    return JsonResponse(clean({'ok': True, 'kind': 'single', 'message': f'Voucher {v.code} created.', 'notes': notes,
                               'voucher': _detail(request, b, v)}))
