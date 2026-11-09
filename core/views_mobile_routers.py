"""Routers in the TapTap mobile app — /api/mobile/v1/routers/…

Each MikroTik on its own card, the way the website's router tabs show it:

    GET  routers/                    every router: online/offline, people online, vouchers in use, sales today,
                                     warnings (two routers on one Link token, last error)       network.manage | vouchers.support
    GET  routers/<id>/               one router: the same plus who is online now and its own activity
    POST routers/<id>/               action=sync          full sync in the background           network.manage
                                     action=test          connection test (Link: a ping command)
                                     action=kick          user=<code> [item_id]: disconnect one person   network.manage | vouchers.support
                                     action=reboot        confirm=<router name>                 network.manage
    GET  routers/<id>/move/          where it may go and what goes with it (core/router_move.py)  network.manage
    POST routers/<id>/move/          business_id, confirm_name, with_vouchers

Everything goes through the same code as the website's buttons, so the phone can never do more than the web.
"""
from __future__ import annotations

from django.db.models import Count, Sum
from django.http import JsonResponse
from django.utils import timezone

from .views_mobile import _body, _business, _err, _perms, _signed_in, clean

VIEW = ('network.manage', 'vouchers.support')
MANAGE = ('network.manage',)


def _need(request, perms):
    b = _business(request)
    if b is None:
        return None, _err('This login is not linked to a TapTap business.', 403)
    if not set(perms) & _perms(request):
        return None, _err('Your role does not allow this.', 403)
    return b, None


def _ago(when, now):
    if not when:
        return ''
    s = int((now - when).total_seconds())
    if s < 60:
        return 'just now'
    if s < 3600:
        return f'{s // 60} min ago'
    if s < 86400:
        return f'{s // 3600} h ago'
    return f'{s // 86400} d ago'


def _warnings(r):
    from django.core.cache import cache
    out = []
    agent = getattr(r, 'agent', None) if r.connection_mode == 'agent' else None
    if agent is not None:
        shared = cache.get(f'tt:link:shared:{agent.pk}')
        if shared:
            out.append({'level': 'danger', 'text': 'Two routers are using this router’s TapTap Link token. On the website, '
                                                     'open the other router’s TapTap Link page, press Rotate token and paste '
                                                     'its own script into it.'})
        if not agent.last_seen_at:
            out.append({'level': 'warning', 'text': 'This router has never checked in. Paste its TapTap Link script '
                                                      'into the MikroTik terminal (website › Routers › TapTap Link).'})
    if r.last_error and not out:
        out.append({'level': 'warning', 'text': r.last_error[:240]})
    return out


def _healed(job):
    """What the full sync's check healed on the router (core/sync_doctor.py), in one line, or ''."""
    try:
        from .sync_doctor import line
        doc = (job.summary or {}).get('doctor') if job.status == 'success' else None
        return line(doc) if isinstance(doc, dict) else ''
    except Exception:
        return ''


def _card(r, now, counts):
    agent = getattr(r, 'agent', None) if r.connection_mode == 'agent' else None
    seen = (agent.last_seen_at if agent else None) or r.last_tested_at
    job = r.sync_jobs.order_by('-created_at').first() if hasattr(r, 'sync_jobs') else None
    return {
        'id': r.pk, 'name': r.name, 'status': r.status or 'Unknown', 'online': (r.status or '') == 'Online',
        'mode': 'TapTap Link' if r.connection_mode == 'agent' else 'Direct API',
        'board': getattr(agent, 'board', '') or '', 'ros': getattr(agent, 'ros_version', '') or '',
        'seen': _ago(seen, now), 'seen_at': seen,
        'users_online': counts['online'].get(r.pk, 0), 'in_use': counts['in_use'].get(r.pk, 0),
        'in_stock': counts['stock'].get(r.pk, 0), 'sales_today': counts['sales'].get(r.pk, {}).get('n', 0),
        'revenue_today': counts['sales'].get(r.pk, {}).get('v', 0) or 0,
        'sync': {'status': job.status, 'progress': job.progress, 'phase': job.phase, 'at': job.created_at,
                 'healed': _healed(job)} if job else None,
        'warnings': _warnings(r),
    }


def _online(b):
    """People online per router, from what TapTap already knows (no router is asked): the sessions a TapTap Link
    router reported at its last check-in, or the last live-sync pass of a Direct API router."""
    from django.core.cache import cache
    from .linklive import sessions
    out = {}
    for r in b.routers.all():
        n = len(sessions(r)) if r.connection_mode == 'agent' else cache.get(f'tt:watch:active:{r.pk}')
        if n:
            out[r.pk] = n
    return out


def _counts(b, now):
    from .models import VoucherSale
    start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    v = b.vouchers.exclude(router__isnull=True)
    return {
        'in_use': dict(v.filter(status='active', used_at__isnull=False).values_list('router_id').annotate(n=Count('id'))),
        'stock': dict(v.filter(status='active', used_at__isnull=True, sold_at__isnull=True).values_list('router_id').annotate(n=Count('id'))),
        'online': _online(b),
        'sales': {row['router_id']: {'n': row['n'], 'v': row['v']} for row in
                  VoucherSale.objects.filter(business=b, sold_at__gte=start, router__isnull=False)
                  .values('router_id').annotate(n=Count('id'), v=Sum('amount'))},
    }


@_signed_in
def routers(request):
    b, refused = _need(request, VIEW)
    if refused:
        return refused
    now = timezone.now()
    counts = _counts(b, now)
    rows = [_card(r, now, counts) for r in b.routers.select_related('agent').order_by('name')]
    perms = _perms(request)
    return JsonResponse(clean({'ok': True, 'routers': rows, 'currency': b.currency or 'D',
                               'business': {'id': b.pk, 'name': b.business_name},
                               'can': {'manage': 'network.manage' in perms, 'kick': bool(set(VIEW) & perms),
                                       'move': 'network.manage' in perms}}))


def _sessions(b, r):
    from .views import _router_rows
    rows, errors = _router_rows(b, 'active_users', only=r)
    out = []
    for x in rows[:200]:
        out.append({'user': str(x.get('user', '')), 'mac': str(x.get('mac_address', '') or x.get('mac-address', '')),
                    'ip': str(x.get('address', '')), 'uptime': str(x.get('uptime', '')), 'item_id': str(x.get('id', '') or x.get('.id', '')),
                    'down': str(x.get('bytes_out', '') or x.get('bytes-out', '')), 'up': str(x.get('bytes_in', '') or x.get('bytes-in', ''))})
    out.sort(key=lambda s: s['user'])
    return out, errors


@_signed_in
def router(request, pk):
    b, refused = _need(request, VIEW)
    if refused:
        return refused
    r = b.routers.select_related('agent').filter(pk=pk).first()
    if r is None:
        return _err('That router is not in this business (it may have been moved).', 404)
    if request.method == 'POST':
        return _act(request, b, r)
    now = timezone.now()
    card = _card(r, now, _counts(b, now))
    sessions, errors = _sessions(b, r)
    card['users_online'] = len(sessions) or card['users_online']
    activity = [{'at': a.created_at, 'ago': _ago(a.created_at, now), 'type': a.type, 'text': a.details, 'ok': a.status != 'Failed'}
                for a in b.activities.filter(router=r).order_by('-created_at')[:25]]
    return JsonResponse(clean({'ok': True, 'router': card, 'sessions': sessions, 'session_errors': errors[:3],
                               'activity': activity}))


def _act(request, b, r):
    d = _body(request)
    action = str(d.get('action') or '')
    perms = _perms(request)
    if action != 'kick' and 'network.manage' not in perms:
        return _err('Your role does not allow this.', 403)
    if action == 'sync':
        from .tasks import enqueue_router_sync
        try:
            job, created = enqueue_router_sync(r, request.user)
        except Exception as exc:
            return _err(f'Could not start the sync: {exc}'[:300], 502)
        return JsonResponse({'ok': True, 'message': f'{r.name}: full sync started.' if created else
                             f'{r.name} is already syncing ({job.progress}%).'})
    if action == 'test':
        if r.connection_mode == 'agent':
            from . import agent as link
            from .linklive import link_state
            online, why = link_state(r)
            if not online:
                return _err(why, 409)
            link.queue(r, 'ping', label='Test connection (app)', user=request.user)
            return JsonResponse({'ok': True, 'message': f'{r.name} is connected through TapTap Link. A test was sent.'})
        from .mikrotik import MikroTikService
        try:
            with MikroTikService(r) as svc:
                svc.test()
        except Exception as exc:
            type(r).objects.filter(pk=r.pk).update(status='Offline', last_error=str(exc)[:2000], last_tested_at=timezone.now())
            return _err(f'Connection failed: {exc}'[:300], 502)
        type(r).objects.filter(pk=r.pk).update(status='Online', last_error='', last_tested_at=timezone.now())
        return JsonResponse({'ok': True, 'message': f'{r.name} answered. RouterOS is reachable.'})
    if action == 'kick':
        if not set(VIEW) & perms:
            return _err('Your role does not allow this.', 403)
        user = str(d.get('user') or '').strip()
        item = str(d.get('item_id') or '').strip()
        from .utils import log
        if r.connection_mode == 'agent':
            from .linkops import QUEUED, send, session_user
            user = user or session_user(r, item)
            if not user:
                return _err('That session has already ended.', 409)
            try:
                send(r, 'disconnect', {'user': user}, label=f'Disconnect {user} (app)', user=request.user)
            except ValueError as exc:
                return _err(str(exc), 409)
            log(b, 'Disconnect', f'{r.name}: {user} disconnected from the app', router=r)
            return JsonResponse({'ok': True, 'message': f'{user}: {QUEUED}'})
        from .mikrotik import MikroTikService
        try:
            with MikroTikService(r) as svc:
                svc.disconnect(item)
        except Exception as exc:
            return _err(str(exc)[:300], 502)
        log(b, 'Disconnect', f'{r.name}: {user or item} disconnected from the app', router=r)
        return JsonResponse({'ok': True, 'message': f'{user or "The person"} was disconnected.'})
    if action == 'reboot':
        if str(d.get('confirm') or '').strip().lower() != r.name.strip().lower():
            return _err(f'Type the router name “{r.name}” to confirm.')
        from .utils import log
        if r.connection_mode == 'agent':
            from . import agent as link
            link.queue(r, 'reboot', label='Reboot router (app)', user=request.user, minutes=5)
            log(b, 'Router Reboot', f'{r.name} reboot sent from the app by {request.user.email}', router=r)
            return JsonResponse({'ok': True, 'message': f'Reboot sent — {r.name} restarts at its next check-in.'})
        from django.conf import settings
        from .mikrotik import MikroTikService
        from .views_ports import do_reboot
        try:
            with MikroTikService(r, timeout=getattr(settings, 'MIKROTIK_TIMEOUT', 10)) as svc:
                do_reboot(svc)
        except Exception as exc:
            if 'closed' not in str(exc).lower():
                return _err(str(exc)[:300], 502)
        type(r).objects.filter(pk=r.pk).update(status='Rebooting', last_tested_at=timezone.now())
        log(b, 'Router Reboot', f'{r.name} rebooted from the app by {request.user.email}', router=r)
        return JsonResponse({'ok': True, 'message': f'{r.name} is rebooting. It is usually back in 1–2 minutes.'})
    return _err('Unknown action.')


@_signed_in
def router_move(request, pk):
    from . import router_move as rm
    b, refused = _need(request, MANAGE)
    if refused:
        return refused
    r = b.routers.filter(pk=pk).first()
    if r is None:
        return _err('That router is not in this business.', 404)
    if request.method != 'POST':
        with_vouchers = request.GET.get('vouchers', '1') != '0'
        out = {'ok': True, 'preview': rm.preview(r, with_vouchers), 'targets': rm.targets(request.user, b),
               'this_business': {'id': b.pk, 'name': b.business_name}}
        bid = request.GET.get('business_id', '').strip()
        if bid:
            try:
                t = rm.find_target(request.user, b, bid)
                out['target'] = {'id': t.pk, 'name': t.business_name, 'routers': t.routers.count()}
            except rm.MoveRefused as exc:
                out['target_error'] = str(exc)
        return JsonResponse(clean(out))
    d = _body(request)
    if str(d.get('confirm_name') or '').strip().lower() != r.name.strip().lower():
        return _err(f'Type the router name “{r.name}” to confirm.')
    try:
        target = rm.find_target(request.user, b, d.get('business_id', ''))
        s = rm.move(r, target, request.user, with_vouchers=d.get('with_vouchers', True) is not False)
    except rm.MoveRefused as exc:
        return _err(str(exc), 403)
    return JsonResponse(clean({'ok': True, 'message': f'{r.name} now belongs to {target.business_name}'
                               + (f' with {s["vouchers"]} vouchers' if s['vouchers'] else '') + '. Sales stay here.',
                               'summary': {k: s[k] for k in ('vouchers', 'plans_copied', 'batches_moved', 'to', 'to_id')},
                               'warnings': s['warnings'][:3]}))
