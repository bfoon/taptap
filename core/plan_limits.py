"""Router-made hotspot users that never expire → give them their plan's time limit.

Security reports "N hotspot user(s) never expire": users made on the MikroTik (WinBox, Mikhmon…)
without ``limit-uptime``. Each user has a profile; the profile matches a TapTap plan (by the plan's
MikroTik profile name, else its name). The fix sets the user's ``limit-uptime`` on the router from
that plan — a 24 h plan gets ``1d`` — and gives the TapTap voucher the same duration, so the strict
expiry (core/expiry.py) switches it off when its time is up.

* **Manual** — Security → "Fix with plan": a preview per profile, and for each profile the owner picks the
  TapTap plan those users belong to (pre-selected when the profile already belongs to one). Applying a plan
  corrects them completely: on the router the plan's time (limit-uptime) and the plan's profile (created if
  missing, so speed and devices match too); in TapTap the vouchers take the plan.
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
            g = groups[prof] = {'profile': prof, 'plan': plan.name if plan else '', 'plan_id': plan.pk if plan and minutes else None, 'minutes': minutes,
                                'limit': routeros(minutes) if minutes else '', 'label': minutes_text(minutes) if minutes else '',
                                'why': '' if minutes else ('Its plan is unlimited' if plan else 'No TapTap plan uses this profile'),
                                'users': [], 'over': 0, 'used_minutes': []}
        g['users'].append(u.username)
        g['used_minutes'].append(int((parse_routeros(u.uptime, 0) or 0)))
        if g['minutes'] and (parse_routeros(u.uptime, 0) or 0) >= g['minutes']:
            g['over'] += 1
    return list(groups.values())


def plan_choices(business):
    """Plans that can fix "never expire": the ones with a time (an unlimited plan would leave them unlimited)."""
    from .durations import text as mtext
    return [{'id': p.pk, 'name': p.name, 'minutes': p.duration_minutes, 'label': mtext(p.duration_minutes), 'price': str(p.price),
             'devices': p.max_devices} for p in business.plans.filter(duration_minutes__gt=0).order_by('duration_minutes', 'name')]


def plan_profile(plan):
    """(profile, shared-users, rate-limit) TapTap uses on routers for this plan."""
    from .models import Voucher
    from .utils import voucher_profile
    return voucher_profile(Voucher(business=plan.business, plan_name=plan.name, duration_minutes=plan.duration_minutes,
                                   max_devices=plan.max_devices, price=plan.price), plan)


def clean_minutes(value):
    try:
        m = int(value)
    except (TypeError, ValueError):
        return 0
    return m if 0 < m <= MAX_MINUTES else 0


def apply(router, user=None, by='user', overrides=None, profiles=None, plans=None):
    """Fix every never-expiring user with its plan.

    plans: {profile: plan_id} chosen by the owner — those users get the plan's time AND the plan's profile, and
    their TapTap vouchers take the plan. Profiles not listed keep the plan they already match (time only).
    overrides: {profile: minutes} (older callers). profiles: only these (None = all).
    Returns {'set': n, 'skipped': n, 'via': ..., 'message': ...}."""
    from .models import RouterConfigChange, RouterHotspotUser, Voucher
    from .voucher_history import channel, record
    overrides = {k: clean_minutes(v) for k, v in (overrides or {}).items() if clean_minutes(v)}
    chosen = {}
    for prof, pid in (plans or {}).items():
        p = router.business.plans.filter(pk=pid, duration_minutes__gt=0).first() if str(pid or '').isdigit() else None
        if p:
            chosen[prof] = p
    plan = []          # (username, minutes, plan or None, profile to move to or '')
    new_profiles = {}  # profile -> (shared, rate) to make sure exists on the router
    skipped = 0
    for g in preview(router):
        if profiles is not None and g['profile'] not in profiles:
            continue
        p = chosen.get(g['profile'])
        if p:
            prof, shared, rate = plan_profile(p)
            new_profiles[prof] = (shared, rate)
            plan += [(name, p.duration_minutes, p, prof if prof != g['profile'] else '') for name in g['users']]
            continue
        minutes = overrides.get(g['profile']) or g['minutes']
        if not minutes:
            skipped += len(g['users']); continue
        plan += [(name, minutes, None, '') for name in g['users']]
    via = channel(router)
    if not plan:
        return {'set': 0, 'skipped': skipped, 'via': via, 'message': 'Nothing to change — choose a plan for at least one profile.'}
    if via == 'TapTap Link':
        from .linkops import send
        for i in range(0, len(plan), LINK_CHUNK):
            part = plan[i:i + LINK_CHUNK]
            users = [{'n': n, 'lim': routeros(m), **({'prof': prof} if prof else {})} for n, m, _, prof in part]
            used = {u['prof'] for u in users if u.get('prof')}
            send(router, 'hotspot_users_limit', {'users': users, 'profiles': [{'name': k, 'shared': int(new_profiles[k][0] or 1),
                                                                                'rate': new_profiles[k][1] or ''} for k in used]},
                 label=f'Give {len(part)} router user{"s" if len(part) != 1 else ""} their plan', user=user)
        result = 'Queued — the router applies it at its next check-in'
    else:
        from .mikrotik import MikroTikService
        svc = MikroTikService(router).connect()
        try:
            for prof, (shared, rate) in new_profiles.items():
                svc.ensure_hotspot_profile(prof, shared, rate)        # the plan's profile, created if missing
            res = svc.resource('/ip/hotspot/user')
            ids = {str(r.get('name', '')): r.get('id') for r in res.get()}
            done = []
            for name, minutes, p, prof in plan:
                if ids.get(name):
                    res.set(id=ids[name], limit_uptime=routeros(minutes), **({'profile': prof} if prof else {}))
                    done.append((name, minutes, p, prof))
            plan = done
        finally:
            svc.close()
        result = 'Applied on the router'
    now = timezone.now()
    for name, minutes, p, prof in plan:
        lim = routeros(minutes)
        RouterHotspotUser.objects.filter(router=router, username=name).update(limit_uptime=lim, **({'profile': prof} if prof else {}))
        v = Voucher.objects.filter(business=router.business, code__iexact=name).first()
        if not v:
            continue
        upd = {}
        if p is not None:                       # the chosen plan: the voucher becomes a voucher of that plan
            upd.update(plan_name=p.name, router_profile='')
            if not v.used_at:
                upd.update(max_devices=p.max_devices, **({'price': p.price} if not v.price else {}))
        if v.source == 'mikrotik' or p is not None:
            if v.duration_minutes != minutes:
                upd['duration_minutes'] = minutes
            if v.used_at and ('duration_minutes' in upd or not v.expires_at):
                upd['expires_at'] = v.used_at + timedelta(minutes=minutes)
        if upd:
            Voucher.objects.filter(pk=v.pk).update(**upd)
        record(v, 'note', user=user, source='auto' if by == 'auto' else 'user', via=via, router_result=result,
               text=(f'Plan {p.name}: ' if p else 'Plan time set on the router: ') + f'{minutes_text(minutes)} (limit-uptime {lim})'
                    + (f', profile {prof}' if prof else ''))
    RouterConfigChange.objects.create(business=router.business, router=router, actor=user if getattr(user, 'is_authenticated', False) else None,
                                      resource_path='/ip/hotspot/user', operation='plan-limits', target_id=f'{len(plan)} users',
                                      fields={'users': len(plan), 'via': via, 'by': by}, status='success')
    from .live import push_event
    push_event(router.business_id, f'{len(plan)} router user{"s" if len(plan) != 1 else ""} on {router.name} now have their plan time', 'fix')
    return {'set': len(plan), 'skipped': skipped, 'via': via, 'at': now.isoformat(),
            'message': f'{len(plan)} user{"s" if len(plan) != 1 else ""} now have their plan and expire with it ({result.lower()}).'
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
