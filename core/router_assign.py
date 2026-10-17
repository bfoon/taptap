"""TapTap-only vouchers → put them on a router whenever you are ready.

A voucher created with "Create locally only" (no router) exists only in TapTap: it can be printed, sold and
handed to agents, but no router knows it yet. From here it can be put on a router at any moment — one voucher,
a whole batch, a list of codes, or everything still waiting:

* the voucher gets that router, a "Put on <router>" line in its history (who, when, why, how);
* only those vouchers are sent (core/voucher_push.push_vouchers): Direct API / Tunnel routers right away in the
  background, TapTap Link routers with their next check-ins — no full sync;
* vouchers that already have a router are never touched (moving between routers would leave a copy behind).

Fit can do the same with the ``put_on_router`` action (core/fit_actions.py) — only after you press "Take action".
"""
from __future__ import annotations

import logging
import re

from django.db import transaction
from django.urls import path

logger = logging.getLogger('taptap.router_assign')
MAX_AT_ONCE = 5000
_INSTALLED = False


class AssignError(ValueError):
    pass


def waiting(business):
    """Every voucher that is only in TapTap and could still be used."""
    return (business.vouchers.filter(router__isnull=True, source='taptap', deleted_at__isnull=True)
            .exclude(status__in=['expired', 'archived']))


def overview(business):
    """{'total', 'batches': [{id, name, count, agent}], 'singles': n, 'members': n} for the page and for Fit."""
    from django.db.models import Count
    qs = waiting(business)
    rows = (qs.filter(batch__isnull=False).values('batch_id', 'batch__name', 'batch__agent__name')
            .annotate(n=Count('id')).order_by('-n'))
    batches = [{'id': r['batch_id'], 'name': r['batch__name'], 'count': r['n'], 'agent': r['batch__agent__name'] or ''} for r in rows]
    singles = qs.filter(batch__isnull=True).exclude(login_type='member').count()
    members = qs.filter(batch__isnull=True, login_type='member').count()
    return {'total': qs.count(), 'batches': batches, 'singles': singles, 'members': members}


def find_router(business, ref):
    ref = str(ref or '').strip()
    routers = business.routers.all()
    if not ref:
        only = list(routers[:2])
        if len(only) == 1:
            return only[0]
        raise AssignError('Which router? Choose one.')
    r = routers.filter(pk=int(ref)).first() if ref.isdigit() else None
    r = r or routers.filter(name__iexact=ref).first()
    if not r:
        raise AssignError(f'There is no router “{ref}” in {business.business_name}.')
    return r


def select(business, *, codes='', batch='', everything=False, singles=False):
    """The TapTap-only vouchers to put on a router. ``batch`` = id or name; ``codes`` = list or comma/space text."""
    qs = waiting(business)
    if everything:
        return qs
    if batch:
        b = str(batch).strip()
        bq = business.batches.filter(pk=int(b)) if b.isdigit() else business.batches.filter(name__iexact=b)
        found = bq.first()
        if not found:
            raise AssignError(f'There is no batch “{b}”.')
        return qs.filter(batch=found)
    if singles:
        return qs.filter(batch__isnull=True)
    if isinstance(codes, str):
        codes = [c for c in re.split(r'[\s,;]+', codes) if c]
    codes = [str(c).strip() for c in codes or [] if str(c).strip()]
    if not codes:
        raise AssignError('Choose the vouchers: a batch, some codes, or everything waiting.')
    from django.db.models import Q
    q = Q()
    for c in codes[:MAX_AT_ONCE]:
        q |= Q(code__iexact=c)
    return qs.filter(q)


def describe(business, qs, router):
    n = qs.count()
    return n, (f'{n} TapTap-only voucher{"s" if n != 1 else ""} get {router.name} as their router and are sent to it '
               f'{"with its next check-ins (TapTap Link)" if _on_link(router) else "now"}. Vouchers already on a router are not touched.')


def _on_link(router):
    from .voucher_history import channel
    return channel(router) == 'TapTap Link'


def place(business, qs, router, *, user=None, reason='', via_fit=False):
    """Give these TapTap-only vouchers ``router`` and send them. Returns a message for the person."""
    from .models import Voucher
    from .voucher_history import record
    from .voucher_push import push_vouchers
    if router.business_id != business.pk:
        raise AssignError('That router is not in this business.')
    ids = list(qs.values_list('pk', flat=True)[:MAX_AT_ONCE + 1])
    if not ids:
        raise AssignError('Nothing to send — those vouchers are already on a router, used up or deleted.')
    more = len(ids) > MAX_AT_ONCE
    ids = ids[:MAX_AT_ONCE]
    with transaction.atomic():
        # router__isnull again: never take a voucher someone else has just put on a router
        changed = Voucher.objects.filter(pk__in=ids, router__isnull=True).update(
            router=router, mikrotik_sync_status='Pending', mikrotik_sync_error='')
    vouchers = list(Voucher.objects.filter(pk__in=ids, router=router).select_related('router', 'business'))
    why = (reason or 'Put on the router when ready')[:200] + (' (approved in Fit)' if via_fit else '')
    for v in vouchers:
        record(v, 'note', user=user, reason=f'Put on {router.name}: {why}', via='TapTap')
    try:
        from .utils import log
        log(business, 'Vouchers put on router', f'{changed} TapTap-only voucher(s) → {router.name}', status='Success')
    except Exception:
        logger.exception('activity log')
    out = push_vouchers(vouchers, user=user)
    msg = ' '.join(out.values()) or f'{changed} voucher(s) given to {router.name}.'
    if more:
        msg += f' Only the first {MAX_AT_ONCE} were done — run it again for the rest.'
    return msg


# ─────────────────────────────── page ───────────────────────────────

def _business(request):
    return getattr(request, 'tt_business', None) or request.user.business


def page(request):
    from django.contrib import messages
    from django.shortcuts import redirect, render
    business = _business(request)
    if request.method == 'POST':
        p = request.POST
        back = p.get('next') if str(p.get('next') or '').startswith('/') else request.path
        try:
            router = find_router(business, p.get('router'))
            qs = select(business, codes=p.get('codes', ''), batch=p.get('batch', ''),
                        everything=p.get('scope') == 'everything', singles=p.get('scope') == 'singles')
            messages.success(request, place(business, qs, router, user=request.user, reason=p.get('reason', '')))
        except AssignError as exc:
            messages.error(request, str(exc))
        except Exception:
            logger.exception('put on router')
            messages.error(request, 'That did not work — please try again.')
        return redirect(back)
    ov = overview(business)
    recent = waiting(business).filter(batch__isnull=True).order_by('-created_at')[:30]
    return render(request, 'core/vouchers_to_router.html', {
        'ov': ov, 'routers': business.routers.order_by('name'), 'recent': recent})


NAV_DESKTOP = '<a href="/vouchers/reports/"><i class="bi bi-chat-square-text"></i> Customer reports</a>'
NAV_MOBILE = '<a href="/vouchers/reports/">Customer reports</a>'


def _inject_nav(request, response):
    """Sidebar: "TapTap only (n)" under Customer reports — only while vouchers are waiting."""
    business = getattr(request, 'tt_business', None)
    perms = getattr(request, 'tt_perms', frozenset())
    if (not business or not ({'vouchers.create', 'vouchers.support'} & set(perms)) or getattr(response, 'streaming', False)
            or response.status_code != 200 or 'text/html' not in response.get('Content-Type', '').lower()):
        return response
    body = response.content.decode(response.charset or 'utf-8')
    if NAV_DESKTOP not in body or 'href="/vouchers/to-router/"' in body:
        return response
    try:
        n = waiting(business).count()
    except Exception:
        return response
    if not n:
        return response
    body = body.replace(NAV_DESKTOP, NAV_DESKTOP + f'<a href="/vouchers/to-router/"><i class="bi bi-cloud-arrow-up"></i> TapTap only '
                                                    f'<span class="badge rounded-pill text-bg-info ms-1">{n}</span></a>', 1)
    body = body.replace(NAV_MOBILE, NAV_MOBILE + f'<a href="/vouchers/to-router/">TapTap only ({n})</a>', 1)
    out = body.encode(response.charset or 'utf-8')
    response.content = out
    if response.has_header('Content-Length'):
        response['Content-Length'] = str(len(out))
    return response


def _install_nav():
    from functools import wraps
    from .team import TeamAccessMiddleware
    original = TeamAccessMiddleware.__call__
    if getattr(original, '_taptap_router_assign_nav', False):
        return

    @wraps(original)
    def wrapped(self, request):
        return _inject_nav(request, original(self, request))
    wrapped._taptap_router_assign_nav = True
    TeamAccessMiddleware.__call__ = wrapped


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    from django.contrib.auth.decorators import login_required
    from . import permissions, urls
    names = {getattr(p, 'name', None) for p in urls.urlpatterns}
    if 'vouchers_to_router' not in names:
        urls.urlpatterns.insert(0, path('vouchers/to-router/', login_required(page), name='vouchers_to_router'))
    permissions.URL_PERMS['vouchers_to_router'] = ('vouchers.create', 'vouchers.support')
    _install_nav()
    _INSTALLED = True
