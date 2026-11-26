"""Flag and blacklist devices.

* **Flag** — mark a device (Watch / Suspicious / Dangerous) with a note; optionally be told whenever it
  enters a voucher or comes online.
* **Blacklist** — the device cannot use your Wi-Fi until you remove it:
    - every MAC TapTap knows for it is blocked on **every** router (HotSpot IP binding "blocked") and its
      sessions are dropped;
    - if it shows up again — with a new random MAC, on another router, or by typing a voucher — TapTap
      recognises it (device ID from the login page, any of its MACs), refuses the voucher with a clear
      message, blocks the new MAC too, and tells you (bell + email).
"""
from __future__ import annotations

import logging

from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from .device_block import norm

logger = logging.getLogger('taptap.blacklist')

LEVELS = {'watch': ('Watch', '#1769e0', '👁️'), 'suspect': ('Suspicious', '#d97706', '⚠️'), 'danger': ('Dangerous', '#dc2626', '🚩')}


def macs_of(sig):
    return sorted({norm(m) for m in list(sig.macs or []) + [sig.last_mac] if norm(m)})


def _index(business):
    """{'mac': {MAC: sig_id}, 'fp': {fp: sig_id}} of blacklisted devices (cached a minute)."""
    key = f'tt:blk:idx:{business.pk}'
    idx = cache.get(key)
    if idx is None:
        idx = {'mac': {}, 'fp': {}}
        for sig in business.device_signatures.filter(blacklisted_at__isnull=False).only('pk', 'fingerprint', 'macs', 'last_mac'):
            idx['fp'][sig.fingerprint] = sig.pk
            for m in macs_of(sig):
                idx['mac'][m] = sig.pk
        cache.set(key, idx, 60)
    return idx


def _forget_index(business):
    cache.delete(f'tt:blk:idx:{business.pk}')


def find(business, mac='', fp=''):
    """The blacklisted device this MAC / device ID belongs to, or None."""
    idx = _index(business)
    pk = idx['mac'].get(norm(mac)) or (idx['fp'].get(fp) if fp else None)
    return business.device_signatures.filter(pk=pk).first() if pk else None


def message(business):
    phone = business.support_phone or business.phone
    return ('This device is blocked on our Wi-Fi.' + (f' Please contact us on {phone}.' if phone else ' Please contact the shop.'))


# ─────────────────────────────── actions ───────────────────────────────

def blacklist(sig, user=None, reason=''):
    from .device_block import block
    from .utils import log
    sig.blacklisted_at, sig.blacklisted_by, sig.blacklist_reason = timezone.now(), user, (reason or '').strip()[:255]
    sig.save(update_fields=['blacklisted_at', 'blacklisted_by', 'blacklist_reason'])
    _forget_index(sig.business)
    label = sig.label or sig.model or sig.os or 'device'
    result = 'No MAC known yet — it is refused as soon as it uses a voucher.'
    if macs_of(sig):
        try:
            result = block(None, macs_of(sig), label=label, user=user, business=sig.business, why='blacklisted')
        except ValueError as exc:
            result = str(exc)
    log(sig.business, 'Device Blacklisted', f'{label} ({", ".join(macs_of(sig)) or "no MAC"}) — {reason or "no reason given"}. {result}')
    return result


def unblacklist(sig, user=None):
    from .device_block import unblock
    from .utils import log
    macs = macs_of(sig)
    sig.blacklisted_at, sig.blacklisted_by, sig.blacklist_reason = None, None, ''
    sig.save(update_fields=['blacklisted_at', 'blacklisted_by', 'blacklist_reason'])
    _forget_index(sig.business)
    n = 0
    if macs:
        try:
            n = unblock(None, macs, label=sig.label or sig.model or 'device', user=user, business=sig.business)
        except ValueError as exc:
            logger.info('unblacklist %s: %s', sig.pk, exc)
    log(sig.business, 'Device Removed From Blacklist', f'{sig.label or sig.model or "device"} can use the Wi-Fi again ({n} block(s) removed)')
    return n


def flag(sig, level, note='', notify=False, user=None):
    sig.flag_level = level if level in LEVELS else ''
    sig.flagged = bool(sig.flag_level)
    sig.flag_notify = bool(notify) and sig.flagged
    sig.note = (note or sig.note or '')[:255]
    sig.flagged_at, sig.flagged_by = (timezone.now(), user) if sig.flagged else (None, None)
    sig.save(update_fields=['flag_level', 'flagged', 'flag_notify', 'note', 'flagged_at', 'flagged_by'])


# ─────────────────────────────── when it shows up ───────────────────────────────

def hit(sig, *, mac='', fp='', voucher=None, where='login page', router=None):
    """A blacklisted device tried to get on: count it, learn and block a new MAC, tell the team."""
    from .device_block import MAC_RE, block
    mac = norm(mac)
    sig.blacklist_hits += 1
    sig.blacklist_last_hit = timezone.now()
    fields = ['blacklist_hits', 'blacklist_last_hit']
    new_mac = mac and MAC_RE.match(mac) and mac not in macs_of(sig)
    if new_mac:
        sig.macs = sorted(set(sig.macs or []) | {mac})[:50]
        fields.append('macs')
    sig.save(update_fields=fields)
    if new_mac:
        _forget_index(sig.business)
        try:
            block(None, [mac], label=sig.label or sig.model or 'device', business=sig.business, why='blacklisted')
        except Exception as exc:
            logger.info('blacklist new mac %s: %s', mac, exc)
    if voucher is not None:
        from .voucher_history import record
        record(voucher, 'enforced', source='auto', reason='Blacklisted device',
               text=f'Refused: {sig.label or sig.model or "a blacklisted device"} ({mac or "device ID"}) tried this voucher on the {where}')
    if cache.add(f'tt:blk:tell:{sig.pk}:{voucher.pk if voucher else where}', 1, 600):
        _tell(sig.business, 'danger', f'🚫 Blacklisted device tried to connect{" with " + voucher.code if voucher else ""}',
              f'{sig.label or sig.model or sig.os or "Device"} ({mac or "same device ID"}) on the {where}'
              + (f' — {router.name}' if router else '') + (f'. New MAC {mac} blocked too.' if new_mac else '.')
              + (f' Reason: {sig.blacklist_reason}' if sig.blacklist_reason else ''), f'/devices/{sig.pk}/', email=True)


def flagged_seen(sig, *, voucher=None, mac='', where='login page'):
    if not sig.flag_notify or not cache.add(f'tt:flag:tell:{sig.pk}', 1, 1800):
        return
    label, _, icon = LEVELS.get(sig.flag_level, ('Flagged', '', '🚩'))
    _tell(sig.business, 'warning', f'{icon} Flagged device ({label.lower()}) is back{" with " + voucher.code if voucher else ""}',
          f'{sig.label or sig.model or sig.os or "Device"} ({norm(mac) or "device ID"}) on the {where}' + (f' — note: {sig.note}' if sig.note else ''),
          f'/devices/{sig.pk}/', email=True)


def _tell(business, level, title, body, link, email=False):
    from .models_events import EventAlert
    EventAlert.objects.create(business=business, kind='blacklist', level=level, title=title[:160], body=body[:400], link=link, sound=True, desktop=True)
    if email:
        try:
            from .notify import notify
            notify(business, 'rule_alert', title, body, link=link, key=f'blk:{link}:{timezone.now():%Y%m%d%H%M}')
        except Exception:
            pass


def check_login(voucher, mac='', fp=''):
    """Login page (device_lock.claim): the refusal message for a blacklisted device, else None.
    Flagged devices with "tell me" are reported here too."""
    business = voucher.business
    sig = find(business, mac, fp)
    if sig is not None:
        hit(sig, mac=mac, fp=fp, voucher=voucher)
        return message(business)
    if fp or mac:
        q = Q(fingerprint=fp) if fp else Q()
        if norm(mac):
            q |= Q(last_mac=norm(mac))
        f = business.device_signatures.filter(q, flag_notify=True).first() if q else None
        if f is not None:
            flagged_seen(f, voucher=voucher, mac=mac)
    return None


def enforce(router):
    """Live sync: a blacklisted device online (block not applied yet, or a router it never used) is dropped."""
    snap = cache.get(f'tt:tr:users:{router.pk}') or {}
    idx = _index(router.business)
    if not idx['mac']:
        return 0
    n = 0
    for code, rows in (snap.get('users') or {}).items():
        for r in rows:
            mac = norm(r.get('mac'))
            if mac in idx['mac'] and cache.add(f'tt:blk:kick:{router.pk}:{mac}', 1, 120):
                sig = router.business.device_signatures.filter(pk=idx['mac'][mac]).first()
                _kick(router, mac)
                if sig is not None:
                    v = router.business.vouchers.filter(code__iexact=code).first() if not str(code).upper().startswith('BYPASS:') else None
                    hit(sig, mac=mac, voucher=v, where='network', router=router)
                n += 1
    return n


def _kick(router, mac):
    from .voucher_history import channel
    try:
        if channel(router) == 'TapTap Link':
            from .linkops import send
            send(router, 'hotspot_kick_mac', {'mac': mac}, label=f'Blacklist: disconnect {mac}', minutes=15)
        else:
            from .mikrotik import MikroTikService
            with MikroTikService(router) as svc:
                for path in ('/ip/hotspot/active', '/ip/hotspot/cookie', '/ip/hotspot/host'):
                    res = svc.resource(path)
                    for row in res.get():
                        if norm(row.get('mac-address')) == mac:
                            res.remove(id=row['id'])
    except Exception as exc:
        logger.info('blacklist kick %s: %s', mac, exc)
