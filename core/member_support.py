"""Member support: the controls on a member's own page (core/views_member_detail.py).

* **Pause / unpause** — a pause is a freeze (core/voucher_freeze.py): internet stops at once and the time left
  stands still; unpausing gives it back from that moment. A pause can be **scheduled**: on a date, or "when N days
  are left" (that follows renewals: if the member renews, the pause moves with the new end date). A pause stays
  until someone unpauses it, or until an optional unpause date. The schedule runs every minute
  (core/tasks.deliver_notifications → run_due).
* **Password** — a new random password, one typed by staff, or "same as username". The router is updated and the
  member signed out (core/members.change_password). The new password is shown once to staff, and can be emailed.
* **Access & security** — send a member-portal access link, sign the member out of the portal everywhere, sign
  their devices out of the Wi-Fi, free their device slots, block / unblock the account.
* **Details** — name, phone, email, reminders, note, Member Plan. Internal support notes go to the history.

Every action lands in the member's history (VoucherEvent) with who did it and why.
"""
from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import Voucher
from .models_member_support import MemberSchedule

logger = logging.getLogger('taptap.members')

PASSWORD_ALPHABET = 'abcdefghjkmnpqrstuvwxyz23456789'        # no 0/o, 1/l/i: easy to read out on the phone
MAX_DAYS_LEFT = 365


class SupportError(ValueError):
    """A problem the person can fix; shown as is."""


def _who(user):
    return user if getattr(user, 'is_authenticated', False) else None


def new_password(length=8):
    return ''.join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


# ─────────────────────────────── scheduled pause ───────────────────────────────

def _end(member):
    from .voucher_history import ends_at
    return ends_at(member)


def pending(member):
    return list(member.member_schedules.filter(status='pending').order_by('run_at'))


def schedule_pause(member, *, when=None, days_left=None, resume_at=None, reason='', user=None, now=None):
    """Pause the member on ``when`` or when ``days_left`` days are left; optionally unpause on ``resume_at``.

    Replaces any pause already waiting for this member. Returns (pause, resume_or_None)."""
    now = now or timezone.now()
    reason = (reason or '').strip()[:255]
    if member.deleted_at:
        raise SupportError('This member is in the bin.')
    if not reason:
        raise SupportError('Give a reason — it is kept in the member’s history and shown on the pause page.')
    if member.frozen_at:
        raise SupportError('This member is already paused. Unpause first, or set an unpause date.')
    if days_left is not None:
        try:
            days_left = int(days_left)
        except (TypeError, ValueError):
            raise SupportError('Days left must be a number.')
        if not 1 <= days_left <= MAX_DAYS_LEFT:
            raise SupportError(f'Days left must be between 1 and {MAX_DAYS_LEFT}.')
        end = _end(member)
        if end is None:
            raise SupportError('This member’s time has not started (or has no end date), so “days left” cannot be worked '
                               'out yet. Choose a date instead.')
        when = end - timedelta(days=days_left)
        if when <= now:
            left = max(0, (end - now).days)
            raise SupportError(f'Only {left} day{"s" if left != 1 else ""} left already — pause now instead.')
    if when is None:
        raise SupportError('Choose when to pause.')
    if timezone.is_naive(when):
        when = timezone.make_aware(when)
    if when <= now + timedelta(minutes=1):
        raise SupportError('Choose a time in the future (or press Pause now).')
    end = _end(member)
    if end and when >= end:
        raise SupportError(f'The member’s time ends {timezone.localtime(end):%d %b %Y %H:%M} — before that pause. '
                           f'Choose an earlier date.')
    if resume_at is not None:
        if timezone.is_naive(resume_at):
            resume_at = timezone.make_aware(resume_at)
        if resume_at <= when:
            raise SupportError('The unpause date must be after the pause.')
    with transaction.atomic():
        old = member.member_schedules.filter(status='pending')
        n_old = old.count()
        old.update(status='cancelled', result='Replaced by a new schedule', done_at=now)
        pause = MemberSchedule.objects.create(business=member.business, member=member, action='pause', run_at=when,
                                              days_left=days_left, reason=reason, created_by=_who(user))
        resume = MemberSchedule.objects.create(business=member.business, member=member, action='resume', run_at=resume_at,
                                               reason=reason, created_by=_who(user)) if resume_at else None
    from .voucher_history import record
    text = (f'Pause when {days_left} day{"s" if days_left != 1 else ""} are left (now {timezone.localtime(when):%d %b %Y %H:%M})'
            if days_left else f'Pause on {timezone.localtime(when):%d %b %Y %H:%M}')
    text += f' · unpause on {timezone.localtime(resume_at):%d %b %Y %H:%M}' if resume_at else ' · stays paused until unpaused'
    if n_old:
        text += ' · replaces the earlier schedule'
    record(member, 'note', user=user, reason=f'Pause scheduled: {reason}', text=text)
    return pause, resume


def schedule_resume(member, *, when, reason='', user=None, now=None):
    """Unpause a paused member on a date (for a pause already in force)."""
    now = now or timezone.now()
    if not member.frozen_at and not member.member_schedules.filter(status='pending', action='pause').exists():
        raise SupportError('This member is not paused and has no pause waiting.')
    if timezone.is_naive(when):
        when = timezone.make_aware(when)
    if when <= now + timedelta(minutes=1):
        raise SupportError('Choose a time in the future (or press Unpause now).')
    with transaction.atomic():
        member.member_schedules.filter(status='pending', action='resume').update(status='cancelled', result='Replaced', done_at=now)
        s = MemberSchedule.objects.create(business=member.business, member=member, action='resume', run_at=when,
                                          reason=(reason or '').strip()[:255], created_by=_who(user))
    from .voucher_history import record
    record(member, 'note', user=user, reason='Unpause scheduled', text=f'Unpause on {timezone.localtime(when):%d %b %Y %H:%M}')
    return s


def cancel_schedule(member, pk, user=None):
    s = member.member_schedules.filter(pk=pk, status='pending').first()
    if not s:
        raise SupportError('That schedule has already run or was cancelled.')
    s.status, s.result, s.done_at = 'cancelled', f'Cancelled by {getattr(user, "username", "staff")}', timezone.now()
    s.save(update_fields=['status', 'result', 'done_at'])
    from .voucher_history import record
    record(member, 'note', user=user, reason=f'{s.get_action_display()} schedule cancelled',
           text=f'Was set for {timezone.localtime(s.run_at):%d %b %Y %H:%M}')
    return s


def follow_days_left(now=None):
    """'When N days are left' pauses move with the member's end date (renewals, added time)."""
    for s in MemberSchedule.objects.filter(status='pending', action='pause', days_left__isnull=False).select_related('member'):
        end = _end(s.member)
        if end is None:
            continue
        want = end - timedelta(days=s.days_left)
        if abs((want - s.run_at).total_seconds()) >= 60:
            MemberSchedule.objects.filter(pk=s.pk, status='pending').update(run_at=want)


def run_due(now=None):
    """Run every schedule whose time has come. Safe to call from several workers at once."""
    from .voucher_freeze import freeze, unfreeze
    now = now or timezone.now()
    try:
        follow_days_left(now)
    except Exception:
        logger.exception('member schedules: following days-left pauses failed')
    ran = 0
    for s in MemberSchedule.objects.filter(status='pending', run_at__lte=now).select_related('member__router', 'created_by')[:200]:
        if not MemberSchedule.objects.filter(pk=s.pk, status='pending').update(status='running'):
            continue                                     # another worker took it
        m = Voucher.all_objects.select_related('router', 'business').get(pk=s.member_id)
        try:
            if s.action == 'pause':
                out = freeze([m], user=s.created_by, reason=f'Scheduled pause: {s.reason}'[:255], source='auto')
            else:
                out = unfreeze([m], user=s.created_by, reason='Scheduled unpause' + (f': {s.reason}' if s.reason else ''), source='auto')
            if out['done']:
                status, result = 'done', '; '.join(msg for _, msg in out['router'])[:255] or 'Done'
            else:
                why = out['skipped'][0][1] if out['skipped'] else 'nothing to do'
                status, result = 'skipped', f'Not run: {why}'
                if s.action == 'pause':                  # a pause that could not happen makes its unpause pointless
                    m.member_schedules.filter(status='pending', action='resume').update(
                        status='cancelled', result='Its pause did not run', done_at=now)
        except Exception as exc:                          # keep the schedule visible with the reason
            logger.exception('member schedule %s failed', s.pk)
            status, result = 'skipped', f'Failed: {exc}'[:255]
        MemberSchedule.objects.filter(pk=s.pk).update(status=status, result=result, done_at=timezone.now())
        ran += 1
    return ran


# ─────────────────────────────── pause / unpause now ───────────────────────────────

def pause_now(member, reason, user=None):
    from .voucher_freeze import freeze, FreezeError
    try:
        out = freeze([member], user=user, reason=reason)
    except FreezeError as exc:
        raise SupportError(str(exc))
    if not out['done']:
        raise SupportError(f'Not paused: {out["skipped"][0][1]}.' if out['skipped'] else 'Not paused.')
    member.member_schedules.filter(status='pending', action='pause').update(status='cancelled', result='Paused by hand instead',
                                                                             done_at=timezone.now())
    return out


def unpause_now(member, user=None, reason=''):
    from .voucher_freeze import unfreeze
    out = unfreeze([member], user=user, reason=reason)
    if not out['done']:
        raise SupportError('This member is not paused.')
    member.member_schedules.filter(status='pending', action='resume').update(status='cancelled', result='Unpaused by hand',
                                                                              done_at=timezone.now())
    return out


# ─────────────────────────────── password & access ───────────────────────────────

def reset_password(member, *, mode='random', password='', reason='', user=None, email=False):
    """Returns (new_password_or_empty, router_result, emailed_to)."""
    from . import members as mem
    if mode == 'random':
        pw, same = new_password(), False
    elif mode == 'same':
        pw, same = '', True
    else:
        pw, same = password, False
    try:
        ok, result = mem.change_password(member, pw, same=same, user=user, reason=reason or 'Reset by staff')
    except mem.MemberError as exc:
        raise SupportError(str(exc))
    shown = member.login_password
    emailed = ''
    if email:
        to = contact_email(member)
        if not to:
            raise SupportError('The password was changed, but this member has no email address to send it to.')
        from .member_notifications import _send
        try:
            _send(to, f'[{member.business.business_name}] Your new Wi-Fi password', 'core/email/member_password_reset.txt',
                  'core/email/member_password_reset.html', {'business': member.business, 'member': member, 'password': shown})
            emailed = to
            from .voucher_history import record
            record(member, 'note', user=user, reason='New password emailed', text=f'Sent to {to}')
        except Exception as exc:
            logger.exception('password email for %s', member.pk)
            raise SupportError(f'The password was changed, but the email could not be sent ({exc}).')
    return shown, result, emailed


def contact_email(member):
    from .members import notification_settings_for
    pref = notification_settings_for(member)
    return (pref.email if pref else '') or ''


def send_portal_link(member, request=None, user=None):
    from .member_self_service import issue_magic_link
    try:
        issue_magic_link(member, request=request, reason='Sent by staff from the member page')
    except ValueError as exc:
        raise SupportError(str(exc))
    from .voucher_history import record
    record(member, 'note', user=user, reason='Member portal link sent', text=f'To {contact_email(member)}')
    return contact_email(member)


def portal_sign_out(member, user=None, reason=''):
    """Every member-portal session of this member ends (their next click asks for a new access link)."""
    from .member_self_service import profile
    p = profile(member)
    p.auth_version += 1
    p.save(update_fields=['auth_version', 'updated_at'])
    from .models_member_portal import MemberPortalMagicLink
    now = timezone.now()
    MemberPortalMagicLink.objects.filter(member=member, used_at__isnull=True, expires_at__gt=now).update(expires_at=now)
    from .voucher_history import record
    record(member, 'note', user=user, reason=reason or 'Signed out of the member portal everywhere',
           text='All member-portal sessions and unused access links ended')


def wifi_sign_out(member, user=None, reason=''):
    """Disconnect the member's devices from the Wi-Fi now (they can log in again)."""
    from .voucher_history import _router_apply, record
    via, result, ok = _router_apply(member, 'reset', user=user)
    record(member, 'note', user=user, reason=reason or 'Signed out of the Wi-Fi', via=via, router_result=result,
           text='Devices disconnected; they can log in again')
    return ok, result


def reset_devices(member, user=None, reason=''):
    from .voucher_history import reset_devices as vh_reset
    return vh_reset(member, user=user, reason=reason or 'Device slots freed by staff')


def block(member, user=None, reason=''):
    from .voucher_history import disable, VoucherActionError
    if not (reason or '').strip():
        raise SupportError('Give a reason for blocking.')
    try:
        return disable(member, user=user, reason=reason)
    except VoucherActionError as exc:
        raise SupportError(str(exc))


def unblock(member, user=None, reason=''):
    from .voucher_history import enable, VoucherActionError, time_is_up
    if member.status != 'disabled' or member.frozen_at:
        raise SupportError('This member is not blocked.' + (' It is paused — use Unpause.' if member.frozen_at else ''))
    if time_is_up(member):
        raise SupportError('This member’s time has run out — renew instead of unblocking.')
    try:
        return enable(member, user=user, reason=reason or 'Unblocked by staff')
    except VoucherActionError as exc:
        raise SupportError(str(exc))


# ─────────────────────────────── details & notes ───────────────────────────────

def update_details(member, data, user=None):
    from . import members as mem
    name = str(data.get('customer_name', member.customer_name) or '').strip()[:120]
    phone = str(data.get('customer_phone', member.customer_phone) or '').strip()[:60]
    note = str(data.get('note', member.note) or '').strip()[:255]
    changed = [label for label, old, new in (('name', member.customer_name, name), ('phone', member.customer_phone, phone),
                                             ('note', member.note, note)) if (old or '') != new]
    pref_before = mem.notification_settings_for(member)
    email_before = pref_before.email if pref_before else ''
    rem_before = pref_before.reminders_enabled if pref_before else False
    try:
        pref = mem.save_member_notifications(member, data, user=user)
    except mem.MemberError as exc:
        raise SupportError(str(exc))
    if (pref.email or '') != (email_before or ''):
        changed.append('email')
    if pref.reminders_enabled != rem_before:
        changed.append('reminders ' + ('on' if pref.reminders_enabled else 'off'))
    Voucher.objects.filter(pk=member.pk).update(customer_name=name, customer_phone=phone, note=note)
    member.customer_name, member.customer_phone, member.note = name, phone, note
    if changed:
        from .voucher_history import record
        record(member, 'note', user=user, reason='Details updated', text='Changed: ' + ', '.join(changed))
    return changed


def add_note(member, text, user=None):
    text = (text or '').strip()
    if not text:
        raise SupportError('Write the note first.')
    from .voucher_history import record
    record(member, 'note', user=user, reason=text[:255], text=text[:1000] if len(text) > 255 else '', support_note=True)


def change_plan(member, plan_id, user=None):
    from . import members as mem
    try:
        plan = mem.plan_choice(member.business, plan_id)
        mem.assign_member_plan(member, plan, user=user)
    except mem.MemberError as exc:
        raise SupportError(str(exc))
    result = 'changed in TapTap only (no router)'
    if member.router_id:
        from .views_agents import push_one
        res = push_one(Voucher.objects.select_related('router').get(pk=member.pk), plan)
        result = f'sent to {member.router.name}' if res is True else f'router not updated yet ({res}) — the next sync sends it'
    return plan, result


# ─────────────────────────────── security overview ───────────────────────────────

def security(member):
    from .models import VoucherEvent
    from .models_member_portal import MemberPortalEvent, MemberPortalMagicLink
    from .member_self_service import profile
    p = profile(member)
    last_pw = VoucherEvent.objects.filter(voucher=member, event='password_changed').order_by('-created_at').first()
    links = list(MemberPortalMagicLink.objects.filter(member=member).order_by('-created_at')[:5])
    events = list(MemberPortalEvent.objects.filter(member=member).order_by('-created_at')[:15])
    ips = []
    for e in events:
        if e.ip_address and e.ip_address not in ips:
            ips.append(e.ip_address)
    flags = []
    if not member.password:
        flags.append(('warn', 'The password is the same as the username — anyone who knows the username can log in. Set a real password.'))
    if not contact_email(member):
        flags.append(('info', 'No email on file: the member cannot use the member portal or receive receipts and reminders.'))
    if member.max_devices and member.device_bindings.count() >= member.max_devices:
        flags.append(('info', f'All {member.max_devices} device slot{"s are" if member.max_devices != 1 else " is"} taken — a new phone cannot log in '
                              f'until a slot is freed.'))
    if len(ips) >= 4:
        flags.append(('warn', f'The member portal was used from {len(ips)} different addresses recently — check the account is not shared.'))
    return {'profile': p, 'last_password': last_pw, 'links': links, 'events': events, 'ips': ips[:6], 'flags': flags}
