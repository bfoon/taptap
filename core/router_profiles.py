"""Hotspot user profiles on your MikroTiks, in one readable list.

A profile is the router's side of a plan: shared users (devices), speed, session length,
idle / keep-alive timers, MAC cookie. TapTap mirrors every router's
/ip/hotspot/user/profile on each sync (RouterHotspotProfile; Link routers through their
inventory). When a router has not been mirrored yet, the config snapshot is used instead.

For each profile this shows:
  * the TapTap plan it belongs to (same profile name, or a plan named like it),
  * whether TapTap made it (a TapTap plan's profile, or a "taptap-…" profile for one-off vouchers),
  * the price and validity Mikhmon / a comment keeps on it,
  * how many hotspot users (vouchers) on that router use it.
"""
from __future__ import annotations

from collections import Counter

from django.utils import timezone

from .durations import text as minutes_text
from .sync import _routeros_minutes, profile_price

YES = ('true', 'yes')


def _val(row, key):
    return str(row.get(key, row.get(key.replace('-', '_'), '')) or '').strip()


def _plans_index(business):
    """name (lowercase) -> plan, by profile name first, then plan name."""
    by_profile, by_name = {}, {}
    for p in business.plans.all():
        if p.mikrotik_profile_name:
            by_profile.setdefault(p.mikrotik_profile_name.lower(), p)
        by_name.setdefault(p.name.lower(), p)
    return by_profile, by_name


def _describe(row, router, business, idx, users):
    by_profile, by_name = idx
    name = _val(row, 'name')
    plan = by_profile.get(name.lower()) or by_name.get(name.lower())
    price, validity, price_from = profile_price(row, business.currency)
    session = _val(row, 'session-timeout')
    session_min = _routeros_minutes(session, 0) if session else 0
    made_by_taptap = name.lower().startswith('taptap-') or bool(plan and plan.source == 'taptap' and
                                                                (plan.mikrotik_profile_name or plan.name).lower() == name.lower())
    cookie = _val(row, 'add-mac-cookie').lower() in YES
    return {
        'router': router, 'name': name, 'id': _val(row, 'id') or _val(row, '.id'),
        'shared': _val(row, 'shared-users') or '1', 'rate': _val(row, 'rate-limit'),
        'session': session, 'session_text': minutes_text(session_min) if session_min else 'No limit',
        'idle': _val(row, 'idle-timeout') or 'none', 'keepalive': _val(row, 'keepalive-timeout') or '—',
        'mac_cookie': cookie, 'mac_cookie_timeout': _val(row, 'mac-cookie-timeout') if cookie else '',
        'pool': _val(row, 'address-pool'), 'comment': _val(row, 'comment'),
        'on_login': bool(_val(row, 'on-login')), 'on_logout': bool(_val(row, 'on-logout')),
        'price': price, 'validity': minutes_text(validity) if validity else '', 'price_from': price_from,
        'plan': plan, 'taptap': made_by_taptap, 'default': name.lower() == 'default',
        'users': users.get(name, 0),
    }


def gather(business, router=None, q=''):
    """[{router, captured_at, source, profiles:[...], count}] for every router (or one)."""
    from .models import RouterHotspotProfile, RouterHotspotUser
    routers = business.routers.all().order_by('name')
    if router is not None:
        routers = routers.filter(pk=router.pk)
    idx = _plans_index(business)
    q = (q or '').strip().lower()
    out = []
    for r in routers:
        users = Counter(RouterHotspotUser.objects.filter(router=r, is_present=True).values_list('profile', flat=True))
        mirrored = list(RouterHotspotProfile.objects.filter(router=r, is_present=True).order_by('name'))
        if mirrored:
            rows = [{**(m.raw_data or {}), 'name': m.name} for m in mirrored]
            captured, source = max(m.last_seen_at for m in mirrored), 'sync'
        else:
            try:
                snap = r.config_snapshot
            except Exception:       # never synced: no snapshot yet
                snap = None
            sec = (snap.sections or {}).get('HotSpot user profiles', {}) if snap else {}
            rows, captured, source = list(sec.get('rows') or []), (snap.captured_at if snap else None), 'snapshot' if sec else ''
        profiles = [_describe(row, r, business, idx, users) for row in rows if _val(row, 'name')]
        if q:
            profiles = [p for p in profiles if q in p['name'].lower() or q in (p['comment'] or '').lower() or
                        (p['plan'] and q in p['plan'].name.lower()) or q in (p['rate'] or '').lower()]
        profiles.sort(key=lambda p: (p['default'], not p['plan'], p['name'].lower()))
        out.append({'router': r, 'captured_at': captured, 'source': source, 'profiles': profiles,
                    'linked': sum(1 for p in profiles if p['plan']), 'unlinked': sum(1 for p in profiles if not p['plan'] and not p['default'])})
    return out


def plans_missing(business, groups):
    """Active TapTap plans whose profile is not on the listed routers yet (TapTap creates it with the first voucher it sends)."""
    seen = {p['name'].lower() for g in groups for p in g['profiles']}
    if not any(g['profiles'] for g in groups):
        return []
    return [p for p in business.plans.filter(active=True).order_by('name')
            if (p.mikrotik_profile_name or p.name).lower() not in seen]


def find_row(router, name):
    """The profile's row (mirror first, then the config snapshot), or None."""
    from .models import RouterHotspotProfile
    m = RouterHotspotProfile.objects.filter(router=router, name=name, is_present=True).first()
    if m:
        return {**(m.raw_data or {}), 'name': m.name}
    try:
        rows = (router.config_snapshot.sections or {}).get('HotSpot user profiles', {}).get('rows') or []
    except Exception:
        rows = []
    return next((r for r in rows if _val(r, 'name') == name), None)
