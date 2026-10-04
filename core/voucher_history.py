"""Voucher state changes and their permanent history.

Every enable / disable / extend / device reset goes through this module so that
TapTap, the router and the history always agree:

* ``change_state()`` updates TapTap, sends the change to the router through the
  right channel (TapTap Tunnel / Direct API, or TapTap Link) and records a
  ``VoucherEvent`` with who, when, why, the channel and the router's answer.
* ``record()`` is used by automatic paths (enforcement, router mirroring,
  voided sales, deletion) so nothing changes a voucher silently.
* ``timeline()`` returns the full history. For vouchers that existed before
  history was recorded it also derives events from existing records (creation,
  sale, first use, device bindings, enforcement incidents, older activity log
  lines), so every voucher has a complete story.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger('taptap.vouchers')

MAX_EXTEND_HOURS = 8760
MAX_EXTEND_MINUTES = 366 * 24 * 60   # up to a year, in any mix of days/hours/minutes


# ─────────────────────────────── state helpers ───────────────────────────────

def ends_at(voucher):
    """When the voucher's time runs out, or None if the clock has not started or stands still (frozen)."""
    if getattr(voucher, 'frozen_at', None):
        return None
    if voucher.expires_at:
        return voucher.expires_at
    if voucher.used_at and voucher.duration_minutes:
        return voucher.used_at + timedelta(minutes=voucher.duration_minutes)
    return None


def time_is_up(voucher, now=None):
    end = ends_at(voucher)
    return bool(end and end <= (now or timezone.now()))


def display_state(voucher, now=None):
    """(key, label) shown to people. Separates 'disabled by you' from 'time ran out'."""
    now = now or timezone.now()
    if getattr(voucher, 'frozen_at', None):
        return ('warned', 'Warning — internet paused') if voucher.freeze_kind == 'warning' else ('frozen', 'Frozen')
    if voucher.status == 'archived':
        return 'archived', 'Archived — off the router'
    if voucher.status == 'disabled':
        return 'disabled', 'Disabled'
    if voucher.status == 'expired' or time_is_up(voucher, now):
        return 'expired', 'Time ran out'
    if voucher.used_at:
        return 'used', 'In use'
    if voucher.sold_at:
        return 'sold', 'Sold, not used'
    return 'stock', 'In stock'


def channel(router):
    """Human name of the channel TapTap will use for this router right now."""
    if not router:
        return 'TapTap only'
    from .linkops import uses_link
    if uses_link(router):
        return 'TapTap Link'
    if getattr(router, 'connection_mode', 'api') == 'agent':
        return 'TapTap Tunnel'
    return 'Direct API'


# ─────────────────────────────── recording ───────────────────────────────

def record(voucher, event, *, user=None, source='user', reason='', via='', router_result='',
           status_before='', status_after='', business=None, code='', **detail):
    """Write one history entry. Never raises: history must not break the action."""
    from .models import VoucherEvent
    try:
        ev = VoucherEvent.objects.create(
            business=business or voucher.business,
            voucher=voucher if voucher is not None and voucher.pk else None,
            voucher_code=(code or getattr(voucher, 'code', '') or '')[:120],
            event=event, source=source,
            user=user if getattr(user, 'is_authenticated', False) else None,
            status_before=status_before or '', status_after=status_after or '',
            reason=(reason or '')[:255], via=via or '', router_result=(router_result or '')[:255],
            detail={k: v for k, v in detail.items() if v not in (None, '')},
        )
    except Exception:
        logger.exception('Could not record voucher history for %s', getattr(voucher, 'code', code))
        return None
    try:   # people tracking this voucher, its batch or its plan (core/tracking.py)
        from .tracking import from_history
        from_history(voucher, event, str(detail.get('text') or reason or '')[:200], user)
    except Exception:
        logger.exception('tracking for %s', getattr(voucher, 'code', code))
    return ev


# ─────────────────────────────── actions ───────────────────────────────

class VoucherActionError(ValueError):
    pass


def _router_apply(voucher, action, hours=None, user=None, minutes=None, total=False):
    """Send the change to the router. Returns (via, result_text, ok)."""
    router = voucher.router
    via = channel(router)
    if not router:
        return via, 'No router assigned — changed in TapTap only', True
    if via == 'TapTap Link':
        from .linkops import send
        try:
            if action == 'disable':
                send(router, 'hotspot_user_set', {'name': voucher.code, 'disabled': True}, label=f'Disable voucher {voucher.code}', user=user)
                send(router, 'disconnect', {'user': voucher.code}, label=f'Disconnect {voucher.code}', user=user)
            elif action == 'enable':
                if minutes:
                    send(router, 'hotspot_user_extend', {'name': voucher.code, 'seconds': int(minutes) * 60, 'total': bool(total)},
                         label=f'Enable {voucher.code} (+{_mtext(minutes)})', user=user)
                else:
                    send(router, 'hotspot_user_set', {'name': voucher.code, 'disabled': False}, label=f'Enable voucher {voucher.code}', user=user)
            elif action == 'reset':
                send(router, 'disconnect', {'user': voucher.code}, label=f'Reset session of {voucher.code}', user=user)
            return via, 'Queued — the router applies it at its next check-in', True
        except ValueError as exc:
            return via, f'Not sent yet: {exc}', False
    from .mikrotik import MikroTikService
    try:
        svc = MikroTikService(router).connect()
        try:
            if action == 'disable':
                svc.disable_voucher(voucher.code)
                svc.reset_active_by_name(voucher.code)
                try:      # free the phone so it sees the login page again (iPhones: see MikroTikService.release_devices)
                    from .models import VoucherDeviceBinding
                    macs = [m for pair in VoucherDeviceBinding.objects.filter(voucher=voucher).values_list('current_mac', 'previous_mac') for m in pair if m]
                    svc.release_devices(voucher.code, macs)
                except Exception:
                    pass
                return via, 'Applied on the router', True
            if action == 'enable':
                if minutes:
                    limit = svc.extend_voucher(voucher.code, seconds=int(minutes) * 60, total=total)
                    if limit is None:
                        return via, 'Voucher not found on the router', False
                    return via, 'Applied on the router' + (f' (uptime limit now {limit})' if limit else ''), True
                return (via, 'Applied on the router', True) if svc.enable_voucher(voucher.code) \
                    else (via, 'Voucher not found on the router', False)
            if action == 'reset':
                svc.reset_active_by_name(voucher.code)
                return via, 'Session removed on the router', True
        finally:
            svc.close()
    except Exception as exc:
        return via, f'Router update failed: {exc}', False
    return via, '', True


def disable(voucher, user=None, reason=''):
    if voucher.frozen_at:
        raise VoucherActionError(f'{voucher.code} is frozen. Unfreeze it first if you want to disable it for good.')
    if voucher.status == 'disabled':
        raise VoucherActionError(f'{voucher.code} is already disabled.')
    before = voucher.status
    voucher.status = 'disabled'
    voucher.save(update_fields=['status'])
    via, result, ok = _router_apply(voucher, 'disable', user=user)
    record(voucher, 'disabled', user=user, reason=reason, via=via, router_result=result,
           status_before=before, status_after='disabled')
    return ok, result


def _mtext(minutes):
    from .durations import text
    return text(int(minutes))


def parse_added_time(days=None, hours=None, minutes=None):
    """Total minutes from free days/hours/minutes fields (blank = 0). Raises VoucherActionError."""
    total = 0
    for val, mult, label in ((days, 1440, 'days'), (hours, 60, 'hours'), (minutes, 1, 'minutes')):
        if val in (None, ''):
            continue
        try:
            n = int(str(val).strip())
        except ValueError:
            raise VoucherActionError(f'The {label} must be a whole number.')
        if n < 0:
            raise VoucherActionError(f'The {label} cannot be negative.')
        total += n * mult
    return total


def enable(voucher, user=None, reason='', add_hours=None, now=None, add_minutes=None):
    """Enable a disabled/expired voucher, and/or add time to it.

    Time can be any amount (``add_minutes``; ``add_hours`` is kept for older callers).
    * Voucher not used yet: the time is added to its duration — the clock still starts
      on first login, and the router's uptime limit becomes the new duration.
    * Clock running: the time goes on top of its current end (or from now if it had run
      out), and the router's uptime limit is raised by the same amount.
    If its time has run out, extra time is required: otherwise live enforcement would
    disconnect it again at once. Adding time is not a sale and is never counted as one."""
    now = now or timezone.now()
    try:
        minutes = int(add_minutes) if add_minutes not in (None, '', 0, '0') else None
        if minutes is None and add_hours not in (None, '', 0, '0'):
            minutes = int(add_hours) * 60
    except (TypeError, ValueError):
        raise VoucherActionError('Extra time must be a whole number.')
    if minutes is not None and not 1 <= minutes <= MAX_EXTEND_MINUTES:
        raise VoucherActionError('Extra time must be between 1 minute and 1 year.')
    if voucher.frozen_at:
        raise VoucherActionError(f'{voucher.code} is frozen. Unfreeze it first — its time then continues from where it stopped.')
    expired = time_is_up(voucher, now)
    if minutes and not voucher.duration_minutes and not voucher.expires_at:
        raise VoucherActionError(f'{voucher.code} has no time limit (unlimited plan), so there is no time to add.'
                                 + (' Use Enable to switch it back on.' if voucher.status != 'active' else ''))
    if voucher.status == 'active' and not expired and not minutes:
        raise VoucherActionError(f'{voucher.code} is already active. Enter the time to add.')
    if expired and not minutes:
        raise VoucherActionError(
            f'The time on {voucher.code} ran out {timezone.localtime(ends_at(voucher)):%d %b %Y %H:%M}. '
            'Add time to enable it — without it, TapTap would disconnect it again straight away.')
    before, old_end, old_duration = voucher.status, ends_at(voucher), voucher.duration_minutes
    voucher.status = 'active'
    fields = ['status']
    not_started = minutes and not old_end and not voucher.used_at
    if not_started:
        voucher.duration_minutes = int(voucher.duration_minutes or 0) + minutes
        fields.append('duration_minutes')
    elif minutes:
        # Extra time counts from now when the voucher had run out, otherwise from its current end.
        base = now if (not old_end or old_end <= now) else old_end
        voucher.expires_at = base + timedelta(minutes=minutes)
        fields.append('expires_at')
    voucher.save(update_fields=fields)
    via, result, ok = _router_apply(voucher, 'enable', user=user, minutes=minutes,
                                    total=bool(not_started))
    if not_started:
        result = (result + ' · ' if result else '') + f'valid for {_mtext(voucher.duration_minutes)} from first login'
    record(voucher, 'extended' if minutes else 'enabled', user=user, reason=reason, via=via, router_result=result,
           status_before=before, status_after='active', added_minutes=minutes,
           duration_before=old_duration if not_started else None,
           duration_after=voucher.duration_minutes if not_started else None,
           ends_before=old_end.isoformat() if old_end else None,
           ends_after=voucher.expires_at.isoformat() if minutes and not not_started else None)
    return ok, result


def reset_devices(voucher, user=None, reason=''):
    """Free every device slot of a sticky voucher: the next devices to log in are locked instead.
    Only people with voucher-support rights reach this (owner, admin, voucher support)."""
    from . import device_lock
    macs = device_lock.release_all(voucher)
    device_lock.unlock_on_router(voucher)     # clear the router-side MAC lock and its MAC cookies
    via, result, ok = _router_apply(voucher, 'reset', user=user)
    record(voucher, 'mac_reset', user=user, reason=reason, via=via, router_result=result,
           status_before=voucher.status, status_after=voucher.status, devices_removed=macs or None)
    return ok, result


# ─────────────────────────────── timeline ───────────────────────────────

ICONS = {
    'created': ('bi-plus-circle', 'secondary'), 'imported': ('bi-download', 'secondary'),
    'sold': ('bi-cash-coin', 'success'), 'activated': ('bi-wifi', 'info'),
    'device': ('bi-phone', 'secondary'), 'time_up': ('bi-hourglass-bottom', 'warning'),
    'disabled': ('bi-slash-circle', 'danger'), 'enabled': ('bi-check-circle', 'success'),
    'extended': ('bi-clock-history', 'success'), 'mac_reset': ('bi-arrow-counterclockwise', 'secondary'),
    'enforced': ('bi-shield-exclamation', 'danger'), 'router_disabled': ('bi-router', 'danger'),
    'router_enabled': ('bi-router', 'success'), 'sale_voided': ('bi-x-circle', 'warning'),
    'deleted': ('bi-trash', 'danger'), 'code_changed': ('bi-input-cursor-text', 'primary'), 'frozen': ('bi-snow', 'info'), 'unfrozen': ('bi-play-circle', 'success'),
    'fup_slowed': ('bi-speedometer', 'warning'), 'fup_restored': ('bi-speedometer2', 'success'), 'fup_lifted': ('bi-lightning-charge', 'info'),
    'warned': ('bi-exclamation-octagon', 'danger'), 'warning_accepted': ('bi-hand-thumbs-up', 'success'), 'shared_resolved': ('bi-people', 'secondary'), 'note': ('bi-chat-left-text', 'secondary'), 'legacy': ('bi-journal-text', 'secondary'),
}


def _item(kind, when, title, text='', who='', via='', result='', reason='', stored=False):
    icon, tone = ICONS.get(kind, ('bi-dot', 'secondary'))
    return {'kind': kind, 'at': when, 'title': title, 'text': text, 'who': who, 'via': via,
            'result': result, 'reason': reason, 'icon': icon, 'tone': tone, 'stored': stored}


def _who(user):
    if not user:
        return ''
    return user.get_full_name() or user.username


def timeline(voucher, now=None):
    """Newest-first list of everything that happened to this voucher."""
    from .models import Activity, SessionIncident, VoucherEvent, VoucherSale
    now = now or timezone.now()
    items = []

    from django.db.models import Q
    codes = [voucher.code] + list(voucher.code_aliases.values_list('code', flat=True)) if voucher.pk else [voucher.code]
    stored = list(VoucherEvent.objects.filter(Q(voucher=voucher) | Q(voucher_code__in=codes, voucher__isnull=True),
                                              business=voucher.business).select_related('user')) if voucher.pk else []
    stored_kinds = {e.event for e in stored}
    for e in stored:
        text = ''
        if e.event == 'extended' and (e.detail.get('added_minutes') or e.detail.get('added_hours')):
            text = '+' + _mtext(e.detail.get('added_minutes') or int(e.detail['added_hours']) * 60)
            if e.detail.get('duration_after'):
                text += f" · now valid for {_mtext(e.detail['duration_after'])} from first login"
            if e.detail.get('ends_after'):
                text += f" · now ends {timezone.localtime(timezone.datetime.fromisoformat(e.detail['ends_after'])):%d %b %Y %H:%M}"
        elif e.event == 'mac_reset' and e.detail.get('devices_removed'):
            text = 'Removed: ' + ', '.join(e.detail['devices_removed'])
        elif e.detail.get('text'):
            text = e.detail['text']
        if e.voucher_code and e.voucher_code.upper() != voucher.code.upper() and e.event != 'code_changed':
            text = (text + ' · ' if text else '') + f'as {e.voucher_code}'
        who = _who(e.user) or {'auto': 'TapTap (automatic)', 'router': 'Router'}.get(e.source, '')
        items.append(_item(e.event, e.created_at, e.get_event_display(), text, who, e.via, e.router_result, e.reason, True))

    # Derived from existing records, so older vouchers have a full history too.
    items.append(_item('imported' if voucher.source == 'mikrotik' else 'created', voucher.created_at,
                       'Imported from the router' if voucher.source == 'mikrotik' else 'Created in TapTap',
                       ' · '.join(x for x in [voucher.plan_name, f'batch {voucher.batch.name}' if voucher.batch_id and voucher.batch else '',
                                              f'for {voucher.customer_name}' if voucher.customer_name else ''] if x)))
    sale = VoucherSale.objects.filter(voucher=voucher).select_related('agent', 'recorded_by').first()
    if sale:
        text = f'{voucher.business.currency}{sale.amount} · {sale.get_payment_method_display()}' + (f' · agent {sale.agent.name}' if sale.agent else '')
        items.append(_item('sold', sale.sold_at, 'Sold', text, _who(sale.recorded_by) or ('TapTap (automatic)' if 'Auto' in (sale.notes or '') else ''), reason=sale.notes))
    elif voucher.sold_at:
        items.append(_item('sold', voucher.sold_at, 'Sold'))
    if voucher.used_at:
        items.append(_item('activated', voucher.used_at, 'First used', 'The clock started on first login'))
    for bnd in voucher.device_bindings.all():
        items.append(_item('device', bnd.first_bound_at, f'Device {bnd.slot_no} connected', bnd.current_mac +
                           (f' (before: {bnd.previous_mac})' if bnd.previous_mac else '')))
    end = ends_at(voucher)
    if end and end <= now:
        items.append(_item('time_up', end, 'Time ran out'))
    if 'enforced' not in stored_kinds:
        for inc in SessionIncident.objects.filter(voucher=voucher, status='fixed').select_related('router', 'fixed_user'):
            items.append(_item('enforced', inc.fixed_at or inc.last_seen, 'Disconnected by enforcement',
                               f'{inc.get_reason_display()} · {inc.mac_address or inc.ip_address} on {inc.router.name}',
                               _who(inc.fixed_user) or ('TapTap (automatic)' if inc.fixed_by == 'auto' else '')))
    # Older activity-log lines that mention this code (before history existed).
    if not stored:
        for a in Activity.objects.filter(business=voucher.business, type__startswith='Voucher', details__icontains=voucher.code)[:50]:
            items.append(_item('legacy', a.created_at, a.type, a.details))

    items.sort(key=lambda x: x['at'] or now, reverse=True)
    return items
