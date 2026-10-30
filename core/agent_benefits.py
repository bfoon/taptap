"""Agent benefits — the Benefits tab on every agent.

Commission
    Off, a % of each sale, or a fixed amount per voucher sold (never more than the sale). Stored on the Agent and
    applied by core/finance.py commission_for at the moment of each sale, so past sales keep the commission they
    were booked with.

Refund of unsold vouchers
    Vouchers the agent still holds — not used, not sold to a customer — and that have waited at least N days since
    they were given to the agent can be refunded:
      * paid upfront (prepaid batch): their sale is taken back, so the agent is owed what they paid for them
        (sale minus commission). That is paid back in cash (recorded as a negative hand-in, so every balance,
        statement and report nets it correctly) or left as credit toward their next vouchers.
      * on credit: nothing was paid, so the vouchers are simply taken back (refund value 0).
    The vouchers are then switched off on the router (recommended — the agent may still hold the printed cards)
    or put back in the shop's stock. Everything is re-checked under a lock inside one transaction.

Free internet
    A member account for the agent: username from the agent's name, a random 5-character password, on a member
    plan — the business's default plan for agents is picked first. Switching it off disables the account (kept,
    so switching on again brings the same login back).
"""
from __future__ import annotations

import re
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import F, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models_agent_benefits import AgentBenefit, AgentBenefitDefaults, AgentRefund

ZERO = Decimal('0.00')
PASSWORD_ALPHABET = 'abcdefghjkmnpqrstuvwxyz23456789'     # no 0/o, 1/l/i: easy to read out and type
PREPAID_NOTE = 'Bought upfront'                           # core/finance.py assign_batch writes this on prepaid sales


class BenefitError(Exception):
    pass


def _money(value, label='Amount'):
    try:
        v = Decimal(str(value if value not in (None, '') else '0').replace(',', '').strip())
    except (InvalidOperation, ValueError):
        raise BenefitError(f'{label} must be a number.')
    if v < 0:
        raise BenefitError(f'{label} cannot be negative.')
    return v


def benefit_for(agent):
    b, _ = AgentBenefit.objects.get_or_create(agent=agent)
    return b


def note(benefit, user, text):
    """Add one line to the agent's benefit history (newest first, last 40 kept)."""
    who = (user.get_full_name() or user.email or user.username) if getattr(user, 'is_authenticated', False) else 'TapTap'
    benefit.history = ([{'at': timezone.now().isoformat(), 'by': who, 'text': text[:200]}] + list(benefit.history or []))[:40]
    benefit.updated_by = user if getattr(user, 'is_authenticated', False) else None
    benefit.save(update_fields=['history', 'updated_by', 'updated_at'])


# ───────────────────────────── commission ─────────────────────────────

def save_commission(agent, data, user=None):
    enabled = str(data.get('commission_enabled', '')) in ('1', 'on', 'true')
    mode = data.get('commission_mode') if data.get('commission_mode') in ('percent', 'fixed') else 'percent'
    pct = _money(data.get('commission_percent', agent.commission_percent), 'The commission %')
    amount = _money(data.get('commission_amount', agent.commission_amount), 'The commission amount')
    if pct > 100:
        raise BenefitError('The commission cannot be more than 100%.')
    if enabled and mode == 'fixed' and amount <= 0:
        raise BenefitError('Enter the amount the agent earns per voucher, or switch commission off.')
    before = agent.commission_label
    agent.commission_enabled, agent.commission_mode = enabled, mode
    agent.commission_percent = pct.quantize(Decimal('0.0001'))
    agent.commission_amount = amount.quantize(Decimal('0.01'))
    agent.save(update_fields=['commission_enabled', 'commission_mode', 'commission_percent', 'commission_amount'])
    after = agent.commission_label
    if before != after:
        note(benefit_for(agent), user, f'Commission: {before} → {after} (new sales only)')
    return before != after


def commission_preview(agent):
    """Per orderable voucher plan: price, what the agent keeps, what they hand in."""
    from .finance import commission_for
    rows = []
    for p in agent.orderable_plans()[:12]:
        c = commission_for(agent, p.price)
        rows.append({'plan': p, 'price': p.price, 'earn': c, 'pays': p.price - c})
    return rows


# ───────────────────────────── refunds ─────────────────────────────

def _held(agent):
    """Vouchers the agent still holds that no customer has bought or used."""
    return (agent.business.vouchers.filter(agent=agent, status='active', used_at__isnull=True, frozen_at__isnull=True)
            .exclude(login_type='member')
            .filter(Q(sold_at__isnull=True) | Q(sale__notes__startswith=PREPAID_NOTE, sale__agent=agent)))


def refund_candidates(agent, min_days):
    """[{voucher, since, days, prepaid, value}] — held at least `min_days` since given to the agent, oldest first."""
    now = timezone.now()
    cutoff = now - timedelta(days=max(0, int(min_days or 0)))
    qs = (_held(agent).annotate(since=Coalesce('batch__issued_at', 'created_at')).filter(since__lte=cutoff)
          .select_related('batch', 'sale').order_by('since', 'id'))
    out = []
    for v in qs[:2000]:
        sale = getattr(v, 'sale', None)
        prepaid = bool(sale and (sale.notes or '').startswith(PREPAID_NOTE))
        value = (sale.amount - sale.commission) if prepaid else ZERO
        out.append({'voucher': v, 'since': v.since, 'days': (now - v.since).days, 'prepaid': prepaid, 'value': max(ZERO, value)})
    return out


def refund_summary(candidates):
    groups = {}
    for c in candidates:
        v = c['voucher']
        key = (v.batch.name if v.batch_id else 'One-off vouchers', v.plan_name)
        g = groups.setdefault(key, {'batch': key[0], 'plan': key[1], 'n': 0, 'value': ZERO, 'oldest': c['days'], 'items': []})
        g['n'] += 1; g['value'] += c['value']; g['oldest'] = max(g['oldest'], c['days']); g['items'].append(c)
    rows = sorted(groups.values(), key=lambda g: -g['oldest'])
    return {'groups': rows, 'n': len(candidates), 'value': sum((c['value'] for c in candidates), ZERO),
            'prepaid': sum(1 for c in candidates if c['prepaid'])}


@transaction.atomic
def refund(agent, voucher_ids, *, min_days, action, payout, method='cash', reference='', user=None):
    """Refund the chosen unsold vouchers. Returns the AgentRefund."""
    from .models import CashCollection, Voucher, VoucherSale
    from .voucher_history import record

    if action not in dict(AgentBenefit.REFUND_ACTIONS):
        raise BenefitError('Choose what happens to the vouchers.')
    if payout not in dict(AgentBenefit.REFUND_PAYOUTS):
        raise BenefitError('Choose how the agent gets the money back.')
    ids = {int(i) for i in voucher_ids if str(i).isdigit()}
    if not ids:
        raise BenefitError('Tick the vouchers to refund.')
    eligible = {c['voucher'].pk: c for c in refund_candidates(agent, min_days)}
    chosen = [i for i in ids if i in eligible]
    if not chosen:
        raise BenefitError('None of those vouchers can be refunded any more (sold, used or not old enough).')
    locked = {v.pk: v for v in Voucher.objects.select_for_update().filter(pk__in=chosen, agent=agent, used_at__isnull=True, status='active')}
    total, codes, done = ZERO, [], []
    for pk in chosen:
        v = locked.get(pk)
        if v is None:
            continue                                            # changed a moment ago: skip it, never guess
        c = eligible[pk]
        if c['prepaid']:
            VoucherSale.objects.filter(voucher=v, agent=agent).delete()
            total += c['value']
        v.sold_at = None
        if action == 'disable':
            v.status = 'disabled'
            v.save(update_fields=['sold_at', 'status'])        # the fast lane sends "disabled" to the router at once
        else:
            v.agent = None
            v.save(update_fields=['sold_at', 'agent'])
        codes.append(v.code); done.append(v)
    if not done:
        raise BenefitError('Those vouchers changed a moment ago — nothing was refunded.')
    total = total.quantize(Decimal('0.01'))
    collection = None
    if payout == 'cash' and total > 0:
        collection = CashCollection.objects.create(
            business=agent.business, agent=agent, amount=-total, payment_method=method or 'cash', reference=(reference or '')[:120],
            note=f'Refund paid to the agent: {len(done)} unsold voucher{"s" if len(done) != 1 else ""} (unsold {min_days}+ days)'[:255],
            recorded_by=user if getattr(user, 'is_authenticated', False) else None)
    r = AgentRefund.objects.create(
        business=agent.business, agent=agent, count=len(done), amount=total, payout=payout, action=action, min_days=int(min_days or 0),
        codes=codes, collection=collection, created_by=user if getattr(user, 'is_authenticated', False) else None,
        note=(reference or '')[:255])
    what = 'switched off' if action == 'disable' else 'back in the shop’s stock'
    for v in done:
        record(v, 'note', user=user, text=f'Refunded to agent {agent.name} ({r.number}) — unsold, {what}')
    money = f'{agent.business.currency}{total:,.2f}'
    note(benefit_for(agent), user, f'Refund {r.number}: {len(done)} unsold voucher{"s" if len(done) != 1 else ""}, '
                                   f'{money} {"paid back" if payout == "cash" else "kept as credit"}, vouchers {what}')
    return r


# ───────────────────────────── free internet ─────────────────────────────

def default_plan(business):
    d = AgentBenefitDefaults.objects.filter(business=business).select_related('member_plan').first()
    if d and d.member_plan and d.member_plan.active:
        return d.member_plan
    return None


def set_default_plan(business, plan):
    d, _ = AgentBenefitDefaults.objects.get_or_create(business=business)
    d.member_plan = plan
    d.save(update_fields=['member_plan'])


def create_agent_plan(business, user=None, devices=2, speed=''):
    """One click: a free, unlimited 'Agent Wi-Fi' member plan, made the default for agents."""
    from .members import save_member_plan
    name = 'Agent Wi-Fi'
    n = 2
    while business.member_plans.filter(name__iexact=name).exists():
        name = f'Agent Wi-Fi {n}'; n += 1
    plan, _ = save_member_plan(business, {'name': name, 'price': '0', 'max_devices': devices, 'speed_limit': speed,
                                          'duration_unit': 'unlimited', 'duration_value': 1}, user=user)
    set_default_plan(business, plan)
    return plan


def suggested_username(agent):
    from .members import USERNAME_RE, username_taken
    base = re.sub(r'[^a-z0-9]+', '.', (agent.name or '').lower()).strip('.')[:24] or 'agent'
    if not base[0].isalnum():
        base = 'agent' + base
    if len(base) < 3:
        base = (base + 'agent')[:24]
    name, n = base, 2
    while username_taken(name) or not USERNAME_RE.match(name):
        name = f'{base}{n}'; n += 1
        if n > 999:
            name = f'agent{secrets.randbelow(10 ** 6)}'
    return name


def new_password():
    return ''.join(secrets.choice(PASSWORD_ALPHABET) for _ in range(5))


def _default_router(agent):
    """The router the agent's vouchers mostly go to, else the business's only/first router."""
    from django.db.models import Count
    business = agent.business
    top = (business.vouchers.filter(agent=agent, router__isnull=False).values('router').annotate(n=Count('id')).order_by('-n').first())
    if top:
        r = business.routers.filter(pk=top['router']).first()
        if r:
            return r
    return business.routers.order_by('name').first()


def enable_free_internet(agent, *, plan=None, router=None, user=None):
    """Create (or switch back on) the agent's member account. Returns (member, created, password_or_None)."""
    from .members import MemberError, assign_member_plan, create_member

    b = benefit_for(agent)
    business = agent.business
    plan = plan or b.member_plan or default_plan(business)
    if plan is None:
        raise BenefitError('Choose a member plan for the agent (or create the free “Agent Wi-Fi” plan).')
    if plan.business_id != business.pk:
        raise BenefitError('That member plan belongs to another business.')
    if not plan.active:
        raise BenefitError(f'The member plan “{plan.name}” is switched off. Switch it on first or choose another.')

    member = b.member
    if member is not None and (member.deleted_at is not None or member.business_id != business.pk):
        member = None
    created, password = False, None
    with transaction.atomic():
        if member is None:
            password = new_password()
            try:
                member = create_member(
                    business, username=suggested_username(agent), password=password, plan_value=str(plan.pk),
                    router=router or _default_router(agent), customer_name=agent.name, customer_phone=agent.phone,
                    note='Free internet — agent benefit', paid=False, user=user)
            except MemberError as exc:
                raise BenefitError(str(exc))
            created = True
        else:
            from .members import plan_for_member
            current = plan_for_member(member)
            if current is None or current.pk != plan.pk:
                try:
                    assign_member_plan(member, plan, user=user)
                except MemberError as exc:
                    raise BenefitError(str(exc))
            if member.status != 'active':
                member.status = 'active'
                member.save(update_fields=['status'])
        b.free_internet, b.member, b.member_plan = True, member, plan
        b.save(update_fields=['free_internet', 'member', 'member_plan', 'updated_at'])
    note(b, user, (f'Free internet on: member {member.code} created on “{plan.name}”' if created
                   else f'Free internet back on: member {member.code} on “{plan.name}”'))
    return member, created, password


def disable_free_internet(agent, user=None):
    b = benefit_for(agent)
    m = b.member
    if m is not None and m.deleted_at is None and m.status == 'active':
        m.status = 'disabled'
        m.save(update_fields=['status'])                        # the fast lane switches it off on the router
    b.free_internet = False
    b.save(update_fields=['free_internet', 'updated_at'])
    note(b, user, f'Free internet off' + (f': member {m.code} switched off (kept, so it can come back)' if m else ''))
    return m


def reset_password(agent, user=None):
    """A new 5-character password for the agent's member account, sent to the router. Returns it."""
    from .members import _push_password
    b = benefit_for(agent)
    m = b.member
    if m is None or m.deleted_at is not None:
        raise BenefitError('The agent has no member account yet.')
    pw = new_password()
    m.password = pw
    m.save(update_fields=['password'])
    via, result, ok = _push_password(m, user)
    note(b, user, f'New password for member {m.code}' + ('' if ok else f' — router: {result}'))
    return pw, ok, result
