"""Bonanza engine — who can spin, picking a prize fairly, paying out.

Who can spin
    A voucher from one of the Bonanza's plans or batches (either list; both empty = none),
    while the Bonanza is live and inside its dates. With ``require_sold`` it must have been
    sold or used. When the Bonanza is shared with agents, only vouchers held by those agents
    take part. Each voucher gets ``spins_per_voucher`` spins.

Picking a prize fairly
    The server picks — never the browser. Each prize still in stock has a chance equal to
    its weight divided by the total weight. ``secrets.SystemRandom`` is used (not the
    predictable ``random``), and prize rows are locked while picking so two customers can
    never both win the last one.

Paying out, depending on the prize
    * Free Wi-Fi voucher — a new voucher of the prize plan is created at once (price 0,
      never a sale) on the same router as the winning voucher.
    * Extra time — added straight to the winning voucher (it continues from where it is).
    * Cash / airtime and gifts — the customer gets a claim code; staff or an agent confirm
      the payout in TapTap.
"""
from __future__ import annotations

import secrets
import string
from dataclasses import dataclass

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import Bonanza, BonanzaPrize, BonanzaSpin, Voucher

RNG = secrets.SystemRandom()
PALETTE = ['#f59e0b', '#1769e0', '#18a66a', '#e5484d', '#7c3aed', '#0ea5a4', '#f97316', '#db2777', '#65a30d', '#2563eb']


class BonanzaError(ValueError):
    """A spin that cannot happen, with a message the customer can read."""


# ─────────────────────────────── state ───────────────────────────────

def is_open(bonanza, now=None):
    now = now or timezone.now()
    if bonanza.status != 'live':
        return False
    if bonanza.starts_at and now < bonanza.starts_at:
        return False
    if bonanza.ends_at and now >= bonanza.ends_at:
        return False
    return True


def closed_reason(bonanza, now=None):
    now = now or timezone.now()
    if bonanza.status == 'draft':
        return 'This Bonanza has not started yet.'
    if bonanza.status == 'paused':
        return 'This Bonanza is paused for a moment. Try again later.'
    if bonanza.status == 'ended' or (bonanza.ends_at and now >= bonanza.ends_at):
        return 'This Bonanza has ended. Thank you for playing!'
    if bonanza.starts_at and now < bonanza.starts_at:
        return f'This Bonanza starts {timezone.localtime(bonanza.starts_at):%d %b %Y at %H:%M}.'
    return ''


def wheel_prizes(bonanza):
    """Prizes shown on the wheel, in order (sold-out 'remove' prizes leave the wheel)."""
    out = []
    for i, p in enumerate(bonanza.prizes.filter(active=True)):
        if p.out and p.when_out == 'remove':
            continue
        out.append(p)
    return out


def chances(bonanza):
    """[(prize, percent)] of winning each prize right now."""
    live = [p for p in wheel_prizes(bonanza) if not p.out and p.weight > 0]
    total = sum(p.weight for p in live) or 1
    return [(p, round(p.weight * 100 / total, 1) if p in live else 0.0) for p in wheel_prizes(bonanza)]


def wheel_json(bonanza):
    items = []
    for i, p in enumerate(wheel_prizes(bonanza)):
        items.append({'id': p.pk, 'label': p.label, 'color': p.color or PALETTE[i % len(PALETTE)], 'out': p.out})
    return items


# ─────────────────────────────── who can spin ───────────────────────────────

@dataclass
class Eligibility:
    voucher: Voucher | None
    spins_left: int
    reason: str = ''

    @property
    def ok(self):
        return self.voucher is not None and self.spins_left > 0 and not self.reason


def clean_code(code):
    return ''.join(ch for ch in str(code or '') if ch.isalnum())


def eligibility(bonanza, code, now=None):
    """Can this voucher code spin? Never says whether an unknown code exists elsewhere."""
    now = now or timezone.now()
    if not is_open(bonanza, now):
        return Eligibility(None, 0, closed_reason(bonanza, now))
    code = clean_code(code)
    if len(code) < 4:
        return Eligibility(None, 0, 'Type the code printed on your voucher.')
    v = bonanza.business.vouchers.filter(code__iexact=code).select_related('batch', 'agent', 'router').first()
    if not v:
        from .models import VoucherCodeAlias
        alias = VoucherCodeAlias.objects.filter(business=bonanza.business, code__iexact=code).select_related('voucher').first()
        v = alias.voucher if alias and not alias.voucher.deleted_at else None
    if not v:
        return Eligibility(None, 0, 'That code was not recognised. Check it and try again.')
    plan_names = set(bonanza.plans.values_list('name', flat=True))
    batch_ids = set(bonanza.batches.values_list('id', flat=True))
    if not ((v.plan_name in plan_names) or (v.batch_id in batch_ids)):
        return Eligibility(None, 0, 'This voucher does not take part in this Bonanza.')
    agent_ids = set(bonanza.agents.values_list('id', flat=True))
    if agent_ids and v.agent_id not in agent_ids:
        return Eligibility(None, 0, 'This voucher does not take part in this Bonanza.')
    if bonanza.require_sold and not (v.sold_at or v.used_at):
        return Eligibility(None, 0, 'This voucher has not been sold yet. Buy it first, then come back to spin.')
    used = bonanza.spins.filter(voucher=v).count()
    left = max(0, bonanza.spins_per_voucher - used)
    if not left:
        return Eligibility(v, 0, 'This voucher has already used its spin' + ('s' if bonanza.spins_per_voucher > 1 else '') + '.')
    return Eligibility(v, left)


# ─────────────────────────────── picking a prize ───────────────────────────────

def pick(prizes):
    """Weighted, unpredictable choice among prizes still in stock."""
    live = [p for p in prizes if p.active and p.weight > 0 and not p.out]
    if not live:
        return None
    total = sum(p.weight for p in live)
    roll = RNG.uniform(0, total)
    upto = 0
    for p in live:
        upto += p.weight
        if roll < upto:
            return p
    return live[-1]


def _claim_code():
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    for _ in range(20):
        c = ''.join(secrets.choice(alphabet) for _ in range(6))
        if not BonanzaSpin.objects.filter(claim_code=c).exists():
            return c
    return ''.join(secrets.choice(string.digits) for _ in range(10))


def spin(bonanza, code, ip='', via_agent=None, now=None):
    """Spin once for this voucher. Returns the BonanzaSpin (payout already attempted)."""
    now = now or timezone.now()
    with transaction.atomic():
        # Lock the Bonanza row: one spin at a time per Bonanza, so stock and spin counts stay exact.
        b = Bonanza.objects.select_for_update().get(pk=bonanza.pk)
        el = eligibility(b, code, now)
        if not el.ok:
            raise BonanzaError(el.reason or 'You cannot spin with this voucher.')
        prizes = list(BonanzaPrize.objects.select_for_update().filter(bonanza=b, active=True).order_by('position', 'id'))
        prize = pick(prizes)
        if prize is None:
            raise BonanzaError('All prizes have been won. Thank you for playing!')
        BonanzaPrize.objects.filter(pk=prize.pk).update(won=F('won') + 1)
        prize.won += 1
        value = {'voucher': prize.plan.name if prize.plan else '', 'time': str(prize.minutes), 'cash': str(prize.amount),
                 'gift': prize.details, 'none': ''}.get(prize.kind, '')
        s = BonanzaSpin.objects.create(
            bonanza=b, voucher=el.voucher, voucher_code=el.voucher.code, prize=prize, prize_label=prize.label,
            prize_kind=prize.kind, prize_value=value, ip=ip[:64], via_agent=via_agent,
            payout='none' if prize.kind == 'none' else ('pending' if prize.kind in ('cash', 'gift') else 'done'),
            claim_code=_claim_code() if prize.kind in ('cash', 'gift') else '')
    if prize.kind in ('voucher', 'time'):
        _pay_instantly(s, prize)
    _history(s)
    return s


# ─────────────────────────────── paying out ───────────────────────────────

def _pay_instantly(s, prize):
    from .utils import generate_code
    from . import voucher_history as vh
    v = s.voucher
    try:
        if prize.kind == 'voucher':
            plan = prize.plan
            if not plan:
                raise ValueError('The prize has no plan set.')
            from .serials import allocate as _serials
            reward = Voucher.objects.create(
                business=v.business, router=v.router, code=generate_code(business=v.business), serial=_serials(v.business, 1, plan=plan.name)[0], created_by_label=f'Bonanza prize — {s.bonanza.name}'[:150], plan_name=plan.name, price=0,
                duration_minutes=plan.duration_minutes, max_devices=plan.max_devices,
                source='taptap', customer_name=v.customer_name, customer_phone=v.customer_phone,
                mikrotik_sync_status='Pending')
            s.reward_voucher = reward
            s.payout, s.paid_at = 'done', timezone.now()
            s.payout_note = f'New voucher {reward.code}'
            vh.record(reward, 'note', source='system', text=f'Bonanza prize from {s.bonanza.name} (won with {v.code})')
            if reward.router_id:
                try:
                    from .voucher_push import push_vouchers
                    push_vouchers([reward])          # just this voucher, not a full sync
                except Exception:
                    pass   # the next background sync publishes it anyway
        elif prize.kind == 'time':
            if v.frozen_at:
                raise ValueError('The voucher is frozen.')
            ok, result = vh.enable(v, reason=f'Bonanza prize: {prize.label}', add_minutes=prize.minutes)
            s.payout, s.paid_at = 'done', timezone.now()
            s.payout_note = f'+{prize.minutes} min on {v.code}' + ('' if ok else f' (router: {result})')
        s.save(update_fields=['reward_voucher', 'payout', 'paid_at', 'payout_note'])
    except Exception as exc:
        s.payout, s.claim_code = 'failed', s.claim_code or _claim_code()
        s.payout_note = f'Automatic payout failed: {exc}'[:255]
        s.save(update_fields=['payout', 'claim_code', 'payout_note'])


def mark_paid(s, user=None, agent=None, note=''):
    """Staff (or an agent) handed over the cash, airtime or gift."""
    if s.payout not in ('pending', 'failed'):
        raise BonanzaError('Nothing to pay for this spin.')
    s.payout, s.paid_at = 'paid', timezone.now()
    s.paid_by = user if getattr(user, 'is_authenticated', False) else None
    s.paid_by_agent = agent
    s.payout_note = (note or s.payout_note or '')[:255]
    s.save(update_fields=['payout', 'paid_at', 'paid_by', 'paid_by_agent', 'payout_note'])
    from . import voucher_history as vh
    if s.voucher_id:
        vh.record(s.voucher, 'note', user=user, text=f'Bonanza prize paid out: {s.prize_label}' + (f' by {agent.name}' if agent else ''))
    from .utils import log
    log(s.bonanza.business, 'Bonanza Payout', f'{s.bonanza.name}: {s.prize_label} for {s.voucher_code}' + (f' (by {agent.name})' if agent else ''))
    return s


def _history(s):
    from . import voucher_history as vh
    if s.voucher_id:
        vh.record(s.voucher, 'note', source='customer',
                  text=f'Bonanza spin ({s.bonanza.name}): ' + ('no prize' if s.prize_kind == 'none' else f'won {s.prize_label}')
                       + (f' — claim code {s.claim_code}' if s.claim_code else ''))


def result_json(s):
    """What the customer page shows after the wheel stops."""
    d = {'prize_id': s.prize_id, 'label': s.prize_label, 'kind': s.prize_kind, 'payout': s.payout}
    if s.prize_kind == 'none':
        d['message'] = 'No prize this time. Better luck next time!'
    elif s.prize_kind == 'voucher' and s.reward_voucher_id:
        rv = s.reward_voucher
        d['message'] = f'You won a free {rv.plan_name} voucher!'
        d['reward_code'] = rv.code
    elif s.prize_kind == 'time' and s.payout == 'done':
        from .durations import text
        d['message'] = f'{text(int(s.prize_value or 0))} added to your voucher {s.voucher_code}!'
    else:
        where = 'Show this claim code to the staff' + (f' or to {s.via_agent.name}' if s.via_agent_id else '') + ' to collect your prize.'
        d['message'] = f'You won {s.prize_label}! {where}'
        d['claim_code'] = s.claim_code
    return d


def stats(bonanza):
    spins = bonanza.spins.all()
    return {'spins': spins.count(), 'winners': spins.exclude(prize_kind='none').count(),
            'pending': spins.filter(payout__in=['pending', 'failed']).count(),
            'vouchers_given': spins.filter(prize_kind='voucher', payout='done').count(),
            'cash_paid': sum(float(x or 0) for x in spins.filter(prize_kind='cash', payout='paid').values_list('prize_value', flat=True))}


def live_for(business):
    """The Bonanza customers should see now (newest live one), or None."""
    for bz in business.bonanzas.filter(status='live').order_by('-created_at')[:5]:
        if is_open(bz):
            return bz
    return None


def portal_ctx(business, base=''):
    bz = live_for(business)
    return {'url': f'{base}/b/{bz.slug}/', 'name': bz.name, 'headline': bz.headline} if bz else None
