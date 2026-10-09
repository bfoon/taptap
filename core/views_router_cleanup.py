"""Clean router memory — scan, plan, clean (core/router_cleanup.py)."""
import json
from datetime import timedelta
import logging

from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import router_cleanup as rc
from .models_router_cleanup import RouterCleanup

logger = logging.getLogger('taptap.cleanup')


def _router(request, pk):
    return get_object_or_404(request.user.business.routers, pk=pk)


def as_json(job):
    out = {'id': job.pk, 'action': job.action, 'status': job.status, 'via': job.via, 'result': job.result or {},
           'at': timezone.localtime(job.created_at).strftime('%d %b %H:%M')}
    if job.action == 'copy':
        from django.urls import reverse
        from .router_file_copy import received
        p = job.params or {}
        out.update(name=p.get('name'), total=p.get('total') or p.get('size') or 0, received=received(job))
        if job.status == 'done' and not (job.result or {}).get('purged'):
            out['download'] = reverse('router_cleanup_download', args=[job.router_id, job.pk])
    # never send the token hash to the page
    return out


def _health(router):
    """Last free/total storage and RAM from the TapTap Link heartbeat — shown before any scan."""
    try:
        from django.core.cache import cache
        from .link_system_health import _cache_key
        data = cache.get(_cache_key(router.pk)) or {}
        res = data.get('resource') or data
        return {'free': rc._int(res.get('free-hdd-space')), 'total': rc._int(res.get('total-hdd-space')),
                'free_mem': rc._int(res.get('free-memory')), 'total_mem': rc._int(res.get('total-memory'))}
    except Exception:
        return {}


def _start(request, router, action, params):
    from .voucher_history import channel
    via = channel(router)
    job = RouterCleanup.objects.create(business=router.business, router=router, action=action, params=params, via=via,
                                       created_by=request.user, status='waiting' if via == 'TapTap Link' else 'running')
    if via == 'TapTap Link':
        from .linkops import send
        try:
            cmd = send(router, 'cleanup', {'job_id': job.pk, 'action': action, 'files': params.get('files', []),
                                           'clear_log': bool(params.get('clear_log'))},
                       label='Scan router storage' if action == 'scan' else 'Clean router storage', user=request.user, minutes=7)
            job.command_id = cmd.pk
            job.save(update_fields=['command_id'])
        except ValueError as exc:
            job.status, job.result, job.finished_at = 'failed', {'error': f'{router.name} cannot take commands right now: {exc}'}, timezone.now()
            job.save(update_fields=['status', 'result', 'finished_at'])
        return job
    from .mikrotik import MikroTikService
    try:
        svc = MikroTikService(router, timeout=40).connect()
        try:
            raw = rc.api_scan(svc) if action == 'scan' else rc.api_clean(svc, params.get('files', []), params.get('clear_log'))
        finally:
            svc.close()
        if action == 'clean' and raw.get('left'):
            params['left'] = raw['left'] + params.get('left', [])
        rc.finish(job, raw)
    except Exception as exc:  # noqa: BLE001
        logger.info('cleanup %s on router %s failed: %s', action, router.pk, exc)
        job.status, job.result, job.finished_at = 'failed', {'error': str(exc)[:300]}, timezone.now()
        job.save(update_fields=['status', 'result', 'finished_at'])
    return job


@login_required
@require_POST
def router_cleanup_scan(request, pk):
    router = _router(request, pk)
    if RouterCleanup.objects.filter(router=router, status='waiting', created_at__gte=timezone.now() - timedelta(minutes=7)).exists():
        job = RouterCleanup.objects.filter(router=router, status='waiting').first()
        return JsonResponse({'ok': True, 'job': as_json(job), 'health': _health(router)})
    job = _start(request, router, 'scan', {})
    return JsonResponse({'ok': True, 'job': as_json(job), 'health': _health(router)})


@login_required
@require_POST
def router_cleanup_clean(request, pk):
    """{"scan": id, "files": [...], "clear_log": bool} — or {"continue": clean_job_id} for the next batch."""
    router = _router(request, pk)
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        data = {}
    if data.get('continue'):
        prev = RouterCleanup.objects.filter(router=router, pk=str(data['continue']), action='clean', status='done').first()
        left = list((prev.params or {}).get('left') or []) if prev else []
        if not left:
            return JsonResponse({'ok': False, 'message': 'Nothing left to clean.'}, status=400)
        free_before, scan_id, clear_log = prev.params.get('free_before'), prev.params.get('scan'), False
        names = left
    else:
        scan = RouterCleanup.objects.filter(router=router, pk=str(data.get('scan') or 0), action='scan', status='done').first()
        if not scan:
            return JsonResponse({'ok': False, 'message': 'Scan the router first.'}, status=400)
        if (timezone.now() - scan.created_at).total_seconds() > 1800:
            return JsonResponse({'ok': False, 'message': 'That scan is more than 30 minutes old — scan again.'}, status=400)
        plan = (scan.result or {}).get('plan') or {}
        allowed = rc.cleanable_names(plan)
        wanted = [str(n) for n in (data.get('files') or []) if isinstance(n, str)]
        bad = [n for n in wanted if n not in allowed]
        if bad:
            return JsonResponse({'ok': False, 'message': f'{bad[0]} cannot be removed here.'}, status=400)
        names = list(dict.fromkeys(wanted))
        clear_log = bool(data.get('clear_log'))
        if not names and not clear_log:
            return JsonResponse({'ok': False, 'message': 'Choose something to clean.'}, status=400)
        free_before, scan_id = (plan.get('storage') or {}).get('free'), scan.pk
    from .voucher_history import channel
    if channel(router) == 'TapTap Link':
        batches = rc.link_batches(names) or [[]]
        first, rest = batches[0], [n for b in batches[1:] for n in b]
    else:
        first, rest = names, []
    job = _start(request, router, 'clean', {'scan': scan_id, 'files': first, 'clear_log': clear_log, 'left': rest,
                                            'free_before': free_before, 'total': len(names)})
    from .utils import log
    log(router.business, 'Router Cleanup', f'{router.name}: removing {len(first)} file(s)' + (' and clearing the log' if clear_log else ''))
    return JsonResponse({'ok': True, 'job': as_json(job), 'left': len(job.params.get('left') or [])})


@login_required
@require_GET
def router_cleanup_job(request, pk, job_id):
    router = _router(request, pk)
    job = RouterCleanup.objects.filter(router=router, pk=job_id).first()
    if not job:
        raise Http404
    rc.expire_waiting(job)
    if job.action == 'copy' and job.status in ('waiting', 'running'):
        _expire_copy(job)
    return JsonResponse({'ok': True, 'job': as_json(job), 'left': len((job.params or {}).get('left') or [])})


def _expire_copy(job):
    from datetime import datetime
    exp = (job.params or {}).get('expires')
    if exp and datetime.fromisoformat(exp) < timezone.now():
        job.status, job.result, job.finished_at = 'failed', {'error': 'The router did not finish sending the file within an hour.'}, timezone.now()
        job.save(update_fields=['status', 'result', 'finished_at'])


@login_required
@require_POST
def router_cleanup_copy(request, pk):
    """Save one scanned file to the computer: {"scan": id, "name": "..."}."""
    from . import router_file_copy as rfc
    router = _router(request, pk)
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        data = {}
    scan = RouterCleanup.objects.filter(router=router, pk=str(data.get('scan') or 0), action='scan', status='done').first()
    if not scan:
        return JsonResponse({'ok': False, 'message': 'Scan the router first.'}, status=400)
    name = str(data.get('name') or '')
    plan = (scan.result or {}).get('plan') or {}
    found = next((f for g in plan.get('groups', []) for f in g['files'] if f['name'] == name), None)
    if not found:
        return JsonResponse({'ok': False, 'message': 'That file was not in the scan.'}, status=400)
    running = RouterCleanup.objects.filter(router=router, action='copy', status__in=('waiting', 'running'),
                                           params__name=name, created_at__gte=timezone.now() - timedelta(minutes=60)).first()
    if running:
        return JsonResponse({'ok': True, 'job': as_json(running)})
    try:
        job = rfc.start(request, router, name, found.get('size'))
    except ValueError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001
        logger.info('file copy on router %s failed to start: %s', router.pk, exc)
        return JsonResponse({'ok': False, 'message': f'Could not reach {router.name}: {exc}'[:300]}, status=400)
    return JsonResponse({'ok': True, 'job': as_json(job)})


@login_required
@require_GET
def router_cleanup_download(request, pk, job_id):
    from django.http import FileResponse
    from .router_file_copy import _path
    router = _router(request, pk)
    job = RouterCleanup.objects.filter(router=router, pk=job_id, action='copy', status='done').first()
    path = _path(job) if job else None
    if not job or not path.exists() or (job.result or {}).get('purged'):
        raise Http404
    name = (job.params or {}).get('name', 'router-file').rsplit('/', 1)[-1]
    return FileResponse(path.open('rb'), as_attachment=True, filename=name)
