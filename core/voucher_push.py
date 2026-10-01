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
    vouchers = list(Voucher.objects.filter(pk__in=voucher_ids, router=router, source='taptap').select_related('router'))
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
        if v.router_id and v.source == 'taptap':
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
