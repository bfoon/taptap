"""The Benefits tab of an agent: commission, refunds of unsold vouchers, free internet."""
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from . import agent_benefits as ab
from .members import plan_for_member
from .models_agent_benefits import AgentBenefit
from .utils import log
from .views_agents import MANUAL_METHODS, push_one, wa_number

MANAGE = {'agents.manage', 'finance.manage', 'finance.agents'}
REFUND_DAYS = (7, 14, 30, 60, 90)


def _members_on(business):
    try:
        from .features import enabled
        return enabled(business, 'members')
    except Exception:
        return True


def _days(value, default):
    try:
        return max(0, min(3650, int(value)))
    except (TypeError, ValueError):
        return default


@login_required
def agent_benefits(request, pk):
    business = request.user.business
    agent = get_object_or_404(business.agents, pk=pk)
    benefit = ab.benefit_for(agent)
    here = reverse('agent_benefits', args=[agent.pk])
    can_manage = bool(MANAGE & set(getattr(request, 'tt_perms', ())))

    if request.method == 'POST':
        if not can_manage:
            return HttpResponseForbidden('Your role cannot change agent benefits.')
        f, action = request.POST, request.POST.get('action', '')
        try:
            if action == 'commission':
                if ab.save_commission(agent, f, request.user):
                    messages.success(request, f'{agent.name}: {agent.commission_label}. It applies to new sales; past sales keep theirs.')
                    log(business, 'Agent Benefit', f'{agent.name}: {agent.commission_label}')
                else:
                    messages.info(request, 'Commission unchanged.')
                return redirect(f'{here}#commission')

            if action == 'refund_settings':
                benefit.refund_enabled = f.get('refund_enabled') in ('1', 'on')
                benefit.refund_after_days = _days(f.get('refund_after_days'), benefit.refund_after_days)
                benefit.refund_action = f.get('refund_action') if f.get('refund_action') in dict(AgentBenefit.REFUND_ACTIONS) else benefit.refund_action
                benefit.refund_payout = f.get('refund_payout') if f.get('refund_payout') in dict(AgentBenefit.REFUND_PAYOUTS) else benefit.refund_payout
                benefit.save(update_fields=['refund_enabled', 'refund_after_days', 'refund_action', 'refund_payout', 'updated_at'])
                ab.note(benefit, request.user, ('Refunds on' if benefit.refund_enabled else 'Refunds off') +
                        (f': unsold {benefit.refund_after_days}+ days, {benefit.get_refund_action_display().lower()}, '
                         f'{benefit.get_refund_payout_display().lower()}' if benefit.refund_enabled else ''))
                messages.success(request, 'Refund settings saved.')
                return redirect(f'{here}#refunds')

            if action == 'refund':
                if not benefit.refund_enabled:
                    raise ab.BenefitError('Switch refunds on for this agent first.')
                r = ab.refund(agent, f.getlist('voucher'), min_days=_days(f.get('min_days'), benefit.refund_after_days),
                              action=f.get('refund_action') or benefit.refund_action, payout=f.get('refund_payout') or benefit.refund_payout,
                              method=f.get('method') or 'cash', reference=f.get('reference', ''), user=request.user)
                cur = business.currency
                messages.success(request, f'{r.number}: {r.count} voucher{"s" if r.count != 1 else ""} refunded — '
                                          + (f'{cur}{r.amount:,.2f} paid back to {agent.name}.' if r.payout == 'cash' and r.amount
                                             else f'{cur}{r.amount:,.2f} kept as credit for {agent.name}.' if r.amount
                                             else 'nothing was paid for them (on credit), so they were simply taken back.'))
                log(business, 'Agent Refund', f'{agent.name}: {r.number}, {r.count} vouchers, {business.currency}{r.amount}')
                return redirect(f'{here}?refund={r.pk}#refunds')

            if action == 'free_on':
                if not _members_on(business):
                    raise ab.BenefitError('Members is switched off for this business, so agents cannot get free internet.')
                plan = None
                if f.get('plan') == 'new':
                    plan = ab.create_agent_plan(business, request.user)
                    messages.info(request, f'Created the free member plan “{plan.name}” (unlimited, {plan.max_devices} devices) and made it the default for agents.')
                elif str(f.get('plan') or '').isdigit():
                    plan = business.member_plans.filter(pk=int(f['plan'])).first()
                router = business.routers.filter(pk=f.get('router') or 0).first()
                if f.get('make_default') in ('1', 'on') and plan is not None:
                    ab.set_default_plan(business, plan)
                member, created, password = ab.enable_free_internet(agent, plan=plan, router=router, user=request.user)
                if member.router_id:
                    push_one(member, plan_for_member(member))
                if created:
                    request.session[f'agent_pw_{agent.pk}'] = password      # shown once on the card, then forgotten
                    messages.success(request, f'Free internet is on: {agent.name} logs in as {member.code}.')
                else:
                    messages.success(request, f'Free internet is back on for {agent.name} ({member.code}).')
                log(business, 'Agent Benefit', f'{agent.name}: free internet on ({member.code})')
                return redirect(f'{here}#internet')

            if action == 'free_off':
                m = ab.disable_free_internet(agent, request.user)
                messages.success(request, f'Free internet is off for {agent.name}' + (f' — {m.code} is switched off.' if m else '.'))
                log(business, 'Agent Benefit', f'{agent.name}: free internet off')
                return redirect(f'{here}#internet')

            if action == 'new_password':
                pw, ok, result = ab.reset_password(agent, request.user)
                request.session[f'agent_pw_{agent.pk}'] = pw
                (messages.success if ok else messages.warning)(request, 'New password made. ' + result)
                return redirect(f'{here}#internet')

            if action == 'default_plan':
                plan = business.member_plans.filter(pk=f.get('plan') or 0, active=True).first()
                ab.set_default_plan(business, plan)
                messages.success(request, f'Default plan for agents: {plan.name}.' if plan else 'No default plan for agents.')
                return redirect(f'{here}#internet')
        except ab.BenefitError as exc:
            messages.error(request, str(exc))
            anchor = 'commission' if action == 'commission' else 'refunds' if 'refund' in action else 'internet'
            return redirect(f'{here}#{anchor}')
        messages.error(request, 'Unknown action.')
        return redirect(here)

    # ── page ──
    min_days = _days(request.GET.get('days'), benefit.refund_after_days)
    candidates = ab.refund_candidates(agent, min_days) if benefit.refund_enabled else []
    member = benefit.member if benefit.member_id and benefit.member and benefit.member.deleted_at is None else None
    password = request.session.pop(f'agent_pw_{agent.pk}', None)
    plans = business.member_plans.filter(active=True).order_by('price', 'name')
    default = ab.default_plan(business)
    preselect = (benefit.member_plan if benefit.member_plan and benefit.member_plan.active else None) or default or plans.first()
    wa = ''
    if member and agent.phone:
        text = (f'Hello {agent.name}, your free {business.business_name} Wi-Fi:\nUsername: {member.code}\n'
                + (f'Password: {password}\n' if password else 'Password: (as given to you)\n') + 'Thank you for selling with us!')
        wa = f'https://wa.me/{wa_number(agent.phone)}?text={quote(text)}'
    from .finance import agent_balances
    bal = next((r for r in agent_balances(business) if r['agent'].id == agent.id), None)
    shown = None
    if str(request.GET.get('refund', '')).isdigit():
        shown = agent.refunds.filter(pk=int(request.GET['refund'])).select_related('created_by').first()
    return render(request, 'core/agent_benefits.html', {
        'agent': agent, 'b': benefit, 'can_manage': can_manage, 'tab': 'benefits',
        'preview': ab.commission_preview(agent), 'bal': bal,
        'min_days': min_days, 'day_chips': REFUND_DAYS, 'refund': ab.refund_summary(candidates),
        'refunds': agent.refunds.select_related('created_by')[:20], 'shown_refund': shown,
        'refund_actions': AgentBenefit.REFUND_ACTIONS, 'refund_payouts': AgentBenefit.REFUND_PAYOUTS,
        'members_on': _members_on(business), 'member': member, 'password': password,
        'member_plan_now': plan_for_member(member) if member else None,
        'plans': plans, 'default_plan': default, 'preselect': preselect,
        'routers': business.routers.order_by('name'), 'router_guess': ab._default_router(agent),
        'suggested': ab.suggested_username(agent) if not member else '', 'wa': wa,
        'history': benefit.history[:15],
        'methods': MANUAL_METHODS,
    })
