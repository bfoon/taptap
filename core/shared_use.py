"""Vouchers used on more devices than they allow.

Detection: the portal identifies each phone/laptop by its device signature (stable even
when the phone randomises its MAC). A voucher seen on more signatures than its
``max_devices`` is being shared.

Each case is decided once, and that decision remembers the devices known at that moment.
The voucher only becomes a new warning when a device appears that was not part of an
earlier decision. Decisions: allow, send a warning, reset devices, freeze, disable.

Warnings can be manual (you press Warn) or automatic (Business.shared_warning_mode).
A warning pauses the internet and the clock; the customer sees the warning page on the
portal and continues by pressing "I agree".
"""
from __future__ import annotations

import logging

from .models import DeviceSignature, SharedUseReview

logger = logging.getLogger('taptap.shared')

ACTIONS = {'allow': 'allowed', 'warn': 'warned', 'reset': 'reset', 'freeze': 'frozen', 'disable': 'disabled'}


class SharedError(ValueError):
    pass


def _signatures(business, code=None):
    qs = business.device_signatures.exclude(vouchers=[]).only('id', 'vouchers', 'model', 'os', 'label', 'fingerprint', 'last_seen', 'last_mac')
    seen = {}
    for sig in qs:
        for c in sig.vouchers or []:
            c = str(c).upper()
            if code is None or c == code.upper():
                seen.setdefault(c, []).append(sig)
    return seen


def cases(business, include_resolved=True, limit=200):
    """[{voucher, code, sigs, devices, allowed, open, new, review}] — open cases first."""
    seen = _signatures(business)
    if not seen:
        return []
    vouchers = {v.code.upper(): v for v in business.vouchers.filter(code__in=list(seen.keys())).select_related('router')}
    latest = {}
    for r in SharedUseReview.objects.filter(business=business, voucher__in=list(vouchers.values())).select_related('by'):
        latest.setdefault(r.voucher_id, r)          # ordering is newest first
    out = []
    for code, sigs in seen.items():
        v = vouchers.get(code)
        if not v:
            continue
        allowed = v.max_devices or 1
        if len(sigs) <= allowed:
            continue
        review = latest.get(v.pk)
        known = set(review.fingerprints) if review else set()
        new = [s for s in sigs if s.fingerprint not in known]
        is_open = bool(new) and not (v.frozen_at and v.freeze_kind == 'warning')
        if not is_open and not include_resolved:
            continue
        out.append({'voucher': v, 'code': v.code, 'sigs': sigs, 'devices': len(sigs), 'allowed': allowed,
                    'open': is_open, 'new': new, 'review': review,
                    'waiting': bool(v.frozen_at and v.freeze_kind == 'warning')})
    out.sort(key=lambda c: (not c['open'], not c['waiting'], -c['devices']))
    return out[:limit]


def case_for(voucher):
    for c in cases(voucher.business):
        if c['voucher'].pk == voucher.pk:
            return c
    return None


def open_count(business):
    return sum(1 for c in cases(business, include_resolved=False))


def _review(voucher, action, user=None, note='', auto=False):
    sigs = _signatures(voucher.business, voucher.code).get(voucher.code.upper(), [])
    return SharedUseReview.objects.create(business=voucher.business, voucher=voucher, code=voucher.code,
                                          fingerprints=[s.fingerprint for s in sigs], devices=len(sigs),
                                          allowed=voucher.max_devices or 1, action=action, note=(note or '')[:255], auto=auto,
                                          by=user if getattr(user, 'is_authenticated', False) else None)


def resolve(voucher, action, user=None, note=''):
    """Decide a case. Returns list of (level, message)."""
    from . import voucher_freeze as vf
    from . import voucher_history as vh
    if action not in ACTIONS:
        raise SharedError('Unknown action.')
    note = (note or '').strip()
    msgs = []
    c = case_for(voucher)
    what = f'Seen on {c["devices"]} devices, plan allows {c["allowed"]}' if c else 'Shared use'
    if action == 'allow':
        _review(voucher, 'allowed', user, note)
        vh.record(voucher, 'shared_resolved', user=user, reason=note, text=f'{what} — allowed')
        msgs.append(('success', f'{voucher.code}: allowed. It shows up again only if another new device uses it.'))
    elif action == 'warn':
        if voucher.frozen_at:
            raise SharedError(f'{voucher.code} is already frozen or warned.')
        out = vf.freeze([voucher], user=user, reason=note or what, kind='warning')
        _review(voucher, 'warned', user, note)
        msgs += vf.summary_messages(out, 'warned — the customer must accept the warning to continue')
    elif action == 'reset':
        ok, result = vh.reset_devices(voucher, user=user, reason=note or what)
        forget(voucher)
        _review(voucher, 'reset', user, note)
        msgs.append(('success' if ok else 'warning', f'{voucher.code}: devices reset — the next devices to log in are counted fresh. {result}'))
    elif action == 'freeze':
        if voucher.frozen_at:
            raise SharedError(f'{voucher.code} is already frozen.')
        out = vf.freeze([voucher], user=user, reason=note or what, kind='freeze')
        _review(voucher, 'frozen', user, note)
        msgs += vf.summary_messages(out, 'frozen')
    elif action == 'disable':
        try:
            ok, result = vh.disable(voucher, user=user, reason=note or what)
        except vh.VoucherActionError as exc:
            raise SharedError(str(exc))
        _review(voucher, 'disabled', user, note)
        msgs.append(('success' if ok else 'warning', f'{voucher.code}: disabled. {result}'))
    return msgs


def forget(voucher):
    """Remove the voucher from every device signature, so devices are counted fresh."""
    code = voucher.code.upper()
    for sig in DeviceSignature.objects.filter(business=voucher.business).exclude(vouchers=[]):
        if any(str(c).upper() == code for c in sig.vouchers or []):
            sig.vouchers = [c for c in sig.vouchers if str(c).upper() != code]
            sig.save(update_fields=['vouchers'])


def customer_accepted(voucher, fingerprint=''):
    """The customer pressed "I agree" on the warning page."""
    from . import voucher_freeze as vf
    if not (voucher.frozen_at and voucher.freeze_kind == 'warning'):
        return None
    out = vf.unfreeze([voucher], source='customer', reason='Accepted on the portal', event='warning_accepted')
    _review(voucher, 'accepted', note=f'Accepted on device {fingerprint[:12]}' if fingerprint else 'Accepted on the portal')
    return out


def check_after_login(business, voucher):
    """Called after the portal recorded a device for this voucher. Warns automatically
    (if switched on) and notifies once per new case. Returns True when the voucher was warned now."""
    try:
        sigs = _signatures(business, voucher.code).get(voucher.code.upper(), [])
        allowed = voucher.max_devices or 1
        if len(sigs) <= allowed or voucher.frozen_at:
            return False
        review = SharedUseReview.objects.filter(voucher=voucher).first()
        known = set(review.fingerprints) if review else set()
        new = [s for s in sigs if s.fingerprint not in known]
        if not new:
            return False
        from .notify import notify
        auto = business.shared_warning_mode == 'auto'
        what = f'{voucher.code} was used on {len(sigs)} devices; its plan allows {allowed}.'
        if auto:
            from . import voucher_freeze as vf
            vf.freeze([voucher], reason=f'Automatic: seen on {len(sigs)} devices, plan allows {allowed}', kind='warning', source='auto')
            _review(voucher, 'warned', note='Automatic warning', auto=True)
        notify(business, 'voucher_shared', f'Voucher {voucher.code} used on {len(sigs)} devices',
               what + (' Its internet was paused until the customer accepts the warning.' if auto else ' Open Devices to decide what to do.'),
               link='/devices/?view=shared', key=f'shared:{voucher.pk}:{len(sigs)}')
        return auto
    except Exception:
        logger.exception('Shared-use check failed for %s', voucher.code)
        return False
