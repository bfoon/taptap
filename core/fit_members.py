"""Fit and members — look up members and member plans, and offer every member change as a "Take action" card.

Same rules as every other Fit action (core/fit_actions.py): the model only *proposes*; the person presses
"Take action"; then the business, the person's role permission, the owner's group switch and the member's
current state are all checked again, and the same service the Members pages use does the work
(core/members.py, core/member_support.py, core/voucher_bin.py). Everything lands in the member's history
with "(approved in Fit)" in the reason, and the member reaches its router straight away (core/member_sync.py).

Groups the owner switches on in Fit settings
    members       create, details & reminders, plan, renew with payment, password, pause / schedule a pause,
                  unpause, block, unblock, sign out of the Wi-Fi, portal link, support note, bin an unused member
    member_plans  create or edit a Member Plan, switch one on or off

Permissions are the ones the Members pages need: creating and renewing = vouchers.create; support work =
vouchers.support; assigning a plan = vouchers.create or plans.manage; plans = plans.manage; bin = vouchers.manage.
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timedelta

from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from .fit_actions import FitActionError, _fit_reason, _fmt, _reason

logger = logging.getLogger('taptap.fit')

GROUPS = {
    'members': ('Members', 'Create members, edit details and reminders, change their plan, renew with payment, reset '
                           'passwords, pause or schedule a pause, block, sign out, send the portal link, bin unused members'),
    'member_plans': ('Member Plans', 'Create or edit Member Plans (price, length, devices, speed) and switch them on or off'),
}

SUPPORT = ('vouchers.support',)
CREATE = ('vouchers.create',)
ASSIGN = ('vouchers.create', 'plans.manage')
PLANS = ('plans.manage',)
BIN = ('vouchers.manage',)


# ─────────────────────────────── lookups ───────────────────────────────

def _member(business, code):
    code = str(code or '').replace(' ', '').strip()
    if not code:
        raise FitActionError('Which member? Give their username.')
    v = (business.vouchers.filter(login_type='member', code__iexact=code)
         .select_related('router', 'business').first())
    if not v:
        if business.vouchers.filter(code__iexact=code).exists():
            raise FitActionError(f'“{code}” is a voucher, not a member.')
        raise FitActionError(f'There is no member “{code}” in {business.business_name}.')
    if v.deleted_at:
        raise FitActionError(f'{v.code} is in the bin.')
    return v


def _plan(business, name, active_only=True):
    name = str(name or '').strip()
    qs = business.member_plans.all()
    if active_only:
        qs = qs.filter(active=True)
    p = qs.filter(name__iexact=name).first() if name else None
    if not p:
        names = ', '.join(qs.values_list('name', flat=True)[:12]) or 'none yet — create one first (save_member_plan)'
        raise FitActionError(f'Which Member Plan? Choose one of: {names}.' if not name
                             else f'There is no {"active " if active_only else ""}Member Plan “{name}”. Plans: {names}.')
    return p


def _router(business, name):
    """(router or None, label). Empty name = the only router; 'none' = TapTap only."""
    name = str(name or '').strip()
    if name.lower() in ('none', 'no', 'taptap only', 'local'):
        return None, 'TapTap only (no router yet)'
    routers = list(business.routers.all())
    if name:
        r = next((x for x in routers if x.name.lower() == name.lower() or str(x.pk) == name), None)
        if not r:
            raise FitActionError(f'There is no router “{name}”. Routers: {", ".join(x.name for x in routers) or "none"}.')
        return r, r.name
    if len(routers) == 1:
        return routers[0], routers[0].name
    if not routers:
        return None, 'TapTap only (no router yet)'
    raise FitActionError(f'Which router should the member use? {", ".join(x.name for x in routers)} — or “none”.')


def _link(v):
    return reverse('member_detail', args=[v.pk])


def duration_text(minutes):
    m = int(minutes or 0)
    for size, word in ((43200, 'month'), (1440, 'day'), (60, 'hour'), (1, 'minute')):
        if m >= size and m % size == 0:
            n = m // size
            return f'{n} {word}{"s" if n != 1 else ""}'
    return f'{m} minutes'


def _money(business, amount):
    return f'{business.currency or "D"}{amount:,.2f}'.replace('.00', '')


def _bool(v):
    return v is True or str(v).lower() in ('1', 'true', 'yes', 'on')


def _when_arg(value, end_of_day=False):
    """A date or date-time the model gave → aware datetime in the business time zone."""
    if value in (None, ''):
        return None
    s = str(value).strip()
    dt = parse_datetime(s)
    if dt is None:
        d = parse_date(s)
        if d is None:
            raise FitActionError(f'“{s}” is not a date. Use YYYY-MM-DD or YYYY-MM-DD HH:MM.')
        dt = datetime.combine(d, dtime(23, 59) if end_of_day else dtime(0, 0))
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def _push_note(v, plan=None):
    """Send to the router now and say what happened (used where Fit must report the router's answer)."""
    from .models import Voucher
    v = Voucher.objects.select_related('router').get(pk=v.pk)
    if not v.router_id:
        return 'Saved in TapTap only — no router assigned.'
    from .views_agents import push_one
    try:
        res = push_one(v, plan)
    except Exception as exc:                       # noqa: BLE001
        res = str(exc)
    return (f'Sent to {v.router.name}.' if res is True
            else f'{v.router.name} not updated yet ({str(res)[:120]}) — it goes with the next sync.')


# ─────────────────────────────── read tools (for fit_tools.TOOLS) ───────────────────────────────

def t_members_overview(ctx):
    from .members import member_stats
    b, now = ctx.business, timezone.now()
    qs = b.vouchers.filter(login_type='member')
    soon = list(qs.filter(status='active', frozen_at__isnull=True, expires_at__gt=now, expires_at__lte=now + timedelta(days=7))
                .order_by('expires_at').values('code', 'customer_name', 'plan_name', 'expires_at')[:15])
    unsynced = list(qs.filter(router__isnull=False).exclude(mikrotik_sync_status='Synced')
                    .values('code', 'mikrotik_sync_status', 'mikrotik_sync_error')[:10])
    return {'stats': member_stats(b),
            'states': {'active': qs.filter(status='active', frozen_at__isnull=True).count(),
                       'paused': qs.filter(frozen_at__isnull=False).count(),
                       'blocked': qs.filter(status='disabled', frozen_at__isnull=True).count(),
                       'expired': qs.filter(Q(status='expired') | Q(expires_at__lte=now)).count(),
                       'no_plan': qs.filter(member_plan_assignment__isnull=True).count()},
            'ending_within_7_days': [{'code': r['code'], 'customer': r['customer_name'] or None, 'plan': r['plan_name'],
                                      'ends': _fmt(r['expires_at'])} for r in soon],
            'not_on_router_yet': [{'code': r['code'], 'state': r['mikrotik_sync_status'],
                                   'error': (r['mikrotik_sync_error'] or '')[:120] or None} for r in unsynced],
            'link': reverse('members')}


def t_member_plans(ctx):
    rows = ctx.business.member_plans.annotate(n=Count('assignments')).order_by('-active', 'name')
    return {'plans': [{'name': p.name, 'active': p.active, 'price': _money(ctx.business, p.price),
                       'length': duration_text(p.duration_minutes) if p.duration_minutes else 'no time limit',
                       'devices': p.max_devices, 'speed': p.speed_limit or 'no limit', 'router_profile': p.router_profile_name,
                       'members': p.n} for p in rows],
            'link': reverse('members')}


def t_find_member(ctx, query):
    from .members import notification_settings_for, plan_for_member
    from .voucher_freeze import time_left_seconds
    q = str(query or '').strip()
    if not q:
        raise FitActionError('Give a member username, name, phone or email.')
    qs = ctx.business.vouchers.filter(login_type='member').select_related('router', 'agent')
    exact = qs.filter(code__iexact=q.replace(' ', '')).first()
    found = [exact] if exact else list(qs.filter(Q(customer_name__icontains=q) | Q(customer_phone__icontains=q) |
                                                 Q(member_notification_settings__email__iexact=q))[:5])
    if not found:
        return {'found': False, 'message': f'No member matching “{q}”.'}
    out = []
    for v in found:
        plan, pref = plan_for_member(v), notification_settings_for(v)
        left = time_left_seconds(v)
        out.append({
            'username': v.code, 'customer': v.customer_name or None, 'phone': v.customer_phone or None,
            'email': (pref.email if pref else '') or None,
            'reminders': ({'on': pref.reminders_enabled, '7d': pref.remind_7_days, '2d': pref.remind_2_days, '1d': pref.remind_1_day}
                          if pref else None),
            'member_plan': plan.name if plan else None, 'price': _money(ctx.business, v.price), 'status': v.status,
            'paused': bool(v.frozen_at), 'pause_reason': v.freeze_reason or None, 'ends': _fmt(v.expires_at),
            'time_left_minutes': left // 60 if left is not None else None, 'devices_allowed': v.max_devices,
            'devices_locked': v.device_bindings.count(), 'router': v.router.name if v.router_id else None,
            'router_sync': v.mikrotik_sync_status, 'router_error': (v.mikrotik_sync_error or '')[:160] or None,
            'pending_schedules': [{'action': s.action, 'when': _fmt(s.run_at), 'reason': s.reason}
                                  for s in v.member_schedules.filter(status='pending')[:3]],
            'note_UNTRUSTED': v.note or None, 'link': _link(v)})
    return {'found': True, 'members': out}


# ─────────────────────────────── create ───────────────────────────────

def _check_create(business, a):
    from . import members as mem
    from .member_support import new_password
    try:
        name = mem.check_username(a.get('username') or a.get('code'))
    except mem.MemberError as exc:
        raise FitActionError(str(exc))
    plan = _plan(business, a.get('plan'))
    router, rlabel = _router(business, a.get('router'))
    same = _bool(a.get('same_as_username'))
    pw = '' if same else (str(a.get('password') or '').strip() or new_password())
    try:
        mem.stored_password(name, pw, same)
        email = mem._email(a.get('email'))
    except mem.MemberError as exc:
        raise FitActionError(str(exc))
    reminders = _bool(a.get('reminders'))
    if reminders and not email:
        raise FitActionError('Reminders need the member’s email address.')
    paid = _bool(a.get('paid'))
    method = str(a.get('method') or 'cash')
    _check_method(method)
    # params use the same keys as the arguments: Take action re-checks them, and the password stays the same
    params = {'username': name, 'plan': plan.name, 'router': router.name if router else 'none', 'password': pw,
              'same_as_username': same, 'customer_name': str(a.get('customer_name') or '').strip()[:120],
              'phone': str(a.get('phone') or '').strip()[:60], 'email': email, 'reminders': reminders,
              'note': str(a.get('note') or '').strip()[:255], 'paid': paid and plan.price > 0, 'method': method,
              'reference': str(a.get('reference') or '')[:120]}
    bits = [f'Plan {plan.name}: {_money(business, plan.price)}, {duration_text(plan.duration_minutes) if plan.duration_minutes else "no time limit"}, '
            f'{plan.max_devices} device{"s" if plan.max_devices != 1 else ""}', f'router: {rlabel}',
            'password = username' if same else f'password: {pw}']
    if params['customer_name']:
        bits.append(f'for {params["customer_name"]}')
    if params['paid']:
        bits.append(f'first payment {_money(business, plan.price)} {method} booked in Finance')
    elif plan.price > 0:
        bits.append('payment NOT booked (say “paid” to record it)')
    if reminders:
        bits.append(f'expiry reminders to {email}')
    return params, f'Create member {name}', ' · '.join(bits), reverse('members')


def _do_create(business, p, user):
    from . import members as mem
    from . import voucher_fastlane
    plan = _plan(business, p['plan'])
    router, _ = _router(business, p['router'])
    same = bool(p.get('same_as_username'))
    try:
        with voucher_fastlane.quiet():                 # we push ourselves below, to report the router's answer
            v = mem.create_member(business, username=p['username'], password='' if same else p['password'], same=same,
                                  plan_value=str(plan.pk), router=router, customer_name=p['customer_name'],
                                  customer_phone=p['phone'], customer_email=p['email'],
                                  reminders_enabled=p['reminders'], note=p['note'] or 'Created with Fit',
                                  paid=p['paid'], method=p['method'], reference=p['reference'], user=user)
    except mem.MemberError as exc:
        raise FitActionError(str(exc))
    from .voucher_history import record
    record(v, 'note', user=user, reason=_fit_reason('Member created'))
    login = 'password = username' if same else f'password {p["password"]}'
    return f'Member {v.code} created on {plan.name} ({login}). ' + _push_note(v, plan)


def _check_method(method):
    from .views_agents import MANUAL_METHODS
    ok = dict(MANUAL_METHODS)
    if method not in ok:
        raise FitActionError(f'Payment method must be one of: {", ".join(ok)}.')


# ─────────────────────────────── details, plan, renew ───────────────────────────────

UPDATE_KEYS = ('customer_name', 'phone', 'note', 'email', 'reminders')


def _update_data(v, a):
    """(data for member_support.update_details, readable changes) from the arguments."""
    from . import members as mem
    pref = mem.notification_settings_for(v)
    data, changes = {}, []
    for key, field, label in (('customer_name', 'customer_name', 'name'), ('phone', 'customer_phone', 'phone'), ('note', 'note', 'note')):
        if a.get(key) not in (None, ''):
            data[field] = str(a[key]).strip()
            changes.append(f'{label} → {data[field][:60]}')
    if a.get('email') not in (None, ''):
        data['customer_email'] = str(a['email']).strip()
        changes.append(f'email → {data["customer_email"]}')
    if a.get('reminders') not in (None, ''):
        data['reminders_enabled'] = _bool(a['reminders'])
        changes.append('reminders ' + ('on' if data['reminders_enabled'] else 'off'))
    if not changes:
        raise FitActionError('What should change? name, phone, email, note or reminders.')
    # keep what is not mentioned
    data.setdefault('customer_email', pref.email if pref else '')
    data.setdefault('reminders_enabled', pref.reminders_enabled if pref else False)
    for k in ('remind_7_days', 'remind_2_days', 'remind_1_day'):
        data[k] = getattr(pref, k, True) if pref else True
    if data['reminders_enabled'] and not data['customer_email']:
        raise FitActionError('Reminders need the member’s email address.')
    return data, changes


def _check_update(business, a):
    v = _member(business, a.get('code'))
    data, changes = _update_data(v, a)
    params = {'code': v.code, **{k: a[k] for k in UPDATE_KEYS if a.get(k) not in (None, '')}}
    return params, f'Update {v.code}', '; '.join(changes), _link(v)


def _do_update(business, p, user):
    from .member_support import SupportError, update_details
    v = _member(business, p['code'])
    data, _ = _update_data(v, p)
    try:
        changed = update_details(v, data, user=user)
    except SupportError as exc:
        raise FitActionError(str(exc))
    return f'{v.code} updated: {", ".join(changed)}.' if changed else f'{v.code} already had those details.'


def _check_change_plan(business, a):
    from .members import plan_for_member
    v = _member(business, a.get('code'))
    plan = _plan(business, a.get('plan'))
    cur = plan_for_member(v)
    if cur and cur.pk == plan.pk:
        raise FitActionError(f'{v.code} is already on {plan.name}.')
    return {'code': v.code, 'plan': plan.name}, f'Move {v.code} to {plan.name}', \
        f'From {cur.name if cur else "no plan"}. Time already used counts in the new plan; price, devices and speed follow ' \
        f'{plan.name} ({_money(business, plan.price)}, {plan.max_devices} device{"s" if plan.max_devices != 1 else ""}). ' \
        'Not a sale — use renew_member to take a payment.', _link(v)


def _do_change_plan(business, p, user):
    from .member_support import SupportError, change_plan
    v = _member(business, p['code'])
    try:
        plan, result = change_plan(v, str(_plan(business, p['plan']).pk), user=user)
    except SupportError as exc:
        raise FitActionError(str(exc))
    return f'{v.code} is now on {plan.name}; {result}.'


def _check_renew(business, a):
    from decimal import Decimal, InvalidOperation
    from .members import plan_for_member
    from .voucher_history import ends_at
    v = _member(business, a.get('code'))
    plan = plan_for_member(v)
    if plan is None:
        raise FitActionError(f'{v.code} has no Member Plan. Give it one first (change_member_plan).')
    if not plan.duration_minutes:
        raise FitActionError(f'{plan.name} has no time limit, so there is nothing to renew.')
    raw = a.get('amount')
    try:
        amount = Decimal(str(raw)) if raw not in (None, '') else plan.price
    except InvalidOperation:
        raise FitActionError(f'“{raw}” is not an amount.')
    if plan.price > 0 and amount <= 0:
        raise FitActionError('This is a paid plan — give the amount actually collected.')
    method = str(a.get('method') or 'cash')
    _check_method(method)
    agent_name = str(a.get('agent') or '').strip()
    agent = business.agents.filter(name__iexact=agent_name).first() if agent_name else None
    if agent_name and not agent:
        raise FitActionError(f'There is no agent “{agent_name}”.')
    end = ends_at(v)
    detail = (f'Adds {duration_text(plan.duration_minutes)} on {plan.name}'
              + (f' (now ends {_fmt(end)})' if end else '') + f'. Books {_money(business, amount)} {method}'
              + (f' via {agent.name}' if agent else '') + ' in Finance with a receipt.'
              + ('' if raw not in (None, '') else ' Amount = plan price; say if a different amount was collected.'))
    return {'code': v.code, 'amount': str(amount), 'method': method, 'agent': agent.name if agent else '',
            'reference': str(a.get('reference') or '')[:120]}, f'Renew {v.code} · {_money(business, amount)}', detail, _link(v)


def _do_renew(business, p, user):
    from . import members as mem
    v = _member(business, p['code'])
    agent = business.agents.filter(name__iexact=p['agent']).first() if p.get('agent') else None
    try:
        ok, result, sale, minutes, _r = mem.renew_with_receipt(v, amount=p['amount'], method=p['method'],
                                                                reference=p.get('reference', ''), agent=agent, user=user)
    except mem.MemberError as exc:
        raise FitActionError(str(exc))
    v.refresh_from_db()
    end = f'ends {_fmt(v.expires_at)}' if v.expires_at else 'the time starts at their first login'
    return f'{v.code} renewed — {end}. {result}'


# ─────────────────────────────── password, pause, block, access ───────────────────────────────

def _check_password(business, a):
    v = _member(business, a.get('code'))
    mode = str(a.get('mode') or ('custom' if a.get('password') else 'random'))
    if mode not in ('random', 'same', 'custom'):
        raise FitActionError('mode is random, same (as the username) or custom.')
    pw = str(a.get('password') or '').strip()
    if mode == 'custom':
        from . import members as mem
        try:
            mem.check_password(pw)
        except mem.MemberError as exc:
            raise FitActionError(str(exc))
    email = _bool(a.get('email_member'))
    if email:
        from .member_support import contact_email
        if not contact_email(v):
            raise FitActionError(f'{v.code} has no email address to send the password to.')
    what = {'random': 'a new random password', 'same': 'the username as password', 'custom': 'the password you gave'}[mode]
    return {'code': v.code, 'mode': mode, 'password': pw, 'email_member': email, 'reason': _reason(a, 'Password reset')}, \
        f'New password for {v.code}', f'Sets {what}; the member is signed out and logs in again.' + \
        (' The new password is emailed to them.' if email else ' You see it once, after it is done.'), _link(v)


def _do_password(business, p, user):
    from .member_support import SupportError, reset_password
    v = _member(business, p['code'])
    try:
        shown, result, emailed = reset_password(v, mode=p['mode'], password=p.get('password', ''),
                                                reason=_fit_reason(p['reason']), user=user, email=bool(p.get('email_member')))
    except SupportError as exc:
        raise FitActionError(str(exc))
    pw = 'the username' if p['mode'] == 'same' else shown
    return f'{v.code}: new password is {pw}. {result}.' + (f' Emailed to {emailed}.' if emailed else '')


def _check_pause(business, a):
    from .voucher_freeze import why_not_freezable
    v = _member(business, a.get('code'))
    reason = str(a.get('reason') or '').strip()
    if not reason:
        raise FitActionError('Give a reason for the pause — it is kept in the history and shown to the member.')
    when, days_left = _when_arg(a.get('when')), a.get('days_left')
    resume = _when_arg(a.get('resume_at'))
    if when or days_left not in (None, ''):
        if v.frozen_at:
            raise FitActionError(f'{v.code} is already paused.')
        if when and when <= timezone.now():
            when = None                                     # "today" / a past time → pause now
        else:
            what = f'on {_fmt(when)}' if when else f'when {int(days_left)} day(s) are left'
            return {'code': v.code, 'reason': reason[:255], 'when': when.isoformat() if when else None,
                    'days_left': int(days_left) if days_left not in (None, '') else None,
                    'resume_at': resume.isoformat() if resume else None}, \
                f'Schedule a pause for {v.code}', f'Internet stops {what}; the time left stands still' + \
                (f' until {_fmt(resume)}' if resume else ' until someone unpauses') + f'. Reason: {reason}', _link(v)
    why = why_not_freezable(v)
    if why:
        raise FitActionError(f'{v.code} cannot be paused: {why}.')
    return {'code': v.code, 'reason': reason[:255], 'when': None, 'days_left': None, 'resume_at': resume.isoformat() if resume else None}, \
        f'Pause {v.code} now', 'Internet stops now and the time left stands still' + \
        (f' until {_fmt(resume)}' if resume else ' until someone unpauses') + f'. Reason: {reason}', _link(v)


def _do_pause(business, p, user):
    from .member_support import SupportError, pause_now, schedule_pause, schedule_resume
    v = _member(business, p['code'])
    resume = parse_datetime(p['resume_at']) if p.get('resume_at') else None
    try:
        if p.get('when') or p.get('days_left') is not None:
            schedule_pause(v, when=parse_datetime(p['when']) if p.get('when') else None, days_left=p.get('days_left'),
                           resume_at=resume, reason=_fit_reason(p['reason']), user=user)
            return f'Pause for {v.code} scheduled.' + (f' Unpauses {_fmt(resume)}.' if resume else '')
        pause_now(v, _fit_reason(p['reason']), user=user)
        if resume:
            schedule_resume(v, when=resume, reason=_fit_reason('Planned unpause'), user=user)
    except SupportError as exc:
        raise FitActionError(str(exc))
    return f'{v.code} is paused — internet stopped, time stands still.' + (f' Unpauses {_fmt(resume)}.' if resume else '')


def _check_unpause(business, a):
    v = _member(business, a.get('code'))
    pending = v.member_schedules.filter(status='pending', action='pause').first()
    if not v.frozen_at and not pending:
        raise FitActionError(f'{v.code} is not paused and has no pause waiting.')
    if not v.frozen_at:
        return {'code': v.code, 'reason': _reason(a, 'Pause cancelled')}, \
            f'Cancel the planned pause of {v.code}', f'The pause set for {_fmt(pending.run_at)} will not happen.', _link(v)
    return {'code': v.code, 'reason': _reason(a, 'Unpaused by staff')}, f'Unpause {v.code}', \
        'Internet works again; the time it had left continues from now.', _link(v)


def _do_unpause(business, p, user):
    from .member_support import SupportError, cancel_schedule, unpause_now
    v = _member(business, p['code'])
    try:
        if not v.frozen_at:
            pending = v.member_schedules.filter(status='pending', action='pause').first()
            if pending:
                cancel_schedule(v, pending.pk, user=user)
                return f'The planned pause of {v.code} is cancelled.'
        unpause_now(v, user=user, reason=_fit_reason(p['reason']))
    except SupportError as exc:
        raise FitActionError(str(exc))
    return f'{v.code} is unpaused — the internet works again.'


def _check_block(business, a):
    v = _member(business, a.get('code'))
    reason = str(a.get('reason') or '').strip()
    if not reason:
        raise FitActionError('Give a reason for blocking.')
    if v.frozen_at:
        raise FitActionError(f'{v.code} is paused. Unpause it first if you want to block it.')
    if v.status == 'disabled':
        raise FitActionError(f'{v.code} is already blocked.')
    return {'code': v.code, 'reason': reason[:200]}, f'Block {v.code}', \
        f'The account stops working and its devices are disconnected. Reason: {reason}', _link(v)


def _do_block(business, p, user):
    from .member_support import SupportError, block
    v = _member(business, p['code'])
    try:
        ok, result = block(v, user=user, reason=_fit_reason(p['reason']))
    except SupportError as exc:
        raise FitActionError(str(exc))
    return f'{v.code} blocked. {result}'


def _check_unblock(business, a):
    from .voucher_history import time_is_up
    v = _member(business, a.get('code'))
    if v.status != 'disabled' or v.frozen_at:
        raise FitActionError(f'{v.code} is not blocked.' + (' It is paused — use unpause_member.' if v.frozen_at else ''))
    if time_is_up(v):
        raise FitActionError(f'{v.code}’s time has run out — renew instead (renew_member).')
    return {'code': v.code, 'reason': _reason(a, 'Unblocked by staff')}, f'Unblock {v.code}', \
        'The account works again with the time it has left.', _link(v)


def _do_unblock(business, p, user):
    from .member_support import SupportError, unblock
    v = _member(business, p['code'])
    try:
        ok, result = unblock(v, user=user, reason=_fit_reason(p['reason']))
    except SupportError as exc:
        raise FitActionError(str(exc))
    return f'{v.code} unblocked. {result}'


def _check_sign_out(business, a):
    v = _member(business, a.get('code'))
    where = str(a.get('where') or 'wifi')
    if where not in ('wifi', 'portal', 'both'):
        raise FitActionError('where is wifi, portal or both.')
    text = {'wifi': 'Their devices are disconnected from the Wi-Fi now; they can log in again.',
            'portal': 'Every member-portal session ends; their next visit needs a new access link.',
            'both': 'Devices disconnected from the Wi-Fi and every member-portal session ended.'}[where]
    return {'code': v.code, 'where': where, 'reason': _reason(a, 'Signed out by staff')}, f'Sign out {v.code}', text, _link(v)


def _do_sign_out(business, p, user):
    from .member_support import portal_sign_out, wifi_sign_out
    v = _member(business, p['code'])
    out = []
    if p['where'] in ('wifi', 'both'):
        ok, result = wifi_sign_out(v, user=user, reason=_fit_reason(p['reason']))
        out.append(f'Wi-Fi: {result}')
    if p['where'] in ('portal', 'both'):
        portal_sign_out(v, user=user, reason=_fit_reason(p['reason']))
        out.append('member portal: signed out everywhere')
    return f'{v.code} signed out — ' + '; '.join(out) + '.'


def _check_portal_link(business, a):
    from .member_support import contact_email
    v = _member(business, a.get('code'))
    to = contact_email(v)
    if not to:
        raise FitActionError(f'{v.code} has no email address. Add one first (update_member email=…).')
    return {'code': v.code}, f'Email {v.code} a member-portal link', f'A one-time sign-in link goes to {to}.', _link(v)


def _do_portal_link(business, p, user):
    from .member_support import SupportError, send_portal_link
    v = _member(business, p['code'])
    try:
        to = send_portal_link(v, user=user)
    except SupportError as exc:
        raise FitActionError(str(exc))
    return f'Member-portal link sent to {to}.'


def _check_note(business, a):
    v = _member(business, a.get('code'))
    text = str(a.get('note') or a.get('body') or '').strip()
    if not text:
        raise FitActionError('Write the note first.')
    return {'code': v.code, 'note': text[:1000]}, f'Add a support note to {v.code}', f'“{text[:180]}” — kept in the member’s history.', _link(v)


def _do_note(business, p, user):
    from .member_support import add_note
    v = _member(business, p['code'])
    add_note(v, p['note'] + ' (via Fit)', user=user)
    return f'Note added to {v.code}.'


def _check_bin(business, a):
    from .voucher_bin import why_not_deletable
    v = _member(business, a.get('code'))
    why = why_not_deletable(v)
    if why:
        raise FitActionError(f'{v.code} cannot be binned: {why}. A member that has been used stays — block it instead.')
    return {'code': v.code, 'reason': _reason(a, 'Removed by staff')}, f'Move member {v.code} to the bin', \
        'It leaves TapTap’s lists and its router; a first payment is taken out of Finance. This cannot be undone.', reverse('members')


def _do_bin(business, p, user):
    from .voucher_bin import delete_vouchers
    v = _member(business, p['code'])
    out = delete_vouchers(business, [v], user=user, reason=_fit_reason(p['reason'])) or {}
    if not out.get('deleted'):
        skipped = out.get('skipped') or []
        raise FitActionError(f'{v.code} was not binned' + (f': {skipped[0][1]}' if skipped and len(skipped[0]) > 1 else '.'))
    r = out.get('router') or []
    items = r.values() if isinstance(r, dict) else r
    routers = '; '.join(str(m[-1]) if isinstance(m, (list, tuple)) else str(m) for m in items)
    sales = f' {out["sales_removed"]} sale(s) taken out of Finance.' if out.get('sales_removed') else ''
    return f'{v.code} is in the bin.{sales}' + (f' Router: {routers}' if routers else '')


# ─────────────────────────────── member plans ───────────────────────────────

UNITS = ('minutes', 'hours', 'days', 'months')


PLAN_KEYS = ('plan', 'name', 'new_name', 'price', 'duration_value', 'duration_unit', 'max_devices', 'speed_limit')
PER_UNIT = {'minutes': 1, 'hours': 60, 'days': 1440, 'months': 43200}


def _plan_data(business, a):
    """(existing plan or None, data for members.save_member_plan, minutes) — unchanged fields keep their values."""
    from . import members as mem
    from .durations import to_minutes
    name = str(a.get('plan') or a.get('name') or '').strip()
    if not name:
        raise FitActionError('Give the Member Plan’s name.')
    plan = business.member_plans.filter(name__iexact=name).first()
    given = lambda k: a.get(k) not in (None, '')            # noqa: E731
    data = {'name': str(a.get('new_name') or '').strip() or (plan.name if plan else name),
            'price': a.get('price') if given('price') else (plan.price if plan else None),
            'max_devices': a.get('max_devices') if given('max_devices') else (plan.max_devices if plan else 1),
            'speed_limit': a.get('speed_limit') if a.get('speed_limit') is not None else (plan.speed_limit if plan else ''),
            'duration_unit': str(a.get('duration_unit') or (plan.duration_unit if plan and plan.duration_unit else 'months')),
            'duration_value': a.get('duration_value') if given('duration_value') else None}
    if data['duration_value'] is None and plan:          # keep the current length, in the plan's own unit
        per = PER_UNIT.get(data['duration_unit'], 43200)
        data['duration_value'] = max(1, round((plan.duration_minutes or per) / per))
    if data['price'] in (None, ''):
        raise FitActionError('What is the price of the new Member Plan?')
    if not data['duration_value']:
        raise FitActionError('How long does the new Member Plan last? (duration_value + duration_unit)')
    if data['duration_unit'] not in UNITS:
        raise FitActionError(f'duration_unit is one of {", ".join(UNITS)}.')
    try:
        minutes = to_minutes(data['duration_value'], data['duration_unit'])
        mem._money(data['price'])
        mem._devices(data['max_devices'])
        mem._speed(data['speed_limit'])
    except (mem.MemberError, ValueError) as exc:
        raise FitActionError(str(exc))
    return plan, {k: str(v) for k, v in data.items()}, minutes


def _check_save_plan(business, a):
    plan, data, minutes = _plan_data(business, a)
    n = plan.assignments.count() if plan else 0
    detail = (f'{data["name"]}: {business.currency or "D"}{data["price"]}, {duration_text(minutes)}, {data["max_devices"]} '
              f'device(s), speed {data["speed_limit"] or "no limit"}.')
    if plan and n:
        detail += (f' The {n} member(s) on it get the new name, price, devices and speed now (sent to their routers); '
                   'a new length applies to time already used too.')
    params = {k: a[k] for k in PLAN_KEYS if a.get(k) not in (None, '')}
    params['plan'] = plan.name if plan else data['name']
    return params, (f'Edit Member Plan {plan.name}' if plan else f'Create Member Plan {data["name"]}'), detail, reverse('members')


def _do_save_plan(business, p, user):
    from . import members as mem
    plan, data, _ = _plan_data(business, p)
    try:
        plan, refreshed = mem.save_member_plan(business, data, plan=plan, user=user)
    except mem.MemberError as exc:
        raise FitActionError(str(exc))
    return f'Member Plan {plan.name} saved.' + (f' {refreshed} member(s) updated and sent to their routers.' if refreshed else '')


def _check_toggle_plan(business, a):
    plan = _plan(business, a.get('plan'), active_only=False)
    on = _bool(a.get('active'))
    if plan.active == on:
        raise FitActionError(f'{plan.name} is already {"on" if on else "off"}.')
    return {'plan': plan.name, 'active': on}, f'Switch Member Plan {plan.name} {"on" if on else "off"}', \
        ('New members can be put on it again.' if on else 'No new members or plan changes onto it; members already on it keep it.'), \
        reverse('members')


def _do_toggle_plan(business, p, user):
    from . import members as mem
    plan = _plan(business, p['plan'], active_only=False)
    mem.set_plan_active(plan, p['active'])
    return f'Member Plan {plan.name} is now {"on" if p["active"] else "off"}.'


# ─────────────────────────────── registry & install ───────────────────────────────

KINDS = {
    'create_member': ('members', CREATE, _check_create, _do_create, 'ok'),
    'update_member': ('members', SUPPORT, _check_update, _do_update, 'ok'),
    'change_member_plan': ('members', ASSIGN, _check_change_plan, _do_change_plan, 'warn'),
    'renew_member': ('members', CREATE, _check_renew, _do_renew, 'ok'),
    'reset_member_password': ('members', SUPPORT, _check_password, _do_password, 'warn'),
    'pause_member': ('members', SUPPORT, _check_pause, _do_pause, 'warn'),
    'unpause_member': ('members', SUPPORT, _check_unpause, _do_unpause, 'ok'),
    'block_member': ('members', SUPPORT, _check_block, _do_block, 'danger'),
    'unblock_member': ('members', SUPPORT, _check_unblock, _do_unblock, 'ok'),
    'sign_out_member': ('members', SUPPORT, _check_sign_out, _do_sign_out, 'warn'),
    'send_member_portal_link': ('members', SUPPORT, _check_portal_link, _do_portal_link, 'ok'),
    'member_note': ('members', SUPPORT, _check_note, _do_note, 'ok'),
    'bin_member': ('members', BIN, _check_bin, _do_bin, 'danger'),
    'save_member_plan': ('member_plans', PLANS, _check_save_plan, _do_save_plan, 'warn'),
    'toggle_member_plan': ('member_plans', PLANS, _check_toggle_plan, _do_toggle_plan, 'warn'),
}

S = {'type': 'string'}
PROPS = {
    'username': {**S, 'description': 'create_member: the new member’s username (other member kinds use code)'},
    'password': {**S, 'description': 'create_member / reset_member_password mode=custom: the password (empty = random)'},
    'same_as_username': {'type': 'boolean', 'description': 'create_member: password = username'},
    'customer_name': {**S, 'description': 'create_member / update_member: the member’s name'},
    'phone': {**S, 'description': 'create_member / update_member: phone'},
    'email': {**S, 'description': 'create_member / update_member: email (needed for reminders and the portal link)'},
    'reminders': {'type': 'boolean', 'description': 'create_member / update_member: expiry reminder emails on/off'},
    'paid': {'type': 'boolean', 'description': 'create_member: the first payment was collected (books it in Finance)'},
    'method': {**S, 'description': 'create_member / renew_member: payment method, e.g. cash, wave, bank, card'},
    'amount': {'type': 'number', 'description': 'renew_member: money actually collected (default: the plan price)'},
    'agent': {**S, 'description': 'renew_member: agent who collected it (name)'},
    'reference': {**S, 'description': 'create_member / renew_member: payment reference'},
    'mode': {'type': 'string', 'enum': ['random', 'same', 'custom'], 'description': 'reset_member_password'},
    'email_member': {'type': 'boolean', 'description': 'reset_member_password: email the new password to the member'},
    'when': {**S, 'description': 'pause_member: date/time to pause (YYYY-MM-DD or YYYY-MM-DD HH:MM; empty = now)'},
    'days_left': {'type': 'integer', 'description': 'pause_member: pause when this many days are left'},
    'resume_at': {**S, 'description': 'pause_member: date/time to unpause by itself (empty = until someone unpauses)'},
    'where': {'type': 'string', 'enum': ['wifi', 'portal', 'both'], 'description': 'sign_out_member'},
    'new_name': {**S, 'description': 'save_member_plan: rename the plan'},
    'price': {'type': 'number', 'description': 'save_member_plan'},
    'duration_value': {'type': 'integer', 'description': 'save_member_plan: length, with duration_unit'},
    'duration_unit': {'type': 'string', 'enum': list(UNITS), 'description': 'save_member_plan'},
    'max_devices': {'type': 'integer', 'description': 'save_member_plan: devices per member'},
    'speed_limit': {**S, 'description': 'save_member_plan: MikroTik rate limit, e.g. 5M/5M (empty = no limit)'},
    'active': {'type': 'boolean', 'description': 'toggle_member_plan: on (true) or off (false)'},
}

TOOLS = {
    'members_overview': ('t_members_overview', 'Members at a glance: totals, active / paused / blocked / expired / without a plan, '
                         'who ends within 7 days, and members not on their router yet.', {}),
    'member_plans': ('t_member_plans', 'The Member Plans of this business: price, length, devices, speed, on/off and how many '
                     'members are on each.', {}),
    'find_member': ('t_find_member', 'Look up a member by username, name, phone or email: plan, state, end date, time left, devices, '
                    'email & reminders, router and whether it is synced, waiting pause/unpause schedules.', {'query': S}),
}

RULE = ('15. Members (monthly / long-term customers who log in with a username and password): look them up with find_member, '
        'members_overview and member_plans (never guess plan names or usernames). Every member change is a propose_action '
        'card: create_member, update_member (name, phone, email, note, reminders), change_member_plan, renew_member (takes a '
        'payment — ask how much was collected and how if not said), reset_member_password, pause_member (now, on a date, or '
        'when N days are left; optional resume_at), unpause_member, block_member / unblock_member, sign_out_member, '
        'send_member_portal_link, member_note, bin_member (only never-used members), save_member_plan and toggle_member_plan. '
        'Members use code = their username. Free goodwill time without a payment is add_time. A member change reaches the '
        'router by itself — no separate sync is needed. If an action needs a detail you do not have (which plan, the amount, '
        'which router when there are several), ask one short question first.')

_INSTALLED = False


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    from . import fit_actions, fit_ai, fit_tools

    # groups: members right after vouchers/time/devices on the settings screen
    items = list(fit_actions.GROUPS.items())
    at = next((i + 1 for i, (k, _) in enumerate(items) if k == 'devices'), len(items))
    for k in GROUPS:
        items = [x for x in items if x[0] != k]
    items[at:at] = list(GROUPS.items())
    fit_actions.GROUPS.clear()
    fit_actions.GROUPS.update(items)
    fit_actions.KINDS.update(KINDS)

    me = globals()
    for name, (fn, desc, props) in TOOLS.items():
        fit_tools.TOOLS[name] = (fit_tools.VOUCHER_PERMS, me[fn], desc,
                                 fit_tools._obj(props, ['query'] if 'query' in props else ()))

    original_spec = fit_tools.propose_spec

    def propose_spec(ctx):
        spec = original_spec(ctx)
        spec['properties'].update(PROPS)
        spec['properties']['code']['description'] = 'Voucher code, or a member’s username (most kinds)'
        spec['properties']['plan']['description'] = ('sync scope=plan: the plan name · create_member / change_member_plan / '
                                                     'save_member_plan / toggle_member_plan: the Member Plan name')
        spec['properties']['router']['description'] += ' · create_member: the member’s router (\"none\" = TapTap only)'
        spec['properties']['note']['description'] += ' · member_note / update_member: the note'
        return spec
    fit_tools.propose_spec = propose_spec

    original_specs = fit_tools.specs

    def specs(ctx):
        out = original_specs(ctx)
        for t in out:
            f = t.get('function', {})
            if f.get('name') == 'propose_action':
                f['description'] += (' Members: create_member, update_member, change_member_plan, renew_member, '
                                     'reset_member_password, pause_member, unpause_member, block_member, unblock_member, '
                                     'sign_out_member, send_member_portal_link, member_note, bin_member, save_member_plan, '
                                     'toggle_member_plan.')
        return out
    fit_tools.specs = specs

    original_prompt = fit_ai.system_prompt

    def system_prompt(ctx, page=None):
        text = original_prompt(ctx, page)
        marker = '\n\nThe person is looking at'
        return text.replace(marker, '\n' + RULE + marker, 1) if marker in text else text + '\n' + RULE
    fit_ai.system_prompt = system_prompt

    _INSTALLED = True
    logger.info('Fit members enabled: %s member actions, %s member tools.', len(KINDS), len(TOOLS))
