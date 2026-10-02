"""Send only the vouchers that changed to their router — no full router sync.

Creating a batch, a single voucher, a member or changing one used to start a full sync
(plans, users, bindings, interfaces, devices, configuration…). Now only those vouchers are
sent: Direct API / Tunnel routers in a small background job over one connection, TapTap Link
routers through their normal voucher queue (25 per check-in). The full sync still runs on its
own schedule and when you press Sync.
"""
from __future__ import annotations

import logging

from django.utils import timezone

logger = logging.getLogger('taptap.push')


def _on_link(router):
    from .voucher_history import channel
    return channel(router) == 'TapTap Link'


def push_now(router, voucher_ids):
    """Push these vouchers to this router over the API (runs in the worker). Returns (sent, failed)."""
    from .durations import router_limit
    from .mikrotik import MikroTikService
    from .models import Voucher
    from .sync import voucher_profile
    vouchers = list(Voucher.objects.filter(pk__in=voucher_ids, router=router, source='taptap').exclude(status__in=['expired', 'archived']).select_related('router'))
    if not vouchers:
        return 0, 0
    plans = {p.name: p for p in router.business.plans.all()}
    sent = failed = 0
    try:
        svc = MikroTikService(router).connect()
    except Exception as exc:
        Voucher.objects.filter(pk__in=[v.pk for v in vouchers]).update(mikrotik_sync_status='Pending', mikrotik_sync_error=f'Router not reachable: {exc}'[:500])
        return 0, len(vouchers)
    ensured = set()
    try:
        for v in vouchers:
            try:
                profile, shared, rate = voucher_profile(v, plans.get(v.plan_name))
                if profile not in ensured:
                    svc.ensure_hotspot_profile(profile, shared, rate); ensured.add(profile)
                kind = 'member' if v.is_member else 'voucher'
                _, item_id = svc.upsert_voucher(v.code, profile, limit_uptime=router_limit(v, plans.get(v.plan_name)), password=v.login_password,
                                                disabled=(v.status != 'active' or bool(v.frozen_at) or bool(v.deleted_at)),
                                                comment=f'TapTap {kind} {v.code}' + (f' · {v.customer_name}' if v.customer_name else ''))
                Voucher.objects.filter(pk=v.pk).update(mikrotik_id=str(item_id or ''), mikrotik_sync_status='Synced', mikrotik_sync_error='')
                sent += 1
            except Exception as exc:
                Voucher.objects.filter(pk=v.pk).update(mikrotik_sync_status='Pending', mikrotik_sync_error=str(exc)[:500])
                failed += 1
    finally:
        svc.close()
    return sent, failed


def push_vouchers(vouchers, user=None):
    """Send just these vouchers to their routers. Returns {router: message}."""
    from .models import Voucher
    by_router = {}
    for v in vouchers:
        if v.router_id and v.source == 'taptap' and v.status not in ('expired', 'archived'):
            by_router.setdefault(v.router, []).append(v.pk)
    out = {}
    for router, ids in by_router.items():
        Voucher.objects.filter(pk__in=ids).update(mikrotik_sync_status='Pending', mikrotik_sync_error='')
        if _on_link(router):
            from .agent import push_pending_vouchers
            push_pending_vouchers(router)       # the rest follow at the next check-ins (25 at a time)
            out[router] = f'{len(ids)} voucher(s) going to {router.name} with its next check-ins'
            continue
        try:
            from .tasks import push_vouchers_task
            push_vouchers_task.delay(router.pk, ids)
            out[router] = f'{len(ids)} voucher(s) being sent to {router.name}'
        except Exception as exc:          # no worker: do it now
            sent, failed = push_now(router, ids)
            out[router] = f'{sent} voucher(s) sent to {router.name}' + (f', {failed} will retry at the next sync' if failed else '')
    return out


def set_router_profile(voucher, profile, shared=1, rate='', user=None):
    """Move one hotspot user onto a profile on its router — any voucher, also ones made on the router
    (Mikhmon/WinBox). Only the profile changes: the user's password, limit and used time are kept.
    The profile is created only if the router does not have it (an existing profile is not edited).
    Returns (ok, message)."""
    router = voucher.router
    if router is None or not profile:
        return False, 'No router or profile.'
    if _on_link(router):
        from .linkops import send
        try:
            send(router, 'hotspot_user_profile', {'name': voucher.code, 'profile': profile, 'shared': int(shared or 1), 'rate': rate or ''},
                 label=f'Move {voucher.code} to profile {profile}', user=user, minutes=60 * 24)
        except ValueError as exc:
            return False, str(exc)
        return True, f'{voucher.code} moves to {profile} at {router.name}\'s next check-in'
    from .mikrotik import MikroTikService
    try:
        svc = MikroTikService(router).connect()
        try:
            profs = svc.resource('/ip/hotspot/user/profile')
            if not profs.get(name=profile):
                fields = {'name': profile, 'shared_users': str(max(1, int(shared or 1)))}
                if rate:
                    fields['rate_limit'] = rate
                profs.add(**fields)
            users = svc.resource('/ip/hotspot/user')
            row = users.get(name=voucher.code)
            if not row:
                return False, f'{voucher.code} is not on {router.name}.'
            users.set(id=row[0]['id'], profile=profile)
        finally:
            svc.close()
    except Exception as exc:
        return False, f'{router.name}: {exc}'
    from .models import RouterHotspotUser
    RouterHotspotUser.objects.filter(router=router, username__iexact=voucher.code).update(profile=profile)
    return True, f'{voucher.code} is now on {profile} on {router.name}'


def set_profile_shared(router, profile, shared, user=None):
    """Set how many devices a router profile allows (shared-users). Returns (ok, message)."""
    shared = max(1, int(shared))
    if _on_link(router):
        from .linkops import send
        try:
            send(router, 'hotspot_profile_shared', {'profile': profile, 'shared': shared}, label=f'Profile {profile}: {shared} devices', user=user, minutes=60 * 24)
        except ValueError as exc:
            return False, str(exc)
        ok, msg = True, f'{profile} on {router.name}: {shared} devices (next check-in)'
    else:
        from .mikrotik import MikroTikService
        try:
            svc = MikroTikService(router).connect()
            try:
                profs = svc.resource('/ip/hotspot/user/profile')
                row = profs.get(name=profile)
                if not row:
                    return False, f'{profile} is not on {router.name}.'
                profs.set(id=row[0]['id'], shared_users=str(shared))
            finally:
                svc.close()
        except Exception as exc:
            return False, f'{router.name}: {exc}'
        ok, msg = True, f'{profile} on {router.name}: {shared} devices'
    from .models import RouterHotspotProfile
    RouterHotspotProfile.objects.filter(router=router, name=profile).update(shared_users=shared)
    return ok, msg
