"""Router-made hotspot users that never expire → give them their plan's time limit.

Security reports "N hotspot user(s) never expire": users made on the MikroTik (WinBox, Mikhmon…)
without ``limit-uptime``. Each user has a profile; the profile matches a TapTap plan (by the plan's
MikroTik profile name, else its name). The fix sets the user's ``limit-uptime`` on the router from
that plan — a 24 h plan gets ``1d`` — and gives the TapTap voucher the same duration, so the strict
expiry (core/expiry.py) switches it off when its time is up.

* **Manual** — Security → "Fix with plan time": a preview per profile, then apply. Profiles with no
  TapTap plan (or an unlimited plan) are listed; the owner can type a time for them or leave them.
* **Automatic** — ``Business.auto_plan_limits``: new router users without a limit get their plan's
  time as soon as the live pass sees them (API routers every pass, TapTap Link once a minute).

Users who already used more than their plan's time are switched off by the router at their next
login and by TapTap's strict expiry — the preview counts them so nobody is surprised.
"""
from __future__ import annotations

import logging
import re
from collections import OrderedDict
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from .durations import parse_routeros, routeros, text as minutes_text

logger = logging.getLogger('taptap.plan_limits')
SKIP = {'admin', 'default-trial'}
LIM_RE = re.compile(r'^(\d+w)?(\d+d)?(\d+h)?(\d+m)?(\d+s)?$')
MAX_MINUTES = 366 * 24 * 60
LINK_CHUNK = 60


def plan_index(business):
    out = {}
    for p in business.plans.all():
        if p.mikrotik_profile_name:
            out.setdefault(p.mikrotik_profile_name.lower(), p)
        out.setdefault(p.name.lower(), p)
    return out


def unlimited_users(router):
    """Router-made users with no time limit (same rule as the Security finding)."""
    return (router.hotspot_users.filter(is_present=True, disabled=False, limit_uptime__in=['', '0s', '0'], source='mikrotik')
            .exclude(username__in=SKIP)
            .exclude(username__in=router.business.vouchers.filter(duration_minutes=0, expires_at__isnull=True).values('code')))


def preview(router):
    """Per profile: the plan, the limit it gives, the users, and how many already used more than that."""
    plans = plan_index(router.business)
    groups = OrderedDict()
    for u in unlimited_users(router).order_by('profile', 'username'):
        prof = u.profile or 'default'
        g = groups.get(prof)
        if g is None:
            plan = plans.get(prof.lower())
            minutes = plan.duration_minutes if plan and plan.duration_minutes else 0
            g = groups[prof] = {'profile': prof, 'plan': plan.name if plan else '', 'minutes': minutes,
                                'limit': routeros(minutes) if minutes else '', 'label': minutes_text(minutes) if minutes else '',
                                'why': '' if minutes else ('Its plan is unlimited' if plan else 'No TapTap plan uses this profile'),
                                'users': [], 'over': 0}
        g['users'].append(u.username)
        if g['minutes'] and (parse_routeros(u.uptime, 0) or 0) >= g['minutes']:
            g['over'] += 1
    return list(groups.values())


def clean_minutes(value):
    try:
        m = int(value)
    except (TypeError, ValueError):
        return 0
    return m if 0 < m <= MAX_MINUTES else 0


def apply(router, user=None, by='user', overrides=None, profiles=None):
    """Set limit-uptime on every never-expiring user that has a plan time (or a time typed by the owner).

    overrides: {profile: minutes} for profiles without a usable plan. profiles: only these (None = all).
    Returns {'set': n, 'skipped': n, 'via': ..., 'message': ...}."""
    from .models import RouterConfigChange, RouterHotspotUser, Voucher
    from .voucher_history import channel, record
    overrides = {k: clean_minutes(v) for k, v in (overrides or {}).items() if clean_minutes(v)}
    plan = []          # (username, minutes)
    skipped = 0
    for g in preview(router):
        if profiles is not None and g['profile'] not in profiles:
            continue
        minutes = overrides.get(g['profile']) or g['minutes']
        if not minutes:
            skipped += len(g['users']); continue
        plan += [(name, minutes) for name in g['users']]
    via = channel(router)
    if not plan:
        return {'set': 0, 'skipped': skipped, 'via': via, 'message': 'Nothing to change — no user has a plan time to apply.'}
    if via == 'TapTap Link':
        from .linkops import send
        for i in range(0, len(plan), LINK_CHUNK):
            part = plan[i:i + LINK_CHUNK]
            send(router, 'hotspot_users_limit', {'users': [{'n': n, 'lim': routeros(m)} for n, m in part]},
                 label=f'Give {len(part)} router user{"s" if len(part) != 1 else ""} their plan time', user=user)
        result = 'Queued — the router applies it at its next check-in'
    else:
        from .mikrotik import MikroTikService
        svc = MikroTikService(router).connect()
        try:
            res = svc.resource('/ip/hotspot/user')
            ids = {str(r.get('name', '')): r.get('id') for r in res.get()}
            done = []
            for name, minutes in plan:
                if ids.get(name):
                    res.set(id=ids[name], limit_uptime=routeros(minutes))
                    done.append((name, minutes))
            plan = done
        finally:
            svc.close()
        result = 'Applied on the router'
    now = timezone.now()
    for name, minutes in plan:
        lim = routeros(minutes)
        RouterHotspotUser.objects.filter(router=router, username=name).update(limit_uptime=lim)
        v = Voucher.objects.filter(business=router.business, code__iexact=name).first()
        if v and v.source == 'mikrotik':
            upd = {}
            if v.duration_minutes != minutes:
                upd['duration_minutes'] = minutes
            if v.used_at and (upd or not v.expires_at):
                upd['expires_at'] = v.used_at + timedelta(minutes=minutes)
            if upd:
                Voucher.objects.filter(pk=v.pk).update(**upd)
            record(v, 'note', user=user, source='auto' if by == 'auto' else 'user', via=via, router_result=result,
                   text=f'Plan time set on the router: {minutes_text(minutes)} (limit-uptime {lim})')
    RouterConfigChange.objects.create(business=router.business, router=router, actor=user if getattr(user, 'is_authenticated', False) else None,
                                      resource_path='/ip/hotspot/user', operation='plan-limits', target_id=f'{len(plan)} users',
                                      fields={'users': len(plan), 'via': via, 'by': by}, status='success')
    from .live import push_event
    push_event(router.business_id, f'{len(plan)} router user{"s" if len(plan) != 1 else ""} on {router.name} now have their plan time', 'fix')
    return {'set': len(plan), 'skipped': skipped, 'via': via, 'at': now.isoformat(),
            'message': f'{len(plan)} user{"s" if len(plan) != 1 else ""} now expire with their plan ({result.lower()}).'
                       + (f' {skipped} left unlimited (no plan time).' if skipped else '')}


def auto(router):
    """Automatic mode: apply plan times to new never-expiring users (at most once a minute per router)."""
    if not getattr(router.business, 'auto_plan_limits', False):
        return None
    if not cache.add(f'tt:planlim:{router.pk}', 1, 60):
        return None
    if not unlimited_users(router).exists():
        return None
    try:
        return apply(router, by='auto')
    except Exception as exc:
        logger.info('auto plan limits on %s: %s', router.name, exc)
        return None
