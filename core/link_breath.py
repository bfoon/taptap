"""Breathing check-ins — TapTap Link asks less of the router when nothing needs it.

Why
---
A Link router checked in every 10 seconds, day and night. Each check-in costs the router:

* a new HTTPS connection (the handshake is the expensive part on a small MikroTik CPU),
* reading up to 300 hotspot sessions, 200 bypassed hosts and 60 interfaces one by one,
* uploading that list (about 20 kB with 300 customers online).

That is 8 640 check-ins a day whether or not anything is happening — and every 30 minutes a full inventory sync on
top, even when the last ones found nothing to repair.

What this module does (server side only — the script on the router is not changed)
-----------------------------------------------------------------------------------
* **The pace follows the work.** At the end of each check-in TapTap decides how soon it needs the next one and, when
  that differs from what the router does now, adds one line to its answer:
  ``/system scheduler set [find name="taptap-link"] interval=20s``.

      fast   the router's own interval (10 s)   a customer is at the login page, a voucher or command is waiting or
                                                was sent in the last 2 minutes, a full sync runs, someone has the
                                                router's live pages open
      calm   2x (20 s, at most 30 s)             customers online, nothing waiting
      rest   3x (30 s, at most 45 s)             nobody online and nothing waiting (night)
      ease   3x (30 s, at most 45 s)             the router's CPU is at 85 % or more: TapTap steps back

  The moment work appears the next answer puts the router back on "fast". The wanted pace is sent again every
  10 minutes, so a router whose script was reinstalled falls back in line by itself.
* **Full syncs spread out while they find nothing.** After 2, 3, 4… automatic syncs in a row that healed nothing,
  the next one waits 60, 120 … minutes (never more than LINK_SYNC_MAX_MINUTES, 120). Anything healed, a failed
  sync, a router restart or a sync someone presses brings it back to 30 minutes.
* **It is measured.** Every check-in's size is counted per router per day (``usage``), so the Link page shows what
  TapTap really costs the router's line and how many check-ins the pace saved.

Offline detection follows the pace (RouterAgent.grace_seconds allows four intervals of silence).

Switch off: LINK_BREATHING=0. Tune: LINK_CALM_FACTOR (2), LINK_REST_FACTOR (3), LINK_CALM_MAX (30), LINK_REST_MAX (45),
LINK_EASE_CPU (85), LINK_FAST_HOLD (120 seconds).
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('taptap.link.breath')

MIN_SCRIPT = 4                    # heartbeat v4+: the scheduler is called "taptap-link" and runs the script by name
REASSERT = 600                    # say the wanted pace again this often (a reinstalled script resets its interval)
TLS_BYTES = 5500                  # rough cost of one new HTTPS connection (handshake both ways), for the estimate
LABEL = {'fast': 'Fast', 'calm': 'Calm', 'rest': 'Resting', 'ease': 'Stepping back'}


def _env(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def enabled():
    return os.getenv('LINK_BREATHING', '1').lower() not in ('0', 'false', 'no', 'off')


def base(agent):
    return max(10, int(getattr(agent, 'poll_seconds', 10) or 10))


# ─────────────────────────────── who needs the router right now ───────────────────────────────

def hold_fast(router_id, seconds=None):
    """Something is going on with this router (a command, a customer, a person looking): stay fast for a while."""
    try:
        cache.set(f'tt:link:busy:{router_id}', 1, seconds or _env('LINK_FAST_HOLD', 120))
    except Exception:
        pass


def watching(business_id, seconds=45):
    """Someone has a live page of this business open (router status, live sessions)."""
    try:
        cache.set(f'tt:link:watch:{business_id}', 1, seconds)
    except Exception:
        pass


def decide(router, agent):
    """(seconds, level, why) — how soon TapTap needs the next check-in."""
    from .models import AgentCommand
    b = base(agent)
    if not enabled():
        return b, 'fast', 'breathing is switched off'
    try:
        if cache.get(f'tt:link:urgent:{router.pk}'):
            hold_fast(router.pk)
            return b, 'fast', 'a customer is at the login page'
        if AgentCommand.objects.filter(router=router, status__in=['queued', 'sent']).exists():
            hold_fast(router.pk)
            return b, 'fast', 'commands are on their way'
        if router.vouchers.filter(source='taptap', mikrotik_sync_status='Pending', status='active').exists():
            hold_fast(router.pk)
            return b, 'fast', 'vouchers are waiting for the router'
        if cache.get(f'tt:link:busy:{router.pk}'):
            return b, 'fast', 'there was work a moment ago'
        if cache.get(f'tt:link:watch:{router.business_id}'):
            return b, 'fast', 'someone is watching the live pages'
    except Exception:
        logger.debug('breath decide %s', router, exc_info=True)
        return b, 'fast', 'could not tell — staying fast'
    cpu = getattr(agent, 'cpu_load', None) or 0
    rest = min(max(b, _env('LINK_REST_MAX', 45)), b * _env('LINK_REST_FACTOR', 3))
    if cpu >= _env('LINK_EASE_CPU', 85):
        return rest, 'ease', f'the router is very busy (CPU {cpu} %)'
    if not (getattr(agent, 'active_sessions', 0) or 0):
        return rest, 'rest', 'nobody is online'
    return min(max(b, _env('LINK_CALM_MAX', 30)), b * _env('LINK_CALM_FACTOR', 2)), 'calm', 'nothing is waiting'


def pace(router_id):
    """The interval TapTap last asked this router for (None: never asked — it runs at its own interval)."""
    try:
        v = cache.get(f'tt:link:pace:{router_id}')
        return int(v['s']) if v else None
    except Exception:
        return None


def state(router_id):
    try:
        return cache.get(f'tt:link:pace:{router_id}') or {}
    except Exception:
        return {}


def line(router, agent):
    """The RouterOS line that sets the pace, or '' when the router already runs at the wanted one."""
    try:
        if int(getattr(agent, 'script_version', 0) or 0) < MIN_SCRIPT:
            return ''
        secs, level, why = decide(router, agent)
        cur = state(router.pk)
        now = timezone.now().timestamp()
        if not cur and secs == base(agent) and enabled():
            # never asked and nothing to change: the router is already at its own interval
            cache.set(f'tt:link:pace:{router.pk}', {'s': secs, 'l': level, 'w': why, 'at': now}, 6 * 3600)
            return ''
        if cur and cur.get('s') == secs and now - float(cur.get('at') or 0) < REASSERT:
            if cur.get('l') != level or cur.get('w') != why:
                cache.set(f'tt:link:pace:{router.pk}', {**cur, 'l': level, 'w': why}, 6 * 3600)
            return ''
        if not enabled() and not cur:
            return ''
        cache.set(f'tt:link:pace:{router.pk}', {'s': secs, 'l': level, 'w': why, 'at': now}, 6 * 3600)
        return f':do {{ /system scheduler set [find name="taptap-link"] interval={int(secs)}s }} on-error={{}}'
    except Exception:
        logger.debug('breath line %s', router, exc_info=True)
        return ''


def with_pace(router, agent, script):
    """The check-in's answer with the pace line added (kept last, after TapTap's commands)."""
    extra = line(router, agent)
    if not extra:
        return script
    return (script + '\n' + extra) if script else extra


# ─────────────────────────────── what TapTap costs the router's line ───────────────────────────────

def _day(now=None):
    return timezone.localtime(now).strftime('%Y%m%d')


def note_traffic(router_id, up, down, now=None, agent=None):
    """One check-in: bytes the router sent and bytes TapTap answered (kept per day, 3 days)."""
    try:
        key = f'tt:link:use:{router_id}:{_day(now)}'
        u = cache.get(key) or {'n': 0, 'up': 0, 'down': 0, 'saved': 0.0, 'last': 0}
        ts = (now or timezone.now()).timestamp()
        u['n'] += 1
        u['up'] += int(up)
        u['down'] += int(down)
        if agent is not None and u.get('last'):
            # check-ins that did not happen because of the pace: the time since the last one, in base intervals
            gap = ts - u['last']
            b = base(agent)
            if b < gap < 120:
                u['saved'] = float(u.get('saved') or 0) + (gap / b - 1)
        u['last'] = ts
        cache.set(key, u, 3 * 86400)
    except Exception:
        pass


def _size(n):
    n = float(n)
    for unit in ('B', 'kB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit in ('B', 'kB') else f'{n:.1f} {unit}'
        n /= 1024


def usage(router_id, now=None):
    """Today's check-ins for the Link page: how many, how much data, and what the pace saved."""
    try:
        u = cache.get(f'tt:link:use:{router_id}:{_day(now)}')
    except Exception:
        u = None
    if not u or not u.get('n'):
        return None
    n, saved = int(u['n']), int(u.get('saved') or 0)
    per = (u['up'] + u['down']) / n + TLS_BYTES
    return {'checkins': n, 'up': _size(u['up']), 'down': _size(u['down']),
            'total': _size(u['up'] + u['down'] + n * TLS_BYTES), 'saved': saved,
            'saved_data': _size(saved * per) if saved else '',
            'saved_pct': round(100 * saved / (n + saved)) if saved else 0}


def summary(router, agent):
    """Everything the Link page shows about TapTap's own load on this router."""
    st = state(router.pk)
    secs = int(st.get('s') or base(agent))
    level = st.get('l') or 'fast'
    out = {'on': enabled(), 'seconds': secs, 'base': base(agent), 'level': level, 'label': LABEL.get(level, level),
           'why': st.get('w') or '', 'usage': usage(router.pk)}
    try:
        out['sync_every'], out['sync_why'] = sync_minutes(router)
    except Exception:
        out['sync_every'], out['sync_why'] = None, ''
    return out


# ─────────────────────────────── full syncs spread out while they find nothing ───────────────────────────────

def _uptime_seconds(agent):
    try:
        from .sync import _routeros_seconds
        return int(_routeros_seconds(getattr(agent, 'uptime', '') or ''))
    except Exception:
        return 0


def sync_minutes(router, base_minutes=None):
    """(minutes, why) until the next AUTOMATIC full sync of a Link router."""
    from django.conf import settings
    every = int(base_minutes or getattr(settings, 'LINK_SYNC_MINUTES', 30))
    if not enabled():
        return every, 'breathing is switched off'
    cap = max(every, _env('LINK_SYNC_MAX_MINUTES', 120))
    jobs = list(router.sync_jobs.filter(status__in=['success', 'failed']).order_by('-finished_at')
                .values_list('status', 'summary', 'finished_at')[:5])
    if not jobs:
        return every, 'no sync yet'
    clean = 0
    for status, summary, _at in jobs:
        doctor = (summary or {}).get('doctor') if isinstance(summary, dict) else None
        if status != 'success' or not isinstance(doctor, dict) or doctor.get('healed'):
            break
        clean += 1
    last_at = jobs[0][2]
    agent = getattr(router, 'agent', None)
    up = _uptime_seconds(agent) if agent is not None else 0
    if last_at and up and up < (timezone.now() - last_at).total_seconds():
        return every, 'the router restarted since the last sync'
    if clean < 2:
        return every, 'the last sync repaired something' if jobs[0][0] == 'success' and clean == 0 else 'checking again soon'
    minutes = min(cap, every * 2 ** (clean - 1))
    return minutes, f'the last {clean} syncs found nothing to repair'
