"""Move every voucher of one plan to another plan — without disrupting anyone online.

* Vouchers in use keep their start time and their own length (the clock does not restart and
  nobody loses or gains time). They get the new plan's devices, unless that is fewer than they
  have now (nobody is cut off).
* Unused vouchers become the new plan fully: its length and devices — and its price if not sold.
* On the router only the profile changes; a session already online carries on and picks up the
  new profile at its next login. TapTap's vouchers are re-sent; vouchers made on the router get
  just their profile changed (password, limit and used time stay).
* Batches of the old plan point to the new plan.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

LIVE = ('active', 'disabled')


def live_vouchers(plan):
    from .models import Voucher
    return Voucher.objects.filter(business=plan.business, plan_name=plan.name, status__in=LIVE)


def move_vouchers(src, dst, user=None):
    """Returns a summary dict."""
    from .models import Voucher, VoucherBatch
    from .utils import voucher_profile
    from .voucher_history import record
    from .voucher_push import push_vouchers, set_router_profile
    if src.pk == dst.pk or src.business_id != dst.business_id:
        raise ValueError('Choose another plan of this business.')
    vouchers = list(live_vouchers(src).select_related('router'))
    in_use = unused = 0
    with transaction.atomic():
        for v in vouchers:
            fields = ['plan_name']
            v.plan_name = dst.name
            if v.used_at or v.expires_at:
                in_use += 1
                if (dst.max_devices or 1) > (v.max_devices or 1):
                    v.max_devices = dst.max_devices; fields.append('max_devices')
            else:
                unused += 1
                v.duration_minutes, v.max_devices = dst.duration_minutes, dst.max_devices
                fields += ['duration_minutes', 'max_devices']
                if not v.sold_at:
                    v.price = dst.price; fields.append('price')
            if v.router_profile:
                v.router_profile = ''; fields.append('router_profile')    # follow the new plan's profile
            Voucher.objects.filter(pk=v.pk).update(**{f: getattr(v, f) for f in fields})
            record(v, 'note', user=user, text=f'Moved from plan {src.name} to {dst.name}'
                   + (' — in use: start time and length kept' if (v.used_at or v.expires_at) else ''))
        VoucherBatch.objects.filter(business=src.business, plan=src).update(plan=dst)
    msgs = []
    taptap = [v for v in vouchers if v.source == 'taptap' and v.router_id]
    if taptap:
        msgs += list(push_vouchers(taptap, user).values())
    router_made = [v for v in vouchers if v.source != 'taptap' and v.router_id]
    moved_on_router = 0
    for v in router_made:
        prof, shared, rate = voucher_profile(v, dst)
        ok, _ = set_router_profile(v, prof, v.max_devices or shared, rate, user)
        moved_on_router += 1 if ok else 0
    from .utils import log
    log(src.business, 'Plan Vouchers Moved', f'{len(vouchers)} voucher(s) moved from {src.name} to {dst.name} ({in_use} in use, {unused} unused)')
    return {'moved': len(vouchers), 'in_use': in_use, 'unused': unused, 'router_made': len(router_made),
            'router_moved': moved_on_router, 'messages': msgs}
