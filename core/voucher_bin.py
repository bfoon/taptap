"""Recycle bin for vouchers and batches.

Deleting is one-way and never erases anything:

* Only UNUSED vouchers can be deleted (never logged in on a router). A used
  voucher is real, earned history and stays where it is.
* The voucher leaves TapTap's lists, reports and finance (the default managers
  hide it) and is removed from its MikroTik router, through the right channel:
  Direct API / TapTap Tunnel straight away, TapTap Link as a queued command.
* If it had been sold, the sale is taken out of finance and kept, as a snapshot,
  on the voucher in the bin — so the bin shows exactly what was removed.
* It goes to the bin with who, when and why. Nothing in the bin can be deleted
  again or brought back, and its code is never issued again.
* Router sync keeps the promise: if a deleted code is still on (or re-appears
  on) a router, TapTap removes it again instead of importing it.
"""
from __future__ import annotations

import logging

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .models import Voucher, VoucherBatch, VoucherEvent, VoucherSale

logger = logging.getLogger('taptap.bin')

LINK_CHUNK = 50          # codes per TapTap Link command (keeps each router script small)
HEAL_EVERY = 600         # seconds between automatic re-removals of the same code on one router


class BinError(ValueError):
    """A delete that cannot be done, with a readable reason."""


# ─────────────────────────────── rules ───────────────────────────────

def why_not_deletable(voucher):
    """None when the voucher may be deleted, else a short reason."""
    if voucher.deleted_at:
        return 'already in the bin'
    if voucher.used_at:
        return 'already used on the router'
    return None


def deleted_codes(business):
    """Upper-case codes of this business's vouchers in the bin."""
    return {c.upper() for c in Voucher.all_objects.binned().filter(business=business).values_list('code', flat=True)}


# ─────────────────────────────── deleting ───────────────────────────────

def _snapshot(v, sale, currency):
    info = {'status': v.status, 'plan': v.plan_name, 'price': str(v.price), 'router': v.router.name if v.router else '',
            'batch': v.batch.name if v.batch_id and v.batch else '', 'agent': v.agent.name if v.agent_id and v.agent else '',
            'customer': v.customer_name or v.customer_phone or '', 'sold_at': v.sold_at.isoformat() if v.sold_at else '',
            'created_at': v.created_at.isoformat() if v.created_at else '', 'source': v.source}
    if sale:
        info['sale'] = {'amount': str(sale.amount), 'commission': str(sale.commission), 'discount': str(sale.discount),
                        'method': sale.get_payment_method_display(), 'agent': sale.agent.name if sale.agent_id and sale.agent else '',
                        'sold_at': sale.sold_at.isoformat(), 'reference': sale.reference, 'currency': currency}
    return {k: val for k, val in info.items() if val not in ('', None)}


def delete_vouchers(business, vouchers, user=None, reason='', via_batch=None, via_plan=None):
    """Move the unused vouchers among `vouchers` to the bin and off their routers.

    Returns a summary dict: deleted, skipped (with reasons), sales_removed, sales_amount, router (per-router messages)."""
    reason = (reason or '').strip()[:255]
    if not reason:
        raise BinError('Give a reason for deleting — it is kept with the record in the bin.')
    now = timezone.now()
    summary = {'deleted': 0, 'skipped': [], 'sales_removed': 0, 'sales_amount': 0, 'router': []}
    todo = []
    for v in vouchers:
        why = why_not_deletable(v)
        if why:
            summary['skipped'].append((v.code, why))
        else:
            todo.append(v)
    if not todo:
        return summary

    by_router = {}
    with transaction.atomic():
        ids = [v.pk for v in todo]
        # Lock the rows and re-check inside the transaction: a voucher may have been used a second ago.
        fresh = {v.pk: v for v in Voucher.objects.select_for_update(of=('self',)).filter(pk__in=ids, business=business).select_related('router', 'agent', 'batch')}
        sales = {s.voucher_id: s for s in VoucherSale.objects.filter(voucher_id__in=ids).select_related('agent')}
        events = []
        for v in todo:
            cur = fresh.get(v.pk)
            if cur is None or cur.used_at:
                summary['skipped'].append((v.code, 'used or changed while deleting'))
                continue
            v = cur
            sale = sales.get(v.pk)
            info = _snapshot(v, sale, business.currency)
            if via_batch is not None:
                info['deleted_with_batch'] = via_batch.name
            if via_plan is not None:
                info['deleted_with_plan'] = via_plan.name
            if sale:
                summary['sales_removed'] += 1
                summary['sales_amount'] += float(sale.amount)
                events.append(VoucherEvent(business=business, voucher=v, voucher_code=v.code, event='sale_voided', source='user',
                                           user=user if getattr(user, 'is_authenticated', False) else None, reason=reason,
                                           status_before=v.status, status_after=v.status,
                                           detail={'text': f'{business.currency}{sale.amount} removed from finance — voucher deleted'}))
                sale.delete()
            before = v.status
            v.deleted_at = now
            v.deleted_by = user if getattr(user, 'is_authenticated', False) else None
            v.delete_reason = reason
            v.delete_info = info
            v.status = 'disabled'          # belt and braces: a binned code must never log anyone in
            v.router_removal = 'not_needed' if not v.router_id else 'failed'
            v.router_removal_note = '' if not v.router_id else 'Waiting to be removed from the router'
            v.save(update_fields=['deleted_at', 'deleted_by', 'delete_reason', 'delete_info', 'status',
                                  'router_removal', 'router_removal_note'])
            events.append(VoucherEvent(business=business, voucher=v, voucher_code=v.code, event='deleted', source='user',
                                       user=user if getattr(user, 'is_authenticated', False) else None, reason=reason,
                                       status_before=before, status_after='deleted',
                                       detail={'text': 'Moved to the bin' + (f' with batch {via_batch.name}' if via_batch else '')
                                               + (f' with plan {via_plan.name}' if via_plan else ''),
                                               'plan': v.plan_name, 'price': str(v.price)}))
            summary['deleted'] += 1
            if v.router_id:
                by_router.setdefault(v.router_id, []).append(v)
        VoucherEvent.objects.bulk_create(events, batch_size=500)

    # Router work happens after the commit, so a slow router never holds database locks.
    from .models import Router
    for router in Router.objects.filter(pk__in=list(by_router)):
        ok, msg = remove_from_router(router, by_router[router.pk], user=user)
        summary['router'].append((router.name, ok, msg))

    from .utils import log
    what = f'batch {via_batch.name}: ' if via_batch else (f'plan {via_plan.name}: ' if via_plan else '')
    log(business, 'Voucher Deleted', f'{what}{summary["deleted"]} voucher(s) to the bin — {reason}')
    return summary


def delete_batch(batch, user=None, reason=''):
    """Delete every unused voucher of a batch. The batch itself goes to the bin once no
    voucher is left in it; used vouchers keep the batch alive (they are real history)."""
    business = batch.business
    vouchers = list(batch.vouchers.select_related('router', 'agent', 'batch'))
    if not vouchers:
        raise BinError('This batch has no vouchers left to delete.')
    if all(v.used_at for v in vouchers):
        raise BinError('Every voucher in this batch has been used, so there is nothing to delete.')
    summary = delete_vouchers(business, vouchers, user=user, reason=reason, via_batch=batch)
    left = batch.vouchers.count()
    summary['batch_binned'] = False
    if left == 0:
        batch.deleted_at = timezone.now()
        batch.deleted_by = user if getattr(user, 'is_authenticated', False) else None
        batch.delete_reason = (reason or '').strip()[:255]
        batch.delete_info = {'vouchers_deleted': summary['deleted'], 'sales_removed': summary['sales_removed'],
                             'sales_amount': round(summary['sales_amount'], 2), 'plan': batch.plan.name if batch.plan else '',
                             'agent': batch.agent.name if batch.agent_id and batch.agent else '', 'quantity': batch.quantity}
        batch.save(update_fields=['deleted_at', 'deleted_by', 'delete_reason', 'delete_info'])
        summary['batch_binned'] = True
    summary['kept_used'] = left
    return summary


# ─────────────────────────────── plans ───────────────────────────────

def plan_usage(plan):
    """What deleting this plan would touch (live vouchers only)."""
    from django.db.models import Count, Q, Sum
    vs = Voucher.objects.filter(business=plan.business, plan_name=plan.name)
    c = vs.aggregate(total=Count('pk'), used=Count('pk', filter=Q(used_at__isnull=False)),
                     unused=Count('pk', filter=Q(used_at__isnull=True)),
                     sold_unused=Count('pk', filter=Q(used_at__isnull=True, sold_at__isnull=False)))
    c['sales_at_risk'] = VoucherSale.objects.filter(voucher__in=vs.filter(used_at__isnull=True)).aggregate(v=Sum('amount'))['v'] or 0
    c['batches'] = plan.voucherbatch_set.count()
    return c


def can_delete_plan(plan, perms):
    """(allowed, reason). Plans whose vouchers were never used: anyone who manages plans.
    Plans with used vouchers: Owner and Admin only ('plans.delete_used')."""
    if plan.deleted_at:
        return False, 'This plan is already in the bin.'
    if 'plans.manage' not in perms:
        return False, 'Your role cannot delete plans.'
    used = Voucher.objects.filter(business=plan.business, plan_name=plan.name, used_at__isnull=False).exists()
    if used and 'plans.delete_used' not in perms:
        return False, 'Vouchers of this plan have been used — only an Owner or Admin can delete it.'
    return True, ''


def delete_plan(plan, user=None, reason='', perms=frozenset()):
    """Move a plan to the bin.

    * Its UNUSED vouchers go to the bin too (and off the routers); if they were sold, the sale leaves finance.
    * USED vouchers stay exactly as they are — they are real history, keep their plan name and keep working
      until their own time runs out.
    * Batches of the plan that end up with no voucher left go to the bin as well.
    * The HotSpot profile stays on the router (vouchers in use still depend on it); a router sync will not
      import it back as a plan while it is in the bin.
    """
    reason = (reason or '').strip()[:255]
    if not reason:
        raise BinError('Give a reason for deleting — it is kept with the plan in the bin.')
    ok, why = can_delete_plan(plan, perms)
    if not ok:
        raise BinError(why)
    business = plan.business
    usage = plan_usage(plan)
    unused = list(Voucher.objects.filter(business=business, plan_name=plan.name, used_at__isnull=True)
                  .select_related('router', 'agent', 'batch'))
    summary = {'deleted': 0, 'skipped': [], 'sales_removed': 0, 'sales_amount': 0, 'router': []}
    if unused:
        summary = delete_vouchers(business, unused, user=user, reason=reason, via_plan=plan)
    now = timezone.now()
    who = user if getattr(user, 'is_authenticated', False) else None
    binned_batches = []
    for batch in VoucherBatch.objects.filter(business=business, plan=plan):
        if not batch.vouchers.exists():
            batch.deleted_at, batch.deleted_by = now, who
            batch.delete_reason = reason
            batch.delete_info = {'plan': plan.name, 'deleted_with_plan': plan.name, 'quantity': batch.quantity,
                                 'agent': batch.agent.name if batch.agent_id and batch.agent else ''}
            batch.save(update_fields=['deleted_at', 'deleted_by', 'delete_reason', 'delete_info'])
            binned_batches.append(batch.name)
    kept = Voucher.objects.filter(business=business, plan_name=plan.name).count()
    plan.deleted_at, plan.deleted_by, plan.delete_reason, plan.active = now, who, reason, False
    plan.delete_info = {
        'price': str(plan.price), 'free': plan.is_free, 'duration': plan.duration_text, 'devices': plan.max_devices,
        'speed': plan.speed_limit, 'source': plan.source, 'profile': plan.mikrotik_profile_name,
        'router': plan.imported_from_router.name if plan.imported_from_router_id and plan.imported_from_router else '',
        'vouchers_total': usage['total'], 'vouchers_deleted': summary['deleted'], 'used_kept': kept,
        'sales_removed': summary['sales_removed'], 'sales_amount': round(summary['sales_amount'], 2),
        'batches_binned': binned_batches,
    }
    plan.save(update_fields=['deleted_at', 'deleted_by', 'delete_reason', 'active', 'delete_info'])
    from .utils import log
    log(business, 'Plan Deleted', f'{plan.name} to the bin — {summary["deleted"]} unused voucher(s) deleted, {kept} used kept — {reason}')
    summary.update({'kept_used': kept, 'batches_binned': binned_batches})
    return summary


# ─────────────────────────────── routers ───────────────────────────────

def remove_with_service(svc, codes):
    """Remove hotspot users (and any session) for these codes over an open API connection.
    Returns (removed, not_there)."""
    want = {c.upper() for c in codes}
    users = svc.resource('/ip/hotspot/user')
    removed, found = [], set()
    for row in users.get():
        name = str(row.get('name', ''))
        if name.upper() in want and row.get('id'):
            users.remove(id=row['id'])
            removed.append(name); found.add(name.upper())
    try:
        active = svc.resource('/ip/hotspot/active')
        for row in active.get():
            if str(row.get('user', '')).upper() in want and row.get('id'):
                active.remove(id=row['id'])
    except Exception:
        pass   # sessions are a bonus; the user entry is what matters
    return removed, [c for c in codes if c.upper() not in found]


def remove_from_router(router, vouchers, user=None):
    """Take these (binned) vouchers off one router. Updates each voucher's router_removal.
    Returns (ok, message)."""
    from .voucher_history import channel
    vouchers = list(vouchers)
    if not vouchers:
        return True, ''
    via = channel(router)
    ids = [v.pk for v in vouchers]
    if via == 'TapTap Link':
        from .linkops import send
        try:
            for i in range(0, len(vouchers), LINK_CHUNK):
                part = vouchers[i:i + LINK_CHUNK]
                send(router, 'hotspot_users_remove', {'names': [v.code for v in part], 'ids': [v.pk for v in part]},
                     label=f'Remove {len(part)} deleted voucher(s)', user=user, minutes=60 * 24 * 3)
        except ValueError as exc:
            Voucher.all_objects.filter(pk__in=ids).update(router_removal='failed', router_removal_note=f'Not sent yet: {exc}'[:255])
            return False, f'{router.name}: not removed yet ({exc}). TapTap retries when the router is back.'
        Voucher.all_objects.filter(pk__in=ids).update(router_removal='queued', router_removal_note='Queued on TapTap Link')
        return True, f'{router.name}: removal queued — applied at the router\'s next check-in.'
    from .mikrotik import MikroTikService
    try:
        svc = MikroTikService(router).connect()
        try:
            removed, missing = remove_with_service(svc, [v.code for v in vouchers])
        finally:
            svc.close()
    except Exception as exc:
        Voucher.all_objects.filter(pk__in=ids).update(router_removal='failed', router_removal_note=f'Router update failed: {exc}'[:255])
        return False, f'{router.name}: not reachable ({exc}). TapTap removes them at the next successful sync.'
    Voucher.all_objects.filter(pk__in=ids).update(router_removal='removed', router_removal_note=f'Removed via {via}')
    return True, f'{router.name}: {len(removed)} removed' + (f', {len(missing)} were not on the router' if missing else '') + '.'


def heal(router, names, user=None):
    """Called by router sync when it sees codes that are in the bin: remove them again,
    at most once every HEAL_EVERY seconds per router so a slow router isn't flooded."""
    if not names:
        return
    key = f'bin-heal:{router.pk}'
    if cache.get(key):
        return
    cache.set(key, 1, HEAL_EVERY)
    upper = {n.upper() for n in names}
    vs = [v for v in Voucher.all_objects.binned().filter(business=router.business) if v.code.upper() in upper]
    if vs:
        # Link: don't queue a second copy while one is still waiting for the router.
        from .models import AgentCommand
        if AgentCommand.objects.filter(router=router, kind='hotspot_users_remove', status__in=['queued', 'sent']).exists():
            return
        ok, msg = remove_from_router(router, vs, user=user)
        logger.info('Bin heal on %s: %s', router.name, msg)


def link_ack(cmd, ok):
    """TapTap Link answered a hotspot_users_remove command."""
    ids = (cmd.params or {}).get('ids', [])
    Voucher.all_objects.filter(pk__in=ids).update(
        router_removal='removed' if ok else 'failed',
        router_removal_note='Removed via TapTap Link' if ok else 'The router reported an error — retrying at the next sync')
