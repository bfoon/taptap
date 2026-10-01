"""Vouchers whose "plan" is a RouterOS internal ID such as ``*1``, ``*C`` or ``*10``.

RouterOS shows a hotspot user's profile as the profile's internal ID when that profile no longer
exists (it was deleted or renamed away) while users still point at it. TapTap imported those users
with the ID as their plan name, so plan lists filled up with ``*1``, ``*10``, ``*C``…

* :func:`resolve` — on import, an ID the router still knows is turned back into the profile name.
* :func:`groups` — the Plans page "Fix unknown profiles" tab: such vouchers per router and ID.
* :func:`fix` — give a group a real plan: TapTap's vouchers get the plan, and on the router the plan's
  profile is created if it is missing and the users are moved onto it (RouterOS API or TapTap Link).
"""
from __future__ import annotations

import re

from django.db.models import Count, Q

ORPHAN_RE = re.compile(r'^\*[0-9A-Fa-f]+$')
ORPHAN_DB_RE = r'^\*[0-9A-Fa-f]+$'
LINK_CHUNK = 100


def is_orphan(name):
    return bool(ORPHAN_RE.match(str(name or '').strip()))


def resolve(router, raw):
    """A profile reference from the router: its name, or — for an internal ID — the name of the profile
    with that ID when the router still has it (else the ID unchanged)."""
    raw = str(raw or '').strip()
    if not is_orphan(raw) or router is None:
        return raw
    from .models import RouterHotspotProfile
    name = (RouterHotspotProfile.objects.filter(router=router, mikrotik_id=raw).exclude(name='')
            .values_list('name', flat=True).first())
    return name or raw


def groups(business):
    """[{router, profile, total, used, unused, codes, suggestion, plan}] — one per router and unknown profile."""
    from .models import Router, RouterHotspotProfile
    rows = (business.vouchers.filter(plan_name__regex=ORPHAN_DB_RE)
            .values('router_id', 'plan_name')
            .annotate(total=Count('id'), used=Count('id', filter=Q(used_at__isnull=False)),
                      unused=Count('id', filter=Q(used_at__isnull=True, status='active')))
            .order_by('router_id', 'plan_name'))
    routers = {r.pk: r for r in Router.objects.filter(business=business)}
    plans = {p.name.lower(): p for p in business.plans.all()}
    plans.update({(p.mikrotik_profile_name or '').lower(): p for p in business.plans.exclude(mikrotik_profile_name='')})
    out = []
    for r in rows:
        router = routers.get(r['router_id'])
        name = (RouterHotspotProfile.objects.filter(router=router, mikrotik_id=r['plan_name']).values_list('name', flat=True).first()
                if router else None)
        codes = list(business.vouchers.filter(router_id=r['router_id'], plan_name=r['plan_name']).order_by('-created_at')
                     .values_list('code', flat=True)[:4])
        out.append({'router': router, 'router_id': r['router_id'] or 0, 'profile': r['plan_name'], 'total': r['total'],
                    'used': r['used'], 'unused': r['unused'], 'codes': codes,
                    'suggestion': name or '', 'plan': plans.get((name or '').lower())})
    return sorted(out, key=lambda g: (-g['total'], g['profile']))


def fix(business, router_id, bad, plan, user=None):
    """Give every voucher with the unknown profile ``bad`` on this router the plan ``plan``.
    Returns a message. Raises ValueError when the router cannot be reached."""
    from .models import RouterConfigChange, RouterHotspotUser, Voucher
    from .utils import log, voucher_profile
    from .voucher_history import channel
    if not is_orphan(bad):
        raise ValueError('That is not an unknown profile.')
    router = business.routers.filter(pk=router_id).first() if router_id else None
    qs = business.vouchers.filter(plan_name=bad, router=router) if router else business.vouchers.filter(plan_name=bad, router__isnull=True)
    sample = qs.first()
    if not sample:
        return 'Nothing to fix — those vouchers were already corrected.'
    # The profile comes from the PLAN: the broken vouchers' own values (often no time at all) would pick
    # TapTap's "unlimited" profile instead.
    prof, shared, rate = voucher_profile(Voucher(business=business, plan_name=plan.name, duration_minutes=plan.duration_minutes,
                                                 max_devices=plan.max_devices, price=plan.price), plan)
    names = list(qs.values_list('code', flat=True))
    result = 'No router — corrected in TapTap only'
    if router:
        if channel(router) == 'TapTap Link':
            from .linkops import send
            for i in range(0, len(names), LINK_CHUNK):
                part = names[i:i + LINK_CHUNK]
                send(router, 'hotspot_users_profile', {'profile': {'name': prof, 'shared': int(shared or 1), 'rate': rate or ''}, 'names': part},
                     label=f'Move {len(part)} voucher{"s" if len(part) != 1 else ""} to profile {prof}', user=user)
            result = 'queued for the router (it applies it at its next check-in)'
        else:
            from .mikrotik import MikroTikService
            try:
                with MikroTikService(router) as svc:
                    svc.ensure_hotspot_profile(prof, shared, rate)          # create the plan's profile if missing
                    users = svc.resource('/ip/hotspot/user')
                    ids = {str(u.get('name', '')): u.get('id') for u in users.get()}
                    moved = 0
                    for n in names:
                        if ids.get(n):
                            users.set(id=ids[n], profile=prof); moved += 1
            except Exception as exc:
                raise ValueError(f'{router.name} could not be updated: {exc}')
            result = f'moved to profile “{prof}” on {router.name}'
    # TapTap side: the plan, and for vouchers not used yet its devices / time / price.
    # (Fixed list of ids: once renamed, "plan_name == bad" would match nothing any more.)
    qs = Voucher.objects.filter(pk__in=list(qs.values_list('pk', flat=True)))
    qs.update(plan_name=plan.name, router_profile='')
    qs.filter(used_at__isnull=True).update(max_devices=plan.max_devices, duration_minutes=plan.duration_minutes)
    qs.filter(used_at__isnull=True, price=0).update(price=plan.price)
    if router:
        RouterHotspotUser.objects.filter(router=router, username__in=names).update(profile=prof)
        RouterConfigChange.objects.create(business=business, router=router, actor=user if getattr(user, 'is_authenticated', False) else None,
                                          resource_path='/ip/hotspot/user', operation='fix-unknown-profile', target_id=bad,
                                          fields={'plan': plan.name, 'profile': prof, 'vouchers': len(names)}, status='success')
    log(business, 'Plan Fixed', f'{len(names)} voucher(s) with unknown profile {bad} → plan {plan.name}')
    return f'{len(names)} voucher{"s" if len(names) != 1 else ""} now use the plan “{plan.name}” — {result}.'


# ─────────────────────────── delete unknown profiles ───────────────────────────
def _traces(business):
    """{(router_id, name): {'plan': VoucherPlan|None, 'on_router': bool}} for *X names that live on as a plan or in
    TapTap's copy of a router's profiles (even with no voucher left)."""
    from .models import RouterHotspotProfile
    out = {}
    for m in RouterHotspotProfile.objects.filter(business=business, name__regex=ORPHAN_DB_RE, is_present=True):
        out.setdefault((m.router_id, m.name), {'plan': None, 'on_router': False})['on_router'] = True
    for p in business.plans.filter(name__regex=ORPHAN_DB_RE):
        key = next((k for k in out if k[1] == p.name), (getattr(p, 'imported_from_router_id', None), p.name))
        out.setdefault(key, {'plan': None, 'on_router': False})['plan'] = p
    return out


def groups_with_traces(business):
    """groups() plus *X names that have no voucher left but still exist as a plan or a router profile."""
    from .models import Router
    gs = groups(business)
    traces = _traces(business)
    routers = {r.pk: r for r in Router.objects.filter(business=business)}
    seen = set()
    for g in gs:
        t = traces.get((g['router_id'] or None, g['profile'])) or next((v for k, v in traces.items() if k[1] == g['profile']), None)
        g['orphan_plan'], g['on_router'] = (t or {}).get('plan'), bool((t or {}).get('on_router'))
        seen.add(g['profile'])
    for (rid, name), t in traces.items():
        if name in seen:
            continue
        gs.append({'router': routers.get(rid), 'router_id': rid or 0, 'profile': name, 'total': 0, 'used': 0, 'unused': 0, 'codes': [],
                   'suggestion': '', 'plan': None, 'orphan_plan': t['plan'], 'on_router': t['on_router']})
    return gs


def _remove_router_profile(router, name, user=None):
    """Remove a profile literally named *X from the router — only when no user uses it any more."""
    from .voucher_history import channel
    if channel(router) == 'TapTap Link':
        from .linkops import send
        send(router, 'hotspot_profile_remove', {'name': name}, label=f'Remove unknown profile {name}', user=user)
        return 'queued for the router'
    from .mikrotik import MikroTikService
    with MikroTikService(router) as svc:
        if svc.resource('/ip/hotspot/user').get(profile=name):
            return 'still used by users on the router — not removed there'
        res = svc.resource('/ip/hotspot/user/profile')
        for row in res.get(name=name):
            res.remove(id=row['id'])
    return 'removed from the router'


def delete(business, router_id, bad, plan=None, user=None):
    """Correct every voucher on the unknown profile ``bad`` (to ``plan``), then delete the name everywhere.
    Returns a message. Raises ValueError when a plan is needed or a router cannot be reached."""
    from .models import RouterHotspotProfile
    from .voucher_bin import BinError, delete_plan
    if not is_orphan(bad):
        raise ValueError('Only unknown profiles (like *1 or *C) can be deleted here.')
    router = business.routers.filter(pk=router_id).first() if router_id else None
    vqs = business.vouchers.filter(plan_name=bad)
    vqs = vqs.filter(router=router) if router else vqs.filter(router__isnull=True)
    parts = []
    if vqs.exists():
        if plan is None:
            raise ValueError(f'{vqs.count()} voucher(s) still use {bad}. Choose the plan to move them to first.')
        parts.append(fix(business, router.pk if router else None, bad, plan, user).rstrip('.'))
    # the name on the router(s), in TapTap's copy, and as a plan — once nothing uses it any more
    mirror = RouterHotspotProfile.objects.filter(business=business, name=bad)
    if router:
        mirror = mirror.filter(router=router)
    for m in mirror.select_related('router'):
        if m.is_present and m.router_id:
            try:
                parts.append(f'profile {bad} {_remove_router_profile(m.router, bad, user)} ({m.router.name})')
            except Exception as exc:
                raise ValueError(f'{m.router.name} could not be reached: {exc}')
    mirror.delete()
    for p in business.plans.filter(name=bad):
        if business.vouchers.filter(plan_name=bad).exists():
            parts.append(f'plan {bad} kept: other vouchers still use it')
            continue
        from .permissions import ALL_PERMISSIONS
        try:
            delete_plan(p, user=user, reason=f'Unknown profile {bad} cleaned up', perms=ALL_PERMISSIONS)
            parts.append(f'plan {bad} moved to the bin')
        except BinError as exc:
            parts.append(f'plan {bad} kept: {exc}')
    from .utils import log
    log(business, 'Plan Fixed', f'Unknown profile {bad} deleted' + (f' on {router.name}' if router else ''))
    return f'Unknown profile {bad} deleted. ' + ('; '.join(parts) + '.' if parts else '')
