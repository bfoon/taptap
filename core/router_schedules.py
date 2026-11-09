"""Scheduled router restarts and shutdowns — on the website (/routers/schedules/) and in the app.

    once · every day · chosen weekdays · a day of the month, at a local time

How a due schedule runs (tick(), every minute from Celery beat):

    1. heads-up      `warn_minutes` before: a bell alert (website + phone push) and, if ticked, an email —
                     "Kunda Mast restarts at 03:00 … tap to skip this time".
    2. quiet moment  from the time it is due it may wait up to `window_minutes` for the moment with the fewest
                     people online: it goes at once when nobody is online; after two thirds of the window it takes
                     the quietest moment it has seen; at the end of the window it goes whatever happens.
                     (Everyone online drops for a minute or two, members too; their time is kept.)
    3. the command   the same reboot the Reboot buttons send (TapTap Link or Direct API), or /system shutdown.
    4. follow-up     restart: "back online after 1 min 40 s" — or, 20 minutes on, "not back yet: check its power
                     and internet". Shutdown: "switched off — someone must power it on", then "back on after 6 h"
                     the day it returns.

While a router is down because of its own schedule, TapTap's "router offline" alerts stay quiet for it
(expected_down()). A run that could not happen (TapTap itself was down past the window) is reported as missed.
"""
from __future__ import annotations

import calendar
import logging
import re
from datetime import date, datetime, time, timedelta

from django.core.cache import cache
from django.db import IntegrityError
from django.utils import timezone

logger = logging.getLogger('taptap.schedules')

LOCK_KEY = 'tt:rsched:tick'
DOWN_KEY = 'tt:rsched:down:{}'
RESTART_BACK_WITHIN = timedelta(minutes=20)       # after this, "not back yet"
FIRST_CONTACT_AFTER = timedelta(seconds=45)       # a check-in this long after the command means it is back
MISSED_GRACE = timedelta(minutes=10)              # TapTap was down: later than window + this = missed
RETRY_FOR = timedelta(minutes=8)                  # the router did not answer: keep trying this long after the window
LATE_OK = timedelta(minutes=15)                   # a Link router heard from this recently is late (busy), not gone
LINK_EXPIRY_MIN = 15                              # a queued restart waits this long for the router's next check-in
MAX_WARN = 120
MAX_WINDOW = 180
WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
PERM = 'network.manage'


class ScheduleError(ValueError):
    pass


# ─────────────────────────────── when ───────────────────────────────

def _aware(d: date, t: time) -> datetime:
    return timezone.make_aware(datetime.combine(d, t.replace(second=0, microsecond=0)), timezone.get_current_timezone())


def _month_day(year, month, day):
    return date(year, month, min(max(1, int(day or 1)), calendar.monthrange(year, month)[1]))


def next_after(s, after: datetime):
    """The first time this schedule is due strictly after `after` (None: a one-off already past)."""
    t = s.at_time
    local = timezone.localtime(after)
    today = local.date()
    if s.repeat == 'once':
        if not s.run_on:
            return None
        at = _aware(s.run_on, t)
        return at if at > after else None
    if s.repeat == 'daily':
        for i in range(0, 3):
            at = _aware(today + timedelta(days=i), t)
            if at > after:
                return at
        return None
    if s.repeat == 'weekly':
        days = {int(x) for x in (s.weekdays or []) if str(x).lstrip('-').isdigit() and 0 <= int(x) <= 6}
        if not days:
            return None
        for i in range(0, 8):
            d = today + timedelta(days=i)
            if d.weekday() in days:
                at = _aware(d, t)
                if at > after:
                    return at
        return None
    if s.repeat == 'monthly':
        y, m = today.year, today.month
        for _ in range(0, 3):
            at = _aware(_month_day(y, m, s.month_day), t)
            if at > after:
                return at
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return None
    return None


def describe(s) -> str:
    """'Every day at 03:00', 'Mon, Wed, Fri at 02:30', 'Once on 12 Nov at 23:00', 'Monthly on day 1 at 04:00'."""
    hm = s.at_time.strftime('%H:%M')
    if s.repeat == 'once':
        return f'Once on {s.run_on:%a %d %b %Y} at {hm}' if s.run_on else f'Once at {hm}'
    if s.repeat == 'daily':
        return f'Every day at {hm}'
    if s.repeat == 'weekly':
        days = sorted({int(x) for x in (s.weekdays or []) if str(x).isdigit() and 0 <= int(x) <= 6})
        if days == [0, 1, 2, 3, 4]:
            return f'Weekdays at {hm}'
        if days == [5, 6]:
            return f'Weekends at {hm}'
        return f'{", ".join(WEEKDAYS[d] for d in days)} at {hm}'
    if s.repeat == 'monthly':
        return f'Every month on day {s.month_day} at {hm}'
    return hm


def _took(secs) -> str:
    secs = int(max(0, secs or 0))
    if secs < 60:
        return f'{secs} s'
    if secs < 3600:
        m, s = divmod(secs, 60)
        return f'{m} min {s} s' if s and m < 10 else f'{m} min'
    h, rest = divmod(secs, 3600)
    if h < 48:
        return f'{h} h {rest // 60} min' if rest >= 60 else f'{h} h'
    return f'{h // 24} days'


def _hm(dt) -> str:
    return timezone.localtime(dt).strftime('%H:%M')


# ─────────────────────────────── what the router is doing ───────────────────────────────

def _agent(r):
    if r.connection_mode != 'agent':
        return None
    try:
        return r.agent
    except Exception:
        return None


def online_count(r):
    """People online on this router from what TapTap already knows (None = unknown)."""
    try:
        if r.connection_mode == 'agent':
            from .linklive import sessions
            return len(sessions(r))
        n = cache.get(f'tt:watch:active:{r.pk}')
        return int(n) if n is not None else None
    except Exception:
        return None


def is_online(r) -> bool:
    a = _agent(r)
    if r.connection_mode == 'agent':
        return bool(a and a.online)
    return (r.status or '') == 'Online'


def last_contact(r):
    try:
        from .router_pulse import seen
        return seen(r, _agent(r))
    except Exception:
        return r.last_watch_at


_UP = re.compile(r'(\d+)\s*([wdhms])')


def uptime_seconds(text):
    """RouterOS uptime ('1w2d03:04:05', '3d4h5m6s', '00:05:12') in seconds, or None."""
    t = str(text or '').strip()
    if not t:
        return None
    total = 0
    clock = re.search(r'(\d+):(\d{2}):(\d{2})$', t)
    if clock:
        total += int(clock.group(1)) * 3600 + int(clock.group(2)) * 60 + int(clock.group(3))
        t = t[:clock.start()]
    units = {'w': 604800, 'd': 86400, 'h': 3600, 'm': 60, 's': 1}
    found = _UP.findall(t)
    if not found and not clock:
        return None
    for n, u in found:
        total += int(n) * units[u]
    return total


def expected_down(router_id) -> bool:
    """True while a router is down because of its own restart / shutdown schedule (offline alerts stay quiet)."""
    try:
        return bool(cache.get(DOWN_KEY.format(router_id)))
    except Exception:
        return False


def _mark_down(r, action):
    try:
        cache.set(DOWN_KEY.format(r.pk), {'action': action, 'at': timezone.now().isoformat()},
                  int(RESTART_BACK_WITHIN.total_seconds()) if action == 'restart' else 14 * 86400)
    except Exception:
        pass


def _clear_down(r):
    try:
        cache.delete(DOWN_KEY.format(r.pk))
    except Exception:
        pass


# ─────────────────────────────── telling people ───────────────────────────────

def link_for(r) -> str:
    return f'/routers/schedules/?router={r.pk}'


def tell(r, title, body, level='info', email=True, key=None):
    """A bell alert (website + phones of the owner and everyone who manages routers) and, if wanted, an email."""
    b = r.business
    try:
        from .models_events import EventAlert
        EventAlert.objects.create(business=b, kind='router_schedule', level={'danger': 'danger', 'warning': 'warning'}.get(level, 'info'),
                                  title=title[:160], body=body[:400], link=link_for(r), sound=True, desktop=True)
    except Exception:
        logger.exception('schedule alert for %s', r)
    if email:
        try:
            from .notify import notify
            notify(b, 'router_schedule', title, body, severity={'danger': 'critical', 'warning': 'warning', 'good': 'good'}.get(level, 'info'),
                   link=link_for(r), key=key)
        except Exception:
            logger.exception('schedule email for %s', r)


# ─────────────────────────────── doing it ───────────────────────────────

def execute(r, action, user=None, label=''):
    """Send the restart / shutdown now. Returns (ok, message)."""
    from .utils import log
    what = 'Restart' if action == 'restart' else 'Shutdown'
    if r.connection_mode == 'agent':
        from . import agent as link
        a = _agent(r)
        if a is None or a.revoked:
            return False, f'{r.name} has no working TapTap Link.'
        heard = a.last_heard
        late = not a.online
        if late and not (heard and timezone.now() - heard < LATE_OK):
            return False, f'{r.name} is offline (no TapTap Link check-in since {_hm(heard) if heard else "never"}).'
        try:
            # It goes alone at the very next check-in (core/agent.build_response), and waits up to LINK_EXPIRY_MIN for
            # a busy router that checks in late — before, it expired after 5 minutes behind voucher changes.
            link.queue(r, 'reboot' if action == 'restart' else 'shutdown', label=label or f'{what} (schedule)', user=user,
                       minutes=LINK_EXPIRY_MIN)
            link.mark_urgent(r, 120)
        except ValueError as exc:
            return False, str(exc)[:300]
        log(r.business, 'Router Reboot', f'{r.name}: {what.lower()} sent through TapTap Link ({label or "schedule"})', router=r)
        if late:
            return True, f'{what} sent through TapTap Link — {r.name} is checking in late (busy), it runs at its next check-in.'
        return True, f'{what} sent through TapTap Link.'
    from django.conf import settings
    from .mikrotik import MikroTikService
    from . import portctl
    try:
        svc = MikroTikService(r, timeout=getattr(settings, 'MIKROTIK_TIMEOUT', 10)).connect()
    except Exception as exc:                       # could not even log in: a real failure (retried by process())
        return False, f'{r.name} did not answer: {exc}'[:300]
    try:
        (portctl.reboot if action == 'restart' else portctl.shutdown)(svc)
    except Exception as exc:
        # The command was delivered; a router going down drops the connection in many ways ("reset by peer",
        # "broken pipe", a timeout…). Before, anything but "closed" was reported as "did not answer" although the
        # router was already restarting.
        if not re.search(r'closed|reset|timed out|timeout|broken|eof|connection|pipe', str(exc), re.I):
            return False, f'{r.name} refused the {what.lower()}: {exc}'[:300]
    finally:
        try:
            svc.close()
        except Exception:
            pass
    if action == 'restart':
        type(r).objects.filter(pk=r.pk).update(status='Rebooting', last_tested_at=timezone.now())
    log(r.business, 'Router Reboot', f'{r.name}: {what.lower()} ({label or "schedule"})', router=r)
    return True, f'{what} sent.'


def _verb(action):
    return 'restarts' if action == 'restart' else 'switches off'


def _advance(s, planned, now):
    s.last_run_at = now
    s.next_at = next_after(s, max(planned, now))
    if s.skip_at and s.skip_at <= planned:
        s.skip_at = None
    if s.repeat == 'once':
        s.active = False
        s.next_at = None
    type(s).objects.filter(pk=s.pk).update(last_run_at=s.last_run_at, next_at=s.next_at, skip_at=s.skip_at, active=s.active)


def _run_for(s, planned, status):
    from .models_router_schedule import RouterScheduleRun
    run = RouterScheduleRun.objects.filter(schedule=s, planned_at=planned).first()
    if run is not None:
        return run, False
    try:
        return RouterScheduleRun.objects.create(schedule=s, router=s.router, action=s.action, planned_at=planned, status=status), True
    except IntegrityError:
        return RouterScheduleRun.objects.filter(schedule=s, planned_at=planned).first(), False


def _go(s, run, n, now, planned):
    """Run now? Nobody online, no window, the count is unknown, the end of the window, or — after two thirds of the
    window — the quietest moment seen so far."""
    if s.window_minutes <= 0 or n is None or n <= 0:
        return True
    end = planned + timedelta(minutes=s.window_minutes)
    if now >= end:
        return True
    seen = [x[1] for x in (run.online_seen or []) if isinstance(x, (list, tuple)) and len(x) == 2 and isinstance(x[1], int)]
    late = now >= planned + timedelta(minutes=s.window_minutes * 2 / 3)
    return late and bool(seen) and n <= min(seen)


def process(s, now=None):
    """One active schedule whose time (or heads-up) has come."""
    now = now or timezone.now()
    r = s.router
    planned = s.next_at
    if planned is None:
        return None
    if s.skip_at and s.skip_at == planned:
        if now >= planned:
            run, _ = _run_for(s, planned, 'skipped')
            if run and run.status in ('warned', 'waiting'):
                type(run).objects.filter(pk=run.pk).update(status='skipped', result=run.result or 'Skipped this time.')
            _advance(s, planned, now)
        return 'skipped'
    if now < planned:
        if s.warn_minutes and now >= planned - timedelta(minutes=s.warn_minutes):
            run, created = _run_for(s, planned, 'warned')
            if created:
                win = f' — TapTap waits up to {s.window_minutes} min for the moment with the fewest people online' if s.window_minutes else ''
                n = online_count(r)
                who = f' {n} {"person" if n == 1 else "people"} online now.' if n else ''
                off = ' It stays OFF until someone powers it on again.' if s.action == 'shutdown' else ' Everyone online drops for a minute or two.'
                tell(r, f'⏰ {r.name} {_verb(s.action)} at {_hm(planned)}',
                     f'{describe(s)}{win}.{off}{who} Open the schedule to skip this time.', level='warning', email=s.email,
                     key=f'rsched:{s.pk}:{planned:%Y%m%d%H%M}:warn')
                return 'warned'
        return None
    end = planned + timedelta(minutes=s.window_minutes)
    run, _ = _run_for(s, planned, 'waiting')
    if run is None:
        return None
    if run.status not in ('warned', 'waiting'):
        _advance(s, planned, now)                     # this one is done already
        return None
    if now > end + MISSED_GRACE:
        type(run).objects.filter(pk=run.pk).update(status='missed', result='TapTap could not run it in time (the server was not running).')
        tell(r, f'{r.name}: scheduled {s.get_action_display().lower()} missed',
             f'It was due at {_hm(planned)} but TapTap could not run it in time. The next one is as planned.', level='warning',
             email=s.email, key=f'rsched:{s.pk}:{planned:%Y%m%d%H%M}:missed')
        _advance(s, planned, now)
        return 'missed'
    n = online_count(r)
    seen = list(run.online_seen or [])[-200:]
    if n is not None:
        seen.append([int((now - planned).total_seconds() // 60), n])
    if not _go(s, run, n, now, planned):
        type(run).objects.filter(pk=run.pk).update(status='waiting', online_seen=seen)
        return 'waiting'
    ok, msg = execute(r, s.action, user=s.created_by, label=f'{describe(s)} (schedule)')
    waited = int((now - planned).total_seconds() // 60)
    if ok:
        _mark_down(r, s.action)
        type(run).objects.filter(pk=run.pk).update(status='sent', ran_at=now, online_at_run=n, online_seen=seen,
                                                   result=(f'Waited {waited} min for a quiet moment. ' if waited > 0 else '') + msg)
    elif now < end + RETRY_FOR:
        # The router did not answer this minute (a blip, a busy moment): keep trying every minute for a while
        # instead of giving up on the first attempt.
        m = re.search(r'\(attempt (\d+)\)$', run.result or '')
        tries = int(m.group(1)) + 1 if m else 1
        type(run).objects.filter(pk=run.pk).update(status='waiting', online_seen=seen,
                                                   result=f'{msg[:220]} — trying again every minute (attempt {tries})')
        return 'retry'
    else:
        type(run).objects.filter(pk=run.pk).update(status='failed', online_at_run=n, online_seen=seen,
                                                   result=f'{msg[:240]} (tried until {_hm(end + RETRY_FOR)})')
        tell(r, f'⚠️ {r.name}: scheduled {s.get_action_display().lower()} did not happen',
             f'{msg} TapTap tried every minute until {_hm(end + RETRY_FOR)}.', level='danger', email=s.email,
             key=f'rsched:{s.pk}:{planned:%Y%m%d%H%M}:failed')
    if ok or now >= end + RETRY_FOR:
        _advance(s, planned, now)
    return 'sent' if ok else 'failed'


def _command(run):
    """TapTap Link: the command this run queued (None for Direct API)."""
    if run.router.connection_mode != 'agent' or not run.ran_at:
        return None
    from .models import AgentCommand
    kind = 'reboot' if run.action == 'restart' else 'shutdown'
    return AgentCommand.objects.filter(router=run.router, kind=kind, created_at__gte=run.ran_at - timedelta(seconds=5)).order_by('created_at').first()


def follow(run, now=None):
    """After the command: is it back (restart), off (shutdown), back on (after a shutdown)?"""
    now = now or timezone.now()
    r = run.router
    s = run.schedule
    email = s.email if s else True
    ran = run.ran_at or run.planned_at
    contact = last_contact(r)
    cmd = _command(run) if run.status == 'sent' else None
    if r.connection_mode == 'agent' and run.status == 'sent':
        # the router confirms the command a moment before it goes down: only a check-in after that counts as "back"
        acked = cmd.done_at if (cmd and cmd.status == 'done' and cmd.done_at) else None
        fresh = bool(acked and contact and contact > acked + timedelta(seconds=20))
        if not fresh and run.action == 'restart' and contact and contact > ran + FIRST_CONTACT_AFTER:
            up = uptime_seconds(getattr(_agent(r), 'uptime', ''))     # its own word for it: started after the command
            fresh = up is not None and up < (now - ran).total_seconds()
    else:
        fresh = bool(contact and contact > ran + FIRST_CONTACT_AFTER)
    online = is_online(r)
    key = f'rsched:run:{run.pk}'
    Run = type(run)
    if run.status == 'sent' and cmd is not None and cmd.status in ('failed', 'expired', 'cancelled'):
        Run.objects.filter(pk=run.pk).update(status='failed', result=f'{r.name} never picked up the command (it was offline).')
        _clear_down(r)
        tell(r, f'⚠️ {r.name}: scheduled {"restart" if run.action == "restart" else "shutdown"} did not happen',
             f'{r.name} did not pick up the command through TapTap Link in time — it was probably offline.', level='danger',
             email=email, key=f'{key}:nopick')
        return 'failed'
    if run.action == 'restart' and run.status == 'sent':
        if online and fresh:
            up = uptime_seconds(getattr(_agent(r), 'uptime', '')) if r.connection_mode == 'agent' else None
            if up is not None and up > (now - ran).total_seconds() + 120:
                Run.objects.filter(pk=run.pk).update(status='failed', back_at=contact, result=f'{r.name} answered but did not restart (uptime {_took(up)}).')
                _clear_down(r)
                tell(r, f'⚠️ {r.name} did not restart', f'It is online, but its uptime ({_took(up)}) shows it did not restart at {_hm(ran)}.',
                     level='warning', email=email, key=f'{key}:norestart')
                return 'failed'
            down = int((contact - ran).total_seconds())
            Run.objects.filter(pk=run.pk).update(status='back', back_at=contact, down_seconds=down)
            _clear_down(r)
            who = f' {run.online_at_run} {"person was" if run.online_at_run == 1 else "people were"} online when it went.' if run.online_at_run else ' Nobody was online when it went.' if run.online_at_run == 0 else ''
            tell(r, f'✅ {r.name} restarted — back online', f'Restarted at {_hm(ran)} and back after {_took(down)}.{who}', level='good',
                 email=email, key=f'{key}:back')
            return 'back'
        if now - ran >= RESTART_BACK_WITHIN:
            Run.objects.filter(pk=run.pk).update(status='late', result=f'Not back {int(RESTART_BACK_WITHIN.total_seconds() // 60)} min after the restart.')
            tell(r, f'🚨 {r.name} is not back after its restart',
                 f'It restarted at {_hm(ran)} and has not come back for {_took((now - ran).total_seconds())}. Check its power and its internet line.',
                 level='danger', email=email, key=f'{key}:late')
            return 'late'
        return None
    if run.action == 'shutdown' and run.status == 'sent':
        acked_or_api = r.connection_mode != 'agent' or (cmd is not None and cmd.status == 'done')
        if acked_or_api and not online and now - ran >= timedelta(seconds=30):
            Run.objects.filter(pk=run.pk).update(status='off')
            tell(r, f'⏻ {r.name} is switched off', f'Switched off as scheduled at {_hm(ran)}. It stays off until someone unplugs it '
                 'and plugs it back in. TapTap tells you when it is back.', level='warning', email=email, key=f'{key}:off')
            return 'off'
        if online and fresh and now - ran >= timedelta(minutes=5):
            Run.objects.filter(pk=run.pk).update(status='failed', result=f'{r.name} is still on 5 min after the shutdown.')
            _clear_down(r)
            tell(r, f'⚠️ {r.name} did not switch off', f'The shutdown was sent at {_hm(ran)}, but it is still online.', level='warning',
                 email=email, key=f'{key}:stillon')
            return 'failed'
        return None
    if run.status in ('off', 'late') and online and fresh:
        down = int((contact - ran).total_seconds())
        Run.objects.filter(pk=run.pk).update(status='back', back_at=contact, down_seconds=down)
        _clear_down(r)
        tell(r, f'✅ {r.name} is back {"on" if run.action == "shutdown" else "online"}',
             f'Back after {_took(down)} ({"switched off" if run.action == "shutdown" else "restarted"} at {timezone.localtime(ran):%a %H:%M}).',
             level='good', email=email, key=f'{key}:backon')
        return 'back'
    return None


def tick(now=None):
    """Every minute (Celery beat): due schedules, then follow-ups. One at a time across workers."""
    from .models_router_schedule import RouterSchedule, RouterScheduleRun
    now = now or timezone.now()
    try:
        if not cache.add(LOCK_KEY, 1, 55):
            return {'skipped': 'another tick is running'}
    except Exception:
        pass
    out = {'due': 0, 'follow': 0}
    try:
        soon = now + timedelta(minutes=MAX_WARN)
        for s in RouterSchedule.objects.filter(active=True, next_at__isnull=False, next_at__lte=soon).select_related('router', 'router__business', 'created_by'):
            try:
                if process(s, now):
                    out['due'] += 1
            except Exception:
                logger.exception('router schedule %s', s.pk)
        for run in RouterScheduleRun.objects.filter(status__in=('sent', 'off', 'late'), ran_at__gte=now - timedelta(days=14)).select_related('router', 'router__business', 'schedule'):
            try:
                if follow(run, now):
                    out['follow'] += 1
            except Exception:
                logger.exception('router schedule run %s', run.pk)
    finally:
        try:
            cache.delete(LOCK_KEY)
        except Exception:
            pass
    return out


# ─────────────────────────────── create · change · skip (website and app) ───────────────────────────────

def _int(v, default, lo, hi):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def save(router, data, user, schedule=None):
    """Create or change a schedule from a form / JSON. Raises ScheduleError with a sentence for the person."""
    from .models_router_schedule import RouterSchedule
    s = schedule or RouterSchedule(router=router, created_by=user)
    action = str(data.get('action') or s.action or 'restart')
    if action not in ('restart', 'shutdown'):
        raise ScheduleError('Choose restart or shutdown.')
    repeat = str(data.get('repeat') or s.repeat or 'daily')
    if repeat not in ('once', 'daily', 'weekly', 'monthly'):
        raise ScheduleError('Choose once, every day, every week or every month.')
    raw_time = str(data.get('at_time') or data.get('time') or '').strip()
    m = re.fullmatch(r'(\d{1,2}):(\d{2})(?::\d{2})?', raw_time)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ScheduleError('Choose a time, e.g. 03:00.')
    s.at_time = time(int(m.group(1)), int(m.group(2)))
    s.action, s.repeat = action, repeat
    s.run_on = None
    s.weekdays = []
    if repeat == 'once':
        try:
            s.run_on = date.fromisoformat(str(data.get('run_on') or data.get('date') or '').strip())
        except ValueError:
            raise ScheduleError('Choose the day.')
    elif repeat == 'weekly':
        days = data.get('weekdays')
        if isinstance(days, str):
            days = [x for x in re.split(r'[,\s]+', days) if x]
        days = sorted({int(x) for x in (days or []) if str(x).isdigit() and 0 <= int(x) <= 6})
        if not days:
            raise ScheduleError('Choose at least one day of the week.')
        s.weekdays = days
    elif repeat == 'monthly':
        s.month_day = _int(data.get('month_day'), 1, 1, 31)
    s.window_minutes = _int(data.get('window_minutes'), 30, 0, MAX_WINDOW)
    s.warn_minutes = _int(data.get('warn_minutes'), 10, 0, MAX_WARN)
    e = data.get('email', True)
    s.email = e is True or str(e).lower() in ('1', 'true', 'on', 'yes')
    s.note = str(data.get('note') or '')[:160]
    if action == 'shutdown' and str(data.get('understood') or '').lower() not in ('1', 'true', 'on', 'yes'):
        raise ScheduleError('A shutdown switches the router OFF: it stays off until someone powers it on again. Tick that you understand.')
    s.active = True
    s.skip_at = None
    now = timezone.now()
    s.next_at = next_after(s, now)
    if s.next_at is None:
        raise ScheduleError('That moment has already passed — choose a time in the future.')
    s.save()
    return s


def skip_next(s, user):
    """Skip only the next time (the schedule goes on after it)."""
    if not s.active or not s.next_at:
        raise ScheduleError('Nothing is planned.')
    planned = s.next_at
    type(s).objects.filter(pk=s.pk).update(skip_at=planned)
    s.skip_at = planned
    from .models_router_schedule import RouterScheduleRun
    RouterScheduleRun.objects.filter(schedule=s, planned_at=planned, status__in=('warned', 'waiting')).update(
        status='skipped', by=user, result=f'Skipped by {getattr(user, "first_name", "") or getattr(user, "email", "someone")}.')
    return planned


def unskip(s):
    type(s).objects.filter(pk=s.pk).update(skip_at=None)
    s.skip_at = None


def set_active(s, on):
    s.active = bool(on)
    s.skip_at = None
    s.next_at = next_after(s, timezone.now()) if s.active else None
    if s.active and s.next_at is None:
        raise ScheduleError('That moment has already passed — change the time first.')
    type(s).objects.filter(pk=s.pk).update(active=s.active, next_at=s.next_at, skip_at=None)


def run_now(router, action, user):
    """Restart / shut down right now, with the same follow-up (back online / switched off) as a schedule."""
    from .models_router_schedule import RouterScheduleRun
    now = timezone.now()
    n = online_count(router)
    ok, msg = execute(router, action, user=user, label=f'{"Restart" if action == "restart" else "Shutdown"} now')
    run = RouterScheduleRun.objects.create(schedule=None, router=router, action=action, planned_at=now, by=user,
                                           status='sent' if ok else 'failed', ran_at=now if ok else None, online_at_run=n,
                                           result=msg[:300])
    if ok:
        _mark_down(router, action)
    return ok, msg, run


def as_json(s, now=None):
    now = now or timezone.now()
    nxt = s.next_at
    return {
        'id': s.pk, 'router': s.router_id, 'router_name': s.router.name, 'action': s.action, 'action_label': s.get_action_display(),
        'repeat': s.repeat, 'run_on': s.run_on.isoformat() if s.run_on else '', 'at_time': s.at_time.strftime('%H:%M'),
        'weekdays': s.weekdays or [], 'month_day': s.month_day, 'window_minutes': s.window_minutes, 'warn_minutes': s.warn_minutes,
        'email': s.email, 'note': s.note, 'active': s.active, 'text': describe(s),
        'next_at': timezone.localtime(nxt).isoformat() if nxt else None,
        'skipped': bool(s.skip_at and nxt and s.skip_at == nxt),
        'after_skip': timezone.localtime(next_after(s, nxt)).isoformat() if (s.skip_at and nxt and s.skip_at == nxt and next_after(s, nxt)) else None,
        'in_minutes': int((nxt - now).total_seconds() // 60) if nxt else None,
        'last_run_at': timezone.localtime(s.last_run_at).isoformat() if s.last_run_at else None,
    }


def run_json(run):
    def t(v):
        return timezone.localtime(v).isoformat() if v else None
    return {'id': run.pk, 'router': run.router_id, 'router_name': run.router.name, 'action': run.action, 'status': run.status,
            'status_label': run.get_status_display(), 'planned_at': t(run.planned_at), 'ran_at': t(run.ran_at),
            'back_at': t(run.back_at), 'down': _took(run.down_seconds) if run.down_seconds is not None else '',
            'online_at_run': run.online_at_run, 'result': run.result, 'schedule': run.schedule_id,
            'manual': run.schedule_id is None}
