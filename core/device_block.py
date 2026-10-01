"""Block / unblock one device (by MAC) — used by the Slowed page for devices of a shared voucher.

A block is a HotSpot IP binding of type ``blocked`` (the router refuses the device entirely), kept in
TapTap as a ``SyncedIPBinding`` so it also shows on the IP Binding page. The device's live session is
dropped at once. If the MAC already had a binding (e.g. a paid "bypassed" device), its old type is kept
and Unblock puts it back instead of deleting it.

Works over the RouterOS API (direct / tunnel) and TapTap Link.
"""
from __future__ import annotations

import logging
import re

from django.utils import timezone

logger = logging.getLogger('taptap.device_block')
MAC_RE = re.compile(r'^([0-9A-F]{2}:){5}[0-9A-F]{2}$')


def norm(mac):
    h = ''.join(c for c in str(mac or '').upper() if c in '0123456789ABCDEF')
    return ':'.join(h[i:i + 2] for i in range(0, 12, 2)) if len(h) == 12 else ''


def blocked_macs(business):
    from .models import SyncedIPBinding
    return {norm(m) for m in SyncedIPBinding.objects.filter(business=business, binding_type='blocked', disabled=False)
            .exclude(mac_address='').values_list('mac_address', flat=True)} - {''}


def routers_for(voucher, macs):
    """The voucher's router plus every router that has seen these MACs (a blocked phone cannot hop)."""
    from .models import Router, RouterDevice
    ids = set(RouterDevice.objects.filter(router__business=voucher.business, mac_address__in=macs).values_list('router_id', flat=True))
    if voucher.router_id:
        ids.add(voucher.router_id)
    if not ids:
        ids = set(voucher.business.routers.values_list('id', flat=True))
    return list(Router.objects.filter(pk__in=ids))


def _on_link(router):
    from .voucher_history import channel
    return channel(router) == 'TapTap Link'


def block(voucher, macs, label='', user=None):
    """Block these MACs on the routers concerned. Returns a short result text."""
    from .models import SyncedIPBinding
    from .voucher_history import record
    macs = [m for m in {norm(x) for x in macs} if MAC_RE.match(m)]
    if not macs:
        raise ValueError('This device has no MAC address TapTap knows, so it cannot be blocked.')
    comment = f'TapTap: blocked ({voucher.code}{" · " + label if label else ""})'[:255]
    done, queued, failed = 0, 0, []
    for router in routers_for(voucher, macs):
        for mac in macs:
            b = SyncedIPBinding.objects.filter(business=voucher.business, router=router, mac_address__iexact=mac).first()
            restore = {}
            if b and b.binding_type != 'blocked':
                restore = {'restore_type': b.binding_type, 'restore_comment': b.comment, 'restore_disabled': b.disabled}
            if b is None:
                b = SyncedIPBinding(business=voucher.business, router=router, mac_address=mac, server='all', source='taptap')
            b.binding_type, b.comment, b.disabled, b.sync_status = 'blocked', comment, False, 'Pending'
            b.raw_data = {**(b.raw_data or {}), **restore, 'blocked_by_taptap': True, 'voucher': voucher.code}
            b.save()
            try:
                if _on_link(router):
                    from .linkops import send
                    send(router, 'binding_upsert', {'mac': mac, 'type': 'blocked', 'server': 'all', 'comment': comment, 'disabled': False},
                         label=f'Block {label or mac}', user=user)
                    send(router, 'hotspot_kick', {'user': voucher.code, 'mac': mac}, label=f'Disconnect {label or mac}', user=user, minutes=15)
                    SyncedIPBinding.objects.filter(pk=b.pk).update(sync_status='Queued')
                    queued += 1
                else:
                    from .mikrotik import MikroTikService
                    with MikroTikService(router) as svc:
                        _, item_id = svc.upsert_binding(b)
                        for row in svc.resource('/ip/hotspot/active').get():
                            if norm(row.get('mac-address')) == mac:
                                svc.resource('/ip/hotspot/active').remove(id=row['id'])
                    SyncedIPBinding.objects.filter(pk=b.pk).update(mikrotik_id=str(item_id or ''), sync_status='Synced', sync_error='')
                    done += 1
            except Exception as exc:
                SyncedIPBinding.objects.filter(pk=b.pk).update(sync_status='Error', sync_error=str(exc)[:500])
                failed.append(f'{router.name}: {exc}')
    record(voucher, 'enforced', user=user, reason='Device blocked', text=f'{label or ", ".join(macs)} blocked ({", ".join(macs)})')
    from .live import push_event
    push_event(voucher.business_id, f'Device {label or macs[0]} of {voucher.code} blocked', 'fix')
    if failed and not (done or queued):
        raise ValueError('The router did not accept the block: ' + '; '.join(failed))
    return ('Blocked on the router' if done else 'Queued for the router') + (f' (not on {"; ".join(failed)})' if failed else '')


def unblock(voucher, macs, label='', user=None):
    """Remove TapTap's block on these MACs; a binding the device had before is put back."""
    from .models import SyncedIPBinding
    from .voucher_history import record
    macs = {norm(x) for x in macs} - {''}
    n = 0
    for b in SyncedIPBinding.objects.filter(business=voucher.business, binding_type='blocked', mac_address__in=list(macs)).select_related('router'):
        raw = b.raw_data or {}
        restore = raw.get('restore_type')
        try:
            if _on_link(b.router):
                from .linkops import send
                if restore:
                    send(b.router, 'binding_upsert', {'mac': b.mac_address, 'type': restore, 'server': b.server or 'all',
                                                      'comment': raw.get('restore_comment') or 'TapTap', 'disabled': bool(raw.get('restore_disabled'))},
                         label=f'Unblock {label or b.mac_address}', user=user)
                else:
                    send(b.router, 'binding_remove', {'mac': b.mac_address}, label=f'Unblock {label or b.mac_address}', user=user)
            else:
                from .mikrotik import MikroTikService
                with MikroTikService(b.router) as svc:
                    if restore:
                        b.binding_type, b.comment, b.disabled = restore, raw.get('restore_comment') or 'TapTap', bool(raw.get('restore_disabled'))
                        svc.upsert_binding(b)
                    else:
                        for row in svc.resource('/ip/hotspot/ip-binding').get():
                            if norm(row.get('mac-address')) == norm(b.mac_address):
                                svc.delete_binding(row['id'])
        except Exception as exc:
            logger.warning('unblock %s on %s: %s', b.mac_address, b.router.name, exc)
            raise ValueError(f'{b.router.name} did not accept it: {exc}')
        if restore:
            SyncedIPBinding.objects.filter(pk=b.pk).update(binding_type=restore, comment=raw.get('restore_comment') or 'TapTap',
                                                           disabled=bool(raw.get('restore_disabled')), raw_data={}, last_seen_at=timezone.now())
        else:
            b.delete()
        n += 1
    if n:
        record(voucher, 'note', user=user, reason='Device unblocked', text=f'{label or ", ".join(sorted(macs))} can use the Wi-Fi again')
    return n
