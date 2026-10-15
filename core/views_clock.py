"""Control Center › Router clock (core/router_clock.py), and the TapTap Link upload endpoint."""
import hmac
import json
from zoneinfo import available_timezones

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import agent as link
from . import router_clock as rc
from .models import AgentCommand
from .utils import log


def _router(request, pk):
    return get_object_or_404(request.user.business.routers, pk=pk)


def _payload(router, reading):
    return {'success': True, 'clock': reading, 'via': rc._via(router), 'manual_command': rc.manual_command(router),
            'server_utc': timezone.now().isoformat()}


@login_required
@require_GET
def clock_status(request, pk):
    router = _router(request, pk)
    data = _payload(router, rc.status(router))
    if request.GET.get('zones') == '1':
        data['zones'] = sorted(z for z in available_timezones() if '/' in z and not z.startswith(('Etc/', 'SystemV/', 'posix/', 'right/')))
    return JsonResponse(data)


@login_required
@require_POST
def clock_read(request, pk):
    router = _router(request, pk)
    try:
        reading = rc.read(router, request.user)
    except ValueError as exc:
        return JsonResponse({'success': False, 'message': str(exc)}, status=400)
    return JsonResponse(_payload(router, reading))


@login_required
@require_POST
def clock_set(request, pk):
    router = _router(request, pk)
    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        body = {}
    ntp = body.get('ntp')
    ntp = ntp if ntp in (True, False) else None
    try:
        reading = rc.correct(router, request.user, tz=body.get('tz', ''), manual=body.get('manual', ''), ntp=ntp)
    except ValueError as exc:
        return JsonResponse({'success': False, 'message': str(exc)}, status=400)
    what = 'manual time ' + body['manual'] if body.get('manual') else 'correct time'
    log(router.business, 'Router Clock', f'{router.name}: clock set to {what}'
        + (f', time zone {body["tz"]}' if body.get('tz') else '') + (', NTP on' if ntp is True else ', NTP off' if ntp is False else ''))
    return JsonResponse(_payload(router, reading))


@csrf_exempt
def agent_clock(request):
    """TapTap Link: a MikroTik's answer to a clock command (signed with the command's nonce)."""
    if request.method != 'POST':
        return HttpResponse(status=405)
    if len(request.body or b'') > 4000:
        return HttpResponse('payload too large', status=413, content_type='text/plain')
    try:
        cid = int(request.GET.get('c', '0'))
    except ValueError:
        return HttpResponse(status=400)
    cmd = AgentCommand.objects.select_related('router__business').filter(pk=cid, kind='clock').first()
    if not cmd:
        return HttpResponse(status=404)
    if not hmac.compare_digest(link.nonce(cmd), str(request.GET.get('n', ''))):
        return HttpResponse(status=403)
    if cmd.status in ('failed', 'expired', 'cancelled') or cmd.expires_at < timezone.now():
        return HttpResponse(status=410)
    reading = rc.receive_link(cmd, request.body)
    return JsonResponse({'ok': True, 'verdict': reading.get('verdict')})
