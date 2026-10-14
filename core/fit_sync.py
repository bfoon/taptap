"""Targeted sync: send exactly what you name to the routers — one voucher, a few, a batch, a plan, the members,
a router, the plan profiles, the time limits, or everything.

Direction: TapTap → router. TapTap is the source of truth for its own vouchers and members, so a sync writes
TapTap's values (profile, password, time limit, enabled/disabled) onto the router. Users made on the router
(source "mikrotik") are never overwritten: for those a sync means reading the router again (a full router sync).

Two halves:
    compare()/status()  read only — what differs between TapTap and the router's last known state, per voucher
    run()               does it, through the services TapTap already uses everywhere:
                          vouchers → voucher_push.push_vouchers (API now, Link 25 per check-in)
                          members  → member_router_alignment.aligned_push_one (exact member clock, verified)
                          time     → router_time.repair_api / repair_link (limit-uptime rule)
                          router   → tasks.enqueue_router_sync (full read + write sync)
                          profiles → each plan's HotSpot profile (devices / speed) created or corrected
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('taptap.fit.sync')

SCOPES = {
    'voucher': 'One or more vouchers/members by code (codes)',
    'batch': 'Every voucher of a batch (batch name)',
    'plan': 'Every active voucher of a plan (plan name)',
    'members': 'Every active member (or the member codes given)',
    'unsynced': 'Every voucher TapTap knows is not on its router yet or failed (Pending / Error)',
    'router_time': 'Time limits only: correct every limit-uptime that disagrees (one router or all)',
    'profiles': 'Plan profiles on the routers: create missing ones, correct devices (shared-users) and speed',
    'router': 'Full sync of one router (or all): read it again and send what TapTap has',
    'everything': 'All of the above, for one router or all',
}
MAX_VOUCHERS = 3000


class SyncError(ValueError):
    pass


def _names(v):
    if isinstance(v, str):
        v = re.split(r'[,\s]+', v)
    return [str(x).strip() for x in (v or []) if str(x).strip()]


def routers_of(business, router=''):
    if not str(router or '').strip() or str(router).strip().lower() in ('all', 'every', '*'):
        return list(business.routers.all())
    r = str(router).strip()
    hit = business.routers.filter(pk=int(r)).first() if r.isdigit() else business.routers.filter(name__iexact=r).first()
    if not hit:
        names = ', '.join(business.routers.values_list('name', flat=True)[:15])
        raise SyncError(f'No router “{r}”. Routers: {names or "none"}.')
    return [hit]


def resolve(business, scope, codes='', batch='', plan='', router=''):
    """The vouchers a scope means (TapTap's own and router-made). Raises SyncError with a readable reason."""
    qs = business.vouchers.filter(deleted_at__isnull=True).select_related('router', 'batch')
    rs = routers_of(business, router)
    if str(router or '').strip() and str(router).lower() not in ('all', 'every', '*'):
        qs = qs.filter(router__in=rs)
    if scope == 'voucher':
        want = _names(codes)
        if not want:
            raise SyncError('Which voucher? Give its code (or several, separated by commas).')
        found = list(qs.filter(code__iregex=r'^(' + '|'.join(map(re.escape, want)) + ')$'))
        missing = sorted({c.upper() for c in want} - {v.code.upper() for v in found})
        if missing:
            raise SyncError(f'No voucher or member {", ".join(missing)} in {business.business_name}.')
        return found
    if scope == 'batch':
        name = str(batch or '').strip()
        if not name:
            raise SyncError('Which batch? Give its name.')
        b = business.batches.filter(name__iexact=name).first() or (business.batches.filter(pk=int(name)).first() if name.isdigit() else None)
        if not b:
            recent = ', '.join(business.batches.order_by('-id').values_list('name', flat=True)[:10])
            raise SyncError(f'No batch “{name}”. Recent batches: {recent or "none"}.')
        return list(qs.filter(batch=b))
    if scope == 'plan':
        name = str(plan or '').strip()
        if not name:
            raise SyncError('Which plan? Give its name.')
        out = list(qs.filter(plan_name__iexact=name).exclude(status__in=['expired', 'archived']))
        if not out:
            raise SyncError(f'No active vouchers on a plan called “{name}”.')
        return out
    if scope == 'members':
        q = qs.filter(login_type='member').exclude(status__in=['expired', 'archived'])
        want = _names(codes)
        if want:
            q = q.filter(code__iregex=r'^(' + '|'.join(map(re.escape, want)) + ')$')
        return list(q)
    if scope == 'unsynced':
        return list(qs.filter(source='taptap', mikrotik_sync_status__in=['Pending', 'Error', '']).exclude(status__in=['expired', 'archived']))
    return []


# ─────────────────────────────── read only: what differs ───────────────────────────────

def _mirrors(vouchers):
    from .models import RouterHotspotUser
    ids = {v.router_id for v in vouchers if v.router_id}
    rows = RouterHotspotUser.objects.filter(router_id__in=ids)
    return {(m.router_id, m.username.upper()): m for m in rows}


def compare(v, mirror, plans=None):
    """Differences between TapTap and the router's last known row for one voucher ([] = in sync)."""
    from .router_time import acceptable, platform_owned
    from .utils import voucher_profile
    if not v.router_id:
        return ['no router assigned']
    if mirror is None or not mirror.is_present:
        if v.status in ('expired', 'archived'):
            return []
        return ['missing on the router'] if platform_owned(v) else ['not seen on the router']
    if not platform_owned(v):
        return []                                    # router-made: the router is the authority
    diffs = []
    want_off = v.status != 'active' or bool(v.frozen_at)
    if bool(mirror.disabled) != want_off:
        diffs.append('router has it ' + ('enabled' if not mirror.disabled else 'disabled') + f', TapTap says {"off" if want_off else "on"}')
    if v.status == 'active':
        plan = (plans or {}).get(v.plan_name)
        try:
            prof = voucher_profile(v, plan)[0]
            if prof and mirror.profile and mirror.profile != prof:
                diffs.append(f'profile {mirror.profile} on the router, TapTap wants {prof}')
        except Exception:
            pass
        if not acceptable(v, mirror.limit_uptime, mirror.uptime, plan):
            diffs.append(f'time limit {mirror.limit_uptime or "none"} (used {mirror.uptime or "0s"}) does not match TapTap')
    if v.mikrotik_sync_status == 'Error' and v.mikrotik_sync_error:
        diffs.append('last send failed: ' + v.mikrotik_sync_error[:120])
    return diffs


def status(business, scope='voucher', codes='', batch='', plan='', router='', limit=40):
    """For Fit: how many of the scope are in sync, and the ones that are not (with why)."""
    if scope in ('router', 'router_time', 'profiles', 'everything'):
        return routers_status(business, router)
    vs = resolve(business, scope, codes, batch, plan, router)[:MAX_VOUCHERS]
    mirrors = _mirrors(vs)
    plans = {p.name: p for p in business.plans.all()}
    bad = []
    for v in vs:
        d = compare(v, mirrors.get((v.router_id, v.code.upper())), plans)
        if d:
            bad.append({'code': v.code, 'router': v.router.name if v.router else None, 'status': v.status, 'differences': d})
    seen = {}
    for v in vs:
        if v.router_id and v.router_id not in seen:
            seen[v.router_id] = v.router
    info = routers_status(business, router, only=list(seen.values()))['routers'] if seen else []
    return {'checked': len(vs), 'in_sync': len(vs) - len(bad), 'out_of_sync': len(bad), 'examples': bad[:limit],
            'routers': info,
            'note': 'Compared with each router\'s last inventory; a full router sync refreshes that picture.'}


def routers_status(business, router='', only=None):
    from .models import AgentCommand
    out = []
    for r in (only if only is not None else routers_of(business, router)):
        job = r.sync_jobs.order_by('-created_at').first()
        out.append({'router': r.name, 'id': r.pk, 'online': r.status == 'Online', 'status': r.status,
                    'last_sync': timezone.localtime(job.finished_at or job.created_at).strftime('%d %b %H:%M') if job else None,
                    'last_sync_result': job.status if job else None,
                    'commands_waiting': AgentCommand.objects.filter(router=r, status__in=['queued', 'sent']).count(),
                    'commands_failed_24h': AgentCommand.objects.filter(router=r, status='failed',
                                                                       done_at__gte=timezone.now() - timedelta(hours=24)).count(),
                    'vouchers_pending': r.vouchers.filter(source='taptap', mikrotik_sync_status__in=['Pending', 'Queued', 'Error'])
                    .exclude(status__in=['expired', 'archived']).count()})
    return {'routers': out}


# ─────────────────────────────── doing it ───────────────────────────────

def _push(vouchers, user=None):
    """Members through the exact member alignment, TapTap vouchers through the normal push.
    Router-made vouchers cannot be overwritten: their routers are read again instead. Returns lines."""
    from .router_time import platform_owned
    from .voucher_push import push_vouchers
    lines, members, normal, router_made, skipped = [], [], [], set(), 0
    for v in vouchers:
        if not v.router_id:
            skipped += 1
        elif not platform_owned(v):
            router_made.add(v.router)
        elif v.status in ('expired', 'archived'):
            skipped += 1                                # the expiry pass keeps these off on the router
        elif v.is_member:
            members.append(v)
        else:
            normal.append(v)
        cache.delete(f'tt:rt:fix:{v.router_id}:{v.code.upper()}')      # let the time check run again at once
    if normal:
        for router, msg in push_vouchers(normal, user).items():
            lines.append(msg)
    if members:
        from .member_router_alignment import aligned_push_one
        ok = failed = 0
        for m in members[:500]:
            res = aligned_push_one(m)
            if res is True:
                ok += 1
            else:
                failed += 1
        lines.append(f'{ok} member{"s" if ok != 1 else ""} aligned on their router' + (f', {failed} failed (see the member page)' if failed else '')
                     + (f'; {len(members) - 500} more next time' if len(members) > 500 else ''))
    for r in router_made:
        lines.append(_full(r, user, why='router-made vouchers: reading the router again'))
    if skipped:
        lines.append(f'{skipped} skipped (no router, or expired — those stay switched off on the router)')
    return lines


def _full(router, user=None, why=''):
    from .linkops import refresh
    try:
        created = refresh(router, user)
        return f'{router.name}: full sync {"started" if created else "already running"}' + (f' ({why})' if why else '')
    except Exception as exc:  # noqa: BLE001
        return f'{router.name}: full sync not started — {exc}'


def _time(router):
    from . import router_time as rt
    from .linkops import uses_link
    try:
        if uses_link(router):
            return f'{router.name}: {rt.repair_link(router)} time limit(s) queued for correction'
        from .mikrotik import MikroTikService
        svc = MikroTikService(router).connect()
        try:
            rows = svc.resource('/ip/hotspot/user').get()
            return f'{router.name}: {rt.repair_api(router, svc, rows)} time limit(s) corrected'
        finally:
            svc.close()
    except Exception as exc:  # noqa: BLE001
        return f'{router.name}: time check failed — {exc}'


def _profiles(router, user=None):
    from .linkops import uses_link
    from .plan_limits import plan_profile
    plans = list(router.business.plans.filter(active=True))
    wanted = {}
    for p in plans:
        try:
            prof, shared, rate = plan_profile(p)
            wanted[prof] = (int(shared or 1), rate or '')
        except Exception:
            continue
    if not wanted:
        return f'{router.name}: no plan profiles to send'
    try:
        if uses_link(router):
            from .agent import queue
            from .voucher_push import set_profile_shared
            queue(router, 'hotspot_users', {'users': [], 'profiles': [{'name': k, 'shared': s, 'rate': r} for k, (s, r) in wanted.items()]},
                  label=f'Make sure {len(wanted)} plan profile(s) exist', user=user, minutes=60 * 24)
            for k, (s, _) in wanted.items():
                set_profile_shared(router, k, s, user=user)
            return f'{router.name}: {len(wanted)} plan profile(s) queued (devices and speed)'
        from .mikrotik import MikroTikService
        svc = MikroTikService(router).connect()
        try:
            for k, (s, r) in wanted.items():
                svc.ensure_hotspot_profile(k, s, r)
        finally:
            svc.close()
        return f'{router.name}: {len(wanted)} plan profile(s) checked and corrected'
    except Exception as exc:  # noqa: BLE001
        return f'{router.name}: profiles not sent — {exc}'


def run(business, scope, codes='', batch='', plan='', router='', user=None):
    """Carry out a sync. Returns a short report (one line per thing done)."""
    if scope not in SCOPES:
        raise SyncError(f'Sync what? One of: {", ".join(SCOPES)}.')
    lines = []
    if scope in ('voucher', 'batch', 'plan', 'members', 'unsynced'):
        vs = resolve(business, scope, codes, batch, plan, router)
        if not vs:
            return 'Nothing to send: no matching vouchers.'
        if len(vs) > MAX_VOUCHERS:
            raise SyncError(f'{len(vs)} vouchers is too many at once — sync a plan, a batch or one router at a time.')
        lines += _push(vs, user)
    else:
        rs = routers_of(business, router)
        if not rs:
            raise SyncError('This business has no routers.')
        for r in rs:
            if scope in ('profiles', 'everything'):
                lines.append(_profiles(r, user))
            if scope in ('router_time', 'everything'):
                lines.append(_time(r))
            if scope == 'everything':
                lines += _push(list(business.vouchers.filter(router=r, source='taptap', deleted_at__isnull=True)
                                    .exclude(status__in=['expired', 'archived']).select_related('router'))[:MAX_VOUCHERS], user)
            if scope in ('router', 'everything'):
                lines.append(_full(r, user))
    try:
        from .utils import log
        log(business, 'Sync', f'{SCOPES[scope].split(" (")[0]}: ' + ' · '.join(lines)[:400])
    except Exception:
        pass
    return ' · '.join(x for x in lines if x) or 'Done.'


def describe(business, scope, codes='', batch='', plan='', router=''):
    """One line for Fit's card: what will be sent where."""
    if scope in ('voucher', 'batch', 'plan', 'members', 'unsynced'):
        vs = resolve(business, scope, codes, batch, plan, router)
        from .router_time import platform_owned
        n_m = sum(1 for v in vs if v.is_member)
        n_r = sum(1 for v in vs if not platform_owned(v))
        n_x = sum(1 for v in vs if v.status in ('expired', 'archived'))
        routers = sorted({v.router.name for v in vs if v.router})
        what = {'voucher': ', '.join(v.code for v in vs[:8]) + ('…' if len(vs) > 8 else ''),
                'batch': f'batch {batch}', 'plan': f'plan {plan}', 'members': 'members', 'unsynced': 'unsent / failed vouchers'}[scope]
        return (len(vs), f'Sends TapTap\'s values for {len(vs)} voucher{"s" if len(vs) != 1 else ""} ({what}) to '
                         f'{", ".join(routers) or "their routers"}: profile, password, time limit, on/off.'
                         + (f' {n_m} member(s) use the exact member alignment.' if n_m else '')
                         + (f' {n_r} router-made: their router is read again instead.' if n_r else '')
                         + (f' {n_x} expired stay off.' if n_x else ''))
    rs = routers_of(business, router)
    names = ', '.join(r.name for r in rs)
    return (len(rs), {'router_time': f'Corrects every time limit that disagrees with TapTap on {names}.',
                      'profiles': f'Creates missing plan profiles and corrects devices/speed on {names}.',
                      'router': f'Full sync of {names}: reads the router again and sends what TapTap has.',
                      'everything': f'Profiles, time limits, every active TapTap voucher and member, then a full sync, on {names}.'}[scope])
