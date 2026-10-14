"""Changes Fit can make — only after a person approves them.

How an action flows
1. The model calls ``propose_action``. ``propose()`` checks the business, the person's permission and
   the details (the voucher exists *in this business*, the MAC is valid…) and stores a FitAction row.
   Nothing is changed yet; the browser shows a card with "Take action".
2. The person presses it. ``run()`` checks everything again — same business, same person, still
   within ACTION_TTL, the owner still allows that group, the person still has the permission, the
   item is still in a state where the change makes sense — and only then calls the same service
   the normal TapTap page uses (voucher_freeze, voucher_history, device_block, chat…). Every
   change is therefore in the voucher history / activity log exactly like a manual one, with
   "Fit" in the reason.

Two locks guard every change: the owner's switch for the group (FitSettings.allowed) and the
person's own role permission. A model reply can never skip either: the model only ever writes a
proposal.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger('taptap.fit')

ACTION_TTL = timedelta(minutes=30)
MAC_RE = re.compile(r'^([0-9A-F]{2}:){5}[0-9A-F]{2}$')

# Groups the owner switches on in Fit settings. Order = order on the settings screen.
GROUPS = {
    'vouchers': ('Vouchers', 'Freeze, unfreeze, warn, disable or enable vouchers'),
    'time': ('Voucher time', 'Add time to a voucher or end it earlier'),
    'devices': ('Devices', 'Block or unblock a device, disconnect a session, reset voucher devices'),
    'reports': ('Customer reports', 'Resolve reports customers send from the sign-in page'),
    'tracking': ('Tracking', 'Follow a voucher so you are told when something happens to it'),
    'chat': ('Chat', 'Send chat messages you approved to your team or TapTap support'),
    'tests': ('Network tests', 'Run ping, DNS, website, traceroute and speed tests from your routers'),
    'monitors': ('Monitors', 'Set up standing watches (slots full, sharing, heavy users, drops, routers) that alert '
                             'you or the member, and may free an idle slot or disconnect extra devices'),
}


MONITOR_PERMS = ('vouchers.support', 'settings.manage', 'network.manage')


class FitActionError(ValueError):
    pass


# ─────────────────────────────── helpers ───────────────────────────────

def norm_mac(mac):
    h = ''.join(c for c in str(mac or '').upper() if c in '0123456789ABCDEF')
    return ':'.join(h[i:i + 2] for i in range(0, 12, 2)) if len(h) == 12 else ''


def _voucher(business, code):
    code = str(code or '').replace(' ', '').strip()
    if not code:
        raise FitActionError('Which voucher? Give its code.')
    v = business.vouchers.filter(code__iexact=code).select_related('router').first()
    if not v:
        raise FitActionError(f'There is no voucher “{code}” in {business.business_name}.')
    return v


def _reason(args, default):
    r = str(args.get('reason') or '').strip()
    return (r or default)[:200]


def _minutes(args):
    try:
        n = int(args.get('minutes') or 0)
    except (TypeError, ValueError):
        raise FitActionError('Time must be a whole number of minutes.')
    if not 1 <= n <= 525600:
        raise FitActionError('Time must be between 1 minute and 1 year.')
    return n


def _mtext(minutes):
    from .durations import text
    return text(int(minutes))


def _fmt(dt):
    return timezone.localtime(dt).strftime('%d %b %Y %H:%M') if dt else '—'


def _vlink(v):
    return reverse('member_detail' if v.is_member else 'voucher_detail', args=[v.pk])


def _fit_reason(reason):
    return f'{reason} (approved in Fit)'[:255]


# ─────────────────────────────── vouchers ───────────────────────────────

def _check_freeze(business, a):
    from .voucher_freeze import why_not_freezable
    v = _voucher(business, a.get('code'))
    why = why_not_freezable(v)
    if why:
        raise FitActionError(f'{v.code} cannot be frozen: {why}.')
    reason = _reason(a, 'Paused by staff')
    return {'code': v.code, 'reason': reason}, f'Freeze voucher {v.code}', \
        f'Internet stops now and the clock stands still. Reason: {reason}', _vlink(v)


def _do_freeze(business, p, user):
    from .voucher_freeze import freeze, summary_messages
    v = _voucher(business, p['code'])
    out = freeze([v], user=user, reason=_fit_reason(p['reason']))
    return ' '.join(m for _, m in summary_messages(out, 'frozen'))


def _check_unfreeze(business, a):
    v = _voucher(business, a.get('code'))
    if not v.frozen_at:
        raise FitActionError(f'{v.code} is not frozen.')
    return {'code': v.code, 'reason': _reason(a, 'Unpaused by staff')}, f'Unfreeze voucher {v.code}', \
        'The time it had left continues from now.', _vlink(v)


def _do_unfreeze(business, p, user):
    from .voucher_freeze import unfreeze, summary_messages
    v = _voucher(business, p['code'])
    out = unfreeze([v], user=user, reason=_fit_reason(p['reason']))
    return ' '.join(m for _, m in summary_messages(out, 'unfrozen'))


def _check_warn(business, a):
    from .voucher_freeze import why_not_freezable, MANUAL_WARNING
    v = _voucher(business, a.get('code'))
    why = why_not_freezable(v)
    if why:
        raise FitActionError(f'{v.code} cannot be warned: {why}.')
    msg = str(a.get('message') or '').strip()[:600] or MANUAL_WARNING
    reason = _reason(a, 'Warning from staff')
    return {'code': v.code, 'reason': reason, 'message': msg}, f'Warn the customer on {v.code}', \
        f'Internet pauses until they read this and press “I agree”: “{msg[:160]}”', _vlink(v)


def _do_warn(business, p, user):
    from .voucher_freeze import freeze, summary_messages
    v = _voucher(business, p['code'])
    out = freeze([v], user=user, reason=_fit_reason(p['reason']), kind='warning', message=p['message'])
    return ' '.join(m for _, m in summary_messages(out, 'warned'))


def _check_disable(business, a):
    v = _voucher(business, a.get('code'))
    if v.frozen_at:
        raise FitActionError(f'{v.code} is frozen. Unfreeze it first if you want to disable it for good.')
    if v.status == 'disabled':
        raise FitActionError(f'{v.code} is already disabled.')
    reason = _reason(a, 'Disabled by staff')
    return {'code': v.code, 'reason': reason}, f'Disable voucher {v.code}', \
        f'It stops working and its session is disconnected. Reason: {reason}', _vlink(v)


def _do_disable(business, p, user):
    from .voucher_history import disable
    ok, result = disable(_voucher(business, p['code']), user=user, reason=_fit_reason(p['reason']))
    return f'{p["code"]} disabled. {result}'


def _check_enable(business, a):
    from .voucher_history import time_is_up
    v = _voucher(business, a.get('code'))
    if v.frozen_at:
        raise FitActionError(f'{v.code} is frozen — use unfreeze instead.')
    if v.status == 'active' and not time_is_up(v):
        raise FitActionError(f'{v.code} is already active.')
    if time_is_up(v):
        raise FitActionError(f'The time on {v.code} has run out. Add time to it instead (add_time).')
    return {'code': v.code, 'reason': _reason(a, 'Enabled by staff')}, f'Enable voucher {v.code}', \
        'It works again with the time it has left.', _vlink(v)


def _do_enable(business, p, user):
    from .voucher_history import enable
    ok, result = enable(_voucher(business, p['code']), user=user, reason=_fit_reason(p['reason']))
    return f'{p["code"]} enabled. {result}'


def _check_add_time(business, a):
    v = _voucher(business, a.get('code'))
    n = _minutes(a)
    if v.frozen_at:
        raise FitActionError(f'{v.code} is frozen. Unfreeze it first.')
    if not v.duration_minutes and not v.expires_at:
        raise FitActionError(f'{v.code} has no time limit, so there is no time to add.')
    return {'code': v.code, 'minutes': n, 'reason': _reason(a, 'Time added by staff')}, \
        f'Add {_mtext(n)} to {v.code}', 'Not a sale — finance is not changed. The router limit is raised too.', _vlink(v)


def _do_add_time(business, p, user):
    from .voucher_history import enable
    ok, result = enable(_voucher(business, p['code']), user=user, reason=_fit_reason(p['reason']), add_minutes=p['minutes'])
    return f'Added {_mtext(p["minutes"])} to {p["code"]}. {result}'


def _check_reduce_time(business, a):
    from .voucher_history import ends_at, time_is_up
    v = _voucher(business, a.get('code'))
    n = _minutes(a)
    if v.frozen_at:
        raise FitActionError(f'{v.code} is frozen — its clock is standing still.')
    end = ends_at(v)
    if v.status != 'active' or time_is_up(v) or not end or not (v.rolled_back_at or v.used_at):
        raise FitActionError(f'{v.code} is not running, so its time cannot be shortened.')
    new_end = end - timedelta(minutes=n)
    ends_now = new_end <= timezone.now()
    return {'code': v.code, 'minutes': n, 'reason': _reason(a, 'Time reduced by staff')}, \
        f'Reduce {v.code} by {_mtext(n)}', \
        ('That uses up all its time: it ends now.' if ends_now else f'It will end {_fmt(new_end)} instead of {_fmt(end)}.'), _vlink(v)


def _do_reduce_time(business, p, user):
    from .voucher_history import ends_at
    from .voucher_schedule import end_at
    v = _voucher(business, p['code'])
    end = ends_at(v)
    if not end:
        raise FitActionError(f'{v.code} has no end time to move.')
    return end_at(v, end - timedelta(minutes=p['minutes']), user=user, reason=_fit_reason(p['reason']))


def _check_reset(business, a):
    v = _voucher(business, a.get('code'))
    n = v.device_bindings.count()
    return {'code': v.code, 'reason': _reason(a, 'Devices reset by staff')}, f'Reset devices of {v.code}', \
        f'Frees {n} locked device slot{"s" if n != 1 else ""}; the next device to log in gets the slot.', _vlink(v)


def _do_reset(business, p, user):
    from .voucher_history import reset_devices
    ok, result = reset_devices(_voucher(business, p['code']), user=user, reason=_fit_reason(p['reason']))
    return f'Devices of {p["code"]} reset. {result}'


def _check_track(business, a):
    v = _voucher(business, a.get('code'))
    return {'code': v.code, 'note': str(a.get('note') or a.get('reason') or '').strip()[:200]}, f'Track voucher {v.code}', \
        'You will be told in the app whenever something happens to it.', reverse('tracking')


def _do_track(business, p, user):
    from .models_watch import Watch
    v = _voucher(business, p['code'])
    Watch.objects.update_or_create(user=user, kind='voucher', object_id=v.pk,
                                   defaults={'business': business, 'label': v.code[:160], 'mode': 'all', 'events': [],
                                             'channel': 'app', 'note': p.get('note') or 'Asked Fit to track it', 'active': True})
    return f'Now tracking {v.code}. Alerts appear under Tracking.'


# ─────────────────────────────── devices ───────────────────────────────

def _check_block(business, a):
    from .device_block import blocked_macs
    mac = norm_mac(a.get('mac'))
    if not MAC_RE.match(mac or ''):
        raise FitActionError('Give the device’s MAC address (like AA:BB:CC:11:22:33).')
    if mac in blocked_macs(business):
        raise FitActionError(f'{mac} is already blocked.')
    code = str(a.get('code') or '').strip()
    v = _voucher(business, code) if code else None
    if not v and not business.routers.exists():
        raise FitActionError('This business has no routers to block the device on.')
    label = str(a.get('label') or '').strip()[:60]
    where = f'the routers {v.code} uses' if v else 'every router of the business'
    return {'mac': mac, 'code': v.code if v else '', 'label': label, 'reason': _reason(a, 'Blocked by staff')}, \
        f'Block device {label or mac}', f'The router refuses {mac} on {where} and drops its session now. You can unblock it later.', \
        reverse('ip_bindings')


def _do_block(business, p, user):
    from .device_block import block
    v = _voucher(business, p['code']) if p.get('code') else None
    return block(v, [p['mac']], label=p.get('label', ''), user=user, business=business, why='blocked via Fit')


def _check_unblock(business, a):
    from .device_block import blocked_macs
    mac = norm_mac(a.get('mac'))
    if mac not in blocked_macs(business):
        raise FitActionError(f'{mac or "That device"} is not blocked by TapTap.')
    return {'mac': mac, 'label': str(a.get('label') or '')[:60]}, f'Unblock device {a.get("label") or mac}', \
        'Its previous IP binding, if it had one, is put back.', reverse('ip_bindings')


def _do_unblock(business, p, user):
    from .device_block import unblock
    unblock(None, [p['mac']], label=p.get('label', ''), user=user, business=business)
    return f'{p.get("label") or p["mac"]} unblocked.'


def _check_disconnect(business, a):
    v = _voucher(business, a.get('code'))
    if not v.router_id:
        raise FitActionError(f'{v.code} has no router, so there is no session to drop.')
    return {'code': v.code}, f'Disconnect {v.code} now', 'Drops its live session; the voucher itself keeps working.', _vlink(v)


def _do_disconnect(business, p, user):
    from .voucher_history import _router_apply, record
    v = _voucher(business, p['code'])
    via, result, ok = _router_apply(v, 'reset', user=user)
    record(v, 'note', user=user, reason='Session disconnected (approved in Fit)', via=via, router_result=result)
    if not ok:
        raise FitActionError(result)
    return f'{v.code}: {result}'


# ─────────────────────────────── customer reports ───────────────────────────────

def _report(business, rid):
    from .models_voucher_reports import VoucherReport
    try:
        return VoucherReport.objects.get(business=business, pk=int(rid))
    except (TypeError, ValueError, VoucherReport.DoesNotExist):
        raise FitActionError('That customer report is not in this business.')


def _check_resolve(business, a):
    r = _report(business, a.get('report_id'))
    if r.status == 'resolved':
        raise FitActionError(f'Report #{r.pk} is already resolved.')
    note = str(a.get('note') or '').strip()[:400] or 'Checked and resolved'
    return {'report_id': r.pk, 'note': note}, f'Resolve report #{r.pk} ({r.code})', f'Note kept on the report: “{note}”', \
        reverse('voucher_reports')


def _do_resolve(business, p, user):
    r = _report(business, p['report_id'])
    if r.status == 'resolved':
        return f'Report #{r.pk} was already resolved.'
    r.status, r.resolved_at, r.resolved_by = 'resolved', timezone.now(), user
    r.staff_note = f'{p["note"]} (resolved with Fit)'[:500]
    r.save(update_fields=['status', 'resolved_at', 'resolved_by', 'staff_note'])
    return f'Report #{r.pk} for {r.code} resolved.'


# ─────────────────────────────── chat ───────────────────────────────

def chat_target(business, user, to):
    """The thread for 'team', 'support' or a colleague's name/email. Only threads this person may use."""
    from . import chat
    to = str(to or 'team').strip()
    low = to.lower().lstrip('@')
    if low in ('team', 'everyone', 'all', ''):
        return chat.team_thread(business), f'{business.business_name} team'
    if low in ('support', 'taptap', 'taptap support'):
        return chat.support_thread(business), 'TapTap Support'
    hits = [u for u in chat.people_of(business) if u.pk != user.pk and low in {
        (u.first_name or '').lower(), (u.get_full_name() or '').lower(), (u.username or '').lower(),
        (u.email or '').lower(), (u.email or '').split('@')[0].lower()}]
    if len(hits) != 1:
        raise FitActionError(f'I could not find exactly one colleague called “{to}”. Use their first name or email.')
    return chat.direct_thread(business, user, hits[0]), chat.display_name(hits[0])


def _check_chat(business, a, user=None):
    body = str(a.get('body') or a.get('message') or '').strip()
    if not body:
        raise FitActionError('What should the message say?')
    if len(body) > 1500:
        raise FitActionError('Keep chat messages under 1500 characters.')
    thread, name = chat_target(business, user, a.get('to'))
    return {'to': str(a.get('to') or 'team')[:80], 'body': body}, f'Send to {name}', body[:480], reverse('chat_page')


def _do_chat(business, p, user):
    from . import chat
    thread, name = chat_target(business, user, p['to'])
    if not chat.can_access(user, thread, business):
        raise FitActionError('You cannot post in that conversation.')
    chat.post(thread, user, p['body'] + '\n\n(sent with Fit)')
    return f'Message sent to {name}.'


# ─────────────────────────────── monitors ───────────────────────────────

def _bool(v):
    return v is True or str(v).strip().lower() in ('1', 'true', 'yes', 'on')


def _num(a, key, lo, hi, kind=int):
    v = a.get(key)
    if v in (None, ''):
        return None
    try:
        n = kind(v)
    except (TypeError, ValueError):
        raise FitActionError(f'{key} must be a number.')
    if not lo <= n <= hi:
        raise FitActionError(f'{key} must be between {lo} and {hi}.')
    return n


def _check_create_monitor(business, a):
    from .fit_monitors import KINDS as MK, _names
    from .models_fit import FitMonitor
    kind = str(a.get('monitor_kind') or '').strip()
    if kind not in MK:
        raise FitActionError(f'Which monitor? One of: {", ".join(MK)}.')
    label, _, defaults, responses = MK[kind]
    params = dict(defaults)
    plans, codes = _names(a.get('plans')), [c.replace(' ', '') for c in _names(a.get('codes'))]
    if plans:
        known = {p.lower(): p for p in business.plans.values_list('name', flat=True)}
        try:
            from .models_member_plans import MemberPlan
            known.update({p.lower(): p for p in MemberPlan.objects.filter(business=business).values_list('name', flat=True)})
        except Exception:
            pass
        known.update({p.lower(): p for p in business.vouchers.exclude(plan_name='').values_list('plan_name', flat=True).distinct()[:500]})
        bad = [p for p in plans if p.lower() not in known]
        if bad:
            raise FitActionError(f'No plan called {", ".join(bad)}. Plans: {", ".join(sorted(set(known.values()))[:25])}.')
        params['plans'] = [known[p.lower()] for p in plans]
    if codes:
        found = {c.upper(): c for c in business.vouchers.filter(code__in=codes).values_list('code', flat=True)}
        found.update({c.upper(): c for c in business.vouchers.filter(code__iregex=r'^(' + '|'.join(map(re.escape, codes)) + ')$')
                      .values_list('code', flat=True)})
        bad = [c for c in codes if c.upper() not in found]
        if bad:
            raise FitActionError(f'No voucher or member {", ".join(bad)} in {business.business_name}.')
        params['codes'] = [found[c.upper()] for c in codes]
    if kind == 'voucher_online' and not codes:
        raise FitActionError('Which voucher or member should I watch? Give its code.')
    if _bool(a.get('members_only')):
        params['members_only'] = True
    for key, lo, hi, cast in (('threshold', 1, 10000, float), ('minutes', 1, 1440, int), ('days', 1, 60, int),
                              ('min_users', 1, 10000, int), ('min_idle_minutes', 0, 1440, int)):
        n = _num(a, key, lo, hi, cast)
        if n is not None:
            params[key] = n
    if 'once' in a:
        params['once'] = _bool(a.get('once'))
    routers = []
    for r in _names(a.get('routers')):
        hit = business.routers.filter(pk=int(r)).first() if r.isdigit() else business.routers.filter(name__iexact=r).first()
        if not hit:
            raise FitActionError(f'No router “{r}”.')
        routers.append(hit.pk)
    if routers:
        params['routers'] = routers
    response = str(a.get('response') or responses[0]).strip()
    if response not in responses:
        raise FitActionError(f'A “{label}” monitor can only: {", ".join(dict(FitMonitor.RESPONSES)[r] for r in responses)}.')
    hours = _num(a, 'hours', 1, 24 * 90, int)
    audience = 'team' if str(a.get('audience') or '').lower() in ('team', 'everyone', 'all') else 'me'
    name = (str(a.get('name') or '').strip() or (label + (f' — {", ".join(params.get("plans") or params.get("codes") or [])}'
                                                      if params.get('plans') or params.get('codes') else '')))[:120]
    p = {'monitor_kind': kind, 'name': name, 'params': params, 'response': response, 'audience': audience,
         'email': _bool(a.get('email')), 'notify_member': _bool(a.get('notify_member')),
         'cooldown_minutes': _num(a, 'cooldown_minutes', 1, 1440, int) or 30, 'hours': hours}
    fake = FitMonitor(business=business, kind=kind, params=params, response=response, audience=audience,
                      email=p['email'], notify_member=p['notify_member'])
    from .fit_monitors import describe
    detail = describe(fake) + (f'. Stops after {hours} h.' if hours else '. Runs until you pause it.')
    if response == 'free_slot':
        detail += ' Only a device that is offline and unused is ever removed.'
    return p, f'Monitor: {name}', detail, reverse('fit_monitors_page')


def _do_create_monitor(business, p, user):
    from .models_fit import FitMonitor
    m = FitMonitor.objects.create(business=business, created_by=user, name=p['name'], kind=p['monitor_kind'], params=p['params'],
                                  response=p['response'], audience=p['audience'], email=p['email'], notify_member=p['notify_member'],
                                  cooldown_minutes=p['cooldown_minutes'], source='fit',
                                  expires_at=timezone.now() + timedelta(hours=p['hours']) if p.get('hours') else None)
    return f'Monitor #{m.pk} “{m.name}” is watching now. See it under Monitors.'


def _monitor(business, a):
    from .models_fit import FitMonitor
    try:
        pk = int(a.get('monitor_id'))
    except (TypeError, ValueError):
        raise FitActionError('Which monitor? Give its number (monitor_id).')
    m = FitMonitor.objects.filter(business=business, pk=pk).first()
    if not m:
        raise FitActionError(f'There is no monitor #{pk} in {business.business_name}.')
    return m


def _check_pause_monitor(business, a):
    m = _monitor(business, a)
    return {'monitor_id': m.pk}, f'Pause monitor “{m.name}”', 'It stops watching until resumed.', reverse('fit_monitors_page')


def _do_pause_monitor(business, p, user):
    m = _monitor(business, p)
    m.active = False
    m.save(update_fields=['active'])
    return f'Monitor “{m.name}” paused.'


def _check_resume_monitor(business, a):
    m = _monitor(business, a)
    return {'monitor_id': m.pk}, f'Resume monitor “{m.name}”', 'It starts watching again.', reverse('fit_monitors_page')


def _do_resume_monitor(business, p, user):
    m = _monitor(business, p)
    m.active, m.expires_at = True, (None if m.expires_at and m.expires_at <= timezone.now() else m.expires_at)
    m.save(update_fields=['active', 'expires_at'])
    return f'Monitor “{m.name}” is watching again.'


def _check_delete_monitor(business, a):
    m = _monitor(business, a)
    return {'monitor_id': m.pk}, f'Delete monitor “{m.name}”', f'{m.hits} alert(s) so far; its log is kept.', reverse('fit_monitors_page')


def _do_delete_monitor(business, p, user):
    m = _monitor(business, p)
    name = m.name
    m.delete()
    return f'Monitor “{name}” deleted.'


def _check_free_idle_slot(business, a):
    from .fit_monitors import idle_binding
    v = _voucher(business, a.get('code'))
    b = idle_binding(v, _num(a, 'min_idle_minutes', 0, 1440, int) or 20)
    if b is None:
        raise FitActionError(f'{v.code} has no idle slot: every locked device is online or was used in the last minutes.')
    return {'code': v.code, 'binding': b.pk, 'reason': _reason(a, 'Freed an idle slot with Fit')}, \
        f'Free one idle slot on {v.code}', f'Removes {b.label or b.current_mac or "slot " + str(b.slot_no)} (slot {b.slot_no}, not online). ' \
        'The other devices stay connected.', f'/vouchers/{v.pk}/'


def _do_free_idle_slot(business, p, user):
    from .shared_voucher_device_control import remove_one
    v = _voucher(business, p['code'])
    ok, result = remove_one(v, p['binding'], user=user)
    return f'{v.code}: {result}'


# ─────────────────────────────── registry ───────────────────────────────
# kind: (group, permissions (any of), check(business, args) -> (params, title, detail, link), do(business, params, user), danger)
KINDS = {
    'freeze_voucher': ('vouchers', ('vouchers.support',), _check_freeze, _do_freeze, 'warn'),
    'unfreeze_voucher': ('vouchers', ('vouchers.support',), _check_unfreeze, _do_unfreeze, 'ok'),
    'warn_voucher': ('vouchers', ('vouchers.warn',), _check_warn, _do_warn, 'warn'),
    'disable_voucher': ('vouchers', ('vouchers.support',), _check_disable, _do_disable, 'danger'),
    'enable_voucher': ('vouchers', ('vouchers.support',), _check_enable, _do_enable, 'ok'),
    'add_time': ('time', ('vouchers.support',), _check_add_time, _do_add_time, 'ok'),
    'reduce_time': ('time', ('vouchers.support',), _check_reduce_time, _do_reduce_time, 'warn'),
    'reset_devices': ('devices', ('vouchers.support',), _check_reset, _do_reset, 'warn'),
    'block_device': ('devices', ('vouchers.support', 'network.manage'), _check_block, _do_block, 'danger'),
    'unblock_device': ('devices', ('vouchers.support', 'network.manage'), _check_unblock, _do_unblock, 'ok'),
    'disconnect_session': ('devices', ('vouchers.support', 'network.manage'), _check_disconnect, _do_disconnect, 'warn'),
    'resolve_report': ('reports', ('vouchers.support', 'vouchers.view'), _check_resolve, _do_resolve, 'ok'),
    'track_voucher': ('tracking', ('vouchers.view', 'vouchers.support', 'vouchers.create', 'plans.manage'), _check_track, _do_track, 'ok'),
    'send_chat': ('chat', (), _check_chat, _do_chat, 'ok'),
    'create_monitor': ('monitors', MONITOR_PERMS, _check_create_monitor, _do_create_monitor, 'ok'),
    'pause_monitor': ('monitors', MONITOR_PERMS, _check_pause_monitor, _do_pause_monitor, 'ok'),
    'resume_monitor': ('monitors', MONITOR_PERMS, _check_resume_monitor, _do_resume_monitor, 'ok'),
    'delete_monitor': ('monitors', MONITOR_PERMS, _check_delete_monitor, _do_delete_monitor, 'warn'),
    'free_idle_slot': ('devices', ('vouchers.support',), _check_free_idle_slot, _do_free_idle_slot, 'warn'),
}


def has_perm(perms, need):
    return not need or any(p in perms for p in need)


def kinds_for(perms):
    """Action kinds this person could ever approve (shown to the model so it does not offer the rest)."""
    return [k for k, (_, need, *_rest) in KINDS.items() if has_perm(perms, need)]


def _validate(business, user, kind, args):
    spec = KINDS.get(kind)
    if not spec:
        raise FitActionError(f'Unknown action “{kind}”.')
    check = spec[2]
    return check(business, args, user) if check is _check_chat else check(business, args)


def propose(ctx, kind, args):
    """Store a proposal. Returns the card the browser shows. Raises FitActionError with a readable reason."""
    from .models_fit import FitAction
    spec = KINDS.get(kind)
    if not spec:
        raise FitActionError(f'Unknown action “{kind}”. Use one of: {", ".join(KINDS)}.')
    group, need, _, _, danger = spec
    if not has_perm(ctx.perms, need):
        raise FitActionError('Your role does not allow this change, so I cannot offer it. Ask the business owner.')
    params, title, detail, link = _validate(ctx.business, ctx.user, kind, args or {})
    a = FitAction.objects.create(business=ctx.business, conversation=ctx.conversation, proposed_to=ctx.user,
                                 kind=kind, params=params, title=title[:200], detail=(detail or '')[:500])
    card = card_json(a, ctx.settings, link=link, danger=danger)
    ctx.cards.setdefault('actions', []).append(card)
    return card


def card_json(a, settings, link='', danger=None):
    group, *_rest = KINDS.get(a.kind, ('?', (), None, None, 'ok'))
    expired = a.status == 'proposed' and timezone.now() - a.created_at > ACTION_TTL
    return {'id': a.pk, 'kind': a.kind, 'title': a.title, 'detail': a.detail, 'status': 'expired' if expired else a.status,
            'result': a.result, 'group': group, 'group_label': GROUPS.get(group, (group,))[0],
            'authorized': bool(settings and settings.allows(group)), 'link': link,
            'danger': danger or KINDS.get(a.kind, (None,) * 5)[4] or 'ok'}


def run(action, user, perms, settings):
    """Carry out an approved proposal. Returns (ok, message)."""
    from .utils import log
    if action.status != 'proposed':
        return False, f'This action is already {action.get_status_display().lower()}.'
    if action.proposed_to_id != user.pk:
        return False, 'Only the person Fit offered this to can approve it.'
    now = timezone.now()
    if now - action.created_at > ACTION_TTL:
        action.status, action.decided_at = 'expired', now
        action.save(update_fields=['status', 'decided_at'])
        return False, 'This offer is more than 30 minutes old. Ask Fit again so it checks the latest state.'
    group, need, _, do, _ = KINDS[action.kind]
    if not settings.allows(group):
        return False, f'The business owner has not allowed Fit to make “{GROUPS[group][0]}” changes. They can switch it on in Fit settings.'
    if not has_perm(perms, need):
        return False, 'Your role does not allow this change.'
    try:
        _validate(action.business, user, action.kind, action.params)        # still makes sense right now?
        result = str(do(action.business, action.params, user) or 'Done.')
        action.status = 'done'
    except Exception as exc:  # noqa: BLE001 — services raise ValueError subclasses and router errors
        if not isinstance(exc, ValueError):
            logger.exception('Fit action %s failed', action.pk)
        result, action.status = str(exc) or 'It did not work.', 'failed'
    action.result, action.decided_by, action.decided_at = result[:500], user, now
    action.save(update_fields=['status', 'result', 'decided_by', 'decided_at'])
    try:
        log(action.business, 'Fit action', f'{action.title}: {action.get_status_display()}', status='Success' if action.status == 'done' else 'Failed')
        from .live import push_event
        push_event(action.business_id, f'Fit: {action.title}', 'fix' if action.status == 'done' else 'info')
    except Exception:
        logger.exception('Fit action log')
    return action.status == 'done', action.result
