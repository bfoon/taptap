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
* **MAC changes without the portal** (auto-login by MAC cookie, a router-served page, a phone that
  rotates its private address): when every slot is taken and an unknown MAC logs in, TapTap checks
  whether it is one of the locked phones that just changed address. The slot moves to the new MAC
  only if that locked device's MAC is **offline** right now, and either
    - its Wi-Fi name (DHCP host name) is the same — phones keep their name when the MAC changes, or
    - the new MAC is a private (randomised) address and the voucher has not moved more than
      ``SWAP_PER_DAY`` times in 24 hours.
  Anything else is a different device and is refused, so a customer whose phone changes its MAC
  keeps working, while a second phone using the same code is still disconnected.
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
SWAP_PER_DAY = 3          # private-MAC changes allowed per voucher in 24 h without a matching host name
KICK_EVERY = 20           # seconds: a refused device is disconnected again at most this often
GENERIC_HOSTS = {'', 'android', 'iphone', 'ipad', 'localhost', 'unknown', 'espressif', 'esp32', 'galaxy', 'samsung',
                 'android-phone', 'phone', 'mobile', 'laptop', 'desktop', 'pc'}


def is_private_mac(mac):
    """Phones' privacy MACs set the 'locally administered' bit (second hex digit 2, 6, A or E)."""
    m = norm_mac(mac)
    return bool(m) and int(m[1], 16) & 2 == 2


def _host(router_id, mac):
    if not router_id or not mac:
        return ''
    from .models import RouterDevice
    h = RouterDevice.objects.filter(router_id=router_id, mac_address__iexact=mac).exclude(hostname='').values_list('hostname', flat=True).first()
    h = str(h or '').strip().lower()
    return '' if h in GENERIC_HOSTS or re.fullmatch(r'android-[0-9a-f]{6,}', h or '') else h


def _swaps_today(voucher):
    from datetime import timedelta
    from .models import VoucherEvent
    return VoucherEvent.objects.filter(voucher=voucher, event='device', detail__kind='mac_swap',
                                       created_at__gte=timezone.now() - timedelta(hours=24)).count()


def _same_phone(voucher, rows, mac, hints):
    """Which locked device is this unknown MAC most likely to be (a MAC change), or None.
    Only with live data (the live pass knows which MACs are online right now). Without it — e.g. a login
    page that sent no device ID — nothing is assumed offline and the device is refused as before."""
    if not hints or 'online_macs' not in hints:
        return None, ''
    online = {norm_mac(m) for m in hints.get('online_macs', ())}
    router_id = (hints or {}).get('router_id') or voucher.router_id
    away = [b for b in rows if b.current_mac and b.current_mac not in online]     # its old MAC is offline now
    if not away:
        return None, ''
    new_host = _host(router_id, mac)
    if new_host:
        for b in away:
            if _host(router_id, b.current_mac) == new_host:
                return b, 'same Wi-Fi name “%s”' % new_host
    if is_private_mac(mac) and _swaps_today(voucher) < SWAP_PER_DAY:
        away.sort(key=lambda b: b.last_seen_at or b.first_bound_at)
        return away[0], 'private MAC address'
    return None, ''


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


def claim(voucher, mac='', fp='', source='portal', label='', hints=None):
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
        if hit and not (fp and hit.device_token_hash == fp) and mac and hit.previous_mac == mac and hit.current_mac != mac \
                and hit.current_mac in {norm_mac(m) for m in (hints or {}).get('online_macs', ())}:
            # Its OLD MAC while its current MAC is online: two phones taking turns on one slot. Refuse.
            n = slots(voucher)
            return Outcome('denied', None, f'This voucher is already in use on {"another device" if n == 1 else f"its {n} devices"}. '
                                           'Ask the staff to reset it if you changed your phone.')
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
        if len(rows) >= slots(voucher) and mac and not fp:
            # No portal device ID: is this a locked phone that changed its MAC? (see module notes)
            same, why = _same_phone(voucher, rows, mac, hints)
            if same is not None:
                old = same.current_mac
                same.previous_mac, same.current_mac, same.last_seen_at = old, mac, timezone.now()
                same.save(update_fields=['previous_mac', 'current_mac', 'last_seen_at'])
                moved = same
            else:
                moved = None
        else:
            moved = None
        if moved is not None:
            from .voucher_history import record
            record(voucher, 'device', source='auto', kind='mac_swap',
                   text=f'{moved.label or "Locked device"} changed its MAC {moved.previous_mac} → {mac} ({why}) — slot {moved.slot_no} follows it')
            if slots(voucher) == 1:
                _router_mac(voucher, mac)
            _forget_mac(voucher, moved.previous_mac)        # the old MAC cannot log back in by cookie
            return Outcome('moved', moved)
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


def _forget_mac(voucher, mac):
    """Remove the hotspot cookie (and any session) of a locked phone's previous MAC after it changed MAC."""
    router, mac = voucher.router, norm_mac(mac)
    if not router or not mac:
        return
    try:
        from .voucher_history import channel
        if channel(router) == 'TapTap Link':
            from .linkops import send
            send(router, 'hotspot_kick', {'user': voucher.code, 'mac': mac}, label=f'Forget old MAC of {voucher.code}', minutes=15)
            return
        from .mikrotik import MikroTikService
        svc = MikroTikService(router).connect()
        try:
            for path, field in (('/ip/hotspot/active', 'mac-address'), ('/ip/hotspot/cookie', 'mac-address')):
                res = svc.resource(path)
                for row in res.get(user=voucher.code):
                    if norm_mac(row.get(field)) == mac:
                        res.remove(id=row['id'])
        finally:
            svc.close()
    except Exception as exc:
        logger.info('forget old MAC %s of %s: %s', mac, voucher.code, exc)


def unlock_on_router(voucher):
    _router_mac(voucher, '')


def kick(router, voucher, mac, session_id='', svc=None):
    """Disconnect one foreign device from a voucher (keeps the locked devices online)."""
    key = f'lock:kick:{router.pk}:{voucher.pk}:{mac}'
    if cache.get(key):
        return False
    cache.set(key, 1, KICK_EVERY)       # every live pass, so a refused phone cannot sit on the voucher
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
    online = {}
    for v, s in voucher_rows:     # every MAC online on each voucher right now
        online.setdefault(v.pk, set()).add(norm_mac(s.get('mac-address')))
    for v, s in voucher_rows:
        mac = norm_mac(s.get('mac-address'))
        if not mac or not enabled(v) or v.frozen_at or v.status != 'active':
            continue
        out = claim(v, mac=mac, source='router', hints={'online_macs': online.get(v.pk, set()) - {mac}, 'router_id': router.pk})
        if out.status == 'denied' and kick(router, v, mac, str(s.get('id', '')), svc=svc):
            kicked += 1
    return kicked


def portal_block(voucher, message):
    return {'success': False, 'blocked': True, 'kind': 'locked', 'code': voucher.code, 'can_accept': False,
            'title': 'Voucher in use on another device', 'message': message, 'contact': voucher.business.phone or '', 'keep': ''}
