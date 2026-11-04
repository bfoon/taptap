"""Move a MikroTik to another TapTap business (Routers › Move, the mobile app's Routers screen).

What moves with the router
* the router itself, its TapTap Link and Tunnel (they belong to the router, so it keeps checking in — nothing to
  re-install on the MikroTik);
* everything about its network: hotspot users and profiles, IP bindings, interfaces, neighbours, devices, sync
  jobs, config history, incidents, alerts, traffic and app usage, backups, routers and APs behind it, its own
  activity history;
* with "vouchers" on (the default): every voucher and member on that router — so customers who are online keep
  working and the new business sees them — plus their history, device locks and customer reports. Plans and
  member plans the new business does not have yet are copied by name. A batch moves when all of its vouchers do;
  otherwise the moved vouchers simply leave the batch.

What stays where it was earned
* money: sales, expenses, agent cash and hand-ins, member payments and charges, online purchases, staff pay.
  Moving a router never changes an agent's balance or last month's profit. A moved voucher that was already sold
  stays sold (it is never booked twice).
* anything that points from a moved row back into the old business (an agent, an alert rule, a portal page, a
  batch that did not move…) is cleared on the moved row, so neither business can reach the other's data.

Who may move a router: the owner or an Admin of BOTH businesses, or TapTap platform staff. The person types the
router's name to confirm. ``preview()`` shows the counts before anything changes; ``move()`` runs in one
transaction and writes an activity line in both businesses.
"""
from __future__ import annotations

import logging

from django.apps import apps
from django.db import models, transaction
from django.db.models import Q

logger = logging.getLogger('taptap.router_move')

# Money stays in the business that earned it (see the module docstring).
MONEY = {
    'VoucherSale', 'Expense', 'OnlineVoucherPurchase', 'StaffPay', 'StaffPayout', 'CashCollection',
    'CashCollectionDetail', 'CashCollectionAllocation', 'MemberCharge', 'MemberRenewal', 'MemberBalancePayment',
    'MemberPaymentIntent', 'AgentCheck', 'AgentHelp', 'AgentOrder', 'AgentBenefit', 'AgentRefund',
    'RoamingPayment', 'RoamingOffset', 'InvestmentAccrual',
}
# Handled by hand below (or never moved).
SPECIAL = {'Router', 'Voucher', 'VoucherPlan', 'VoucherBatch', 'Business', 'RoamingMirror', 'RoamingSession',
           'MemberPlan', 'MemberPlanAssignment'}
PLAN_COPY_FIELDS = ('price', 'duration_minutes', 'duration_unit', 'max_devices', 'speed_limit', 'data_limit_mb',
                    'active', 'source', 'mikrotik_profile_name', 'price_source', 'is_free')
MEMBER_PLAN_COPY_FIELDS = ('price', 'duration_minutes', 'duration_unit', 'max_devices', 'speed_limit', 'active')
MANAGER_ROLES = ('owner', 'admin')


class MoveRefused(Exception):
    """Shown to the person as it is."""


# ─────────────────────────── who may move it, and where ───────────────────────────
def _role_in(user, business):
    """'owner', a team role, 'platform' for superusers, or None."""
    if business.user_id == user.pk:
        return 'owner'
    from .models_team import TeamMember
    m = TeamMember.objects.filter(user=user, business=business, is_active=True).first()
    if m:
        return m.role
    return 'platform' if user.is_superuser else None


def targets(user, source):
    """Businesses this person may move a router of ``source`` into: owner or Admin there (not ``source``)."""
    from .team import accessible_businesses
    out = []
    for biz, member in accessible_businesses(user):
        if biz.pk == source.pk:
            continue
        role = 'owner' if member is None else member.role
        if role in MANAGER_ROLES:
            out.append({'id': biz.pk, 'name': biz.business_name, 'role': 'Owner' if role == 'owner' else 'Admin'})
    return out


def find_target(user, source, business_id):
    """The business with this ID, if this person may move a router into it (else MoveRefused)."""
    from .models import Business
    try:
        bid = int(str(business_id).strip().lstrip('#'))
    except (TypeError, ValueError):
        raise MoveRefused('Type the business ID as a number, e.g. 42. It is shown under Settings in that business.')
    if bid == source.pk:
        raise MoveRefused('The router is already in this business. Pick another business.')
    target = Business.objects.filter(pk=bid).first()
    if not target:
        raise MoveRefused(f'There is no TapTap business with ID {bid}. Check the number in that business’s Settings.')
    if _role_in(user, source) not in MANAGER_ROLES + ('platform',):
        raise MoveRefused('Only the owner or an Admin of this business can move its routers.')
    if _role_in(user, target) not in MANAGER_ROLES + ('platform',):
        raise MoveRefused(f'You can move a router only into a business you own or run as Admin. Ask the owner of '
                          f'business #{bid} to add you to their team as Admin, then try again.')
    return target


# ─────────────────────────── the models involved ───────────────────────────
def _business_field(model):
    """The many-to-one 'business' field of a model (not a one-to-one per-business setting), or None."""
    from .models import Business
    for f in model._meta.concrete_fields:
        if f.name == 'business' and f.is_relation and f.many_to_one and f.related_model is Business:
            return f
    return None


def _has_business(model):
    from .models import Business
    return any(f.name == 'business' and f.is_relation and f.related_model is Business for f in model._meta.concrete_fields)


def _links(model, to):
    return [f for f in model._meta.concrete_fields if f.is_relation and (f.many_to_one or f.one_to_one) and f.related_model is to]


def _catalogue():
    """[(model, router fields, voucher fields)] for every core model tied to a router or a voucher."""
    from .models import Router, Voucher
    out = []
    for model in apps.get_app_config('core').get_models():
        if model.__name__ in SPECIAL or model._meta.proxy:
            continue
        rf, vf = _links(model, Router), _links(model, Voucher)
        if rf or vf:
            out.append((model, rf, vf))
    return out


def _condition(router, rfields, vfields, voucher_ids, with_vouchers):
    q = Q()
    for f in rfields:
        q |= Q(**{f.name: router})
    if with_vouchers:
        for f in vfields:
            q |= Q(**{f'{f.name}__in': voucher_ids})
    return q


def _voucher_ids(router):
    from .models import Voucher
    return Voucher._base_manager.filter(router=router, business_id=router.business_id).values('pk')


def _label(model):
    text = str(model._meta.verbose_name_plural)
    return text[:1].upper() + text[1:]


# ─────────────────────────── preview ───────────────────────────
def preview(router, with_vouchers=True):
    """What a move would take along (counts only, nothing changes)."""
    from .models import Voucher, VoucherSale
    vq = Voucher._base_manager.filter(router=router, business_id=router.business_id)
    vids = _voucher_ids(router)
    moved, stays = {}, {}
    for model, rf, vf in _catalogue():
        bf = _business_field(model)
        if bf is None:
            continue
        q = _condition(router, rf, vf, vids, with_vouchers)
        if not q:
            continue
        n = model._base_manager.filter(q, business_id=router.business_id).count()
        if n:
            (stays if model.__name__ in MONEY else moved)[_label(model)] = n
    v = {
        'total': vq.count() if with_vouchers else 0,
        'in_use': vq.filter(used_at__isnull=False, status='active').count() if with_vouchers else 0,
        'unsold': vq.filter(sold_at__isnull=True, used_at__isnull=True, status='active').count() if with_vouchers else 0,
        'members': vq.filter(login_type='member').count() if with_vouchers else 0,
        'left_behind': 0 if with_vouchers else vq.count(),
    }
    roaming = 0
    try:
        roaming = apps.get_model('core', 'RoamingMirror').objects.filter(router=router).count()
    except LookupError:
        pass
    return {
        'router': {'id': router.pk, 'name': router.name},
        'vouchers': v,
        'sales_stay': VoucherSale.objects.filter(Q(router=router) | Q(voucher__in=vids), business_id=router.business_id).count(),
        'moved': dict(sorted(moved.items(), key=lambda kv: -kv[1])[:12]),
        'stays': stays,
        'roaming_mirrors': roaming,
    }


# ─────────────────────────── the move ───────────────────────────
def _copy_by_name(model, source, target, names, fields, extra=None):
    """{lower name: target row} for these names; rows the target lacks are copied from the source's rows."""
    names = {n for n in names if n}
    if not names:
        return {}, 0
    live = model._base_manager.filter(deleted_at__isnull=True) if hasattr(model, 'deleted_at') else model._base_manager.all()
    have = {p.name.lower(): p for p in live.filter(business=target)}
    src = {p.name.lower(): p for p in live.filter(business=source)}
    copied = 0
    for low in {n.lower() for n in names}:
        if low in have or low not in src:
            continue
        s = src[low]
        kwargs = {f: getattr(s, f) for f in fields if hasattr(s, f)}
        kwargs.update(extra(s) if extra else {})
        have[low] = model._base_manager.create(business=target, name=s.name, **kwargs)
        copied += 1
    return have, copied


def move(router, target, user=None, with_vouchers=True):
    """Move ``router`` (and, with ``with_vouchers``, its vouchers) into ``target``. Returns a summary dict."""
    from .models import Router, Voucher, VoucherBatch, VoucherPlan
    from .utils import log
    source = router.business
    if target.pk == source.pk:
        raise MoveRefused('The router is already in this business.')
    summary = {'router': router.name, 'from': source.business_name, 'to': target.business_name,
               'from_id': source.pk, 'to_id': target.pk, 'moved': {}, 'cleared': {}, 'plans_copied': 0,
               'member_plans_copied': 0, 'batches_moved': 0, 'vouchers': 0, 'warnings': []}
    with transaction.atomic():
        router = Router.objects.select_for_update().get(pk=router.pk)
        vids_list = list(Voucher._base_manager.filter(router=router, business=source).values_list('pk', flat=True)) \
            if with_vouchers else []

        # 1. plans the moved vouchers and batches use, by name
        if with_vouchers and vids_list:
            vqs = Voucher._base_manager.filter(pk__in=vids_list)
            names = set(vqs.values_list('plan_name', flat=True))
            batch_ids = set(vqs.exclude(batch__isnull=True).values_list('batch_id', flat=True))
            names |= set(VoucherBatch._base_manager.filter(pk__in=batch_ids, plan__isnull=False).values_list('plan__name', flat=True))
            plans, summary['plans_copied'] = _copy_by_name(
                VoucherPlan, source, target, names, PLAN_COPY_FIELDS,
                extra=lambda s: {'imported_from_router_id': router.pk if s.imported_from_router_id == router.pk else None,
                                 'created_by_label': f'Moved with {router.name} from {source.business_name}'[:150]})
            # 2. a batch moves when every one of its vouchers moves
            for batch in VoucherBatch._base_manager.filter(pk__in=batch_ids, business=source):
                if not Voucher._base_manager.filter(batch=batch).exclude(pk__in=vids_list).exists():
                    batch.business = target
                    batch.agent = None
                    if batch.plan_id:
                        batch.plan = plans.get(batch.plan.name.lower())
                    batch.save(update_fields=['business', 'agent', 'plan'])
                    summary['batches_moved'] += 1
            # 3. member plans of moved members
            try:
                MemberPlan = apps.get_model('core', 'MemberPlan')
                MPA = apps.get_model('core', 'MemberPlanAssignment')
                rows = list(MPA.objects.filter(member_id__in=vids_list).select_related('plan'))
                mplans, summary['member_plans_copied'] = _copy_by_name(
                    MemberPlan, source, target, {a.plan.name for a in rows}, MEMBER_PLAN_COPY_FIELDS)
                for a in rows:
                    if a.plan.business_id != target.pk and a.plan.name.lower() in mplans:
                        MPA.objects.filter(pk=a.pk).update(plan=mplans[a.plan.name.lower()])
            except LookupError:
                pass
            # 4. the vouchers themselves (agents belong to the old business)
            summary['vouchers'] = Voucher._base_manager.filter(pk__in=vids_list).update(business=target, agent=None)

        # 5. everything else tied to the router (or, with vouchers, to a moved voucher)
        catalogue = _catalogue()
        moved_q = {}
        for model, rf, vf in catalogue:
            bf = _business_field(model)
            name = model.__name__
            q = _condition(router, rf, vf, vids_list, with_vouchers)
            if not q:
                continue
            if name in MONEY:
                continue                                  # stays in the business that earned it
            if bf is None:
                # No business of its own: it follows the router (interfaces, agent commands…). A per-business
                # setting that points at this router (a store's default router) lets go of it.
                if _has_business(model):
                    for f in rf:
                        if f.null:
                            n = model._base_manager.filter(**{f.name: router}).update(**{f.name: None})
                            if n:
                                summary['cleared'][f'{model._meta.verbose_name_plural} · {f.name}'] = n
                continue
            n = model._base_manager.filter(q, business=source).update(business=target)
            if n:
                summary['moved'][_label(model)] = n
            moved_q[model] = q
        # Not moving vouchers: rows of the router that point at the old business's vouchers let go of them below.

        # 6. the router
        Router.objects.filter(pk=router.pk).update(business=target, uplink_router=None, uplink_site=None,
                                                    uplink_internet=False, uplink_ignored=[])
        Router.objects.filter(business=source, uplink_router=router).update(uplink_router=None)
        router.business = target

        # 7. nothing that moved may still point into the old business
        for model, q in list(moved_q.items()) + ([(Voucher, Q(pk__in=vids_list))] if vids_list else []):
            for f in model._meta.concrete_fields:
                if not (f.is_relation and (f.many_to_one or f.one_to_one)) or f.name == 'business':
                    continue
                rel = f.related_model
                if rel is None or rel.__name__ in ('Business', 'User') or not _has_business(rel):
                    continue
                if rel.__name__ == 'Router' and f.name != 'uplink_router':
                    # the moved router is in the target now; another router of the old business is not
                    stale = (model._base_manager.filter(q, business=target)
                             .exclude(**{f'{f.name}__in': Router.objects.filter(business=target).values('pk')})
                             .exclude(**{f'{f.name}__isnull': True}))
                else:
                    stale = (model._base_manager.filter(q, business=target)
                             .exclude(**{f'{f.name}__isnull': True})
                             .exclude(**{f'{f.name}__business': target}))
                if f.null:
                    n = stale.update(**{f.name: None})
                    if n:
                        summary['cleared'][f'{model._meta.verbose_name_plural} · {f.verbose_name}'] = n
                else:
                    n = stale.count()
                    if n:
                        summary['warnings'].append(f'{n} {model._meta.verbose_name_plural} still point at a '
                                                   f'{rel._meta.verbose_name} of {source.business_name}.')

        who = (user.get_full_name() or user.email) if user else 'TapTap'
        log(source, 'Router moved', f'{router.name} moved to {target.business_name} (#{target.pk}) by {who}'
            + (f' with {summary["vouchers"]} vouchers' if with_vouchers else ', vouchers stayed here'))
        log(target, 'Router moved', f'{router.name} arrived from {source.business_name} (#{source.pk}), moved by {who}'
            + (f' with {summary["vouchers"]} vouchers' if with_vouchers else ''), router=router)
    _after_move(router, source, target)
    logger.info('router %s moved from business %s to %s: %s', router.pk, source.pk, target.pk, summary)
    return summary


def _after_move(router, source, target):
    """Outside the transaction: tell both businesses' live views, forget cached per-business state."""
    try:
        from .live import push_event
        push_event(source.pk, f'{router.name} moved to {target.business_name}', 'info')
        push_event(target.pk, f'{router.name} joined from {source.business_name}', 'good')
    except Exception:
        pass
    try:
        from django.core.cache import cache
        for b in (source.pk, target.pk):
            cache.delete_many([f'tt:topology:{b}', f'tt:graph:{b}', f'tt:live:status:{b}'])
    except Exception:
        pass
