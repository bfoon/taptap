"""Batch detail → "Profile & stock health": every voucher of a batch not used yet must work.

For each voucher not used yet (active, not frozen — in stock or sold to a customer who has not
logged in), TapTap compares what it expects with the router's hotspot user:

* **profile** — the router profile must be the batch's profile in TapTap (the plan's profile, or the
  one chosen for these vouchers). If not, the router is switched to TapTap's: TapTap is the truth;
* **disabled** on the router while active in TapTap;
* **locked** — a MAC address on the router user, sticky-lock slots left in TapTap, or old cookies /
  sessions on the router;
* **time limit** — a TapTap voucher must carry its plan time (``limit-uptime``);
* **missing** on the router — TapTap vouchers are sent again; router-made ones are only reported
  (sending them again would replace their password);
* **used on the router** — uptime above zero although TapTap thinks it is unused: reported, left alone.

``swap`` gives all of them another profile first (a router profile, or another plan's profile) and then
repairs. Only the fields above are touched on the router — never passwords or names.
"""
from __future__ import annotations

import logging
import re

from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger('taptap.batch_health')
ANY_MAC = '00:00:00:00:00:00'
LINK_CHUNK = 60
PROBLEMS = [
    ('missing', 'Not on the router'), ('profile', 'Wrong profile on the router'), ('disabled', 'Disabled on the router'),
    ('mac_lock', 'Locked to a MAC on the router'), ('device_lock', 'Device-lock slots left in TapTap'),
    ('limit', 'Missing or wrong time limit'), ('used_on_router', 'Already used on the router'),
]
FIXABLE = {'missing', 'profile', 'disabled', 'mac_lock', 'device_lock', 'limit'}


def unused(batch):
    """Vouchers of the batch nobody has used yet and that should work: active, not frozen."""
    return batch.vouchers.filter(deleted_at__isnull=True, used_at__isnull=True, status='active', frozen_at__isnull=True) \
        .select_related('router').order_by('serial', 'code')


def expected_profile(voucher, plan=None):
    from .utils import voucher_profile
    return voucher_profile(voucher, plan)


def _plan(batch):
    return batch.plan or batch.business.plans.filter(name=batch.vouchers.values_list('plan_name', flat=True).first() or '').first()


def _router_rows(router, live):
    """name.upper() -> row. Live from the router (API) when asked and possible, else TapTap's copy."""
    if live:
        from .voucher_history import channel
        if channel(router) != 'TapTap Link':
            from .mikrotik import MikroTikService
            try:
                with MikroTikService(router) as svc:
                    return {str(r.get('name', '')).upper(): r for r in svc.resource('/ip/hotspot/user').get()}, 'live'
            except Exception as exc:
                logger.info('batch health: %s not reachable (%s), using the last copy', router.name, exc)
    rows = {}
    for u in router.hotspot_users.filter(is_present=True):
        rows[u.username.upper()] = {'name': u.username, 'profile': u.profile, 'mac-address': u.mac_address, 'disabled': 'true' if u.disabled else 'false',
                                    'limit-uptime': u.limit_uptime, 'uptime': u.uptime, 'id': u.mikrotik_id}
    return rows, 'copy'


def _secs(v):
    from .sync import _routeros_seconds
    return _routeros_seconds(v or '')


def check(batch, live=False):
    """{'vouchers': [...], 'counts': {problem: n}, 'total', 'ok', 'sold', 'source', 'profile'}"""
    from .durations import router_limit
    plan = _plan(batch)
    vs = list(unused(batch))
    from .models import VoucherDeviceBinding
    locked = set(VoucherDeviceBinding.objects.filter(voucher__in=vs).values_list('voucher_id', flat=True))
    by_router, source = {}, set()
    for r in {v.router for v in vs if v.router}:
        by_router[r.pk], s = _router_rows(r, live)
        source.add(s)
    out, counts = [], {k: 0 for k, _ in PROBLEMS}
    for v in vs:
        want, _, _ = expected_profile(v, plan)
        row = by_router.get(v.router_id, {}).get(v.code.upper()) if v.router_id else None
        problems = []
        if v.router_id and row is None:
            problems.append('missing')
        if row is not None:
            if _secs(row.get('uptime')) > 0:
                problems.append('used_on_router')
            if str(row.get('profile', '')) != want:
                problems.append('profile')
            if str(row.get('disabled', 'false')).lower() in ('true', 'yes'):
                problems.append('disabled')
            if str(row.get('mac-address', '') or ANY_MAC).upper() not in ('', ANY_MAC):
                problems.append('mac_lock')
            if v.source == 'taptap':
                lim = router_limit(v)
                if lim and _secs(row.get('limit-uptime')) != _secs(lim):
                    problems.append('limit')
        if v.pk in locked:
            problems.append('device_lock')
        for p in problems:
            counts[p] += 1
        out.append({'v': v, 'want': want, 'have': str(row.get('profile', '')) if row else '', 'problems': problems})
    return {'vouchers': out, 'counts': counts, 'total': len(vs), 'ok': sum(1 for x in out if not x['problems']),
            'sold': sum(1 for x in out if x['v'].sold_at), 'source': 'live' if source == {'live'} else ('copy' if source else 'none'),
            'profile': expected_profile(vs[0], plan)[0] if vs else (plan.mikrotik_profile_name or plan.name if plan else ''),
            'labels': dict(PROBLEMS), 'checked_at': timezone.now()}


def swap(batch, target, user=None):
    """Give every voucher not used yet another profile, then repair. target: '' (the plan's own profile),
    'router:NAME' (a router profile) or 'plan:ID' (that plan's profile; the vouchers keep their plan and price)."""
    from .models import Voucher
    ids = list(unused(batch).values_list('pk', flat=True))
    if not target:
        name = ''
    elif target.startswith('router:'):
        name = target[7:].strip()[:120]
        if not re.match(r'^[\w .@:+/-]{1,120}$', name):
            raise ValueError('That profile name is not valid.')
    elif target.startswith('plan:') and target[5:].isdigit():
        p = batch.business.plans.filter(pk=target[5:]).first()
        if not p:
            raise ValueError('That plan does not exist any more.')
        name, _, _ = expected_profile(Voucher(business=batch.business, plan_name=p.name, duration_minutes=p.duration_minutes,
                                              max_devices=p.max_devices, price=p.price), p)
    else:
        raise ValueError('Choose a profile.')
    Voucher.objects.filter(pk__in=ids).update(router_profile=name)
    return repair(batch, user=user, note=f'Profile swapped to {name or "the plan’s own profile"}')


def repair(batch, user=None, note=''):
    """Fix every fixable problem of the vouchers not used yet, on the router and in TapTap. Returns a message."""
    from .models import RouterHotspotUser, VoucherDeviceBinding, VoucherEvent
    from .voucher_history import channel
    report = check(batch, live=True)
    todo = [x for x in report['vouchers'] if set(x['problems']) & FIXABLE]
    if not todo:
        return f'All {report["total"]} unused voucher{"s" if report["total"] != 1 else ""} already work: right profile, enabled, not locked.'
    plan = _plan(batch)
    VoucherDeviceBinding.objects.filter(voucher__in=[x['v'] for x in todo if 'device_lock' in x['problems']]).delete()
    by_router = {}
    for x in todo:
        if x['v'].router_id:
            by_router.setdefault(x['v'].router, []).append(x)
    done, queued, failed, resent = 0, 0, [], 0
    for router, items in by_router.items():
        missing = [x['v'] for x in items if 'missing' in x['problems'] and x['v'].source == 'taptap']
        present = [x for x in items if 'missing' not in x['problems']]
        profiles = {}
        for x in present:
            prof, shared, rate = expected_profile(x['v'], plan)
            profiles[prof] = (shared, rate)
        try:
            if channel(router) == 'TapTap Link':
                from .linkops import send
                users = [_heal_row(x) for x in present]
                for i in range(0, len(users), LINK_CHUNK):
                    send(router, 'hotspot_users_heal', {'profiles': [{'name': n, 'shared': int(s or 1), 'rate': r or ''} for n, (s, r) in profiles.items()],
                                                        'users': users[i:i + LINK_CHUNK]},
                         label=f'Repair {len(users[i:i + LINK_CHUNK])} unused voucher(s) of {batch.name}', user=user)
                queued += len(present)
            else:
                from .mikrotik import MikroTikService
                with MikroTikService(router) as svc:
                    for prof, (shared, rate) in profiles.items():
                        svc.ensure_hotspot_profile(prof, shared, rate)        # created if the router does not have it
                    res = svc.resource('/ip/hotspot/user')
                    ids = {str(r.get('name', '')).upper(): r.get('id') for r in res.get()}
                    for x in present:
                        rid = ids.get(x['v'].code.upper())
                        if not rid:
                            continue
                        h = _heal_row(x)
                        fields = {'profile': h['prof'], 'disabled': 'no', 'mac_address': ANY_MAC}
                        if h.get('lim'):
                            fields['limit_uptime'] = h['lim']
                        res.set(id=rid, **fields)
                        for path in ('/ip/hotspot/active', '/ip/hotspot/cookie'):
                            try:
                                r2 = svc.resource(path)
                                for row in r2.get(user=x['v'].code):
                                    r2.remove(id=row['id'])
                            except Exception:
                                pass
                        done += 1
            for x in present:
                RouterHotspotUser.objects.filter(router=router, username__iexact=x['v'].code).update(
                    profile=x['want'], disabled=False, mac_address='')
        except Exception as exc:
            failed.append(f'{router.name}: {exc}')
        if missing:
            from .voucher_push import push_vouchers
            push_vouchers(missing, user)
            resent += len(missing)
    VoucherEvent.objects.bulk_create([VoucherEvent(business=batch.business, voucher=x['v'], voucher_code=x['v'].code, event='note', source='user',
                                                   user=user if getattr(user, 'is_authenticated', False) else None,
                                                   reason=(note or 'Stock repaired from the batch page')[:255],
                                                   detail={'text': 'Fixed: ' + ', '.join(dict(PROBLEMS)[p] for p in x['problems'] if p in FIXABLE),
                                                           'profile': x['want']}) for x in todo])
    from .utils import log
    log(batch.business, 'Batch Repaired', f'{batch.name}: {len(todo)} unused voucher(s) — {note or "repair"}')
    parts = []
    if done:
        parts.append(f'{done} fixed on the router')
    if queued:
        parts.append(f'{queued} queued for TapTap Link (applied at the next check-in)')
    if resent:
        parts.append(f'{resent} sent to the router again')
    skipped = sum(1 for x in todo if 'missing' in x['problems'] and x['v'].source != 'taptap')
    if skipped:
        parts.append(f'{skipped} router-made voucher(s) missing on the router — re-create them there')
    msg = (note + '. ' if note else '') + ('; '.join(parts) or 'Fixed in TapTap') + '.'
    if failed:
        msg += ' Not reached: ' + '; '.join(failed)
    return msg


def _heal_row(x):
    from .durations import router_limit
    v = x['v']
    return {'n': v.code, 'prof': x['want'], 'lim': router_limit(v) if v.source == 'taptap' else ''}
