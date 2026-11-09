"""TapTap Link rhythm — a busy router is late, not offline; and TapTap stops making it busy.

What people saw
---------------
"After I restart the router everything is normal, but after some time it starts showing *connection unstable*."

Why
---
Every two minutes TapTap asked each Link router for an apps & sites sample: its whole DNS cache and every
connection with more than 20 kB, printed as one array in RouterOS script and uploaded. Right after a restart both
tables are nearly empty. A few hours later, with hundreds of customers, the DNS cache holds thousands of names and
the connection table tens of thousands of entries: the router spent most of its CPU and memory every two minutes
printing and uploading them, its own check-ins came late, TapTap marked it offline after 90 s, it came back, three
times in half an hour → "connection unstable". A restart emptied the tables and the cycle began again.

What this module does
---------------------
* ``note_checkin`` learns each router's own check-in rhythm (the gaps between check-ins, last 60). The online rule
  (``RouterAgent.grace_seconds``) allows a router up to 2.5× its usual slowest gap, never more than 5 minutes.
* ``heard`` — an answer to a command or an upload proves the router alive as much as a check-in does
  (``RouterAgent.last_heard``): a router busy uploading a big table is not "silent".
* ``sample_interval`` — apps & sites samples follow the router's load: every 2 minutes when it is relaxed, every 5
  when it is busy (CPU 60 %+, many customers, a slow last sample, late check-ins), every 15 when it struggles.
  The samples themselves are capped on the router (core/agent_inventory.py): the newest 600 DNS names and 400
  busiest connections, instead of the whole tables.
* ``summary`` — the Link page shows the rhythm: usual and slowest gap, CPU, how often samples run and why.
"""
from __future__ import annotations

from django.core.cache import cache
from django.utils import timezone

KEEP = 60                   # gaps remembered per router
TTL = 6 * 3600
MAX_GRACE = 300             # never more than 5 minutes of silence counts as online
TURBO_GAP = 4               # quick re-check-ins (turbo) say nothing about the rhythm
SAMPLE_FAST, SAMPLE_BUSY, SAMPLE_SLOW = 120, 300, 900


def _gk(agent_id):
    return f'tt:link:gaps:{agent_id}'


def note_checkin(agent, now=None, previous=None):
    """Remember the gap since the previous check-in (call before last_seen_at is overwritten)."""
    now = now or timezone.now()
    prev = previous if previous is not None else agent.last_seen_at
    if not prev:
        return
    gap = (now - prev).total_seconds()
    if gap < TURBO_GAP or gap > 3600:          # turbo, or the first check-in after a real outage
        return
    try:
        gaps = list(cache.get(_gk(agent.pk)) or [])
        gaps.append(round(gap, 1))
        cache.set(_gk(agent.pk), gaps[-KEEP:], TTL)
    except Exception:
        pass


def gaps(agent):
    try:
        return list(cache.get(_gk(agent.pk)) or [])
    except Exception:
        return []


def _p(values, q):
    v = sorted(values)
    return v[min(len(v) - 1, int(len(v) * q))] if v else None


def learned_grace(agent):
    """Seconds of silence this router's own rhythm allows (0 = not enough data yet)."""
    g = gaps(agent)
    if len(g) < 10:
        return 0
    return int(min(MAX_GRACE, _p(g, .9) * 2.5))


def heard(router_id, when=None):
    """The router answered something (a command, an upload): it is alive."""
    try:
        cache.set(f'tt:link:heard:{router_id}', (when or timezone.now()).isoformat(), TTL)
    except Exception:
        pass


def heard_at(router_id):
    try:
        v = cache.get(f'tt:link:heard:{router_id}')
        return timezone.datetime.fromisoformat(v) if v else None
    except Exception:
        return None


def note_sample(router_id, seconds):
    """How long the router took to send an apps & sites sample."""
    try:
        cache.set(f'tt:link:sample-took:{router_id}', round(float(seconds), 1), TTL)
    except Exception:
        pass


def sample_interval(agent):
    """(seconds, reason) between apps & sites samples for this router."""
    try:
        took = float(cache.get(f'tt:link:sample-took:{agent.router_id}') or 0)
    except Exception:
        took = 0
    cpu = int(getattr(agent, 'cpu_load', 0) or 0)
    users = int(getattr(agent, 'active_sessions', 0) or 0)
    g = gaps(agent)
    usual = _p(g, .5) if len(g) >= 10 else None
    poll = int(getattr(agent, 'poll_seconds', 10) or 10)
    late = bool(usual and usual > poll * 3)
    if cpu >= 85 or took >= 90 or (usual and usual > poll * 6):
        return SAMPLE_SLOW, (f'CPU {cpu} %' if cpu >= 85 else f'the last sample took {int(took)} s' if took >= 90
                             else 'check-ins are very late')
    if cpu >= 60 or took >= 30 or users >= 250 or late:
        return SAMPLE_BUSY, (f'CPU {cpu} %' if cpu >= 60 else f'the last sample took {int(took)} s' if took >= 30
                             else f'{users} customers online' if users >= 250 else 'check-ins are late')
    return SAMPLE_FAST, 'the router is relaxed'


def summary(agent):
    """For the Link page: the router's rhythm in plain words."""
    g = gaps(agent)
    every, why = sample_interval(agent)
    out = {'samples': len(g), 'usual': _p(g, .5), 'slow': _p(g, .9), 'worst': max(g) if g else None,
           'grace': agent.grace_seconds, 'cpu': getattr(agent, 'cpu_load', None), 'sample_every': every, 'sample_why': why}
    try:
        out['sample_took'] = cache.get(f'tt:link:sample-took:{agent.router_id}')
    except Exception:
        out['sample_took'] = None
    return out


def late_by(agent, now=None):
    """Seconds past the router's usual rhythm (0 when on time) — for 'late, not offline' wording."""
    now = now or timezone.now()
    heard_ = agent.last_heard
    if not heard_:
        return 0
    usual = _p(gaps(agent), .5) or (agent.poll_seconds or 10)
    return max(0, int((now - heard_).total_seconds() - usual))

