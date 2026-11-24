"""Sync control page (core/sync_control.py): /routers/<pk>/sync-control/ — one router's choices, its recent syncs and
the day's traffic around a 24-hour dial; the business's other routers are one tab away."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import sync_control as sc
from .models_sync import RouterSyncPolicy


def _business(request):
    return request.user.business


def _router(request, pk):
    return get_object_or_404(_business(request).routers, pk=pk)


@login_required
def sync_control_page(request, pk):
    router = _router(request, pk)
    data = sc.overview(router)
    routers = list(_business(request).routers.all().order_by('name'))
    saved = {p.router_id: p for p in RouterSyncPolicy.objects.filter(router__in=routers)}
    tabs = []
    for r in routers:
        p = saved.get(r.pk) or sc.policy(r)
        tabs.append({'router': r, 'mode': p.get_full_mode_display(), 'held': sc.held_count(r) if r.pk != router.pk else data['held']})
    return render(request, 'core/sync_control.html', {
        'router': router, 'o': data, 'p': data['policy'], 'tabs': tabs, 'every_choices': sc.EVERY_CHOICES,
        'full_modes': RouterSyncPolicy.FULL_MODES, 'paces': RouterSyncPolicy.PACES,
        'voucher_modes': RouterSyncPolicy.VOUCHER_MODES, 'sample_modes': RouterSyncPolicy.SAMPLE_MODES,
        'hours': range(24), 'others': len(routers) - 1,
    })


@login_required
@require_POST
def sync_control_save(request, pk):
    router = _router(request, pk)
    p, notes = sc.save(router, request.POST, request.user)
    msg = f'Sync control saved for {router.name}.'
    if request.POST.get('all_routers'):
        n = sc.copy_to(p, _business(request).routers.all(), request.user)
        if n:
            msg += f' The same choices now apply to {n} other router(s).'
    messages.success(request, ' '.join([msg] + notes))
    try:
        from .utils import log
        log(router.business, 'Router Sync', f'{router.name}: sync control — full sync {p.get_full_mode_display().lower()}'
            f' (every {p.full_every} min, quiet hours {sc.window_text(p)}), vouchers {p.get_voucher_mode_display().lower()}, '
            f'pace {p.get_pace_display().lower()}', router=router)
    except Exception:
        pass
    return redirect('sync_control', pk=router.pk)


@login_required
@require_POST
def sync_control_action(request, pk):
    router = _router(request, pk)
    action = request.POST.get('action', '')
    if action == 'release':
        n = sc.release(router)
        messages.success(request, f'{n} waiting voucher(s) are on their way to {router.name}.' if n else 'No voucher is waiting.')
    elif action == 'sync':
        from .tasks import enqueue_router_sync
        try:
            job, created = enqueue_router_sync(router, request.user)
            messages.success(request, f'{router.name} is syncing now.' if created else f'A sync is already running ({job.progress} %).')
        except Exception as exc:  # noqa: BLE001
            messages.error(request, f'Could not start the sync: {exc}')
    else:
        messages.error(request, 'Unknown action.')
    return redirect('sync_control', pk=router.pk)


@login_required
def sync_control_status(request, pk):
    """Light polling for the page: CPU, the running sync, what TapTap decided last."""
    router = _router(request, pk)
    p = sc.policy(router)
    job = router.sync_jobs.filter(status__in=('queued', 'running')).order_by('-created_at').first()
    is_busy, why = sc.busy(router, p)
    return JsonResponse({'cpu': sc.cpu_now(router), 'busy': is_busy, 'busy_why': why, 'quiet': sc.in_quiet(p),
                         'decision': sc.last_decision(router), 'held': sc.held_count(router),
                         'job': {'progress': job.progress, 'phase': job.phase, 'status': job.status} if job else None})


URLS = [
    ('routers/<int:pk>/sync-control/', sync_control_page, 'sync_control'),
    ('routers/<int:pk>/sync-control/save/', sync_control_save, 'sync_control_save'),
    ('routers/<int:pk>/sync-control/action/', sync_control_action, 'sync_control_action'),
    ('routers/<int:pk>/sync-control/status/', sync_control_status, 'sync_control_status'),
]


def install():
    """URLs and permissions (network.manage, like the Sync button)."""
    from django.urls import path
    from . import permissions, urls
    names = {getattr(x, 'name', None) for x in urls.urlpatterns}
    for route, view, name in URLS:
        permissions.URL_PERMS[name] = 'network.manage'
        if name not in names:
            urls.urlpatterns.append(path(route, view, name=name))
