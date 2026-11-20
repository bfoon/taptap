"""Fit missions — jobs Fit does later, by itself, once their moment comes.

    "Restart Mast tonight when the fewest people are online, and email Awa when it is back."
    "Every Monday at 08:00, look at last week's agent sales and email me what you think."
    "When Tower has been offline for 10 minutes, tell the team in chat and email Lamin."
    "When sales today reach D5,000, tell me."
    "When Daily stock falls under 30 vouchers, email me."
    "At midnight, disable every frozen voucher."

A mission has three parts (models_fit.FitMission):

WHEN (trigger)
    at       a moment ("today 23:00", "in 45 minutes", "2026-11-24 03:00")
    repeat   a timetable: every day / chosen weekdays / a day of the month, at a local time
    quiet    the quietest moment of a window ("between 01:00 and 05:00"), on one router or all of them:
               * with max_online: as soon as at most that many people are online; at the end of the window it runs
                 anyway (otherwise=run) or is skipped and you are told (otherwise=skip)
               * without: the same rule as router schedules — at once when nobody is online, after two thirds of the
                 window at the quietest moment seen so far, at the end of the window whatever happens
             once, or every day (daily)
    when     a condition that has been true for some minutes (CONDITIONS below), optionally only inside a window
             of the day; once, or every time it happens again (after it was false, and not more often than its
             cool-down)

DO (task)
    notify           only tell people (the message)
    restart_router   the same restart the Reboot button and router schedules send; TapTap then follows it and tells
                     everyone when the router is back (or not back after 20 minutes)
    shutdown_router  a real power-off: it stays OFF until someone powers it on (asked to be understood first)
    action           any change Fit can offer (freeze, add time, sync, router change, bulk voucher changes…). It is
                     checked once when you approve the mission and again, with the situation at that moment, when it
                     runs. A group change that would now touch far more vouchers than when you approved stops and
                     asks you instead.
    report           overview · sales · network, sent to the people it tells
    ask_fit          a question Fit answers at that moment with its read-only tools; the answer is sent on

TELL
    me (bell + phone push), team (bell for everyone), emails (team members or — Owner/Admin only — any address),
    chat (a note in the team room), before_minutes (a heads-up before a timed run)

Safety
    * A mission exists only after a person approved its card (or created it on the Missions page).
    * Every run checks again that this person is still in the business, still has the right, and that the owner
      still allows Fit's "Missions" group (and the group of the change it makes). Otherwise it fails and says why.
    * Members are never cut off by a mission except by a router restart / shutdown, which everyone is warned about
      (their time is kept, as with schedules).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

from django.core.cache import cache
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.db.models import Sum, Count
from django.utils import timezone

logger = logging.getLogger('taptap.fit.missions')

LOCK_KEY = 'tt:fitmission:tick'
MISSED_AFTER = timedelta(minutes=30)        # TapTap was down: a timed mission this late is reported as missed
MAX_WINDOW = timedelta(hours=12)
MAX_EMAILS = 6
MAX_LOG = 30
MAX_PER_BUSINESS = 40
BULK_GROWTH = 2                             # a group change may touch at most 2× (+50) the vouchers it had when approved
WEEKDAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
NETWORK_PERMS = ('network.manage',)
VOUCHER_PERMS = ('vouchers.view', 'vouchers.support', 'vouchers.create')
SALES_PERMS = ('finance.view', 'sales.daily', 'dashboard.view')
PAGE = '/fit/missions/'


class MissionError(ValueError):
    pass


# condition: (label, what it needs, permissions (any of), default minutes)
CONDITIONS = {
    'online_below': ('People online at or below a number', 'value, router (empty = all routers), for_minutes', NETWORK_PERMS, 5),
    'online_above': ('People online at or above a number', 'value, router (empty = all routers), for_minutes', NETWORK_PERMS, 5),
    'router_offline': ('A router is offline', 'router, for_minutes (default 5)', NETWORK_PERMS, 5),
    'router_online': ('A router comes back online after being offline', 'router', NETWORK_PERMS, 0),
    'cpu_above': ('A router’s CPU stays high', 'router (TapTap Link), value %, for_minutes', NETWORK_PERMS, 5),
    'sales_reach': ('Sales today reach an amount', 'value (money)', SALES_PERMS, 0),
    'stock_below': ('Vouchers in stock for a plan fall below a number', 'plan, value', VOUCHER_PERMS, 0),
    'voucher_online': ('A voucher or member comes online', 'code', VOUCHER_PERMS, 0),
    'reports_open': ('Customer reports are waiting', 'value (how many, default 1)', VOUCHER_PERMS, 0),
}

REPORTS = {
    'overview': ('Overview', 'Vouchers, sales today, routers and open reports', ()),
    'sales': ('Sales today', 'Sales so far today against yesterday, best plans and agents', SALES_PERMS),
    'network': ('Network', 'Every router: online or not, people online, CPU', NETWORK_PERMS),
}


# ─────────────────────────────── who may ───────────────────────────────

def perms_for(user, business):
    """(perms, role) the person has in this business right now — None when they are no longer in it."""
    from .fit_bulk import ADMIN_PERM, ADMIN_ROLES
    from .permissions import ALL_PERMISSIONS
    if user is None or not user.is_active:
        return None
    if business.user_id == user.pk:
        return frozenset(ALL_PERMISSIONS) | {ADMIN_PERM}, 'owner'
    from .models_team import TeamMember
    m = TeamMember.objects.filter(business=business, user=user, is_active=True).first()
    if m is not None:
        p = frozenset(m.permissions)
        return (p | {ADMIN_PERM}) if m.role in ADMIN_ROLES else p, m.role
    if user.is_superuser:
        return frozenset(ALL_PERMISSIONS) | {ADMIN_PERM}, 'platform'
    return None


def _can(perms, need):
    return not need or any(p in perms for p in need)


# ─────────────────────────────── reading what the person said ───────────────────────────────

def _s(v):
    return str(v if v is not None else '').strip()


def _bool(v):
    return v is True or _s(v).lower() in ('1', 'true', 'yes', 'on')


def _int(v, lo, hi, name, default=None):
    if v in (None, ''):
        return default
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        raise MissionError(f'{name} must be a number.')
    if not lo <= n <= hi:
        raise MissionError(f'{name} must be between {lo} and {hi}.')
    return n


def _num(v, name):
    if v in (None, ''):
        return None
    try:
        return float(str(v).replace(',', '').strip().lstrip('Dd$'))
    except ValueError:
        raise MissionError(f'{name} must be a number.')


def _hm(v, name='time'):
    m = re.fullmatch(r'(\d{1,2})[:.h](\d{2})(?::\d{2})?', _s(v))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise MissionError(f'Give the {name} as HH:MM, e.g. 03:00.')
    return time(int(m.group(1)), int(m.group(2)))


def _aware(d: date, t: time) -> datetime:
    return timezone.make_aware(datetime.combine(d, t.replace(second=0, microsecond=0)), timezone.get_current_timezone())


def parse_at(v, now):
    """A moment from 'HH:MM' (the next time it comes), 'YYYY-MM-DD HH:MM' or an ISO time."""
    s = _s(v).replace('T', ' ')
    if not s:
        raise MissionError('When? Give a time, e.g. 23:00, or a date and time.')
    if re.fullmatch(r'\d{1,2}[:.h]\d{2}', s):
        t = _hm(s)
        local = timezone.localtime(now)
        at = _aware(local.date(), t)
        return at if at > now else _aware(local.date() + timedelta(days=1), t)
    m = re.fullmatch(r'(\d{4}-\d{2}-\d{2})[ ](\d{1,2}:\d{2})(?::\d{2})?(?:\.\d+)?([+-]\d{2}:?\d{2}|Z)?', s)
    if not m:
        raise MissionError('Give the time as HH:MM or YYYY-MM-DD HH:MM.')
    if m.group(3):
        try:
            return datetime.fromisoformat(s.replace('Z', '+00:00')).replace(second=0, microsecond=0)
        except ValueError:
            raise MissionError('That date does not exist.')
    try:
        return _aware(date.fromisoformat(m.group(1)), _hm(m.group(2)))
    except ValueError:
        raise MissionError('That date does not exist.')


def _weekdays(v):
    if isinstance(v, str):
        v = [x for x in re.split(r'[,\s]+', v.lower()) if x]
    out = set()
    for x in v or []:
        x = _s(x).lower()
        if x.isdigit() and 0 <= int(x) <= 6:
            out.add(int(x))
        elif x[:3] in WEEKDAYS:
            out.add(WEEKDAYS.index(x[:3]))
        elif x in ('weekdays', 'workdays'):
            out |= {0, 1, 2, 3, 4}
        elif x in ('weekend', 'weekends'):
            out |= {5, 6}
    return sorted(out)


def _router(business, name, required=False):
    name = _s(name)
    qs = business.routers.all()
    if not name or name.lower() in ('all', 'any', 'every', 'everywhere'):
        if required:
            if qs.count() == 1:
                return qs.first()
            raise MissionError('Which router? ' + ', '.join(qs.values_list('name', flat=True)[:12]))
        return None
    r = qs.filter(pk=int(name)).first() if name.isdigit() else qs.filter(name__iexact=name).first()
    if r is None:
        r = qs.filter(name__icontains=name).first()
    if r is None:
        raise MissionError(f'No router “{name}”. Routers: ' + ', '.join(qs.values_list('name', flat=True)[:12]))
    return r


def _people(business, user, perms, raw):
    """'me, Lamin, awa@x.gm' → [{'user': id, 'name'} | {'email', 'name'}]. Outside addresses: Owner/Admin only."""
    from .fit_bulk import ADMIN_PERM
    from .models_team import TeamMember
    parts = raw if isinstance(raw, list) else re.split(r'[,;]+|\s+and\s+', _s(raw))
    out, seen = [], set()
    team = [(business.user, 'owner')] + [(m.user, m.role) for m in TeamMember.objects.filter(business=business, is_active=True).select_related('user')]
    for p in parts:
        p = _s(p)
        if not p:
            continue
        if p.lower() in ('me', 'myself', 'i'):
            u = user
        else:
            low = p.lower()
            u = next((x for x, _ in team if (x.email or '').lower() == low), None) or \
                next((x for x, _ in team if (x.get_full_name() or '').lower() == low), None) or \
                next((x for x, _ in team if (x.first_name or '').lower() == low), None)
        if u is not None:
            if u.pk in seen:
                continue
            if not u.email:
                raise MissionError(f'{u.get_full_name() or u.username} has no email address in TapTap.')
            seen.add(u.pk)
            out.append({'user': u.pk, 'name': u.get_full_name() or u.first_name or u.email})
            continue
        try:
            validate_email(p)
        except ValidationError:
            raise MissionError(f'“{p}” is not someone in your team and not an email address.')
        if ADMIN_PERM not in perms:
            raise MissionError(f'Only the Owner or an Admin can have Fit email an address outside the team ({p}).')
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append({'email': p, 'name': p})
    if len(out) > MAX_EMAILS:
        raise MissionError(f'At most {MAX_EMAILS} people can be emailed by one mission.')
    return out


# ─────────────────────────────── building a mission from Fit's proposal ───────────────────────────────

def _timetable(when):
    return SimpleNamespace(repeat=when.get('repeat'), at_time=_hm(when.get('time')), run_on=None,
                           weekdays=when.get('weekdays') or [], month_day=int(when.get('month_day') or 1))


def _window(when, day):
    start = _aware(day, _hm(when['from'], 'window start'))
    end = _aware(day, _hm(when['to'], 'window end'))
    if end <= start:
        end += timedelta(days=1)                 # 23:00 → 05:00 runs past midnight
    return start, end


def _next_window(when, now):
    """The window that is open now, or the next one."""
    local = timezone.localtime(now).date()
    for i in (-1, 0, 1, 2):
        start, end = _window(when, local + timedelta(days=i))
        if end > now:
            return start, end
    return None, None


def first_due(trigger, when, now):
    from .router_schedules import next_after
    if trigger == 'at':
        return datetime.fromisoformat(when['at'])
    if trigger == 'repeat':
        return next_after(_timetable(when), now)
    if trigger == 'quiet':
        if when.get('day'):
            start, end = _window(when, date.fromisoformat(when['day']))
            return start if end > now else None
        return _next_window(when, now)[0]
    return now                                   # 'when': checked from now on


def build(business, user, perms, a, now=None):
    """Read a proposal (from Fit or the Missions page). Returns (clean args, fields, title, facts, warnings, danger, confirm).

    clean args can be passed to build() again (approval re-checks it) and give the same mission: relative times are
    turned into real ones the first time."""
    from . import fit_actions
    from .fit_bulk import ADMIN_PERM
    now = now or timezone.now()
    a = dict(a or {})
    clean = {}
    facts, warnings = [], []
    danger, confirm = 'ok', ''

    # ── WHEN ──
    trigger = _s(a.get('trigger')).lower()
    if not trigger:
        trigger = ('when' if a.get('condition') else 'quiet' if a.get('window_from') or a.get('max_online') not in (None, '')
                   else 'repeat' if a.get('repeat') else 'at')
    if trigger not in ('at', 'repeat', 'quiet', 'when'):
        raise MissionError('When should it run? at a time, on a timetable (repeat), at a quiet moment, or when something happens.')
    when = {}
    repeat = False
    if trigger == 'at':
        if a.get('in_minutes') not in (None, ''):
            at = now + timedelta(minutes=_int(a.get('in_minutes'), 1, 60 * 24 * 60, 'in_minutes'))
        else:
            at = parse_at(a.get('at') or a.get('time'), now)
        at = at.replace(second=0, microsecond=0)
        if at <= now - timedelta(minutes=1):
            raise MissionError('That moment has already passed — choose a time in the future.')
        if at > now + timedelta(days=90):
            raise MissionError('A mission can be set at most 90 days ahead.')
        when = {'at': timezone.localtime(at).isoformat()}
        clean.update(trigger='at', at=timezone.localtime(at).strftime('%Y-%m-%d %H:%M'))
    elif trigger == 'repeat':
        rep = _s(a.get('repeat')).lower() or 'daily'
        rep = {'every day': 'daily', 'day': 'daily', 'week': 'weekly', 'month': 'monthly'}.get(rep, rep)
        if rep not in ('daily', 'weekly', 'monthly'):
            raise MissionError('Repeat every day, every week (weekdays) or every month.')
        when = {'repeat': rep, 'time': _hm(a.get('time') or a.get('at')).strftime('%H:%M')}
        if rep == 'weekly':
            days = _weekdays(a.get('weekdays'))
            if not days:
                raise MissionError('Which days of the week? e.g. mon, wed, fri — or weekdays.')
            when['weekdays'] = days
        if rep == 'monthly':
            when['month_day'] = _int(a.get('month_day'), 1, 31, 'month_day', 1)
        repeat = True
        clean.update(trigger='repeat', repeat=rep, time=when['time'], weekdays=when.get('weekdays') or '',
                     month_day=when.get('month_day') or '')
    elif trigger == 'quiet':
        frm, to = a.get('window_from') or a.get('from'), a.get('window_to') or a.get('to')
        when = {'from': _hm(frm, 'window start').strftime('%H:%M'), 'to': _hm(to, 'window end').strftime('%H:%M')}
        start, end = _window(when, timezone.localtime(now).date())
        if end - start > MAX_WINDOW:
            raise MissionError('The window can be at most 12 hours.')
        r = _router(business, a.get('router'))
        if r is not None:
            when['router'] = r.pk
        mx = _int(a.get('max_online'), 0, 100000, 'max_online')
        if mx is not None:
            when['max_online'] = mx
        when['otherwise'] = 'skip' if _s(a.get('otherwise')).lower() == 'skip' else 'run'
        repeat = _bool(a.get('daily'))
        if not repeat:
            s0, e0 = _next_window(when, now)
            when['day'] = timezone.localtime(s0).date().isoformat()
        if not _can(perms, NETWORK_PERMS):
            raise MissionError('Waiting for a quiet moment needs the right to manage the network.')
        clean.update(trigger='quiet', window_from=when['from'], window_to=when['to'], router=r.pk if r else '',
                     max_online=mx if mx is not None else '', otherwise=when['otherwise'], daily=repeat, day=when.get('day', ''))
    else:
        cond = _s(a.get('condition')).lower()
        if cond not in CONDITIONS:
            raise MissionError('When what? One of: ' + '; '.join(f'{k} = {v[0]}' for k, v in CONDITIONS.items()))
        label, _, need, default_minutes = CONDITIONS[cond]
        if not _can(perms, need):
            raise MissionError(f'Your role cannot see what “{label}” watches.')
        when = {'condition': cond, 'for_minutes': _int(a.get('for_minutes'), 0, 24 * 60, 'for_minutes', default_minutes)}
        if cond in ('online_below', 'online_above', 'router_offline', 'router_online', 'cpu_above'):
            r = _router(business, a.get('router'), required=cond in ('router_offline', 'router_online', 'cpu_above'))
            if r is not None:
                when['router'] = r.pk
        if cond in ('online_below', 'online_above', 'cpu_above', 'sales_reach', 'stock_below', 'reports_open'):
            v = _num(a.get('value'), 'value')
            if v is None:
                if cond == 'reports_open':
                    v = 1
                else:
                    raise MissionError(f'“{label}” needs a number (value).')
            if cond == 'cpu_above' and not 1 <= v <= 100:
                raise MissionError('CPU is between 1 and 100 %.')
            when['value'] = v
        if cond == 'stock_below':
            plan = _s(a.get('plan'))
            names = {p.lower(): p for p in business.plans.values_list('name', flat=True)}
            if plan.lower() not in names:
                raise MissionError(f'No plan “{plan}”. Plans: ' + ', '.join(sorted(names.values())[:20]))
            when['plan'] = names[plan.lower()]
        if cond == 'voucher_online':
            code = _s(a.get('code')).replace(' ', '')
            v = business.vouchers.filter(code__iexact=code).first() if code else None
            if v is None:
                raise MissionError(f'No voucher or member “{code}” in {business.business_name}.')
            when['code'] = v.code
        if a.get('window_from') and a.get('window_to'):
            when['from'] = _hm(a.get('window_from'), 'window start').strftime('%H:%M')
            when['to'] = _hm(a.get('window_to'), 'window end').strftime('%H:%M')
        repeat = _bool(a.get('every_time'))
        clean.update(trigger='when', condition=cond, for_minutes=when['for_minutes'], router=when.get('router', ''),
                     value=when.get('value', ''), plan=when.get('plan', ''), code=when.get('code', ''),
                     window_from=when.get('from', ''), window_to=when.get('to', ''), every_time=repeat)

    # ── DO ──
    task = _s(a.get('mission_task') or a.get('task_kind')).lower() or ('action' if a.get('action_kind') else 'notify')
    task = {'restart': 'restart_router', 'reboot': 'restart_router', 'shutdown': 'shutdown_router', 'tell': 'notify',
            'ask': 'ask_fit'}.get(task, task)
    if task not in ('notify', 'restart_router', 'shutdown_router', 'action', 'report', 'ask_fit'):
        raise MissionError('Do what? notify, restart_router, shutdown_router, action, report or ask_fit.')
    tp = {}
    group = None
    message = _s(a.get('message'))[:600]
    if task in ('restart_router', 'shutdown_router'):
        if not _can(perms, NETWORK_PERMS):
            raise MissionError('Restarting a router needs the right to manage the network.')
        r = _router(business, a.get('router') or (when.get('router') if trigger in ('quiet', 'when') else ''), required=True)
        tp['router'] = r.pk
        group = 'router'
        clean['router'] = r.pk
        if task == 'shutdown_router':
            if not _bool(a.get('understood')):
                raise MissionError(f'A shutdown switches {r.name} OFF: it stays off until someone powers it on again. '
                                   'Say that you understand (understood=true).')
            clean['understood'] = True
            danger = 'danger'
            warnings.append(f'{r.name} is switched OFF and stays off until someone unplugs it and plugs it back in.')
        else:
            danger = 'warn'
        warnings.append(f'Everyone online on {r.name} drops for a minute or two (members keep their time).')
        if trigger == 'quiet' and when.get('router') and when['router'] != r.pk:
            warnings.append('The quiet moment is measured on another router than the one restarted.')
        if trigger == 'quiet' and not when.get('router'):
            when['router'] = r.pk                    # the quiet moment of the router that restarts
            clean['router'] = r.pk
    elif task == 'action':
        kind = _s(a.get('action_kind'))
        if kind not in fit_actions.KINDS or kind.endswith('_mission'):
            raise MissionError('Which change? action_kind must be one of: ' + ', '.join(k for k in fit_actions.KINDS if not k.endswith('_mission')))
        args = a.get('action_args') or {}
        if isinstance(args, str):
            try:
                args = json.loads(args or '{}')
            except ValueError:
                raise MissionError('action_args must be an object, e.g. {"code": "ABC123", "minutes": 60}.')
        if not isinstance(args, dict):
            raise MissionError('action_args must be an object.')
        args = {k: v for k, v in args.items() if not str(k).startswith('_') and k not in ('ids', 'kind')}
        g, need, *_rest, kdanger = fit_actions.KINDS[kind]
        if not fit_actions.has_perm(perms, need) or not fit_actions._extra_perms_ok(kind, args, perms):
            raise MissionError('Your role does not allow that change, so a mission cannot do it either.')
        group = g
        try:
            params, title, detail, _link = fit_actions._validate(business, user, kind, dict(args))
            facts.append(['The change', f'{title}' + (f' — {detail}' if detail else '')])
            extra = params.get('_card') if isinstance(params, dict) else None
            if isinstance(extra, dict):
                warnings += [str(w) for w in (extra.get('warnings') or [])][:6]
                confirm = _s(extra.get('confirm'))
            if isinstance(params, dict) and isinstance(params.get('ids'), list):
                tp['approved_count'] = len(params['ids'])
                facts.append(['Vouchers', f'{len(params["ids"])} match right now. When it runs, Fit takes the ones that match '
                                          f'then — and stops to ask you if that is far more.'])
            danger = (params.get('danger') if isinstance(params, dict) and params.get('danger') else kdanger) or 'ok'
        except fit_actions.FitActionError as exc:
            warnings.append(f'Right now this change would not work: {exc} Fit checks again when the moment comes.')
        tp.update(kind=kind, args=args)
        clean.update(action_kind=kind, action_args=args)
    elif task == 'report':
        rep = _s(a.get('report')).lower() or 'overview'
        if rep not in REPORTS:
            raise MissionError('Which report? ' + ', '.join(REPORTS))
        if not _can(perms, REPORTS[rep][2]):
            raise MissionError(f'Your role cannot see the {REPORTS[rep][0].lower()} report.')
        tp['report'] = rep
        clean['report'] = rep
    elif task == 'ask_fit':
        q = _s(a.get('question'))[:800]
        if len(q) < 6:
            raise MissionError('What should Fit look into at that moment? (question)')
        tp['question'] = q
        clean['question'] = q
        facts.append(['How', 'Fit answers with its read-only tools at that moment: it looks, it never changes anything.'])
    if task == 'notify' and not message:
        raise MissionError('What should the message say? (message)')
    clean.update(mission_task=task, message=message)

    # ── TELL ──
    tell = {'me': True if a.get('tell_me') in (None, '') else _bool(a.get('tell_me')), 'team': _bool(a.get('tell_team')),
            'chat': 'team' if _bool(a.get('chat_team')) or _s(a.get('chat')).lower() == 'team' else '',
            'before_minutes': _int(a.get('before_minutes'), 0, 24 * 60, 'before_minutes', 0) or 0, 'message': message}
    emails = _people(business, user, perms, a.get('email_to') or '')
    if _bool(a.get('email_me')) and not any(e.get('user') == user.pk for e in emails):
        if not user.email:
            raise MissionError('You have no email address in TapTap.')
        emails.insert(0, {'user': user.pk, 'name': user.get_full_name() or user.first_name or user.email})
    tell['emails'] = emails
    if task in ('notify', 'report', 'ask_fit') and not (tell['me'] or tell['team'] or tell['chat'] or emails):
        raise MissionError('Who should be told? (tell_me, tell_team, chat_team or email_to)')
    if tell['before_minutes'] and trigger == 'when':
        tell['before_minutes'] = 0                   # nobody knows beforehand when a condition comes true
    clean.update(tell_me=tell['me'], tell_team=tell['team'], chat_team=bool(tell['chat']), before_minutes=tell['before_minutes'],
                 email_to=_emails_clean(emails, user))

    # ── how long ──
    expires = None
    if a.get('until') not in (None, ''):
        expires = parse_at(a.get('until'), now)
    elif a.get('hours') not in (None, ''):
        expires = now + timedelta(hours=_int(a.get('hours'), 1, 24 * 365, 'hours'))
    if expires is not None:
        clean['until'] = timezone.localtime(expires).strftime('%Y-%m-%d %H:%M')
    cooldown = _int(a.get('cooldown_minutes'), 1, 7 * 24 * 60, 'cooldown_minutes', 60)
    clean['cooldown_minutes'] = cooldown

    nxt = first_due(trigger, when, now)
    if nxt is None:
        raise MissionError('That moment has already passed — choose a time in the future.')

    fields = {'trigger': trigger, 'when': when, 'task': task, 'task_params': tp, 'tell': tell, 'repeat': repeat,
              'cooldown_minutes': cooldown, 'next_at': nxt, 'expires_at': expires}
    probe = SimpleNamespace(business=business, **fields)
    name = _s(a.get('name'))[:120] or _auto_name(probe)
    clean['name'] = name
    fields['name'] = name
    wtext = describe_when(probe)
    t = describe_task(probe)
    facts = [['When', wtext[5:] if wtext.startswith('when ') else wtext], ['Then', t[:1].upper() + t[1:]],
             ['Tells', describe_tell(probe)]] + facts
    if expires:
        facts.append(['Until', f'{timezone.localtime(expires):%a %d %b %H:%M}'])
    facts.append(['Rights', 'It runs with yours: if you leave the business or lose the right, it stops.'])
    fields['_group'] = group
    return clean, fields, f'Mission: {name}', facts, warnings, danger, confirm


def _emails_clean(emails, user):
    """The addresses again, in a form build() reads back the same way (team members by their email)."""
    from django.contrib.auth.models import User
    ids = {e['user'] for e in emails if e.get('user')}
    by_id = {u.pk: u.email for u in User.objects.filter(pk__in=ids)}
    return ', '.join((by_id.get(e['user']) or '') if e.get('user') else e.get('email', '') for e in emails)


def _emails_text(emails):
    return ', '.join(e.get('email') or e.get('name') or '' for e in emails)


def _auto_name(m):
    t = describe_task(m)
    return (t[:1].upper() + t[1:])[:120]


# ─────────────────────────────── words ───────────────────────────────

def _router_name(business, pk):
    if not pk:
        return 'all routers'
    r = business.routers.filter(pk=pk).only('name').first()
    return r.name if r else f'router #{pk}'


def _fmt_at(dt):
    from .router_schedules import _hm as hm
    local = timezone.localtime(dt)
    today = timezone.localdate()
    if local.date() == today:
        return f'today at {hm(dt)}'
    if local.date() == today + timedelta(days=1):
        return f'tomorrow at {hm(dt)}'
    return f'{local:%a %d %b} at {hm(dt)}'


def describe_when(m):
    w = m.when or {}
    if m.trigger == 'at':
        return _fmt_at(datetime.fromisoformat(w['at']))
    if m.trigger == 'repeat':
        from .router_schedules import describe
        text = describe(_timetable(w))
        return text if text.startswith(('Every', 'Once', 'Weekdays', 'Weekends')) else f'Every {text}'
    if m.trigger == 'quiet':
        who = _router_name(m.business, w.get('router'))
        day = 'every day' if m.repeat else (_day_word(w.get('day')) if w.get('day') else 'once')
        how = (f'as soon as {int(w["max_online"])} or fewer people are online on {who}' if w.get('max_online') is not None
               else f'at the quietest moment on {who}')
        tail = '' if w.get('max_online') is None else (' (if it never gets that quiet: at the end of the window anyway)' if w.get('otherwise') != 'skip'
                                                       else ' (if it never gets that quiet: not at all that day)')
        return f'between {w["from"]} and {w["to"]} {day}, {how}{tail}'
    c = w.get('condition')
    label = CONDITIONS.get(c, (c,))[0]
    v = w.get('value')
    vs = f'{v:g}' if isinstance(v, (int, float)) else ''
    who = _router_name(m.business, w.get('router'))
    text = {
        'online_below': f'{vs} or fewer people are online on {who}',
        'online_above': f'{vs} or more people are online on {who}',
        'router_offline': f'{who} is offline',
        'router_online': f'{who} comes back online',
        'cpu_above': f'{who}’s CPU is at {vs} % or more',
        'sales_reach': f'sales today reach {getattr(m.business, "currency", "") or ""}{vs}',
        'stock_below': f'fewer than {vs} {w.get("plan")} vouchers are in stock',
        'voucher_online': f'{w.get("code")} comes online',
        'reports_open': f'{vs} or more customer reports are waiting',
    }.get(c, label)
    mins = int(w.get('for_minutes') or 0)
    if mins and c not in ('router_online', 'sales_reach', 'stock_below', 'voucher_online', 'reports_open'):
        text += f' for {mins} min'
    if w.get('from') and w.get('to'):
        text += f', only between {w["from"]} and {w["to"]}'
    return 'when ' + text + (' — every time it happens' if m.repeat else ' — once')


def _day_word(iso):
    d = date.fromisoformat(iso)
    today = timezone.localdate()
    return 'today' if d == today else 'tomorrow' if d == today + timedelta(days=1) else f'on {d:%a %d %b}'


def describe_task(m):
    tp = m.task_params or {}
    if m.task in ('restart_router', 'shutdown_router'):
        name = _router_name(m.business, tp.get('router'))
        return f'restart {name}' if m.task == 'restart_router' else f'shut {name} down (power off)'
    if m.task == 'action':
        a = tp.get('args') or {}
        bits = ', '.join(f'{k}={v}' for k, v in a.items() if v not in (None, '', [], {}))[:120]
        return f'{tp.get("kind", "").replace("_", " ")}' + (f' ({bits})' if bits else '')
    if m.task == 'report':
        return f'send the {REPORTS.get(tp.get("report"), ("?",))[0].lower()} report'
    if m.task == 'ask_fit':
        return f'ask Fit “{tp.get("question", "")[:80]}” and send the answer'
    return f'tell: “{(m.tell or {}).get("message", "")[:80]}”'


def describe_tell(m):
    t = m.tell or {}
    out = []
    if t.get('me'):
        out.append('you (bell + phone)')
    if t.get('team'):
        out.append('the whole team (bell)')
    if t.get('chat'):
        out.append('the team chat room')
    if t.get('emails'):
        out.append('email to ' + _emails_text(t['emails']))
    if t.get('before_minutes'):
        out.append(f'heads-up {t["before_minutes"]} min before')
    return ', '.join(out) or 'nobody'


# ─────────────────────────────── telling people ───────────────────────────────

def _addresses(m):
    from django.contrib.auth.models import User
    out = []
    for e in (m.tell or {}).get('emails') or []:
        if e.get('user'):
            u = User.objects.filter(pk=e['user'], is_active=True).first()
            if u is None or not u.email or perms_for(u, m.business) is None:
                continue                              # left the business: not told any more
            out.append((u.email, u.get_full_name() or u.first_name or u.email))
        elif e.get('email'):
            out.append((e['email'], e.get('name') or e['email']))
    return out


def _email(m, subject, body, level):
    from django.conf import settings
    from django.core.mail import EmailMultiAlternatives, get_connection
    from django.template.loader import render_to_string
    from .notify import SEVERITY_COLOR
    to = _addresses(m)
    if not to:
        return []
    site = (getattr(settings, 'SITE_URL', '') or '').rstrip('/')
    b = m.business
    sent = []
    try:
        conn = get_connection(fail_silently=False)
    except Exception:
        logger.exception('mission email connection')
        return []
    for email, name in to:
        ctx = {'business': b, 'subject': subject, 'body': body, 'name': name, 'mission': m.name, 'link': f'{site}{PAGE}',
               'color': SEVERITY_COLOR.get({'danger': 'critical', 'warning': 'warning', 'good': 'good'}.get(level, 'info'), '#1769e0'),
               'by': (m.created_by.get_full_name() or m.created_by.first_name or m.created_by.email) if m.created_by else ''}
        try:
            msg = EmailMultiAlternatives(subject=f'[{b.business_name}] {subject}'[:180],
                                         body=render_to_string('core/email/fit_mission.txt', ctx),
                                         from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None), to=[email], connection=conn)
            msg.attach_alternative(render_to_string('core/email/fit_mission.html', ctx), 'text/html')
            msg.send()
            sent.append(email)
        except Exception as exc:                      # one bad address must not stop the others
            logger.warning('mission %s email to %s failed: %s', m.pk, email, exc)
    return sent


def tell(m, title, body, level='info', email=True, chat=True):
    """Bell (me / team), email to the chosen people, a note in the team room. Returns a short 'told …' text."""
    from .models_events import EventAlert
    t = m.tell or {}
    told = []
    try:
        if t.get('team'):
            EventAlert.objects.create(business=m.business, user=None, kind='fit_mission', level=_lvl(level), title=title[:160],
                                      body=body[:400], link=PAGE, sound=True, desktop=True)
            told.append('team')
        elif t.get('me') and m.created_by_id:
            EventAlert.objects.create(business=m.business, user=m.created_by, kind='fit_mission', level=_lvl(level), title=title[:160],
                                      body=body[:400], link=PAGE, sound=True, desktop=True)
            told.append('you')
    except Exception:
        logger.exception('mission %s bell', m.pk)
    if email:
        sent = _email(m, title, body, level)
        if sent:
            told.append('email ' + ', '.join(sent))
    if chat and t.get('chat') == 'team' and m.created_by_id:
        try:
            from . import chat as ch
            ch.post(ch.team_thread(m.business), m.created_by, f'🤖 Fit mission “{m.name}”: {title}\n{body}'[:3900], system=True)
            told.append('team chat')
        except Exception:
            logger.exception('mission %s chat', m.pk)
    try:
        from .live import push_event
        push_event(m.business_id, f'Fit mission: {title}', 'info')
    except Exception:
        pass
    return ', '.join(told)


def _lvl(level):
    return {'danger': 'danger', 'warning': 'warning', 'good': 'info'}.get(level, 'info')


def _note(m, text, ok=True, now=None):
    now = now or timezone.now()
    log = list(m.log or [])[-(MAX_LOG - 1):]
    log.append({'at': timezone.localtime(now).isoformat(), 'text': text[:400], 'ok': ok})
    m.log = log


# ─────────────────────────────── what is true right now ───────────────────────────────

def _online(business, router_id):
    from .router_schedules import online_count
    routers = business.routers.filter(pk=router_id) if router_id else business.routers.all()
    total, known = 0, False
    for r in routers:
        n = online_count(r)
        if n is not None:
            total += n
            known = True
    return total if known else None


def check(m, now):
    """(true / false / None = cannot tell, a few words about what TapTap sees now)."""
    from .router_schedules import is_online
    w = m.when or {}
    b = m.business
    c = w.get('condition')
    v = w.get('value')
    if c in ('online_below', 'online_above'):
        n = _online(b, w.get('router'))
        if n is None:
            return None, 'TapTap does not know how many people are online yet'
        return (n <= v if c == 'online_below' else n >= v), f'{n} online'
    if c in ('router_offline', 'router_online', 'cpu_above'):
        r = b.routers.filter(pk=w.get('router')).first()
        if r is None:
            return None, 'the router is no longer in this business'
        on = is_online(r)
        if c == 'router_offline':
            return (not on), f'{r.name} is {"online" if on else "offline"}'
        if c == 'router_online':
            st = m.state or {}
            if not on:
                st['was_off'] = True
                m.state = st
                return False, f'{r.name} is offline'
            return bool(st.get('was_off')), f'{r.name} is online'
        try:
            cpu = r.agent.cpu_load if r.connection_mode == 'agent' else None
        except Exception:
            cpu = None
        if cpu is None:
            return None, f'no CPU reading from {r.name} (TapTap Link routers report it)'
        return cpu >= v, f'CPU {cpu} %'
    if c == 'sales_reach':
        start = _aware(timezone.localdate(), time(0, 0))
        s = b.sales.filter(sold_at__gte=start).aggregate(v=Sum('amount'), n=Count('id'))
        amount = float(s['v'] or 0)
        return amount >= v, f'{b.currency or ""}{amount:,.0f} from {s["n"] or 0} sale(s) today'
    if c == 'stock_below':
        n = b.vouchers.filter(deleted_at__isnull=True, status='active', used_at__isnull=True, sold_at__isnull=True,
                              plan_name__iexact=w.get('plan')).count()
        return n < v, f'{n} {w.get("plan")} vouchers in stock'
    if c == 'voucher_online':
        code = str(w.get('code') or '').upper()
        for r in b.routers.all():
            users = (cache.get(f'tt:tr:users:{r.pk}') or {}).get('users') or {}
            if code in {str(u).upper() for u in users}:
                return True, f'{w.get("code")} is online on {r.name}'
        return False, f'{w.get("code")} is not online'
    if c == 'reports_open':
        from .models_voucher_reports import VoucherReport
        n = VoucherReport.objects.filter(business=b, status='open').count()
        return n >= (v or 1), f'{n} customer report(s) waiting'
    return None, 'unknown condition'


def _in_window(w, now):
    if not (w.get('from') and w.get('to')):
        return True
    start, end = _next_window(w, now)
    return start is not None and start <= now < end


# ─────────────────────────────── doing it ───────────────────────────────

def _fit_ctx(m, perms, role, settings, conversation=None):
    from . import fit_tools
    from .permissions import ROLES
    return fit_tools.FitContext(business=m.business, user=m.created_by, perms=perms, settings=settings,
                                conversation=conversation, role_label=ROLES[role][0] if role in ROLES else ('Owner' if role == 'owner' else 'Staff'),
                                read_only=True)


def report_text(m, perms, role, settings):
    """A short plain report for the email / bell (from the same lookups Fit uses)."""
    from . import fit_tools
    ctx = _fit_ctx(m, perms, role, settings)
    rep = (m.task_params or {}).get('report') or 'overview'
    b = m.business
    lines = []
    if rep in ('overview', 'sales') and ctx.can(SALES_PERMS):
        start = _aware(timezone.localdate(), time(0, 0))
        y0 = start - timedelta(days=1)
        today = b.sales.filter(sold_at__gte=start).aggregate(v=Sum('amount'), n=Count('id'))
        same_time = b.sales.filter(sold_at__gte=y0, sold_at__lt=timezone.now() - timedelta(days=1)).aggregate(v=Sum('amount'))
        cur = b.currency or ''
        t, y = float(today['v'] or 0), float(same_time['v'] or 0)
        diff = f' ({(t - y) / y * 100:+.0f}% vs yesterday at this time)' if y else ''
        lines.append(f'Sales today: {cur}{t:,.0f} from {today["n"] or 0} voucher(s){diff}.')
        if rep == 'sales':
            top = list(b.sales.filter(sold_at__gte=start).values('plan_name').annotate(n=Count('id'), v=Sum('amount')).order_by('-v')[:3]) \
                if hasattr(b.sales.model, 'plan_name') else []
            if top:
                lines.append('Best plans: ' + ', '.join(f'{x["plan_name"] or "?"} {cur}{float(x["v"] or 0):,.0f} ({x["n"]})' for x in top) + '.')
    if rep in ('overview', 'network') and ctx.can(NETWORK_PERMS):
        from .router_schedules import is_online, online_count
        rows = []
        for r in b.routers.select_related('agent').all()[:20]:
            on = is_online(r)
            n = online_count(r)
            cpu = None
            try:
                cpu = r.agent.cpu_load if r.connection_mode == 'agent' else None
            except Exception:
                pass
            rows.append(f'{r.name}: {"online" if on else "OFFLINE"}' + (f', {n} people' if n is not None else '') + (f', CPU {cpu} %' if cpu is not None else ''))
        lines.append('Routers — ' + ('; '.join(rows) if rows else 'none') + '.')
    if rep == 'overview':
        ov = fit_tools.t_overview(ctx)
        v = ov.get('vouchers') or {}
        if v:
            lines.append(f'Vouchers: {v.get("active_unused_stock", 0)} in stock, {v.get("in_use_now", 0)} in use, {v.get("frozen", 0)} frozen.')
        rp = (ov.get('open_customer_reports') or {}).get('count')
        if rp:
            lines.append(f'{rp} customer report(s) waiting.')
    return ' '.join(lines) or 'Nothing to report that your role can see.'


def ask_fit(m, perms, role, settings):
    from . import fit_ai
    from .models_fit import FitConversation
    if not fit_ai.ready():
        raise MissionError('Fit’s AI is not set up on this server right now.')
    conv = FitConversation.objects.create(business=m.business, user=m.created_by, title=f'Mission: {m.name}'[:120])
    ctx = _fit_ctx(m, perms, role, settings, conversation=conv)
    q = (m.task_params or {}).get('question') or ''
    out = fit_ai.ask(ctx, q + '\n\n(This is a scheduled mission: answer in a short report. You cannot propose changes now.)')
    return re.sub(r'\[([^\]]+)\]\((/[^)]*)\)', r'\1', out.get('reply') or '').strip()[:3000]


def _restart(m, user, now):
    from . import router_schedules as rs
    from .models_router_schedule import RouterScheduleRun
    r = m.business.routers.filter(pk=(m.task_params or {}).get('router')).first()
    if r is None:
        raise MissionError('That router is no longer in this business.')
    action = 'restart' if m.task == 'restart_router' else 'shutdown'
    n = rs.online_count(r)
    ok, msg = rs.execute(r, action, user=user, label=f'Fit mission “{m.name}”')
    if not ok:
        raise MissionError(msg)
    run = RouterScheduleRun.objects.create(schedule=None, router=r, action=action, planned_at=now, by=user, status='sent',
                                           ran_at=now, online_at_run=n, result=f'Fit mission “{m.name}”: {msg}'[:300])
    rs._mark_down(r, action)
    st = dict(m.state or {})
    st['follow'] = run.pk
    m.state = st
    who = f' {n} {"person was" if n == 1 else "people were"} online.' if n else (' Nobody was online.' if n == 0 else '')
    return f'{msg}{who} TapTap tells you when it is back.' if action == 'restart' else f'{msg}{who}'


def _action(m, user, perms, settings):
    from . import fit_actions
    from .models_fit import FitAction
    tp = m.task_params or {}
    kind = tp.get('kind')
    spec = fit_actions.KINDS.get(kind)
    if not spec:
        raise MissionError(f'Unknown change “{kind}”.')
    group, need, _, do, _ = spec
    if not settings.allows(group):
        raise MissionError(f'The owner no longer allows Fit to make “{fit_actions.GROUPS[group][0]}” changes.')
    args = dict(tp.get('args') or {})
    if not fit_actions.has_perm(perms, need) or not fit_actions._extra_perms_ok(kind, args, perms):
        raise MissionError('The person who set this mission no longer has the right to make this change.')
    params, title, detail, _ = fit_actions._validate(m.business, user, kind, args)
    approved = tp.get('approved_count')
    if isinstance(params, dict) and isinstance(params.get('ids'), list) and approved is not None:
        n = len(params['ids'])
        if n > max(approved * BULK_GROWTH, approved + 50):
            raise MissionError(f'{n} vouchers match now, against {approved} when you approved it. Fit stopped to be safe: '
                               'check and set the mission again if that is right.')
    result = str(do(m.business, params, user) or 'Done.')
    FitAction.objects.create(business=m.business, proposed_to=user, kind=kind, params=params, title=f'{title} (mission)'[:200],
                             detail=(detail or '')[:500], status='done', result=result[:500], decided_by=user, decided_at=timezone.now())
    return f'{title}: {result}'


def perform(m, now=None):
    """Do the task now. Returns (ok, result text). Every right is checked again here."""
    from .models_fit import FitSettings
    from .utils import log
    now = now or timezone.now()
    user = m.created_by
    who = perms_for(user, m.business)
    if who is None:
        return False, 'The person who set this mission is no longer in the business, so it cannot run.'
    perms, role = who
    settings, _ = FitSettings.objects.get_or_create(business=m.business)
    if not settings.enabled:
        return False, 'Fit is switched off for this business.'
    if not settings.allows('missions'):
        return False, 'The owner no longer allows Fit missions (Fit settings → Missions).'
    try:
        if m.task in ('restart_router', 'shutdown_router'):
            if not settings.allows('router'):
                raise MissionError('The owner does not allow Fit “Router control”, which a restart needs.')
            if not _can(perms, NETWORK_PERMS):
                raise MissionError('The person who set it can no longer manage the network.')
            result = _restart(m, user, now)
        elif m.task == 'action':
            result = _action(m, user, perms, settings)
        elif m.task == 'report':
            result = report_text(m, perms, role, settings)
        elif m.task == 'ask_fit':
            result = ask_fit(m, perms, role, settings)
        else:
            result = (m.tell or {}).get('message') or 'Its moment has come.'
        ok = True
    except Exception as exc:  # noqa: BLE001 — services raise ValueError subclasses, routers anything
        if not isinstance(exc, ValueError):
            logger.exception('mission %s', m.pk)
        ok, result = False, str(exc) or 'It did not work.'
    try:
        log(m.business, 'Fit mission', f'{m.name}: {"done" if ok else "failed"} — {result[:200]}', status='Success' if ok else 'Failed')
    except Exception:
        pass
    return ok, result


# ─────────────────────────────── the clock ───────────────────────────────

def _save(m, *fields):
    m.save(update_fields=list(fields) + ['updated_at'])


def _finish(m, ok, result, now, why=''):
    """After a run: tell people, then wait for the next time or stop."""
    from .router_schedules import next_after
    m.runs += 1
    m.last_run_at = now
    m.last_result = result[:500]
    _note(m, (why + ' ' if why else '') + result, ok, now)
    title = (f'✅ {m.name}' if ok else f'⚠️ {m.name} did not work')
    body = (why + ' ' if why else '') + (result if ok else f'{result}')
    if m.task in ('notify',) and ok:
        title = f'🔔 {m.name}'
    told = tell(m, title, body, level='good' if ok else 'danger')
    if told:
        _note(m, f'Told {told}.', True, now)
    st = dict(m.state or {})
    st.pop('since', None)
    st.pop('seen', None)
    st.pop('warned', None)
    if m.trigger == 'at' or not m.repeat:
        m.status = 'done' if ok else 'failed'
        m.next_at = None
    elif m.trigger == 'repeat':
        m.next_at = next_after(_timetable(m.when), now)
    elif m.trigger == 'quiet':
        _, end = _next_window(m.when, now)
        m.next_at = _next_window(m.when, end + timedelta(minutes=1))[0] if end else None
    else:
        st['armed'] = False                       # every time: only after it was false again
        st['cool_until'] = (now + timedelta(minutes=m.cooldown_minutes)).isoformat()
    m.state = st
    _save(m, 'runs', 'last_run_at', 'last_result', 'log', 'status', 'next_at', 'state')


def _heads_up(m, due, now):
    mins = int((m.tell or {}).get('before_minutes') or 0)
    if not mins or now < due - timedelta(minutes=mins) or now >= due:
        return False
    st = dict(m.state or {})
    if st.get('warned') == due.isoformat():
        return False
    st['warned'] = due.isoformat()
    m.state = st
    what = describe_task(m)
    extra = ' Everyone online drops for a minute or two.' if m.task == 'restart_router' else \
        ' It stays OFF until someone powers it on.' if m.task == 'shutdown_router' else ''
    when = f'between {m.when["from"]} and {m.when["to"]}' if m.trigger == 'quiet' else f'at {timezone.localtime(due):%H:%M}'
    told = tell(m, f'⏰ In {mins} min: {m.name}', f'Fit will {what} {when}.{extra} Open Missions to pause it.', level='warning', chat=False)
    _note(m, f'Heads-up sent ({told or "nobody"}).', True, now)
    _save(m, 'state', 'log')
    return True


def process(m, now=None):
    """One waiting mission. Returns what happened ('ran', 'waiting', 'missed', 'expired', None)."""
    now = now or timezone.now()
    if m.expires_at and now >= m.expires_at:
        m.status = 'expired'
        _note(m, 'Its time limit passed.', False, now)
        _save(m, 'status', 'log')
        tell(m, f'{m.name} ended', 'Its time limit passed' + (' before it ever ran.' if not m.runs else '.'), email=False, chat=False)
        return 'expired'
    if m.trigger in ('at', 'repeat'):
        due = m.next_at
        if due is None:
            m.status = 'done'
            _save(m, 'status')
            return None
        if now < due:
            _heads_up(m, due, now)
            return None
        if now - due > MISSED_AFTER:
            why = f'It was due {_fmt_at(due)} but TapTap could not run it in time (the server was not running).'
            _note(m, why, False, now)
            tell(m, f'{m.name} was missed', why + (' The next one is as planned.' if m.trigger == 'repeat' else ''), level='warning', chat=False)
            if m.trigger == 'repeat':
                from .router_schedules import next_after
                m.next_at = next_after(_timetable(m.when), now)
            else:
                m.status, m.next_at = 'missed', None
            _save(m, 'status', 'next_at', 'log')
            return 'missed'
        ok, result = perform(m, now)
        _finish(m, ok, result, now)
        return 'ran'
    if m.trigger == 'quiet':
        return _process_quiet(m, now)
    return _process_when(m, now)


def _process_quiet(m, now):
    w = m.when or {}
    start = m.next_at
    if start is None:
        m.status = 'done'
        _save(m, 'status')
        return None
    _, end = _window(w, timezone.localtime(start).date())
    if now < start:
        _heads_up(m, start, now)
        return None
    st = dict(m.state or {})
    if now > end + timedelta(minutes=10) and not st.get('seen'):
        why = f'Its window ({w["from"]}–{w["to"]}) passed while TapTap was not running.'
        _note(m, why, False, now)
        tell(m, f'{m.name} was missed', why, level='warning', chat=False)
        if m.repeat:
            m.next_at = _next_window(w, end + timedelta(minutes=1))[0]
        else:
            m.status, m.next_at = 'missed', None
        m.state = {k: v for k, v in st.items() if k not in ('seen',)}
        _save(m, 'status', 'next_at', 'log', 'state')
        return 'missed'
    n = _online(m.business, w.get('router'))
    seen = list(st.get('seen') or [])[-300:]
    if n is not None:
        seen.append([int((now - start).total_seconds() // 60), n])
    st['seen'] = seen
    m.state = st
    mx = w.get('max_online')
    counts = [x[1] for x in seen[:-1] if isinstance(x, list) and len(x) == 2]
    at_end = now >= end
    go, why = False, ''
    if mx is not None:
        if n is not None and n <= mx:
            go, why = True, f'{n} {"person" if n == 1 else "people"} online (you asked for {mx} or fewer).'
        elif at_end:
            low = min([x[1] for x in seen if isinstance(x, list) and len(x) == 2] or [None]) if seen else None
            if w.get('otherwise') == 'skip':
                why = f'It never got down to {mx} online between {w["from"]} and {w["to"]}' + (f' (lowest: {low}).' if low is not None else '.')
                _note(m, why + ' Not run, as you asked.', False, now)
                tell(m, f'{m.name}: not run this time', why + ' Not run, as you asked.', level='warning', chat=False)
                if m.repeat:
                    m.next_at = _next_window(w, end + timedelta(minutes=1))[0]
                else:
                    m.status, m.next_at = 'done', None
                m.state = {k: v for k, v in st.items() if k != 'seen'}
                _save(m, 'status', 'next_at', 'log', 'state')
                return 'skipped'
            go, why = True, f'End of the window: it never got down to {mx} online' + (f' (lowest {low}); ' if low is not None else '; ') + f'ran with {n if n is not None else "?"} online.'
    else:
        if n is not None and n <= 0:
            go, why = True, 'Nobody was online.'
        elif at_end:
            go, why = True, f'End of the window; {n if n is not None else "?"} online.'
        elif n is not None and now >= start + (end - start) * 2 / 3 and counts and n <= min(counts):
            go, why = True, f'The quietest moment of the window: {n} online (from {max(counts + [n])} at most).'
    if not go:
        _save(m, 'state')
        return 'waiting'
    ok, result = perform(m, now)
    _finish(m, ok, result, now, why)
    return 'ran'


def _process_when(m, now):
    w = m.when or {}
    st = dict(m.state or {})
    if not _in_window(w, now):
        if st.get('since'):
            st.pop('since', None)
            m.state = st
            _save(m, 'state')
        return None
    true, words = check(m, now)
    st = dict(m.state or {})                       # check() may note something (router_online: was_off)
    st['now'] = words
    if true is None:
        m.state = st
        _save(m, 'state')
        return None
    if not true:
        st.pop('since', None)
        st['armed'] = True
        m.state = st
        _save(m, 'state')
        return None
    since = datetime.fromisoformat(st['since']) if st.get('since') else now
    st['since'] = since.isoformat()
    armed = st.get('armed', True)
    cool = datetime.fromisoformat(st['cool_until']) if st.get('cool_until') else None
    mins = int(w.get('for_minutes') or 0)
    if not armed or (cool and now < cool) or now - since < timedelta(minutes=mins):
        m.state = st
        _save(m, 'state')
        return 'waiting'
    m.state = st
    if w.get('condition') == 'router_online':
        st['was_off'] = False
    ok, result = perform(m, now)
    why = f'It happened: {describe_when(m)[5:].split(" — ")[0]} ({words}).'
    _finish(m, ok, result, now, why)
    return 'ran'


def follow(m, now=None):
    """After a mission restarted / shut down a router: tell its people when it is back (or not)."""
    from .models_router_schedule import RouterScheduleRun
    now = now or timezone.now()
    st = dict(m.state or {})
    run = RouterScheduleRun.objects.filter(pk=st.get('follow')).select_related('router').first()
    if run is None:
        st.pop('follow', None)
        m.state = st
        _save(m, 'state')
        return None
    done = None
    if run.status == 'back':
        done = ('good', f'✅ {run.router.name} is back online',
                f'Back after {run.down_seconds // 60} min {run.down_seconds % 60} s.' if run.down_seconds is not None else 'It is back online.')
    elif run.status in ('late', 'failed'):
        done = ('danger', f'⚠️ {run.router.name}: {run.get_status_display().lower()}', run.result or 'Check its power and its internet line.')
    elif run.status == 'off':
        done = ('warning', f'⏻ {run.router.name} is switched off', 'It stays off until someone powers it on.')
    elif now - (run.ran_at or run.planned_at) > timedelta(hours=2) and run.action == 'restart':
        done = ('danger', f'⚠️ {run.router.name} is not back', 'Two hours after the restart, it has not come back.')
    if done is None:
        return None
    level, title, body = done
    told = tell(m, title, body, level=level)
    _note(m, f'{title} — {body}' + (f' Told {told}.' if told else ''), level != 'danger', now)
    st.pop('follow', None)
    m.state = st
    _save(m, 'state', 'log')
    return 'followed'


def tick(now=None):
    """Every minute (Celery beat): due missions, then follow-ups. One at a time across workers."""
    from .models_fit import FitMission
    now = now or timezone.now()
    try:
        if not cache.add(LOCK_KEY, 1, 55):
            return {'skipped': 'another tick is running'}
    except Exception:
        pass
    out = {'ran': 0, 'checked': 0, 'followed': 0}
    try:
        soon = now + timedelta(hours=24)
        qs = FitMission.objects.filter(status='waiting').select_related('business', 'created_by')
        for m in qs.filter(trigger='when') | qs.exclude(trigger='when').filter(next_at__lte=soon) | qs.filter(expires_at__lte=now):
            if not m.business.has_access:
                continue
            out['checked'] += 1
            try:
                if process(m, now) == 'ran':
                    out['ran'] += 1
            except Exception:
                logger.exception('fit mission %s', m.pk)
        for m in FitMission.objects.filter(state__has_key='follow').select_related('business', 'created_by'):
            try:
                if follow(m, now):
                    out['followed'] += 1
            except Exception:
                logger.exception('fit mission follow %s', m.pk)
    finally:
        try:
            cache.delete(LOCK_KEY)
        except Exception:
            pass
    return out


# ─────────────────────────────── create · pause · resume · cancel · run now ───────────────────────────────

def create(business, user, fields):
    from .models_fit import FitMission
    if FitMission.objects.filter(business=business, status__in=('waiting', 'paused')).count() >= MAX_PER_BUSINESS:
        raise MissionError(f'This business already has {MAX_PER_BUSINESS} missions waiting. Cancel some first.')
    f = {k: v for k, v in fields.items() if not k.startswith('_')}
    m = FitMission(business=business, created_by=user, **f)
    _note(m, f'Set by {user.get_full_name() or user.first_name or user.email}: {describe_when(m)} → {describe_task(m)}.')
    m.save()
    return m


def pause(m, user):
    if m.status != 'waiting':
        raise MissionError('Only a waiting mission can be paused.')
    m.status = 'paused'
    _note(m, f'Paused by {user.first_name or user.email}.')
    _save(m, 'status', 'log')


def resume(m, user, now=None):
    now = now or timezone.now()
    if m.status != 'paused':
        raise MissionError('Only a paused mission can be resumed.')
    nxt = first_due(m.trigger, m.when, now) if m.trigger != 'at' else m.next_at
    if m.trigger == 'at' and (nxt is None or nxt < now - MISSED_AFTER):
        raise MissionError('Its moment has passed while it was paused. Set a new mission.')
    if nxt is None:
        raise MissionError('Its moment has passed while it was paused. Set a new mission.')
    m.status, m.next_at = 'waiting', nxt
    m.state = {k: v for k, v in (m.state or {}).items() if k == 'follow'}
    _note(m, f'Resumed by {user.first_name or user.email}.')
    _save(m, 'status', 'next_at', 'state', 'log')


def cancel(m, user):
    if m.status not in ('waiting', 'paused'):
        raise MissionError('This mission is not waiting any more.')
    m.status, m.next_at = 'cancelled', None
    _note(m, f'Cancelled by {user.first_name or user.email}.')
    _save(m, 'status', 'next_at', 'log')


def run_now(m, user, now=None):
    """Do it now (a waiting or paused mission); a repeating one then goes on as planned."""
    now = now or timezone.now()
    if m.status not in ('waiting', 'paused'):
        raise MissionError('This mission is not waiting any more.')
    ok, result = perform(m, now)
    keep = m.status
    _finish(m, ok, result, now, f'Run now by {user.first_name or user.email}.')
    if keep == 'paused' and m.status == 'waiting':
        m.status = 'paused'
        _save(m, 'status')
    return ok, result


def as_json(m, now=None):
    now = now or timezone.now()

    def t(v):
        return timezone.localtime(v).isoformat() if v else None
    st = m.state or {}
    return {'id': m.pk, 'name': m.name, 'status': m.status, 'status_label': m.get_status_display(), 'trigger': m.trigger,
            'task': m.task, 'when_text': describe_when(m), 'task_text': describe_task(m), 'tell_text': describe_tell(m),
            'next_at': t(m.next_at), 'expires_at': t(m.expires_at), 'repeat': m.repeat, 'runs': m.runs,
            'last_run_at': t(m.last_run_at), 'last_result': m.last_result, 'now': st.get('now') or '',
            'waiting_since': st.get('since'), 'following': bool(st.get('follow')),
            'by': (m.created_by.get_full_name() or m.created_by.first_name or m.created_by.email) if m.created_by else '',
            'by_id': m.created_by_id, 'log': list(reversed(m.log or []))[:12], 'created_at': t(m.created_at)}
