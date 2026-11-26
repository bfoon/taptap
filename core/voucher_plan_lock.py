"""Locking a voucher plan — the owner stops NEW vouchers; everything already made keeps working.

Only the business owner (the account itself — not staff, not TapTap support "viewing as") locks and reopens.

* **Locked** — no new voucher of this plan, by anyone, anywhere, until the owner reopens it — or until a date the
  owner chose, when it reopens by itself (a holiday plan, a promotion that ends).
* **Sealed** — closed for good, and the plan itself can no longer be edited or deleted, so the vouchers still
  running on it stay exactly as they were sold. The owner types the plan's name to confirm.

What keeps working: every voucher already made — unsold stock on the shelf or with agents, sold ones not used yet,
the ones running right now — until it runs out, and payments already taken (an online purchase being paid, a
batch already sold on the TapTap Market, a Bonanza prize already promised).

What stops, at every door (one rule, ``check_new`` / ``open_plans``): Generate (website and app), single vouchers
(website and app), agents ordering, the online shop, the portal's price list, Bonanza prizes being set up, Market
listings, giving a fresh member the plan, and moving other vouchers onto it. A message names the plan to use
instead, when the owner picked one.

``sunset(plan)`` tells the owner how much is still out there and when the last voucher will end.
"""
from __future__ import annotations

from django.db.models import Count, Max, Q
from django.utils import timezone

KINDS = {'locked': 'Locked', 'sealed': 'Sealed'}


class PlanClosedError(ValueError):
    """A new voucher was asked for on a locked or sealed plan."""


def is_closed(plan, now=None):
    if plan is None or not getattr(plan, 'sale_lock', ''):
        return False
    if plan.sale_lock == 'locked' and plan.sale_lock_until and plan.sale_lock_until <= (now or timezone.now()):
        return False                     # its reopening date has come
    return True


def open_q(now=None):
    now = now or timezone.now()
    return Q(sale_lock='') | Q(sale_lock='locked', sale_lock_until__lte=now)


def open_plans(qs):
    """Only the plans new vouchers may be made with."""
    return qs.filter(open_q())


def closed_plans(qs):
    return qs.exclude(open_q())


def describe(plan):
    if not is_closed(plan):
        return ''
    who = ''
    if plan.sale_locked_by_id:
        u = plan.sale_locked_by
        who = f' by {u.get_full_name() or u.username}'
    when = timezone.localtime(plan.sale_locked_at).strftime('%d %b %Y') if plan.sale_locked_at else ''
    until = (f' · reopens {timezone.localtime(plan.sale_lock_until):%d %b %Y %H:%M}'
             if plan.sale_lock == 'locked' and plan.sale_lock_until else '')
    return f'{KINDS.get(plan.sale_lock, "Locked")}{who}' + (f' on {when}' if when else '') + until + \
        (f' — “{plan.sale_lock_reason}”' if plan.sale_lock_reason else '')


def message(plan):
    rep = plan.sale_replacement if plan.sale_replacement_id else None
    instead = f' Use “{rep.name}” instead.' if rep and not is_closed(rep) and rep.deleted_at is None else ''
    state = 'sealed for good' if plan.sale_lock == 'sealed' else 'locked by the owner'
    until = (f' It reopens on {timezone.localtime(plan.sale_lock_until):%d %b %Y}.'
             if plan.sale_lock == 'locked' and plan.sale_lock_until else '')
    return f'“{plan.name}” is {state}: no new vouchers can be made with it. Vouchers already made keep working.{until}{instead}'


def check_new(plan):
    """Raise PlanClosedError if a new voucher may not be made with ``plan``."""
    if is_closed(plan):
        raise PlanClosedError(message(plan))


def check_edit(plan):
    """A sealed plan cannot be edited or deleted: its running vouchers stay exactly as sold."""
    if plan is not None and getattr(plan, 'sale_lock', '') == 'sealed':
        raise PlanClosedError(f'“{plan.name}” is sealed: it cannot be edited or deleted, so the vouchers still running on it '
                              'stay exactly as they were sold.')


# ─────────────────────────────── what is still out there ───────────────────────────────

def sunset(plan, now=None):
    """Vouchers of this plan still out there, and when the last one ends."""
    now = now or timezone.now()
    qs = plan.business.vouchers.filter(plan_name=plan.name).exclude(login_type='member')
    live = qs.filter(status__in=('active', 'disabled'))
    agg = live.aggregate(
        stock=Count('id', filter=Q(sold_at__isnull=True, used_at__isnull=True, agent__isnull=True)),
        agents=Count('id', filter=Q(sold_at__isnull=True, used_at__isnull=True, agent__isnull=False)),
        sold=Count('id', filter=Q(sold_at__isnull=False, used_at__isnull=True)),
        running=Count('id', filter=Q(used_at__isnull=False) & (Q(expires_at__isnull=True) | Q(expires_at__gt=now))),
        last=Max('expires_at', filter=Q(expires_at__gt=now)))
    waiting = agg['stock'] + agg['agents'] + agg['sold']
    total = waiting + agg['running']
    if total == 0:
        when = 'Nothing is running on it any more.'
    elif waiting:
        when = 'Unused vouchers start their time when first used, so there is no end date yet.'
    elif agg['last']:
        when = f'The last voucher ends {timezone.localtime(agg["last"]):%d %b %Y %H:%M}.'
    else:
        when = 'Some vouchers have no time limit.'
    return {**agg, 'waiting': waiting, 'total': total, 'when': when}


# ─────────────────────────────── locking and reopening ───────────────────────────────

def _owner(request, business):
    from .plan_lock import is_owner
    return is_owner(request, business)


def lock(plan, request, kind='locked', reason='', until=None, replacement=None, confirm=''):
    from .utils import log
    if not _owner(request, plan.business):
        raise PlanClosedError('Only the business owner can lock a plan.')
    if kind not in KINDS:
        raise PlanClosedError('Choose Lock or Seal.')
    if plan.sale_lock == 'sealed':
        raise PlanClosedError(f'“{plan.name}” is already sealed.')
    if kind == 'sealed' and str(confirm or '').strip().lower() != plan.name.strip().lower():
        raise PlanClosedError(f'To seal it for good, type the plan’s name exactly: {plan.name}')
    if until is not None and until <= timezone.now():
        raise PlanClosedError('The reopening date must be in the future.')
    if replacement is not None and (replacement.pk == plan.pk or replacement.business_id != plan.business_id):
        replacement = None
    plan.sale_lock, plan.sale_lock_reason = kind, str(reason or '').strip()[:200]
    plan.sale_locked_at, plan.sale_locked_by = timezone.now(), request.user
    plan.sale_lock_until = until if kind == 'locked' else None
    plan.sale_replacement = replacement
    plan.save(update_fields=['sale_lock', 'sale_lock_reason', 'sale_locked_at', 'sale_locked_by', 'sale_lock_until', 'sale_replacement'])
    s = sunset(plan)
    log(plan.business, 'Plan Locked', f'{plan.name}: {"SEALED for good" if kind == "sealed" else "locked"} by the owner — no new vouchers; '
                                      f'{s["total"]} already made keep working' + (f' ({plan.sale_lock_reason})' if plan.sale_lock_reason else ''))
    _refresh(plan.business)
    return plan


def unlock(plan, request):
    from .utils import log
    if plan.sale_lock == 'sealed':
        raise PlanClosedError(f'“{plan.name}” is sealed: it cannot be reopened, not even by the owner.')
    if not _owner(request, plan.business):
        raise PlanClosedError('Only the business owner can reopen a plan.')
    if not plan.sale_lock:
        return plan
    clear(plan)
    log(plan.business, 'Plan Reopened', f'{plan.name}: reopened by the owner — new vouchers can be made again')
    _refresh(plan.business)
    return plan


def clear(plan):
    plan.sale_lock, plan.sale_lock_reason, plan.sale_locked_at, plan.sale_locked_by = '', '', None, None
    plan.sale_lock_until, plan.sale_replacement = None, None
    plan.save(update_fields=['sale_lock', 'sale_lock_reason', 'sale_locked_at', 'sale_locked_by', 'sale_lock_until', 'sale_replacement'])


def reopen_due(business=None, now=None):
    """Locks whose reopening date has come are cleared (shown as open everywhere already; this tidies them up)."""
    from .models import VoucherPlan
    from .utils import log
    now = now or timezone.now()
    qs = VoucherPlan.objects.filter(sale_lock='locked', sale_lock_until__lte=now)
    if business is not None:
        qs = qs.filter(business=business)
    n = 0
    for p in qs.select_related('business'):
        clear(p)
        log(p.business, 'Plan Reopened', f'{p.name}: reopened by itself on the date the owner chose')
        _refresh(p.business)
        n += 1
    return n


def _refresh(business):
    """The portal price list and the online shop show only open plans: refresh the router pages."""
    try:
        from .portal_deploy import schedule_redeploy
        schedule_redeploy(business)
    except Exception:
        pass
