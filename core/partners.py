"""Three kinds of TapTap account, chosen when signing up (``Business.account_kind``):

    wifi      a Wi-Fi owner — the TapTap everyone knows (routers, vouchers, finance…)
    agent     sells the vouchers of Wi-Fi owners near them, BUYING BEFORE RESELLING: finds networks in their area,
              applies, and once an owner accepts, asks for top-ups ("I want to spend D2,000"). The owner accepts or
              lowers the amount; when the agent has paid (cash, Wave, QMoney, Afrimoney) a batch is made for them at
              once — D2,500 of vouchers for D2,000 when the owner gives 20 %. They resell at the owner's prices.
    reseller  buys batches on the TapTap Market and resells them (core/market.py): their own shop and wallets.

Agents and resellers have their own portal instead of the Wi-Fi dashboard (``PartnerGateMiddleware``), a free trial
(``PARTNER_TRIAL_DAYS``) and then a monthly fee (``AGENT_MONTHLY_FEE`` / ``RESELLER_MONTHLY_FEE``, default D50),
paid as a normal TapTap subscription the platform confirms. Without paid time they keep seeing their batches and
codes, but cannot ask for new stock until they pay.

On the owner's side an accepted agent is a normal ``core.Agent`` with a percentage commission equal to the owner's
discount, so every top-up becomes a batch handed over **paid upfront** (core/finance.assign_batch, settlement
"prepaid"): the sales and the agent's payment land in the owner's books like any agent's. The owner's Market wallets
take the payments: Wave on the owner's own API key or through TapTap (paid out at once, core/market_wave.py) is
confirmed by Wave itself; QMoney, Afrimoney, Wave by hand and cash are confirmed by the owner.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger('taptap.partners')

KINDS = {'wifi': ('Wi-Fi owner', 'bi-router'), 'agent': ('Agent', 'bi-person-badge'), 'reseller': ('Reseller', 'bi-shop-window')}
PARTNER_KINDS = ('agent', 'reseller')
PAY_HOURS = 48                     # an approved top-up waits this long for the payment
MAX_VOUCHERS = 5000


class PartnerError(ValueError):
    pass


def dec(v, what='amount'):
    try:
        d = Decimal(str(v).replace(',', '').strip())
    except (InvalidOperation, ValueError):
        raise PartnerError(f'Type the {what} as a number.')
    if d != d or d < 0:
        raise PartnerError(f'Type the {what} as a number.')
    return d.quantize(Decimal('0.01'))


def kind_of(business):
    return getattr(business, 'account_kind', 'wifi') or 'wifi'


def is_partner(business):
    return kind_of(business) in PARTNER_KINDS


def trial_days():
    return int(getattr(settings, 'PARTNER_TRIAL_DAYS', getattr(settings, 'TRIAL_DAYS', 7)) or 0)


def monthly_fee(kind):
    name = 'AGENT_MONTHLY_FEE' if kind == 'agent' else 'RESELLER_MONTHLY_FEE'
    try:
        return Decimal(str(getattr(settings, name, 50)))
    except Exception:
        return Decimal('50')


def fee_currency():
    return getattr(settings, 'PARTNER_FEE_CURRENCY', 'GMD')


def money(business, v):
    try:
        v = Decimal(str(v or 0))
    except Exception:
        v = Decimal('0')
    s = f'{v:,.2f}'
    if s.endswith('.00'):
        s = s[:-3]
    return f'{(getattr(business, "currency", "") or "D").strip()}{s}'


# ─────────────────────────────── access (trial and the monthly fee) ───────────────────────────────

def standing(business):
    """Where a partner stands: {'ok', 'trial', 'days', 'until', 'fee', 'pending'}."""
    now = timezone.now()
    until = business.access_expires_at()
    ok = bool(business.is_unlimited or (until and until > now))
    pending = business.subscriptions.filter(payment_status='Pending').order_by('-created_at').first()
    return {'ok': ok, 'trial': business.subscription_status != 'active', 'until': until,
            'days': max(0, (until - now).days + 1) if ok and until else 0, 'unlimited': business.is_unlimited,
            'fee': monthly_fee(kind_of(business)), 'currency': fee_currency(), 'pending': pending}


def require_access(business):
    if not standing(business)['ok']:
        raise PartnerError(f'Your {"free trial" if business.subscription_status != "active" else "month"} has ended. '
                           f'Pay {money(business, monthly_fee(kind_of(business)))} for the next month to keep buying.')


def request_month(business, months=1):
    """Ask to pay the monthly fee; the platform confirms it like any subscription (Platform console › Subscriptions)."""
    from .models import Subscription
    from .subscriptions import PLAN_DAYS
    months = 12 if int(months or 1) >= 12 else (3 if int(months or 1) >= 3 else 1)
    plan = {1: '1 Month', 3: '3 Months', 12: '1 Year'}[months]
    if plan not in PLAN_DAYS:
        plan = '1 Month'
    if business.subscriptions.filter(payment_status='Pending', plan=plan).exists():
        raise PartnerError('That payment is already waiting for TapTap to confirm it.')
    amount = monthly_fee(kind_of(business)) * months
    return Subscription.objects.create(business=business, plan=plan, amount=amount, currency=fee_currency(), country=business.country,
                                       payment_method='Manual', payment_status='Pending', tier='')


# ─────────────────────────────── signing up ───────────────────────────────

def setup_account(business, kind, area='', join_code=''):
    """Right after a partner account is created: its kind, trial, profile — and an application when the agent
    came through an owner's "join my agent network" link."""
    from .models_partners import AgentProgram, PartnerProfile
    business.account_kind = kind
    business.trial_ends_at = timezone.now() + timedelta(days=trial_days())
    business.feature_tier = 'free'          # a partner has no Wi-Fi tools; the free tier keeps every module off
    business.save(update_fields=['account_kind', 'trial_ends_at', 'feature_tier'])
    PartnerProfile.objects.update_or_create(business=business, defaults={'area': (area or '').strip()[:120]})
    if kind == 'agent' and join_code:
        prog = AgentProgram.objects.filter(invite_code=str(join_code).upper()[:12], open=True).select_related('business').first()
        if prog:
            try:
                apply(business, prog.business, 'Joined with your invitation link.')
            except PartnerError:
                pass


# ─────────────────────────────── owners' programmes and the directory ───────────────────────────────

def program(owner, create=True):
    from .models_partners import AgentProgram
    p = AgentProgram.objects.filter(business=owner).first()
    if p is None and create:
        p, _ = AgentProgram.objects.get_or_create(business=owner)
    return p


def program_plans(prog):
    """Plans an agent may buy from this owner: the ticked ones, else every open plan with a price."""
    from .voucher_plan_lock import open_plans
    qs = open_plans(prog.business.plans.filter(active=True, price__gt=0).exclude(name__startswith='*'))
    ticked = prog.plans.all() if prog.pk else None
    if ticked is not None and ticked.exists():
        qs = qs.filter(pk__in=ticked.values('pk'))
    return qs.order_by('price', 'name')


def unit_cost(price, discount_percent=None, agent=None):
    """What the agent pays for one voucher of ``price`` — the same rounding as the commission on the owner's sale."""
    from .finance import commission_for
    price = Decimal(str(price))
    if agent is not None:
        return price - commission_for(agent, price)
    off = (price * Decimal(str(discount_percent or 0)) / Decimal('100')).quantize(Decimal('0.01'))
    return price - off


def directory(agent_business, q=''):
    """Wi-Fi networks open to agents, nearest-sounding first: areas matching the agent's area or the search."""
    from .models_partners import AgentLink, AgentProgram
    progs = (AgentProgram.objects.filter(open=True, business__account_kind='wifi')
             .exclude(business=agent_business).select_related('business'))
    country = getattr(agent_business, 'country', '')
    if country:
        progs = progs.filter(business__country=country)
    words = [w for w in (q or getattr(getattr(agent_business, 'partner_profile', None), 'area', '') or '').replace(',', ' ').split() if len(w) > 2]
    links = {l.owner_id: l for l in AgentLink.objects.filter(agent_account=agent_business)}
    out = []
    for p in progs[:200]:
        b = p.business
        hay = f'{p.areas} {b.business_name} {b.wifi_ssid}'.lower()
        score = sum(1 for w in words if w.lower() in hay)
        if q and not score:
            continue
        plans = list(program_plans(p)[:6])
        if not plans:
            continue
        rows = [{'plan': x, 'cost': unit_cost(x.price, p.discount_percent), 'profit': x.price - unit_cost(x.price, p.discount_percent)}
                for x in plans]
        agents = b.agent_network.filter(status='active').count()
        online = b.routers.filter(status='Online').count()
        out.append({'program': p, 'owner': b, 'score': score, 'plans': rows, 'agents': agents, 'online': online,
                    'routers': b.routers.count(), 'link': links.get(b.pk),
                    'example': _example(p, plans[0]) if plans else None})
    out.sort(key=lambda r: (-r['score'], -r['online'], -r['agents'], r['owner'].business_name.lower()))
    return out


def _example(prog, plan):
    """"Pay D2,000 → resell D2,500": a round example near the owner's minimum top-up."""
    cost = unit_cost(plan.price, prog.discount_percent)
    if cost <= 0:
        return None
    target = max(prog.min_topup, Decimal('1000'))
    qty = int((target / cost).to_integral_value(rounding=ROUND_DOWN)) or 1
    return {'qty': qty, 'pay': cost * qty, 'worth': plan.price * qty, 'profit': (plan.price - cost) * qty, 'plan': plan}


# ─────────────────────────────── applying and deciding ───────────────────────────────

def _tell(business, subject, body, link, key=None):
    try:
        from .notify import notify
        notify(business, 'agent_network', subject, body, link=link, key=key)
    except Exception:
        logger.exception('agent network notification')


def _log(business, what, details):
    try:
        from .utils import log
        log(business, what, details[:255])
    except Exception:
        pass


@transaction.atomic
def apply(agent_business, owner, message=''):
    from .models_partners import AgentLink
    if kind_of(agent_business) != 'agent':
        raise PartnerError('Only agent accounts can apply to sell for a Wi-Fi network.')
    require_access(agent_business)
    prog = program(owner, create=False)
    if not prog or not prog.open or kind_of(owner) != 'wifi':
        raise PartnerError('This Wi-Fi network is not taking agents right now.')
    link, made = AgentLink.objects.select_for_update().get_or_create(
        agent_account=agent_business, owner=owner, defaults={'message': str(message or '').strip()[:300]})
    if not made:
        if link.status in ('active', 'pending'):
            return link
        if link.status == 'paused':
            raise PartnerError('The owner paused you on this network. Talk to them to be resumed.')
        link.status, link.message, link.created_at, link.decided_at = 'pending', str(message or '').strip()[:300], timezone.now(), None
        link.save(update_fields=['status', 'message', 'created_at', 'decided_at'])
    area = getattr(getattr(agent_business, 'partner_profile', None), 'area', '')
    _tell(owner, f'New agent wants to sell your Wi-Fi: {agent_business.business_name}',
          f'{agent_business.business_name}{f" from {area}" if area else ""} ({agent_business.phone}) applied to resell your vouchers, '
          f'buying them upfront. Accept them with your discount, or decline.' + (f' They wrote: “{link.message}”' if link.message else ''),
          reverse('agent_network') + '#applications', key=f'agent-apply-{link.pk}')
    return link


@transaction.atomic
def accept(link_id, owner, discount_percent=None, note='', user=None):
    from .models import Agent
    from .models_partners import AgentLink
    link = AgentLink.objects.select_for_update().select_related('agent_account', 'agent').get(pk=link_id, owner=owner)
    if link.status == 'active':
        return link
    prog = program(owner)
    pct = prog.discount_percent if discount_percent in (None, '') else dec(discount_percent, 'discount')
    if not Decimal('0') <= pct < Decimal('100'):
        raise PartnerError('The discount must be between 0 and 99 %.')
    a = link.agent_account
    area = getattr(getattr(a, 'partner_profile', None), 'area', '')
    agent = link.agent
    if agent is None:
        agent = Agent.objects.create(business=owner, name=f'{a.business_name}'[:110] + ' · TapTap agent', phone=a.phone[:60],
                                     location=area[:160], commission_enabled=True, commission_mode='percent',
                                     commission_percent=pct, active=True)
    else:
        Agent.objects.filter(pk=agent.pk).update(active=True, commission_enabled=True, commission_mode='percent', commission_percent=pct)
    link.agent, link.status, link.discount_percent = agent, 'active', pct
    link.owner_note, link.decided_at = str(note or '').strip()[:300], timezone.now()
    link.save(update_fields=['agent', 'status', 'discount_percent', 'owner_note', 'decided_at'])
    _log(owner, 'Agent accepted', f'{a.business_name} sells your vouchers, buying upfront with {pct:g}% off')
    _tell(a, f'{owner.business_name} accepted you as their agent',
          f'You can now buy their vouchers {pct:g}% below the price customers pay, and resell them. Ask for your first top-up.'
          + (f' {owner.business_name}: “{link.owner_note}”' if link.owner_note else ''),
          reverse('partner_owner', args=[owner.pk]))
    return link


@transaction.atomic
def set_status(link_id, owner, status, note=''):
    """Owner: decline an application, pause or resume an agent."""
    from .models import Agent
    from .models_partners import AgentLink
    link = AgentLink.objects.select_for_update().select_related('agent_account').get(pk=link_id, owner=owner)
    if status not in ('declined', 'paused', 'active'):
        raise PartnerError('Unknown choice.')
    if status == 'active' and link.status != 'paused':
        raise PartnerError('Only a paused agent can be resumed.')
    link.status, link.owner_note, link.decided_at = status, str(note or '').strip()[:300], timezone.now()
    link.save(update_fields=['status', 'owner_note', 'decided_at'])
    if link.agent_id:
        Agent.objects.filter(pk=link.agent_id).update(active=(status == 'active'))
    if status in ('declined', 'paused'):
        for t in link.topups.filter(status__in=('requested', 'approved')):
            t.status, t.note = 'cancelled', f'The owner {"declined" if status == "declined" else "paused"} you.'
            t.save(update_fields=['status', 'note', 'updated_at'])
    words = {'declined': 'declined your application', 'paused': 'paused you as their agent', 'active': 'resumed you as their agent'}[status]
    _tell(link.agent_account, f'{owner.business_name} {words}', (f'They wrote: “{link.owner_note}”. ' if link.owner_note else '')
          + ('Vouchers you already bought keep working.' if status != 'active' else 'You can ask for top-ups again.'),
          reverse('partner_home'))
    return link


@transaction.atomic
def leave(link_id, agent_business):
    from .models import Agent
    from .models_partners import AgentLink
    link = AgentLink.objects.select_for_update().get(pk=link_id, agent_account=agent_business)
    if link.topups.filter(status__in=('claimed', 'review')).exists():
        raise PartnerError('A payment of yours is still being checked by the owner. Wait for it first.')
    link.topups.filter(status__in=('requested', 'approved')).update(status='cancelled', note='You left this network.')
    link.status, link.decided_at = 'left', timezone.now()
    link.save(update_fields=['status', 'decided_at'])
    if link.agent_id:
        Agent.objects.filter(pk=link.agent_id).update(active=False)
    _tell(link.owner, f'{agent_business.business_name} stopped selling your Wi-Fi', 'Vouchers they already bought keep working.',
          reverse('agent_network'))
    return link


# ─────────────────────────────── top-ups ───────────────────────────────

def quote(link, plan, amount):
    """How many vouchers ``amount`` buys: {'qty', 'cost', 'pay', 'worth', 'profit'}."""
    cost = unit_cost(plan.price, agent=link.agent) if link.agent_id else unit_cost(plan.price, link.discount_percent)
    if cost <= 0:
        raise PartnerError('This plan has no price.')
    qty = int((Decimal(str(amount)) / cost).to_integral_value(rounding=ROUND_DOWN))
    return {'qty': qty, 'cost': cost, 'pay': cost * qty, 'worth': plan.price * qty, 'profit': (plan.price - cost) * qty}


@transaction.atomic
def request_topup(link, plan_id, amount, note=''):
    from .models_partners import AgentLink, AgentTopUp
    link = AgentLink.objects.select_for_update().select_related('owner', 'agent_account', 'agent').get(pk=link.pk)
    if link.status != 'active':
        raise PartnerError('The owner has not accepted you yet.' if link.status == 'pending' else 'You cannot buy from this network now.')
    require_access(link.agent_account)
    prog = program(link.owner)
    plan = program_plans(prog).filter(pk=plan_id if str(plan_id or '').isdigit() else 0).first()
    if not plan:
        raise PartnerError('Choose one of the plans this owner sells to agents.')
    amount = dec(amount)
    if amount < prog.min_topup:
        raise PartnerError(f'The smallest top-up here is {money(link.owner, prog.min_topup)}.')
    if amount > prog.max_topup:
        raise PartnerError(f'The largest top-up here is {money(link.owner, prog.max_topup)}.')
    if link.topups.filter(status__in=AgentTopUp.OPEN).count() >= 3:
        raise PartnerError('You already have 3 top-ups in progress here. Finish or cancel one first.')
    q = quote(link, plan, amount)
    if q['qty'] < 1:
        raise PartnerError(f'That is not enough for one voucher ({money(link.owner, q["cost"])}).')
    if q['qty'] > MAX_VOUCHERS:
        raise PartnerError(f'That is more than {MAX_VOUCHERS} vouchers at once — ask for less.')
    t = AgentTopUp.objects.create(link=link, owner=link.owner, plan=plan, plan_name=plan.name, face_price=plan.price,
                                  unit_cost=q['cost'], requested_amount=amount, note=str(note or '').strip()[:255])
    if prog.auto_approve and prog.auto_approve_limit and amount <= prog.auto_approve_limit:
        approve(t.pk, link.owner, amount=amount, auto=True)
        t.refresh_from_db()
        return t
    _tell(link.owner, f'Top-up request: {link.agent_account.business_name} wants {money(link.owner, amount)} of {plan.name}',
          f'That is {q["qty"]} vouchers ({money(link.owner, q["worth"])} at your prices) for {money(link.owner, q["pay"])}. '
          'Accept it, lower the amount, or decline. The batch is made by itself once they pay.',
          reverse('agent_network') + '#topups', key=f'topup-{t.pk}')
    return t


def _topup(ref_or_id, **filters):
    from .models_partners import AgentTopUp
    qs = AgentTopUp.objects.select_for_update().select_related('link', 'link__agent', 'link__agent_account', 'owner', 'plan')
    key = {'pk': ref_or_id} if isinstance(ref_or_id, int) else {'reference': ref_or_id}
    return qs.get(**key, **filters)


@transaction.atomic
def approve(topup_id, owner, amount=None, router_id=None, note='', auto=False):
    t = _topup(topup_id, owner=owner)
    if t.status != 'requested':
        raise PartnerError('This request was already answered.')
    if t.link.status != 'active':
        raise PartnerError('This agent is not active on your network.')
    amount = t.requested_amount if amount in (None, '') else dec(amount)
    if amount > t.requested_amount:
        raise PartnerError(f'You can accept the amount asked ({money(owner, t.requested_amount)}) or less, not more.')
    if t.plan is None:
        raise PartnerError('That plan no longer exists.')
    q = quote(t.link, t.plan, amount)
    if q['qty'] < 1:
        raise PartnerError('That amount does not buy a single voucher.')
    prog = program(owner)
    router = owner.routers.filter(pk=router_id).first() if str(router_id or '').isdigit() else None
    router = router or prog.router or owner.routers.order_by('pk').first()
    t.quantity, t.unit_cost, t.amount, t.router = q['qty'], q['cost'], q['pay'], router
    t.status, t.approved_at = 'approved', timezone.now()
    if note:
        t.note = str(note).strip()[:255]
    t.save(update_fields=['quantity', 'unit_cost', 'amount', 'router', 'status', 'approved_at', 'note', 'updated_at'])
    lowered = q['pay'] < t.requested_amount and amount < t.requested_amount
    _tell(t.link.agent_account, f'{owner.business_name} {"approved" if not lowered else "approved a smaller"} top-up — pay {money(owner, t.amount)}',
          f'{t.quantity} vouchers of {t.plan_name} ({money(owner, t.face_value)} at the shop price) for {money(owner, t.amount)}. '
          f'Pay within {PAY_HOURS} hours; your batch is made as soon as the payment is confirmed.'
          + (f' {owner.business_name}: “{t.note}”' if note else '') + (' (approved automatically)' if auto else ''),
          reverse('partner_topup', args=[t.reference]), key=f'topup-ok-{t.pk}')
    return t


@transaction.atomic
def decline(topup_id, owner, why=''):
    t = _topup(topup_id, owner=owner)
    if t.status not in ('requested', 'approved', 'review'):
        raise PartnerError('This top-up cannot be declined now.')
    if t.status == 'review' or t.paid_at:
        raise PartnerError('A payment was made for this top-up: confirm it, or settle it with the agent first.')
    t.status, t.note = 'declined', str(why or '').strip()[:255]
    t.save(update_fields=['status', 'note', 'updated_at'])
    _tell(t.link.agent_account, f'{owner.business_name} declined your top-up {t.reference}', t.note or 'No reason given.',
          reverse('partner_topup', args=[t.reference]))
    return t


@transaction.atomic
def cancel(reference, agent_business):
    t = _topup(reference, link__agent_account=agent_business)
    if t.status not in ('requested', 'approved'):
        raise PartnerError('Only a top-up you have not paid can be cancelled.')
    t.status = 'cancelled'
    t.save(update_fields=['status', 'updated_at'])
    return t


def payment_options(t):
    """How the agent can pay this top-up: [{'method', 'label', 'wallet', 'auto'}]."""
    from .market import wallets
    out = []
    for w in wallets(t.owner):
        out.append({'method': w.provider, 'label': w.get_provider_display(), 'wallet': w,
                    'auto': w.provider == 'wave' and getattr(w, 'automatic', False)})
    if program(t.owner).take_cash:
        out.append({'method': 'cash', 'label': 'Cash', 'wallet': None, 'auto': False})
    return out


@transaction.atomic
def claim(reference, agent_business, method, payment_ref=''):
    """The agent says they paid (mobile money with its transaction ID, or cash handed over)."""
    t = _topup(reference, link__agent_account=agent_business)
    if t.status != 'approved':
        raise PartnerError('This top-up is not waiting for a payment.')
    allowed = {o['method'] for o in payment_options(t)}
    if method not in allowed:
        raise PartnerError('Choose one of the ways this owner takes payments.')
    ref = str(payment_ref or '').strip()[:80]
    if method != 'cash' and len(ref) < 4:
        raise PartnerError('Type the transaction ID from your payment message.')
    t.status, t.method, t.payment_ref, t.claimed_at, t.route = 'claimed', method, ref, timezone.now(), 'confirm'
    t.save(update_fields=['status', 'method', 'payment_ref', 'claimed_at', 'route', 'updated_at'])
    how = 'will pay cash' if method == 'cash' else f'paid your {dict(t.METHODS).get(method, method)} (transaction {ref})'
    _tell(t.owner, f'{agent_business.business_name} {how} — {money(t.owner, t.amount)} for {t.reference}',
          f'Confirm when the money is with you: the batch of {t.quantity} {t.plan_name} is then made for them automatically.',
          reverse('agent_network') + '#topups', key=f'topup-claim-{t.pk}')
    return t


@transaction.atomic
def confirm_paid(topup_id, owner, user=None, method=''):
    """The owner has the money (cash in hand, or seen in their wallet) — the batch is made now.
    ``method`` lets the owner take cash for an approved top-up the agent never marked (they paid at the counter)."""
    t = _topup(topup_id, owner=owner)
    if t.status == 'approved' and method:
        t.method, t.route = method if method in dict(t.METHODS) else 'cash', 'confirm'
    elif t.status not in ('claimed', 'review'):
        raise PartnerError('This top-up is not waiting for your confirmation.')
    t.paid_at = timezone.now()
    t.save(update_fields=['method', 'route', 'paid_at', 'updated_at'])
    return issue(t, user)


def issue(t, user=None):
    """Paid: make the batch for the agent, bought upfront (inside the caller's transaction)."""
    from .utils import code_format_from_post
    from .voucher_make import make_batch
    if t.status == 'issued':
        return t
    if t.plan is None:
        t.status, t.error = 'review', 'The plan was deleted: make the batch by hand.'
        t.save(update_fields=['status', 'error', 'updated_at'])
        return t
    owner = t.owner
    fmt = code_format_from_post({}, owner)
    try:
        batch, notes = make_batch(owner, t.plan, t.quantity, fmt, router=t.router,
                                  name=f'{t.link.agent_account.business_name[:60]} · {t.reference}',
                                  note=f'TapTap agent top-up {t.reference}', agent=t.link.agent, settlement='prepaid',
                                  pay_method=t.method or 'cash', reference=t.reference, user=user)
    except Exception as exc:          # a locked plan, a code format problem…: the money is in, a person must look
        logger.warning('Top-up %s: batch not made: %s', t.reference, exc)
        t.status, t.error = 'review', f'Paid, but the batch could not be made: {exc}'[:255]
        t.save(update_fields=['status', 'error', 'updated_at'])
        _tell(owner, f'Top-up {t.reference} is paid but needs you', t.error, reverse('agent_network') + '#topups')
        return t
    t.batch, t.status, t.issued_at, t.error = batch, 'issued', timezone.now(), ''
    if not t.paid_at:
        t.paid_at = t.issued_at
    t.save(update_fields=['batch', 'status', 'issued_at', 'paid_at', 'error', 'updated_at'])
    _log(owner, 'Agent top-up', f'{t.reference}: {t.quantity}× {t.plan_name} bought by {t.link.agent_account.business_name} '
                                f'for {money(owner, t.amount)}')
    _tell(t.link.agent_account, f'Your {t.quantity} vouchers of {t.plan_name} are ready',
          f'{owner.business_name} received {money(owner, t.amount)}. Your batch is in your portal: share or print the codes and '
          f'resell them at {money(owner, t.face_price)} each.', reverse('partner_batch', args=[t.reference]))
    return t


# ─────────────────────────────── Wave ───────────────────────────────

def checkout(reference, agent_business, request):
    """Pay an approved top-up with Wave — on the owner's own Wave account, or TapTap's (paid out to the owner)."""
    from .market import best_wallet
    from .market_wave import checkout_provider
    from .models_partners import AgentTopUp
    t = AgentTopUp.objects.select_related('owner', 'link').get(reference=reference, link__agent_account=agent_business)
    if t.status != 'approved':
        raise PartnerError('This top-up is not waiting for a payment.')
    wallet = best_wallet(t.owner, 'wave')
    if not wallet or not getattr(wallet, 'automatic', False):
        raise PartnerError("This owner takes Wave by hand: send to their number and type the transaction ID.")
    provider = checkout_provider(wallet.route, wallet)
    page = request.build_absolute_uri(reverse('partner_topup', args=[t.reference]))
    provider.return_urls = lambda thing, req: (page, page + '?payment=error')
    AgentTopUp.objects.filter(pk=t.pk).update(route=wallet.route, currency=provider.iso_currency, method='wave')
    t.refresh_from_db()
    try:
        return provider.create_checkout(t, request)
    except Exception as exc:
        logger.warning('Top-up checkout %s failed: %s', t.reference, exc)
        raise PartnerError('Wave could not start the payment. Try again in a moment, or pay by hand.')


@transaction.atomic
def settle(event, wallet=None, route='own'):
    """A verified Wave event for a top-up (references TOP-…), from the owner's Wave or TapTap's."""
    from .market import _check_event, best_wallet
    from .models_partners import AgentTopUp
    ref = str(event.get('reference') or '').strip()
    qs = AgentTopUp.objects.select_for_update().select_related('link', 'link__agent', 'link__agent_account', 'owner', 'plan').filter(reference=ref, route=route)
    if wallet is not None:
        qs = qs.filter(owner=wallet.business)
    t = qs.first()
    if not t:
        raise PartnerError('Unknown top-up reference.')
    if t.status == 'issued':
        return t                                   # Wave retries are harmless
    why = _check_event(t, event)
    t.provider_payload = event.get('raw') or {}
    if why == 'not paid':
        t.save(update_fields=['provider_payload', 'updated_at'])
        return t
    if why:
        t.status, t.error = 'review', ('Wave: ' + why)[:255]
        t.save(update_fields=['status', 'error', 'provider_payload', 'updated_at'])
        return t
    t.payment_ref, t.paid_at, t.method = str(event.get('transaction_id') or '')[:80], timezone.now(), 'wave'
    t.save(update_fields=['payment_ref', 'paid_at', 'method', 'provider_payload', 'updated_at'])
    issue(t)
    if route == 'platform':
        w = best_wallet(t.owner, 'wave')
        if w:
            from .market_wave import queue_payout
            queue_payout(t.owner, w, t.amount, t.currency)
    t.refresh_from_db()
    return t


# ─────────────────────────────── housekeeping ───────────────────────────────

def sweep(now=None):
    """Approved top-ups nobody paid in time expire."""
    from .models_partners import AgentTopUp
    now = now or timezone.now()
    return (AgentTopUp.objects.filter(status='approved', approved_at__lt=now - timedelta(hours=PAY_HOURS))
            .update(status='expired', note='Not paid in time.', updated_at=now))


# ─────────────────────────────── what an agent holds ───────────────────────────────

def batch_stock(t):
    """Vouchers of a top-up's batch: total, used, still unused, resold (the agent's own notes)."""
    if not t.batch_id:
        return {'total': 0, 'used': 0, 'left': 0, 'resold': 0, 'pct': 0}
    qs = t.batch.vouchers.all()
    total = qs.count()
    used = qs.filter(used_at__isnull=False).count()
    resold = qs.filter(Q(agent_retail__isnull=False) | Q(used_at__isnull=False)).count()
    return {'total': total, 'used': used, 'left': total - used, 'resold': resold, 'pct': int(round(100 * used / total)) if total else 0}


def agent_summary(agent_business):
    from .models_partners import AgentTopUp
    issued = AgentTopUp.objects.filter(link__agent_account=agent_business, status='issued')
    agg = issued.aggregate(spent=Sum('amount'), n=Count('id'))
    worth = sum((t.face_price * t.quantity for t in issued), Decimal('0'))
    left_value = Decimal('0')
    for t in issued.select_related('batch'):
        s = batch_stock(t)
        left_value += t.face_price * s['left']
    return {'spent': agg['spent'] or Decimal('0'), 'batches': agg['n'], 'worth': worth, 'profit': worth - (agg['spent'] or 0),
            'left_value': left_value}


def owner_counts(owner):
    """Badges for the owner's Agent network link."""
    from .models_partners import AgentLink, AgentTopUp
    return {'applications': AgentLink.objects.filter(owner=owner, status='pending').count(),
            'topups': AgentTopUp.objects.filter(owner=owner, status__in=('requested', 'claimed', 'review')).count()}


# ─────────────────────────────── the gate: partners see their portal, not the Wi-Fi dashboard ───────────────────────────────

OPEN_NAMES = {'home', 'login', 'logout', 'register', 'verify_code', 'resend_code', 'cancel_verification', 'account_password',
              'business_switch', 'team_leave', 'tiklab', 'tiklab_download', 'tiklab_latest', 'app_download', 'app_download_android',
              'landing_ad_click', 'landing_ad_image', 'agent_join'}
RESELLER_NAMES = {'market', 'market_setup', 'market_listing', 'market_listing_reserve', 'market_deal', 'market_order_action',
                  'market_shop', 'market_order'}
SKIP = ('/static/', '/media/', '/api/', '/p/', '/admin/', '/ag/', '/b/', '/c/', '/member/')
PAYING = {'market_listing_reserve'}      # needs trial or paid time


class PartnerGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        r = self.gate(request)
        return r if r is not None else self.get_response(request)

    @staticmethod
    def gate(request):
        user = getattr(request, 'user', None)
        if not (user and user.is_authenticated) or any(request.path.startswith(p) for p in SKIP):
            return None
        business = getattr(request, 'tt_business', None)
        if business is None:
            return None
        from django.urls import Resolver404, resolve
        try:
            name = resolve(request.path_info).url_name or ''
        except Resolver404:
            return None
        kind = kind_of(business)
        if kind == 'wifi':
            if name.startswith('partner_'):
                from django.shortcuts import redirect
                return redirect('dashboard')
            return None
        if name.startswith('partner_') or name in OPEN_NAMES or name.startswith('platform_'):
            return None
        if kind == 'reseller' and name in RESELLER_NAMES:
            if request.method == 'POST' and name in PAYING and not standing(business)['ok']:
                from django.contrib import messages
                from django.shortcuts import redirect
                messages.error(request, 'Your free trial has ended. Pay the monthly fee to buy new batches.')
                return redirect('partner_billing')
            return None
        accept = request.headers.get('Accept', '')
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or (accept and 'text/html' not in accept and '*/*' not in accept):
            from django.http import JsonResponse
            return JsonResponse({'ok': False, 'error': 'Not part of your account.'}, status=403)
        from django.shortcuts import redirect
        return redirect('partner_home')


def base_template(request):
    """The page frame: the partner portal for agents and resellers, the Wi-Fi dashboard for everyone else."""
    business = getattr(request, 'tt_business', None)
    return 'core/partner/base.html' if business is not None and is_partner(business) else 'core/base.html'


# ─────────────────────────────── install ───────────────────────────────

def _settle_hook():
    """Wave webhooks for the Market (the owner's own and TapTap's) also carry top-up payments (references TOP-…)."""
    from functools import wraps
    from . import market
    original = market.settle
    if getattr(original, '_tt_topup', False):
        return

    @wraps(original)
    def settle_any(event, wallet=None, route='own'):
        if str(event.get('reference') or '').startswith('TOP-'):
            try:
                return settle(event, wallet=wallet, route=route)
            except PartnerError as e:
                raise market.MarketError(str(e))
        return original(event, wallet=wallet, route=route)
    settle_any._tt_topup = True
    market.settle = settle_any


def _platform_hook():
    """TapTap's own Wave webhook (Online sales) passes TOP-… references on, like the Market's MKT-…."""
    from functools import wraps
    from . import online_buy
    original = online_buy.settle_provider_event
    if getattr(original, '_tt_topup', False):
        return

    @wraps(original)
    def settle_provider_event(provider_code, event):
        if str(event.get('reference') or '').startswith('TOP-'):
            return settle(event, route='platform')
        return original(provider_code, event)
    settle_provider_event._tt_topup = True
    online_buy.settle_provider_event = settle_provider_event


def install():
    from django.urls import path
    from . import middleware, notify, permissions, team, urls
    from .views_partners import URLS
    names = {getattr(x, 'name', None) for x in urls.urlpatterns}
    for route, view, name in URLS:
        if name not in names:
            urls.urlpatterns.append(path(route, view, name=name))
        if name == 'agent_join':
            permissions.MEMBER_ALLOWED.add(name)
            team.PLATFORM_OPEN.add(name)
        elif name == 'agent_network':
            permissions.URL_PERMS[name] = ('finance.agents', 'agents.manage', 'finance.view')
        else:
            permissions.URL_PERMS[name] = ('dashboard.view',)     # partner pages: the account owner (team members never land here)
        middleware.SubscriptionAccessMiddleware.ALLOWED.add(name)
    notify.EVENTS.setdefault('agent_network', ('Agents and top-ups', 'An agent applied to sell your Wi-Fi, asked for a top-up or paid; '
                                               'for agents: an owner answered, approved a top-up or your batch is ready.',
                                               'instant', 'warning', 'Business'))
    _settle_hook()
    _platform_hook()
