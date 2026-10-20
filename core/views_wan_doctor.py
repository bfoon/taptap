"""Internet Doctor — views (the card on the Internet lines page). Logic lives in core/wan_doctor.py."""
import json

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from . import wan_doctor as doc
from .mikrotik import MikroTikService
from .utils import log
from .views_wan import _audit, _on_link, _router, _setup

_LINK_MSG = ('{name} is on TapTap Link and its TapTap Tunnel is not connected right now, so TapTap cannot talk to it live. '
             'Download the Internet Doctor script and paste it into the router’s terminal — it then works on the router by itself.')


def _body(request):
    try:
        return json.loads(request.body or '{}')
    except ValueError:
        return {}


def _connect(router):
    try:
        return MikroTikService(router).connect(), None
    except Exception as exc:
        return None, JsonResponse({'success': False, 'message': f'Could not reach {router.name}: {exc}'}, status=502)


def _link_only(router):
    return JsonResponse({'success': False, 'via_link': True, 'message': _LINK_MSG.format(name=router.name)}, status=409)


@login_required
def doctor_status(request, pk):
    """Is the Doctor on the router, what did it do lately. Quick — no tests."""
    router = _router(request, pk)
    if _on_link(router):
        return JsonResponse({'success': True, 'via_link': True, 'installed': None, 'recent': [], 'status': '',
                             'lines': doc.lines_from_setup(_setup(router)), 'escalation': doc.ESCALATION})
    svc, err = _connect(router)
    if err:
        return err
    try:
        out = {'success': True, 'installed': doc.installed(svc), 'recent': doc.recent_activity(svc), 'status': doc.status_line(svc),
               'lines': doc.lines_for(svc, _setup(router)), 'escalation': doc.ESCALATION}
    finally:
        svc.close()
    return JsonResponse(out)


@login_required
@require_POST
def doctor_check(request, pk):
    """Live check of every line (pings from the router)."""
    router = _router(request, pk)
    if _on_link(router):
        return _link_only(router)
    svc, err = _connect(router)
    if err:
        return err
    try:
        out = doc.diagnose(svc, doc.lines_for(svc, _setup(router)))
        out['installed'] = doc.installed(svc)
        out['recent'] = doc.recent_activity(svc)
    except Exception as exc:
        return JsonResponse({'success': False, 'message': f'The check failed: {exc}'}, status=500)
    finally:
        svc.close()
    return JsonResponse({'success': True, **out})


@login_required
@require_POST
def doctor_install(request, pk):
    router = _router(request, pk); data = _body(request)
    if _on_link(router):
        return _link_only(router)
    allow_mac = data.get('allow_mac', True) is not False
    svc, err = _connect(router)
    if err:
        return err
    try:
        lines = doc.lines_for(svc, _setup(router))
        watched = doc.install_on_router(svc, lines, allow_mac=allow_mac, router_name=router.name)
    except doc.DoctorError as exc:
        return JsonResponse({'success': False, 'message': str(exc)}, status=400)
    except Exception as exc:
        _audit(request, router, 'doctor_install', {}, False, str(exc))
        return JsonResponse({'success': False, 'message': f'Could not install: {exc}'}, status=500)
    finally:
        svc.close()
    _audit(request, router, 'doctor_install', {'lines': watched, 'allow_mac': allow_mac})
    log(router.business, 'Internet Doctor On', f'{router.name}: watching ' + ', '.join(watched))
    return JsonResponse({'success': True, 'installed': True, 'lines': watched})


@login_required
@require_POST
def doctor_remove(request, pk):
    router = _router(request, pk)
    if _on_link(router):
        return _link_only(router)
    svc, err = _connect(router)
    if err:
        return err
    try:
        removed = doc.uninstall_from_router(svc)
    finally:
        svc.close()
    _audit(request, router, 'doctor_remove', {'removed': removed})
    log(router.business, 'Internet Doctor Off', router.name)
    return JsonResponse({'success': True, 'installed': False, 'removed': removed})


# fixes, plus switching a line on/off and putting it in or out of use
STEPS = {'refresh', 'new_mac', 'reset_mac', 'turn_on', 'turn_off', 'use', 'unuse'}


@login_required
@require_POST
def doctor_fix(request, pk):
    router = _router(request, pk); data = _body(request)
    iface, step = str(data.get('interface') or '').strip(), str(data.get('step') or '')
    if _on_link(router):
        return _link_only(router)
    if step not in STEPS or not iface:
        return JsonResponse({'success': False, 'message': 'Choose a line and an action.'}, status=400)
    svc, err = _connect(router)
    if err:
        return err
    try:
        lines = doc.lines_for(svc, _setup(router))
        if iface not in {l['interface'] for l in lines}:
            return JsonResponse({'success': False, 'message': f'{iface} is not one of this router’s internet lines.'}, status=400)
        if step in {'turn_on', 'turn_off'}:
            message = doc.turn_line(svc, iface, step == 'turn_on', lines)
        elif step in {'use', 'unuse'}:
            message = doc.set_in_use(svc, iface, step == 'use', lines)
        else:
            message = doc.fix(svc, iface, step)
    except doc.DoctorError as exc:
        return JsonResponse({'success': False, 'message': str(exc)}, status=400)
    except Exception as exc:
        _audit(request, router, f'doctor_{step}', {'interface': iface}, False, str(exc))
        return JsonResponse({'success': False, 'message': f'The fix failed: {exc}'}, status=500)
    finally:
        svc.close()
    _audit(request, router, f'doctor_{step}', {'interface': iface})
    log(router.business, 'Internet Line Change' if step in {'turn_on', 'turn_off', 'use', 'unuse'} else 'Internet Doctor Fix', f'{router.name}: {message}')
    return JsonResponse({'success': True, 'message': message})


@login_required
def doctor_script(request, pk):
    """.rsc to install or remove the Doctor by hand (works for TapTap Link routers too)."""
    router = _router(request, pk)
    kind = request.GET.get('kind', 'install')
    if kind == 'remove':
        text = doc.render_remove_rsc()
    else:
        lines = doc.lines_from_setup(_setup(router))
        if not lines and not _on_link(router):
            svc, err = _connect(router)
            if not err:
                try:
                    lines = doc.lines_for(svc, _setup(router))
                finally:
                    svc.close()
        if not lines:
            return HttpResponse('No internet lines known yet — press “Detect again” on the Internet lines page first.',
                                status=400, content_type='text/plain')
        text = doc.render_install_rsc(lines, allow_mac=request.GET.get('mac') != 'no', router_name=router.name)
    resp = HttpResponse(text, content_type='text/plain; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="taptap-{slugify(router.name) or "router"}-internet-doctor{"-remove" if kind == "remove" else ""}.rsc"'
    return resp
