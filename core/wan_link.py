"""Internet lines (load balancing / failover) applied through TapTap Link — no live connection needed.

Before this, a router reachable only through TapTap Link could not get its Internet lines from TapTap at all
("needs a live RouterOS connection so TapTap can undo the change"). Owners pasted a downloaded script or built
it by hand, and lines ended up "No route yet" or both going out through one modem.

How it stays safe without a live connection
-------------------------------------------
1. ``arm``   An undo timer goes onto the router FIRST (/system scheduler TapTap-WAN-undo). If nothing cancels it,
             it puts the router back the way it was a few minutes later — no TapTap needed.
2. ``part``  The new setup, a few lines per command. Everything is ADDED before anything old is removed, so the
             router never has a moment without a default route between two check-ins (with no route it could not
             check in again for the next part). The old TapTap setup is removed in the last part.
3. The router keeps checking in. ``progress()`` (every heartbeat) waits until every part is done and the
   router has checked in again at least SETTLE seconds later — proof its internet works through the new
   routes — and then sends ``confirm``, which removes the undo timer. A part that fails sends ``undo`` at once.
   If the router cannot check in, its own timer undoes everything and ``confirm`` later finds the timer gone.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from . import wan

logger = logging.getLogger('taptap.wan')

LINK_KIND = 'wan_lines'
STEPS = {'arm', 'part', 'confirm', 'undo'}
CHUNK = 1300               # raw characters of RouterOS per command (escaping roughly doubles it; Link batch budget)
SETTLE = 60                # seconds the router must stay online on the new routes before TapTap confirms
MIN_MINUTES = 8            # the undo timer must outlast delivering every part one check-in at a time
_INSTALLED = False


class LinkApplyError(Exception):
    pass


# ─────────────────────────── the RouterOS text ───────────────────────────
def undo_text(original, v7):
    """What the undo timer (or an ``undo`` command) runs: the normal undo, plus putting back the owner's own
    default routes that this apply switched off (they are marked LINK_OFF instead of remembered by id)."""
    text = wan.render_undo(original or {}, v7, guarded=True)
    restore = (f':do {{ :foreach r in=[/ip route find where comment~"{wan.LINK_OFF}"] do={{ '
               f':local c [:tostr [/ip route get $r comment]]; '
               f'/ip route set $r disabled=no comment=[:pick $c 0 [:find $c "{wan.LINK_OFF}"]] }} }} on-error={{}}')
    return restore + '\n' + text + '\n:log warning "TapTap: Internet lines change undone - the router is back to its earlier setup"'


def plan_lines(plan):
    """The plan as CLI lines in the safe order: new items, then removal of older TapTap runs, then the hand-over
    of DHCP/PPPoE default routes (only once the new routes exist)."""
    v7, run = plan['v7'], plan['run']
    body, tail = [], []
    for op in plan['ops']:
        if op['op'] == 'purge':
            continue                                   # replaced by the keep-this-run clean-up below
        (tail if op.get('group') == 'clients' else body).extend(wan.op_lines(op, v7, run=run))
    cleanup = [f':do {{ {c} }} on-error={{}}' for c in wan.purge_lines(v7, keep_run=run)]
    return body + cleanup + tail


def chunks(lines, size=CHUNK):
    out, cur = [], ''
    for line in lines:
        if cur and len(cur) + len(line) + 1 > size:
            out.append(cur); cur = ''
        cur = (cur + '\n' + line) if cur else line
    if cur:
        out.append(cur)
    return out


def body(params):
    """RouterOS commands for one queued ``wan_lines`` command. Only TapTap writes these params."""
    step = str(params.get('step') or '')
    if step not in STEPS:
        raise ValueError('Unknown Internet lines step.')
    name = wan.UNDO_SCHEDULER
    if step == 'arm':
        return (f'/system scheduler remove [find where name="{name}"]; '
                f'/system scheduler add name={name} interval={int(params["minutes"])}m policy=read,write,test '
                f'on-event={wan.q(str(params["undo"]).replace(chr(10), "; "))} '
                f'comment="{wan.TAG} #{params["run"]} · automatic undo unless TapTap confirms"')
    if step == 'part':
        return str(params['source'])
    if step == 'confirm':
        # The timer gone means the router already undid the change on its own: that must not count as confirmed.
        return (f':if ([:len [/system scheduler find where name="{name}"]] = 0) do={{ :error "TapTap: already undone" }}; '
                f'/system scheduler remove [find where name="{name}"]; '
                ':log warning "TapTap: new Internet lines confirmed"')
    # undo: run it now and drop the timer
    return (f'/system scheduler remove [find where name="{name}"]; ' + str(params['undo']).replace('\n', '; '))


# ─────────────────────────── start / progress ───────────────────────────
def start(router, setup, cfg, facts, user=None, minutes=None):
    """Queue the whole apply. Returns a summary dict. Raises LinkApplyError / ValueError (router offline)."""
    from .linkops import ensure_online, send
    ensure_online(router)
    if pending(router):
        raise LinkApplyError('An Internet lines change is still being sent to the router — wait for it to finish.')
    minutes = max(MIN_MINUTES, int(minutes or MIN_MINUTES))
    plan = wan.build_plan(cfg, facts)
    original = setup.original if setup.original and setup.status in {'pending', 'active'} else wan.original_from_facts(facts)
    undo = undo_text(original, plan['v7'])
    parts = chunks(plan_lines(plan))
    ids = [send(router, LINK_KIND, {'step': 'arm', 'run': plan['run'], 'minutes': minutes, 'undo': undo},
                label='Internet lines: safety timer (undoes the change unless confirmed)', user=user, minutes=30).pk]
    for k, src in enumerate(parts):
        ids.append(send(router, LINK_KIND, {'step': 'part', 'run': plan['run'], 'part': k, 'parts': len(parts), 'source': src},
                        label=f'Internet lines: part {k + 1} of {len(parts)}', user=user, minutes=30).pk)
    doctor = []
    if plan.get('doctor_lines'):
        try:
            from .wan_doctor import link_install
            doctor, _ = link_install(router, plan['doctor_lines'], user=user)
        except Exception as exc:                      # the Doctor must never stop an apply
            logger.warning('Internet Doctor via Link failed for %s: %s', router, exc)
    now = timezone.now()
    setup.config = cfg_raw(setup, cfg)
    setup.facts = facts
    setup.original = original
    setup.run_id = plan['run']
    setup.status = 'pending'
    setup.applied_at = now
    setup.confirm_by = now + timedelta(minutes=minutes)
    setup.confirmed_at = None
    if getattr(user, 'is_authenticated', False):
        setup.applied_by = user
    setup.last_result = {'via': 'link', 'commands': ids, 'parts': len(parts), 'minutes': minutes, 'doctor': doctor,
                         'stage': 'sending', 'message': f'Sending {len(parts)} part(s) to the router through TapTap Link.'}
    setup.save()
    return {'parts': len(parts), 'minutes': minutes, 'run': plan['run'], 'doctor': doctor}


def cfg_raw(setup, cfg):
    """Keep what the owner typed (setup.config) — cfg is the normalized form; only fill it when empty."""
    return setup.config or {'strategy': cfg['strategy'], 'links': [dict(l) for l in cfg['links']]}


def pending(router):
    from .models import AgentCommand
    return AgentCommand.objects.filter(router=router, kind=LINK_KIND, status__in=['queued', 'sent']).exists()


def progress(router, now=None):
    """Called on every TapTap Link check-in. Moves a Link apply on: confirm, undo, or report. Returns the stage."""
    from .linkops import send
    from .models import AgentCommand, WanSetup
    setup = WanSetup.objects.filter(router=router).first()
    if not setup or setup.status != 'pending' or (setup.last_result or {}).get('via') != 'link':
        return ''
    now = now or timezone.now()
    res = dict(setup.last_result)
    cmds = {c.pk: c for c in AgentCommand.objects.filter(pk__in=res.get('commands') or [])}
    stage = res.get('stage')

    def finish(status, stage, message, **extra):
        res.update(stage=stage, message=message, **extra)
        setup.status = status
        setup.last_result = res
        if status == 'active':
            setup.confirmed_at, setup.confirm_by = now, None
        elif status in {'undone', 'failed'}:
            setup.confirm_by = None
        setup.save()
        return stage

    if stage == 'confirming':
        c = AgentCommand.objects.filter(pk=res.get('confirm')).first()
        if c and c.status == 'done':
            return finish('active', 'active', 'The router stayed online on the new Internet lines — confirmed.')
        if c and c.status in {'failed', 'expired', 'cancelled'}:
            return finish('undone', 'undone', 'The router lost its internet with the new setup and undid it by itself. '
                                              'Nothing changed — check the lines and apply again.')
        return stage
    if stage == 'undoing':
        c = AgentCommand.objects.filter(pk=res.get('undo')).first()
        if c and c.status in {'done', 'failed', 'expired'}:
            return finish('failed', 'undone', res.get('message') or 'Undone.')
        return stage
    bad = [c for c in cmds.values() if c.status in {'failed', 'expired', 'cancelled'}]
    if bad:
        why = (bad[0].result or '').strip()[:200]
        try:
            u = send(router, LINK_KIND, {'step': 'undo', 'undo': undo_text(setup.original, bool((setup.facts or {}).get('v7', True)))},
                     label='Internet lines: undo (a part failed)', minutes=30)
            res['undo'] = u.pk
            return finish('pending', 'undoing', f'{bad[0].label} failed on the router{": " + why if why else ""}. '
                                                'TapTap is putting the earlier setup back.')
        except ValueError:
            return finish('pending', 'undoing', 'A part failed; the router will undo the change by itself.')
    if cmds and all(c.status == 'done' for c in cmds.values()) and len(cmds) == len(res.get('commands') or []):
        last = max((c.done_at for c in cmds.values() if c.done_at), default=None)
        if last and (now - last).total_seconds() >= SETTLE:
            c = send(router, LINK_KIND, {'step': 'confirm'}, label='Internet lines: confirm (remove the safety timer)', minutes=30)
            res['confirm'] = c.pk
            return finish('pending', 'confirming', 'The router is online on the new setup — confirming.')
        res.update(stage='settling', message='All parts are on the router — checking that it stays online.')
        setup.last_result = res
        setup.save(update_fields=['last_result', 'updated_at'])
        return 'settling'
    return stage or 'sending'


def undo(router, setup, user=None):
    """Owner pressed Undo on a Link router."""
    from .linkops import send
    c = send(router, LINK_KIND, {'step': 'undo', 'undo': undo_text(setup.original or wan.original_from_facts(setup.facts or {}),
                                                                    bool((setup.facts or {}).get('v7', True)))},
             label='Internet lines: undo', user=user, minutes=60)
    setup.status, setup.confirm_by = 'undone', None
    setup.last_result = {**(setup.last_result or {}), 'via': 'link', 'stage': 'undone', 'undo': c.pk,
                         'message': 'Undo sent — the router returns to its earlier setup at its next check-in.'}
    setup.save()
    return c


# ─────────────────────────── hooks ───────────────────────────
def _wrap_command_body(original):
    from functools import wraps

    @wraps(original)
    def command_body(cmd):
        if cmd.kind == LINK_KIND:
            return body(cmd.params or {})
        return original(cmd)
    return command_body


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    from . import agent
    agent.SAFE_KINDS.add(LINK_KIND)
    agent.NEVER_RESEND.add(LINK_KIND)        # a part re-run would add every route twice: fail and undo instead
    agent.ACK_WAIT.setdefault(LINK_KIND, 180)
    agent.command_body = _wrap_command_body(agent.command_body)
    _INSTALLED = True
