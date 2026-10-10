"""Internet Lines designer — views."""
import json
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from . import wan
from .mikrotik import MikroTikService
from .models import WanSetup, RouterConfigSnapshot, RouterConfigChange
from .utils import log


def _on_link(router):
    """True when this router must be handled through TapTap Link right now
    (enrolled in Link and its TapTap Tunnel is not healthy)."""
    from .linkops import uses_link
    return uses_link(router)


_LINK_ONLY_MSG = ('{name} is on TapTap Link and its TapTap Tunnel is not connected right now. '
                  'Changing Internet lines needs a live RouterOS connection so TapTap can undo the change '
                  'if the Internet drops. Wait for the tunnel to come back (see the TapTap Link page), '
                  'or make the change from WinBox.')


def _router(request, pk):
    return get_object_or_404(request.user.business.routers, pk=pk)


def _setup(router):
    return WanSetup.objects.get_or_create(router=router)[0]


def _body(request):
    try:
        return json.loads(request.body or '{}')
    except ValueError:
        return {}


def _client_facts(data, stored):
    """Facts for preview: detected ones when we have them, else what the owner typed (offline mode)."""
    if stored.get('links') is not None and not data.get('manual'):
        return stored
    f = data.get('facts') or {}
    return {'v7': bool(f.get('v7', True)), 'links': [], 'lans': [l for l in (f.get('lans') or []) if isinstance(l, dict)][:20],
            'hotspot': bool(f.get('hotspot')), 'manual': True}


def summary(cfg, facts):
    """Plain-language description + diagram data of what the design does."""
    L, strat = cfg['links'], cfg['strategy']
    lines, flows = [], []
    if strat == 'single':
        l = L[0]
        lines.append(f'All customers use {l["label"]}.')
        if len(facts.get('links', [])) > 1:
            lines.append('Other lines stay connected but carry no customer traffic.')
        flows = [{'id': l['id'], 'interface': l['interface'], 'label': l['label'], 'share': 100, 'role': 'Only line'}]
    elif strat == 'failover':
        lines.append(f'{L[0]["label"]} carries all traffic. If it fails, ' + ', then '.join(x['label'] for x in L[1:]) + ' takes over.')
        lines.append('When the main line recovers, traffic moves back to it automatically.')
        flows = [{'id': l['id'], 'interface': l['interface'], 'label': l['label'], 'share': 100 if i == 0 else 0, 'role': 'Main' if i == 0 else f'Backup {i}'} for i, l in enumerate(L)]
    elif strat == 'balance':
        tot = sum(l['weight'] for l in L)
        shares = [round(l['weight'] * 100 / tot) for l in L]
        lines.append('Customers are spread across ' + ' and '.join(f'{l["label"]} ({s}%)' for l, s in zip(L, shares)) + '.')
        lines.append('Each customer stays on one line for the whole session — good for HotSpot, banking and video calls.' if cfg['sticky'] == 'src-address'
                     else 'Each new connection can take a different line — the most even spread, but some sites may log customers out.')
        lines.append('If a line fails, its customers move to the others within about 10–30 seconds.')
        flows = [{'id': l['id'], 'interface': l['interface'], 'label': l['label'], 'share': s, 'role': f'{l["weight"]} of {tot} parts'} for l, s in zip(L, shares)]
    elif strat == 'ecmp':
        lines.append('Connections are shared equally between ' + ', '.join(l['label'] for l in L) + '.')
        lines.append('A line that stops answering is left out until it recovers.')
        flows = [{'id': l['id'], 'interface': l['interface'], 'label': l['label'], 'share': round(100 / len(L)), 'role': 'Combined'} for l in L]
    else:
        by = {l['interface']: l for l in L}
        for sp in cfg['splits']:
            lines.append(f'{sp.get("label") or sp["network"]} uses {by[sp["interface"]]["label"]}.')
        lines.append(f'Everything else uses {L[0]["label"]}, with the other lines as backup. Each network falls back to another line if its own fails.')
        flows = [{'id': l['id'], 'interface': l['interface'], 'label': l['label'], 'share': None, 'role': ', '.join(sp.get('label') or sp['network'] for sp in cfg['splits'] if sp['interface'] == l['interface']) or ('Everything else' if i == 0 else 'Backup')} for i, l in enumerate(L)]
    if cfg['health']:
        lines.append('Health checks: ' + '; '.join(f'{l["label"]} pings {l["check"]}' for l in L) + ' — a dead ISP is noticed even when the modem is still on.')
    if strat != 'single' and len(L) > 1:
        lines.append('Replies always leave by the line they arrived on, so port forwards and remote access keep working.')
    return lines, flows


def _preview_payload(router, setup, data):
    facts = _client_facts(data, setup.facts or {})
    cfg, errors, warnings = wan.normalize(data.get('config') or {}, facts)
    out = {'errors': errors, 'warnings': warnings, 'config': cfg}
    if not errors:
        plan = wan.build_plan(cfg, facts, run='preview')
        out['script'] = wan.render_script(plan, cfg, facts, router.business.business_name)
        out['summary'], out['flows'] = summary(cfg, facts)
        counts = {}
        for op in plan['ops']:
            counts[op.get('group')] = counts.get(op.get('group'), 0) + 1
        out['counts'] = {k: v for k, v in counts.items() if k != 'cleanup'}
        out['ops'] = len(plan['ops'])
        out['untouched'] = [l for l in facts.get('links', []) if l['interface'] not in {x['interface'] for x in cfg['links']} and l['type'] in {'dhcp', 'pppoe'}]
    return out, facts


# ─────────────────────────── pages ───────────────────────────
@login_required
def wan_designer(request, pk):
    router = _router(request, pk); setup = _setup(router)
    snap = RouterConfigSnapshot.objects.filter(router=router).first()
    return render(request, 'core/wan_designer.html', {
        'router': router, 'setup': setup, 'current': (snap.load_balancing if snap else {}) or {},
        'strategies': wan.STRATEGIES, 'check_hosts': wan.CHECK_HOSTS,
        'state': {'config': setup.config, 'status': setup.status, 'facts': setup.facts,
                  'confirm_by': setup.confirm_by.isoformat() if setup.confirm_by else None,
                  'applied_at': setup.applied_at.isoformat() if setup.applied_at else None, 'last_result': setup.last_result},
    })


@login_required
def wan_detect(request, pk):
    router = _router(request, pk); setup = _setup(router)
    try:
        if _on_link(router):
            from .linkops import SnapshotService
            svc = SnapshotService(router)   # reads the latest TapTap Link sync
        else:
            svc = MikroTikService(router).connect()
        try:
            facts = wan.detect(svc)
        finally:
            svc.close()
    except Exception as exc:
        return JsonResponse({'success': False, 'message': str(exc)}, status=502)
    analysis = facts.pop('analysis', {})
    setup.facts = facts; setup.save(update_fields=['facts', 'updated_at'])
    from .wan_names import named
    analysis = named(router, analysis)
    return JsonResponse({'success': True, 'facts': facts, 'current': {'method': analysis.get('method'), 'description': analysis.get('description'),
                                                                     'links': analysis.get('wan_links', []), 'warnings': analysis.get('warnings', [])}})


@login_required
@require_POST
def wan_preview(request, pk):
    router = _router(request, pk); setup = _setup(router); data = _body(request)
    out, facts = _preview_payload(router, setup, data)
    if not out['errors']:
        setup.config = data.get('config') or {}
        if facts.get('manual'):
            setup.facts = {**facts, 'links': facts.get('links', [])}
        setup.save(update_fields=['config', 'facts', 'updated_at'])
    return JsonResponse(out)


@login_required
def wan_script(request, pk):
    router = _router(request, pk); setup = _setup(router); kind = request.GET.get('kind', 'setup')
    facts = setup.facts or {'v7': True, 'links': [], 'lans': []}
    if kind == 'undo':
        original = setup.original or wan.original_from_facts(facts)
        text = ('# TapTap Internet Lines — UNDO\n# Removes everything TapTap added and restores the DHCP/PPPoE clients.\n'
                + wan.render_undo(original, facts.get('v7', True), guarded=False) + '\n')
    else:
        cfg, errors, _ = wan.normalize(setup.config or {}, facts)
        if errors:
            return HttpResponse('Fix these first:\n' + '\n'.join(errors), status=400, content_type='text/plain')
        text = wan.render_script(wan.build_plan(cfg, facts), cfg, facts, router.business.business_name)
    resp = HttpResponse(text, content_type='text/plain; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="taptap-{slugify(router.name) or "router"}-{"undo" if kind == "undo" else "internet-lines"}.rsc"'
    return resp


# ─────────────────────────── apply / confirm / undo ───────────────────────────
def _audit(request, router, op, fields, ok=True, error=''):
    RouterConfigChange.objects.create(business=router.business, router=router, actor=request.user, resource_path='/taptap/internet-lines',
                                      operation=op, fields=fields, status='success' if ok else 'failed', error=error[:2000])


@login_required
@require_POST
def wan_apply(request, pk):
    router = _router(request, pk); setup = _setup(router); data = _body(request)
    minutes = 0 if data.get('undo_minutes') in (0, '0') else max(2, min(15, int(data.get('undo_minutes') or 5)))
    if router.connection_mode == 'agent' and not minutes:
        # Over TapTap Tunnel a bad WAN change can cut the tunnel itself, so the
        # on-router safety timer is mandatory: it undoes the change unless TapTap
        # can confirm through the tunnel afterwards.
        minutes = 5
    if _on_link(router):
        return JsonResponse({'success': False, 'message': _LINK_ONLY_MSG.format(name=router.name)}, status=409)
    try:
        svc = MikroTikService(router).connect()
    except Exception as exc:
        return JsonResponse({'success': False, 'message': f'Could not reach {router.name}: {exc}'}, status=502)
    try:
        facts = wan.detect(svc); facts.pop('analysis', None)
        cfg, errors, warnings = wan.normalize(data.get('config') or setup.config or {}, facts)
        if errors:
            return JsonResponse({'success': False, 'message': ' '.join(errors)}, status=400)
        # Keep the very first "before" state across re-applies, so undo always returns to the owner's own setup.
        original = setup.original if setup.original and setup.status in {'pending', 'active'} else wan.capture_original(svc, cfg, facts)
        plan = wan.build_plan(cfg, facts)
        result = wan.apply_plan(svc, plan, facts, original, undo_minutes=minutes)
    except wan.WanError as exc:
        setup.status = 'failed'; setup.last_result = {'error': str(exc)}; setup.save()
        _audit(request, router, 'apply', {'strategy': (data.get('config') or {}).get('strategy')}, False, str(exc))
        return JsonResponse({'success': False, 'message': str(exc)}, status=500)
    except Exception as exc:
        return JsonResponse({'success': False, 'message': f'Unexpected error: {exc}'}, status=500)
    finally:
        svc.close()

    now = timezone.now()
    setup.config = data.get('config') or setup.config; setup.facts = facts; setup.original = original; setup.run_id = plan['run']
    setup.status = 'pending' if minutes else 'active'; setup.applied_at = now; setup.applied_by = request.user
    setup.confirm_by = now + timedelta(minutes=minutes) if minutes else None; setup.confirmed_at = None if minutes else now
    # Re-connect on a fresh session: proves TapTap can still reach the router through the new routing.
    reach, lines = False, []
    try:
        svc2 = MikroTikService(router).connect()
        try:
            reach = True; lines = wan.health(svc2, cfg)
            lb = svc2.analyze_load_balancing()
            RouterConfigSnapshot.objects.filter(router=router).update(load_balancing=lb)
        finally:
            svc2.close()
    except Exception as exc:
        lines = [{'error': str(exc)}]
    setup.last_result = {**result, 'reachable_after': reach, 'health': lines, 'warnings': warnings}
    setup.save()
    _audit(request, router, 'apply', {'strategy': cfg['strategy'], 'lines': [l['interface'] for l in cfg['links']], 'run': plan['run'], 'undo_minutes': minutes})
    log(router.business, 'Internet Lines Applied', f'{router.name}: {wan.STRATEGIES[cfg["strategy"]]} on ' + ', '.join(l['label'] for l in cfg['links']))
    return JsonResponse({'success': True, 'status': setup.status, 'confirm_by': setup.confirm_by.isoformat() if setup.confirm_by else None,
                         'reachable_after': reach, 'health': lines, 'counts': result['counts'], 'notes': result['notes']})


@login_required
@require_POST
def wan_confirm(request, pk):
    router = _router(request, pk); setup = _setup(router)
    if _on_link(router):
        return JsonResponse({'success': False, 'message': _LINK_ONLY_MSG.format(name=router.name)}, status=409)
    try:
        svc = MikroTikService(router).connect()
        try:
            still = wan.confirm(svc)
        finally:
            svc.close()
    except Exception as exc:
        return JsonResponse({'success': False, 'message': f'Could not reach {router.name}: {exc}'}, status=502)
    if not still and setup.status == 'pending':
        setup.status = 'undone'; setup.save(update_fields=['status', 'updated_at'])
        return JsonResponse({'success': False, 'message': 'Too late — the router already undid the change on its own. Apply again when ready.'}, status=409)
    setup.status = 'active'; setup.confirmed_at = timezone.now(); setup.confirm_by = None
    setup.save(update_fields=['status', 'confirmed_at', 'confirm_by', 'updated_at'])
    _audit(request, router, 'confirm', {'run': setup.run_id})
    log(router.business, 'Internet Lines Confirmed', router.name)
    return JsonResponse({'success': True, 'status': 'active'})


@login_required
@require_POST
def wan_undo(request, pk):
    router = _router(request, pk); setup = _setup(router)
    if _on_link(router):
        return JsonResponse({'success': False, 'message': _LINK_ONLY_MSG.format(name=router.name)}, status=409)
    try:
        svc = MikroTikService(router).connect()
        try:
            v7 = svc.ros_version()[0] >= 7
            removed = wan.undo(svc, setup.original or wan.original_from_facts(setup.facts or {}), v7)
        finally:
            svc.close()
    except Exception as exc:
        return JsonResponse({'success': False, 'message': f'Could not reach {router.name}: {exc}'}, status=502)
    setup.status = 'undone'; setup.confirm_by = None; setup.original = {}
    setup.save(update_fields=['status', 'confirm_by', 'original', 'updated_at'])
    _audit(request, router, 'undo', {'removed': removed})
    log(router.business, 'Internet Lines Undone', f'{router.name}: removed {removed} TapTap items and restored the original settings')
    return JsonResponse({'success': True, 'removed': removed})


@login_required
def wan_status(request, pk):
    router = _router(request, pk); setup = _setup(router)
    cfg, errors, _ = wan.normalize(setup.config or {}, setup.facts or {})
    if _on_link(router):
        return JsonResponse({'success': True, 'status': setup.status, 'undo_pending': False, 'health': [], 'via_link': True,
                             'confirm_by': setup.confirm_by.isoformat() if setup.confirm_by else None})
    try:
        svc = MikroTikService(router).connect()
        try:
            pending = wan.undo_pending(svc)
            lines = [] if errors else wan.health(svc, cfg)
        finally:
            svc.close()
    except Exception as exc:
        return JsonResponse({'success': False, 'message': str(exc)}, status=502)
    if setup.status == 'pending' and not pending:
        setup.status = 'undone'; setup.original = {}; setup.save(update_fields=['status', 'original', 'updated_at'])
    return JsonResponse({'success': True, 'status': setup.status, 'undo_pending': pending, 'health': lines,
                         'confirm_by': setup.confirm_by.isoformat() if setup.confirm_by else None})
