"""Profiles decide the length of a voucher — and TapTap and MikroTik must always agree.

* **How long a profile lasts** — :func:`profile_minutes` reads, in order: the profile's
  session-timeout, the Mikhmon validity in its on-login script, a "validity:" note in its comment,
  and finally its name ("24H", "1-Day", "30DAYS", "Weekly", "Monthly", "3hrs" …). A profile that
  says 24 hours is never treated as "no time limit".
* :func:`audit` — what is out of line for a business:
    - plans whose length differs from their router profile,
    - vouchers with no time limit although their profile has one,
    - TapTap vouchers whose router copy is on a different profile than TapTap says,
    - vouchers whose profile was deleted on the router.
* :func:`apply` — fixes all of it: plans and vouchers get the profile's length in TapTap, and
  TapTap's own vouchers are re-sent so the router has the same profile and limit-uptime.
  A voucher already in use keeps its start time — only its end is set.
* :func:`reconcile_router` — run after every sync: a TapTap voucher whose router copy drifted to
  another profile (changed in WinBox, Mikhmon, a restore…) is put back on TapTap's profile.
"""
from __future__ import annotations

import logging
import re

from django.db.models import Q

logger = logging.getLogger('taptap.profiles')

UNIT_MIN = {'m': 1, 'min': 1, 'mins': 1, 'minute': 1, 'minutes': 1, 'mn': 1,
            'h': 60, 'hr': 60, 'hrs': 60, 'hour': 60, 'hours': 60, 'jam': 60,
            'd': 1440, 'day': 1440, 'days': 1440, 'dy': 1440, 'hari': 1440,
            'w': 10080, 'wk': 10080, 'wks': 10080, 'week': 10080, 'weeks': 10080,
            'mo': 43200, 'mon': 43200, 'month': 43200, 'months': 43200, 'mth': 43200, 'mths': 43200, 'bulan': 43200}
WORDS = {'hourly': 60, 'daily': 1440, 'day': 1440, 'weekly': 10080, 'week': 10080, 'monthly': 43200, 'month': 43200,
         'yearly': 525600, 'annual': 525600}
NUM_UNIT = re.compile(r'(\d+(?:[.,]\d+)?)\s*[-_ ]?\s*(minutes|minute|mins|min|mn|hours|hour|hrs|hr|jam|h|days|day|dy|hari|d|weeks|week|wks|wk|w|months|month|mths|mth|mon|mo|bulan)(?![a-z])', re.I)
NO_LIMIT = re.compile(r'\b(unlimited|unlim|no[\s_-]?limit|vip|staff|admin|default)\b', re.I)


def name_minutes(text):
    """Length a profile name or note implies, in minutes (0 = says nothing)."""
    t = str(text or '').strip()
    if not t or NO_LIMIT.search(t):
        return 0
    m = NUM_UNIT.search(t)
    if m:
        n = float(m.group(1).replace(',', '.'))
        return int(round(n * UNIT_MIN[m.group(2).lower()]))
    low = re.sub(r'[^a-z]', ' ', t.lower())
    for w, minutes in WORDS.items():
        if re.search(rf'\b{w}\b', low):
            return minutes
    return 0


def profile_minutes(row):
    """(minutes, where) — how long a router profile lasts. row = the profile's RouterOS fields."""
    from .durations import parse_routeros
    from .sync import parse_mikhmon
    row = row or {}
    st = parse_routeros(row.get('session-timeout') or row.get('session_timeout') or '', 0) or 0
    if st:
        return st, 'session-timeout'
    try:
        _, validity = parse_mikhmon(row.get('on-login') or row.get('on_login') or '')
    except Exception:
        validity = 0
    if validity:
        return int(validity), 'Mikhmon validity'
    note = str(row.get('comment') or '')
    m = re.search(r'validity\s*[:=]\s*([0-9a-z .-]+)', note, re.I)
    if m:
        v = parse_routeros(m.group(1).strip().replace(' ', ''), 0) or name_minutes(m.group(1))
        if v:
            return v, 'comment'
    v = name_minutes(row.get('name'))
    return (v, 'name') if v else (0, '')


DEV_RE = re.compile(r'(\d{1,3})\s*[-_ ]?\s*(devices?|devs?|users?|pax|persons?|people|sharing|shared|phones?)(?![a-z])', re.I)


def name_devices(name):
    """Devices a profile name implies ("mywifi-10-devices", "FAMILY 5 USERS"), 0 = says nothing."""
    m = DEV_RE.search(str(name or ''))
    return int(m.group(1)) if m and 0 < int(m.group(1)) <= 500 else 0


def profile_devices(router, profile_name, default=1):
    """shared-users of a router profile (how many devices one voucher may use)."""
    from .models import RouterHotspotProfile
    if router is None or not profile_name:
        return default
    p = RouterHotspotProfile.objects.filter(router=router, name=profile_name, is_present=True).first()
    if not p:
        return default
    try:
        return max(1, int(p.shared_users or (p.raw_data or {}).get('shared-users') or default))
    except (TypeError, ValueError):
        return default


def expected_devices(voucher, plans, profiles, mirror):
    """How many devices a voucher should allow, or None when TapTap cannot tell.
    TapTap vouchers: their plan (or the profile chosen by hand). Router-made vouchers: the router
    profile they are on (the router enforces its shared-users)."""
    def shared(rid, name):
        p = profiles.get((rid, name))
        try:
            return max(1, int(p.shared_users)) if p is not None and p.shared_users else None
        except (TypeError, ValueError):
            return None
    if voucher.router_profile and shared(voucher.router_id, voucher.router_profile):
        return shared(voucher.router_id, voucher.router_profile)
    plan = plans.get(voucher.plan_name)
    if voucher.source == 'taptap':
        return plan.max_devices if plan is not None else None
    have = mirror.get((voucher.router_id, voucher.code.upper()))
    return shared(voucher.router_id, have) if have else (plan.max_devices if plan is not None else None)


def _profiles(business):
    """{(router_id, name): RouterHotspotProfile} of profiles present on the routers."""
    from .models import RouterHotspotProfile
    return {(p.router_id, p.name): p for p in RouterHotspotProfile.objects.filter(business=business, is_present=True)}


def _row(p):
    return {**(p.raw_data or {}), 'name': p.name,
            **({'session-timeout': p.session_timeout} if getattr(p, 'session_timeout', '') else {})}


def profile_state(voucher, profiles=None):
    """('ok' | 'deleted' | 'unknown', profile name) for a voucher's profile on its router."""
    from .models import RouterHotspotUser
    if not voucher.router_id:
        return 'unknown', ''
    u = RouterHotspotUser.objects.filter(router_id=voucher.router_id, username__iexact=voucher.code).first()
    name = (u.profile if u else '') or voucher.router_profile or ''
    if not name:
        return 'unknown', ''
    profiles = profiles if profiles is not None else _profiles(voucher.business)
    any_seen = any(k[0] == voucher.router_id for k in profiles)
    if not any_seen:
        return 'unknown', name                  # router's profiles not read yet
    return ('ok' if (voucher.router_id, name) in profiles else 'deleted'), name


def expected_profile(voucher, plans=None):
    from .utils import voucher_profile
    plan = (plans or {}).get(voucher.plan_name) if plans is not None else voucher.business.plans.filter(name=voucher.plan_name).first()
    return voucher_profile(voucher, plan)[0]


def audit(business):
    from .models import RouterHotspotUser
    profiles = _profiles(business)
    plans = {p.name: p for p in business.plans.all()}
    out = {'plans': [], 'vouchers': [], 'drift': [], 'deleted': [], 'devices': []}
    # 1) plans vs their profile
    for p in plans.values():
        pname = p.mikrotik_profile_name or p.name
        rows = [prof for (rid, n), prof in profiles.items() if n == pname]
        if not rows or (p.is_free and not p.duration_minutes):
            continue                         # free + unlimited plans (staff, members) stay as you set them
        mins = {profile_minutes(_row(r)) for r in rows}
        best = max(mins, key=lambda x: x[0])
        if best[0] and best[0] != (p.duration_minutes or 0):
            out['plans'].append({'plan': p, 'profile': pname, 'minutes': best[0], 'where': best[1]})
    fixed_len = {x['plan'].name: x['minutes'] for x in out['plans']}
    # 1c) plans allowing a different number of devices than their router profile
    out['plan_devices'] = []
    for p in plans.values():
        pname = p.mikrotik_profile_name or p.name
        for (rid, n), prof in profiles.items():
            if n == pname and (prof.shared_users or 1) != (p.max_devices or 1) and not (p.is_free and not p.duration_minutes):
                out['plan_devices'].append({'plan': p, 'router': prof.router, 'profile': n, 'profile_allows': prof.shared_users or 1})
                break
    # 2) vouchers with no time limit although their profile has one
    mirror = {(u.router_id, u.username.upper()): u.profile for u in
              RouterHotspotUser.objects.filter(business=business, is_present=True).only('router_id', 'username', 'profile')}
    for v in (business.vouchers.filter(duration_minutes=0, expires_at__isnull=True, status='active')
              .exclude(login_type='member').select_related('router')[:5000]):
        plan = plans.get(v.plan_name)
        if plan is not None and plan.is_free and not plan.duration_minutes and v.plan_name not in fixed_len:
            continue                         # on a free + unlimited plan on purpose
        mins = fixed_len.get(v.plan_name) or (plan.duration_minutes if plan is not None else 0)
        where = f'plan {v.plan_name}'
        if not mins:
            pname = mirror.get((v.router_id, v.code.upper())) or v.router_profile or v.plan_name
            prof = profiles.get((v.router_id, pname))
            mins, where = profile_minutes(_row(prof)) if prof else (name_minutes(pname), 'profile name')
        if mins:
            out['vouchers'].append({'voucher': v, 'minutes': mins, 'where': where})
    # 1b) router profiles whose name says N devices but which allow a different number (shared-users)
    out['profile_devices'] = []
    for (rid, name), prof in profiles.items():
        n = name_devices(name)
        if n and n != (prof.shared_users or 1):
            out['profile_devices'].append({'router': prof.router, 'profile': name, 'name_says': n, 'allows': prof.shared_users or 1})
    renamed = {(x['router'].pk, x['profile']): x['name_says'] for x in out['profile_devices']}
    for k, n in renamed.items():
        if k in profiles:
            profiles[k].shared_users = n           # judge vouchers by what the profile will allow once fixed
    # 2b) vouchers allowing a different number of devices than their plan / profile (e.g. 1 instead of 10)
    for v in business.vouchers.filter(status='active').exclude(login_type='member').select_related('router')[:20000]:
        want = expected_devices(v, plans, profiles, mirror)
        if want and want != (v.max_devices or 1):
            out['devices'].append({'voucher': v, 'devices': want})
    # 3) TapTap vouchers whose router copy is on another profile; 4) deleted profiles
    for v in business.vouchers.filter(router__isnull=False, status='active').exclude(login_type='member').select_related('router')[:20000]:
        have = mirror.get((v.router_id, v.code.upper()))
        if have is None:
            continue
        if any(k[0] == v.router_id for k in profiles) and (v.router_id, have) not in profiles:
            out['deleted'].append({'voucher': v, 'profile': have})
        if v.source == 'taptap':
            want = expected_profile(v, plans)
            if want and have != want:
                out['drift'].append({'voucher': v, 'router_has': have, 'taptap_says': want})
    return out


def apply(business, user=None):
    """Fix everything audit() found. Returns a summary dict."""
    from .durations import best_unit
    from .voucher_history import record
    from .voucher_push import push_vouchers
    a = audit(business)
    from .voucher_push import set_profile_shared
    shared_msgs = []
    for x in a.get('profile_devices', []):
        ok, m = set_profile_shared(x['router'], x['profile'], x['name_says'], user)
        shared_msgs.append(m)
        if ok:
            business.plans.filter(mikrotik_profile_name=x['profile']).update(max_devices=x['name_says'])
            business.plans.filter(name=x['profile'], mikrotik_profile_name='').update(max_devices=x['name_says'])
    if a.get('profile_devices'):
        a = audit(business)
    # plans vs their profile's devices: router-made plans follow the router; TapTap's plans set the router profile
    for x in a.get('plan_devices', []):
        p = x['plan']
        if p.source == 'mikrotik':
            type(p).objects.filter(pk=p.pk).update(max_devices=x['profile_allows'])
        else:
            ok, m = set_profile_shared(x['router'], x['profile'], p.max_devices or 1, user)
            shared_msgs.append(m)
    if a.get('plan_devices'):
        a = audit(business)
    for x in a['plans']:
        p = x['plan']
        p.duration_minutes, p.duration_unit = x['minutes'], best_unit(x['minutes'])
        p.save(update_fields=['duration_minutes', 'duration_unit'])
    to_push = []
    for x in audit(business)['vouchers'] if a['plans'] else a['vouchers']:
        v = x['voucher']
        v.duration_minutes = x['minutes']
        v.save(update_fields=['duration_minutes'])
        record(v, 'note', user=user, text=f'Length set from its profile: {x["minutes"]} min ({x["where"]})'
               + (' — in use: start time kept, end time now set' if v.used_at else ''))
        if v.source == 'taptap':
            to_push.append(v)
    for x in a['devices']:
        v = x['voucher']
        before = v.max_devices
        type(v).objects.filter(pk=v.pk).update(max_devices=x['devices'])
        v.max_devices = x['devices']
        record(v, 'note', user=user, text=f'Devices allowed corrected: {before} → {x["devices"]} (from its plan / profile)')
        if v.source == 'taptap':
            to_push.append(v)
    for x in a['drift']:
        to_push.append(x['voucher'])
    seen, uniq = set(), []
    for v in to_push:
        if v.pk not in seen:
            seen.add(v.pk); uniq.append(v)
    msgs = push_vouchers(uniq, user) if uniq else {}
    return {'plans': len(a['plans']), 'vouchers': len(a['vouchers']), 'drift': len(a['drift']), 'deleted': len(a['deleted']), 'devices': len(a['devices']),
            'profile_devices': len(shared_msgs),
            'pushed': len(uniq), 'messages': list(msgs.values())}


def reconcile_router(router):
    """After a sync: put drifted TapTap vouchers of this router back on TapTap's profile."""
    from .models import RouterHotspotUser
    from .voucher_push import push_vouchers
    plans = {p.name: p for p in router.business.plans.all()}
    mirror = {u.username.upper(): u.profile for u in RouterHotspotUser.objects.filter(router=router, is_present=True).only('username', 'profile')}
    drifted = []
    for v in router.vouchers.filter(source='taptap', status='active').exclude(login_type='member'):
        have = mirror.get(v.code.upper())
        if have is not None and have != expected_profile(v, plans):
            drifted.append(v)
    if drifted:
        logger.info('%s: %d voucher(s) on the wrong profile, re-sending', router, len(drifted))
        push_vouchers(drifted)
    # a profile chosen by hand in TapTap wins on the router too — also for vouchers made on the router
    from .voucher_push import set_router_profile
    moved = 0
    for v in router.vouchers.exclude(router_profile='').exclude(source='taptap').filter(status='active'):
        have = mirror.get(v.code.upper())
        if have is not None and have != v.router_profile:
            ok, _ = set_router_profile(v, v.router_profile, v.max_devices or 1)
            moved += 1 if ok else 0
    return len(drifted) + moved
