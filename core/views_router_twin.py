"""Routers › Migrate & twins: move what one MikroTik holds to another, and keep a standby that has everything."""
from __future__ import annotations

import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from . import router_migrate as mig
from . import router_twin as tw
from .models_router_twin import RouterMigration, RouterTwin


def _business(request):
    return getattr(request, 'tt_business', None) or request.user.business


def _data(request):
    if request.content_type == 'application/json':
        try:
            return json.loads(request.body or b'{}')
        except ValueError:
            return {}
    return request.POST


def _twin(request, pk):
    return get_object_or_404(RouterTwin.objects.select_related('primary', 'standby', 'business'), business=_business(request), pk=pk)


def _router_cards(business):
    twins = {}
    for t in RouterTwin.objects.filter(business=business).select_related('primary', 'standby'):
        twins[t.primary_id] = ('main', t)
        twins[t.standby_id] = ('standby', t)
    out = []
    for r in business.routers.order_by('name'):
        role = twins.get(r.pk)
        out.append({'id': r.pk, 'name': r.name, 'status': r.status, 'online': tw.online(r),
                    'via': 'TapTap Link' if r.connection_mode == 'agent' else 'Direct API',
                    'role': role[0] if role else '', 'twin': role[1].pk if role else None,
                    'users': r.hotspot_users.filter(is_present=True).count(),
                    'vouchers': r.vouchers.filter(status__in=('active', 'disabled')).count()})
    return out


def _twin_card(t):
    try:
        ready = tw.readiness(t)
    except Exception as exc:           # a page must open even if one twin cannot be measured
        ready = {'percent': 0, 'verdict': f'Could not be measured: {exc}', 'tone': 'bad', 'kinds': {}, 'in_use': 0, 'in_use_ready': 0}
    return {'twin': t, 'ready': ready, 'open': t.events.filter(ended_at__isnull=True, kind__in=('failover', 'standby_down')).first()}


def _migrations(business):
    names = {k: l for k, l, _i, _h in mig.PARTS}
    out = list(RouterMigration.objects.filter(business=business)[:8])
    for m in out:
        m.part_names = ', '.join(names.get(p, p) for p in (m.parts or []))
    return out


@login_required
@require_GET
def twins_home(request):
    business = _business(request)
    twins = [_twin_card(t) for t in RouterTwin.objects.filter(business=business).select_related('primary', 'standby')]
    routers = _router_cards(business)
    free = [r for r in routers if not r['role']]
    ctx = {'twins': twins, 'routers': routers, 'free': free, 'parts': mig.PARTS, 'default_parts': mig.DEFAULT_PARTS,
           'migrations': _migrations(business), 'switch_choices': RouterTwin.SWITCH,
           'tab': request.GET.get('tab') if request.GET.get('tab') in ('migrate', 'twins') else ('twins' if twins else 'migrate'),
           'pre_source': request.GET.get('source', ''), 'pre_target': request.GET.get('target', '')}
    return render(request, 'core/router_twins.html', ctx)


@login_required
@require_POST
def twin_pair(request):
    business = _business(request)
    d = request.POST
    try:
        primary = business.routers.get(pk=int(d.get('primary') or 0))
        standby = business.routers.get(pk=int(d.get('standby') or 0))
    except Exception:
        messages.error(request, 'Choose the main router and its standby.')
        return redirect(reverse('router_twins') + '?tab=twins')
    opts = {k: d.get(k) == 'on' for k in ('copy_vouchers', 'copy_members', 'copy_router_users', 'copy_profiles', 'copy_bindings')}
    opts['switch_over'] = d.get('switch_over') or 'ssid'
    try:
        twin = tw.pair(business, primary, standby, user=request.user, **opts)
    except tw.TwinError as exc:
        messages.error(request, str(exc))
        return redirect(reverse('router_twins') + '?tab=twins')
    _start_mirror(twin, request.user)
    messages.success(request, f'{standby.name} is now the standby of {primary.name}. TapTap is bringing it up to date.')
    return redirect('router_twin_detail', pk=twin.pk)


def _start_mirror(twin, user):
    try:
        from django.conf import settings
        if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
            raise RuntimeError('eager')
        from .tasks import router_twin_mirror_task
        router_twin_mirror_task.delay(twin.pk, getattr(user, 'pk', None))
        return 'started'
    except Exception:
        return tw._summary_text(tw.mirror_now(twin, user=user))


@login_required
@require_GET
def twin_detail(request, pk):
    t = _twin(request, pk)
    card = _twin_card(t)
    return render(request, 'core/router_twin_detail.html', {
        'twin': t, 'ready': card['ready'], 'open_event': card['open'], 'events': t.events.all()[:30],
        'switch_choices': RouterTwin.SWITCH, 'problems': t.items.exclude(status__in=('sent', 'queued')).order_by('kind', 'key')[:40],
        'primary_link': tw.on_link(t.primary), 'standby_link': tw.on_link(t.standby)})


@login_required
@require_GET
def twin_state(request, pk):
    t = _twin(request, pk)
    r = tw.readiness(t)
    return JsonResponse({'ok': True, 'ready': {k: v for k, v in r.items() if k != 'skipped'},
                         'primary': {'name': t.primary.name, 'status': t.primary.status, 'online': tw.online(t.primary)},
                         'standby': {'name': t.standby.name, 'status': t.standby.status, 'online': tw.online(t.standby)},
                         'paused': t.paused, 'failover': bool(t.failover_since),
                         'last_mirror_at': t.last_mirror_at.isoformat() if t.last_mirror_at else None, 'last_error': t.last_error,
                         'events': [{'kind': e.kind, 'label': e.get_kind_display(), 'detail': e.detail, 'at': e.started_at.isoformat(),
                                     'ended': e.ended_at.isoformat() if e.ended_at else None, 'activated': e.activated}
                                    for e in t.events.all()[:12]]})


@login_required
@require_POST
def twin_action(request, pk):
    t = _twin(request, pk)
    d = _data(request)
    action = d.get('action')
    wants_json = request.content_type == 'application/json'

    def done(msg, ok=True, **extra):
        if wants_json:
            return JsonResponse({'ok': ok, 'message': msg, **extra}, status=200 if ok else 400)
        (messages.success if ok else messages.error)(request, msg)
        return redirect('router_twin_detail', pk=t.pk) if action not in ('unpair',) or not ok else redirect(reverse('router_twins') + '?tab=twins')

    if action == 'mirror':
        res = _start_mirror(t, request.user)
        return done('Bringing the standby up to date in the background.' if res == 'started' else res)
    if action in ('pause', 'resume'):
        tw.set_paused(t, action == 'pause', user=request.user)
        return done('Mirroring paused.' if action == 'pause' else 'Mirroring resumed.')
    if action == 'settings':
        opts = {k: str(d.get(k)) in ('on', 'true', '1', 'True') for k in ('copy_vouchers', 'copy_members', 'copy_router_users', 'copy_profiles', 'copy_bindings')}
        opts['switch_over'] = d.get('switch_over')
        tw.update(t, user=request.user, **opts)
        return done('Saved. The standby follows the new choices at the next update.')
    if action == 'portal':
        from .portal_deploy import deploy
        ok, msg = deploy(t.standby, user=request.user)
        return done(msg, ok)
    if action == 'promote':
        if str(d.get('confirm') or '').strip().lower() != t.standby.name.strip().lower():
            return done(f'Type the standby\'s name “{t.standby.name}” to confirm.', False)
        n = tw.promote(t, user=request.user)
        return done(f'{t.standby.name} is now the main router: {n} voucher(s) and member(s) moved to it. '
                    f'{t.primary.name} becomes its standby when it is back.')
    if action == 'unpair':
        if str(d.get('confirm') or '').strip().lower() != t.primary.name.strip().lower():
            return done(f'Type the main router\'s name “{t.primary.name}” to confirm.', False)
        note = tw.unpair(t, user=request.user, clean=str(d.get('clean')) in ('on', 'true', '1'))
        msg = 'The twin is ended.' + (f' {note}' if note else '')
        if wants_json:
            return JsonResponse({'ok': True, 'message': msg, 'next': reverse('router_twins') + '?tab=twins'})
        messages.success(request, msg)
        return redirect(reverse('router_twins') + '?tab=twins')
    return done('Unknown action.', False)


@login_required
@require_POST
def twin_vrrp(request, pk):
    t = _twin(request, pk)
    d = _data(request)
    try:
        out = tw.vrrp_scripts(t, d.get('interface'), d.get('address'), d.get('vrid'), dhcp_gateway=str(d.get('dhcp')) in ('on', 'true', '1', 'True'))
    except tw.TwinError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)}, status=400)
    return JsonResponse({'ok': True, **out})


# ─────────────────────────────── migration ───────────────────────────────

@login_required
@require_GET
def migrate_preview(request):
    try:
        return JsonResponse({'ok': True, **mig.preview(_business(request), request.GET.get('source'), request.GET.get('target'))})
    except mig.MigrateError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)}, status=400)


@login_required
@require_POST
def migrate_start(request):
    d = _data(request)
    business = _business(request)
    try:
        _s, target = mig._routers(business, d.get('source'), d.get('target'))
    except mig.MigrateError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)}, status=400)
    if str(d.get('confirm') or '').strip().lower() != target.name.strip().lower():
        return JsonResponse({'ok': False, 'message': f'Type the new router\'s name “{target.name}” to confirm.'}, status=400)
    parts = d.get('parts') if isinstance(d.get('parts'), list) else (d.getlist('parts') if hasattr(d, 'getlist') else [])
    try:
        m = mig.start(business, d.get('source'), d.get('target'), parts, clean_source=str(d.get('clean')) in ('on', 'true', '1', 'True'), user=request.user)
    except mig.MigrateError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)}, status=400)
    m.refresh_from_db()
    return JsonResponse({'ok': True, 'migration': mig.as_json(m)})


@login_required
@require_GET
def migrate_status(request, pk):
    m = get_object_or_404(RouterMigration, business=_business(request), pk=pk)
    return JsonResponse({'ok': True, 'migration': mig.as_json(m)})


@login_required
@require_GET
def migrate_config(request):
    """The old router's backups on the TapTap server, for a restore on the new router."""
    business = _business(request)
    try:
        source, target = mig._routers(business, request.GET.get('source'), request.GET.get('target'))
    except mig.MigrateError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)}, status=400)
    warn = [('warn', f'A full restore REPLACES everything on {target.name} (its addresses, interfaces, users) with {source.name}\'s '
                     'configuration, then it restarts. Only do it on a new or spare router.'),
            ('info', '.backup brings back everything (passwords too) but needs the same model; .rsc (export) works across models '
                     'but has no passwords or certificates.')]
    if tw.twin_of(target) is not None or tw.twin_of(source) is not None:
        warn.append(('warn', 'Twins: a standby must keep its OWN addresses. Restore only on a router that will replace the old one.'))
    return JsonResponse({'ok': True, 'backups': mig.config_backups(source), 'warnings': warn,
                         'source': {'id': source.pk, 'name': source.name}, 'target': {'id': target.pk, 'name': target.name, 'status': target.status},
                         'urls': {'backup': reverse('router_backups', args=[source.pk]), 'prepare': reverse('router_restore_prepare', args=[target.pk]),
                                  'start': reverse('router_restore_start', args=[target.pk, 0]).replace('/0/', '/{id}/'),
                                  'status': reverse('router_restore_status', args=[target.pk, 0]).replace('/0/', '/{id}/')}})


@login_required
def migrate_sections(request):
    """GET ?backup=<id>: the sections of that export. POST: a script with only the ticked sections."""
    from .models import RouterBackup
    business = _business(request)
    bid = request.GET.get('backup') or request.POST.get('backup')
    b = RouterBackup.objects.filter(router__business=business, pk=str(bid or '0') if str(bid or '').isdigit() else 0).select_related('router').first()
    if b is None:
        raise Http404
    text = mig.export_text(b)
    if request.method == 'GET':
        if not text.strip():
            return JsonResponse({'ok': False, 'message': 'This backup has no readable export (.rsc) on the TapTap server.'}, status=400)
        return JsonResponse({'ok': True, 'sections': mig.section_list(text), 'router': b.router.name, 'name': b.name})
    target = business.routers.filter(pk=str(request.POST.get('target') or '0') if str(request.POST.get('target') or '').isdigit() else 0).first()
    try:
        script, n = mig.sections_script(text, request.POST.getlist('sections'), b.router.name, target.name if target else '',
                                        skip_errors=request.POST.get('strict') != 'on')
    except mig.MigrateError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)}, status=400)
    from .utils import log
    log(business, 'Router Migration', f'{b.router.name}: script of {n} command(s) from {b.name} downloaded', router=b.router)
    safe = ''.join(ch if ch.isalnum() or ch in '-_' else '-' for ch in b.router.name)[:40] or 'router'
    resp = HttpResponse(script, content_type='text/plain; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="taptap-migrate-{safe}.rsc"'
    return resp
