"""Fit endpoints (JSON, used by static/js/fit.js).

The business is always request.tt_business (set by TeamAccessMiddleware from the signed-in person's
own memberships) and permissions are request.tt_perms. Conversations and actions are looked up with
business *and* user in the filter, so an id from another business or another person is a 404.
"""
import json

from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import fit_actions, fit_ai, fit_tools
from .models_fit import FitAction, FitConversation, FitMessage, FitSettings
from .permissions import ROLES

SETTINGS_PERMS = ('settings.manage',)


def _biz(request):
    b = getattr(request, 'tt_business', None)
    if b is None:
        raise Http404
    return b


def _perms(request):
    return frozenset(getattr(request, 'tt_perms', None) or ())


def _settings(b):
    s, _ = FitSettings.objects.get_or_create(business=b)
    return s


def _body(request):
    try:
        return json.loads(request.body or '{}')
    except ValueError:
        return {}


def _can_manage(request):
    return getattr(request, 'tt_role', None) == 'owner' or any(p in _perms(request) for p in SETTINGS_PERMS)


def _ctx(request, b, s, conv=None):
    role = getattr(request, 'tt_role', None)
    return fit_tools.FitContext(business=b, user=request.user, perms=_perms(request), settings=s, conversation=conv,
                                role_label=ROLES.get(role, ('Staff',))[0] if role else 'Staff')


def _conv(request, b, pk=None, create=False):
    qs = FitConversation.objects.filter(business=b, user=request.user)
    pk = int(pk) if str(pk or '').isdigit() else None
    c = qs.filter(pk=pk).first() if pk else qs.first()
    if c is None and create:
        c = FitConversation.objects.create(business=b, user=request.user)
    return c


def _refresh_cards(b, s, cards):
    """Action cards as they are *now* (done / expired / allowed changes since the reply was written)."""
    acts = cards.get('actions') or []
    if not acts:
        return cards
    live = {a.pk: a for a in FitAction.objects.filter(business=b, pk__in=[x.get('id') for x in acts])}
    fresh = []
    for x in acts:
        a = live.get(x.get('id'))
        if a:
            fresh.append(fit_actions.card_json(a, s, link=x.get('link', ''), danger=x.get('danger')))
    return {**cards, 'actions': fresh}


def _settings_json(s, can_manage):
    return {'enabled': s.enabled, 'allowed': list(s.allowed or []), 'auto_close_reports': s.auto_close_reports,
            'can_manage': can_manage,
            'groups': [{'key': k, 'label': v[0], 'help': v[1]} for k, v in fit_actions.GROUPS.items()]}


@login_required
@require_GET
def fit_state(request):
    b = _biz(request)
    s = _settings(b)
    manage = _can_manage(request)
    if not s.enabled and not manage:
        return JsonResponse({'ok': True, 'enabled': False})
    conv = _conv(request, b)
    msgs = []
    if conv:
        for m in FitMessage.objects.filter(conversation=conv).exclude(role='tool').order_by('-id')[:30][::-1]:
            if m.role == 'assistant' and not m.content and not m.cards:
                continue                                  # a tool-call step, not something to show
            msgs.append({'role': m.role, 'text': m.content, 'cards': _refresh_cards(b, s, m.cards or {}),
                         'at': timezone.localtime(m.created_at).isoformat()})
    ctx = _ctx(request, b, s)
    u = request.user
    return JsonResponse({
        'ok': True, 'enabled': s.enabled, 'ready': fit_ai.ready(),
        'me': {'name': u.first_name or u.get_full_name() or u.username},
        'business': {'name': b.business_name, 'color': b.brand_color or '#1769e0'},
        'settings': _settings_json(s, manage),
        'conversation': conv.pk if conv else None, 'messages': msgs,
        'insights': fit_tools.insights(ctx) if s.enabled else [],
        'suggestions': _suggestions(ctx),
    })


def _suggestions(ctx):
    out = []
    if ctx.can(fit_tools.NETWORK_PERMS):
        out += ['Summarise what happened on my network today', 'Run an internet check on my router']
    if ctx.can(fit_tools.FINANCE_PERMS):
        out.append('How are my finances this month?')
    if ctx.can(fit_tools.AGENT_PERMS):
        out.append('Which agents’ sales dropped?')
    if ctx.can(fit_tools.VOUCHER_PERMS):
        out.append('Go through the open customer reports')
    if not out:
        out = ['What can you help me with?']
    return out[:5]


@login_required
@require_POST
def fit_ask(request):
    b = _biz(request)
    s = _settings(b)
    if not s.enabled:
        return JsonResponse({'ok': False, 'message': 'Fit is switched off for this business.'}, status=403)
    d = _body(request)
    text = str(d.get('message') or '').strip()
    if not text:
        return JsonResponse({'ok': False, 'message': 'Ask Fit something first.'}, status=400)
    if not fit_ai.rate_ok(request.user):
        return JsonResponse({'ok': False, 'message': 'That is a lot of questions in a few minutes. Give Fit a short break.'}, status=429)
    conv = _conv(request, b, pk=d.get('conversation'), create=True)
    ctx = _ctx(request, b, s, conv)
    page = d.get('page') if isinstance(d.get('page'), dict) else None
    try:
        out = fit_ai.ask(ctx, text, page=page)
    except fit_ai.FitAIError as exc:
        text = str(exc)
        if exc.detail and _can_manage(request):          # owners see the real cause; staff get the short message
            text += f'\n\n**Why:** {exc.detail}\n\nRun `python manage.py fit_check` on the server for a full connection test.'
        FitMessage.objects.create(conversation=conv, role='assistant', content=text)
        return JsonResponse({'ok': True, 'conversation': conv.pk, 'reply': text, 'cards': {}, 'mood': 'sad'})
    return JsonResponse({'ok': True, 'conversation': conv.pk, **out})


@login_required
@require_POST
def fit_action(request, pk):
    b = _biz(request)
    s = _settings(b)
    a = get_object_or_404(FitAction, pk=pk, business=b, proposed_to=request.user)
    decision = _body(request).get('decision')
    if decision == 'dismiss':
        if a.status == 'proposed':
            a.status, a.decided_by, a.decided_at = 'dismissed', request.user, timezone.now()
            a.save(update_fields=['status', 'decided_by', 'decided_at'])
        return JsonResponse({'ok': True, 'card': fit_actions.card_json(a, s)})
    if decision != 'run':
        return JsonResponse({'ok': False, 'message': 'Choose run or dismiss.'}, status=400)
    ok, msg = fit_actions.run(a, request.user, _perms(request), s)
    a.refresh_from_db()
    return JsonResponse({'ok': ok, 'message': msg, 'card': fit_actions.card_json(a, s)}, status=200 if ok else 400)


@login_required
@require_POST
def fit_settings(request):
    b = _biz(request)
    if not _can_manage(request):
        return JsonResponse({'ok': False, 'message': 'Only the owner or someone with Settings rights can change what Fit may do.'}, status=403)
    s = _settings(b)
    d = _body(request)
    if 'allowed' in d and isinstance(d['allowed'], list):
        s.allowed = [g for g in fit_actions.GROUPS if g in d['allowed']]
    if 'enabled' in d:
        s.enabled = bool(d['enabled'])
    if 'auto_close_reports' in d:
        s.auto_close_reports = bool(d['auto_close_reports'])
    s.updated_by = request.user
    s.save()
    from .utils import log
    log(b, 'Fit settings', 'Allowed: ' + (', '.join(fit_actions.GROUPS[g][0] for g in s.allowed) or 'read-only'))
    return JsonResponse({'ok': True, 'settings': _settings_json(s, True)})


@login_required
@require_POST
def fit_new(request):
    b = _biz(request)
    conv = FitConversation.objects.create(business=b, user=request.user)
    return JsonResponse({'ok': True, 'conversation': conv.pk})


# ─────────────────────────────── Monitors page ───────────────────────────────
VIEW_MONITOR_PERMS = ('vouchers.view', 'vouchers.support', 'network.manage', 'settings.manage')


def _can_change_monitors(request):
    return getattr(request, 'tt_role', None) == 'owner' or any(p in _perms(request) for p in fit_actions.MONITOR_PERMS)


@login_required
def fit_monitors_page(request):
    """Standing watches: list, create (same checks as Fit's proposal), and their log."""
    from django.contrib import messages
    from django.shortcuts import redirect, render
    from . import fit_monitors
    from .models_fit import FitMonitor, FitMonitorEvent
    b = _biz(request)
    if not (getattr(request, 'tt_role', None) == 'owner' or any(p in _perms(request) for p in VIEW_MONITOR_PERMS)):
        raise Http404
    can_change = _can_change_monitors(request)
    if request.method == 'POST':
        if not can_change:
            raise Http404
        data = {k: v for k, v in request.POST.items() if k != 'csrfmiddlewaretoken' and v not in ('', None)}
        for flag in ('email', 'notify_member', 'members_only', 'once'):
            data[flag] = flag in request.POST
        try:
            params, *_ = fit_actions._check_create_monitor(b, data)
            msg = fit_actions._do_create_monitor(b, params, request.user)
            from .utils import log
            log(b, 'Monitor', msg)
            messages.success(request, msg)
        except fit_actions.FitActionError as exc:
            messages.error(request, str(exc))
        return redirect('fit_monitors_page')
    monitors = list(FitMonitor.objects.filter(business=b).select_related('created_by'))
    for m in monitors:
        m.what = fit_monitors.describe(m)
        m.live = m.active and not (m.expires_at and m.expires_at <= timezone.now())
    events = FitMonitorEvent.objects.filter(business=b).select_related('monitor')[:60]
    kinds = [{'key': k, 'label': v[0], 'needs': v[1], 'responses': v[3]} for k, v in fit_monitors.KINDS.items()]
    plans = sorted(set(b.plans.values_list('name', flat=True)) | set(
        b.vouchers.exclude(plan_name='').values_list('plan_name', flat=True).distinct()[:200]))
    return render(request, 'core/fit_monitors.html', {
        'monitors': monitors, 'events': events, 'kinds': kinds, 'plans': plans, 'routers': b.routers.all(),
        'can_change': can_change, 'responses': FitMonitor.RESPONSES,
        'slots': fit_monitors.slots_report(b)['plans'][:12]})


@login_required
@require_POST
def fit_monitor_toggle(request, pk):
    from django.contrib import messages
    from django.shortcuts import redirect
    from . import fit_monitors
    from .models_fit import FitMonitor
    b = _biz(request)
    if not _can_change_monitors(request):
        raise Http404
    m = get_object_or_404(FitMonitor, business=b, pk=pk)
    op = request.POST.get('op')
    if op == 'pause':
        m.active = False; m.save(update_fields=['active']); messages.success(request, f'“{m.name}” paused.')
    elif op == 'resume':
        m.active = True
        if m.expires_at and m.expires_at <= timezone.now():
            m.expires_at = None
        m.save(update_fields=['active', 'expires_at']); messages.success(request, f'“{m.name}” is watching again.')
    elif op == 'delete':
        name = m.name; m.delete(); messages.success(request, f'“{name}” deleted.')
    elif op == 'test':
        r = fit_monitors.dry_run(m)
        n = r.get('full_now', r.get('true_now', r.get('would_free', 0)))
        ex = ', '.join(str(x if not isinstance(x, dict) else x.get('code')) for x in (r.get('examples') or [])[:6])
        messages.info(request, f'Test of “{m.name}”: {n} match{"es" if n != 1 else ""} right now' + (f' — {ex}' if ex else '') + '. Nothing was sent or changed.')
    return redirect('fit_monitors_page')


# ─────────────────────────────── live dashboards (core/fit_charts) ───────────────────────────────

def _chart_ctx(request):
    b = _biz(request)
    return _ctx(request, b, _settings(b))


@login_required
def fit_dashboards_page(request):
    from django.shortcuts import render
    from .models_fit import FitDashboard
    ctx = _chart_ctx(request)
    return render(request, 'core/fit_dashboards.html', {'dashboards': FitDashboard.objects.filter(business=ctx.business), 'dash': None})


@login_required
def fit_dashboard(request, pk):
    from django.shortcuts import get_object_or_404, render
    from .models_fit import FitDashboard
    ctx = _chart_ctx(request)
    dash = get_object_or_404(FitDashboard, business=ctx.business, pk=pk)
    if request.method == 'POST' and request.POST.get('delete'):
        from django.shortcuts import redirect
        dash.delete()
        return redirect('fit_dashboards_page')
    return render(request, 'core/fit_dashboards.html', {'dashboards': FitDashboard.objects.filter(business=ctx.business), 'dash': dash})


@login_required
def fit_dashboard_data(request, pk):
    from django.shortcuts import get_object_or_404
    from . import fit_charts
    from .models_fit import FitDashboard
    ctx = _chart_ctx(request)
    dash = get_object_or_404(FitDashboard, business=ctx.business, pk=pk)
    return JsonResponse({'ok': True, 'currency': ctx.business.currency or 'D', **fit_charts.dashboard_data(ctx, dash)})


@login_required
def fit_chart_data(request):
    """Refresh one chart card shown in the chat (live sources)."""
    from . import fit_charts
    ctx = _chart_ctx(request)
    spec = {k: request.GET.get(k) for k in ('source', 'type', 'title', 'period', 'days', 'hours', 'router') if request.GET.get(k)}
    try:
        return JsonResponse({'ok': True, **fit_charts.data(ctx, spec)})
    except (fit_charts.ChartError, ValueError) as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
