"""Investors — owner pages (Finance permission) and the investor portal (/p/investor/, own session)."""
from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import investors as svc
from .models_investors import Investment, InvestmentOpportunity, Investor

logger = logging.getLogger('taptap.investors')


def _business(request):
    return getattr(request, 'tt_business', None) or request.user.business


def _can_manage(request):
    return 'finance.manage' in getattr(request, 'tt_perms', frozenset())


def _checked(post, name):
    return post.get(name) in ('1', 'on', 'true', 'yes')


def _opp_router_ids(post, business):
    from .models import Router
    ids = [int(x) for x in post.getlist('routers') if str(x).isdigit()]
    return list(Router.objects.filter(business=business, pk__in=ids))


# ═══════════════════════════════ owner ═══════════════════════════════

@login_required
def admin_page(request):
    business = _business(request)
    for inv in Investment.objects.filter(business=business, status='active'):
        svc.accrue(inv)
    investors = (Investor.objects.filter(business=business)
                 .annotate(n=Count('investments', filter=Q(investments__status__in=['active', 'completed'])),
                           invested=Sum('investments__principal', filter=Q(investments__status__in=['active', 'completed'])),
                           earned=Sum('investments__earned_total')))
    deals = list(Investment.objects.filter(business=business).select_related('investor', 'opportunity').order_by('-created_at')[:300])
    from .models_investors import InvestorPayout
    paid = {r['investment']: r['v'] for r in InvestorPayout.objects.filter(business=business).values('investment').annotate(v=Sum('amount'))}
    for d in deals:
        d.paid = svc.q(paid.get(d.pk))
        d.balance = svc.q(d.earned_total - d.paid)
    opps = list(InvestmentOpportunity.objects.filter(business=business).prefetch_related('routers'))
    for o in opps:
        o.raised, o.pending, o.left = svc.committed(o), svc.pending_offers(o), svc.remaining(o)
        o.pct = round(min(100.0, float(o.raised / o.target_amount * 100)), 1) if o.target_amount else 0
    active = [d for d in deals if d.status in ('active', 'completed')]
    kpi = {
        'capital': svc.q(sum((d.principal for d in active), Decimal('0'))),
        'earned': svc.q(sum((d.earned_total for d in active), Decimal('0'))),
        'repaid': svc.q(sum((d.principal_repaid for d in active), Decimal('0'))),
        'owed': svc.q(sum((d.balance for d in active), Decimal('0'))),
        'investors': sum(1 for i in investors if i.status == 'active'),
    }
    edit_opp = InvestmentOpportunity.objects.filter(business=business, pk=request.GET.get('edit_opp')).first() if request.GET.get('edit_opp', '').isdigit() else None
    edit_investor = Investor.objects.filter(business=business, pk=request.GET.get('edit_investor')).first() if request.GET.get('edit_investor', '').isdigit() else None
    return render(request, 'core/investors_admin.html', {
        'business': business, 'investors': investors, 'deals': deals, 'opps': opps, 'kpi': kpi, 'can_manage': _can_manage(request),
        'offers': [d for d in deals if d.status == 'offered'], 'payments': [d for d in deals if d.status in ('payment_review', 'awaiting_payment')],
        'routers': business.routers.all().order_by('name'), 'settings_obj': svc.settings_for(business), 'load': svc.share_load(business),
        'edit_opp': edit_opp, 'edit_opp_router_ids': list(edit_opp.routers.values_list('pk', flat=True)) if edit_opp else [],
        'edit_opp_invited_ids': list(edit_opp.invited.values_list('pk', flat=True)) if edit_opp else [],
        'edit_investor': edit_investor, 'checkout_configured': svc.checkout_configured(), 'tab': request.GET.get('tab', 'overview'),
    })


def _save_opportunity(request, business):
    p = request.POST
    opp = None
    if p.get('id', '').isdigit():
        opp = get_object_or_404(InvestmentOpportunity, business=business, pk=int(p['id']))
    target, share = svc.dec(p.get('target_amount')), svc.dec(p.get('share_pct'))
    if not target or target <= 0:
        raise ValueError('Enter how much money you are raising.')
    terms = p.get('terms') if p.get('terms') in ('profit_share', 'fixed') else 'profit_share'
    if share is None or share <= 0 or share > 100:
        raise ValueError('The profit share for the full amount must be between 0 and 100 %.')
    payback = svc.dec(p.get('payback_pct'), Decimal('70'))
    if not (0 <= payback <= 100):
        raise ValueError('The capital part of each earning must be between 0 and 100 %.')
    fixed = svc.dec(p.get('fixed_return_pct'), Decimal('0'))
    if terms == 'fixed' and fixed <= 0:
        raise ValueError('Enter the fixed return % (for example 25).')
    if terms == 'profit_share' and payback == 0 and not svc.dec(p.get('cap_pct')):
        raise ValueError('With 0 % to capital the investor is never paid back — set a capital part or a cap.')
    scope = 'sites' if p.get('scope') == 'sites' else 'business'
    routers = _opp_router_ids(p, business)
    if scope == 'sites' and not routers:
        raise ValueError('Choose the site(s) whose profit this opportunity shares.')
    closes = None
    if p.get('closes_at'):
        try:
            closes = date.fromisoformat(p['closes_at'])
        except ValueError:
            raise ValueError('Closing date must be a date.')
    fields = dict(
        title=(p.get('title') or '').strip()[:150] or 'Investment opportunity', summary=(p.get('summary') or '').strip()[:300],
        description=(p.get('description') or '').strip()[:5000], use_of_funds=(p.get('use_of_funds') or '').strip()[:3000],
        risks=(p.get('risks') or '').strip()[:3000], scope=scope, target_amount=svc.q(target),
        min_ticket=svc.q(svc.dec(p.get('min_ticket'), Decimal('0'))), max_ticket=svc.dec(p.get('max_ticket')),
        terms=terms, share_pct=share, payback_pct=payback, after_payback_share_pct=svc.dec(p.get('after_payback_share_pct')),
        fixed_return_pct=fixed, cap_pct=svc.dec(p.get('cap_pct')), expected_monthly_profit=svc.dec(p.get('expected_monthly_profit')),
        visibility='invited' if p.get('visibility') == 'invited' else 'all', closes_at=closes,
        status=p.get('status') if p.get('status') in ('draft', 'open', 'funded', 'closed') else 'draft')
    if opp is None:
        opp = InvestmentOpportunity.objects.create(business=business, reference=svc.new_reference('OPP'), **fields)
    else:
        for k, v in fields.items():
            setattr(opp, k, v)
        opp.save()
    opp.routers.set(routers if scope == 'sites' else [])
    invited = [int(x) for x in p.getlist('invited') if str(x).isdigit()]
    opp.invited.set(Investor.objects.filter(business=business, pk__in=invited))
    return opp


@login_required
@require_POST
def admin_action(request):
    business = _business(request)
    p, do = request.POST, request.POST.get('do', '')
    back = p.get('next') or reverse('investors_admin')
    if not back.startswith('/'):
        back = reverse('investors_admin')
    try:
        if do == 'investor_save':
            email = (p.get('email') or '').strip().lower()
            name = (p.get('name') or '').strip()
            if not name or '@' not in email:
                raise ValueError('Enter the investor’s name and email.')
            inv = Investor.objects.filter(business=business, pk=p.get('id')).first() if p.get('id', '').isdigit() else None
            if Investor.objects.filter(business=business, email__iexact=email).exclude(pk=getattr(inv, 'pk', None)).exists():
                raise ValueError('Another investor already uses that email.')
            created = inv is None
            inv = inv or Investor(business=business)
            inv.name, inv.email = name[:150], email
            inv.phone, inv.company, inv.notes = p.get('phone', '')[:60], p.get('company', '')[:150], p.get('notes', '')[:500]
            inv.daily_summary, inv.allow_fit = _checked(p, 'daily_summary'), _checked(p, 'allow_fit')
            inv.status = 'paused' if p.get('status') == 'paused' else 'active'
            inv.save()
            if created and _checked(p, 'send_link'):
                svc.send_link(inv, request, by=svc._who(request.user))
            messages.success(request, f'{inv.name} saved.' + (' Portal link emailed.' if created and _checked(p, 'send_link') else ''))
        elif do == 'investor_link':
            inv = get_object_or_404(Investor, business=business, pk=p.get('id'))
            svc.send_link(inv, request, by=svc._who(request.user))
            messages.success(request, f'Portal link emailed to {inv.email}.')
        elif do == 'investor_signout':
            inv = get_object_or_404(Investor, business=business, pk=p.get('id'))
            svc.sign_out_everywhere(inv, request.user)
            messages.success(request, f'{inv.name} is signed out everywhere.')
        elif do == 'investor_summary_now':
            inv = get_object_or_404(Investor, business=business, pk=p.get('id'))
            messages.success(request, 'Summary sent.' if svc.send_summary(inv) else 'Nothing to summarise yet (no live investment).')
        elif do == 'opp_save':
            opp = _save_opportunity(request, business)
            messages.success(request, f'Opportunity “{opp.title}” saved ({opp.get_status_display()}).')
            back = reverse('investors_admin') + '?tab=opportunities'
        elif do == 'deal_create':
            investor = get_object_or_404(Investor, business=business, pk=p.get('investor'))
            data = {k: p.get(k) for k in ('title', 'principal', 'share_pct', 'payback_pct', 'fixed_return_pct', 'after_payback_share_pct',
                                          'cap_pct', 'terms', 'scope', 'start_date', 'payment_reference')}
            data.update(routers=p.getlist('routers'), carry_losses=_checked(p, 'carry_losses'), show_basis=_checked(p, 'show_basis'),
                        already_paid=_checked(p, 'already_paid'))
            inv = svc.create_direct(business, investor, data, request.user)
            messages.success(request, f'{inv.reference} created for {investor.name}.')
            back = reverse('investment_admin', args=[inv.pk])
        elif do in ('accept', 'reject', 'confirm', 'cancel', 'recalc', 'payout', 'deal_flags'):
            inv = get_object_or_404(Investment, business=business, pk=p.get('id'))
            back = reverse('investment_admin', args=[inv.pk])
            if do == 'accept':
                svc.decide(inv, True, p.get('note', ''), request.user, p.get('amount'))
                messages.success(request, 'Offer accepted — the investor has the payment instructions.')
            elif do == 'reject':
                svc.decide(inv, False, p.get('note', ''), request.user)
                messages.success(request, 'Offer declined and the investor informed.')
            elif do == 'confirm':
                start = date.fromisoformat(p['start_date']) if p.get('start_date') else None
                svc.activate(inv, request.user, start, p.get('reference', ''))
                messages.success(request, f'Payment confirmed — {inv.reference} is live.')
            elif do == 'cancel':
                svc.cancel(inv, request.user, p.get('note', ''))
                messages.success(request, 'Investment cancelled.')
            elif do == 'recalc':
                n = svc.recalculate(inv, request.user)
                messages.success(request, f'Earnings rebuilt ({n} days).')
            elif do == 'payout':
                svc.record_payout(inv, p.get('amount'), p.get('method', ''), p.get('reference', ''), p.get('note', ''), request.user)
                messages.success(request, 'Payout recorded and the investor informed.')
            elif do == 'deal_flags':
                inv.show_basis, inv.carry_losses = _checked(p, 'show_basis'), _checked(p, 'carry_losses')
                inv.save(update_fields=['show_basis', 'carry_losses', 'updated_at'])
                messages.success(request, 'Saved.')
        elif do == 'settings':
            s = svc.settings_for(business)
            s.enabled = _checked(p, 'enabled')
            s.portal_intro = p.get('portal_intro', '')[:300]
            s.payment_instructions = p.get('payment_instructions', '')[:3000]
            hour = svc.dec(p.get('summary_hour'), Decimal('8'))
            s.summary_hour = int(min(23, max(0, hour)))
            s.save()
            messages.success(request, 'Investor settings saved.')
            back = reverse('investors_admin') + '?tab=settings'
        else:
            raise ValueError('Unknown action.')
    except ValueError as exc:
        messages.error(request, str(exc))
    except Exception:
        logger.exception('investor action %s', do)
        messages.error(request, 'That did not work — please try again.')
    return redirect(back)


@login_required
def admin_deal(request, pk):
    business = _business(request)
    inv = get_object_or_404(Investment.objects.select_related('investor', 'opportunity'), business=business, pk=pk)
    svc.accrue(inv)
    inv.refresh_from_db()
    from .models_investors import InvestorEvent
    return render(request, 'core/investment_admin.html', {
        'business': business, 'inv': inv, 'card': svc.deal_card(inv), 'can_manage': _can_manage(request),
        'accruals': inv.accruals.order_by('-day')[:60], 'payouts': inv.payouts.all(),
        'events': InvestorEvent.objects.filter(investment=inv)[:50], 'today': timezone.localdate().isoformat(),
        'remaining': svc.remaining(inv.opportunity) if inv.opportunity_id else None,
        'instructions': svc.settings_for(business).payment_instructions,
    })


@login_required
@require_GET
def admin_deal_data(request, pk):
    inv = get_object_or_404(Investment, business=_business(request), pk=pk)
    return JsonResponse(svc.deal_detail(inv, owner=True))


# ═══════════════════════════════ investor portal ═══════════════════════════════

def _portal_ctx(investor, **extra):
    business = investor.business
    s = svc.settings_for(business)
    ctx = {'investor': investor, 'business': business, 'settings_obj': s, 'cur': business.currency,
           'fit_ready': _fit_ready() and investor.allow_fit}
    ctx.update(extra)
    return ctx


def _fit_ready():
    try:
        from . import fit_ai
        return bool(fit_ai.routes())
    except Exception:
        return False


def _need_investor(view):
    def wrapped(request, *args, **kwargs):
        investor = svc.current_investor(request)
        if investor is None:
            if request.headers.get('x-requested-with') == 'fetch' or request.path.endswith(('/data/', '/live/', '/estimate/', '/fit/')):
                return JsonResponse({'ok': False, 'error': 'Your session ended — sign in again.'}, status=401)
            return redirect('investor_portal')
        request.investor = investor
        return view(request, *args, **kwargs)
    wrapped.__name__ = view.__name__
    return wrapped


def portal(request):
    investor = svc.current_investor(request)
    if investor is None:
        return render(request, 'core/investor/login.html', {'sent': request.GET.get('sent') == '1'})
    return render(request, 'core/investor/dashboard.html', _portal_ctx(investor))


@require_POST
def portal_request_link(request):
    svc.request_link(request, request.POST.get('email'))
    return redirect(reverse('investor_portal') + '?sent=1')


def portal_access(request, token):
    investor = svc.consume_link(token)
    if investor is None:
        return render(request, 'core/investor/login.html', {'expired': True}, status=400)
    svc.start_session(request, investor)
    return redirect('investor_portal')


@require_POST
def portal_logout(request):
    for k in (svc.SESSION_ID, svc.SESSION_AV):
        request.session.pop(k, None)
    return redirect('investor_portal')


@_need_investor
@require_GET
def portal_live(request):
    return JsonResponse(svc.dashboard(request.investor))


def _my_deal(request, ref):
    inv = (Investment.objects.filter(investor=request.investor, reference=ref.upper()).exclude(status='cancelled')
           .select_related('business', 'opportunity').first())
    if not inv:
        raise Http404
    return inv


@_need_investor
def portal_investment(request, ref):
    inv = _my_deal(request, ref)
    return render(request, 'core/investor/investment.html', _portal_ctx(
        request.investor, inv=inv, card=svc.deal_card(inv), checkout=svc.checkout_configured()))


@_need_investor
@require_GET
def portal_investment_data(request, ref):
    return JsonResponse(svc.deal_detail(_my_deal(request, ref)))


@_need_investor
@require_POST
def portal_mark_paid(request, ref):
    inv = _my_deal(request, ref)
    try:
        svc.mark_paid(inv, request.POST.get('method', ''), request.POST.get('reference', ''), request)
        messages.success(request, 'Thank you — the business will confirm your payment shortly.')
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect('investor_investment', ref=inv.reference)


@_need_investor
@require_POST
def portal_checkout(request, ref):
    inv = _my_deal(request, ref)
    try:
        return redirect(svc.checkout_url(inv, request))
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect('investor_investment', ref=inv.reference)


def _my_opp(request, ref):
    o = svc.visible_opportunities(request.investor).filter(reference=ref.upper()).prefetch_related('routers').first()
    if not o:
        raise Http404
    return o


@_need_investor
def portal_opportunity(request, ref):
    o = _my_opp(request, ref)
    raised = svc.committed(o)
    mine = Investment.objects.filter(investor=request.investor, opportunity=o).exclude(status='cancelled')
    suggested = max(o.min_ticket, min(svc.remaining(o), (o.target_amount / 10).quantize(Decimal('1'))))
    return render(request, 'core/investor/opportunity.html', _portal_ctx(
        request.investor, opp=o, raised=raised, left=svc.remaining(o), mine=mine, suggested=suggested,
        pct=round(min(100.0, float(raised / o.target_amount * 100)), 1) if o.target_amount else 0,
        uses=[x.strip('-• ').strip() for x in o.use_of_funds.splitlines() if x.strip()],
        sites=', '.join(o.routers.values_list('name', flat=True))))


@_need_investor
@require_GET
def portal_estimate(request, ref):
    o = _my_opp(request, ref)
    amount = svc.dec(request.GET.get('amount'))
    if not amount or amount <= 0:
        return JsonResponse({'ok': False, 'error': 'Enter an amount.'}, status=400)
    return JsonResponse({'ok': True, **svc.estimate(o, min(amount, o.target_amount))})


@_need_investor
@require_POST
def portal_offer(request, ref):
    o = _my_opp(request, ref)
    try:
        inv = svc.place_offer(request.investor, o, request.POST.get('amount'), request.POST.get('note', ''), request)
        messages.success(request, 'Your offer was sent. You will get an email as soon as the business answers.')
        return redirect('investor_investment', ref=inv.reference)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect('investor_opportunity', ref=o.reference)


@_need_investor
@require_POST
def portal_fit(request):
    from . import investor_fit
    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        body = {}
    try:
        return JsonResponse({'ok': True, 'reply': investor_fit.ask(request.investor, body.get('text', ''))})
    except ValueError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
    except Exception as exc:
        logger.warning('investor fit: %s', exc)
        return JsonResponse({'ok': False, 'error': str(exc) or 'Fit is not available right now.'}, status=503)


@csrf_exempt
@require_POST
def payment_webhook(request):
    if not svc.verify_webhook(request.body, request.headers.get('X-TapTap-Signature', '')):
        return JsonResponse({'ok': False, 'error': 'bad signature'}, status=401)
    try:
        inv = svc.settle_webhook(json.loads(request.body or b'{}'))
    except ValueError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
    return JsonResponse({'ok': True, 'reference': inv.reference, 'status': inv.status})
