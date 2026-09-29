"""Sticky vouchers — each voucher is locked to the devices that use it.

* The first device that uses a voucher takes slot 1; a voucher for N devices locks the first N
  different devices, one slot each, and the slots stay taken for good.
* A device is recognised by its device ID from the portal (it stays the same when a phone
  changes its MAC address) or by its MAC address. When a known device shows up with a new
  MAC, its slot follows it.
* Once every slot is taken, any other device is refused on the portal and, if it still gets
  on, disconnected by live sync at once.
* Only a person with voucher-support rights (owner, admin, voucher support) can free the
  slots, with "Reset devices" on the voucher.
* One-device vouchers are also locked on the router itself (the hotspot user's mac-address),
  so the router refuses other devices even when TapTap cannot be reached.
"""
from __future__ import annotations

import logging
import re

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger('taptap.devicelock')

MAC_RE = re.compile(r'^([0-9A-F]{2}:){5}[0-9A-F]{2}$')
ANY_MAC = '00:00:00:00:00:00'


def norm_mac(mac):
    m = re.sub(r'[^0-9A-Fa-f]', '', str(mac or '')).upper()
    if len(m) != 12:
        return ''
    m = ':'.join(m[i:i + 2] for i in range(0, 12, 2))
    return '' if m == ANY_MAC else m


def slots(voucher):
    return max(1, int(voucher.max_devices or 1))


def enabled(voucher):
    return bool(getattr(voucher.business, 'device_lock', True))


def summary(voucher):
    used = voucher.device_bindings.count()
    return {'used': used, 'total': slots(voucher), 'free': max(0, slots(voucher) - used)}


def _label(business, fp):
    if not fp:
        return ''
    from .models import DeviceSignature
    sig = DeviceSignature.objects.filter(business=business, fingerprint=fp).only('label', 'model', 'os', 'device_type').first()
    if not sig:
        return ''
    return (sig.label or sig.model or ' '.join(x for x in (sig.os, sig.device_type) if x)).strip()[:120]


class Outcome:
    def __init__(self, status, binding=None, message=''):
        self.status, self.binding, self.message = status, binding, message

    @property
    def allowed(self):
        return self.status != 'denied'

    def __repr__(self):
        return f'<Outcome {self.status}>'


def claim(voucher, mac='', fp='', source='portal', label=''):
    """Is this device allowed on the voucher? Takes a free slot when there is one.
    status: 'known' (already locked to it), 'moved' (known device, new MAC), 'new' (slot taken now),
    'denied' (all slots belong to other devices), 'off' (locking switched off)."""
    from .models import Voucher, VoucherDeviceBinding
    if not enabled(voucher):
        return Outcome('off')
    mac, fp = norm_mac(mac), (fp or '').strip()[:128]
    if not mac and not fp:
        return Outcome('off')
    with transaction.atomic():
        Voucher.objects.select_for_update().filter(pk=voucher.pk).first()   # one claim at a time per voucher
        rows = list(VoucherDeviceBinding.objects.filter(voucher=voucher).order_by('slot_no'))
        hit = next((b for b in rows if fp and b.device_token_hash == fp), None) or \
            next((b for b in rows if mac and (b.current_mac == mac or b.previous_mac == mac)), None)
        if hit:
            changed, status = [], 'known'
            if mac and hit.current_mac != mac:
                if hit.current_mac:
                    hit.previous_mac = hit.current_mac; changed.append('previous_mac')
                hit.current_mac = mac; changed.append('current_mac'); status = 'moved' if hit.previous_mac else 'known'
            if fp and not hit.device_token_hash:
                hit.device_token_hash = fp; changed.append('device_token_hash')
            if not hit.label and (label or fp):
                hit.label = label or _label(voucher.business, fp)
                if hit.label: changed.append('label')
            hit.last_seen_at = timezone.now(); changed.append('last_seen_at')
            hit.save(update_fields=changed)
            if status == 'moved' and slots(voucher) == 1:
                _router_mac(voucher, mac)
            return Outcome(status, hit)
        if len(rows) >= slots(voucher):
            n = slots(voucher)
            return Outcome('denied', None, f'This voucher is already in use on {"another device" if n == 1 else f"its {n} devices"}. '
                                           'Ask the staff to reset it if you changed your phone.')
        taken = {b.slot_no for b in rows}
        slot = next(i for i in range(1, slots(voucher) + 1) if i not in taken)
        b = VoucherDeviceBinding.objects.create(business=voucher.business, voucher=voucher, slot_no=slot, device_token_hash=fp,
                                                current_mac=mac, label=label or _label(voucher.business, fp), locked_by=source)
    from .voucher_history import record
    what = b.label or mac or 'a device'
    record(voucher, 'device', source='auto', text=f'Locked to {what}' + (f' ({mac})' if mac and b.label else '') +
           f' — device {slot} of {slots(voucher)}')
    if slots(voucher) == 1 and mac:
        _router_mac(voucher, mac)
    return Outcome('new', b)


def release_all(voucher):
    """Free every slot (Reset devices). Returns the MACs that were locked."""
    macs = [m for m in voucher.device_bindings.values_list('current_mac', flat=True) if m]
    voucher.device_bindings.all().delete()
    return macs


# ─────────────────────────────── router side ───────────────────────────────

def _router_mac(voucher, mac):
    """Lock a one-device voucher to this MAC on the router itself ('' = unlock)."""
    router = voucher.router
    if not router:
        return
    from .voucher_history import channel
    try:
        if channel(router) == 'TapTap Link':
            from .linkops import send
            send(router, 'hotspot_user_mac', {'name': voucher.code, 'mac': mac or ANY_MAC},
                 label=f'{"Lock" if mac else "Unlock"} {voucher.code} {"to " + mac if mac else ""}'.strip(), minutes=60 * 24)
            return
        from .mikrotik import MikroTikService
        svc = MikroTikService(router).connect()
        try:
            users = svc.resource('/ip/hotspot/user')
            row = users.get(name=voucher.code)
            if row:
                users.set(id=row[0]['id'], mac_address=mac or ANY_MAC)
            if not mac:
                cookies = svc.resource('/ip/hotspot/cookie')
                for c in cookies.get(user=voucher.code):
                    cookies.remove(id=c['id'])
        finally:
            svc.close()
    except Exception as exc:
        logger.info('router MAC lock for %s: %s', voucher.code, exc)


def unlock_on_router(voucher):
    _router_mac(voucher, '')


def kick(router, voucher, mac, session_id='', svc=None):
    """Disconnect one foreign device from a voucher (keeps the locked devices online)."""
    key = f'lock:kick:{router.pk}:{voucher.pk}:{mac}'
    if cache.get(key):
        return False
    cache.set(key, 1, 90)
    try:
        from .voucher_history import channel
        if svc is None and channel(router) == 'TapTap Link':
            from .linkops import send
            send(router, 'hotspot_kick', {'user': voucher.code, 'mac': mac}, label=f'Remove {mac} from {voucher.code} (locked voucher)', minutes=30)
        else:
            own = svc is None
            if own:
                from .mikrotik import MikroTikService
                svc = MikroTikService(router).connect()
            try:
                act = svc.resource('/ip/hotspot/active')
                for row in act.get(user=voucher.code):
                    if norm_mac(row.get('mac-address')) == mac:
                        act.remove(id=row['id'])
                cookies = svc.resource('/ip/hotspot/cookie')
                for c in cookies.get(user=voucher.code):
                    if norm_mac(c.get('mac-address')) == mac:
                        cookies.remove(id=c['id'])
            finally:
                if own:
                    svc.close()
    except Exception as exc:
        logger.info('kick %s from %s: %s', mac, voucher.code, exc)
        return False
    from .voucher_history import record
    record(voucher, 'enforced', source='auto', reason='Device not allowed',
           text=f'{mac} was disconnected — this voucher is locked to {"another device" if slots(voucher) == 1 else f"its {slots(voucher)} devices"}')
    return True


def enforce_sessions(router, voucher_rows, svc=None):
    """Live sync: lock new devices and disconnect foreign ones. voucher_rows = [(voucher, session_row)]."""
    kicked = 0
    for v, s in voucher_rows:
        mac = norm_mac(s.get('mac-address'))
        if not mac or not enabled(v) or v.frozen_at or v.status != 'active':
            continue
        out = claim(v, mac=mac, source='router')
        if out.status == 'denied' and kick(router, v, mac, str(s.get('id', '')), svc=svc):
            kicked += 1
    return kicked


def portal_block(voucher, message):
    return {'success': False, 'blocked': True, 'kind': 'locked', 'code': voucher.code, 'can_accept': False,
            'title': 'Voucher in use on another device', 'message': message, 'contact': voucher.business.phone or '', 'keep': ''}
