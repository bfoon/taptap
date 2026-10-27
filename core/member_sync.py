"""Members reach their router the moment they are created or changed.

Why members were left waiting
-----------------------------
The voucher fast lane (core/voucher_fastlane.py) sends a voucher to its router when it is *saved*: it listens
to ``post_save``. Most member code changes members with ``Voucher.objects.filter(pk=…).update(…)`` — a new
Member Plan, a plan edit that refreshes every member on it, a renewal, a password, the clock rebase — and
``QuerySet.update()`` never sends ``post_save``. Those members stayed "Pending" until the next full sync, or
until someone pressed "Fix on router".

What this module does
---------------------
* ``QuerySet.update()`` on vouchers is watched. When it really changes something the router must know about
  (status, pause, plan, time, profile, speed, devices, password, router) for a TapTap **member**, that member
  is queued in the fast lane and sent when the transaction commits — once per commit, no matter how many
  updates touched it. Nothing is sent for a write that leaves the values as they were, so the live loop and
  imports never cause a flood of router commands. Plain vouchers are left exactly as before.
* Direct API / TapTap Tunnel members go through the member-exact sender (core/member_router_alignment:
  write, read back, retry) on the voucher worker, instead of the generic voucher push.
* ``voucher_fastlane.quiet()`` still mutes everything (bulk jobs, or a caller that pushes by itself and wants
  the router's answer, like Fit's create member).

Very large updates (more than MAX_IDS members at once) are left to the normal background sync.
"""
from __future__ import annotations

import logging

from django.db.models.expressions import Combinable

logger = logging.getLogger('taptap.member_sync')

MAX_IDS = 2000
_ORIGINAL_UPDATE = None


def _watched():
    from .voucher_fastlane import WATCHED
    return set(WATCHED) | {'router'}


def _column(name):
    return 'router_id' if name == 'router' else name


def _norm(field, value):
    if field == 'frozen_at':
        return bool(value)
    if field in ('router', 'router_id'):
        return getattr(value, 'pk', value)
    return value


def _muted():
    from . import voucher_fastlane
    return bool(getattr(voucher_fastlane._local, 'muted', 0))


def _changed_member_ids(qs, kwargs):
    """Members in ``qs`` whose watched values differ from ``kwargs`` (read before the update runs)."""
    watched = _watched() & set(kwargs)
    if not watched:
        return []
    cols = sorted({_column(f) for f in watched} | {'router_id'})
    base = qs
    if kwargs.get('login_type') != 'member':                # turning a voucher into a member: look at all rows
        base = base.filter(login_type='member')
    rows = list(base.filter(source='taptap', deleted_at__isnull=True).values('pk', *cols)[:MAX_IDS + 1])
    if len(rows) > MAX_IDS:
        logger.info('member sync: %s+ members changed at once — left to the background sync', MAX_IDS)
        return []
    out = []
    for row in rows:
        if 'router' in kwargs:
            new_router = _norm('router', kwargs['router'])
        elif 'router_id' in kwargs:
            new_router = _norm('router_id', kwargs['router_id'])
        else:
            new_router = row['router_id']
        if not new_router:
            continue                                          # no router: nothing to send yet
        for f in watched:
            new = kwargs[f]
            if isinstance(new, Combinable) or _norm(f, row[_column(f)]) != _norm(f, new):
                out.append(row['pk'])
                break
    return out


def _update(self, **kwargs):
    from .models import Voucher
    if self.model is not Voucher or _muted() or not (_watched() & set(kwargs)):
        return _ORIGINAL_UPDATE(self, **kwargs)
    try:
        ids = _changed_member_ids(self, kwargs)
    except Exception:                                         # never let the safety net break a write
        logger.exception('member sync: could not read members before an update')
        ids = []
    n = _ORIGINAL_UPDATE(self, **kwargs)
    if ids:
        _schedule_many(ids)
    return n


def _schedule_many(ids):
    """Queue members in the fast lane: one send per router when the transaction commits (or right away)."""
    from django.db import transaction
    from . import voucher_fastlane as vf
    pending = getattr(vf._local, 'pending', None)
    if pending is None:
        pending = vf._local.pending = set()
    first = not pending
    pending.update(ids)
    if first:
        transaction.on_commit(vf._flush)


# ─────────────────────────── direct API / tunnel members: the exact sender ───────────────────────────

def push_members(voucher_ids):
    """Send these members with the member-exact sender. Returns (sent, failed)."""
    from .models import Voucher
    from .views_agents import push_one                        # the aligned member sender once installed
    sent = failed = 0
    for v in Voucher.objects.filter(pk__in=list(voucher_ids), router__isnull=False).select_related('router', 'business'):
        try:
            res = push_one(v)
        except Exception as exc:                              # noqa: BLE001 — router offline / API error
            res = str(exc)
        if res is True:
            sent += 1
        else:
            failed += 1
    return sent, failed


def _wrap_send_api(original):
    def wrapped(router, items):
        members = [v for v in items if v.is_member]
        plain = [v for v in items if not v.is_member]
        parts = []
        if plain:
            parts.append(original(router, plain))
        if members:
            ids = [v.pk for v in members]
            try:
                from django.conf import settings
                if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
                    raise RuntimeError('eager')
                from .tasks import push_members_task
                push_members_task.delay(ids)
                parts.append(f'{len(ids)} member(s) being sent to {router.name}')
            except Exception:
                sent, failed = push_members(ids)
                parts.append(f'{sent} member(s) sent to {router.name}' + (f', {failed} retry at the next sync' if failed else ''))
        return '; '.join(parts)
    wrapped._taptap_member_sync = True
    return wrapped


# ─────────────────────────── one send per member state ───────────────────────────
# A service that pushes a member itself (change plan, block, renew…) and the safety net above would otherwise both
# send the same state. The second send of a state that was sent *successfully* within DEDUPE_S is skipped.
# Only successes count, so "Fix on router" after a failure always tries again.

DEDUPE_S = 15


def _dedupe_key(v):
    return f'tt:mpush:{v.pk}'


def _wrap_push_one(original):
    def push_one(voucher, plan=None):
        if not getattr(voucher, 'is_member', False) or not getattr(voucher, 'router_id', None):
            return original(voucher, plan)
        from django.core.cache import cache
        from .voucher_fastlane import _state_hash
        try:
            voucher.refresh_from_db()
            state = f'{voucher.router_id}:{_state_hash(voucher)}'
        except Exception:
            state = None
        if state and cache.get(_dedupe_key(voucher)) == state:
            return True                                       # this exact state was just sent
        res = original(voucher, plan)
        if res is True and state:
            cache.set(_dedupe_key(voucher), state, DEDUPE_S)
        return res
    push_one._taptap_member_alignment = True
    push_one._taptap_member_dedupe = True
    return push_one


def install():
    global _ORIGINAL_UPDATE
    from django.db.models.query import QuerySet
    from . import voucher_fastlane
    if not getattr(QuerySet.update, '_taptap_member_sync', False):
        _ORIGINAL_UPDATE = QuerySet.update
        _update._taptap_member_sync = True
        QuerySet.update = _update
    from . import views_agents, views_members
    if not getattr(views_agents.push_one, '_taptap_member_dedupe', False):
        views_agents.push_one = _wrap_push_one(views_agents.push_one)   # wraps the member-exact sender
        views_members.push_one = views_agents.push_one
    if not getattr(voucher_fastlane._send_api, '_taptap_member_sync', False):
        voucher_fastlane._send_api = _wrap_send_api(voucher_fastlane._send_api)
    logger.info('Member sync enabled: members reach their router as soon as they are created or changed.')
