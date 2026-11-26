"""Pages of agents and resellers, and the Wi-Fi owner's "Agent network" page (rules in core/partners.py)."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import partners as pt
from .models_partners import AgentLink, AgentProgram, AgentRetail, AgentTopUp


def _me(request):
    return getattr(request, 'tt_business', None) or request.user.business


def _agent_only(request):
    b = _me(request)
    if pt.kind_of(b) != 'agent':
        raise Http404
    return b


def _ctx(request, **kw):
    b = _me(request)
    return {'me': b, 'standing': pt.standing(b), 'kind': pt.kind_of(b), 'profile': getattr(b, 'partner_profile', None), **kw}


# ─────────────────────────────── home ───────────────────────────────

@login_required
def home(request):
    b = _me(request)
    if not pt.is_partner(b):
        return redirect('dashboard')
    pt.sweep()
    if pt.kind_of(b) == 'reseller':
        return _reseller_home(request, b)
    links = list(AgentLink.objects.filter(agent_account=b).select_related('owner').order_by('-status', 'owner__business_name'))
    topups = list(AgentTopUp.objects.filter(link__agent_account=b).select_related('owner', 'batch').order_by('-created_at')[:30])
    waiting = [t for t in topups if t.status in AgentTopUp.OPEN]
    ready = [t for t in topups if t.status == 'issued'][:6]
    for t in ready:
        t.stock = pt.batch_stock(t)
    summary = pt.agent_summary(b)
    nearby = pt.directory(b)[:4] if not any(l.status == 'active' for l in links) else []
    steps = [
        ('Find Wi-Fi near you', any(True for _ in links), reverse('partner_find')),
        ('An owner accepts you', any(l.status == 'active' for l in links), reverse('partner_find')),
        ('Ask for your first top-up', any(True for _ in topups), reverse('partner_find')),
        ('Pay and get your batch', any(t.status == 'issued' for t in topups), reverse('partner_vouchers')),
    ]
    return render(request, 'core/partner/agent_home.html', _ctx(
        request, links=links, waiting=waiting, ready=ready, summary=summary, nearby=nearby, steps=steps,
        steps_done=sum(1 for s in steps if s[1])))


def _reseller_home(request, b):
    from .market import available
    from .models_market import MarketDeal, MarketOrder, MarketShop
    deals = list(MarketDeal.objects.filter(buyer=b).select_related('listing', 'seller').order_by('-created_at')[:20])
    active = [d for d in deals if d.status == 'active']
    for d in active:
        d.left = available(d)
    earned = MarketOrder.objects.filter(deal__buyer=b, status='issued').aggregate(v=Sum('amount'), n=Count('id'))
    shop = MarketShop.objects.filter(business=b).first()
    waiting = MarketOrder.objects.filter(deal__buyer=b, status__in=('claimed', 'review')).count()
    from .market import wallets
    has_wallet = wallets(b).exists()
    steps = [('Add the wallet customers pay', has_wallet, reverse('market') + '?tab=setup'),
             ('Buy your first batch', bool(deals), reverse('market')),
             ('Share your shop link', bool(shop), reverse('market') + '?tab=setup'),
             ('First customer pays you', bool(earned['n']), reverse('market') + '?tab=stock')]
    return render(request, 'core/partner/reseller_home.html', _ctx(
        request, deals=deals, active=active, earned=earned, shop=shop, waiting=waiting, steps=steps,
        steps_done=sum(1 for s in steps if s[1]),
        shop_url=request.build_absolute_uri(reverse('market_shop', args=[shop.slug])) if shop else '',
        spent=sum((d.price for d in deals if d.status in MarketDeal.OWNED), Decimal('0'))))


# ─────────────────────────────── agents: finding Wi-Fi, applying, topping up ───────────────────────────────

@login_required
def find(request):
    b = _agent_only(request)
    q = (request.GET.get('q') or '').strip()[:80]
    rows = pt.directory(b, q)
    return render(request, 'core/partner/find.html', _ctx(request, rows=rows, q=q))


@login_required
def owner_page(request, owner_id):
    b = _agent_only(request)
    from .models import Business
    owner = get_object_or_404(Business, pk=owner_id, account_kind='wifi')
    prog = pt.program(owner, create=False)
    link = AgentLink.objects.filter(agent_account=b, owner=owner).select_related('agent').first()
    if not link and (not prog or not prog.open):
        raise Http404
    plans = []
    if prog:
        for p in pt.program_plans(prog):
            cost = (pt.unit_cost(p.price, agent=link.agent) if link and link.agent_id
                    else pt.unit_cost(p.price, link.discount_percent if link and link.status == 'active' else prog.discount_percent))
            plans.append({'plan': p, 'cost': cost, 'profit': p.price - cost})
    topups = list(link.topups.select_related('batch').order_by('-created_at')[:20]) if link else []
    checker = ''
    if link and link.status == 'active' and link.agent_id:
        checker = request.build_absolute_uri(reverse('agent_portal', args=[link.agent.ensure_portal_token()]))
    return render(request, 'core/partner/owner.html', _ctx(
        request, owner=owner, prog=prog, link=link, plans=plans, topups=topups, checker=checker,
        wallets=_wallet_labels(owner), online=owner.routers.filter(status='Online').count(), routers=owner.routers.count()))


def _wallet_labels(owner):
    from .market import wallets
    out = [w.get_provider_display() for w in wallets(owner)]
    prog = pt.program(owner, create=False)
    if prog and prog.take_cash:
        out.append('Cash')
    return out


@login_required
@require_POST
def apply(request, owner_id):
    b = _agent_only(request)
    from .models import Business
    owner = get_object_or_404(Business, pk=owner_id, account_kind='wifi')
    try:
        link = pt.apply(b, owner, request.POST.get('message', ''))
        messages.success(request, f'Application sent to {owner.business_name}. You will get an email when they answer.'
                         if link.status == 'pending' else f'You already sell for {owner.business_name}.')
    except pt.PartnerError as e:
        messages.error(request, str(e))
        if 'trial' in str(e) or 'month' in str(e):
            return redirect('partner_billing')
    return redirect('partner_owner', owner_id=owner.pk)


@login_required
@require_POST
def leave(request, owner_id):
    b = _agent_only(request)
    link = get_object_or_404(AgentLink, agent_account=b, owner_id=owner_id)
    try:
        pt.leave(link.pk, b)
        messages.success(request, f'You stopped selling for {link.owner.business_name}. Vouchers you bought keep working.')
    except pt.PartnerError as e:
        messages.error(request, str(e))
    return redirect('partner_owner', owner_id=owner_id)


@login_required
@require_POST
def topup_new(request, owner_id):
    b = _agent_only(request)
    link = get_object_or_404(AgentLink, agent_account=b, owner_id=owner_id)
    try:
        t = pt.request_topup(link, request.POST.get('plan'), request.POST.get('amount'), request.POST.get('note', ''))
    except pt.PartnerError as e:
        messages.error(request, str(e))
        if 'trial' in str(e) or 'month' in str(e):
            return redirect('partner_billing')
        return redirect('partner_owner', owner_id=owner_id)
    messages.success(request, f'Top-up {t.reference} sent to {link.owner.business_name}.' if t.status == 'requested'
                     else f'Top-up {t.reference} approved at once — pay to get your batch.')
    return redirect('partner_topup', reference=t.reference)


@login_required
def topup_page(request, reference):
    b = _agent_only(request)
    pt.sweep()
    t = get_object_or_404(AgentTopUp.objects.select_related('owner', 'link', 'plan', 'batch'), reference=reference, link__agent_account=b)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'wave':
                return HttpResponseRedirect(pt.checkout(t.reference, b, request))
            if action == 'claim':
                pt.claim(t.reference, b, request.POST.get('method', ''), request.POST.get('payment_ref', ''))
                messages.success(request, 'Thank you. The owner confirms the payment and your batch is made at once.')
            elif action == 'cancel':
                pt.cancel(t.reference, b)
                messages.success(request, 'Top-up cancelled.')
        except pt.PartnerError as e:
            messages.error(request, str(e))
        return redirect('partner_topup', reference=t.reference)
    if request.GET.get('payment') == 'error':
        messages.warning(request, 'The Wave payment did not go through. You can try again.')
    options = pt.payment_options(t) if t.status == 'approved' else []
    timeline = [('Asked', t.created_at, True), ('Owner approved', t.approved_at, bool(t.approved_at)),
                ('You paid', t.claimed_at or t.paid_at, bool(t.claimed_at or t.paid_at)), ('Batch ready', t.issued_at, bool(t.issued_at))]
    return render(request, 'core/partner/topup.html', _ctx(request, t=t, options=options, timeline=timeline,
                                                           pay_until=t.approved_at + timedelta(hours=pt.PAY_HOURS) if t.approved_at else None))


@login_required
def vouchers(request):
    b = _agent_only(request)
    items = list(AgentTopUp.objects.filter(link__agent_account=b, status='issued').select_related('owner', 'batch').order_by('-issued_at'))
    for t in items:
        t.stock = pt.batch_stock(t)
    return render(request, 'core/partner/vouchers.html', _ctx(request, items=items, summary=pt.agent_summary(b)))


@login_required
def batch(request, reference):
    b = _agent_only(request)
    t = get_object_or_404(AgentTopUp.objects.select_related('owner', 'batch', 'link'), reference=reference, link__agent_account=b, status='issued')
    if request.method == 'POST':
        v = t.batch.vouchers.filter(pk=request.POST.get('voucher') or 0).first()
        if v is None:
            raise Http404
        if request.POST.get('action') == 'unsold':
            AgentRetail.objects.filter(voucher=v, agent_account=b).delete()
            messages.info(request, f'{v.code} is back in your stock.')
        else:
            try:
                price = pt.dec(request.POST.get('price') or t.face_price, 'price')
            except pt.PartnerError:
                price = t.face_price
            AgentRetail.objects.update_or_create(voucher=v, defaults={'agent_account': b, 'price': price,
                                                                      'customer': (request.POST.get('customer') or '').strip()[:120]})
            messages.success(request, f'{v.code} marked as sold for {pt.money(t.owner, price)}.')
        return redirect(reverse('partner_batch', args=[t.reference]) + (f'?show={request.GET.get("show")}' if request.GET.get('show') else ''))
    show = request.GET.get('show') or 'left'
    qs = t.batch.vouchers.select_related('agent_retail').order_by('serial', 'pk')
    if show == 'left':
        qs = qs.filter(used_at__isnull=True, agent_retail__isnull=True)
    elif show == 'sold':
        qs = qs.filter(Q(used_at__isnull=False) | Q(agent_retail__isnull=False))
    owner = t.owner
    login_url = owner.hotspot_url or (f'http://{owner.hotspot_dns_name}/login' if owner.hotspot_dns_name else '')
    retail = AgentRetail.objects.filter(voucher__batch=t.batch).aggregate(v=Sum('price'), n=Count('id'))
    tpl = 'core/partner/print.html' if request.GET.get('print') else 'core/partner/batch.html'
    return render(request, tpl, _ctx(request, t=t, codes=list(qs[:1000]), show=show, stock=pt.batch_stock(t), owner=owner,
                                     login_url=login_url, retail=retail))


# ─────────────────────────────── both: fee, profile, help ───────────────────────────────

@login_required
def billing(request):
    b = _me(request)
    if not pt.is_partner(b):
        return redirect('subscription')
    if request.method == 'POST':
        try:
            sub = pt.request_month(b, request.POST.get('months') or 1)
            messages.success(request, f'Request sent: {sub.plan} for {pt.money(b, sub.amount)}. TapTap switches it on as soon as your payment arrives.')
        except pt.PartnerError as e:
            messages.error(request, str(e))
        return redirect('partner_billing')
    fee = pt.monthly_fee(pt.kind_of(b))
    options = [(1, '1 month', fee), (3, '3 months', fee * 3), (12, '1 year', fee * 12)]
    return render(request, 'core/partner/billing.html', _ctx(request, options=options, history=b.subscriptions.order_by('-created_at')[:12],
                                                             pay_to=getattr(settings, 'PARTNER_PAY_TO', '')))


@login_required
def profile(request):
    b = _me(request)
    if not pt.is_partner(b):
        return redirect('settings')
    from .models_partners import PartnerProfile
    prof, _ = PartnerProfile.objects.get_or_create(business=b)
    if request.method == 'POST':
        name = (request.POST.get('business_name') or '').strip()[:180]
        phone = (request.POST.get('phone') or '').strip()[:60]
        if name:
            b.business_name = name
        if phone:
            b.phone = phone
        b.save(update_fields=['business_name', 'phone'])
        prof.area = (request.POST.get('area') or '').strip()[:120]
        prof.about = (request.POST.get('about') or '').strip()[:240]
        prof.whatsapp = (request.POST.get('whatsapp') or '').strip()[:60]
        prof.save()
        messages.success(request, 'Profile saved.')
        return redirect('partner_profile')
    return render(request, 'core/partner/profile.html', _ctx(request, prof=prof))


@login_required
def help_page(request):
    _me(request)
    return render(request, 'core/partner/help.html', _ctx(request))


# ─────────────────────────────── a Wi-Fi owner's "join my agent network" link ───────────────────────────────

def join(request, code):
    prog = AgentProgram.objects.filter(invite_code=str(code).upper()[:12]).select_related('business').first()
    if not prog or not prog.open:
        messages.info(request, 'This invitation is not open any more.')
        return redirect('home')
    if not request.user.is_authenticated:
        request.session['tt_join_code'] = prog.invite_code
        return redirect(reverse('register') + '?as=agent')
    me = _me(request)
    if pt.kind_of(me) == 'agent':
        return redirect('partner_owner', owner_id=prog.business_id)
    messages.info(request, 'Invitations are for agent accounts. Sign out and create an agent account to join.')
    return redirect('partner_home' if pt.is_partner(me) else 'dashboard')


# ─────────────────────────────── the Wi-Fi owner's side ───────────────────────────────

def _owner_ok(request):
    return getattr(request, 'tt_role', None) == 'owner' and not getattr(request, 'tt_view_as', None)


@login_required
def network(request):
    owner = _me(request)
    if pt.is_partner(owner):
        return redirect('partner_home')
    pt.sweep()
    prog = pt.program(owner)
    if request.method == 'POST':
        _owner_post(request, owner, prog)
        cache.delete(f'agentnet:waiting:{owner.pk}')
        return redirect(reverse('agent_network') + (request.POST.get('back') or ''))
    links = list(AgentLink.objects.filter(owner=owner).select_related('agent_account', 'agent_account__partner_profile', 'agent')
                 .annotate(bought=Sum('topups__amount', filter=Q(topups__status='issued')),
                           batches=Count('topups', filter=Q(topups__status='issued'))))
    topups = list(AgentTopUp.objects.filter(owner=owner).select_related('link__agent_account', 'router', 'batch')
                  .order_by('-created_at')[:60])
    for t in topups:
        if t.status == 'requested' and t.plan_id:
            try:
                t.q = pt.quote(t.link, t.plan, t.requested_amount)
            except pt.PartnerError:
                t.q = None
    month = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    stats = AgentTopUp.objects.filter(owner=owner, status='issued', issued_at__gte=month).aggregate(v=Sum('amount'), n=Count('id'), q=Sum('quantity'))
    from .market import wallets
    from .voucher_plan_lock import open_plans
    requests_ = [t for t in topups if t.status == 'requested']
    confirm_ = [t for t in topups if t.status in ('claimed', 'review')]
    return render(request, 'core/agent_network.html', {
        'prog': prog, 'needs': len(requests_) + len(confirm_), 'links': links, 'pending': [l for l in links if l.status == 'pending'],
        'active': [l for l in links if l.status in ('active', 'paused')],
        'requests': [t for t in topups if t.status == 'requested'], 'to_confirm': [t for t in topups if t.status in ('claimed', 'review')],
        'awaiting': [t for t in topups if t.status == 'approved'], 'history': [t for t in topups if t.status not in AgentTopUp.OPEN][:25],
        'stats': stats, 'wallets': list(wallets(owner)), 'routers': owner.routers.order_by('name'),
        'all_plans': open_plans(owner.plans.filter(active=True, price__gt=0).exclude(name__startswith='*')).order_by('price'),
        'chosen': set(prog.plans.values_list('pk', flat=True)), 'is_owner': _owner_ok(request),
        'join_url': request.build_absolute_uri(reverse('agent_join', args=[prog.invite_code])),
        'example': pt._example(prog, pt.program_plans(prog).first()) if pt.program_plans(prog).exists() else None,
    })


def _owner_post(request, owner, prog):
    P = request.POST
    a = P.get('action')
    money_actions = {'save', 'accept', 'decline_app', 'pause', 'resume', 'approve', 'decline_topup'}
    if a in money_actions and not _owner_ok(request):
        messages.error(request, 'Only the business owner can change the agent programme, accept agents or approve top-ups.')
        return
    try:
        if a == 'save':
            prog.open = P.get('open') == '1'
            prog.areas = (P.get('areas') or '').strip()[:240]
            prog.discount_percent = pt.dec(P.get('discount_percent') or 0, 'discount')
            if not Decimal('0') <= prog.discount_percent < Decimal('100'):
                raise pt.PartnerError('The discount must be between 0 and 99 %.')
            prog.min_topup = pt.dec(P.get('min_topup') or 0, 'minimum top-up')
            prog.max_topup = max(prog.min_topup, pt.dec(P.get('max_topup') or 50000, 'maximum top-up'))
            prog.take_cash = P.get('take_cash') == '1'
            prog.auto_approve = P.get('auto_approve') == '1'
            prog.auto_approve_limit = pt.dec(P.get('auto_approve_limit') or 0, 'limit')
            prog.router = owner.routers.filter(pk=P.get('router') or 0).first()
            prog.note = (P.get('note') or '').strip()[:300]
            prog.save()
            from .voucher_plan_lock import open_plans
            prog.plans.set(open_plans(owner.plans.filter(pk__in=P.getlist('plans'))))
            messages.success(request, 'Agent programme saved.' + (' Agents can find your Wi-Fi now.' if prog.open else ' It is closed: nobody new can apply.'))
        elif a == 'accept':
            link = pt.accept(int(P.get('link')), owner, P.get('discount_percent'), P.get('note', ''), request.user)
            messages.success(request, f'{link.agent_account.business_name} is now your agent with {link.discount_percent:g}% off.')
        elif a == 'decline_app':
            pt.set_status(int(P.get('link')), owner, 'declined', P.get('note', ''))
            messages.info(request, 'Application declined.')
        elif a in ('pause', 'resume'):
            pt.set_status(int(P.get('link')), owner, 'paused' if a == 'pause' else 'active', P.get('note', ''))
            messages.info(request, 'Agent paused — they cannot buy until you resume them.' if a == 'pause' else 'Agent resumed.')
        elif a == 'approve':
            t = pt.approve(int(P.get('topup')), owner, P.get('amount'), P.get('router'), P.get('note', ''))
            if P.get('cash_now') == '1':
                with transaction.atomic():
                    t = pt.confirm_paid(t.pk, owner, request.user, method='cash')
                messages.success(request, f'{t.reference}: cash received — {t.quantity} vouchers made for {t.link.agent_account.business_name}.'
                                 if t.status == 'issued' else f'{t.reference}: {t.error}')
            else:
                messages.success(request, f'{t.reference} approved: {t.quantity} vouchers for {pt.money(owner, t.amount)}. '
                                          'The batch is made as soon as the payment is confirmed.')
        elif a == 'decline_topup':
            pt.decline(int(P.get('topup')), owner, P.get('note', ''))
            messages.info(request, 'Top-up declined.')
        elif a == 'confirm':
            if not (_owner_ok(request) or 'finance.agents' in getattr(request, 'tt_perms', ())):
                raise pt.PartnerError('Your role cannot confirm payments.')
            with transaction.atomic():
                t = pt.confirm_paid(int(P.get('topup')), owner, request.user, method=P.get('method', ''))
            if t.status == 'issued':
                messages.success(request, f'{t.reference}: payment confirmed — {t.quantity} vouchers of {t.plan_name} made for '
                                          f'{t.link.agent_account.business_name}.')
            else:
                messages.warning(request, f'{t.reference}: {t.error}')
        else:
            messages.error(request, 'Unknown action.')
    except AgentTopUp.DoesNotExist:
        messages.error(request, 'That top-up is not on your network.')
    except AgentLink.DoesNotExist:
        messages.error(request, 'That agent is not on your network.')
    except (pt.PartnerError, ValueError) as e:
        messages.error(request, str(e))


URLS = [
    ('partner/', home, 'partner_home'),
    ('partner/find/', find, 'partner_find'),
    ('partner/wifi/<int:owner_id>/', owner_page, 'partner_owner'),
    ('partner/wifi/<int:owner_id>/apply/', apply, 'partner_apply'),
    ('partner/wifi/<int:owner_id>/leave/', leave, 'partner_leave'),
    ('partner/wifi/<int:owner_id>/topup/', topup_new, 'partner_topup_new'),
    ('partner/topup/<str:reference>/', topup_page, 'partner_topup'),
    ('partner/vouchers/', vouchers, 'partner_vouchers'),
    ('partner/vouchers/<str:reference>/', batch, 'partner_batch'),
    ('partner/fee/', billing, 'partner_billing'),
    ('partner/profile/', profile, 'partner_profile'),
    ('partner/help/', help_page, 'partner_help'),
    ('join/<str:code>/', join, 'agent_join'),
    ('agents/network/', network, 'agent_network'),
]
