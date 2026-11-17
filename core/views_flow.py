"""Connection lab (voucher / device pages), internet-line switch, DHCP lease, and the Traffic page's
Idle / Slow / Internet lines / DHCP tabs.  Engines: core/conn_lab.py, core/line_steer.py, core/dhcp_lease.py,
core/traffic_idle.py.  Every router test here is a NetTest (core/nettools.py), so it also shows in Network tools →
Recent tests with who ran it."""
import json
import logging
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import DatabaseError
from django.http import Http404, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import conn_lab as lab
from . import dhcp_lease as dl
from . import line_steer as ls
from . import nettools as nt
from . import traffic_idle as ti
from .models_nettools import NetTest
from .views_nettools import _queue_late, as_json

logger = logging.getLogger('taptap.flow')


def _b(request):
    return request.user.business


def _missing_table(exc):
    t = str(exc).lower()
    return 'no such table' in t or ('relation' in t and 'does not exist' in t) or 'undefinedtable' in t


def _safe(view):
    """Every answer of these pages is JSON the page can show — never a bare "Server Error (500)" page.
    The full traceback still goes to the server log (docker compose logs web)."""
    @wraps(view)
    def inner(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except Http404:
            raise
        except DatabaseError as exc:
            logger.exception('flow view %s: database error', view.__name__)
            msg = ls.MIGRATE_HINT if _missing_table(exc) else \
                'TapTap could not read its database just now. Try again in a moment; if it keeps happening, the details are in the server log.'
            return JsonResponse({'ok': False, 'message': msg}, status=500)
        except Exception as exc:  # noqa: BLE001 — shown to the person, logged in full
            logger.exception('flow view %s failed', view.__name__)
            return JsonResponse({'ok': False, 'message': f'TapTap hit an error here ({type(exc).__name__}). The details are in the server log '
                                                         f'(docker compose logs web) — please send them so it can be fixed.'}, status=500)
    return inner


def _perms(request):
    return getattr(request, 'tt_perms', frozenset())


def _can_network(request):
    return 'network.manage' in _perms(request)


def _body(request):
    try:
        return json.loads(request.body or b'{}') if request.content_type == 'application/json' else request.POST.dict()
    except ValueError:
        return {}


def _router(request, value):
    v = str(value or '')
    return _b(request).routers.filter(pk=v).first() if v.isdigit() else None


def _bad(message, status=400):
    return JsonResponse({'ok': False, 'message': message}, status=status)


def _via(router):
    from .voucher_history import channel
    return channel(router)


def _after(t):
    """What a finished test leaves behind for other pages."""
    if t.status == 'done' and t.kind == 'slowscan':
        ti.remember_scan(t.router, t.result or {})
    return t


def start(business, router, kind, data, user):
    """Create and run one lab test. API / Tunnel: now. TapTap Link: queued for the next check-in."""
    params = lab.clean(kind, data, router)
    nt.limit_check(business, router, kind)
    via = _via(router)
    t = NetTest.objects.create(business=business, router=router, kind=kind, target=str(params.get('target', ''))[:320], params=params,
                               via=via, created_by=user, status='waiting' if via == 'TapTap Link' else 'running')
    if via == 'TapTap Link':
        from . import agent as link
        from .linkops import send
        label = f'{t.get_kind_display()} {params.get("target", "")}'.strip()[:200]
        payload = {'test_id': t.pk, 'kind': kind, **params}
        try:
            cmd = send(router, 'nettest', payload, label=label, user=user, minutes=6)
        except ValueError as exc:
            cmd = _queue_late(router, t, kind, params, label, user)
            if cmd is None:
                t.status, t.result, t.finished_at = 'failed', {'error': f'{router.name} cannot take commands right now: {exc}'}, timezone.now()
                t.save(update_fields=['status', 'result', 'finished_at'])
        if cmd is not None:
            t.command_id = cmd.pk
            t.save(update_fields=['command_id', 'result'])
            try:
                link.mark_urgent(router)          # a person is waiting: ask the router to check in sooner
            except Exception:
                pass
        return t
    try:
        t.status, t.result = 'done', nt.run_api(t)
    except Exception as exc:  # noqa: BLE001 - routeros_api raises many types
        logger.info('lab test %s on router %s failed: %s', kind, router.pk, exc)
        t.status, t.result = 'failed', {'error': nt._ros_error(exc)}
    t.finished_at = timezone.now()
    t.save(update_fields=['status', 'result', 'finished_at'])
    return _after(t)


def _start_json(request, router, kind, data):
    try:
        t = start(_b(request), router, kind, data, request.user)
    except ValueError as exc:
        return None, _bad(str(exc))
    return t, None


@login_required
@require_GET
@_safe
def flow_test(request, pk):
    t = _b(request).net_tests.select_related('router').filter(pk=pk, kind__in=lab.LAB_KINDS).first()
    if not t:
        raise Http404
    nt.expire_waiting(t)
    return JsonResponse({'ok': True, 'test': as_json(_after(t))})


# ─────────────────────────────── Connection lab ───────────────────────────────

def _sessions(business, codes=(), macs=(), router=None):
    """Live sessions (from the last live-sync pass) of some codes and/or MACs, with the router they are on."""
    codes = {c.upper() for c in codes if c}
    macs = {m.upper() for m in macs if m}
    now = timezone.now()
    out = []
    for r in ([router] if router else business.routers.all()):
        users, age = ti._snap(r, now)
        if age is None:
            continue
        for code, sessions in users.items():
            for s in sessions:
                if code.upper() in codes or (s.get('mac') or '').upper() in macs:
                    out.append((r, code, s, age))
    return out


def _target(business, r, code, s, age, info_cache, can_switch):
    from .traffic_report import human_bps, human_bytes
    info = info_cache.get(r.pk)
    if info is None:
        info = info_cache[r.pk] = ls.lanes(r)
    now_ts = timezone.now().timestamp()
    quiet = s.get('calm')
    pin = ls.active_pin(r, mac=s.get('mac', ''), ip=s.get('ip', ''))
    multi = len(info['lanes']) > 1
    return {'router_id': r.pk, 'router': r.name, 'via': _via(r), 'code': code, 'ip': s.get('ip', ''), 'mac': s.get('mac', ''),
            'down_bps': s.get('down_bps') or 0, 'up_bps': s.get('up_bps') or 0,
            'down': human_bps(s.get('down_bps')), 'up': human_bps(s.get('up_bps')), 'uptime': s.get('uptime', ''),
            'data': human_bytes((s.get('session_down') or 0) + (s.get('session_up') or 0)),
            'quiet_s': int(now_ts - quiet) if quiet else 0, 'stale': age > ti.FRESH, 'age': int(age),
            'lines': {'multi': multi, 'why': info['why'], 'sticky': info['sticky'],
                      'lanes': [{'mark': l['mark'], 'label': l['label'], 'share': l['share']} for l in info['lanes']],
                      'where': ls.where(r, s.get('ip'), info) if multi else {},
                      'pin': {'mark': pin.mark, 'label': pin.label, 'since': pin.created_at.isoformat(),
                              'by': (pin.created_by.get_full_name() or pin.created_by.username) if pin.created_by else ''} if pin else None,
                      'can_switch': multi and can_switch}}


@login_required
@require_GET
@_safe
def flow_lab(request):
    """Everything the lab shows before any router is asked: who, where, live speed, limits, internet line."""
    from .voucher_history import display_state
    b = _b(request)
    g = request.GET
    v = d = None
    code, macs, router = '', [], None
    if g.get('voucher', '').isdigit():
        v = b.vouchers.select_related('router').filter(pk=int(g['voucher'])).first()
        if not v:
            return _bad('That voucher is gone.', 404)
        code = v.code
    elif g.get('device', '').isdigit():
        d = b.device_signatures.filter(pk=int(g['device'])).first()
        if not d:
            return _bad('That device is gone.', 404)
        macs = [str(m).upper() for m in (d.macs or []) if m] or ([d.last_mac.upper()] if d.last_mac else [])
    else:
        router = _router(request, g.get('router'))
        code, macs = g.get('code', '').strip()[:120], [ls.clean_mac(g.get('mac'))]
        if not router:
            return _bad('Choose a router first.')
        v = b.vouchers.filter(code__iexact=code).first() if code else None
    codes = [code] if code else []
    if v is not None:
        try:
            from .models import VoucherCodeAlias
            codes += list(VoucherCodeAlias.objects.filter(voucher=v).values_list('code', flat=True))
        except Exception:
            pass
    # a code typed in Network tools may be online on any router; an address belongs to the router it was seen on
    found = _sessions(b, codes, macs, router if g.get('ip') else None)
    can_switch = bool({'network.manage', 'vouchers.support'} & set(_perms(request))) and ls.pins_ready()
    info_cache = {}
    targets = [_target(b, r, c, s, age, info_cache, can_switch) for r, c, s, age in found]
    if not targets and router and nt.is_ip(g.get('ip', '')):
        # asked for by address (Traffic rows of devices without a hotspot session)
        targets = [_target(b, router, code, {'ip': g['ip'], 'mac': macs[0] if macs else ''}, 0, info_cache, can_switch)]
    targets.sort(key=lambda t: -(t['down_bps'] + t['up_bps']))
    if v is None and targets and targets[0]['code']:
        v = b.vouchers.filter(code__iexact=targets[0]['code']).first()
    for t in targets:
        t['limits'] = ti.device_limits(b, v, t['code'], t['ip'], b.routers.filter(pk=t['router_id']).first())
    out = {'ok': True, 'setup': '' if ls.pins_ready() else ls.MIGRATE_HINT, 'targets': targets, 'can_lift': bool(v) and can_switch, 'can_dhcp': _can_network(request), 'can_fix': bool(v) and 'vouchers.support' in _perms(request)}
    if v is not None:
        key, label = display_state(v)
        end, left = ti._ends(v, timezone.now())
        out['voucher'] = {'id': v.pk, 'code': v.code, 'plan': v.plan_name, 'state': key, 'state_label': label,
                          'left': ti.human_secs(left) if left else '', 'url': f'/vouchers/{v.pk}/', 'member': getattr(v, 'is_member', False)}
    if d is not None:
        out['device'] = {'id': d.pk, 'name': d.label or d.model or d.os or 'Device', 'macs': macs[:4]}
    if not targets:
        who = (v.code if v else '') or (out.get('device') or {}).get('name') or 'It'
        out['offline'] = (f'{who} is not online right now, so there is no connection to measure. '
                          + ('Its time is running — if the customer says it will not connect, use “Can\'t connect?”.' if v is not None and out['voucher']['left'] else
                             'The lab works while a device is signed in.'))
    return JsonResponse(out)


@login_required
@require_POST
@_safe
def flow_lab_run(request):
    """Start the device check and/or the speed check for one online device."""
    data = _body(request)
    router = _router(request, data.get('router'))
    if not router:
        return _bad('Choose a router first.')
    kinds = [k for k in (data.get('kinds') or ['client', 'cspeed']) if k in ('client', 'cspeed')]
    tests = []
    for kind in kinds:
        t, err = _start_json(request, router, kind, data)
        if err:
            return err if not tests else JsonResponse({'ok': True, 'tests': tests, 'message': json.loads(err.content)['message']})
        tests.append(as_json(t))
    return JsonResponse({'ok': True, 'tests': tests})


@login_required
@require_POST
@_safe
def flow_lab_verdict(request):
    """The headline: TapTap's own knowledge + the finished router checks."""
    b = _b(request)
    data = _body(request)
    router = _router(request, data.get('router'))
    if not router:
        return _bad('Choose a router first.')

    def res(pk, kind):
        t = b.net_tests.filter(pk=pk, router=router, kind=kind, status='done').first() if str(pk or '').isdigit() else None
        return t.result if t else None
    code, ip = str(data.get('code') or '')[:120], str(data.get('ip') or '')
    v = b.vouchers.filter(code__iexact=code).first() if code else None
    live = next((s for _r, _c, s, _a in _sessions(b, [code] if code else [], [], router) if s.get('ip') == ip), None)
    ctx = ti.device_limits(b, v, code, ip, router)
    ctx.update(online=bool(live), down_bps=(live or {}).get('down_bps', 0), up_bps=(live or {}).get('up_bps', 0),
               quiet_s=int(timezone.now().timestamp() - live['calm']) if live and live.get('calm') else 0,
               lane=ls.where(router, ip) or None)
    return JsonResponse({'ok': True, 'verdict': lab.verdict(ctx, res(data.get('client'), 'client'), res(data.get('cspeed'), 'cspeed'))})


@login_required
@require_GET
@_safe
def flow_live(request):
    """One device's speed right now (polled for the lab's moving line)."""
    router = _router(request, request.GET.get('router'))
    if not router:
        raise Http404
    ip, code = request.GET.get('ip', ''), request.GET.get('code', '')
    now = timezone.now()
    users, age = ti._snap(router, now)
    s = next((x for c, ss in users.items() for x in ss if x.get('ip') == ip and (not code or c.upper() == code.upper())), None)
    if s is None:
        s = next((x for ss in users.values() for x in ss if x.get('ip') == ip), None)
    return JsonResponse({'ok': True, 'online': bool(s) and age is not None and age <= ti.FRESH, 'age': int(age) if age is not None else None,
                         'down_bps': (s or {}).get('down_bps', 0), 'up_bps': (s or {}).get('up_bps', 0),
                         'quiet_s': int(now.timestamp() - s['calm']) if s and s.get('calm') else 0})


# ─────────────────────────────── Internet lines ───────────────────────────────

@login_required
@require_POST
@_safe
def flow_line(request):
    """look / switch / auto for one device.  switch: {'mark': 'TT-ether2'} or {'mark': 'next'}."""
    from .utils import log
    b = _b(request)
    data = _body(request)
    router = _router(request, data.get('router'))
    if not router:
        return _bad('Choose a router first.')
    action = data.get('action')
    if action == 'look':
        t, err = _start_json(request, router, 'lane', data)
        return err or JsonResponse({'ok': True, 'test': as_json(t)})
    info = ls.lanes(router)
    if len(info['lanes']) < 2:
        return _bad(info['why'])
    try:
        p = lab.device_params(data.get('ip'), data.get('mac'), data.get('code'))
    except ValueError as exc:
        return _bad(str(exc))
    key = ls.pin_key(p.get('mac', ''), p['ip'])
    who = p['code'] or p['ip']
    if action == 'auto':
        t, err = _start_json(request, router, 'laneoff', data)
        if err:
            return err
        ls.release_pin(router, key, request.user)
        log(b, 'Internet lines', f'{router.name}: {who} back to automatic line sharing', router=router)
        return JsonResponse({'ok': True, 'test': as_json(t)})
    if action != 'switch':
        return _bad('Unknown action.')
    if not ls.pins_ready():
        return _bad(ls.MIGRATE_HINT)
    now_on = (ls.where(router, p['ip'], info) or {})
    pin = ls.active_pin(router, mac=p.get('mac', ''), ip=p['ip'])
    current = (pin.mark if pin else '') or now_on.get('lane', '') or str(data.get('current') or '')
    want = str(data.get('mark') or 'next')
    lane = ls.next_lane(info, current) if want == 'next' else ls.lane_by_mark(info, want)
    if not lane:
        return _bad('That internet line is not part of this router\'s load sharing.')
    t, err = _start_json(request, router, 'lanepin', {**data, 'mark': lane['mark']})
    if err:
        return err
    if t.status != 'failed':
        from_label = next((l['label'] for l in info['lanes'] if l['mark'] == current), '')
        ls.record_pin(router, key, p.get('mac', ''), p['ip'], p['code'], lane, from_label, request.user)
        if t.status == 'done':                       # API: the answer is already here — bring the record in line with it
            ls.result(router, 'lane', t.params, {'counts': {}, 'total': 0,
                                                 'pin': '' if (t.result or {}).get('failed_step') else t.params['fields']['comment']})
        log(b, 'Internet lines', f'{router.name}: {who} switched to {lane["label"]}' + (f' (was {from_label})' if from_label else ''), router=router)
    return JsonResponse({'ok': True, 'test': as_json(t), 'lane': {'mark': lane['mark'], 'label': lane['label']}})


@login_required
@require_GET
@_safe
def flow_lines(request):
    """Traffic → Internet lines: who is on which line right now, per router, and every switch in force."""
    from .router_scope import scope
    from .traffic_report import human_bps
    b = _b(request)
    selected, routers = scope(request, b)
    now = timezone.now()
    out = []
    for r in ([selected] if selected else routers):
        info = ls.lanes(r)
        row = {'router_id': r.pk, 'router': r.name, 'via': _via(r), 'multi': len(info['lanes']) > 1, 'why': info['why'],
               'sticky': info['sticky'], 'as_of': info['as_of'], 'design_url': reverse('wan_designer', args=[r.pk])}
        if row['multi']:
            users, age = ti._snap(r, now)
            sessions = [{'code': c, **{k: s.get(k) for k in ('ip', 'mac', 'down_bps', 'up_bps')}} for c, ss in users.items() for s in ss] \
                if age is not None and age <= ti.FRESH else []
            sp = ls.spread(r, sessions, info)
            for line in sp['lines']:
                line['down'], line['up'] = human_bps(line['down_bps']), human_bps(line['up_bps'])
                for p in line['people']:
                    p['down'] = human_bps(p.get('down_bps'))
            pins = [{'id': p.pk, 'code': p.code, 'mac': p.mac, 'ip': p.ip, 'mark': p.mark, 'label': p.label, 'from': p.from_label,
                     'confirmed': p.confirmed, 'since': p.created_at.isoformat(),
                     'by': (p.created_by.get_full_name() or p.created_by.username) if p.created_by else ''}
                    for p in ls.pins_of(r)]
            seen_at = ls.seen(r).get('at')
            row.update(lines=sp['lines'], placed=sp['placed'], unknown=sp['unknown'], pins=pins, seen_at=seen_at,
                       online=len(sessions), stale=age is None or age > ti.FRESH)
        out.append(row)
    return JsonResponse({'ok': True, 'routers': out, 'can_switch': ls.pins_ready(), 'setup': '' if ls.pins_ready() else ls.MIGRATE_HINT})


# ─────────────────────────────── Idle / Slow (Traffic tabs) ───────────────────────────────

def _scope_id(request):
    from .router_scope import scope
    selected, _ = scope(request, _b(request))
    return selected.pk if selected else None


@login_required
@require_GET
@_safe
def flow_idle(request):
    try:
        minutes = max(1, min(1440, int(request.GET.get('min') or 10)))
    except ValueError:
        minutes = 10
    level = 'silent' if request.GET.get('level') == 'silent' else 'calm'
    return JsonResponse({'ok': True, **ti.idle(_b(request), _scope_id(request), minutes, level)})


@login_required
@require_GET
@_safe
def flow_slow(request):
    return JsonResponse({'ok': True, **ti.slow(_b(request), _scope_id(request))})


@login_required
@require_POST
@_safe
def flow_slow_scan(request):
    """Ask the routers in view which speed limits are dropping packets."""
    b = _b(request)
    rid = _scope_id(request)
    tests, notes = [], []
    for r in (b.routers.filter(pk=rid) if rid else b.routers.order_by('name')[:12]):
        gate = f'tt:slowscan:gate:{r.pk}'
        if not cache.add(gate, 1, 20):
            notes.append(f'{r.name}: scanned a moment ago')
            continue
        try:
            tests.append(as_json(start(b, r, 'slowscan', {}, request.user)))
        except ValueError as exc:
            notes.append(f'{r.name}: {exc}')
    return JsonResponse({'ok': True, 'tests': tests, 'notes': notes})


# ─────────────────────────────── DHCP lease ───────────────────────────────

@login_required
@_safe
def flow_dhcp(request):
    b = _b(request)
    if request.method == 'GET':
        want = _router(request, request.GET.get('router')) if request.GET.get('router') else None
        rid = want.pk if want else _scope_id(request)
        out = []
        for r in (b.routers.filter(pk=rid) if rid else b.routers.order_by('name')):
            st = dl.stored(r)
            out.append({'router_id': r.pk, 'router': r.name, 'via': _via(r), **st})
        return JsonResponse({'ok': True, 'routers': out, 'presets': [{'minutes': m, 'label': l} for m, l in dl.PRESETS]})
    if request.method != 'POST':
        return _bad('Method not allowed.', 405)
    from .utils import log
    data = _body(request)
    router = _router(request, data.get('router'))
    if not router:
        return _bad('Choose a router first.')
    if data.get('action') == 'read':
        t, err = _start_json(request, router, 'dhcp', {})
        return err or JsonResponse({'ok': True, 'test': as_json(t)})
    t, err = _start_json(request, router, 'dhcpset', data)
    if err:
        return err
    names = t.params.get('servers') or []
    log(b, 'DHCP', f'{router.name}: lease time set to {dl.human(t.params["minutes"])} on ' + (', '.join(names) if names else 'every network'), router=router)
    return JsonResponse({'ok': True, 'test': as_json(t)})
