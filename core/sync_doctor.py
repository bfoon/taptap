"""Sync doctor — every full sync ends by healing what differs between TapTap and the router.

What people saw
---------------
"I pressed Full sync, it said Completed, but the voucher is still not on the router / still Pending / still
switched off / still has the wrong time."

Why
---
A full sync READ the router and imported what it found. On TapTap Link routers it never compared the result with
TapTap's own vouchers, so anything TapTap believed was already on the router ("Synced") but was not — a router
reset or restored from a backup, a user deleted in WinBox, a batch the router cut short — stayed missing for good.
Each other kind of drift had its own little repair somewhere (profiles here, time limits there, expired ones in
the expiry pass), and none of them ran together, at the moment the router's picture was freshest.

What this module does
---------------------
When a full sync finishes (Direct API, Tunnel or TapTap Link, pressed by hand or automatic), ``schedule`` runs
``run`` in the worker. It takes the router's user list from that very sync — or, on Direct API routers, reads it
once more AFTER the sync has written, so it checks the final state — and compares it with every TapTap voucher
and member of the router. Each difference is sent back the way TapTap already sends it everywhere:

    missing      TapTap has it, the router does not          → sent again (members: exact member alignment)
    onoff        on/off differs (frozen, paused, disabled)   → TapTap's state sent
    profile      on another profile than its plan's           → sent with the plan's profile
    time         the time limit disagrees with TapTap's clock → sent with the right limit
    expired_on   time is up in TapTap, still on on the router → switched off (the expiry path, gates cleared)
    status       the router has it right, TapTap still says Pending / Error / lost in the queue → marked Synced
    profiles     a plan's HotSpot profile is missing         → plan profiles created
    stuck        "Queued" vouchers no Link command carries   → released and sent again
    gone         made on the router, no longer there         → flagged on the voucher (never deleted)

Safe by design
--------------
* TapTap is the source of truth only for its OWN vouchers and members; router-made users are never overwritten.
* Nothing is guessed from a picture that did not arrive: if the router's user list was not part of this sync,
  voucher checks are skipped and the report says why.
* Vouchers already on their way (in a Link batch the router has not answered yet) and vouchers made after the
  router was read are left alone.
* Codes the router refused 3 times are not retried by an automatic sync (a sync someone presses retries them,
  as before), so a broken code cannot loop.
* At most MAX_PUSH vouchers and MAX_MEMBERS members per run; the next sync continues.
* Sends use the voucher fast lane's de-duplication: the same state is never sent twice within its window.

The report is saved on the sync job (``summary['doctor']``) and shown on the Routers page: how much was in sync,
what was healed, and — live — how many of the healed vouchers the router has confirmed since.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('taptap.sync.doctor')

MAX_PUSH = 3000
MAX_MEMBERS = 300
MAX_IDS = 1000                      # healed voucher ids kept on the job for the live "confirmed" count
EXAMPLES = 12
RESET_SHARE = 0.5                   # more than half of TapTap's vouchers missing: the router was reset or restored
RESET_MIN = 20
RENAME_DAYS = 2                     # a code changed this recently is still being renamed on Link routers
CLASSES = ('missing', 'onoff', 'profile', 'time', 'expired_on', 'status', 'profiles', 'stuck', 'gone')
LABELS = {
    'missing': 'missing on the router — sent again',
    'onoff': 'switched on/off differently — TapTap’s state sent',
    'profile': 'on the wrong profile — moved to their plan’s profile',
    'time': 'wrong time limit — corrected',
    'expired_on': 'time up but still on — switched off',
    'status': 'were right on the router but still showed Pending/Error — marked Synced',
    'profiles': 'plan profiles missing on the router — created',
    'stuck': 'stuck in the Link queue — released and sent again',
    'gone': 'made on the router and no longer there — flagged',
}
SHORT = {'missing': 'missing', 'onoff': 'on/off', 'profile': 'profile', 'time': 'time limit', 'expired_on': 'expired still on',
         'status': 'stuck on Pending', 'profiles': 'plan profile', 'stuck': 'lost in queue', 'gone': 'gone from router'}
GONE_NOTE = 'Not on the router any more (deleted there?). Re-create it on the router, or delete it in TapTap.'


class Row:
    """One hotspot user as the router has it (the same attributes as RouterHotspotUser)."""
    __slots__ = ('username', 'profile', 'disabled', 'limit_uptime', 'uptime', 'is_present')

    def __init__(self, username, profile='', disabled=False, limit_uptime='', uptime=''):
        self.username, self.profile, self.disabled = username, profile, disabled
        self.limit_uptime, self.uptime, self.is_present = limit_uptime, uptime, True


# ─────────────────────────────── when ───────────────────────────────

def schedule(job):
    """Run the doctor for a finished full sync (once per job), in the worker when there is one."""
    if not job or not getattr(job, 'pk', None):
        return False
    if not cache.add(f'tt:doctor:job:{job.pk}', 1, 6 * 3600):
        return False
    try:
        from django.conf import settings
        if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
            raise RuntimeError('no worker')
        from .tasks import sync_doctor_task
        sync_doctor_task.apply_async((job.pk,), countdown=2)
    except Exception:
        try:
            run(job.pk)
        except Exception:
            logger.exception('sync doctor failed for job %s', job.pk)
    return True


# ─────────────────────────────── the router's picture ───────────────────────────────

def _link_picture(router, job):
    """(mirror, read_at, note) from this Link sync's hotspot users table; mirror None when it did not arrive."""
    from .agent_inventory import job_pieces
    from .models import RouterHotspotUser
    piece = next((c for c in job_pieces(router, job.pk) if (c.params or {}).get('kind') == 'hotspot_users'), None)
    if piece is None or not (piece.params or {}).get('received'):
        return None, None, 'The router’s user list did not arrive in this sync, so vouchers were not compared.'
    read_at = piece.done_at or job.started_at or job.created_at
    mirror = {u.username.upper(): u for u in RouterHotspotUser.objects.filter(router=router, is_present=True)
              .only('username', 'profile', 'disabled', 'limit_uptime', 'uptime', 'is_present')}
    return mirror, read_at, ''


def _api_picture(router):
    """(mirror, profiles, read_at, note) read from the router now — after the sync wrote to it."""
    from .mikrotik import MikroTikService
    from .mikrotik import ros_bool
    try:
        svc = MikroTikService(router).connect()
    except Exception as exc:  # noqa: BLE001
        return None, None, None, f'Could not read the router again to check ({exc}).'
    try:
        users = list(svc.resource('/ip/hotspot/user').get())
        try:
            profiles = {str(p.get('name', '')) for p in svc.hotspot_profiles()}
        except Exception:
            profiles = None
    except Exception as exc:  # noqa: BLE001
        return None, None, None, f'Could not read the router’s users to check ({exc}).'
    finally:
        try:
            svc.close()
        except Exception:
            pass
    mirror = {}
    for r in users:
        name = str(r.get('name', '')).strip()
        if name:
            mirror[name.upper()] = Row(name, str(r.get('profile', '') or ''), ros_bool(r.get('disabled', False)),
                                       str(r.get('limit-uptime', '') or ''), str(r.get('uptime', '') or ''))
    return mirror, profiles, timezone.now(), ''


# ─────────────────────────────── one voucher ───────────────────────────────

def diagnose(v, row, plans, now=None):
    """What differs for one live voucher (not expired/archived): a list of CLASSES keys, [] when in sync."""
    from .router_time import acceptable, platform_owned
    from .utils import voucher_profile
    from .voucher_fastlane import can_be_online
    if not platform_owned(v):
        if row is None:
            return ['gone'] if v.status == 'active' else []
        return ['status'] if v.mikrotik_sync_status != 'Synced' else []
    if row is None:
        return ['missing']
    out = []
    want_off = not can_be_online(v, now)
    if bool(row.disabled) != want_off:
        out.append('onoff')
    if not v.is_member and not want_off:                # members: their own exact alignment keeps profile and clock
        plan = plans.get((v.plan_name or '').lower())
        try:
            prof = voucher_profile(v, plan)[0]
            if prof and row.profile and row.profile.lower() != prof.lower():
                out.append('profile')
        except Exception:
            pass
        try:
            if not acceptable(v, row.limit_uptime, row.uptime, plan, now):
                out.append('time')
        except Exception:
            pass
    if not out and v.mikrotik_sync_status != 'Synced':
        out.append('status')
    return out


# ─────────────────────────────── the run ───────────────────────────────

def _save(job_id, doctor):
    from .models import RouterSyncJob
    job = RouterSyncJob.objects.filter(pk=job_id).first()
    if not job:
        return
    summary = dict(job.summary or {})
    summary['doctor'] = doctor
    RouterSyncJob.objects.filter(pk=job_id).update(summary=summary)


def run(job_id):
    """Compare and heal one router after its full sync. Returns the report (also saved on the job)."""
    from .models import RouterSyncJob
    job = RouterSyncJob.objects.select_related('router', 'router__business').filter(pk=job_id).first()
    if not job or job.status != 'success' or not job.router_id:
        return None
    _save(job.pk, {'state': 'running', 'at': timezone.now().isoformat()})
    try:
        report = heal(job.router, job)
    except Exception as exc:  # noqa: BLE001
        logger.exception('sync doctor %s', job.router)
        report = {'state': 'failed', 'at': timezone.now().isoformat(), 'notes': [f'The check could not finish: {exc}'[:300]]}
    _save(job.pk, report)
    try:
        if report.get('healed'):
            from .utils import log
            log(job.router.business, 'Router Sync', f'{job.router.name}: {line(report)}'[:400], router=job.router)
    except Exception:
        pass
    return report


def heal(router, job=None, mirror=None, read_at=None, profiles=None):
    """The whole check for one router. ``mirror`` may be given (tests, callers that just read the router)."""
    from .agent import REFUSED_MARK
    from .linkops import uses_link
    from .models import AgentCommand, RouterHotspotProfile, Voucher
    from .router_time import platform_owned
    now = timezone.now()
    link = uses_link(router)
    report = {'state': 'done', 'at': now.isoformat(), 'transport': 'TapTap Link' if link else 'RouterOS API',
              'checked': 0, 'in_sync': 0, 'healed': 0, 'fixed': {k: 0 for k in CLASSES}, 'examples': [], 'notes': [],
              'ids': [], 'refused': 0, 'reset_suspected': False}
    manual = bool(job and getattr(job, 'requested_by_id', None))

    # 0. the Link queue: 'Queued' vouchers no command carries any more go back to Pending first
    if link:
        try:
            from .voucher_fastlane import release_stuck
            report['fixed']['stuck'] = int(release_stuck(router, force=True) or 0)
        except Exception as exc:  # noqa: BLE001
            report['notes'].append(f'Link queue check: {exc}'[:200])

    # 1. the router's picture
    note = ''
    if mirror is None:
        if link:
            mirror, read_at, note = _link_picture(router, job) if job else (None, None, 'No sync to compare with.')
        else:
            mirror, profiles, read_at, note = _api_picture(router)
    if link and profiles is None:
        profiles = set(RouterHotspotProfile.objects.filter(router=router, is_present=True).values_list('name', flat=True))
    if note:
        report['notes'].append(note)

    # 2. plan profiles
    try:
        from .plan_limits import plan_profile
        if profiles is not None:
            have = {p.lower() for p in profiles}
            want = set()
            for p in router.business.plans.filter(active=True):
                try:
                    want.add(plan_profile(p)[0])
                except Exception:
                    continue
            lacking = sorted(w for w in want if w and w.lower() not in have)
            if lacking:
                from .fit_sync import _profiles
                _profiles(router)
                report['fixed']['profiles'] = len(lacking)
                report['examples'].append({'code': ', '.join(lacking[:4]), 'what': 'plan profile missing on the router'})
    except Exception as exc:  # noqa: BLE001
        report['notes'].append(f'Plan profiles: {exc}'[:200])

    if mirror is None:
        report['state'] = 'partial'
        return _finish(report)

    # 3. every live voucher and member of this router
    plans = {p.name.lower(): p for p in router.business.plans.all()}
    carried = set()
    if link:
        for params in AgentCommand.objects.filter(router=router, kind='hotspot_users',
                                                  status__in=['queued', 'sent']).values_list('params', flat=True):
            carried.update((params or {}).get('ids') or [])
    cut = (read_at or now) - timedelta(seconds=5)
    renamed = {}                                       # vouchers whose code was changed: the rename path moves them
    try:
        from .models import VoucherCodeAlias
        recent = now - timedelta(days=RENAME_DAYS)
        for old, vid, at in VoucherCodeAlias.objects.filter(business=router.business).values_list('code', 'voucher_id', 'changed_at'):
            renamed.setdefault(vid, []).append(old.upper())
            if at and at >= recent:
                renamed.setdefault(('recent', vid), True)
    except Exception:
        pass
    to_push, members, synced, gone = [], [], [], []
    taptap_total = taptap_missing = 0
    qs = (router.vouchers.filter(deleted_at__isnull=True).exclude(status__in=['expired', 'archived'])
          .select_related('router'))
    vouchers = list(qs)
    _warm_shared(router, vouchers, mirror, plans)
    for v in vouchers:
        if v.created_at and v.created_at > cut:
            continue                                   # made after the router was read: the normal path sends it
        if v.pk in carried:
            continue                                   # on its way in a Link batch
        report['checked'] += 1
        row = mirror.get(v.code.upper())
        if platform_owned(v):
            taptap_total += 1
            if row is None:
                taptap_missing += 1
        found = diagnose(v, row, plans, now)
        if not found:
            report['in_sync'] += 1
            continue
        if found == ['status']:
            synced.append(v.pk)
            report['fixed']['status'] += 1
            continue
        if found == ['gone']:
            gone.append(v.pk)
            report['fixed']['gone'] += 1
            continue
        if 'missing' in found and v.pk in renamed and (any(o in mirror for o in renamed[v.pk])
                                                       or (link and ('recent', v.pk) in renamed)):
            report['renamed'] = report.get('renamed', 0) + 1     # its old code is being renamed on the router
            continue
        if (v.mikrotik_sync_error or '').startswith(REFUSED_MARK) and not manual:
            report['refused'] += 1                     # refused 3×: only a sync someone presses retries it
            continue
        for k in found:
            if k in report['fixed'] and k != 'status':
                report['fixed'][k] += 1
        if len(report['examples']) < EXAMPLES:
            report['examples'].append({'code': v.code, 'what': ', '.join(LABELS[k].split(' — ')[0] for k in found)})
        if link and found == ['time']:
            continue                                   # Link: the time repair right after the import already queued it
        (members if v.is_member else to_push).append(v)

    # 4. time up in TapTap, still switched on on the router
    expired_on = []
    on_names = [r.username for r in mirror.values() if not r.disabled]
    for i in range(0, len(on_names), 500):
        expired_on += list(router.vouchers.filter(status='expired', frozen_at__isnull=True, code__in=on_names[i:i + 500])
                           .only('pk', 'code', 'router_id', 'used_at', 'business_id'))
    expired_on = [v for v in expired_on if platform_owned(v)]

    # 5. heal
    if synced:
        Voucher.objects.filter(pk__in=synced).update(mikrotik_sync_status='Synced', mikrotik_sync_error='')
    if gone:
        Voucher.objects.filter(pk__in=gone).update(mikrotik_sync_status='Error', mikrotik_sync_error=GONE_NOTE)
    if taptap_total >= RESET_MIN and taptap_missing > taptap_total * RESET_SHARE:
        report['reset_suspected'] = True
        report['notes'].insert(0, f'{taptap_missing} of {taptap_total} TapTap vouchers were missing — the router looks reset '
                                  'or restored from an old backup. TapTap is sending them all back.')
    first = sorted(to_push, key=lambda v: (0 if v.used_at else 1 if getattr(v, 'sold_at', None) else 2, -v.pk))
    if len(first) > MAX_PUSH:
        report['notes'].append(f'{len(first) - MAX_PUSH} more will be checked at the next sync.')
        first = first[:MAX_PUSH]
    if first:
        try:
            _send(router, first, link)
        except Exception as exc:  # noqa: BLE001
            report['notes'].append(f'Sending: {exc}'[:200])
    if members:
        from .member_router_alignment import aligned_push_one
        failed = 0
        for m in members[:MAX_MEMBERS]:
            try:
                if aligned_push_one(m) is not True:
                    failed += 1
            except Exception:
                failed += 1
        if failed:
            report['notes'].append(f'{failed} member(s) could not be aligned now — see the member page.')
        if len(members) > MAX_MEMBERS:
            report['notes'].append(f'{len(members) - MAX_MEMBERS} more member(s) next time.')
    if expired_on:
        report['fixed']['expired_on'] = _switch_off(router, expired_on, link)
        for v in expired_on[:max(0, EXAMPLES - len(report['examples']))]:
            report['examples'].append({'code': v.code, 'what': 'time up but still on'})
    if report['refused']:
        report['notes'].append(f'{report["refused"]} code(s) the router refused 3 times were left alone — look for '
                               '“TapTap: could not save” in the router log, then press Sync.')
    report['ids'] = [v.pk for v in first + members[:MAX_MEMBERS]][:MAX_IDS]
    return _finish(report)


def _warm_shared(router, vouchers, mirror, plans):
    """router_time.router_shared() costs two queries per voucher; answer them all from one read of the profiles
    (the same rule: the profile the router has the user on, else TapTap's profile; its shared-users)."""
    try:
        from .models import RouterHotspotProfile
        from .router_time import SHARED_TTL, _profile_shared
        from .utils import voucher_profile
        shared = {p.name: _profile_shared(p) for p in RouterHotspotProfile.objects.filter(router=router, is_present=True)}
        batch = {}
        for v in vouchers:
            row = mirror.get(v.code.upper())
            name = row.profile if row is not None and row.profile else voucher_profile(v, plans.get((v.plan_name or '').lower()))[0]
            batch[f'tt:rt:sh:{router.pk}:{v.code.upper()}'] = shared.get(name, 0)
            if len(batch) >= 1000:
                cache.set_many(batch, SHARED_TTL)
                batch = {}
        if batch:
            cache.set_many(batch, SHARED_TTL)
    except Exception:
        logger.debug('warm shared-users %s', router, exc_info=True)


def _send(router, vouchers, link):
    """TapTap's values for these vouchers to the router, the usual way (Link queue / API in one connection)."""
    from .models import Voucher
    ids = [v.pk for v in vouchers]
    if link:                         # the fast lane's de-duplication stays: a voucher sent seconds ago is on its way
        from .voucher_push import push_vouchers
        push_vouchers(vouchers)
        return
    Voucher.objects.filter(pk__in=ids).update(mikrotik_sync_status='Pending', mikrotik_sync_error='')
    from .voucher_push import push_now
    push_now(router, ids)


def _switch_off(router, vouchers, link):
    from . import expiry
    codes = [v.code for v in vouchers]
    cache.delete_many([f'tt:exp:link:{router.pk}:{c.upper()}' for c in codes]
                      + [expiry._confirmed_key(router.pk, c) for c in codes] + [f'tt:exp:api:{router.pk}'])
    try:
        return int((expiry._queue_link if link else expiry._push_api)(router, vouchers) or 0)
    except Exception:
        logger.info('doctor switch-off %s', router, exc_info=True)
        return 0


def _finish(report):
    fixed = report['fixed']
    report['healed'] = sum(fixed.values())
    checked = report.get('checked') or 0
    bad = sum(fixed[k] for k in ('missing', 'onoff', 'profile', 'time', 'status', 'gone'))
    report['score'] = round(100 * (checked - min(bad, checked)) / checked) if checked else None
    return report


# ─────────────────────────────── words ───────────────────────────────

def line(report):
    """One line for the sync list and the activity log."""
    if not report:
        return ''
    state = report.get('state')
    if state == 'running':
        return 'Checking every voucher against the router…'
    if state == 'failed':
        return (report.get('notes') or ['The check could not finish.'])[0]
    parts = [f'{n} {SHORT.get(k, k)}' for k, n in (report.get('fixed') or {}).items() if n]
    if not parts:
        checked = report.get('checked') or 0
        return f'All {checked} voucher(s) match the router' if checked else (report.get('notes') or ['Nothing to check'])[0]
    return 'Healed ' + str(report.get('healed', 0)) + ': ' + ' · '.join(parts)


def live(report):
    """The report with how many healed vouchers the router has confirmed since (for the Routers page)."""
    if not report or not report.get('ids'):
        return report
    from .models import Voucher
    out = dict(report)
    out['confirmed'] = Voucher.objects.filter(pk__in=report['ids'], mikrotik_sync_status='Synced').count()
    out['waiting'] = len(report['ids']) - out['confirmed']
    out.pop('ids', None)
    return out
