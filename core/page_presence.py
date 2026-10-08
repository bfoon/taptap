"""Who else is on this page right now — avatars next to the page title.

Pages: an agent, a voucher, a plan, the members list and a member's page. Each open tab sends a small heartbeat
(every 15 s, slower when the tab is hidden) and gets back everyone in the same business looking at the same record.
A viewer disappears 45 s after their last heartbeat, or at once when they leave the page.

Stored in the cache (Redis in production), never in the database: user id, display name, role, when they opened
the page, last heartbeat, and whether they are typing in a form on it ("editing") or the tab is in the background
("away"). Nothing from the page itself is stored. Records are scoped by business, so staff of one business never see
another's.
"""
from __future__ import annotations

import hashlib
import time

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.http import require_POST

KINDS = {'agent': 'agent_detail', 'voucher': 'voucher_detail', 'plan': 'plan_detail', 'members': 'members', 'member': 'member_detail'}
ACTIVE_SECONDS = 45
CACHE_TTL = 120
STATES = {'viewing', 'editing', 'away'}
COLORS = ['#1769e0', '#0f9d58', '#d93025', '#8e24aa', '#f29900', '#00897b', '#5e35b1', '#c2185b', '#3949ab', '#6d4c41']


def _key(business_id, kind, pk):
    return f'tt:pp:{business_id}:{kind}:{pk}'


def _page(request):
    """(kind, id) from the request — only for a page this person may open."""
    kind = str(request.POST.get('kind') or '')
    raw = str(request.POST.get('id') or '0')
    if kind not in KINDS or not raw.isdigit() or len(raw) > 12:
        return None
    perms = getattr(request, 'tt_perms', None)
    if perms is not None and getattr(request, 'tt_member', None) is not None:
        from .permissions import allowed
        if not allowed(perms, KINDS[kind]):
            return None
    return kind, int(raw)


def display_name(user):
    full = (user.get_full_name() or '').strip()
    if full:
        return full
    base = (user.email or user.username or 'Staff').split('@')[0]
    return base.replace('.', ' ').replace('_', ' ').strip().title() or 'Staff'


def initials(name):
    parts = [p for p in name.replace('-', ' ').split() if p[:1].isalnum()]
    if not parts:
        return '?'
    return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else '')).upper()


def color_for(user_id):
    return COLORS[int(hashlib.md5(str(user_id).encode()).hexdigest(), 16) % len(COLORS)]


def role_label(request):
    business = request.user.business
    if business and business.user_id == request.user.pk:
        return 'Owner'
    from .permissions import ROLES
    role = getattr(request, 'tt_role', None)
    if role in ROLES:
        return ROLES[role][0]
    member = getattr(request, 'tt_member', None)
    if member is not None:
        try:
            return member.get_role_display()
        except Exception:
            pass
    return 'Staff'


def _viewers(key, now):
    rows = cache.get(key) or {}
    return {uid: r for uid, r in rows.items() if now - r.get('seen', 0) <= ACTIVE_SECONDS}


def _public(rows, me):
    out = []
    for uid, r in rows.items():
        out.append({'id': uid, 'name': r['name'], 'initials': r['initials'], 'color': r['color'], 'role': r['role'],
                    'state': r['state'], 'since': int(r['since']), 'you': uid == me, 'tabs': len(r.get('tabs', {}))})
    out.sort(key=lambda r: (r['you'], r['since']))                         # others first, longest-present first
    return out


@login_required
@require_POST
def ping(request):
    business = request.user.business
    page = _page(request)
    if not business or not page:
        return JsonResponse({'ok': False}, status=400)
    now = time.time()
    me = request.user.pk
    key = _key(business.pk, *page)
    state = request.POST.get('state', 'viewing')
    state = state if state in STATES else 'viewing'
    tab = str(request.POST.get('tab') or '')[:16]
    rows = _viewers(key, now)
    mine = rows.get(me) or {'since': now, 'tabs': {}}
    tabs = {t: s for t, s in (mine.get('tabs') or {}).items() if now - s.get('seen', 0) <= ACTIVE_SECONDS}
    tabs[tab or 'x'] = {'seen': now, 'state': state}
    # Several tabs: show the most "present" state (editing > viewing > away).
    best = 'editing' if any(t['state'] == 'editing' for t in tabs.values()) else (
        'viewing' if any(t['state'] == 'viewing' for t in tabs.values()) else 'away')
    name = display_name(request.user)
    rows[me] = {'name': name, 'initials': initials(name), 'color': color_for(me), 'role': role_label(request),
                'state': best, 'since': mine.get('since', now), 'seen': now, 'tabs': tabs}
    cache.set(key, rows, CACHE_TTL)
    return JsonResponse({'ok': True, 'viewers': _public(rows, me), 'every': 15})


@login_required
@require_POST
def leave(request):
    business = request.user.business
    page = _page(request)
    if not business or not page:
        return JsonResponse({'ok': False}, status=400)
    key = _key(business.pk, *page)
    now, me = time.time(), request.user.pk
    rows = _viewers(key, now)
    mine = rows.get(me)
    if mine:
        tabs = {t: s for t, s in (mine.get('tabs') or {}).items() if t != str(request.POST.get('tab') or '')[:16]}
        if tabs:
            mine['tabs'] = tabs
        else:
            rows.pop(me, None)
        cache.set(key, rows, CACHE_TTL)
    return JsonResponse({'ok': True})
