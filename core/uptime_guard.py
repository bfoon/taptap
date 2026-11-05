"""When is a router "offline" worth an email? — hysteresis, flap control and TapTap-side incidents.

What people saw
---------------
"It keeps saying the internet is down and back, and emails me — as if the internet goes off and on every few
seconds." Every short blip became an alert:

* a Direct API router was marked offline on ONE failed connection (one slow answer in a 15 s pass) and back online
  on the next one — an offline/online pair of events every few seconds, and emails every 15 minutes;
* a TapTap Link router was marked offline after 90 s without a check-in, and the router counted check-ins as
  "missed" that never were (a customer logging out while it listed sessions, TapTap answering slowly at busy
  hours, a server error) — each "outage" came back with a "could not reach TapTap … internet link was down" email;
* when TapTap itself was slow or restarting, every router looked offline at once.

What this module does
---------------------
* ``still_down()`` is called on every check that finds a router unreachable. The owner is emailed only once the
  router has been unreachable for ``ROUTER_OFFLINE_ALERT_MINUTES`` (default 3) — a blip never emails.
* ``came_back()`` sends "back online" only after an "offline" email went out, with how long it was down and why.
* Flapping: three or more drops within 30 minutes send ONE "connection unstable" email (how many, the longest,
  the likely cause) and hold back further offline/online emails for an hour. The dashboard still shows every change.
* ``taptap_side_incident()``: when nearly every TapTap Link router goes silent at the same moment, the problem is
  TapTap's server or its connection (a restart, an outage), not the routers: nobody is marked offline or emailed.
* ``history()`` keeps the last 30 changes per router for the Link page and ``manage.py connection_history``.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('taptap.uptime')

FLAP_WINDOW = timedelta(minutes=30)
FLAP_DROPS = 3
QUIET = 60 * 60
HISTORY = 30


def alert_minutes():
    return max(0, int(getattr(settings, 'ROUTER_OFFLINE_ALERT_MINUTES', 3)))


def _k(router_id, what):
    return f'tt:up:{what}:{router_id}'


def _iso(dt):
    return dt.isoformat() if dt else ''


def _dt(s):
    try:
        return timezone.datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def took(secs):
    secs = int(max(0, secs))
    if secs < 90:
        return f'{secs} s'
    mins = secs // 60
    return f'{mins // 60} h {mins % 60} min' if mins >= 60 else f'{mins} min'


def history(router_id):
    return list(cache.get(_k(router_id, 'hist')) or [])


def _remember(router_id, **entry):
    h = history(router_id)
    h.append({'at': _iso(timezone.now()), **entry})
    cache.set(_k(router_id, 'hist'), h[-HISTORY:], 30 * 86400)


def down_since(router_id):
    d = cache.get(_k(router_id, 'down')) or {}
    return _dt(d.get('since'))


def went_down(router, since=None, why=''):
    """The router was just marked offline (status changed). Remember when it really stopped answering."""
    since = since or timezone.now()
    d = cache.get(_k(router.pk, 'down'))
    if not d:
        cache.set(_k(router.pk, 'down'), {'since': _iso(since), 'why': str(why)[:300], 'alerted': False}, 7 * 86400)
        _remember(router.pk, event='down', why=str(why)[:160])


def still_down(router, since=None, why='', via=''):
    """Called on every check that finds the router unreachable: emails once it has been down long enough."""
    went_down(router, since, why)
    d = cache.get(_k(router.pk, 'down')) or {}
    start = _dt(d.get('since')) or timezone.now()
    if d.get('alerted') or timezone.now() - start < timedelta(minutes=alert_minutes()):
        return False
    from .router_schedules import expected_down
    if expected_down(router.pk):                  # its own scheduled restart / shutdown: the schedule reports on it
        return False
    d['alerted'] = True
    cache.set(_k(router.pk, 'down'), d, 7 * 86400)
    if cache.get(_k(router.pk, 'quiet')):
        logger.info('%s offline for %s — email held back (connection unstable)', router, took((timezone.now() - start).total_seconds()))
        return False
    from .notify import notify
    since_txt = timezone.localtime(start).strftime('%H:%M')
    detail = (f'TapTap has not been able to reach {router.name} since {since_txt} '
              f'({took((timezone.now() - start).total_seconds())}).')
    if via == 'TapTap Link':
        detail += ' It has stopped checking in through TapTap Link. Check its power and its internet connection.'
    elif why:
        detail += f' Last error: {str(why)[:200]}'
    notify(router.business, 'router_offline', f'{router.name} is offline', detail, severity='critical', link='/routers/',
           key=f'router:{router.pk}:offline')
    return True


def came_back(router, via='', detail=''):
    """The router answers again. Returns True when an email (back online / unstable) was sent."""
    d = cache.get(_k(router.pk, 'down'))
    cache.delete(_k(router.pk, 'down'))
    if not d:
        return False
    start = _dt(d.get('since')) or timezone.now()
    secs = (timezone.now() - start).total_seconds()
    _remember(router.pk, event='up', secs=int(secs), via=via, why=str(d.get('why', ''))[:160])
    from .notify import notify
    # flapping: several drops in a short time → one summary, then quiet for an hour
    now = timezone.now()
    drops = [e for e in history(router.pk) if e.get('event') == 'up' and _dt(e.get('at')) and now - _dt(e['at']) <= FLAP_WINDOW]
    if len(drops) >= FLAP_DROPS and not cache.get(_k(router.pk, 'quiet')):
        cache.set(_k(router.pk, 'quiet'), 1, QUIET)
        longest = max(int(e.get('secs') or 0) for e in drops)
        reasons = sorted({e.get('why') for e in drops if e.get('why')})
        notify(router.business, 'router_unstable', f'{router.name}: connection unstable',
               f'{router.name} dropped {len(drops)} times in the last 30 minutes (longest {took(longest)}). '
               + (f'Last reported cause: {reasons[-1]}. ' if reasons else '')
               + 'TapTap keeps watching but will not email each drop for the next hour. If the internet line is fine, '
                 'see Routers › TapTap Link › Connection history.',
               severity='warning', link='/routers/', key=f'router:{router.pk}:unstable')
        return True
    if d.get('alerted') and not cache.get(_k(router.pk, 'quiet')):
        notify(router.business, 'router_online', f'{router.name} is back online',
               f'{router.name} answers again{" through " + via if via else ""} after {took(secs)}.' + (f' {detail}' if detail else ''),
               link='/routers/', key=f'router:{router.pk}:online')
        return True
    return False


def quiet(router_id):
    """True during the hour after a "connection unstable" email (no per-drop emails then)."""
    return bool(cache.get(_k(router_id, 'quiet')))


def taptap_side_incident(silent, total):
    """Nearly every Link router silent at once (80 %+, at least 3) = TapTap's server or connection — a restart, an
    outage — not the routers. (Several routers sharing one ISP can go down together for real: that stays below 80 %
    on any installation with more than a handful of routers, and is reported as usual.)"""
    if total >= 3 and silent >= max(3, total * 0.8):
        if cache.add('tt:up:incident', 1, 300):
            logger.warning('TapTap Link: %s of %s routers stopped checking in at the same time — treating it as a TapTap-side '
                           'problem (server, network or restart); no router is marked offline.', silent, total)
        return True
    return False
