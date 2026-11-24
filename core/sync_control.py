"""Sync control — the owner decides how and when TapTap reads a router and sends it new vouchers.

What people saw
---------------
"Sometimes the router is busy and the sync runs, and the router runs at full." A full sync prints and uploads every
RouterOS table (hotspot users, hosts, leases, firewall, routes…). TapTap started one every 30 minutes, whatever the
router was doing, and sent the tables as fast as each check-in could carry them; a batch of new vouchers went out at
once too. On a router already at 90 % CPU in the evening that meant late check-ins, "offline" alerts, and customers
feeling it.

What the owner controls (one RouterSyncPolicy per router, core/models_sync.py)
---------------------------------------------------------------------------
* Full sync         Automatic (every N minutes) · Only in quiet hours · Only when I press Sync.
* Busy guard        automatic syncs wait while the router's CPU is at or above a level. A sync that has waited too
                    long (OVERDUE) runs anyway, gently, so TapTap's copy never goes stale for good.
* Pace              Normal, or Gentle: the router prints one table at a time, the next only after it delivered the
                    last. TapTap also goes gentle by itself while the router is busy.
* New voucher batches  At once · In quiet hours · When I press Send. A waiting voucher ("Held") is still sent the
                    moment a customer types it at the login page (core/voucher_ready.py), so nobody is turned away.
                    Single vouchers, online sales and rewards always go at once — a customer is waiting for those.
* Apps & sites samples  Follow the router's load · Only in quiet hours · Off.
* Quiet hours       any window (it may cross midnight). "Use my quietest hours" picks the 4 hours with the least
                    traffic over the last two weeks.

Pressing Sync, "Send now", a voucher change (switch off, freeze, extend…), online sales and the login guarantee
are never held back: only background work that can wait does.

A router without a saved policy keeps the old behaviour: TapTap Link routers sync by themselves every
LINK_SYNC_MINUTES, direct API routers when someone presses Sync.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('taptap.sync_control')

HELD = 'Held'                       # Voucher.mikrotik_sync_status of a new voucher waiting for its moment
EVERY_CHOICES = (15, 30, 60, 120, 240, 360, 720, 1440)
OVERDUE_HOURS = 6                   # an automatic sync that waited this long for a quiet router runs anyway, gently
RELEASE_BATCH = 500                 # held vouchers set free per router per housekeeping pass
BUSY_GRACE = 300                    # seconds of silence allowed while the router is printing a sync table
QUIET_HOURS = 4                     # length of the suggested quiet window


# ─────────────────────────────── the policy ───────────────────────────────

def _default_every():
    return int(getattr(settings, 'LINK_SYNC_MINUTES', 30) or 30)


def _checks_in(router):
    return getattr(router, 'connection_mode', '') == 'agent'


def plan_allows(router):
    """Is Sync control part of this business's plan (core/features.py "schedules")? Without it the saved choices
    rest and TapTap's defaults apply — a business that moved to Free is never left on "manual"."""
    try:
        from .features import enabled
        return enabled(router.business, 'schedules')
    except Exception:
        return True


def policy(router):
    """The router's saved policy, or an unsaved one holding TapTap's defaults for this kind of router."""
    from .models_sync import RouterSyncPolicy
    p = RouterSyncPolicy.objects.filter(router=router).first()
    if p is not None and not plan_allows(router):
        p = None
    if p is None:
        p = RouterSyncPolicy(router=router, full_every=_default_every(),
                             full_mode='auto' if _checks_in(router) else 'manual')
    return p


def in_quiet(p, now=None):
    """Is it quiet hours for this policy right now (local time)? A window may cross midnight."""
    t = timezone.localtime(now or timezone.now()).time()
    a, b = p.quiet_from, p.quiet_to
    if a == b:
        return True                                   # a 24-hour window
    return a <= t < b if a < b else (t >= a or t < b)


def next_quiet(p, now=None):
    """When the next quiet window starts (now, if it is quiet now)."""
    now = now or timezone.now()
    if in_quiet(p, now):
        return now
    local = timezone.localtime(now)
    start = local.replace(hour=p.quiet_from.hour, minute=p.quiet_from.minute, second=0, microsecond=0)
    if start <= local:
        start += timedelta(days=1)
    return start


def window_text(p):
    return f'{p.quiet_from:%H:%M}–{p.quiet_to:%H:%M}'


# ─────────────────────────────── is the router busy? ───────────────────────────────

def cpu_now(router):
    """The router's CPU % from its latest TapTap Link check-in (None when unknown or older than 10 minutes)."""
    try:
        agent = router.agent
    except Exception:
        return None
    if agent is None or agent.cpu_load is None or not agent.last_seen_at:
        return None
    if (timezone.now() - agent.last_seen_at).total_seconds() > 600:
        return None
    return int(agent.cpu_load)


def busy(router, p=None):
    """(busy?, why) — only the CPU reading counts; a router TapTap has no reading for is never called busy."""
    p = p or policy(router)
    if not p.busy_guard:
        return False, ''
    cpu = cpu_now(router)
    if cpu is not None and cpu >= p.busy_cpu:
        return True, f'the router is busy (CPU {cpu} %, the limit is {p.busy_cpu} %)'
    return False, ''


# ─────────────────────────────── automatic full syncs ───────────────────────────────

@dataclass
class Decision:
    ok: bool
    why: str
    gentle: bool = False


def _last_ok(router):
    return (router.sync_jobs.filter(status='success').order_by('-finished_at')
            .values_list('finished_at', flat=True).first())


def _remember(router, d, now):
    try:
        cache.set(f'tt:sync:decision:{router.pk}', {'at': now.isoformat(), 'ok': d.ok, 'why': d.why, 'gentle': d.gentle}, 3600)
    except Exception:
        pass


def last_decision(router):
    try:
        return cache.get(f'tt:sync:decision:{router.pk}')
    except Exception:
        return None


def _interval(router, p):
    """Minutes between automatic syncs: the owner's choice, spread further while syncs find nothing to repair."""
    every = max(15, int(p.full_every or _default_every()))
    if p.full_mode == 'auto' and _checks_in(router):
        try:
            from .link_breath import sync_minutes
            return sync_minutes(router, every)[0]
        except Exception:
            logger.debug('sync spreading for %s', router, exc_info=True)
    return every


def decide(router, now=None, first=False, retry=False):
    """May TapTap start an AUTOMATIC full sync of this router now?

    ``first``: the router's very first check-in (TapTap knows nothing about it yet) — always.
    ``retry``: the last sync failed and an early retry is wanted — the interval is not waited for, everything else is."""
    now = now or timezone.now()
    if first:
        d = Decision(True, 'the first check-in of this router')
        _remember(router, d, now)
        return d
    p = policy(router)
    if p.full_mode == 'manual':
        d = Decision(False, 'full sync is manual — it runs when you press Sync')
    elif p.full_mode == 'quiet' and not in_quiet(p, now):
        d = Decision(False, f'waits for quiet hours ({window_text(p)})')
    elif router.sync_jobs.filter(status__in=('queued', 'running')).exists():
        d = Decision(False, 'a sync is running')
    else:
        last = _last_ok(router)
        every = _interval(router, p)
        overdue = not last or now - last >= timedelta(hours=max(OVERDUE_HOURS, every * 4 / 60))
        if not retry and last and now - last < timedelta(minutes=every):
            due = timezone.localtime(last + timedelta(minutes=every))
            d = Decision(False, f'next one at about {due:%H:%M} (every {every} min)')
        else:
            is_busy, why = busy(router, p)
            if is_busy and not overdue:
                d = Decision(False, f'waits: {why}')
            elif is_busy:
                d = Decision(True, f'overdue — running gently although {why}', gentle=True)
            else:
                d = Decision(True, 'retrying the failed sync' if retry else 'due', gentle=p.pace == 'gentle')
    _remember(router, d, now)
    return d


def start_if_due(router, first=False, retry=False):
    """Queue an automatic full sync when the policy allows it now. True when one was queued."""
    d = decide(router, first=first, retry=retry)
    if not d.ok:
        return False
    from .tasks import enqueue_router_sync
    if d.gentle:
        try:
            cache.set(f'tt:sync:gentle:{router.pk}', 1, 4 * 3600)
        except Exception:
            pass
    _job, created = enqueue_router_sync(router)
    return created


# ─────────────────────────────── gentle pace (TapTap Link) ───────────────────────────────

def gentle(router):
    """Should this check-in carry at most one sync table, and only after the last one was delivered?"""
    p = policy(router)
    if p.pace == 'gentle':
        return True
    if busy(router, p)[0]:
        return True
    try:
        return bool(cache.get(f'tt:sync:gentle:{router.pk}'))
    except Exception:
        return False


def note_piece_sent(router_id):
    """A sync table went to the router: while it prints and uploads it, its check-ins may come late."""
    try:
        cache.set(f'tt:sync:printing:{router_id}', 1, BUSY_GRACE)
    except Exception:
        pass


def busy_grace(router_id):
    """Seconds of silence still counted as online right now (RouterAgent.grace_seconds): a router printing a
    big sync table is late, not offline — the "connection unstable" alerts during syncs came from here."""
    try:
        return BUSY_GRACE if cache.get(f'tt:sync:printing:{router_id}') else 0
    except Exception:
        return 0


def job_done(router):
    """A full sync finished: forget the gentle flag it was started with."""
    try:
        cache.delete(f'tt:sync:gentle:{router.pk}')
    except Exception:
        pass


# ─────────────────────────────── apps & sites samples ───────────────────────────────

def samples_allowed(router):
    p = policy(router)
    if p.samples_mode == 'off':
        return False
    if p.samples_mode == 'quiet':
        return in_quiet(p)
    return True


# ─────────────────────────────── new voucher batches ───────────────────────────────

def holds_batches(router, now=None):
    """Do new voucher batches for this router wait instead of going at once?"""
    if router is None:
        return False
    p = policy(router)
    if p.voucher_mode == 'manual':
        return True
    return p.voucher_mode == 'quiet' and not in_quiet(p, now)


def hold_text(router):
    p = policy(router)
    if p.voucher_mode == 'manual':
        when = 'until you press “Send waiting vouchers” (Routers › Sync control)'
    else:
        when = f'until quiet hours ({timezone.localtime(next_quiet(p)):%H:%M})'
    return (f'They wait {when}. A customer who types one earlier still gets in: TapTap sends that voucher to '
            f'{router.name} the moment it is typed at the login page.')


def hold(vouchers_qs):
    """Mark freshly made vouchers as waiting (only those the router has never had)."""
    return vouchers_qs.filter(source='taptap', mikrotik_id='').exclude(mikrotik_sync_status__in=('Synced', 'Queued')) \
        .exclude(login_type='member').update(mikrotik_sync_status=HELD, mikrotik_sync_error='')


def held_count(router):
    return router.vouchers.filter(mikrotik_sync_status=HELD, deleted_at__isnull=True).exclude(status__in=('expired', 'archived')).count()


def release(router, limit=None):
    """Waiting vouchers go: they become Pending and leave with the normal voucher path (TapTap Link: a batch per
    check-in, newest first; direct API: the background sender). Returns how many were set free."""
    from .models import Voucher
    qs = router.vouchers.filter(mikrotik_sync_status=HELD)
    ids = list(qs.order_by('-pk').values_list('pk', flat=True)[:limit] if limit else qs.values_list('pk', flat=True))
    if not ids:
        return 0
    n = Voucher.objects.filter(pk__in=ids, mikrotik_sync_status=HELD).update(mikrotik_sync_status='Pending', mikrotik_sync_error='')
    if n and _checks_in(router):
        try:
            from .agent import push_pending_vouchers
            push_pending_vouchers(router)
        except Exception:
            logger.debug('push released vouchers on %s', router, exc_info=True)
    return n


# ─────────────────────────────── housekeeping ───────────────────────────────

def tick(now=None):
    """Every housekeeping pass: quiet hours set waiting vouchers free, and direct API routers whose owner chose
    automatic syncs get them (TapTap Link routers are handled at their check-ins)."""
    from .models_sync import RouterSyncPolicy
    now = now or timezone.now()
    done = {'released': 0, 'synced': 0}
    for p in RouterSyncPolicy.objects.select_related('router', 'router__business').all():
        r = p.router
        try:
            if not r.business.has_access:
                continue
        except Exception:
            continue
        if not plan_allows(r):              # the plan no longer has Sync control: nothing waits any more
            try:
                done['released'] += release(r, RELEASE_BATCH)
            except Exception:
                logger.exception('release held vouchers on %s', r)
            continue
        if p.voucher_mode == 'quiet' and in_quiet(p, now):
            try:
                done['released'] += release(r, RELEASE_BATCH)
            except Exception:
                logger.exception('release held vouchers on %s', r)
        if not _checks_in(r) and p.full_mode != 'manual' and cache.add(f'tt:sync:api-tick:{r.pk}', 1, 60):
            try:
                done['synced'] += int(start_if_due(r))
            except Exception:
                logger.exception('automatic sync of %s', r)
    return done


# ─────────────────────────────── the quietest hours ───────────────────────────────

def hourly_load(router, days=14):
    """Traffic of this router per local hour of the day (24 numbers, bytes) over the last ``days`` days."""
    from .models import DeviceAppUsage
    since = timezone.now() - timedelta(days=days)
    out = [0] * 24
    rows = DeviceAppUsage.objects.filter(router=router, hour__gte=since).values_list('hour', 'download', 'upload')
    for hour, down, up in rows.iterator(chunk_size=5000):
        out[timezone.localtime(hour).hour] += int(down or 0) + int(up or 0)
    return out


def quietest(load, hours=QUIET_HOURS):
    """The start hour of the ``hours``-long window with the least traffic (wrapping midnight), or None without data."""
    if not any(load):
        return None
    best, start = None, None
    for h in range(24):
        total = sum(load[(h + i) % 24] for i in range(hours))
        if best is None or total < best:
            best, start = total, h
    return start


# ─────────────────────────────── what the page shows ───────────────────────────────

def overview(router):
    """Everything the Sync control page needs about one router."""
    p = policy(router)
    now = timezone.now()
    jobs = list(router.sync_jobs.order_by('-created_at')[:12])
    timeline = []
    for j in jobs:
        took = (j.finished_at - (j.started_at or j.created_at)).total_seconds() if j.finished_at else None
        doctor = (j.summary or {}).get('doctor') if isinstance(j.summary, dict) else None
        timeline.append({'at': timezone.localtime(j.created_at), 'status': j.status, 'by': 'you' if j.requested_by_id else 'automatic',
                         'who': (j.requested_by.get_full_name() or j.requested_by.username) if j.requested_by_id and j.requested_by else '',
                         'took': int(took) if took is not None else None, 'progress': j.progress, 'phase': j.phase,
                         'healed': (doctor or {}).get('healed') if isinstance(doctor, dict) else None,
                         'hour': timezone.localtime(j.created_at).hour + timezone.localtime(j.created_at).minute / 60})
    load = hourly_load(router)
    start = quietest(load)
    d = last_decision(router)
    if d is None or not _checks_in(router):
        dd = decide(router, now)
        d = {'ok': dd.ok, 'why': dd.why, 'gentle': dd.gentle}
    is_busy, busy_why = busy(router, p)
    return {
        'policy': p, 'saved': bool(p.pk), 'checks_in': _checks_in(router), 'cpu': cpu_now(router),
        'busy': is_busy, 'busy_why': busy_why, 'quiet_now': in_quiet(p, now), 'window': window_text(p),
        'decision': d, 'timeline': timeline, 'held': held_count(router), 'load': load,
        'load_max': max(load) or 1, 'suggest': start, 'suggest_to': (start + QUIET_HOURS) % 24 if start is not None else None,
        'now_hour': timezone.localtime(now).hour + timezone.localtime(now).minute / 60,
        'running': next((t for t in timeline if t['status'] in ('queued', 'running')), None),
    }


def card_summaries(routers):
    """{router_id: {...}} for the router cards on the MikroTik Control page — two queries for all routers."""
    from django.db.models import Count
    from .models import Voucher
    from .models_sync import RouterSyncPolicy
    ids = [r.pk for r in routers]
    saved = {p.router_id: p for p in RouterSyncPolicy.objects.filter(router_id__in=ids)}
    held = dict(Voucher.objects.filter(router_id__in=ids, mikrotik_sync_status=HELD, deleted_at__isnull=True)
                .exclude(status__in=('expired', 'archived')).values('router_id').annotate(n=Count('id')).values_list('router_id', 'n'))
    out = {}
    for r in routers:
        p = saved.get(r.pk) if plan_allows(r) else None
        if p is None:
            p = RouterSyncPolicy(router=r, full_every=_default_every(), full_mode='auto' if _checks_in(r) else 'manual')
        every = int(p.full_every or _default_every())
        span = f'{every} min' if every < 60 else (f'{every // 60} h' if every < 1440 else 'day')
        text = {'auto': f'Auto · every {span}', 'quiet': f'Quiet hours · {window_text(p)}', 'manual': 'Manual'}[p.full_mode]
        out[r.pk] = {'mode': p.full_mode, 'text': text, 'held': held.get(r.pk, 0), 'gentle': p.pace == 'gentle',
                     'saved': bool(p.pk), 'vouchers': p.voucher_mode}
    return out


# ─────────────────────────────── saving ───────────────────────────────

def _time(value, default):
    try:
        return datetime.strptime(str(value or '').strip()[:5], '%H:%M').time()
    except ValueError:
        return default


def save(router, data, user=None):
    """Save one router's policy from the form. Returns (policy, messages)."""
    from .models_sync import RouterSyncPolicy
    p = policy(router)
    if p.pk is None:
        p = RouterSyncPolicy(router=router)
    before_voucher = p.voucher_mode if p.pk else 'instant'
    choice = lambda key, options, default: data.get(key) if data.get(key) in options else default
    p.full_mode = choice('full_mode', dict(RouterSyncPolicy.FULL_MODES), p.full_mode)
    try:
        every = int(data.get('full_every') or p.full_every)
    except (TypeError, ValueError):
        every = p.full_every
    p.full_every = min(EVERY_CHOICES, key=lambda c: abs(c - every))
    p.quiet_from = _time(data.get('quiet_from'), p.quiet_from or time(1, 0))
    p.quiet_to = _time(data.get('quiet_to'), p.quiet_to or time(5, 0))
    p.busy_guard = str(data.get('busy_guard', '')).lower() in ('1', 'on', 'true', 'yes')
    try:
        p.busy_cpu = max(40, min(95, int(data.get('busy_cpu') or p.busy_cpu)))
    except (TypeError, ValueError):
        pass
    p.pace = choice('pace', dict(RouterSyncPolicy.PACES), p.pace)
    p.voucher_mode = choice('voucher_mode', dict(RouterSyncPolicy.VOUCHER_MODES), p.voucher_mode)
    p.samples_mode = choice('samples_mode', dict(RouterSyncPolicy.SAMPLE_MODES), p.samples_mode)
    p.updated_by = user if getattr(user, 'is_authenticated', False) else None
    p.save()
    notes = []
    if p.voucher_mode == 'instant' and before_voucher != 'instant':
        n = release(router)
        if n:
            notes.append(f'{n} waiting voucher(s) are on their way to {router.name}.')
    return p, notes


def copy_to(source, routers, user=None):
    """The same choices on other routers of the business."""
    fields = ('full_mode', 'full_every', 'quiet_from', 'quiet_to', 'busy_guard', 'busy_cpu', 'pace', 'voucher_mode', 'samples_mode')
    data = {f: getattr(source, f) for f in fields}
    data['quiet_from'], data['quiet_to'] = f'{source.quiet_from:%H:%M}', f'{source.quiet_to:%H:%M}'
    data['busy_guard'] = 'on' if source.busy_guard else ''
    out = 0
    for r in routers:
        if r.pk != source.router_id:
            save(r, data, user)
            out += 1
    return out
