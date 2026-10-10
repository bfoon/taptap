"""Click the voucher time bar: end the time there, or freeze the voucher when the clock gets there.

end_at()       moves the end of a running voucher to a chosen moment.
                 earlier  → expires_at moves; strict expiry (core/expiry.py) switches it off at that moment
                 now/past → it ends at once (the expiry sweep runs straight away)
                 later    → time is added with the normal "add time" action, which also raises the router limit
plan_freeze()  saves a freeze for a moment in the future (or freezes now, if that moment has come).
run_due()      called by the live loop (core/live.py) next to the expiry sweep: fires planned freezes.
Every step is written to the voucher history.
"""
import math
from datetime import datetime, timedelta, timezone as dt_tz

from django.utils import timezone

NOW_SLACK = timedelta(seconds=45)       # a click this close to "now" means now


class TimePointError(ValueError):
    pass


def parse_point(value):
    """The moment clicked on the bar: milliseconds since 1970 (from the browser) or an ISO date-time."""
    try:
        if str(value).strip().lstrip('-').isdigit():
            return datetime.fromtimestamp(int(value) / 1000, tz=dt_tz.utc)
        when = datetime.fromisoformat(str(value))
        return when if timezone.is_aware(when) else timezone.make_aware(when)
    except (TypeError, ValueError, OverflowError, OSError):
        raise TimePointError('That point on the time bar could not be read. Click it again.')


def _fmt(when):
    return f'{timezone.localtime(when):%d %b %Y %H:%M}'


def _checks(v, now):
    from .voucher_history import ends_at, time_is_up
    if v.deleted_at:
        raise TimePointError(f'{v.code} is in the bin.')
    if v.frozen_at:
        raise TimePointError(f'{v.code} is frozen. Unfreeze it first — its clock is standing still.')
    if v.status != 'active' or time_is_up(v, now):
        raise TimePointError(f'{v.code} is not running ({v.get_status_display().lower() if hasattr(v, "get_status_display") else v.status}). '
                             'Use Enable / Add time instead.')
    end = ends_at(v)
    if not end or not (v.rolled_back_at or v.used_at):
        raise TimePointError(f'The clock of {v.code} has not started yet, so there is no time bar to click.')
    return end


def end_at(v, when, user=None, reason=''):
    """End the voucher's time at `when`. Returns a message for the user."""
    from .voucher_history import enable, record
    now = timezone.now()
    end = _checks(v, now)
    when = when.replace(second=0, microsecond=0) if when - now > NOW_SLACK else now
    reason = (reason or '').strip()[:255]

    if when > end:
        minutes = math.ceil((when - end).total_seconds() / 60)
        if minutes < 1:
            return f'{v.code} already ends at {_fmt(end)}.'
        ok, result = enable(v, user=user, reason=reason or 'Time bar: end moved later', add_minutes=minutes)
        _drop_plan_after(v, user)
        return f'{v.code} now ends {_fmt(v.expires_at)}. {result}'

    before = end
    v.expires_at = when
    v.save(update_fields=['expires_at'])
    ended_now = when <= now
    record(v, 'time_shortened', user=user, reason=reason, status_before=v.status, status_after=v.status,
           ends_before=before.isoformat(), ends_after=when.isoformat(),
           text=('Ended now' if ended_now else f'Now ends {_fmt(when)}') + f' (was {_fmt(before)})')
    _drop_plan_after(v, user)
    if ended_now:
        try:                                    # switch it off at once instead of at the next sweep
            from .expiry import sweep
            sweep(business=v.business, now=now)
        except Exception:
            pass
        return f'{v.code} has ended. Its internet is being switched off now.'
    return (f'{v.code} now ends {_fmt(when)} instead of {_fmt(before)}. '
            'TapTap switches it off at that moment.')


def _drop_plan_after(v, user):
    """A planned freeze that now falls after the end can never happen: cancel it."""
    from .voucher_history import ends_at
    end = ends_at(v)
    if v.freeze_planned_at and end and v.freeze_planned_at >= end:
        cancel_plan(v, user, why='the time now ends before it')


def plan_freeze(v, when, user=None, reason=''):
    """Freeze `v` when its clock reaches `when` (now, if that moment has come). Returns a message."""
    from .voucher_freeze import freeze, summary_messages
    from .voucher_history import record
    now = timezone.now()
    end = _checks(v, now)
    reason = (reason or '').strip()[:255]
    if not reason:
        raise TimePointError('Give a reason — it is kept in the voucher history.')
    if when >= end:
        raise TimePointError(f'That point is after its time runs out ({_fmt(end)}). Pick a point before the end, '
                             'or use “End the time here”.')
    if when - now <= NOW_SLACK:
        out = freeze([v], user=user, reason=reason)
        return ' '.join(m for _, m in summary_messages(out, 'frozen')) or f'{v.code} is frozen.'
    when = when.replace(second=0, microsecond=0)
    replaced = v.freeze_planned_at
    v.freeze_planned_at, v.freeze_planned_reason, v.freeze_planned_by = when, reason, user if getattr(user, 'is_authenticated', False) else None
    v.save(update_fields=['freeze_planned_at', 'freeze_planned_reason', 'freeze_planned_by'])
    record(v, 'freeze_planned', user=user, reason=reason,
           text=f'Freezes at {_fmt(when)}' + (f' (was {_fmt(replaced)})' if replaced else ''), planned_at=when.isoformat())
    left = end - when
    return (f'{v.code} will freeze at {_fmt(when)}, keeping about {_short(left)} of its time for later. '
            'You can cancel it on the time bar.')


def _short(delta):
    from .durations import text
    return text(max(1, int(delta.total_seconds() // 60)))


def cancel_plan(v, user=None, why=''):
    from .voucher_history import record
    if not v.freeze_planned_at:
        return f'{v.code} has no planned freeze.'
    when = v.freeze_planned_at
    v.freeze_planned_at, v.freeze_planned_reason, v.freeze_planned_by = None, '', None
    v.save(update_fields=['freeze_planned_at', 'freeze_planned_reason', 'freeze_planned_by'])
    record(v, 'freeze_plan_cancelled', user=user, reason=why, text=f'Was planned for {_fmt(when)}')
    return f'The freeze planned for {_fmt(when)} is cancelled.'


def run_due(now=None):
    """Fire planned freezes whose moment has come. Returns how many vouchers were frozen."""
    from .models import Voucher
    from .voucher_freeze import freeze, why_not_freezable
    now = now or timezone.now()
    done = 0
    for v in Voucher.objects.filter(freeze_planned_at__lte=now).select_related('router', 'business', 'freeze_planned_by')[:500]:
        user, reason = v.freeze_planned_by, v.freeze_planned_reason or 'Planned freeze'
        why = why_not_freezable(v, now)
        Voucher.objects.filter(pk=v.pk).update(freeze_planned_at=None, freeze_planned_reason='', freeze_planned_by=None)
        v.freeze_planned_at = None
        if why:
            cancel_plan_record(v, why)
            continue
        try:
            out = freeze([v], user=user, reason=f'Planned: {reason}', source='auto')
            done += out.get('done', 0)
        except Exception:
            continue
    return done


def cancel_plan_record(v, why):
    from .voucher_history import record
    record(v, 'freeze_plan_cancelled', source='auto', reason=f'Not frozen: {why}')
