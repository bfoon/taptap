"""Router schedules — restart / shut down at set times (core/router_schedules.py), on the website and in the app.

    website   /routers/schedules/                 every schedule of the business, the next ones, recent runs
              POST /routers/schedules/save/       create / change
              POST /routers/schedules/action/     skip · unskip · pause · resume · delete · run_now
    app       GET  /api/mobile/v1/schedules/      the same as data (network.manage or vouchers.support may look)
              POST /api/mobile/v1/schedules/      {action: save | skip | unskip | pause | resume | delete | run_now, …}
                                                  changes need network.manage, like the Reboot button

Both sides go through act() below, so the phone can never do more than the website.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import router_schedules as rs
from .models_router_schedule import RouterSchedule, RouterScheduleRun
from .views_mobile import _signed_in


def _schedules(business):
    return (RouterSchedule.objects.filter(router__business=business).select_related('router')
            .order_by('-active', 'next_at', 'router__name', 'id'))


def _runs(business, router_id=None, limit=30):
    qs = RouterScheduleRun.objects.filter(router__business=business).select_related('router')
    if router_id:
        qs = qs.filter(router_id=router_id)
    return list(qs.order_by('-planned_at', '-id')[:limit])


def _router(business, rid):
    try:
        return business.routers.filter(pk=int(rid or 0)).first()
    except (TypeError, ValueError):
        return None


def act(business, data, user):
    """One change from the website or the app. Returns (ok, message, schedule_or_None)."""
    action = str(data.get('action') or '')
    if action == 'save':                      # the kind of schedule comes as 'what' (restart / shutdown)
        return _act_save(business, data, {**data, 'action': data.get('what') or 'restart'}, user)
    if action == 'run_now':
        r = _router(business, data.get('router'))
        what = str(data.get('what') or 'restart')
        if r is None or what not in ('restart', 'shutdown'):
            return False, 'Choose a router and restart or shutdown.', None
        if str(data.get('confirm') or '').strip().lower() != r.name.strip().lower():
            return False, f'Type the router name “{r.name}” to confirm.', None
        ok, msg, _run = rs.run_now(r, what, user)
        if not ok:
            return False, msg, None
        if what == 'restart':
            return True, f'{r.name} is restarting. TapTap tells you when it is back.', None
        return True, f'{r.name} is switching off. Someone has to power it on again.', None
    s = RouterSchedule.objects.filter(pk=int(data.get('id') or 0), router__business=business).select_related('router').first()
    if s is None:
        return False, 'That schedule is gone.', None
    try:
        if action == 'skip':
            planned = rs.skip_next(s, user)
            after = rs.next_after(s, planned)
            then = f' The one after is {timezone.localtime(after):%a %d %b at %H:%M}.' if after else ''
            return True, f'Skipped {timezone.localtime(planned):%a %d %b %H:%M} only.{then}', s
        if action == 'unskip':
            rs.unskip(s)
            return True, 'It runs as planned again.', s
        if action == 'pause':
            rs.set_active(s, False)
            return True, 'Paused — nothing runs until you resume it.', s
        if action == 'resume':
            rs.set_active(s, True)
            return True, f'Back on — next on {timezone.localtime(s.next_at):%a %d %b at %H:%M}.', s
        if action == 'delete':
            s.delete()
            return True, 'Schedule deleted.', None
    except rs.ScheduleError as exc:
        return False, str(exc), None
    return False, 'Unknown action.', None


# ─────────────────────────────── website ───────────────────────────────

@login_required
def router_schedules(request):
    b = request.user.business
    routers = list(b.routers.all().order_by('name'))
    focus = _router(b, request.GET.get('router'))
    edit = None
    if request.GET.get('edit'):
        edit = RouterSchedule.objects.filter(pk=int(request.GET.get('edit') or 0) if str(request.GET.get('edit')).isdigit() else 0,
                                             router__business=b).select_related('router').first()
    now = timezone.now()
    schedules = list(_schedules(b))
    for s in schedules:
        s.text = rs.describe(s)
        s.skipped = bool(s.skip_at and s.next_at and s.skip_at == s.next_at)
    upcoming = [s for s in schedules if s.active and s.next_at][:6]
    for r in routers:
        r.people_online = rs.online_count(r)
        r.is_up = rs.is_online(r)
    return render(request, 'core/router_schedules.html', {
        'routers': routers, 'schedules': schedules, 'upcoming': upcoming, 'runs': _runs(b, focus.pk if focus else None),
        'focus': focus, 'edit': edit, 'now': now, 'weekdays': list(enumerate(rs.WEEKDAYS)),
        'max_window': rs.MAX_WINDOW, 'max_warn': rs.MAX_WARN, 'warn_choices': [0, 5, 10, 15, 30, 60, 120],
    })


@login_required
@require_POST
def router_schedule_save(request):
    data = request.POST.dict()
    data['weekdays'] = request.POST.getlist('weekdays')
    data['email'] = 'email' in request.POST
    data['action'] = 'save'
    ok, msg, s = act(request.user.business, data, request.user)
    (messages.success if ok else messages.error)(request, msg)
    if ok:
        return redirect(f'/routers/schedules/?router={s.router_id}')
    if str(data.get('id') or '').isdigit():
        return redirect(f"/routers/schedules/?edit={data['id']}")
    return redirect('/routers/schedules/' + (f"?router={data['router']}" if str(data.get('router') or '').isdigit() else ''))


def _act_save(business, data, s_data, user):
    s = None
    if data.get('id'):
        s = RouterSchedule.objects.filter(pk=int(data.get('id') or 0), router__business=business).select_related('router').first()
        if s is None:
            return False, 'That schedule is gone.', None
    r = _router(business, data.get('router')) or (s.router if s else None)
    if r is None:
        return False, 'Choose a router.', None
    if s is not None and s.router_id != r.pk:
        s.router = r
    try:
        s = rs.save(r, s_data, user, schedule=s)
    except rs.ScheduleError as exc:
        return False, str(exc), None
    when = timezone.localtime(s.next_at).strftime('%a %d %b at %H:%M')
    return True, f'Saved — {r.name} {rs._verb(s.action)} next on {when}.', s


@login_required
@require_POST
def router_schedule_action(request):
    data = request.POST.dict()
    ok, msg, s = act(request.user.business, data, request.user)
    (messages.success if ok else messages.error)(request, msg)
    rid = data.get('router') or (s.router_id if s else '')
    return redirect('/routers/schedules/' + (f'?router={rid}' if rid else ''))


# ─────────────────────────────── the app ───────────────────────────────

def _mobile_need(request, write):
    from .views_mobile import _business, _err, _perms
    b = _business(request)
    if b is None:
        return None, _err('This login is not linked to a TapTap business.', 403)
    perms = _perms(request)
    if write and 'network.manage' not in perms:
        return None, _err('Your role does not allow changing router schedules.', 403)
    if not write and not ({'network.manage', 'vouchers.support'} & perms):
        return None, _err('Your role cannot see routers.', 403)
    return b, None


def _mobile(request):
    """GET: schedules, the next runs, recent runs, routers. POST: one change (see act())."""
    from .views_mobile import _body, _perms, clean
    write = request.method == 'POST'
    b, refused = _mobile_need(request, write)
    if refused:
        return refused
    if write:
        d = _body(request)
        ok, msg, s = act(b, d, request.user)
        if not ok:
            return JsonResponse({'ok': False, 'message': msg}, status=400)
        return JsonResponse(clean({'ok': True, 'message': msg, 'schedule': rs.as_json(s) if s else None}))
    focus = _router(b, request.GET.get('router'))
    now = timezone.now()
    schedules = [rs.as_json(s, now) for s in _schedules(b) if not focus or s.router_id == focus.pk]
    routers = [{'id': r.pk, 'name': r.name, 'online': rs.is_online(r), 'people_online': rs.online_count(r),
                'mode': 'TapTap Link' if r.connection_mode == 'agent' else 'Direct API'} for r in b.routers.all().order_by('name')]
    return JsonResponse(clean({'ok': True, 'schedules': schedules, 'runs': [rs.run_json(x) for x in _runs(b, focus.pk if focus else None)],
                               'routers': routers, 'can_manage': 'network.manage' in _perms(request),
                               'weekdays': rs.WEEKDAYS, 'max_window': rs.MAX_WINDOW, 'max_warn': rs.MAX_WARN}))


mobile = _signed_in(_mobile)
