"""Freezing vouchers.

A frozen voucher stops working at once (session disconnected, user disabled on the
router) and its clock stands still. When it is unfrozen it continues from exactly
where it stopped: the time left at the freeze is given back from the moment of the
unfreeze. A voucher that had not been used yet simply keeps its full time.

A *warning* is a freeze with a message: the customer sees a warning page on the
portal and gets their internet back by pressing "I agree" (see views_studio).

Freezing is never a sale, never touches finance, and every freeze / unfreeze is in
the voucher history.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import Router, Voucher

logger = logging.getLogger('taptap.vouchers')

LINK_CHUNK = 50

DEFAULT_WARNING = ('This voucher has been used on more devices than it allows. Sharing or reselling a voucher '
                   'is not permitted. Your internet was paused — your remaining time is kept. '
                   'Press "I agree" to confirm you will use it only on your allowed device(s).')


class FreezeError(ValueError):
    pass


def warning_text(business):
    return (business.shared_warning_text or '').strip() or DEFAULT_WARNING


def time_left_seconds(voucher, now=None):
    """Seconds of use left, or None when the clock has not started yet."""
    from .voucher_history import ends_at
    if voucher.frozen_at:
        return voucher.frozen_left
    end = ends_at(voucher)
    if end is None:
        return None
    return max(0, int((end - (now or timezone.now())).total_seconds()))


def _who(user):
    return user if getattr(user, 'is_authenticated', False) else None


# ─────────────────────────────── router ───────────────────────────────

def _router_set(router, vouchers, disabled, user=None):
    """Disable (and disconnect) or enable hotspot users on one router. Returns (ok, message)."""
    from .voucher_history import channel
    codes = [v.code for v in vouchers]
    via = channel(router)
    if via == 'TapTap Link':
        from .linkops import send
        try:
            for i in range(0, len(codes), LINK_CHUNK):
                part = codes[i:i + LINK_CHUNK]
                send(router, 'hotspot_users_disable', {'names': part, 'disabled': bool(disabled)},
                     label=f'{"Freeze" if disabled else "Unfreeze"} {len(part)} voucher(s)', user=user, minutes=60 * 24)
        except ValueError as exc:
            return False, f'{router.name}: not sent yet ({exc}) — TapTap applies it when the router is back.'
        return True, f'{router.name}: queued — applied at the router\'s next check-in.'
    from .mikrotik import MikroTikService
    try:
        svc = MikroTikService(router).connect()
        try:
            want = {c.upper() for c in codes}
            users = svc.resource('/ip/hotspot/user')
            found = 0
            for row in users.get():
                if str(row.get('name', '')).upper() in want and row.get('id'):
                    users.set(id=row['id'], disabled='yes' if disabled else 'no'); found += 1
            if disabled:
                active = svc.resource('/ip/hotspot/active')
                for row in active.get():
                    if str(row.get('user', '')).upper() in want and row.get('id'):
                        active.remove(id=row['id'])
        finally:
            svc.close()
    except Exception as exc:
        return False, f'{router.name}: not reachable ({exc}). Live sync keeps a frozen voucher offline.'
    missing = len(codes) - found
    return True, f'{router.name}: {found} updated' + (f', {missing} not on the router yet' if missing else '') + '.'


def _apply_on_routers(vouchers, disabled, user=None):
    by_router = {}
    for v in vouchers:
        if v.router_id:
            by_router.setdefault(v.router_id, []).append(v)
    out = []
    for router in Router.objects.filter(pk__in=list(by_router)):
        out.append(_router_set(router, by_router[router.pk], disabled, user=user))
    if not out and vouchers:
        out.append((True, 'No router assigned — changed in TapTap only.'))
    return out


# ─────────────────────────────── freeze / unfreeze ───────────────────────────────

def why_not_freezable(v, now=None):
    from .voucher_history import time_is_up
    if v.deleted_at:
        return 'in the bin'
    if v.frozen_at:
        return 'already frozen'
    if v.status == 'expired' or time_is_up(v, now):
        return 'its time has run out'
    return None


def freeze(vouchers, user=None, reason='', kind='freeze', source='user'):
    """Freeze these vouchers. Returns {'done': n, 'skipped': [(code, why)], 'router': [(ok, msg)]}."""
    from .voucher_history import record, channel
    from .utils import log
    now = timezone.now()
    kind = 'warning' if kind == 'warning' else 'freeze'
    reason = (reason or '').strip()[:255]
    if not reason and source == 'user':
        raise FreezeError('Give a reason — it is kept in the voucher history.')
    out = {'done': 0, 'skipped': [], 'router': []}
    done = []
    with transaction.atomic():
        for v in vouchers:
            why = why_not_freezable(v, now)
            if why:
                out['skipped'].append((v.code, why)); continue
            left = time_left_seconds(v, now)
            before = v.status
            v.frozen_at, v.frozen_by, v.freeze_kind, v.freeze_reason, v.frozen_left = now, _who(user), kind, reason, left
            v.status = 'disabled'
            v.save(update_fields=['frozen_at', 'frozen_by', 'freeze_kind', 'freeze_reason', 'frozen_left', 'status'])
            v._freeze_before = before
            done.append(v)
    out['router'] = _apply_on_routers(done, True, user=user)
    for v in done:
        left = v.frozen_left
        from .durations import text as mtext
        record(v, 'warned' if kind == 'warning' else 'frozen', user=user, source=source, reason=reason, via=channel(v.router),
               router_result='; '.join(m for _, m in out['router'])[:255], status_before=v._freeze_before, status_after='frozen',
               text=('Time left kept: ' + mtext(max(1, left // 60))) if left is not None else 'Not used yet — full time kept')
    out['done'] = len(done)
    if done:
        log(done[0].business, 'Voucher Warned' if kind == 'warning' else 'Voucher Frozen',
            f'{len(done)} voucher(s)' + (f' ({done[0].code})' if len(done) == 1 else '') + (f' — {reason}' if reason else ''))
    return out


def unfreeze(vouchers, user=None, reason='', source='user', event='unfrozen'):
    """Unfreeze: the time left at the freeze continues from now. Returns the same summary as freeze()."""
    from .voucher_history import record, channel
    from .utils import log
    now = timezone.now()
    out = {'done': 0, 'skipped': [], 'router': []}
    done = []
    with transaction.atomic():
        for v in vouchers:
            if not v.frozen_at:
                out['skipped'].append((v.code, 'not frozen')); continue
            paused = now - v.frozen_at
            if v.frozen_left is not None:
                v.expires_at = now + timedelta(seconds=v.frozen_left)
            v._paused, v._kind = paused, v.freeze_kind
            v.status = 'active'
            v.frozen_at, v.frozen_by, v.freeze_kind, v.freeze_reason, v.frozen_left = None, None, '', '', None
            v.save(update_fields=['expires_at', 'status', 'frozen_at', 'frozen_by', 'freeze_kind', 'freeze_reason', 'frozen_left'])
            done.append(v)
    out['router'] = _apply_on_routers(done, False, user=user)
    from .durations import text as mtext
    for v in done:
        mins = int(v._paused.total_seconds() // 60)
        record(v, event, user=user, source=source, reason=(reason or '')[:255], via=channel(v.router),
               router_result='; '.join(m for _, m in out['router'])[:255], status_before='frozen', status_after='active',
               text=f'Paused for {mtext(mins) if mins else "less than a minute"}'
                    + (f' · now ends {timezone.localtime(v.expires_at):%d %b %Y %H:%M}' if v.expires_at else ' · clock starts on first login'))
    out['done'] = len(done)
    if done and source != 'customer':
        log(done[0].business, 'Voucher Unfrozen', f'{len(done)} voucher(s)' + (f' ({done[0].code})' if len(done) == 1 else ''))
    return out


def summary_messages(out, verb):
    """Readable lines for messages.* after freeze()/unfreeze()."""
    lines = []
    if out['done']:
        lines.append(('success', f'{out["done"]} voucher(s) {verb}.'))
    for ok, msg in out['router']:
        lines.append(('info' if ok else 'warning', msg))
    if out['skipped']:
        lines.append(('warning', 'Skipped: ' + ', '.join(f'{c} ({w})' for c, w in out['skipped'][:8])
                      + ('…' if len(out['skipped']) > 8 else '')))
    if not out['done'] and not out['skipped']:
        lines.append(('info', 'Nothing to change.'))
    return lines


def portal_block(voucher):
    """What the portal shows instead of logging in, or None when the voucher is not frozen."""
    if not voucher.frozen_at:
        return None
    from .durations import text as mtext
    left = voucher.frozen_left
    keep = f'Your remaining time ({mtext(max(1, left // 60))}) is kept.' if left else 'Your time is kept.'
    b = voucher.business
    if voucher.freeze_kind == 'warning':
        return {'success': False, 'blocked': True, 'kind': 'warning', 'code': voucher.code, 'can_accept': True,
                'title': 'Warning', 'message': warning_text(b), 'keep': keep,
                'button': 'I agree', 'contact': b.phone or ''}
    return {'success': False, 'blocked': True, 'kind': 'freeze', 'code': voucher.code, 'can_accept': False,
            'title': 'This voucher is paused', 'keep': keep, 'contact': b.phone or '',
            'message': 'Your voucher was paused by the Wi-Fi staff' + (f': {voucher.freeze_reason}' if voucher.freeze_reason else '')
                       + '. Ask staff to unpause it — it will continue from where it stopped.'}
