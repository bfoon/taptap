"""Locked Member Plans — the business owner freezes a plan and the members on it.

Two strengths, chosen by the owner:

* **Locked** — only the owner (the account itself: not staff, not platform support "viewing as") can unlock it.
* **Sealed** — nobody can ever unlock it, not even the owner. The owner types the plan's name to confirm.

While a plan is locked or sealed:

* nobody — the owner included — gives it to a new member, moves a member onto it, or moves a member off it;
* nobody edits it (name, price, speed, devices, validity), switches it on or off, or puts it in the bin;
* the members already on it keep it **exactly as it is**: renewals, pauses, passwords, devices and their router
  profile carry on unchanged, because nothing on the plan can change.

Enforced in one place for every door — staff pages, the member page, Fit, agent benefits, the bin: the core
functions in core/members.py call :func:`check_edit` / :func:`check_assign`, and ``MemberPlanAssignment.save()``
calls :func:`guard_assignment` as the last line of defence.
"""
from __future__ import annotations

from django.utils import timezone

KINDS = {'locked': 'Locked', 'sealed': 'Sealed'}


def _error():
    from .members import MemberError
    return MemberError


class PlanLocked(Exception):
    """Raised as a MemberError subclass (see :func:`locked_error`) so every existing handler shows it as a message."""


_CLS = []


def locked_error(message):
    """A MemberError that is also a PlanLocked: every existing `except MemberError` shows it as a plain message."""
    if not _CLS:
        _CLS.append(type('PlanLockedError', (_error(), PlanLocked), {}))
    return _CLS[0](message)


def is_locked(plan):
    return bool(plan is not None and getattr(plan, 'lock_kind', ''))


def describe(plan):
    if not is_locked(plan):
        return ''
    who = ''
    if plan.locked_by_id:
        u = plan.locked_by
        who = f' by {u.get_full_name() or u.username}'
    when = timezone.localtime(plan.locked_at).strftime('%d %b %Y') if plan.locked_at else ''
    return (f'{KINDS.get(plan.lock_kind, "Locked")}{who}' + (f' on {when}' if when else '') +
            (f' — “{plan.lock_reason}”' if plan.lock_reason else ''))


def _owner_hint(plan):
    return ('It is sealed: nobody can unlock it.' if plan.lock_kind == 'sealed' else
            'Only the business owner can unlock it.')


# ─────────────────────────────── who may lock ───────────────────────────────

def is_owner(request, business=None):
    """The real account owner — not a team member, and not platform support viewing as the owner."""
    business = business or getattr(request.user, 'business', None)
    return (bool(business) and request.user.is_authenticated and business.user_id == request.user.pk
            and getattr(request, 'tt_role', 'owner') == 'owner' and not getattr(request, 'tt_view_as', None))


# ─────────────────────────────── the checks ───────────────────────────────

def check_edit(plan, what='changed'):
    """A locked plan cannot be edited, switched on/off or deleted — by anyone."""
    if is_locked(plan):
        raise locked_error(f'Member Plan “{plan.name}” is {KINDS[plan.lock_kind].lower()} by the owner, so it cannot be {what}. '
                           f'Its members keep it exactly as it is. {_owner_hint(plan)}')


def check_assign(member, plan):
    """Nobody joins a locked plan, and nobody already on one is moved off it. Re-confirming the same plan is fine."""
    from .members import plan_for_member
    current = plan_for_member(member) if getattr(member, 'pk', None) else None
    if current is not None and plan is not None and current.pk == plan.pk:
        return
    if is_locked(current):
        raise locked_error(f'{member.code} is on “{current.name}”, which is {KINDS[current.lock_kind].lower()} by the owner: its members stay '
                           f'exactly as they are and cannot be moved to another plan. {_owner_hint(current)}')
    if is_locked(plan):
        raise locked_error(f'Member Plan “{plan.name}” is {KINDS[plan.lock_kind].lower()} by the owner: nobody can be put on it. '
                           f'Its members keep it. {_owner_hint(plan)}')


def guard_assignment(assignment):
    """MemberPlanAssignment.save(): the last line of defence for any path that writes an assignment."""
    from .models_member_plans import MemberPlan, MemberPlanAssignment
    plan = MemberPlan.all_objects.filter(pk=assignment.plan_id).only('pk', 'name', 'lock_kind').first() if assignment.plan_id else None
    old_id = None
    if assignment.pk:
        old_id = MemberPlanAssignment.objects.filter(pk=assignment.pk).values_list('plan_id', flat=True).first()
    if old_id == assignment.plan_id and assignment.pk:
        return
    old = MemberPlan.all_objects.filter(pk=old_id).only('pk', 'name', 'lock_kind').first() if old_id else None
    if is_locked(old):
        raise locked_error(f'Members of “{old.name}” cannot be moved: the plan is {KINDS[old.lock_kind].lower()} by the owner.')
    if is_locked(plan):
        raise locked_error(f'Nobody can be put on “{plan.name}”: the plan is {KINDS[plan.lock_kind].lower()} by the owner.')


def usable(qs):
    """Plans that may be offered for a new member or a plan change."""
    return qs.filter(lock_kind='')


# ─────────────────────────────── locking and unlocking ───────────────────────────────

def lock(plan, request, kind='locked', reason='', confirm=''):
    from .utils import log
    if not is_owner(request, plan.business):
        raise locked_error('Only the business owner can lock a Member Plan.')
    if kind not in KINDS:
        raise locked_error('Choose Lock or Seal.')
    if plan.lock_kind == 'sealed':
        raise locked_error(f'“{plan.name}” is already sealed.')
    if kind == 'sealed' and str(confirm or '').strip().lower() != plan.name.strip().lower():
        raise locked_error(f'To seal it for good, type the plan’s name exactly: {plan.name}')
    plan.lock_kind, plan.lock_reason = kind, str(reason or '').strip()[:200]
    plan.locked_at, plan.locked_by = timezone.now(), request.user
    plan.save(update_fields=['lock_kind', 'lock_reason', 'locked_at', 'locked_by', 'updated_at'])
    n = plan.assignments.count()
    log(plan.business, 'Member Plan', f'{plan.name}: {"SEALED for good" if kind == "sealed" else "locked"} by the owner — '
                                      f'{n} member{"s" if n != 1 else ""} keep it' + (f' ({plan.lock_reason})' if plan.lock_reason else ''))
    return plan


def unlock(plan, request):
    from .utils import log
    if plan.lock_kind == 'sealed':
        raise locked_error(f'“{plan.name}” is sealed: nobody can unlock it, not even the owner.')
    if not is_owner(request, plan.business):
        raise locked_error('Only the business owner can unlock a Member Plan.')
    if not plan.lock_kind:
        return plan
    plan.lock_kind, plan.lock_reason, plan.locked_at, plan.locked_by = '', '', None, None
    plan.save(update_fields=['lock_kind', 'lock_reason', 'locked_at', 'locked_by', 'updated_at'])
    log(plan.business, 'Member Plan', f'{plan.name}: unlocked by the owner')
    return plan
