"""Recycle bin for Member Plans.

Deleting a Member Plan is one-way and never erases anything:

* Only a SWITCHED-OFF (inactive) plan can be deleted — "Make inactive" first, so nobody is put on it
  while you decide.
* Members are never left on a deleted plan. A plan that still has members can be deleted only by moving
  them to another active Member Plan in the same step. Each member keeps the time already used (the same
  rule as "Change Member Plan", core/member_time.py) and is sent to the router again — nobody whose time is
  not finished is disconnected.
* Members already in the bin, and renewal receipts, keep pointing at the plan: the receipt still prints.
* The plan goes to the bin with who, when and why, plus a snapshot of its price, length, devices and speed.
  Nothing in the bin can be deleted again or brought back. Its name can be used by a new plan.
* The plan's HotSpot profile (taptap-member-plan-<id>) is left on the router; profile lists show it under
  "plan deleted" and nobody can pick it any more (core/profile_groups.py).
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .models_member_plans import MemberPlan, MemberPlanAssignment


class MemberPlanBinError(ValueError):
    """A delete that cannot be done, with a readable reason."""


def live_members(plan):
    """Assignments of members still on this plan (members in the bin don't count)."""
    return (MemberPlanAssignment.objects
            .filter(plan=plan, member__deleted_at__isnull=True)
            .select_related('member', 'member__router'))


def usage(plan):
    """What deleting this plan would touch."""
    return {
        'members': live_members(plan).count(),
        'binned_members': MemberPlanAssignment.objects.filter(plan=plan, member__deleted_at__isnull=False).count(),
        'renewals': plan.renewals.count(),
    }


def can_delete(plan, perms=None):
    """(allowed, reason). ``perms`` None means "not checked" (Fit and tests pass their own)."""
    if plan.deleted_at:
        return False, 'This Member Plan is already in the bin.'
    if perms is not None and 'plans.manage' not in perms:
        return False, 'Your role cannot delete Member Plans.'
    if plan.active:
        return False, f'{plan.name} is still active. Make it inactive first, then delete it.'
    return True, ''


def move_targets(plan):
    """Active Member Plans of the same business that the members can move to."""
    return plan.business.member_plans.filter(active=True).exclude(pk=plan.pk).order_by('price', 'name')


def delete_member_plan(plan, *, user=None, reason='', move_to=None, perms=None):
    """Move a switched-off Member Plan to the bin. Returns a summary dict.

    ``move_to``: the active Member Plan its members move to first (required when it still has members).
    """
    from . import members as mem

    reason = (reason or '').strip()[:255]
    if not reason:
        raise MemberPlanBinError('Give a reason for deleting — it is kept with the plan in the bin.')
    ok, why = can_delete(plan, perms)
    if not ok:
        raise MemberPlanBinError(why)

    who = user if getattr(user, 'is_authenticated', False) else None
    business = plan.business
    moved, ids = [], []

    with transaction.atomic():
        # Lock the plan and re-check inside the transaction: it may have been switched on a second ago.
        plan = MemberPlan.objects.select_for_update().get(pk=plan.pk)
        ok, why = can_delete(plan, perms)
        if not ok:
            raise MemberPlanBinError(why)

        rows = list(live_members(plan))
        if rows:
            if move_to is None:
                raise MemberPlanBinError(
                    f'{plan.name} still has {len(rows)} member{"s" if len(rows) != 1 else ""}. '
                    'Choose the Member Plan they move to, then delete it.')
            if move_to.pk == plan.pk or move_to.business_id != business.pk or move_to.deleted_at:
                raise MemberPlanBinError('Choose another Member Plan of this business for the members.')
            if not move_to.active:
                raise MemberPlanBinError(f'{move_to.name} is inactive. Choose an active Member Plan for the members.')
            for a in rows:
                try:
                    mem.assign_member_plan(a.member, move_to, user=user)
                except mem.MemberError as exc:
                    raise MemberPlanBinError(f'{a.member.code}: {exc}') from exc
                moved.append(a.member.code)
                ids.append(a.member_id)

        u = usage(plan)
        plan.deleted_at, plan.deleted_by, plan.delete_reason, plan.active = timezone.now(), who, reason, False
        plan.delete_info = {
            'price': str(plan.price), 'free': not plan.price, 'duration': plan.duration_text,
            'devices': plan.max_devices, 'speed': plan.speed_limit, 'profile': plan.router_profile_name,
            'members_moved': len(moved), 'moved_to': move_to.name if moved else '',
            'binned_members': u['binned_members'], 'renewals': u['renewals'],
        }
        plan.save(update_fields=['deleted_at', 'deleted_by', 'delete_reason', 'active', 'delete_info', 'updated_at'])

        if ids:
            # Router work after the commit, so a slow router never holds database locks.
            mem._push_after_commit(ids, move_to)

    from .utils import log
    log(business, 'Member Plan Deleted',
        f'{plan.name} to the bin' + (f' — {len(moved)} member(s) moved to {move_to.name}' if moved else '') + f' — {reason}')
    return {'plan': plan, 'moved': moved, 'moved_to': move_to if moved else None}
