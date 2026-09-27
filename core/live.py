"""Live sync — a light, frequent pass over each router.

A full sync reads ~30 RouterOS tables and takes seconds to minutes. This pass
reads only three (hotspot users, active sessions, IP bindings), so it can run
every few seconds and keep TapTap in step with the router:

* new vouchers made on the router appear in TapTap, with their price
* first use of a voucher is detected within seconds, so sales are dated correctly
* disabled/enabled users and IP bindings follow the router
* sessions whose voucher expired or was disabled become Security incidents and
  are fixed automatically after a grace period (or immediately with "Fix now")
* timed IP-binding access is switched off when it runs out

It runs from Celery beat (``core.tasks.live_watch_all``), from the
``live_watch`` management command, or — when neither is running — from the
browser heartbeat of any open TapTap page.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections, connection
from django.utils import timezone

from .finance import mark_activated
from .mikrotik import MikroTikService, ros_bool
from .models import (Business, IPBindingAccessExpiry, Router, RouterHotspotUser, SessionIncident, SyncedIPBinding,
                     Voucher)
from .sync import _has_uptime, _routeros_hours, _routeros_seconds, first_use_estimate, price_from_text
from .utils import log

logger = logging.getLogger('taptap.live')
SKIP_USERS = {'default-trial'}
EVENTS_MAX = 40


def interval():
    return max(5, int(getattr(settings, 'LIVE_WATCH_SECONDS', 15)))


# ─────────────────────────── change feed (shown in the top bar) ───────────────────────────
def push_event(business_id, text, kind='info'):
    key = f'tt:live:events:{business_id}'
    try:
        events = cache.get(key) or []
        events.insert(0, {'t': timezone.now().isoformat(), 'text': text[:160], 'kind': kind})
        cache.set(key, events[:EVENTS_MAX], 86400)
    except Exception:
        pass


def recent_events(business_id):
    try:
        return cache.get(f'tt:live:events:{business_id}') or []
    except Exception:
        return []


# ─────────────────────────── enforcement ───────────────────────────
def voucher_problem(voucher, now):
    """Why this voucher must not be online, or None."""
    if voucher.status == 'disabled':
        return 'disabled', 'Disabled in TapTap but still connected on the router'
    expires = voucher.expires_at
    if not expires and voucher.used_at and voucher.duration_hours:
        expires = voucher.used_at + timedelta(hours=voucher.duration_hours)
    if voucher.status == 'expired' or (expires and expires <= now):
        late = now - expires if expires else None
        mins = int(late.total_seconds() // 60) if late else 0
        human = f'{mins // 1440}d {mins % 1440 // 60}h' if mins >= 1440 else (f'{mins // 60}h {mins % 60}m' if mins >= 60 else f'{mins} min')
        return 'expired', f'Voucher ran out {human} ago' if late else 'Voucher is marked expired'
    return None


def fix_incident(incident, svc=None, user=None, by='user'):
    """Disconnect the session and disable the voucher on the router. Returns (ok, message)."""
    own = svc is None
    try:
        if own:
            svc = MikroTikService(incident.router, timeout=getattr(settings, 'MIKROTIK_LIVE_TIMEOUT', 5)).connect()
        svc.reset_active_by_name(incident.username)
        try:
            svc.disable_voucher(incident.username)
        except Exception as exc:  # user may not exist on the router (e.g. RADIUS)
            logger.info('disable %s: %s', incident.username, exc)
        RouterHotspotUser.objects.filter(router=incident.router, username=incident.username).update(disabled=True)
        if incident.voucher_id:
            new_status = 'expired' if incident.reason == 'expired' else 'disabled'
            Voucher.objects.filter(pk=incident.voucher_id).exclude(status=new_status).update(status=new_status)
        incident.status, incident.fixed_at, incident.fixed_by, incident.fixed_user, incident.error = 'fixed', timezone.now(), by, user, ''
        incident.save(update_fields=['status', 'fixed_at', 'fixed_by', 'fixed_user', 'error'])
        msg = f'{"Auto-fixed" if by == "auto" else "Fixed"}: disconnected {incident.username} on {incident.router.name} ({incident.get_reason_display().lower()})'
        log(incident.business, 'Session Enforced', msg)
        push_event(incident.business_id, msg, 'fix')
        return True, msg
    except Exception as exc:
        incident.error = str(exc)[:255]
        incident.save(update_fields=['error'])
        return False, str(exc)
    finally:
        if own and svc:
            svc.close()


# ─────────────────────────── one router ───────────────────────────
def watch_router(router, force=False):
    lock = f'tt:watch:router:{router.pk}'
    if not cache.add(lock, 1, timeout=max(30, interval() * 3)):
        return {'skipped': 'already running'}
    summary = {'router': router.name, 'new_vouchers': 0, 'activated': 0, 'users_changed': 0, 'bindings_changed': 0,
               'incidents': 0, 'fixed': 0, 'expiries': 0}
    now = timezone.now()
    business = router.business
    try:
        svc = MikroTikService(router, timeout=getattr(settings, 'MIKROTIK_LIVE_TIMEOUT', 5)).connect()
    except Exception as exc:
        if router.status != 'Offline':
            Router.objects.filter(pk=router.pk).update(status='Offline', last_error=str(exc)[:2000], last_tested_at=now)
            push_event(business.pk, f'{router.name} went offline', 'bad')
        cache.delete(lock)
        return {**summary, 'error': str(exc)[:300]}
    try:
        users = svc.hotspot_users()
        active = svc.active_users()
        bindings = svc.bindings()
        baseline = router.sales_baseline_at

        # ---------------- hotspot users → vouchers ----------------
        mirror = {u.username: u for u in RouterHotspotUser.objects.filter(router=router)}
        names = [str(r.get('name', '')).strip() for r in users if r.get('name')]
        vouchers = {v.code.upper(): v for v in Voucher.objects.filter(business=business, code__in=names)}
        missing = [n for n in names if n.upper() not in vouchers]
        if missing:  # codes stored in a different letter case
            variants = {x for n in missing for x in (n.upper(), n.lower())}
            for v in Voucher.objects.filter(business=business, code__in=variants):
                vouchers.setdefault(v.code.upper(), v)
        plans = {}
        for p in business.plans.all():
            plans[(p.mikrotik_profile_name or p.name).lower()] = p
            plans.setdefault(p.name.lower(), p)
        seen = set()
        for row in users:
            name = str(row.get('name', '')).strip()
            if not name or name in SKIP_USERS:
                continue
            seen.add(name)
            disabled = ros_bool(row.get('disabled', False))
            profile = str(row.get('profile', 'default') or 'default')
            uptime = str(row.get('uptime', ''))
            m = mirror.get(name)
            if m is None:
                m = RouterHotspotUser.objects.create(business=business, router=router, username=name, mikrotik_id=str(row.get('id', '')),
                                                     profile=profile, comment=str(row.get('comment', ''))[:255], disabled=disabled,
                                                     limit_uptime=str(row.get('limit-uptime', '')), uptime=uptime, is_present=True,
                                                     source='taptap' if name.upper() in vouchers and vouchers[name.upper()].source == 'taptap' else 'mikrotik',
                                                     raw_data=dict(row), last_seen_at=now)
                summary['users_changed'] += 1
            else:
                changed = []
                for field, value in (('disabled', disabled), ('profile', profile), ('is_present', True),
                                     ('comment', str(row.get('comment', ''))[:255]), ('limit_uptime', str(row.get('limit-uptime', '')))):
                    if getattr(m, field) != value:
                        setattr(m, field, value); changed.append(field)
                if uptime != m.uptime and (not _has_uptime(m.uptime) or changed or (now - m.last_seen_at).total_seconds() > 300):
                    m.uptime = uptime; changed.append('uptime')
                if changed:
                    m.last_seen_at = now; changed.append('last_seen_at')
                    m.save(update_fields=changed)
                    if set(changed) - {'uptime', 'last_seen_at'}:
                        summary['users_changed'] += 1

            v = vouchers.get(name.upper())
            if v is None:
                plan = plans.get(profile.lower())
                price = price_from_text(row.get('comment', ''), business.currency) or (plan.price if plan and plan.price else 0)
                try:
                    v = Voucher.objects.create(business=business, router=router, code=name, plan_name=profile, price=price or 0,
                                               duration_hours=_routeros_hours(row.get('limit-uptime', ''), plan.duration_hours if plan else 24),
                                               max_devices=plan.max_devices if plan else 1, status='disabled' if disabled else 'active',
                                               source='mikrotik', mikrotik_id=str(row.get('id', '')), mikrotik_sync_status='Synced')
                except Exception:
                    continue  # code already used by another business
                vouchers[name.upper()] = v
                summary['new_vouchers'] += 1
                push_event(business.pk, f'New voucher {name} ({profile}) appeared on {router.name}', 'new')
            elif v.source == 'mikrotik':
                want = 'disabled' if disabled else ('expired' if v.status == 'expired' else 'active')
                if v.status != want:
                    Voucher.objects.filter(pk=v.pk).update(status=want); v.status = want
                    push_event(business.pk, f'Voucher {name} {"disabled" if disabled else "enabled"} on {router.name}')
            if v.router_id is None:
                Voucher.objects.filter(pk=v.pk).update(router=router); v.router_id = router.pk

            if _has_uptime(uptime) and not v.used_at:
                when = first_use_estimate(uptime, now, v.created_at if v.source == 'taptap' else None)
                if baseline or v.source == 'taptap':
                    if mark_activated(v, when):
                        summary['activated'] += 1
                        push_event(business.pk, f'Voucher {name} started on {router.name}', 'sale')
                else:
                    Voucher.objects.filter(pk=v.pk, used_at__isnull=True).update(used_at=when); v.used_at = when

        gone = [u for n, u in mirror.items() if n not in seen and u.is_present]
        if gone:
            RouterHotspotUser.objects.filter(pk__in=[u.pk for u in gone]).update(is_present=False)
            summary['users_changed'] += len(gone)
            if len(gone) <= 3:
                for u in gone: push_event(business.pk, f'Voucher {u.username} was removed from {router.name}')
            else:
                push_event(business.pk, f'{len(gone)} vouchers were removed from {router.name}')

        # ---------------- active sessions → activation + enforcement ----------------
        open_now = {}
        for s in active:
            user = str(s.get('user', '')).strip()
            if not user or user.upper().startswith('T-'):
                continue  # HotSpot trial users
            v = vouchers.get(user.upper())
            if v is None:
                continue
            if not v.used_at:
                when = now - timedelta(seconds=_routeros_seconds(s.get('uptime')))
                if mark_activated(v, when):
                    summary['activated'] += 1
                    push_event(business.pk, f'Voucher {user} started on {router.name}', 'sale')
                v.refresh_from_db(fields=['used_at', 'expires_at', 'status'])
            if v.used_at and not v.expires_at and v.duration_hours:
                v.expires_at = v.used_at + timedelta(hours=v.duration_hours)
                Voucher.objects.filter(pk=v.pk, expires_at__isnull=True).update(expires_at=v.expires_at)
            problem = voucher_problem(v, now)
            if not problem:
                continue
            mac = str(s.get('mac-address', '')).upper()
            key = (user.upper(), mac)
            open_now[key] = (s, v, problem)

        incidents = {(i.username.upper(), i.mac_address.upper()): i for i in
                     SessionIncident.objects.filter(router=router, status__in=['open', 'ignored'])}
        grace = timedelta(minutes=business.enforce_grace_minutes or 0)
        for key, (s, v, (reason, detail)) in open_now.items():
            inc = incidents.get(key)
            if inc is None:
                inc = SessionIncident.objects.create(business=business, router=router, voucher=v, username=v.code, mac_address=key[1],
                                                     ip_address=str(s.get('address', '')), session_id=str(s.get('id', '')),
                                                     reason=reason, detail=detail, first_seen=now, last_seen=now, fix_due_at=now + grace)
                summary['incidents'] += 1
                push_event(business.pk, f'{v.code} is online on {router.name} but its voucher {"expired" if reason == "expired" else "is disabled"}', 'bad')
            else:
                inc.last_seen, inc.detail, inc.session_id = now, detail, str(s.get('id', ''))
                inc.save(update_fields=['last_seen', 'detail', 'session_id'])
            if inc.status == 'open' and business.auto_enforce and inc.fix_due_at and inc.fix_due_at <= now:
                ok, _ = fix_incident(inc, svc=svc, by='auto')
                if ok:
                    summary['fixed'] += 1
        ended = [i.pk for k, i in incidents.items() if k not in open_now and i.status == 'open']
        if ended:
            SessionIncident.objects.filter(pk__in=ended).update(status='ended', fixed_at=now, fixed_by='router')

        # ---------------- traffic & consumption ----------------
        try:
            from .traffic import collect
            summary['traffic'] = collect(router, svc, active, now)
        except Exception as exc:  # reporting must never break voucher sync
            logger.info('traffic collection %s: %s', router, exc)

        # ---------------- port guards, timed actions, nightly backup ----------------
        try:
            from .portctl import tick as port_tick
            summary['ports'] = port_tick(router, svc, now)
        except Exception as exc:
            logger.info('port control %s: %s', router, exc)

        # ---------------- IP bindings ----------------
        bmirror = {b.mikrotik_id: b for b in SyncedIPBinding.objects.filter(router=router) if b.mikrotik_id}
        bseen = set()
        for row in bindings:
            mid = str(row.get('id', ''))
            if not mid:
                continue
            bseen.add(mid)
            vals = {'mac_address': str(row.get('mac-address', '')).upper(), 'address': str(row.get('address', '')),
                    'server': str(row.get('server', '')), 'binding_type': str(row.get('type', 'regular') or 'regular'),
                    'comment': str(row.get('comment', ''))[:255], 'disabled': ros_bool(row.get('disabled', False)), 'is_present': True}
            b = bmirror.get(mid)
            if b is None:
                b = SyncedIPBinding.objects.filter(router=router, mikrotik_id='', mac_address=vals['mac_address']).first() if vals['mac_address'] else None
            if b is None:
                SyncedIPBinding.objects.create(business=business, router=router, mikrotik_id=mid, source='mikrotik', sync_status='Synced',
                                               raw_data=dict(row), last_seen_at=now, **vals)
                summary['bindings_changed'] += 1
                continue
            changed = [k for k, val in vals.items() if getattr(b, k) != val]
            if b.mikrotik_id != mid:
                b.mikrotik_id = mid; changed.append('mikrotik_id')
            if changed:
                if 'disabled' in changed:
                    push_event(business.pk, f'Binding {vals["comment"] or vals["mac_address"]} {"disabled" if vals["disabled"] else "enabled"} on {router.name}')
                for k in changed:
                    if k in vals: setattr(b, k, vals[k])
                b.last_seen_at = now; b.sync_status = 'Synced'; b.raw_data = dict(row)
                b.save(update_fields=list(set(changed) | {'last_seen_at', 'sync_status', 'raw_data', 'updated_at'}))
                summary['bindings_changed'] += 1
        stale = [b.pk for mid, b in bmirror.items() if mid not in bseen and b.is_present]
        if stale:
            SyncedIPBinding.objects.filter(pk__in=stale).update(is_present=False)
            summary['bindings_changed'] += len(stale)

        # Timed access that ran out
        for exp in IPBindingAccessExpiry.objects.filter(router=router, expires_at__lte=now):
            try:
                svc.toggle_binding(exp.binding_id, True)
                SyncedIPBinding.objects.filter(router=router, mikrotik_id=exp.binding_id).update(disabled=True)
                push_event(business.pk, f'Timed access ended for {exp.mac_address or exp.binding_id} on {router.name}')
                summary['expiries'] += 1
            except Exception as exc:
                logger.info('expire binding %s: %s', exp.binding_id, exc)
            exp.delete()

        updates = {'last_watch_at': now, 'last_tested_at': now}
        if router.status != 'Online':
            updates.update(status='Online', last_error='')
            push_event(business.pk, f'{router.name} is back online', 'good')
        if not baseline:
            updates['sales_baseline_at'] = now
        Router.objects.filter(pk=router.pk).update(**updates)

        # ---------------- device presence (once a minute) ----------------
        try:
            from .presence import presence_interval, scan
            if cache.add(f'tt:pres:gate:{router.pk}', 1, presence_interval()):
                router.status = 'Online'
                summary['presence'] = scan(router)
        except Exception as exc:
            logger.info('presence scan %s: %s', router, exc)
        return summary
    except Exception as exc:
        logger.exception('live watch %s failed', router)
        return {**summary, 'error': str(exc)[:300]}
    finally:
        svc.close()
        cache.delete(lock)


# ─────────────────────────── many routers ───────────────────────────
def _watch_in_thread(router_id, force):
    close_old_connections()
    try:
        router = Router.objects.select_related('business').get(pk=router_id)
        return watch_router(router, force)
    finally:
        connection.close()


def watch_business(business, force=False):
    if not business.live_sync and not force:
        return []
    ids = []
    for r in business.routers.all():
        # Offline routers are retried once a minute, not every pass.
        if r.status == 'Offline' and not force and not cache.add(f'tt:watch:retry:{r.pk}', 1, 60):
            continue
        ids.append(r.pk)
    if not ids:
        return []
    if len(ids) == 1 or connection.vendor == 'sqlite':
        return [watch_router(Router.objects.select_related('business').get(pk=i), force) for i in ids]
    with ThreadPoolExecutor(max_workers=min(6, len(ids))) as pool:
        return list(pool.map(lambda i: _watch_in_thread(i, force), ids))


def watch_all():
    """One pass over every business with live sync on (called by Celery beat)."""
    try:
        cache.set('tt:beat', timezone.now().isoformat(), interval() * 4)
    except Exception:
        pass
    if not cache.add('tt:watch:all', 1, timeout=interval() * 4):
        return 'previous pass still running'
    try:
        if cache.add('tt:traffic:prune', 1, 3600):
            try:
                from .traffic import prune
                prune()
            except Exception as exc:
                logger.info('traffic prune: %s', exc)
        results = []
        for business in Business.objects.filter(live_sync=True, routers__isnull=False).distinct():
            if not business.has_access:
                continue
            results += watch_business(business)
        return results
    finally:
        cache.delete('tt:watch:all')


def beat_alive():
    try:
        return bool(cache.get('tt:beat'))
    except Exception:
        return False
