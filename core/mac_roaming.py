"""Safely release the previous MAC after the EXISTING device fingerprint identifies it.

This does not turn a 1-device voucher into a shareable voucher: TapTap's normal
claim() still decides whether a device is known and allowed. On a verified move
we clear the OLD MAC session/cookie, not the voucher or its device binding.

Important constraint: after forgetting Wi-Fi, iOS may generate a new MAC and
captive-browser storage may no longer expose the same device token. Without a
persistent token or unchanged MAC, it is impossible to prove it is the same
phone; re-entering the voucher or an owner-authorized reset is then required.
"""
from __future__ import annotations
import logging
from functools import wraps

logger = logging.getLogger('taptap.mac_roaming')


def _recognized_roam(original):
    @wraps(original)
    def wrapped(voucher, mac='', fp='', source='portal', label=''):
        from . import device_lock
        from .models import VoucherDeviceBinding

        new_mac = device_lock.norm_mac(mac)
        old_mac = ''
        token = str(fp or '').strip()[:128]
        # Only fingerprint-matched migrations. A new MAC without a matching
        # retained token cannot safely be identified as the old phone.
        if token and new_mac and device_lock.enabled(voucher):
            old = VoucherDeviceBinding.objects.filter(
                voucher=voucher, device_token_hash=token
            ).order_by('slot_no').first()
            if old:
                old_mac = device_lock.norm_mac(old.current_mac)

        result = original(voucher, mac=mac, fp=fp, source=source, label=label)
        if not (old_mac and new_mac and old_mac != new_mac and result.allowed
                and getattr(result.binding, 'current_mac', '') == new_mac):
            return result

        router = getattr(voucher, 'router', None)
        if not router:
            return result
        # Normal claim() already instructs RouterOS to update the MAC lock.
        # Then forget only the stale OLD MAC, keeping any other allowed devices
        # on multi-device vouchers undisturbed.
        try:
            from .voucher_history import channel
            if channel(router) == 'TapTap Link':
                from .linkops import send
                send(router, 'hotspot_kick',
                     {'user': voucher.code, 'mac': old_mac},
                     label=f'Release previous MAC for {voucher.code}', minutes=15)
            else:
                from .mikrotik import MikroTikService
                svc = MikroTikService(router).connect()
                try:
                    act = svc.resource('/ip/hotspot/active')
                    for row in act.get(user=voucher.code):
                        if device_lock.norm_mac(row.get('mac-address')) == old_mac:
                            act.remove(id=row['id'])
                    cookies = svc.resource('/ip/hotspot/cookie')
                    for row in cookies.get(user=voucher.code):
                        if device_lock.norm_mac(row.get('mac-address')) == old_mac:
                            cookies.remove(id=row['id'])
                finally:
                    svc.close()
        except Exception:
            # Fail securely: preserve binding and let support retry router sync.
            # Do not make a new device slot or erase the authorization record.
            logger.exception('Previous-MAC cleanup failed on %s', voucher.code)
        return result
    return wrapped


def install():
    from . import device_lock
    if not getattr(device_lock.claim, '_taptap_mac_roaming', False):
        wrapped = _recognized_roam(device_lock.claim)
        wrapped._taptap_mac_roaming = True
        device_lock.claim = wrapped
