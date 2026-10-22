"""Group changes Fit can offer — many vouchers at once, approved only by the Owner or an Admin.

Examples a person can ask for
    “disable all the frozen vouchers”          operation=disable  state=frozen
    “unfreeze every warned voucher on Tower”   operation=unfreeze state=warned router=Tower
    “add 2 hours to everyone on the Daily plan who is using it now”
                                               operation=add_time state=in_use plan=Daily minutes=120
    “put the unused vouchers of batch B12 in the bin”
                                               operation=to_bin   state=unused batch=B12

How it stays safe
1. Only the Owner, an Admin or platform staff get the pseudo-permission ADMIN_PERM (views_fit adds it), so the
   kind is not even described to the model for anyone else, and run() refuses it again at approval time.
2. The owner must also allow the “Bulk voucher changes” group in Fit settings (the normal second lock).
3. The proposal stores the exact voucher ids that matched when the card was made. Approving acts on those ids
   only — a voucher that appeared afterwards is never touched — and each one is checked again just before it is
   changed (a voucher used, unfrozen or deleted in the meantime is skipped and listed).
4. The card shows the context: how many match, a sample of codes, where they are (routers / plans), how many are
   online right now, and plain warnings (people cut off, members included, saved time dropped, sales removed from
   finance, routers offline). Big or irreversible changes ask the person to type the number before Take action.
5. Every voucher gets its normal history entry (“approved in Fit”), routers are updated in batches through the same
   services the TapTap pages use, and the whole change is one line in the activity log.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, time as dtime

from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger('taptap.fit')

ADMIN_PERM = 'fit.admin'                  # granted only to Owner / Admin / platform staff (core/views_fit._perms)
ADMIN_ROLES = frozenset({'owner', 'admin', 'platform'})
SCAN_LIMIT = 20000                        # vouchers looked at for one request
MAX_IDS = 2000                            # vouchers one card may change
SLOW_MAX = 200                            # for operations done voucher by voucher
CONFIRM_AT = 20                           # from this many vouchers the person types the number to confirm
SAMPLE = 10

STATES = {
    'frozen': 'Frozen (paused by staff)',
    'warned': 'Warned (waiting for “I agree”)',
    'paused': 'Frozen or warned',
    'disabled': 'Disabled (not frozen)',
    'in_use': 'In use — clock running',
    'online': 'Online right now',
    'expired': 'Time ran out',
    'unused': 'Never used',
    'stock': 'In stock (not sold, not used)',
    'sold_unused': 'Sold but not used yet',
    'any': 'Any state',
}

# operation: (label, needs one more permission (any of) or None, done one by one?, danger)
OPERATIONS = {
    'disable': ('Disable', None, False, 'danger'),
    'enable': ('Enable', None, False, 'ok'),
    'freeze': ('Freeze', None, False, 'warn'),
    'unfreeze': ('Unfreeze', None, False, 'ok'),
    'warn': ('Warn', ('vouchers.warn',), False, 'warn'),
    'add_time': ('Add time to', None, True, 'ok'),
    'reset_devices': ('Reset the devices of', None, True, 'warn'),
    'disconnect': ('Disconnect', None, True, 'warn'),
    'to_bin': ('Move to the bin', ('vouchers.manage',), False, 'danger'),
}


class BulkError(ValueError):
    pass


def is_admin(request):
    """Owner, Admin, or TapTap platform staff (superuser)."""
    user = getattr(request, 'user', None)
    return bool(getattr(user, 'is_superuser', False)) or getattr(request, 'tt_role', None) in ADMIN_ROLES


def _s(v):
    return str(v if v is not None else '').strip()


def _names(v):
    if isinstance(v, (list, tuple)):
        return [_s(x) for x in v if _s(x)]
    return [x.strip() for x in re.split(r'[,;\n]', _s(v)) if x.strip()]


def _date(v, end=False):
    v = _s(v)
    if not v:
        return None
    try:
        d = datetime.strptime(v[:10], '%Y-%m-%d').date()
    except ValueError:
        raise BulkError(f'Dates must look like 2026-10-22 (got “{v}”).')
    return timezone.make_aware(datetime.combine(d, dtime.max if end else dtime.min))


def _mtext(minutes):
    from .durations import text
    return text(int(minutes))


def _fit_reason(reason):
    return f'{reason} (approved in Fit)'[:255]


# ─────────────────────────────── who is online ───────────────────────────────
def online_codes(business):
    """Upper-case usernames online now, from the last live sync of every router (best effort, cheap)."""
    out = set()
    try:
        from .linklive import sessions
    except Exception:
        sessions = None
    for r in business.routers.all():
        snap = cache.get(f'tt:tr:users:{r.pk}') or {}
        out |= {str(k).upper() for k in (snap.get('users') or {})}
        if sessions:
            try:
                out |= {str(x.get('user') or '').upper() for x in sessions(r) if x.get('user')}
            except Exception:
                pass
    out.discard('')
    return out


# ─────────────────────────────── selecting ───────────────────────────────
def _filters(business, a):
    """Validated filters from the model's (or the stored) arguments."""
    f = {'state': _s(a.get('state')).lower() or 'any', 'plans': [], 'batches': [], 'routers': [], 'codes': [],
         'agent': '', 'members': _s(a.get('members')).lower() or 'include',
         'created_after': _s(a.get('created_after')), 'created_before': _s(a.get('created_before'))}
    if f['state'] not in STATES:
        raise BulkError(f'Which vouchers? state is one of: {", ".join(STATES)}.')
    if f['members'] not in ('include', 'exclude', 'only'):
        f['members'] = 'include'
    plans = _names(a.get('plans') or a.get('plan'))
    if plans:
        known = {p.lower(): p for p in business.vouchers.exclude(plan_name='').values_list('plan_name', flat=True).distinct()[:2000]}
        bad = [p for p in plans if p.lower() not in known]
        if bad:
            raise BulkError(f'No vouchers on plan {", ".join(bad)}. Plans in use: {", ".join(sorted(known.values())[:20])}.')
        f['plans'] = [known[p.lower()] for p in plans]
    for name in _names(a.get('batches') or a.get('batch')):
        hit = business.batches.filter(name__iexact=name).first() if hasattr(business, 'batches') else None
        if hit is None:
            raise BulkError(f'There is no batch “{name}”.')
        f['batches'].append(hit.pk)
    for name in _names(a.get('routers') or a.get('router')):
        if name.lower() in ('all', 'every', 'any'):
            continue
        hit = business.routers.filter(pk=int(name)).first() if name.isdigit() else business.routers.filter(name__iexact=name).first()
        if hit is None:
            raise BulkError(f'There is no router “{name}”. Routers: {", ".join(business.routers.values_list("name", flat=True)[:12])}.')
        f['routers'].append(hit.pk)
    f['codes'] = [c.replace(' ', '') for c in _names(a.get('codes') or a.get('code'))][:MAX_IDS]
    agent = _s(a.get('agent'))
    if agent:
        hit = business.agents.filter(name__iexact=agent).first() if hasattr(business, 'agents') else None
        if hit is None:
            raise BulkError(f'There is no agent “{agent}”.')
        f['agent'] = hit.pk
    _date(f['created_after']); _date(f['created_before'], end=True)
    narrowed = f['state'] != 'any' or f['plans'] or f['batches'] or f['routers'] or f['codes'] or f['agent'] \
        or f['created_after'] or f['created_before'] or f['members'] == 'only'
    if not narrowed:
        raise BulkError('Say which vouchers: a state (frozen, warned, in use, online…), a plan, a batch, a router, an agent, '
                        'dates or codes. Fit never changes “all vouchers” without a filter.')
    return f


def select(business, f, now=None):
    """(matching vouchers, scanned_all) for the filters. Never outside this business, never from the bin."""
    now = now or timezone.now()
    qs = business.vouchers.filter(deleted_at__isnull=True)
    if f['plans']:
        qs = qs.filter(plan_name__in=f['plans'])
    if f['batches']:
        qs = qs.filter(batch_id__in=f['batches'])
    if f['routers']:
        qs = qs.filter(router_id__in=f['routers'])
    if f['codes']:
        q = Q()
        for c in f['codes']:
            q |= Q(code__iexact=c)
        qs = qs.filter(q)
    if f['agent']:
        qs = qs.filter(Q(agent_id=f['agent']) | Q(batch__agent_id=f['agent']))
    if f['members'] == 'exclude':
        qs = qs.exclude(login_type='member')
    elif f['members'] == 'only':
        qs = qs.filter(login_type='member')
    if f['created_after']:
        qs = qs.filter(created_at__gte=_date(f['created_after']))
    if f['created_before']:
        qs = qs.filter(created_at__lte=_date(f['created_before'], end=True))
    st = f['state']
    if st == 'frozen':
        qs = qs.filter(frozen_at__isnull=False).exclude(freeze_kind='warning')
    elif st == 'warned':
        qs = qs.filter(frozen_at__isnull=False, freeze_kind='warning')
    elif st == 'paused':
        qs = qs.filter(frozen_at__isnull=False)
    elif st == 'disabled':
        qs = qs.filter(frozen_at__isnull=True, status='disabled')
    elif st in ('in_use', 'expired'):
        qs = qs.filter(frozen_at__isnull=True, used_at__isnull=False) if st == 'in_use' else qs.filter(frozen_at__isnull=True)
    elif st == 'unused':
        qs = qs.filter(used_at__isnull=True)
    elif st == 'stock':
        qs = qs.filter(used_at__isnull=True, sold_at__isnull=True, frozen_at__isnull=True).exclude(status='disabled')
    elif st == 'sold_unused':
        qs = qs.filter(used_at__isnull=True, sold_at__isnull=False)
    rows = list(qs.select_related('router').order_by('pk')[:SCAN_LIMIT + 1])
    complete = len(rows) <= SCAN_LIMIT
    rows = rows[:SCAN_LIMIT]
    from .voucher_history import time_is_up
    if st == 'in_use':
        rows = [v for v in rows if v.status == 'active' and not time_is_up(v, now)]
    elif st == 'expired':
        rows = [v for v in rows if v.status == 'expired' or time_is_up(v, now)]
    elif st == 'online':
        on = online_codes(business)
        rows = [v for v in rows if v.code.upper() in on]
    return rows, complete


# ─────────────────────────────── who can be changed ───────────────────────────────
def why_not(op, v, now, online=None):
    """None when `op` makes sense for this voucher right now, else a short reason."""
    from .voucher_history import time_is_up
    from .voucher_freeze import why_not_freezable
    if v.deleted_at:
        return 'in the bin'
    if op == 'disable':
        return 'already disabled' if v.status == 'disabled' and not v.frozen_at else None
    if op == 'enable':
        if v.frozen_at:
            return 'frozen — unfreeze instead'
        if v.status != 'disabled':
            return 'not disabled'
        if time_is_up(v, now) or v.status == 'expired':
            return 'time ran out — add time instead'
        return None
    if op in ('freeze', 'warn'):
        return why_not_freezable(v, now)
    if op == 'unfreeze':
        return None if v.frozen_at else 'not frozen'
    if op == 'add_time':
        if v.frozen_at:
            return 'frozen'
        if not v.duration_minutes and not v.expires_at:
            return 'unlimited — no time to add'
        return None
    if op == 'reset_devices':
        return None
    if op == 'disconnect':
        if not v.router_id:
            return 'no router'
        if online is not None and v.code.upper() not in online:
            return 'not online'
        return None
    if op == 'to_bin':
        from .voucher_bin import why_not_deletable
        return why_not_deletable(v)
    return 'unknown operation'


# ─────────────────────────────── the proposal ───────────────────────────────
def _operation(a):
    op = _s(a.get('operation')).lower()
    if op not in OPERATIONS:
        raise BulkError(f'What should happen to them? operation is one of: {", ".join(OPERATIONS)}.')
    return op


def _minutes(a):
    try:
        n = int(a.get('minutes') or 0)
    except (TypeError, ValueError):
        raise BulkError('Time must be a whole number of minutes.')
    if not 1 <= n <= 525600:
        raise BulkError('Time must be between 1 minute and 1 year.')
    return n


def _router_notes(business, vouchers):
    """Warnings about routers that cannot take the change right now."""
    notes = []
    ids = {v.router_id for v in vouchers if v.router_id}
    if not ids:
        return notes
    try:
        from .linkops import is_link, uses_link
        from .linklive import link_state
        for r in business.routers.filter(pk__in=ids):
            if is_link(r) and uses_link(r):
                online, _ = link_state(r)
                if not online:
                    notes.append(f'{r.name} is offline: TapTap changes its vouchers now and the router catches up when it is back.')
    except Exception:
        logger.debug('router notes', exc_info=True)
    return notes


def check(business, a):
    """Validate a request (or re-check a stored proposal). Returns (params, title, detail, link)."""
    from .fit_actions import FitActionError
    try:
        return _check(business, a)
    except BulkError as exc:
        raise FitActionError(str(exc))


def _check(business, a):
    op = _operation(a)
    label, _, slow, danger = OPERATIONS[op]
    now = timezone.now()
    if a.get('ids') is not None:                          # re-check at approval time: the same vouchers, nothing new
        ids = [int(x) for x in a.get('ids') or [] if str(x).isdigit()][:MAX_IDS]
        if not ids:
            raise BulkError('This offer has no vouchers left.')
        return a, a.get('_title', ''), a.get('_detail', ''), a.get('_link', '')
    f = _filters(business, a)
    rows, complete = select(business, f, now)
    if not rows:
        raise BulkError(f'No vouchers match ({STATES[f["state"]].lower()}'
                        + (f', plan {", ".join(f["plans"])}' if f['plans'] else '') + ').')
    online = online_codes(business) if op in ('disable', 'freeze', 'warn', 'to_bin', 'disconnect', 'unfreeze') else None
    ok, skipped = [], Counter()
    for v in rows:
        why = why_not(op, v, now, online)
        if why:
            skipped[why] += 1
        else:
            ok.append(v)
    if not ok:
        raise BulkError(f'{len(rows)} voucher(s) match, but none can be changed that way: '
                        + '; '.join(f'{n} {w}' for w, n in skipped.most_common(4)) + '.')
    cap = SLOW_MAX if slow else MAX_IDS
    over = max(0, len(ok) - cap)
    ok = ok[:cap]
    p = {'operation': op, 'ids': [v.pk for v in ok], 'filters': f, 'danger': danger,
         'reason': (_s(a.get('reason')) or f'{label} — group change')[:200]}
    if op == 'add_time':
        p['minutes'] = _minutes(a)
    if op == 'warn':
        from .voucher_freeze import MANUAL_WARNING
        p['message'] = _s(a.get('message'))[:600] or MANUAL_WARNING
    n = len(ok)
    noun = f'{n} voucher{"s" if n != 1 else ""}'
    what = STATES[f['state']].split(' (')[0].lower() if f['state'] != 'any' else ''
    title = {'add_time': f'Add {_mtext(p.get("minutes", 1))} to {noun}', 'to_bin': f'Move {noun} to the bin'}.get(
        op, f'{label} {noun}') + (f' ({what})' if what else '')

    # ── context the person sees before deciding ──
    members = sum(1 for v in ok if v.login_type == 'member')
    on_now = sum(1 for v in ok if online is not None and v.code.upper() in online)
    by_router = Counter(v.router.name if v.router_id and v.router else 'no router' for v in ok)
    by_plan = Counter(v.plan_name or '—' for v in ok)
    facts = [['Matched', f'{len(rows)}' + ('' if complete else '+')], ['Will change', str(n)]]
    if skipped:
        facts.append(['Skipped', ', '.join(f'{c} {w}' for w, c in skipped.most_common(3))])
    facts.append(['Routers', ', '.join(f'{k} ({c})' for k, c in by_router.most_common(4))])
    facts.append(['Plans', ', '.join(f'{k} ({c})' for k, c in by_plan.most_common(4))])
    if online is not None:
        facts.append(['Online now', str(on_now)])
    if members:
        facts.append(['Members', str(members)])

    warnings = []
    if op in ('disable', 'freeze', 'warn', 'to_bin', 'disconnect') and on_now:
        warnings.append(f'{on_now} {"is" if on_now == 1 else "are"} online right now and will be cut off at once.')
    if members and op in ('disable', 'freeze', 'warn', 'to_bin', 'disconnect'):
        warnings.append(f'{members} of them {"is a member" if members == 1 else "are members"} (monthly accounts), not just vouchers.')
    if op == 'disable':
        fz = [v for v in ok if v.frozen_at]
        if fz:
            secs = sum(v.frozen_left or 0 for v in fz)
            warnings.append(f'{len(fz)} {"is" if len(fz) == 1 else "are"} frozen: the freeze ends and they stay disabled for good — '
                            f'the time they had saved' + (f' (about {_mtext(max(1, secs // 60))} in total)' if secs else '')
                            + ' is no longer kept. Choose unfreeze instead if they should work again.')
        warnings.append('Disabled vouchers stop working until someone enables them again.')
    if op == 'unfreeze':
        warnings.append('Their clocks start again from now with the time each one had left.')
    if op == 'enable':
        warnings.append('They work again straight away with the time they have left.')
    if op == 'warn':
        warnings.append(f'Their internet pauses until each customer presses “I agree” on: “{p["message"][:140]}”.')
    if op == 'add_time':
        warnings.append('Adding time is not a sale — finance does not change. It cannot be taken back in one step.')
    if op == 'reset_devices':
        warnings.append('Every locked device is forgotten; the next devices that log in take the slots.')
    if op == 'to_bin':
        sold = [v for v in ok if v.sold_at]
        if sold:
            total = sum(float(v.price or 0) for v in sold)
            warnings.append(f'{len(sold)} of them {"was" if len(sold) == 1 else "were"} sold: their sale'
                            f'{"s are" if len(sold) != 1 else " is"} removed from finance (about {business.currency or "D"}{total:,.0f}).')
        warnings.append('They are removed from the routers. They can be restored from the bin.')
    if over:
        warnings.append(f'{over} more match: only the first {cap} are in this card. Ask Fit again for the rest afterwards.')
    if not complete:
        warnings.append(f'More than {SCAN_LIMIT} vouchers fit the filter — narrow it (plan, batch, router, dates) for a full picture.')
    warnings += _router_notes(business, ok)

    happens = {'disable': 'be disabled', 'enable': 'be enabled', 'freeze': 'be frozen', 'unfreeze': 'be unfrozen',
               'warn': 'be warned', 'add_time': f'get {_mtext(p.get("minutes", 1))} more', 'reset_devices': 'have their devices reset',
               'disconnect': 'be disconnected', 'to_bin': 'go to the bin'}[op]
    detail = f'{noun} will {happens}. Reason kept in each history: {p["reason"]}'
    codes = [v.code for v in ok]
    p['_card'] = {'facts': facts, 'warnings': warnings, 'sample': codes[:SAMPLE], 'more': max(0, n - SAMPLE),
                  'confirm': str(n) if (n >= CONFIRM_AT or op == 'to_bin') else '', 'count': n}
    link = reverse('vouchers')
    p['_title'], p['_detail'], p['_link'] = title, detail, link
    return p, title, detail, link


# ─────────────────────────────── doing it ───────────────────────────────
def _fresh(business, p):
    from .models import Voucher
    ids = [int(x) for x in p.get('ids') or [] if str(x).isdigit()][:MAX_IDS]
    return list(Voucher.objects.filter(business=business, pk__in=ids, deleted_at__isnull=True).select_related('router').order_by('pk')), len(ids)


def do(business, p, user):
    """Carry out an approved group change on the stored vouchers. Returns a plain-language result."""
    from .fit_actions import FitActionError
    from .utils import log
    op = p.get('operation')
    if op not in OPERATIONS:
        raise FitActionError('Unknown group change.')
    vouchers, asked = _fresh(business, p)
    now = timezone.now()
    online = online_codes(business) if op == 'disconnect' else None
    todo, skipped = [], Counter()
    for v in vouchers:
        why = why_not(op, v, now, online)
        if why:
            skipped[why] += 1
        else:
            todo.append(v)
    gone = asked - len(vouchers)
    if gone:
        skipped['deleted meanwhile'] += gone
    reason = _fit_reason(p.get('reason') or 'Group change')
    if not todo:
        raise FitActionError('Nothing left to change: ' + '; '.join(f'{n} {w}' for w, n in skipped.most_common(4)) + '.')
    done, router_msgs, failed = 0, [], 0
    try:
        if op == 'disable':
            done, router_msgs = _disable(todo, user, reason)
        elif op == 'enable':
            done, router_msgs = _enable(todo, user, reason)
        elif op in ('freeze', 'warn'):
            from .voucher_freeze import freeze
            out = freeze(todo, user=user, reason=reason, kind='warning' if op == 'warn' else 'freeze',
                         message=p.get('message', '') if op == 'warn' else '')
            done, router_msgs = out['done'], [m for _, m in out['router']]
            for c, w in out['skipped']:
                skipped[w] += 1
        elif op == 'unfreeze':
            from .voucher_freeze import unfreeze
            out = unfreeze(todo, user=user, reason=reason)
            done, router_msgs = out['done'], [m for _, m in out['router']]
        elif op == 'to_bin':
            from .voucher_bin import delete_vouchers
            out = delete_vouchers(business, todo, user=user, reason=reason)
            done = out['deleted']
            router_msgs = [f'{name}: {msg}' for name, ok, msg in out['router']]
            for c, w in out['skipped']:
                skipped[w] += 1
        else:
            done, failed = _one_by_one(op, todo, user, reason, p)
    except FitActionError:
        raise
    except ValueError as exc:
        raise FitActionError(str(exc))
    label = OPERATIONS[op][0]
    parts = [f'{label}: {done} of {asked} voucher(s) done.']
    if failed:
        parts.append(f'{failed} failed (see their history).')
    if skipped:
        parts.append('Skipped: ' + '; '.join(f'{n} {w}' for w, n in skipped.most_common(4)) + '.')
    if router_msgs:
        parts.append(' '.join(router_msgs[:3]) + ('…' if len(router_msgs) > 3 else ''))
    try:
        log(business, 'Fit group change', f'{label} {done} voucher(s) — {p.get("reason") or ""}'[:250])
    except Exception:
        logger.exception('Fit group change log')
    return ' '.join(parts)


def _disable(todo, user, reason):
    """Disable in one go. A frozen voucher stops being frozen and stays disabled (no moment online in between)."""
    from .models import Voucher
    from .voucher_freeze import _apply_on_routers
    from .voucher_history import channel, record
    done = []
    with transaction.atomic():
        fresh = {v.pk: v for v in Voucher.objects.select_for_update(of=('self',)).filter(pk__in=[v.pk for v in todo])}
        for v in todo:
            cur = fresh.get(v.pk)
            if cur is None or cur.deleted_at or (cur.status == 'disabled' and not cur.frozen_at):
                continue
            cur._before = 'frozen' if cur.frozen_at else cur.status
            cur._left = cur.frozen_left if cur.frozen_at else None
            cur.status = 'disabled'
            cur.frozen_at, cur.frozen_by, cur.freeze_kind, cur.freeze_reason, cur.frozen_left = None, None, '', '', None
            cur.warning_message = ''
            cur.save(update_fields=['status', 'frozen_at', 'frozen_by', 'freeze_kind', 'freeze_reason', 'frozen_left', 'warning_message'])
            cur.router = v.router
            done.append(cur)
    msgs = [m for _, m in _apply_on_routers(done, True, user=user)] if done else []
    for v in done:
        text = ''
        if v._before == 'frozen':
            text = 'Freeze ended — disabled for good' + (f' (saved time dropped: {_mtext(max(1, v._left // 60))})' if v._left else '')
        record(v, 'disabled', user=user, reason=reason, via=channel(v.router), router_result='; '.join(msgs)[:255],
               status_before=v._before, status_after='disabled', text=text)
    return len(done), msgs


def _enable(todo, user, reason):
    from .models import Voucher
    from .voucher_freeze import _apply_on_routers
    from .voucher_history import channel, record, time_is_up
    done, now = [], timezone.now()
    with transaction.atomic():
        fresh = {v.pk: v for v in Voucher.objects.select_for_update(of=('self',)).filter(pk__in=[v.pk for v in todo])}
        for v in todo:
            cur = fresh.get(v.pk)
            if cur is None or cur.deleted_at or cur.frozen_at or cur.status != 'disabled' or time_is_up(cur, now):
                continue
            cur.status = 'active'
            cur.save(update_fields=['status'])
            cur.router = v.router
            done.append(cur)
    msgs = [m for _, m in _apply_on_routers(done, False, user=user)] if done else []
    for v in done:
        record(v, 'enabled', user=user, reason=reason, via=channel(v.router), router_result='; '.join(msgs)[:255],
               status_before='disabled', status_after='active')
    return len(done), msgs


def _one_by_one(op, todo, user, reason, p):
    from .voucher_history import _router_apply, enable, record, reset_devices
    done = failed = 0
    for v in todo:
        try:
            if op == 'add_time':
                enable(v, user=user, reason=reason, add_minutes=p['minutes'])
            elif op == 'reset_devices':
                reset_devices(v, user=user, reason=reason)
            elif op == 'disconnect':
                via, result, ok = _router_apply(v, 'reset', user=user)
                record(v, 'note', user=user, reason=f'Session disconnected — {reason}'[:255], via=via, router_result=result)
                if not ok:
                    raise ValueError(result)
            done += 1
        except Exception as exc:  # noqa: BLE001 — one voucher must never stop the rest
            failed += 1
            logger.info('Fit group %s on %s failed: %s', op, v.code, exc)
    return done, failed


# ─────────────────────────────── read-only overview for the model ───────────────────────────────
def t_count_vouchers(ctx, state='any', plan='', batch='', router='', agent='', codes='', members='include',
                     created_after='', created_before=''):
    """How many vouchers match, by state — so Fit can explain before it offers a group change."""
    from .fit_tools import ToolError
    try:
        f = _filters(ctx.business, {'state': state, 'plan': plan, 'batch': batch, 'router': router, 'agent': agent,
                                    'codes': codes, 'members': members, 'created_after': created_after,
                                    'created_before': created_before})
    except BulkError as exc:
        if 'Say which vouchers' not in str(exc):
            raise ToolError(str(exc))
        f = {'state': 'any', 'plans': [], 'batches': [], 'routers': [], 'codes': [], 'agent': '', 'members': 'include',
             'created_after': '', 'created_before': ''}
    rows, complete = select(ctx.business, f)
    on = online_codes(ctx.business)
    from .voucher_history import display_state
    states = Counter(display_state(v)[1] for v in rows)
    out = {'matched': len(rows), 'complete': complete, 'by_state': dict(states.most_common()),
           'online_now': sum(1 for v in rows if v.code.upper() in on),
           'members': sum(1 for v in rows if v.login_type == 'member'),
           'by_router': dict(Counter(v.router.name if v.router_id and v.router else 'no router' for v in rows).most_common(6)),
           'by_plan': dict(Counter(v.plan_name or '—' for v in rows).most_common(6)),
           'sample': [v.code for v in rows[:8]],
           'can_change_in_bulk': ADMIN_PERM in ctx.perms}
    if not out['can_change_in_bulk']:
        out['note'] = 'Only the Owner or an Admin can approve group changes. This person can still change vouchers one by one.'
    return out


SPEC_PROPS = {
    'operation': {'type': 'string', 'enum': list(OPERATIONS),
                  'description': 'bulk_vouchers: what to do to every matching voucher'},
    'state': {'type': 'string', 'enum': list(STATES),
              'description': 'bulk_vouchers / count_vouchers: ' + '; '.join(f'{k} = {v}' for k, v in STATES.items())},
    'agent': {'type': 'string', 'description': 'bulk_vouchers: only vouchers of this agent'},
    'members': {'type': 'string', 'enum': ['include', 'exclude', 'only'], 'description': 'bulk_vouchers: members too? (default include)'},
    'created_after': {'type': 'string', 'description': 'bulk_vouchers: created on/after YYYY-MM-DD'},
    'created_before': {'type': 'string', 'description': 'bulk_vouchers: created on/before YYYY-MM-DD'},
}
