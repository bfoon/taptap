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
    polls0 = None
    if via == 'TapTap Link':
        try:
            polls0 = int(router.agent.polls or 0)
        except Exception:
            polls0 = None
    t = NetTest.objects.create(business=business, router=router, kind=kind, target=str(params.get('target', ''))[:320], params=params,
                               via=via, created_by=user, status='waiting' if via == 'TapTap Link' else 'running',
                               result={'polls0': polls0} if polls0 is not None else {})
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
            if polls0 is not None:
                t.result = {**(t.result or {}), 'polls0': polls0}
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


def delivery(t):
    """Where a TapTap Link test is: queued (with what is ahead of it), handed to the router, or answered — and how the
    router has been checking in meanwhile. The lab shows this instead of a bare spinner."""
    if t.via != 'TapTap Link':
        return {'link': False}
    from .agent import VOUCHER_KINDS, ros_major
    from .models import AgentCommand
    out = {'link': True, 'stage': {'waiting': 'queued', 'done': 'answered', 'failed': 'failed'}.get(t.status, t.status),
           'age': int((timezone.now() - t.created_at).total_seconds()), 'express': False}
    try:
        agent = t.router.agent
        heard = agent.last_heard
        out['heard_s'] = int((timezone.now() - heard).total_seconds()) if heard else None
        from .link_rhythm import _p, gaps
        out['usual_s'] = int(_p(gaps(agent), .5) or agent.poll_seconds or 10)
        p0 = (t.result or {}).get('polls0')
        out['checkins'] = max(0, int(agent.polls or 0) - int(p0)) if p0 is not None else None
        out['express'] = ros_major(t.router) >= 7
    except Exception:
        pass
    cmd = AgentCommand.objects.filter(pk=t.command_id).first() if t.command_id else None
    if cmd is not None:
        out['attempts'] = cmd.attempts
        if cmd.status == 'queued':
            ahead = AgentCommand.objects.filter(router=t.router, status='queued', created_at__lt=cmd.created_at)
            out['ahead'] = ahead.count()
            out['vouchers_ahead'] = ahead.filter(kind__in=VOUCHER_KINDS).count()
        elif cmd.status in ('sent', 'done') and t.status == 'waiting':
            out['stage'] = 'running'
            out['sent_s'] = int((timezone.now() - cmd.sent_at).total_seconds()) if cmd.sent_at else None
    return out


def test_json(t):
    return {**as_json(t), 'delivery': delivery(t)}


@login_required
@require_GET
@_safe
def flow_test(request, pk):
    t = _b(request).net_tests.select_related('router').filter(pk=pk, kind__in=lab.LAB_KINDS).first()
    if not t:
        raise Http404
    nt.expire_waiting(t)
    return JsonResponse({'ok': True, 'test': test_json(_after(t))})


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
    kinds = [k for k in (data.get('kinds') or ['labq', 'labd']) if k in ('labq', 'labd', 'client', 'cspeed')]
    tests = []
    for kind in kinds:
        t, err = _start_json(request, router, kind, data)
        if err:
            return err if not tests else JsonResponse({'ok': True, 'tests': tests, 'message': json.loads(err.content)['message']})
        tests.append(test_json(t))
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

    def res(pk, kinds):
        t = b.net_tests.filter(pk=pk, router=router, kind__in=kinds, status='done').first() if str(pk or '').isdigit() else None
        return t.result if t else None
    code, ip = str(data.get('code') or '')[:120], str(data.get('ip') or '')
    v = b.vouchers.filter(code__iexact=code).first() if code else None
    found = [s for _r, _c, s, _a in _sessions(b, [code] if code else [], [], router) if s.get('ip') == ip]
    if not found:
        users, _age = ti._snap(router, timezone.now())
        found = [x for ss in users.values() for x in ss if x.get('ip') == ip]
    live = found[0] if found else None
    mac = str(data.get('mac') or (live or {}).get('mac') or '')
    ctx = ti.device_limits(b, v, code, ip, router)
    ctx.update(online=bool(live), down_bps=(live or {}).get('down_bps', 0), up_bps=(live or {}).get('up_bps', 0),
               quiet_s=int(timezone.now().timestamp() - live['calm']) if live and live.get('calm') else 0,
               lane=ls.where(router, ip) or None, snap=_snap_facts(router, ip, mac), heard_s=_heard_s(router), via=_via(router))
    q, d = res(data.get('labq'), ('labq',)), res(data.get('labd'), ('labd',))
    old_c, old_s = res(data.get('client'), ('client',)), res(data.get('cspeed'), ('cspeed',))
    dev = {**(old_c or {}), **{k: (q or {}).get(k) for k in ('ping', 'arp', 'active') if q}, **{k: (d or {}).get(k) for k in ('port', 'lease', 'host', 'wifi') if d}}
    spd = {**(old_s or {}), **{k: (q or {}).get(k) for k in ('queues', 'lane') if q}, **{k: (d or {}).get(k) for k in ('cpu', 'drops', 'internet') if d}}
    dev = {k: v2 for k, v2 in dev.items() if v2 is not None}
    spd = {k: v2 for k, v2 in spd.items() if v2 is not None}
    done = bool(q and d) or bool(old_c and old_s)
    return JsonResponse({'ok': True, 'verdict': lab.verdict(ctx, dev, spd, done=done)})


def _heard_s(router):
    """Seconds since a TapTap Link router last reached TapTap (proof its internet works); None for other channels."""
    try:
        if _via(router) != 'TapTap Link':
            return None
        heard = router.agent.last_heard
        return int((timezone.now() - heard).total_seconds()) if heard else None
    except Exception:
        return None


def _snap_facts(router, ip, mac):
    """What the last router sync says about one device: port / interface, Wi-Fi signal — and how old that is."""
    from django.db.models import Q
    from .linkops import snapshot_rows
    out = {}
    mac = ls.clean_mac(mac)
    try:
        q = (Q(ip_address=ip) | Q(mac_address__iexact=mac)) if mac else Q(ip_address=ip)
        d = router.devices.filter(q).order_by('-last_seen_at').first()
        if d:
            out.update(iface=d.interface_name or '', type=d.connection_type or '', age=int((timezone.now() - d.last_seen_at).total_seconds()))
            mac = mac or ls.clean_mac(d.mac_address)
    except Exception:
        pass
    if mac:
        for _cli, path, sig, ccq in lab.WIFI_TABLES:
            try:
                rows, _l, at = snapshot_rows(router, path)
            except Exception:
                rows, at = None, None
            row = next((r for r in rows or [] if ls.clean_mac(r.get('mac-address')) == mac), None)
            if row:
                out['wifi'] = lab._wifi(row.get(sig), row.get('tx-rate'), row.get('rx-rate'), row.get(ccq) if ccq else '', row.get('interface'))
                if at:
                    out['age'] = int((timezone.now() - at).total_seconds())
                out.setdefault('iface', out['wifi'].get('interface', ''))
                break
    return out


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
        return err or JsonResponse({'ok': True, 'test': test_json(t)})
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
        return err or JsonResponse({'ok': True, 'test': test_json(t)})
    t, err = _start_json(request, router, 'dhcpset', data)
    if err:
        return err
    names = t.params.get('servers') or []
    log(b, 'DHCP', f'{router.name}: lease time set to {dl.human(t.params["minutes"])} on ' + (', '.join(names) if names else 'every network'), router=router)
    return JsonResponse({'ok': True, 'test': as_json(t)})


# ─────────────────────────────── Speed per device (core/device_lanes.py) ───────────────────────────────

def _lane_json(lane):
    return {'id': lane.pk, 'scope': lane.scope, 'voucher': lane.voucher.code if lane.voucher_id else '', 'voucher_id': lane.voucher_id,
            'until': lane.until.isoformat() if lane.until else None, 'since': lane.created_at.isoformat(), 'reason': lane.reason,
            'by': (lane.created_by.get_full_name() or lane.created_by.username) if lane.created_by else ''}


def _plans_math(business):
    from .device_lanes import math
    out = []
    for p in business.plans.filter(active=True).order_by('name'):
        if (p.max_devices or 1) > 1:
            out.append({'name': p.name, 'kind': 'Plan', 'devices': p.max_devices, 'rate': p.speed_limit, **math(p.speed_limit, p.max_devices)})
    try:
        for p in business.member_plans.filter(active=True).order_by('name'):
            if (p.max_devices or 1) > 1:
                out.append({'name': p.name, 'kind': 'Member plan', 'devices': p.max_devices, 'rate': p.speed_limit, **math(p.speed_limit, p.max_devices)})
    except Exception:
        pass
    return out


def _voucher_view(request, v):
    from . import device_lanes as dlan
    from .traffic_report import human_bps
    b = _b(request)
    rate, up, down, profile = dlan.speed_of(v)
    shared, per = dlan.active(b)
    lane = per.get(v.pk)
    codes = [v.code]
    try:
        from .models import VoucherCodeAlias
        codes += list(VoucherCodeAlias.objects.filter(voucher=v).values_list('code', flat=True))
    except Exception:
        pass
    found = _sessions(b, codes, [])
    labels = ti._labels(b, [s.get('mac') for _r, _c, s, _a in found])
    devices, routers = [], {}
    for r, code, s, age in found:
        checked = (dlan.last(r) or {})
        state = next((d for x in (checked.get('result') or {}).get('vouchers') or [] if x['code'].upper() == v.code.upper()
                      for d in x['devices'] if d['ip'] == s.get('ip')), None)
        fresh = bool(checked.get('at')) and (timezone.now() - timezone.datetime.fromisoformat(checked['at'])).total_seconds() < 1800
        d = labels.get((s.get('mac') or '').upper())
        devices.append({'router_id': r.pk, 'router': r.name, 'ip': s.get('ip', ''), 'mac': s.get('mac', ''), 'label': d[1] if d else '',
                        'down_bps': s.get('down_bps') or 0, 'up_bps': s.get('up_bps') or 0,
                        'down': human_bps(s.get('down_bps')), 'up': human_bps(s.get('up_bps')),
                        'pct': min(100, round((s.get('down_bps') or 0) * 100 / down)) if down else None,
                        'state': (state or {}).get('state') if fresh else None, 'text': (state or {}).get('text', '') if fresh else '',
                        'checked_at': checked.get('at') if fresh and state else None, 'stale': age > ti.FRESH})
        routers[r.pk] = r.name
    devices.sort(key=lambda x: x['ip'])
    guaranteed = bool(lane) or (shared is not None and (v.max_devices or 1) > 1)
    perms = set(_perms(request))
    return JsonResponse({'ok': True, 'setup': '' if dlan.ready() else ls.MIGRATE_HINT,
                         'voucher': {'id': v.pk, 'code': v.code, 'plan': v.plan_name, 'devices_allowed': v.max_devices or 1,
                                     'rate': rate, 'down_bps': down, 'up_bps': up, 'profile': profile, 'member': getattr(v, 'is_member', False)},
                         'math': dlan.math(rate, v.max_devices or 1), 'devices': devices, 'routers': routers,
                         'lane': _lane_json(lane) if lane else None, 'shared_on': shared is not None, 'guaranteed': guaranteed,
                         'can_lane': bool({'vouchers.support', 'network.manage'} & perms) and dlan.ready() and bool(down),
                         'can_fix': 'network.manage' in perms})


@_safe
@login_required
@require_GET
def flow_speed(request):
    """Traffic → Speed per device (router-wide), or ?voucher=<id> for the voucher / member pages."""
    from . import device_lanes as dlan
    b = _b(request)
    if request.GET.get('voucher', '').isdigit():
        v = b.vouchers.select_related('router').filter(pk=int(request.GET['voucher'])).first()
        if not v:
            raise Http404
        return _voucher_view(request, v)
    rid = _scope_id(request)
    shared, per = dlan.active(b)
    out = []
    for r in (b.routers.filter(pk=rid) if rid else b.routers.order_by('name')):
        rows, age = dlan.online(r, business=b)
        last = dlan.last(r) or {}
        out.append({'router_id': r.pk, 'router': r.name, 'via': _via(r), 'shared_online': len(rows),
                    'vouchers_online': len({c for c, _s in rows}), 'stale': age is None or age > ti.FRESH,
                    'last': last.get('result'), 'last_at': last.get('at')})
    lanes = [_lane_json(l) for l in per.values()]
    lanes.sort(key=lambda x: x['since'], reverse=True)
    return JsonResponse({'ok': True, 'setup': '' if dlan.ready() else ls.MIGRATE_HINT, 'routers': out,
                         'shared_on': shared is not None, 'shared': _lane_json(shared) if shared else None, 'lanes': lanes[:200],
                         'plans': _plans_math(b), 'can_fix': _can_network(request)})


@_safe
@login_required
@require_POST
def flow_speed_check(request):
    """Ask the router(s) which queue really carries each device: of one voucher, or of every shared voucher online."""
    from . import device_lanes as dlan
    b = _b(request)
    data = _body(request)
    tests, notes = [], []
    if str(data.get('voucher') or '').isdigit():
        v = b.vouchers.filter(pk=int(data['voucher'])).first()
        if not v:
            return _bad('That voucher is gone.', 404)
        by_router = {}
        for r, _c, s, _a in _sessions(b, [v.code], []):
            by_router.setdefault(r.pk, (r, []))[1].append(s.get('ip'))
        if not by_router:
            return _bad(f'{v.code} is not online right now — the check looks at devices that are signed in.')
        for r, ips in by_router.values():
            try:
                tests.append(as_json(start(b, r, 'lanes', {'ips': ips, 'codes': [v.code]}, request.user)))
            except ValueError as exc:
                notes.append(f'{r.name}: {exc}')
    else:
        if not _can_network(request):
            return _bad('Checking every router needs network rights.', 403)
        rid = _scope_id(request)
        for r in (b.routers.filter(pk=rid) if rid else b.routers.order_by('name')[:12]):
            rows, age = dlan.online(r, business=b)
            if not rows:
                notes.append(f'{r.name}: no shared voucher online right now' + (' (live sync is quiet)' if age is None or age > ti.FRESH else ''))
                continue
            try:
                tests.append(as_json(start(b, r, 'lanes', {'ips': [s.get('ip') for _c, s in rows][:dlan.MAX_IPS]}, request.user)))
            except ValueError as exc:
                notes.append(f'{r.name}: {exc}')
    if not tests and notes:
        return _bad(' · '.join(notes))
    return JsonResponse({'ok': True, 'tests': tests, 'notes': notes})


@_safe
@login_required
@require_POST
def flow_speed_fix(request):
    """Give every device its own lane: fix the router (profiles, parent queue, FastTrack) and/or add TapTap lanes."""
    from . import device_lanes as dlan
    from .utils import log
    b = _b(request)
    data = _body(request)
    if not dlan.ready():
        return _bad(ls.MIGRATE_HINT)
    router = _router(request, data.get('router'))
    if not router:
        return _bad('Choose a router first.')
    last = (dlan.last(router) or {}).get('result')
    if not last:
        return _bad('Check the router first, so TapTap knows what to fix.')
    fx = last.get('fix') or {}
    net = _can_network(request)
    profiles, guard = bool(data.get('profiles', True)) and net, bool(data.get('guard', True)) and net
    params = {'ips': [d['ip'] for x in last.get('vouchers') or [] for d in x['devices']][:dlan.MAX_IPS],
              'rates': (fx.get('rates') or []) if profiles else [], 'unparent': (fx.get('unparent') or []) if profiles else [],
              'guard': bool(fx.get('guard')) and guard, 'nets': fx.get('nets') or []}
    lanes_made = 0
    if data.get('lanes', True) and fx.get('lanes'):
        vs = list(b.vouchers.filter(pk__in=fx['lanes']))
        lanes_made = dlan.add_lanes(b, vs, request.user, reason='Speed check: a device did not have its own lane')
        if lanes_made:
            dlan.apply_now(b)
    test = None
    if params['rates'] or params['unparent'] or params['guard']:
        t, err = _start_json(request, router, 'lanefix', params)
        if err:
            return err
        test = as_json(t)
    if not test and not lanes_made:
        return JsonResponse({'ok': True, 'test': None, 'lanes_made': 0, 'message': 'Nothing to fix — every device checked already has its own lane.'})
    log(b, 'Speed per device', f'{router.name}: fix — {len(params["rates"])} profile speed(s), {len(params["unparent"])} parent queue(s) removed'
        + (', FastTrack guard' if params['guard'] else '') + f', {lanes_made} voucher(s) given their own lanes', router=router)
    return JsonResponse({'ok': True, 'test': test, 'lanes_made': lanes_made})


@_safe
@login_required
@require_POST
def flow_speed_lane(request):
    """Lanes by hand: one voucher on / off, or the always-on guarantee for every shared voucher."""
    from . import device_lanes as dlan
    from .utils import log
    b = _b(request)
    data = _body(request)
    if not dlan.ready():
        return _bad(ls.MIGRATE_HINT)
    action = data.get('action')
    if action in ('shared_on', 'shared_off'):
        if not _can_network(request):
            return _bad('Turning this on for the whole business needs network rights.', 403)
        dlan.set_shared(b, action == 'shared_on', request.user)
        dlan.apply_now(b)
        log(b, 'Speed per device', 'Every device of a shared voucher gets its own lane: ' + ('ON' if action == 'shared_on' else 'off'))
        return JsonResponse({'ok': True, 'shared_on': action == 'shared_on'})
    if action == 'stop':
        n = dlan.stop(b, int(data.get('lane') or 0), request.user)
        dlan.apply_now(b)
        return JsonResponse({'ok': bool(n)})
    if action == 'add':
        v = b.vouchers.filter(pk=int(data.get('voucher') or 0)).first() if str(data.get('voucher') or '').isdigit() else None
        if not v:
            return _bad('That voucher is gone.', 404)
        if not dlan.speed_of(v)[2]:
            return _bad(f'{v.code} has no speed limit, so there is no speed to give each device.')
        dlan.add_lanes(b, [v], request.user, reason='Given by hand on the voucher page')
        dlan.apply_now(b)
        log(b, 'Speed per device', f'{v.code}: each device gets its own lane at the plan speed')
        return JsonResponse({'ok': True})
    return _bad('Unknown action.')
