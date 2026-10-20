"""Internet Doctor — views (the card on the Internet lines page). Logic lives in core/wan_doctor.py."""
import json

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.utils.text import slugify
from django.utils.timesince import timesince
from django.views.decorators.http import require_POST

from . import wan_doctor as doc
from .mikrotik import MikroTikService
from .utils import log
from .views_wan import _audit, _on_link, _router, _setup

_LINK_MSG = ('{name} is on TapTap Link and its TapTap Tunnel is not connected right now, so TapTap cannot talk to it live. '
             'Download the Internet Doctor script and paste it into the router’s terminal — it then works on the router by itself.')
_LINK_SENT = 'Sent through TapTap Link — the router does it at its next check-in (a few seconds). The page refreshes when the router reports back.'


def _link_info(router):
    """Freshness of what TapTap knows about a Link router."""
    from .models import RouterConfigSnapshot
    snap = RouterConfigSnapshot.objects.filter(router=router).first()
    when = snap.captured_at if snap else None
    return {'synced': f'{timesince(when)} ago' if when else 'never', 'pending': doc.link_pending(router)}


def _link_status(router):
    return {'success': True, 'via_link': True, 'installed': doc.link_installed(router), 'recent': [], 'status': '',
            'lines_state': doc.link_lines_state(router, _setup(router)), 'escalation': doc.ESCALATION, 'link': _link_info(router)}


def _link_refresh(request, router):
    from .linkops import refresh
    try:
        refresh(router, request.user)
    except Exception:
        pass


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
        return JsonResponse(_link_status(router))
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
    """Live check of every line (pings from the router). Through TapTap Link: ask the router for fresh data
    and run the on-router Doctor once, then show what the last sync says."""
    router = _router(request, pk)
    if _on_link(router):
        try:
            doc.link_send(router, 'run', user=request.user)
        except ValueError as exc:
            return JsonResponse({'success': False, 'via_link': True, 'message': str(exc)}, status=409)
        _link_refresh(request, router)
        return JsonResponse({**_link_status(router), 'message': 'Asked ' + router.name + ' for fresh data through TapTap Link — the lines update in about 30 seconds.'})
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
    allow_mac = data.get('allow_mac', True) is not False
    if _on_link(router):
        lines = doc.lines_from_setup(_setup(router))
        if not lines:
            from .linkops import SnapshotService
            lines = doc.lines_for(SnapshotService(router), _setup(router))
        if not lines:
            return JsonResponse({'success': False, 'message': 'No internet lines known yet — press “Detect again” above first.'}, status=400)
        try:
            watched, parts = doc.link_install(router, lines, allow_mac=allow_mac, user=request.user)
        except ValueError as exc:
            return JsonResponse({'success': False, 'via_link': True, 'message': str(exc)}, status=409)
        _audit(request, router, 'doctor_install', {'lines': watched, 'allow_mac': allow_mac, 'via': 'link', 'parts': parts})
        log(router.business, 'Internet Doctor On', f'{router.name}: watching ' + ', '.join(watched) + ' (through TapTap Link)')
        return JsonResponse({'success': True, 'installed': True, 'via_link': True, 'lines': watched, 'message': _LINK_SENT})
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
        try:
            doc.link_send(router, 'remove', user=request.user)
        except ValueError as exc:
            return JsonResponse({'success': False, 'via_link': True, 'message': str(exc)}, status=409)
        _audit(request, router, 'doctor_remove', {'via': 'link'})
        log(router.business, 'Internet Doctor Off', f'{router.name} (through TapTap Link)')
        return JsonResponse({'success': True, 'installed': False, 'via_link': True, 'message': _LINK_SENT})
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
    if step not in STEPS or not iface:
        return JsonResponse({'success': False, 'message': 'Choose a line and an action.'}, status=400)
    if _on_link(router):
        return _link_fix(request, router, iface, step)
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


def _link_fix(request, router, iface, step):
    """The same actions through TapTap Link, with the safety checks done on the last sync."""
    from .linkops import SnapshotService
    snap = SnapshotService(router)
    lines = doc.lines_for(snap, _setup(router))
    line = next((l for l in lines if l['interface'] == iface), None)
    if not line:
        return JsonResponse({'success': False, 'message': f'{iface} is not one of this router’s internet lines.'}, status=400)
    others = [l['label'] for l in lines if l['interface'] != iface and doc.use_state(snap, l['interface'])['state'] != 'out']
    if step in {'turn_off', 'unuse'} and not others:
        return JsonResponse({'success': False, 'message': f'{line["label"]} is the only line customers use — that would cut the internet '
                                                          'and TapTap Link itself. Put another line in use first.'}, status=400)
    params = {'interface': iface}
    if step == 'new_mac':
        if not any(r.get('name') == iface for r in snap.safe_get('/interface/ethernet')):
            return JsonResponse({'success': False, 'message': f'{iface} is not an Ethernet port, so its MAC cannot be changed.'}, status=400)
        params['mac'] = doc.random_mac()
    try:
        doc.link_send(router, step, user=request.user, **params)
    except ValueError as exc:
        return JsonResponse({'success': False, 'via_link': True, 'message': str(exc)}, status=409)
    _link_refresh(request, router)
    what = {'turn_on': 'switch on', 'turn_off': 'switch off', 'use': 'put in use', 'unuse': 'take out of use', 'refresh': 'renew',
            'new_mac': f'give a new MAC ({params.get("mac", "")})', 'reset_mac': 'put the original MAC back on'}[step]
    _audit(request, router, f'doctor_{step}', {'interface': iface, 'via': 'link'})
    log(router.business, 'Internet Line Change' if step in {'turn_on', 'turn_off', 'use', 'unuse'} else 'Internet Doctor Fix',
        f'{router.name}: {what} {line["label"]} (through TapTap Link)')
    return JsonResponse({'success': True, 'via_link': True, 'message': f'{line["label"]}: {what} — ' + _LINK_SENT})
