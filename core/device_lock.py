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
* The router itself is NOT pinned to a MAC any more (that refused phones that changed address
  before TapTap could follow); TapTap enforces the lock.
* **Ghost sessions:** a session that moved no data for LIVE_SECONDS is treated as gone — so a
  phone that returns with a new random MAC is never kicked because of its own old session, and
  a device is only disconnected when it is in use at the same time as the locked one.
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
SWAP_PER_DAY = 6          # private-MAC changes allowed per voucher in 24 h without a matching host name
LIVE_BYTES = 3000         # a session must move at least this much data between readings to count as "in use"
LIVE_SECONDS = 90         # …within this many seconds; a session silent for longer is a ghost (phone left / changed MAC)
STRIKES_TO_KICK = 2       # a refused device is disconnected only when seen in use alongside the locked device twice in a row
KICK_EVERY = 20           # seconds: a refused device is disconnected again at most this often
GENERIC_HOSTS = {'', 'android', 'iphone', 'ipad', 'localhost', 'unknown', 'espressif', 'esp32', 'galaxy', 'samsung',
                 'android-phone', 'phone', 'mobile', 'laptop', 'desktop', 'pc'}


# ─────────────────────── liveness: is a session really in use, or a ghost? ───────────────────────
# Sticky sessions keep a phone's session on the router for a while after the phone left. When the phone
# comes back with a new (random) MAC, the old session is still listed — and used to make TapTap think two
# devices were on the voucher, so the returning phone was kicked. TapTap now watches each session's data:
# a session that moved no data for LIVE_SECONDS is a ghost, not a device in use.

def note_activity(router_id, mac, total_bytes, now_ts):
    key = f'lock:act:{router_id}:{mac}'
    rec = cache.get(key)
    total = int(total_bytes or 0)
    if not rec or total < rec['b'] or total - rec['b'] >= LIVE_BYTES:
        rec = {'b': total, 't': now_ts, 'seen': now_ts}        # first sight, new session or real traffic
    else:
        rec['seen'] = now_ts
    cache.set(key, rec, 3600)
    return rec


def is_live(router_id, mac, now_ts=None):
    """True when this MAC moved data recently (a device actually in use)."""
    import time as _t
    now_ts = now_ts or _t.time()
    rec = cache.get(f'lock:act:{router_id}:{norm_mac(mac)}')
    return bool(rec) and now_ts - rec['t'] <= LIVE_SECONDS and now_ts - rec['seen'] <= 3 * LIVE_SECONDS


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
            return Outcome(status, hit)
        if len(rows) >= slots(voucher) and mac:
            # Unknown MAC (and no matching device ID — e.g. the phone's captive-portal window, which keeps
            # its own storage): is this a locked phone that changed its MAC? (see module notes)
            same, why = _same_phone(voucher, rows, mac, hints)
            if same is not None:
                old = same.current_mac
                same.previous_mac, same.current_mac, same.last_seen_at = old, mac, timezone.now()
                fields = ['previous_mac', 'current_mac', 'last_seen_at']
                if fp:
                    same.device_token_hash = fp; fields.append('device_token_hash')
                same.save(update_fields=fields)
                moved = same
            else:
                moved = None
        else:
            moved = None
        if moved is not None:
            from .voucher_history import record
            record(voucher, 'device', source='auto', kind='mac_swap',
                   text=f'{moved.label or "Locked device"} changed its MAC {moved.previous_mac} → {mac} ({why}) — slot {moved.slot_no} follows it')
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
        try:            # in the background: a live pass or a login page must never wait on a router
            from .tasks import forget_mac_task
            forget_mac_task.delay(voucher.pk, mac)
            return
        except Exception:
            pass
        forget_mac_now(voucher, mac)
    except Exception as exc:
        logger.info('forget old MAC %s of %s: %s', mac, voucher.code, exc)


def forget_mac_now(voucher, mac):
    router = voucher.router
    try:
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


def kick(router, voucher, mac, session_id='', svc=None, quiet=False):
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
    if quiet:
        record(voucher, 'device', source='auto', text=f'Old session {mac} removed so the locked device can log in again')
        return True
    record(voucher, 'enforced', source='auto', reason='Device not allowed',
           text=f'{mac} was disconnected — this voucher is locked to {"another device" if slots(voucher) == 1 else f"its {slots(voucher)} devices"}')
    return True


def enforce_sessions(router, voucher_rows, svc=None):
    """Live sync: lock new devices, follow phones that changed MAC, disconnect real extra devices.
    voucher_rows = [(voucher, session_row)].

    * Every session's data is watched: one that moved nothing for LIVE_SECONDS is a ghost.
    * Ghosts never count as "a device in use", so a returning phone with a new MAC takes over its slot
      (and the ghost is removed from the router so it does not block the login).
    * A refused device is disconnected only when it is in use at the same time as the locked device on
      STRIKES_TO_KICK readings in a row — a phone switching MAC is never caught mid-switch."""
    import time as _t
    now_ts = _t.time()
    kicked = 0
    live, ghosts = {}, {}
    for v, s in voucher_rows:
        mac = norm_mac(s.get('mac-address'))
        if not mac:
            continue
        note_activity(router.pk, mac, int(s.get('bytes-in') or 0) + int(s.get('bytes-out') or 0), now_ts)
    for v, s in voucher_rows:
        mac = norm_mac(s.get('mac-address'))
        if mac:
            (live if is_live(router.pk, mac, now_ts) else ghosts).setdefault(v.pk, set()).add(mac)
    for v, s in voucher_rows:
        mac = norm_mac(s.get('mac-address'))
        if not mac or not enabled(v) or v.frozen_at or v.status != 'active':
            continue
        if mac in ghosts.get(v.pk, set()):
            continue          # a ghost is not a device in use: it must not take (or take back) a slot
        out = claim(v, mac=mac, source='router', hints={'online_macs': live.get(v.pk, set()) - {mac}, 'router_id': router.pk})
        if out.status == 'moved':
            continue          # claim() already removed the ghost's session and cookie (_forget_mac)
        if out.status != 'denied':
            cache.delete(f'lock:strike:{v.pk}:{mac}')
            continue
        locked_live = {norm_mac(m) for m in v.device_bindings.values_list('current_mac', flat=True)} & live.get(v.pk, set())
        if not is_live(router.pk, mac, now_ts) or not locked_live:
            continue          # not two devices in use at once (a ghost, or the locked phone is away): wait
        key = f'lock:strike:{v.pk}:{mac}'
        strikes = (cache.get(key) or 0) + 1
        cache.set(key, strikes, 300)
        if strikes >= STRIKES_TO_KICK and kick(router, v, mac, str(s.get('id', '')), svc=svc):
            cache.delete(key)
            kicked += 1
    return kicked


def portal_block(voucher, message):
    return {'success': False, 'blocked': True, 'kind': 'locked', 'code': voucher.code, 'can_accept': False,
            'title': 'Voucher in use on another device', 'message': message, 'contact': voucher.business.phone or '', 'keep': ''}


# ─────────────────────── smooth re-login (no "router lock", no stale session) ───────────────────────
# The router no longer pins a voucher to one MAC (a phone that came back with a new MAC address was
# refused by the router — "invalid username" — before TapTap could follow it). TapTap enforces the
# lock itself: the portal refuses other devices and live sync removes any that get on.

def online_hints(voucher):
    """Live data for claim(): the MACs this voucher has online on its router right now."""
    if not voucher.router_id:
        return None
    snap = cache.get(f'tt:tr:users:{voucher.router_id}')
    if not snap:
        return None
    rows = (snap.get('users') or {}).get(voucher.code.upper(), [])
    # a session that moved no data in the last reading is a ghost (the phone left or changed MAC)
    live = [r.get('mac') for r in rows if r.get('mac') and (is_live(voucher.router_id, r.get('mac'))
                                                          or (r.get('down_bps') or 0) + (r.get('up_bps') or 0) > 2000)]
    return {'online_macs': live, 'router_id': voucher.router_id}


def release_stale(voucher, mac):
    """The locked device is logging in again: remove this voucher's old sessions that would block it
    (a ghost of the same phone with its old MAC, kept by the router after the phone left — the router
    then answers "no more sessions are allowed for user"). Returns seconds the login page should wait
    (TapTap Link applies it at the next check-in)."""
    mac = norm_mac(mac)
    if not voucher.router_id or not mac:
        return 0
    snap = cache.get(f'tt:tr:users:{voucher.router_id}') or {}
    sessions = (snap.get('users') or {}).get(voucher.code.upper(), [])
    keep = {norm_mac(m) for m in voucher.device_bindings.values_list('current_mac', flat=True)} - {mac}
    stale = [norm_mac(r.get('mac')) for r in sessions
             if norm_mac(r.get('mac')) and norm_mac(r.get('mac')) != mac and norm_mac(r.get('mac')) not in keep]
    if not stale or len(sessions) < slots(voucher):
        return 0                                  # a free session slot: nothing in the way
    from .voucher_history import channel
    for old in stale:
        cache.delete(f'lock:kick:{voucher.router_id}:{voucher.pk}:{old}')
        kick(voucher.router, voucher, old, quiet=True)
    return 15 if channel(voucher.router) == 'TapTap Link' else 0


def unlock_router(router):
    """Remove the per-MAC lock older TapTap versions put on hotspot users (once per router)."""
    key = f'tt:maclock:cleared:{router.pk}'
    if cache.get(key):
        return False
    from .voucher_history import channel
    try:
        if channel(router) == 'TapTap Link':
            from .linkops import send
            send(router, 'hotspot_mac_unlock_all', {}, label='Let locked vouchers log in again from a new MAC (TapTap checks the device)', minutes=60 * 24)
        else:
            from .mikrotik import MikroTikService
            svc = MikroTikService(router).connect()
            try:
                users = svc.resource('/ip/hotspot/user')
                for row in users.get():
                    if norm_mac(row.get('mac-address')) and str(row.get('comment', '')).startswith('TapTap') and row.get('id'):
                        users.set(id=row['id'], mac_address=ANY_MAC)
            finally:
                svc.close()
    except Exception as exc:
        logger.info('unlock %s: %s', router, exc)
        return False
    cache.set(key, 1, 86400 * 365)
    return True
