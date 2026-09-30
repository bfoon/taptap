"""Members page: customers who log in with a username and password (see core/members.py)."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import members as mem
from . import voucher_history as vh
from .utils import log
from .views_agents import MANUAL_METHODS, push_one


def _b(request):
    return request.user.business


def _back(request, fallback='members'):
    nxt = request.POST.get('next', '')
    return redirect(nxt if nxt.startswith('/') and not nxt.startswith('//') else fallback)


@login_required
def members(request):
    business = _b(request)
    plans = business.plans.filter(active=True).order_by('is_free', 'price', 'name')
    if request.method == 'POST':
        perms = getattr(request, 'tt_perms', None)
        if perms is not None and 'vouchers.create' not in perms:
            messages.error(request, 'Your role cannot create members.')
            return redirect('members')
        f = request.POST
        router = business.routers.filter(pk=f.get('router') or 0).first()
        agent = business.agents.filter(pk=f.get('agent') or 0).first()
        method = f.get('method') if f.get('method') in dict(MANUAL_METHODS) else 'cash'
        try:
            v = mem.create_member(
                business, username=f.get('username'), password=f.get('password', ''), same=bool(f.get('same')),
                plan_value=f.get('plan'), devices=f.get('devices'), rate_limit=f.get('rate_limit'), router=router,
                agent=agent, customer_name=f.get('customer_name'), customer_phone=f.get('customer_phone'),
                note=f.get('note'), paid=bool(f.get('paid')), method=method, reference=f.get('reference'), user=request.user)
        except mem.MemberError as exc:
            messages.error(request, str(exc))
            return render(request, 'core/members.html', _ctx(request, business, plans, form=f), status=400)
        log(business, 'Member Created', f'{v.code} ({v.plan_name})' + (f' for {v.customer_name}' if v.customer_name else ''))
        if router:
            plan = business.plans.filter(name=v.plan_name).first()
            pushed = push_one(v, plan)
            if pushed is True:
                messages.success(request, f'Member {v.code} is live on {router.name} — they can log in now with their username and password.')
            else:
                messages.warning(request, f'Member {v.code} saved, but {router.name} could not be updated right now ({pushed}). '
                                          'TapTap sends it at the next router sync.')
        else:
            messages.success(request, f'Member {v.code} created. Choose a router so they can log in.')
        return redirect(f"{request.path}?created={v.pk}")
    return render(request, 'core/members.html', _ctx(request, business, plans))


def _ctx(request, business, plans, form=None):
    q = (request.GET.get('q') or '').strip()
    kind = request.GET.get('kind', '')
    qs = business.vouchers.filter(login_type='member').select_related('router', 'agent').order_by('-created_at')
    if q:
        qs = qs.filter(Q(code__icontains=q) | Q(customer_name__icontains=q) | Q(customer_phone__icontains=q))
    free_q = Q(duration_minutes=0, expires_at__isnull=True, price=0)
    if kind == 'free':
        qs = qs.filter(free_q)
    elif kind == 'paid':
        qs = qs.exclude(free_q)
    page = Paginator(qs, 50).get_page(request.GET.get('p'))
    now = timezone.now()
    rows = []
    for v in page:
        key, label = vh.display_state(v, now)
        label = {'stock': 'Not logged in yet', 'sold': 'Paid, not used yet'}.get(key, label)   # member wording
        end = vh.ends_at(v)
        rows.append({'v': v, 'key': key, 'label': label, 'ends_at': end, 'kind': mem.kind_of(v),
                     'left': (end - now) if end and end > now else None})
    created = business.vouchers.filter(login_type='member', pk=request.GET.get('created') or 0).first() \
        if str(request.GET.get('created') or '').isdigit() else None
    return {'plans': plans, 'routers': business.routers.all(), 'agents': business.agents.filter(active=True),
            'methods': MANUAL_METHODS, 'rows': rows, 'page': page, 'q': q, 'kind': kind,
            'stats': mem.member_stats(business), 'form': form or {'plan': 'free', 'same': '', 'devices': '1'},
            'created': created, 'free_plan_name': mem.FREE_PLAN_NAME,
            'default_router': business.routers.first()}


@login_required
@require_POST
def member_password(request, pk):
    business = _b(request)
    v = get_object_or_404(business.vouchers.filter(login_type='member'), pk=pk)
    try:
        ok, result = mem.change_password(v, request.POST.get('password', ''), same=bool(request.POST.get('same')),
                                         user=request.user, reason=request.POST.get('reason', ''))
    except mem.MemberError as exc:
        messages.error(request, str(exc))
        return _back(request)
    (messages.success if ok else messages.warning)(request, f'Password of {v.code} changed. {result}.')
    return _back(request)


@login_required
@require_POST
def member_renew(request, pk):
    business = _b(request)
    v = get_object_or_404(business.vouchers.filter(login_type='member'), pk=pk)
    method = request.POST.get('method') if request.POST.get('method') in dict(MANUAL_METHODS) else 'cash'
    agent = business.agents.filter(pk=request.POST.get('agent') or 0).first()
    try:
        ok, result, sale, minutes = mem.renew(v, amount=request.POST.get('amount'), method=method,
                                              reference=request.POST.get('reference', ''), agent=agent, user=request.user)
    except (mem.MemberError, vh.VoucherActionError) as exc:
        messages.error(request, str(exc))
        return _back(request)
    from .durations import text
    paid = f' Payment of {business.currency}{sale.amount:,.2f} recorded.' if sale else ''
    (messages.success if ok else messages.warning)(request, f'{v.code} renewed: +{text(minutes)}.{paid} {result}.')
    return _back(request)
