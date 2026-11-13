"""MikroTik Config Studio — build a configuration from tiles, try it on a virtual router, back up, deploy, keep or undo.

    /routers/<id>/studio/

How a board reaches a router
----------------------------
1. The browser sends tile types and the values typed into their blanks — never RouterOS commands.
2. ``config_studio_tiles`` checks every value and turns the board into operations (data).
3. ``config_studio_sim`` applies them to a copy of the router's configuration and tests the result with packets.
4. Deploy needs a fresh backup. An undo timer goes onto the router FIRST (/system scheduler TapTap-Studio-undo):
   if nobody presses "Keep" in time — for example because the change cut the router off — the router puts the
   old values back by itself.
5. The operations are applied in order: over the RouterOS API (direct routers and Link routers with a healthy
   tunnel) or as TapTap Link commands, one piece at a time, each built by the server from the stored plan.
   A failure stops there and undoes what was already applied.

Self-installing like the other add-ons (see CoreConfig.ready): URLs, permissions and the Link command kind.
"""
from __future__ import annotations

import functools
import json
import logging
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import path, reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import config_studio_sim as sim
from . import config_studio_tiles as tiles
from .models import Router, RouterBackup, RouterConfigSnapshot
from .models_config_studio import RouterStudioBoard, RouterStudioDeploy

logger = logging.getLogger('taptap.studio')

UNDO_SCHEDULER = 'TapTap-Studio-undo'
LINK_KIND = 'config_studio'
BACKUP_FRESH = 60                      # minutes a backup counts as "just made"
UNDO_CHOICES = (3, 5, 10, 15, 0)
LINK_SCRIPT_MAX = 7000
ACTIVE = ('sending', 'applied')
# Rows the undo can re-create after a tile removed them: resource → (key fields, other fields kept).
RECREATE = {'/interface/bridge/port': (('interface',), ('bridge', 'pvid', 'comment'))}

KITS = [
    {'key': 'shop', 'label': 'Shop hotspot', 'icon': 'bi-shop', 'hint': 'For a fresh router: internet in, a hotspot for customers, the router protected.',
     'tiles': [{'type': 'wan_dhcp', 'params': {'port': 'ether1'}},
               {'type': 'bridge', 'params': {'ports': 'ether2, ether3, ether4', 'name': 'bridge-hotspot'}},
               {'type': 'hotspot', 'params': {'interface': 'bridge-hotspot', 'network': '10.5.50.1/24'}},
               {'type': 'fw_basic', 'params': {'wan': 'ether1'}}, {'type': 'services', 'params': {'off': 'telnet, ftp'}}]},
    {'key': 'office', 'label': 'Office + guests', 'icon': 'bi-building', 'hint': 'For a fresh router: your own network, and a slower one for visitors that cannot see it.',
     'tiles': [{'type': 'wan_dhcp', 'params': {'port': 'ether1'}},
               {'type': 'bridge', 'params': {'ports': 'ether2, ether3, ether4', 'name': 'bridge-lan'}},
               {'type': 'lan', 'params': {'interface': 'bridge-lan', 'network': '192.168.88.1/24'}},
               {'type': 'guest', 'params': {'interface': 'ether5', 'network': '10.10.10.1/24', 'down': '10M'}},
               {'type': 'dns', 'params': {'servers': '1.1.1.1, 8.8.8.8'}}, {'type': 'fw_basic', 'params': {'wan': 'ether1'}}]},
    {'key': 'harden', 'label': 'Lock it down', 'icon': 'bi-shield-lock', 'hint': 'For a router that already works: close what should not be open.',
     'tiles': [{'type': 'fw_basic', 'params': {'wan': 'ether1'}}, {'type': 'services', 'params': {'off': 'telnet, ftp, www'}},
               {'type': 'clock', 'params': {'zone': 'Africa/Banjul'}}]},
]


class StudioError(Exception):
    pass


# ───────────────────────────── small helpers ─────────────────────────────
def _business(request):
    return getattr(request, 'tt_business', None) or request.user.business


def _router(request, pk):
    return get_object_or_404(Router, pk=pk, business=_business(request))


def _body(request):
    try:
        data = json.loads(request.body or b'{}')
    except (ValueError, UnicodeDecodeError):
        data = {}
    return data if isinstance(data, dict) else {}


def _fail(message, status=400, **extra):
    return JsonResponse({'ok': False, 'message': str(message), **extra}, status=status)


def _link(router):
    """True when changes must travel as TapTap Link commands right now."""
    from .linkops import uses_link
    return uses_link(router)


def transport_name(router):
    if _link(router):
        return 'TapTap Link'
    return 'TapTap Tunnel' if getattr(router, 'connection_mode', 'api') == 'agent' else 'RouterOS API'


def trusted_addresses():
    try:
        from .self_trust import learned
        return [a for a in learned() if a]
    except Exception:
        return []


def snapshot_of(router):
    return RouterConfigSnapshot.objects.filter(router=router).first()


def ros_major(router, snap=None):
    from .routeros_analysis import parse_version
    text = ''
    rows = (((snap.sections if snap else {}) or {}).get('System resources') or {}).get('rows') or []
    if rows:
        text = str(rows[0].get('version', ''))
    if not text:
        try:
            text = router.agent.ros_version or ''
        except Exception:
            text = ''
    return (parse_version(text) or (7,))[0] or 7


def context_for(router, snap=None):
    return {'ros_major': ros_major(router, snap or snapshot_of(router)), 'trusted': trusted_addresses()}


def base_state(router, base='router'):
    """(state, info). ``state`` is None when the real router has not been read yet."""
    if base == 'lab-default':
        return sim.lab_state('default'), {'base': base, 'label': 'Practice router (factory default)', 'lab': True}
    if base == 'lab-empty':
        return sim.lab_state('empty'), {'base': base, 'label': 'Practice router (empty)', 'lab': True}
    snap = snapshot_of(router)
    if not snap or not snap.sections:
        return None, {'base': 'router', 'label': router.name, 'lab': False, 'captured_at': None}
    return sim.state_from_sections(snap.sections), {'base': 'router', 'label': router.name, 'lab': False,
                                                    'captured_at': snap.captured_at.isoformat() if snap.captured_at else None}


def board_of(router):
    board, _ = RouterStudioBoard.objects.get_or_create(router=router)
    return board


def save_board(router, user, data):
    board = board_of(router)
    if 'tiles' in data:
        board.tiles = tiles.clean_board(data.get('tiles'))
    if data.get('base') in dict(RouterStudioBoard.BASES):
        board.base = data['base']
    board.updated_by = user
    board.save()
    return board


def labels():
    from .mikrotik import MikroTikService
    return {p: label for label, p in MikroTikService.CONFIG_CATALOG}


def picture(vr):
    """What the virtual router looks like: ports, groups, internet lines — for the faceplate drawing."""
    wans = vr.wan_interfaces()
    addrs = {}
    for a, iface, row in vr.addresses():
        addrs.setdefault(iface, []).append({'address': str(a), 'mark': row.get('_sim', '')})
    hotspots = {h.get('interface') for h in vr.live('/ip/hotspot')}
    dhcp = {s.get('interface') for s in vr.live('/ip/dhcp-server')}
    guests = {c['row'].get('interface') for c in vr.changes if c['resource'] == '/ip/address'
              and str(c['row'].get('comment', '')).startswith(tiles.TAG + 'guest')}
    ports, groups = [], []
    for r in vr.rows('/interface'):
        name = r.get('name')
        if not name or r.get('_sim') == 'removed':
            continue
        kind = str(r.get('type') or 'ether')
        item = {'name': name, 'type': kind, 'running': sim.yes(r.get('running', 'false')), 'off': sim.yes(r.get('disabled', 'false')),
                'bridge': vr.bridge_of(name) or '', 'addresses': addrs.get(name, []), 'mark': r.get('_sim', ''),
                'role': 'wan' if name in wans else 'hotspot' if name in hotspots else 'guest' if name in guests
                        else 'lan' if name in dhcp or name in addrs else ''}
        (groups if kind in ('bridge', 'vlan', 'pppoe-client', 'pppoe-out', 'bond', 'wg', 'wireguard') else ports).append(item)
    for g in groups:
        g['members'] = [p['name'] for p in ports if p['bridge'] == g['name']]
        for p in ports:
            if p['bridge'] == g['name'] and not p['role']:
                p['role'] = g['role']
    sources = [{'key': f'net:{iface}', 'label': f'A device on {iface} ({a.network})', 'ip': sim.sample_ip(a), 'iface': iface}
               for a, iface, _ in vr.addresses() if iface not in wans and a.network.prefixlen <= 30]
    sources += [{'key': f'wan:{w}', 'label': f'Somebody on the internet (arrives on {w})', 'ip': sim.INTERNET_IP, 'iface': w} for w in wans]
    return {'ports': ports[:48], 'groups': groups[:24], 'wans': wans, 'sources': sources,
            'identity': str((vr.rows('/system/identity') or [{}])[0].get('name', ''))}


def examine_board(router, board_tiles, base):
    """Compile + simulate. Returns the dict the browser draws (JSON-safe) and the pieces deploy needs."""
    state, info = base_state(router, base)
    ctx = context_for(router)
    ops, problems = tiles.compile_board(board_tiles, ctx)
    out = {'ok': True, 'info': info, 'problems': problems, 'code': tiles.readable(ops), 'ops': len(ops), 'needs_read': state is None}
    if state is None:
        out.update(findings=[{'level': 'info', 'text': f'TapTap has not read {router.name} yet. Press “Read the router”, or practise on a practice router.',
                              'tile': '', 'fix': ''}], tests=[], changes=[], counts={}, safe=False, picture=None)
        return out, ops, None
    result = sim.examine(state, ops, board_tiles, router=None if info['lab'] else router, trusted=ctx['trusted'])
    vr = result.pop('after')
    out.update(findings=result['findings'], tests=result['tests'], changes=_redact(result['changes']), counts=result['counts'],
               safe=result['safe'] and not problems, picture=picture(vr))
    return out, ops, vr


def _redact(value):
    from .mikrotik import redact
    return redact(value)


# ───────────────────────────── deploy engine ─────────────────────────────
def kw(d):
    return {str(k).replace('-', '_'): v for k, v in (d or {}).items()}


def _rid(row):
    return row.get('id') or row.get('.id')


class ApiReader:
    def __init__(self, svc):
        self.svc = svc

    def rows(self, resource, find):
        res = self.svc.resource(resource)
        return [dict(r) for r in (res.get(**kw(find)) if find else res.get())]


class SnapshotReader:
    """For TapTap Link: "what is there now" comes from the last synced configuration."""

    def __init__(self, state):
        self.vr = sim.VirtualRouter(state or {})

    def rows(self, resource, find):
        return [dict(r) for r in (self.vr.match(resource, find) if find else self.vr.rows(resource))]


def before_state(reader, ops):
    """For every operation: did its row exist, and which of the values it will write were there before."""
    out = []
    for o in ops:
        rows = reader.rows(o['resource'], {} if o['action'] == 'singleton_set' else o['find'])
        row = sim.hy(rows[0]) if rows else {}
        prior = {k: row[k] for k in o['values'] if k in row and not any(s in k for s in tiles.SECRET_KEYS)}
        if rows and 'comment' in o['values'] and 'comment' not in row:
            prior['comment'] = ''
        keep = RECREATE.get(o['resource'])
        gone = [{k: sim.hy(r).get(k) for k in keep[0] + keep[1] if sim.hy(r).get(k) not in (None, '')} for r in rows] \
            if o['action'] == 'remove_matching' and keep else []
        out.append({'exists': bool(rows), 'prior': prior, 'removed': gone, 'count': len(rows) if o['action'] == 'remove_matching' else 0})
    return out


def rollback_plan(ops, before):
    """(operations that put things back — in reverse order, what cannot be put back automatically)."""
    undo, manual = [], []
    for o, b in reversed(list(zip(ops, before))):
        res, label = o['resource'], o['label']
        if o['action'] == 'singleton_set':
            if b['prior']:
                undo.append(tiles.op(res, 'singleton_set', {}, b['prior'], f'Put back: {label}'))
        elif o['action'] in ('ensure', 'set'):
            if b['exists']:
                if b['prior']:
                    undo.append(tiles.op(res, 'set', o['find'], b['prior'], f'Put back: {label}', soft=True))
            elif o['action'] == 'ensure':
                find = dict(o['find'])
                if o['values'].get('comment'):
                    find['comment'] = o['values']['comment']
                undo.append(tiles.op(res, 'remove_matching', find, {}, f'Remove: {label}'))
        elif o['action'] == 'remove_matching':
            keep = RECREATE.get(res)
            if keep:
                for row in b['removed']:
                    undo.append(tiles.op(res, 'ensure', {k: row[k] for k in keep[0] if k in row},
                                         {k: row[k] for k in keep[1] if k in row}, f'Put back: {label}'))
            elif b['count']:
                manual.append(f'{label}: {b["count"]} removed row(s) are not re-created automatically (restore them from the backup).')
    return undo, manual


def run_op(svc, o):
    """One operation over the RouterOS API."""
    res = svc.resource(o['resource'])
    find, values, action = kw(o['find']), kw(o['values']), o['action']
    if action == 'singleton_set':
        rows = res.get()
        rid = _rid(rows[0]) if rows else None
        res.set(id=rid, **values) if rid else res.set(**values)
        return
    rows = res.get(**find) if find else res.get()
    if action == 'remove_matching':
        for row in rows:
            if _rid(row):
                res.remove(id=_rid(row))
        return
    if action == 'set':
        if not rows:
            if o.get('soft'):
                return
            raise StudioError(f'{o["label"]}: the router has no such row.')
        for row in rows:
            res.set(id=_rid(row), **values)
        return
    if rows:                                           # ensure
        if values:
            res.set(id=_rid(rows[0]), **values)
        return
    extra = {}
    if o.get('place') == 'top':
        chain = o['values'].get('chain') or o['find'].get('chain')
        first = next((r for r in res.get(chain=chain) if not sim.yes(r.get('dynamic', 'false'))), None) if chain else None
        if first and _rid(first):
            extra['place_before'] = _rid(first)
    res.add(**find, **values, **extra)


def undo_script(undo_ops):
    """The undo as RouterOS text: every line on its own, so one that no longer applies never stops the rest."""
    return '; '.join(f':do {{ {tiles.safe_line(o)} }} on-error={{}}' for o in undo_ops)


def arm_script(deploy):
    body = undo_script(deploy.rollback_plan)
    event = (f'{body}; /system scheduler remove [find where name="{UNDO_SCHEDULER}"]; '
             f':log warning "TapTap Studio: change #{deploy.pk} was undone automatically (nobody pressed Keep)"')
    return (f'/system scheduler remove [find where name="{UNDO_SCHEDULER}"]; '
            f'/system scheduler add name={UNDO_SCHEDULER} interval={int(deploy.undo_minutes)}m policy=read,write,test '
            f'on-event={tiles.q(event)} comment={tiles.q(f"TapTap Studio #{deploy.pk}: automatic undo unless TapTap confirms")}')


KEEP_SCRIPT = (f':if ([:len [/system scheduler find where name="{UNDO_SCHEDULER}"]] = 0) do={{ :error "TapTap Studio: already undone" }}; '
               f'/system scheduler remove [find where name="{UNDO_SCHEDULER}"]; :log info "TapTap Studio: change confirmed"')


def _timer_rows(svc):
    return list(svc.resource('/system/scheduler').get(name=UNDO_SCHEDULER))


def _arm(svc, deploy):
    sched = svc.resource('/system/scheduler')
    for row in _timer_rows(svc):
        sched.remove(id=_rid(row))
    body = undo_script(deploy.rollback_plan)
    event = (f'{body}; /system scheduler remove [find where name="{UNDO_SCHEDULER}"]; '
             f':log warning "TapTap Studio: change #{deploy.pk} was undone automatically (nobody pressed Keep)"')
    sched.add(name=UNDO_SCHEDULER, interval=f'{int(deploy.undo_minutes)}m', policy='read,write,test', on_event=event,
              comment=f'TapTap Studio #{deploy.pk}: automatic undo unless TapTap confirms')


def _disarm(svc):
    """True when the timer was still there (the change is still on the router)."""
    rows = _timer_rows(svc)
    for row in rows:
        svc.resource('/system/scheduler').remove(id=_rid(row))
    return bool(rows)


def _finish(deploy, status, **fields):
    deploy.status = status
    if status in ('kept', 'failed', 'undone'):
        deploy.finished_at = timezone.now()
    for k, v in fields.items():
        setattr(deploy, k, v)
    deploy.save()
    try:
        from .utils import log
        words = {'applied': 'put on the router (waiting for Keep)', 'kept': 'kept', 'failed': 'failed', 'undone': 'undone'}
        if status in words:
            log(deploy.business, 'Config Studio', f'{deploy.router.name}: change #{deploy.pk} {words[status]} — {deploy.summary}',
                status='Failed' if status == 'failed' else 'Success', router=deploy.router)
    except Exception:
        logger.exception('Config Studio: activity log failed')
    if status in ('applied', 'kept', 'undone'):
        _reread(deploy.router, deploy.actor)
    return deploy


def _reread(router, user=None):
    try:
        from .linkops import refresh
        refresh(router, user)
    except Exception:
        pass


def deploy_direct(deploy, connect=None):
    """Apply over the RouterOS API. ``connect`` is replaced in tests."""
    from .mikrotik import MikroTikService
    svc = (connect or (lambda r: MikroTikService(r).connect()))(deploy.router)
    done = 0
    try:
        before = before_state(ApiReader(svc), deploy.plan)
        deploy.rollback_plan, deploy.manual_undo = rollback_plan(deploy.plan, before)
        deploy.status, deploy.transport = 'sending', transport_name(deploy.router)
        deploy.save()
        armed = bool(deploy.undo_minutes and deploy.rollback_plan)
        if armed:
            _arm(svc, deploy)
        try:
            for o in deploy.plan:
                run_op(svc, o)
                done += 1
        except Exception as exc:
            partial, _ = rollback_plan(deploy.plan[:done], before[:done])
            cleaned = True
            for o in partial:
                try:
                    run_op(svc, o)
                except Exception:
                    cleaned = False
            try:
                _disarm(svc)
            except Exception:
                pass
            failed = deploy.plan[done]['label'] if done < len(deploy.plan) else 'the change'
            note = ' The steps before it were undone; the router is as it was.' if cleaned else \
                ' Some earlier steps could not be undone automatically — check the router or restore the backup.'
            return _finish(deploy, 'failed', error=f'The router refused “{failed}”: {exc}.{note}'[:1500], undone_by='failure')
        now = timezone.now()
        if armed:
            return _finish(deploy, 'applied', applied_at=now, undo_until=now + timedelta(minutes=deploy.undo_minutes))
        return _finish(deploy, 'kept', applied_at=now)
    finally:
        try:
            svc.close()
        except Exception:
            pass


def deploy_link(deploy, state):
    """Queue the first piece through TapTap Link; every answer from the router sends the next one."""
    before = before_state(SnapshotReader(state), deploy.plan)
    deploy.rollback_plan, deploy.manual_undo = rollback_plan(deploy.plan, before)
    armed = bool(deploy.undo_minutes and deploy.rollback_plan)
    parts = {f'p{i}': text for i, text in enumerate(tiles.chunks(deploy.plan))}
    order = list(parts)
    undo = f'/system scheduler remove [find where name="{UNDO_SCHEDULER}"]; ' + undo_script(deploy.rollback_plan)
    if armed:
        parts['arm'] = arm_script(deploy)
        order.insert(0, 'arm')
    if max(len(parts.get('arm', '')), len(undo)) > LINK_SCRIPT_MAX:
        raise StudioError('This board is too large to send through TapTap Link in one go. Switch some tiles off, deploy, '
                          'then switch them on and deploy again.')
    parts.update(keep=KEEP_SCRIPT, undo=undo, order=order)
    deploy.link_parts, deploy.status, deploy.transport = parts, 'sending', 'TapTap Link'
    deploy.save()
    _queue_step(deploy, order[0])
    return deploy


def _queue_step(deploy, step):
    from .linkops import send
    order = [s for s in deploy.link_parts.get('order', []) if s != 'arm']
    label = {'arm': 'undo timer', 'keep': 'keep the change', 'undo': 'undo the change'}.get(step) \
        or f'part {order.index(step) + 1} of {len(order)}'
    send(deploy.router, LINK_KIND, {'deploy': deploy.pk, 'step': step}, label=f'Config Studio #{deploy.pk}: {label}',
         user=deploy.actor, minutes=30)
    deploy.link_step = step
    deploy.save(update_fields=['link_step'])


def link_body(cmd):
    """The script of one queued Link command — read from the plan stored on the server, never from the browser."""
    params = cmd.params or {}
    deploy = RouterStudioDeploy.objects.filter(pk=int(params.get('deploy') or 0), router=cmd.router).first()
    step = str(params.get('step') or '')
    text = (deploy.link_parts or {}).get(step) if deploy and step != 'order' else None
    if not isinstance(text, str) or not text:
        raise ValueError('This Config Studio change no longer exists.')
    return text


def link_ack(cmd, ok, result=''):
    params = cmd.params or {}
    deploy = RouterStudioDeploy.objects.filter(pk=int(params.get('deploy') or 0), router=cmd.router).first()
    if not deploy:
        return
    step, order, now = str(params.get('step') or ''), list((deploy.link_parts or {}).get('order') or []), timezone.now()
    if step == 'keep':
        if ok:
            _finish(deploy, 'kept')
        else:
            _finish(deploy, 'undone', undone_by='router', error='The undo timer had already run: the router put the old setup back by itself.')
        return
    if step == 'undo':
        if deploy.status == 'failed':
            deploy.error = (deploy.error + (' The steps before it were undone.' if ok else ' The undo did not complete — check the router.'))[:1500]
            deploy.save(update_fields=['error'])
            _reread(deploy.router, deploy.actor)
        elif ok:
            _finish(deploy, 'undone', undone_by=deploy.undone_by or 'user')
        else:
            deploy.error = 'The router could not undo everything — check it, or restore the backup.'
            deploy.save(update_fields=['error'])
        return
    if step not in order or deploy.status != 'sending':
        return
    if not ok:
        _finish(deploy, 'failed', undone_by='failure',
                error=f'The router refused {"the undo timer" if step == "arm" else "a part of the change"}: {result or "no reason given"}.'[:1500])
        if step != 'arm':          # the timer is armed and earlier parts may be in: take them back now
            try:
                _queue_step(deploy, 'undo')
            except Exception:
                logger.exception('Config Studio: could not queue the undo')
        return
    if step == 'arm':
        deploy.undo_until = now + timedelta(minutes=deploy.undo_minutes)
        deploy.save(update_fields=['undo_until'])
    nxt = order.index(step) + 1
    if nxt < len(order):
        try:
            _queue_step(deploy, order[nxt])
        except Exception as exc:
            _finish(deploy, 'failed', error=f'TapTap Link stopped answering in the middle: {exc}', undone_by='failure')
        return
    if 'arm' in order:
        _finish(deploy, 'applied', applied_at=now)
    else:
        _finish(deploy, 'kept', applied_at=now)


def settle(deploy):
    """A change nobody kept before the timer ran out is gone: the router undid it."""
    if deploy.status == 'applied' and deploy.undo_until and timezone.now() > deploy.undo_until + timedelta(seconds=20):
        _finish(deploy, 'undone', undone_by='router',
                error='Nobody pressed “Keep” in time, so the router put the old setup back by itself.')
    return deploy


def keep(deploy, connect=None):
    settle(deploy)
    if deploy.status != 'applied':
        raise StudioError('This change is not waiting for “Keep” any more.')
    if deploy.transport == 'TapTap Link':
        _queue_step(deploy, 'keep')
        return deploy
    from .mikrotik import MikroTikService
    svc = (connect or (lambda r: MikroTikService(r).connect()))(deploy.router)
    try:
        still = _disarm(svc)
    finally:
        svc.close()
    if still:
        return _finish(deploy, 'kept')
    return _finish(deploy, 'undone', undone_by='router', error='The undo timer had already run: the router put the old setup back by itself.')


def undo(deploy, connect=None):
    if deploy.status not in ('applied', 'kept'):
        raise StudioError('Only a change that is on the router can be undone.')
    newer = RouterStudioDeploy.objects.filter(router=deploy.router, status__in=('applied', 'kept'), pk__gt=deploy.pk).exists()
    if newer:
        raise StudioError('A newer change was made after this one. Undo that one first.')
    if deploy.transport == 'TapTap Link':
        deploy.undone_by = 'user'
        deploy.save(update_fields=['undone_by'])
        _queue_step(deploy, 'undo')
        return deploy
    from .mikrotik import MikroTikService
    svc = (connect or (lambda r: MikroTikService(r).connect()))(deploy.router)
    errors = []
    try:
        _disarm(svc)
        for o in deploy.rollback_plan:
            try:
                run_op(svc, o)
            except Exception as exc:
                errors.append(f'{o["label"]}: {exc}')
    finally:
        svc.close()
    return _finish(deploy, 'undone', undone_by='user', error=('Not everything could be put back: ' + '; '.join(errors))[:1500] if errors else '')


def fresh_backup(router):
    since = timezone.now() - timedelta(minutes=BACKUP_FRESH)
    return RouterBackup.objects.filter(router=router, created_at__gte=since, error='').order_by('-created_at').first()


def start_backup(router, user, request=None):
    """The existing TapTap backup (server copy when the router supports it). Returns a RouterBackup."""
    try:
        from .backup_server import start_backup as server_backup
        record, _storage = server_backup(router, user=user, request=request)
        return record
    except ImportError:
        pass
    if _link(router):
        from . import agent
        import re
        name = re.sub(r'[^A-Za-z0-9_-]', '-', f'taptap-{router.name}-{timezone.localtime():%Y%m%d-%H%M%S}')[:60]
        agent.queue(router, 'backup', {'file': name}, label='Backup before a Config Studio change', user=user, minutes=30)
        return RouterBackup.objects.create(router=router, name=name, backup_file=name + '.backup', export_file=name + '.rsc', created_by=user)
    from .mikrotik import MikroTikService
    from .portctl import backup
    svc = MikroTikService(router).connect()
    try:
        return backup(svc, router, user=user)
    finally:
        svc.close()


def backup_json(record):
    if not record:
        return None
    state = ''
    try:
        state = record.server_storage.get_status_display()
    except Exception:
        state = 'Saved on the router' if record.backup_file or record.export_file else ''
    return {'id': record.pk, 'name': record.name, 'at': record.created_at.isoformat(), 'state': state, 'error': record.error,
            'fresh': not record.error and record.created_at >= timezone.now() - timedelta(minutes=BACKUP_FRESH)}


def deploy_json(d):
    settle(d)
    left = int((d.undo_until - timezone.now()).total_seconds()) if d.status == 'applied' and d.undo_until else 0
    order = (d.link_parts or {}).get('order') or []
    return {'id': d.pk, 'status': d.status, 'status_label': d.get_status_display(), 'summary': d.summary, 'transport': d.transport,
            'error': d.error, 'undone_by': d.undone_by, 'seconds_left': max(0, left), 'undo_minutes': d.undo_minutes,
            'at': (d.applied_at or d.created_at).isoformat(), 'by': (d.actor.get_full_name() or d.actor.username) if d.actor else '',
            'steps': len(d.plan), 'manual_undo': d.manual_undo, 'can_undo': d.status in ('applied', 'kept') and bool(d.rollback_plan),
            'progress': [order.index(d.link_step) + 1, len(order)] if d.link_step in order and d.status == 'sending' else None,
            'tiles': [{**t, 'params': {k: ('' if k in tiles.secret_fields(t['type']) else v) for k, v in t['params'].items()}}
                      for t in tiles.clean_board(d.tiles)]}


# ───────────────────────────── views ─────────────────────────────
@login_required
@require_GET
def studio(request, pk):
    router = _router(request, pk)
    urls = {name: reverse(f'config_studio_{name}', args=[router.pk])
            for name in ('state', 'check', 'section', 'trace', 'ai', 'read', 'backup', 'deploy', 'export')}
    urls['action'] = reverse('config_studio_action', args=[router.pk, 0])
    urls['control'] = reverse('router_control', args=[router.pk])
    return render(request, 'core/config_studio.html', {
        'router': router, 'studio_boot': {'urls': urls, 'catalogue': tiles.catalogue(), 'kits': KITS, 'router': router.name,
                                          'undo_choices': list(UNDO_CHOICES), 'backup_fresh': BACKUP_FRESH}})


@login_required
@require_GET
def studio_state(request, pk):
    router = _router(request, pk)
    board = board_of(router)
    from . import config_studio_ai as ai
    deploys = [deploy_json(d) for d in RouterStudioDeploy.objects.filter(router=router).select_related('actor')[:8]]
    online, why = True, ''
    if _link(router):
        from .linklive import link_state
        online, why = link_state(router)
    return JsonResponse({'ok': True, 'tiles': board.tiles, 'base': board.base, 'chat': board.chat[-12:], 'deploys': deploys,
                         'backup': backup_json(RouterBackup.objects.filter(router=router).order_by('-created_at').first()),
                         'transport': transport_name(router), 'online': online, 'offline_reason': why, 'ai_ready': ai.ready(),
                         'status': router.status})


@login_required
@require_POST
def studio_check(request, pk):
    router = _router(request, pk)
    board = save_board(router, request.user, _body(request))
    out, _ops, _vr = examine_board(router, board.tiles, board.base)
    out['sections'] = section_list(router, board.base, _vr)
    return JsonResponse(out)


def section_list(router, base, vr=None):
    state, _ = base_state(router, base)
    names = labels()
    marks = {}
    for c in (vr.changes if vr else []):
        if c['kind'] in ('new', 'changed', 'removed'):
            marks[c['resource']] = marks.get(c['resource'], 0) + 1
    paths = list(dict.fromkeys(list(state or {}) + list(marks)))
    return [{'path': p, 'label': names.get(p) or p.strip('/').replace('/', ' › '), 'group': p.strip('/').split('/')[0],
             'count': len((state or {}).get(p) or []), 'touched': marks.get(p, 0)} for p in sorted(paths)]


@login_required
@require_POST
def studio_section(request, pk):
    """Rows of one RouterOS menu: as the router has them now, or as they would be after the board."""
    router = _router(request, pk)
    data = _body(request)
    board = board_of(router)
    want = '/' + str(data.get('path') or '').strip().strip('/')
    state, info = base_state(router, board.base)
    if state is None:
        return _fail('The router has not been read yet.')
    vr = sim.VirtualRouter(state)
    if data.get('view') != 'now':
        ops, _ = tiles.compile_board(board.tiles, context_for(router))
        vr.apply(ops)
    rows = [{'mark': r.get('_sim', ''), 'was': _redact(r.get('_was', {})), 'row': _redact(sim._public(r))} for r in vr.rows(want)[:400]]
    return JsonResponse({'ok': True, 'path': want, 'rows': rows, 'total': len(vr.rows(want)), 'label': labels().get(want, want)})


@login_required
@require_POST
def studio_trace(request, pk):
    router = _router(request, pk)
    data = _body(request)
    board = board_of(router)
    state, _ = base_state(router, board.base)
    if state is None:
        return _fail('The router has not been read yet.')
    ops, _ = tiles.compile_board(board.tiles, context_for(router))
    vr = sim.VirtualRouter(state)
    if data.get('view') != 'now':
        vr.apply(ops)
    try:
        dst = str(data.get('to') or 'internet')
        if dst not in ('internet', 'router'):
            dst = tiles.v_ip(dst, 'destination')
        mac = tiles.v_mac(data['mac']) if str(data.get('mac') or '').strip() else ''
        host = tiles.v_host(data['host']) if str(data.get('host') or '').strip() else ''
        proto = tiles.v_choice(data.get('proto'), 'protocol', ('tcp', 'udp', 'icmp'), 'tcp')
        port = tiles.v_int(data.get('port') or 443, 'port', 0, 65535)
        src = tiles.v_ip(data.get('ip'), 'from address') if str(data.get('ip') or '').strip() else ''
        iface = tiles.v_name(data.get('iface'), 'port') if str(data.get('iface') or '').strip() else ''
    except tiles.TileError as exc:
        return _fail(exc)
    return JsonResponse({'ok': True, 'trace': vr.trace(src_ip=src, in_if=iface, dst=dst, proto=proto, port=port, mac=mac, host=host,
                                                       logged_in=bool(data.get('logged_in')))})


@login_required
@require_POST
def studio_ai(request, pk):
    router = _router(request, pk)
    data = _body(request)
    from . import config_studio_ai as ai
    text = str(data.get('text') or '').strip()[:1200]
    if not text:
        return _fail('Tell the helper what you want the router to do.')
    board = save_board(router, request.user, data)
    out, _ops, vr = examine_board(router, board.tiles, board.base)
    try:
        answer = ai.ask(request.user, router, text, board.tiles, vr, out.get('findings') or [], context_for(router))
    except ai.HelperBusy as exc:
        return _fail(exc, status=429)
    board.tiles = tiles.clean_board(answer['tiles'])
    board.chat = (board.chat + [{'who': 'you', 'text': text}, {'who': 'helper', 'text': answer['say'], 'engine': answer['engine']}])[-12:]
    board.save()
    return JsonResponse({'ok': True, 'say': answer['say'], 'engine': answer['engine'], 'tiles': board.tiles, 'touched': answer['touched'],
                         'chat': board.chat})


@login_required
@require_POST
def studio_read(request, pk):
    """Read the router's configuration again (the picture every simulation starts from)."""
    router = _router(request, pk)
    if _link(router):
        from .linkops import refresh
        try:
            created = refresh(router, request.user)
        except ValueError as exc:
            return _fail(exc)
        return JsonResponse({'ok': True, 'queued': True, 'message': 'Reading through TapTap Link — the picture fills in over the next minute.'
                             if created else 'A read is already running.'})
    from .mikrotik import MikroTikService
    try:
        svc = MikroTikService(router).connect()
        try:
            cfg = svc.configuration_snapshot()
        finally:
            svc.close()
    except Exception as exc:
        return _fail(f'Could not read {router.name}: {exc}')
    RouterConfigSnapshot.objects.update_or_create(router=router, defaults={
        'sections': cfg['sections'], 'load_balancing': cfg['load_balancing'], 'captured_at': cfg['captured_at']})
    return JsonResponse({'ok': True, 'queued': False, 'message': f'{router.name} read: {len(cfg["sections"])} menus.'})


@login_required
@require_POST
def studio_backup(request, pk):
    router = _router(request, pk)
    try:
        record = start_backup(router, request.user, request)
    except Exception as exc:
        return _fail(f'The backup could not be made: {exc}')
    if record.error:
        return _fail(f'The backup reported a problem: {record.error}', backup=backup_json(record))
    return JsonResponse({'ok': True, 'backup': backup_json(record)})


@login_required
@require_POST
def studio_deploy(request, pk):
    router = _router(request, pk)
    data = _body(request)
    board = board_of(router)
    if RouterStudioDeploy.objects.filter(router=router, status='sending').exists():
        return _fail('A change is already on its way to this router. Wait for it to finish.', status=409)
    waiting = RouterStudioDeploy.objects.filter(router=router, status='applied').first()
    if waiting and settle(waiting).status == 'applied':
        return _fail('The last change is still waiting for “Keep” or “Undo”. Decide that first.', status=409)
    out, ops, _vr = examine_board(router, board.tiles, 'router')
    if out['needs_read']:
        return _fail('TapTap has not read this router yet. Press “Read the router” first.')
    if out['problems']:
        return _fail('Some tiles still have a blank that is not right. Fix the red tiles first.')
    if not ops:
        return _fail('The board is empty: there is nothing to put on the router.')
    changing = [c for c in out['changes'] if c['kind'] in ('new', 'changed', 'removed')]
    if not changing:
        return _fail('The router already has everything on this board. Nothing to deploy.')
    if not out['safe'] and str(data.get('confirm') or '').strip().upper() != 'DEPLOY ANYWAY':
        return _fail('The virtual router found a serious problem. Fix it, or type DEPLOY ANYWAY to go ahead.', status=409, needs='DEPLOY ANYWAY')
    backup = fresh_backup(router)
    if not backup and str(data.get('no_backup') or '').strip().upper() != 'NO BACKUP':
        return _fail(f'Make a backup first (one from the last {BACKUP_FRESH} minutes is needed).', status=409, needs_backup=True)
    try:
        minutes = int(data.get('undo_minutes', 5))
    except (TypeError, ValueError):
        minutes = 5
    if minutes not in UNDO_CHOICES:
        minutes = 5
    c = out['counts']
    deploy = RouterStudioDeploy.objects.create(
        business=router.business, router=router, actor=request.user, backup=backup, tiles=board.tiles, plan=ops, undo_minutes=minutes,
        summary=f'{len([t for t in board.tiles if not t.get("off")])} tiles · {c.get("new", 0)} new, {c.get("changed", 0)} changed, {c.get("removed", 0)} removed'[:255])
    try:
        if _link(router):
            from .linkops import ensure_online
            ensure_online(router)
            state, _ = base_state(router, 'router')
            deploy_link(deploy, state)
        else:
            deploy_direct(deploy)
    except Exception as exc:
        if deploy.status in ('draft', 'sending'):
            _finish(deploy, 'failed', error=str(exc)[:1500])
        logger.warning('Config Studio deploy #%s failed: %r', deploy.pk, exc)
    return JsonResponse({'ok': deploy.status != 'failed', 'deploy': deploy_json(deploy), 'message': deploy.error})


@login_required
def studio_action(request, pk, deploy_id):
    """GET: where a deploy stands. POST {action: keep | undo}."""
    router = _router(request, pk)
    deploy = get_object_or_404(RouterStudioDeploy, pk=deploy_id, router=router)
    if request.method == 'POST':
        action = str(_body(request).get('action') or '')
        try:
            if action == 'keep':
                keep(deploy)
            elif action == 'undo':
                undo(deploy)
            else:
                return _fail('Unknown action.')
        except (StudioError, ValueError) as exc:
            return _fail(exc, deploy=deploy_json(deploy))
        except Exception as exc:
            when = f' If this lasts, the router undoes the change by itself at {timezone.localtime(deploy.undo_until):%H:%M}.' \
                if deploy.status == 'applied' and deploy.undo_until else ''
            return _fail(f'TapTap cannot reach {router.name} right now: {exc}.{when}', deploy=deploy_json(deploy))
    return JsonResponse({'ok': True, 'deploy': deploy_json(deploy)})


@login_required
@require_GET
def studio_export(request, pk):
    router = _router(request, pk)
    board = board_of(router)
    ops, problems = tiles.compile_board(board.tiles, context_for(router))
    text = tiles.script(ops, f'{router.name} — Config Studio board, {timezone.localtime():%Y-%m-%d %H:%M}')
    if problems:
        text = ''.join(f'# NOT INCLUDED: {p["message"]}\n' for p in problems) + text
    response = HttpResponse(text, content_type='text/plain; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{tiles.slug(router.name, 40)}-studio.rsc"'
    return response


# ───────────────────────────── installation ─────────────────────────────
URLS = [
    ('routers/<int:pk>/studio/', studio, 'config_studio'),
    ('routers/<int:pk>/studio/state/', studio_state, 'config_studio_state'),
    ('routers/<int:pk>/studio/check/', studio_check, 'config_studio_check'),
    ('routers/<int:pk>/studio/section/', studio_section, 'config_studio_section'),
    ('routers/<int:pk>/studio/trace/', studio_trace, 'config_studio_trace'),
    ('routers/<int:pk>/studio/ai/', studio_ai, 'config_studio_ai'),
    ('routers/<int:pk>/studio/read/', studio_read, 'config_studio_read'),
    ('routers/<int:pk>/studio/backup/', studio_backup, 'config_studio_backup'),
    ('routers/<int:pk>/studio/deploy/', studio_deploy, 'config_studio_deploy'),
    ('routers/<int:pk>/studio/deploy/<int:deploy_id>/', studio_action, 'config_studio_action'),
    ('routers/<int:pk>/studio/export.rsc', studio_export, 'config_studio_export'),
]


def _install_agent():
    from . import agent
    if getattr(agent, '_config_studio_installed', False):
        return
    agent.SAFE_KINDS.add(LINK_KIND)
    agent.DEFAULT_EXPIRY.setdefault(LINK_KIND, 30)

    original_body = agent.command_body

    @functools.wraps(original_body)
    def command_body(cmd):
        if cmd.kind == LINK_KIND:
            return link_body(cmd)
        return original_body(cmd)
    agent.command_body = command_body

    original_ack = agent.handle_ack

    @functools.wraps(original_ack)
    def handle_ack(cmd_id, given_nonce, status, result=''):
        accepted = original_ack(cmd_id, given_nonce, status, result)
        if accepted:
            try:
                from .models import AgentCommand
                cmd = AgentCommand.objects.filter(pk=cmd_id, kind=LINK_KIND).first()
                if cmd:
                    link_ack(cmd, status == 'ok', result)
            except Exception:
                logger.exception('Config Studio: Link answer could not be handled')
        return accepted
    agent.handle_ack = handle_ack
    agent._config_studio_installed = True


def install():
    from . import permissions, urls
    names = {getattr(x, 'name', None) for x in urls.urlpatterns}
    for route, view, name in URLS:
        permissions.URL_PERMS[name] = 'network.manage'
        if name not in names:
            urls.urlpatterns.append(path(route, view, name=name))
    _install_agent()
