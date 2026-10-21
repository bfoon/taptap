"""Voucher fast lane — vouchers always come first, and a changed voucher reaches its router at once.

Why customers stayed offline after "Reset devices"
---------------------------------------------------
* A shared-use / sharing / Fit warning freezes the voucher: ``status='disabled'``, ``frozen_at`` set and the
  hotspot user disabled on the router. Reset only freed the device slots, so the voucher stayed frozen and
  the live loop (``live.voucher_problem``) kept disconnecting every login.
* The old device signatures were still counted (only the Devices-page reset forgot them), so the next login
  was "a new device on a full voucher" and the voucher was warned again straight away.
* TapTap Link re-sends a command the router has not acknowledged. A disable / disconnect sent before the
  reset was re-sent AFTER it and switched the voucher off again on the router.
* A phone that mistyped a code a few times stayed locked out by voucher-entry security.

What this module does
---------------------
``heal()``             the real reset: forget devices, lift a warning, clear entry locks, cancel stale router
                       commands, and make the router user exactly right (enabled, any MAC, no old sessions).
``supersede()``        latest intent wins on TapTap Link: an enable cancels older pending disables of the same
                       voucher and the other way round (a re-sent command can never undo a newer one).
``schedule()``         every save that changes what the router must know (created, status, freeze, plan, time,
                       profile, devices, password, router) is sent for just that voucher when the transaction
                       commits — no full sync, no waiting.
``release_stuck()``    vouchers left "Queued" by a Link batch that expired or was never answered are sent again.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from functools import wraps

from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import post_init, post_save
from django.utils import timezone

logger = logging.getLogger('taptap.fastlane')

LINK_CHUNK = 25            # vouchers per immediate Link command (same size as push_pending_vouchers)
CLAIM_TTL = 15             # seconds: the same voucher state is not pushed twice within this time
STUCK_EVERY = 60           # seconds between stuck-"Queued" checks per router
# Fields whose change the router must hear about. ``code`` is left out on purpose: renames go through
# voucher_codes (a rename keeps the used time; an upsert under the new name would make a second user).
WATCHED = ('status', 'frozen_at', 'plan_name', 'duration_minutes', 'expires_at', 'router_id', 'router_profile',
           'rate_limit', 'max_devices', 'password', 'login_type')

DISABLE_PARAM = {'hotspot_user_set': 'name', 'hotspot_users_disable': 'names'}

_local = threading.local()


# ─────────────────────────────── small helpers ───────────────────────────────

def _on_link(router):
    from .voucher_history import channel
    return bool(router) and channel(router) == 'TapTap Link'


def can_be_online(voucher, now=None):
    """True when TapTap itself allows this voucher on the internet right now."""
    from .voucher_history import time_is_up
    return (voucher.status == 'active' and not voucher.frozen_at and not voucher.deleted_at
            and not time_is_up(voucher, now))


def _state(voucher):
    return tuple(bool(getattr(voucher, f)) if f == 'frozen_at' else getattr(voucher, f, None) for f in WATCHED)


def _state_hash(voucher):
    return hashlib.sha1(repr(_state(voucher)).encode()).hexdigest()[:16]


def claim(voucher):
    """True if this exact voucher state has not been sent in the last CLAIM_TTL seconds (and claims it).
    A different state always gets through, so A → B → A within seconds still ends at A on the router."""
    key, h = f'tt:vfast:{voucher.pk}', _state_hash(voucher)
    if cache.get(key) == h:
        return False
    cache.set(key, h, CLAIM_TTL)
    return True


def unclaim(voucher_ids):
    cache.delete_many([f'tt:vfast:{pk}' for pk in voucher_ids])


def _plans(business):
    return {p.name.lower(): p for p in business.plans.all()}


def user_row(voucher, plan=None):
    """One hotspot user row for the Link ``hotspot_users`` command — the router's view of this voucher."""
    from .durations import router_limit
    from .sync import voucher_profile
    prof, shared, rate = voucher_profile(voucher, plan)
    row = {'n': voucher.code, 'prof': prof, 'lim': router_limit(voucher, plan), 'dis': not can_be_online(voucher),
           'c': f'TapTap member {voucher.code}' if voucher.is_member else f'TapTap voucher {voucher.code}'}
    if voucher.is_member:
        row['pw'] = voucher.login_password
    return row, {'name': prof, 'shared': shared, 'rate': rate}


# ─────────────────────────── latest intent wins (TapTap Link) ───────────────────────────

def _intent(kind, params):
    """{CODE: True (enable) / False (disable)} that this command expresses about each voucher."""
    p = params or {}
    if kind == 'hotspot_user_set' and p.get('name'):
        return {str(p['name']).upper(): not p.get('disabled')}
    if kind == 'hotspot_users_disable':
        return {str(n).upper(): not p.get('disabled') for n in p.get('names') or []}
    if kind == 'hotspot_user_extend' and p.get('name'):
        return {str(p['name']).upper(): True}
    if kind == 'hotspot_users_heal':
        return {str(u.get('n')).upper(): True for u in p.get('users') or [] if u.get('n')}
    if kind == 'hotspot_users':
        return {str(u.get('n')).upper(): not u.get('dis') for u in p.get('users') or [] if u.get('n')}
    return {}


def supersede(router, intents, exclude_pk=None, kicks=False):
    """Make older pending Link commands agree with the newest intent {CODE: enabled}.

    Queued commands are cancelled or have the voucher taken out of them. Commands already sent are never
    cancelled (the router may be running them, and the order there must stay), but they are marked so they
    are not RE-sent: build_response() cancels a superseded command instead of sending it a second time.
    With ``kicks`` (a reset), pending disconnects/kicks of those vouchers go too."""
    if not router or not intents:
        return 0
    from .models import AgentCommand
    changed = 0
    kinds = ['hotspot_user_set', 'hotspot_users_disable', 'hotspot_user_extend', 'hotspot_users_heal',
             'hotspot_users', 'disconnect'] + (['hotspot_kick'] if kicks else [])
    qs = AgentCommand.objects.filter(router=router, kind__in=kinds, status__in=['queued', 'sent'])
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    for cmd in qs:
        p = dict(cmd.params or {})
        if cmd.kind in ('disconnect', 'hotspot_kick'):
            code = str(p.get('user', '')).upper()
            # an enabled voucher must not be signed out by an old enforcement; a disabled one keeps its disconnect
            if code in intents and (intents[code] and (cmd.kind == 'disconnect' or kicks)):
                changed += _drop(cmd, p)
            continue
        old = _intent(cmd.kind, p)
        clash = {c for c, en in old.items() if c in intents and intents[c] != en}
        if not clash:
            continue
        if cmd.kind == 'hotspot_users':
            # the row carries the whole user (profile, time…): flip only its enabled flag, keep the rest
            for u in p.get('users') or []:
                if str(u.get('n', '')).upper() in clash:
                    u['dis'] = not intents[str(u['n']).upper()]
            changed += _rewrite(cmd, p)
        elif cmd.kind in ('hotspot_users_disable', 'hotspot_users_heal'):
            key = 'names' if cmd.kind == 'hotspot_users_disable' else 'users'
            keep = [x for x in p.get(key) or [] if str(x.get('n') if isinstance(x, dict) else x).upper() not in clash]
            if keep:
                p[key] = keep
                changed += _rewrite(cmd, p)
            else:
                changed += _drop(cmd, p)
        else:
            changed += _drop(cmd, p)
    if changed:
        logger.info('fast lane: %s older Link command(s) on %s superseded', changed, router)
    return changed


def _drop(cmd, params):
    from .models import AgentCommand
    if cmd.status == 'queued':
        AgentCommand.objects.filter(pk=cmd.pk, status='queued').update(
            status='cancelled', done_at=timezone.now(), result='Replaced by a newer voucher change')
    else:
        params['superseded'] = True
        AgentCommand.objects.filter(pk=cmd.pk).update(params=params)
    return 1


def _rewrite(cmd, params):
    from .models import AgentCommand
    if cmd.status == 'sent':
        params['superseded'] = True          # what the router has is what it runs; just never send it again
    AgentCommand.objects.filter(pk=cmd.pk).update(params=params)
    return 1


def merge_rows(router, codes, exclude_pk=None):
    """A newer hotspot_users row replaces older rows of the same voucher still WAITING in the queue,
    so the router gets each voucher once, in its latest state. Returns how many rows were dropped."""
    from .models import AgentCommand
    codes = {str(c).upper() for c in codes}
    dropped = 0
    qs = AgentCommand.objects.filter(router=router, kind='hotspot_users', status='queued')
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    for cmd in qs:
        p = dict(cmd.params or {})
        users = p.get('users') or []
        keep = [u for u in users if str(u.get('n', '')).upper() not in codes]
        if len(keep) == len(users) or p.get('member_upsert'):
            continue
        dropped += len(users) - len(keep)
        if keep:
            p['users'] = keep
            AgentCommand.objects.filter(pk=cmd.pk).update(params=p)
        else:
            AgentCommand.objects.filter(pk=cmd.pk, status='queued').update(
                status='cancelled', done_at=timezone.now(), result='Merged into a newer voucher change')
    return dropped


def _wrapped_queue(original):
    @wraps(original)
    def queue(router, kind, params=None, label='', user=None, minutes=None):
        cmd = original(router, kind, params, label=label, user=user, minutes=minutes)
        try:
            if kind == 'hotspot_users' and not (params or {}).get('member_upsert'):
                merge_rows(router, [u.get('n') for u in (params or {}).get('users') or [] if u.get('n')],
                           exclude_pk=getattr(cmd, 'pk', None))
            intents = _intent(kind, params)
            if intents:
                supersede(router, intents, exclude_pk=getattr(cmd, 'pk', None))
        except Exception:
            logger.exception('fast lane: supersede after %s on %s', kind, router)
        return cmd
    queue._taptap_fastlane = True
    return queue


# ─────────────────────────── stuck "Queued" vouchers (TapTap Link) ───────────────────────────

def release_ids(ids, why='The router never confirmed the batch — sending again'):
    """Vouchers of a Link batch that expired / failed unanswered: back to Pending so they are re-sent."""
    from .models import Voucher
    if ids:
        Voucher.objects.filter(pk__in=list(ids), mikrotik_sync_status='Queued').update(
            mikrotik_sync_status='Pending', mikrotik_sync_error=why[:500])


def release_stuck(router, force=False):
    """Safety net: 'Queued' vouchers that no live hotspot_users command carries any more."""
    if not force and not cache.add(f'tt:vfast:stuck:{router.pk}', 1, STUCK_EVERY):
        return 0
    from .models import AgentCommand, Voucher
    queued = set(Voucher.objects.filter(router=router, mikrotik_sync_status='Queued').values_list('pk', flat=True))
    if not queued:
        return 0
    carried = set()
    for params in AgentCommand.objects.filter(router=router, kind='hotspot_users',
                                              status__in=['queued', 'sent']).values_list('params', flat=True):
        carried.update((params or {}).get('ids') or [])
    lost = queued - carried
    release_ids(lost, 'Lost from the router queue — sending again')
    return len(lost)


# ─────────────────────────── immediate per-voucher sync ───────────────────────────

def sync_now(voucher_ids, user=None):
    """Send just these vouchers to their routers right now. Returns {router_name: message}."""
    from .models import Voucher
    vouchers = list(Voucher.objects.filter(pk__in=list(voucher_ids), source='taptap', router__isnull=False)
                    .exclude(status__in=['expired', 'archived']).select_related('router', 'business'))
    by_router = {}
    for v in vouchers:
        if claim(v):
            by_router.setdefault(v.router, []).append(v)
    out = {}
    for router, items in by_router.items():
        try:
            out[router.name] = _send_link(router, items, user) if _on_link(router) else _send_api(router, items)
        except Exception as exc:
            unclaim([v.pk for v in items])
            logger.exception('fast lane: sync of %s voucher(s) to %s', len(items), router)
            out[router.name] = f'not sent yet ({exc}); it goes with the next check-in'
    return out


def _send_link(router, items, user=None):
    from .linkops import send
    from .models import Voucher
    from .sticky import profile_values
    members = [v for v in items if v.is_member]
    plain = [v for v in items if not v.is_member]
    for v in members:                       # members have their own authoritative push (member_router_alignment)
        try:
            from .views_agents import push_one
            push_one(v)
        except Exception:
            logger.exception('fast lane: member %s', v.code)
    now_part, later = plain[:LINK_CHUNK], plain[LINK_CHUNK:]
    if now_part:
        plans = _plans(router.business)
        users, profiles = [], {}
        for v in now_part:
            row, prof = user_row(v, plans.get((v.plan_name or '').lower()))
            users.append(row)
            profiles[prof['name']] = prof
        try:
            send(router, 'hotspot_users', {'users': users, 'profiles': list(profiles.values()),
                                           'ids': [v.pk for v in now_part], 'sticky': profile_values(router.business),
                                           'fastlane': True},
                 label=f'Voucher change: {", ".join(v.code for v in now_part[:3])}' + ('…' if len(now_part) > 3 else ''),
                 user=user, minutes=60 * 24)
        except ValueError as exc:            # router offline: the normal pending path sends it when it is back
            Voucher.objects.filter(pk__in=[v.pk for v in now_part]).update(mikrotik_sync_status='Pending',
                                                                            mikrotik_sync_error=str(exc)[:500])
            unclaim([v.pk for v in now_part])
            return f'{len(now_part)} voucher(s) waiting: {exc}'
        Voucher.objects.filter(pk__in=[v.pk for v in now_part]).update(mikrotik_sync_status='Queued', mikrotik_sync_error='')
    if later:                                # big batches: the rest go 25 per check-in, ahead of everything else
        Voucher.objects.filter(pk__in=[v.pk for v in later]).update(mikrotik_sync_status='Pending', mikrotik_sync_error='')
        unclaim([v.pk for v in later])
    return f'{len(now_part) + len(members)} voucher(s) go to {router.name} at its next check-in' \
           + (f', {len(later)} more right after' if later else '')


def _send_api(router, items):
    from .models import Voucher
    ids = [v.pk for v in items]
    Voucher.objects.filter(pk__in=ids).update(mikrotik_sync_status='Pending', mikrotik_sync_error='')
    try:
        from django.conf import settings
        if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
            raise RuntimeError('eager')
        from .tasks import push_vouchers_task
        push_vouchers_task.delay(router.pk, ids)
        return f'{len(ids)} voucher(s) being sent to {router.name}'
    except Exception:
        from .voucher_push import push_now
        sent, failed = push_now(router, ids)
        return f'{sent} voucher(s) sent to {router.name}' + (f', {failed} retry at the next sync' if failed else '')


# ─────────────────────────── save hook: change → sync ───────────────────────────

def _loaded(instance):
    """Watched values that are already loaded (never touches a deferred field: that would query and recurse)."""
    d = instance.__dict__
    out = {}
    for f in WATCHED:
        if f in d:
            out[f] = bool(d[f]) if f == 'frozen_at' else d[f]
    return out


def _snapshot(sender, instance, **kwargs):
    d = instance.__dict__
    if d.get('id') is None:
        instance._tt_fast_snap, instance._tt_fast_code = None, None
        return
    instance._tt_fast_snap = _loaded(instance)
    instance._tt_fast_code = d.get('code')


def _changed(sender, instance, created, update_fields=None, **kwargs):
    try:
        d = instance.__dict__
        if getattr(instance, '_tt_no_fastlane', False) or d.get('source', 'taptap') != 'taptap' or not d.get('router_id'):
            return
        if d.get('deleted_at') or d.get('status') in ('expired', 'archived'):
            return
        before = getattr(instance, '_tt_fast_snap', None) or {}
        now_state = _loaded(instance)
        old_code = getattr(instance, '_tt_fast_code', None)
        renamed = old_code is not None and 'code' in d and old_code != d['code']
        instance._tt_fast_snap, instance._tt_fast_code = now_state, d.get('code')
        if renamed:
            return
        if not created:
            fields = set(update_fields) if update_fields else set(now_state)
            if not any(f in fields and before.get(f, object()) != now_state.get(f) for f in now_state):
                return
        schedule(instance.pk)
    except Exception:
        logger.exception('fast lane: change hook for voucher %s', getattr(instance, 'pk', None))


def schedule(voucher_id):
    """Sync this voucher as soon as the current transaction commits (one send per router per commit)."""
    pending = getattr(_local, 'pending', None)
    if pending is None:
        pending = _local.pending = set()
    first = not pending
    pending.add(voucher_id)
    if first:
        transaction.on_commit(_flush)


def _flush():
    ids = getattr(_local, 'pending', None) or set()
    _local.pending = set()
    if ids:
        try:
            sync_now(ids)
        except Exception:
            logger.exception('fast lane: flush of %s voucher(s)', len(ids))


class quiet:
    """``with voucher_fastlane.quiet(): ...`` — saves inside do not trigger an immediate sync (bulk jobs)."""
    def __enter__(self):
        _local.muted = getattr(_local, 'muted', 0) + 1
        return self

    def __exit__(self, *exc):
        _local.muted -= 1


def _changed_guarded(sender, instance, created, **kwargs):
    if getattr(_local, 'muted', 0):
        return
    _changed(sender, instance, created, **kwargs)


# ─────────────────────────── the real reset ───────────────────────────

def heal(voucher, user=None, reason='', released_macs=()):
    """Reset devices, done properly. Returns (via, result_text, ok, notes)."""
    from . import shared_use
    from .voucher_history import channel
    now = timezone.now()
    notes = []
    macs = {str(m).upper() for m in released_macs or () if m}

    # 1. Devices are counted fresh: forget every signature that used this code (and remember their MACs).
    code = voucher.code.upper()
    from .models import DeviceSignature
    for sig in DeviceSignature.objects.filter(business=voucher.business).exclude(vouchers=[]).only('vouchers', 'macs', 'last_mac'):
        if any(str(c).upper() == code for c in sig.vouchers or []):
            macs.update(str(m).upper() for m in (sig.macs or []) if m)
            if sig.last_mac:
                macs.add(sig.last_mac.upper())
    shared_use.forget(voucher)
    try:
        shared_use._review(voucher, 'reset', user, (reason or 'Reset devices')[:255])
    except Exception:
        logger.exception('fast lane: shared-use review for %s', voucher.code)

    # 2. A warning is about devices — the reset answers it. A freeze or disable put on by staff stays.
    if voucher.frozen_at and voucher.freeze_kind == 'warning':
        from .voucher_freeze import unfreeze
        unfreeze([voucher], user=user, reason='Devices reset — warning lifted', source='user')
        voucher.refresh_from_db()
        notes.append('warning lifted, its time continues')
    elif voucher.frozen_at:
        notes.append('still FROZEN by staff — press Unfreeze so it can connect')
    elif voucher.status == 'disabled':
        notes.append('still DISABLED — press Enable so it can connect')

    # 3. Phones locked out for mistyped codes may try again.
    try:
        from .models_voucher_entry import VoucherEntryDevice
        if macs:
            n = VoucherEntryDevice.objects.filter(business=voucher.business, mac_address__in=list(macs)).filter(
                blocked_until__gt=now).update(blocked_until=None, blocked_at=None, attempts=0, window_started_at=None,
                                              last_unblocked_at=now,
                                              last_unblocked_by=user if getattr(user, 'is_authenticated', False) else None)
            if n:
                notes.append(f'{n} locked-out phone(s) unblocked')
    except Exception:
        logger.exception('fast lane: entry unblock for %s', voucher.code)

    # 4. The router: exactly TapTap's state, no MAC lock, no old sessions or cookies.
    online = can_be_online(voucher, now)
    via = channel(voucher.router)
    if not voucher.router:
        return via, 'No router assigned — changed in TapTap only', True, notes
    try:
        result = _heal_link(voucher, online, user) if via == 'TapTap Link' else _heal_api(voucher, online, macs)
        ok = True
    except Exception as exc:
        result, ok = f'Router update failed: {exc}', False
    if not online and not notes:
        notes.append('its time is up — add time so it can connect')
    return via, result, ok, notes


def _heal_link(voucher, online, user):
    from .linkops import send
    from .models import Voucher
    supersede(voucher.router, {voucher.code.upper(): online}, kicks=True)
    plan = _plans(voucher.business).get((voucher.plan_name or '').lower())
    row, prof = user_row(voucher, plan)
    from .sticky import profile_values
    try:
        send(voucher.router, 'hotspot_users', {'users': [row], 'profiles': [prof], 'ids': [voucher.pk],
                                               'sticky': profile_values(voucher.business), 'fastlane': True},
             label=f'Reset {voucher.code}: router user', user=user, minutes=60 * 24)
        Voucher.objects.filter(pk=voucher.pk).update(mikrotik_sync_status='Queued', mikrotik_sync_error='')
        claim(voucher)
        if online:
            send(voucher.router, 'hotspot_users_heal', {'profiles': [prof], 'users': [{'n': row['n'], 'prof': row['prof'], 'lim': row['lim']}]},
                 label=f'Reset {voucher.code}: enable, any device, clear old sessions', user=user, minutes=60 * 24)
        else:
            send(voucher.router, 'hotspot_user_mac', {'name': voucher.code, 'mac': '00:00:00:00:00:00'},
                 label=f'Reset {voucher.code}: any device', user=user, minutes=60 * 24)
    except ValueError as exc:
        return f'Not sent yet: {exc}'
    return 'Queued — the router applies it at its next check-in (a few seconds)'


def _heal_api(voucher, online, macs):
    from .mikrotik import MikroTikService
    from .models import Voucher
    from .sync import voucher_profile
    from .durations import router_limit
    plan = _plans(voucher.business).get((voucher.plan_name or '').lower())
    profile, shared, rate = voucher_profile(voucher, plan)
    svc = MikroTikService(voucher.router).connect()
    try:
        svc.ensure_hotspot_profile(profile, shared, rate)
        kind = 'member' if voucher.is_member else 'voucher'
        _, item_id = svc.upsert_voucher(voucher.code, profile, limit_uptime=router_limit(voucher, plan),
                                        password=voucher.login_password, disabled=not online,
                                        comment=f'TapTap {kind} {voucher.code}' + (f' · {voucher.customer_name}' if voucher.customer_name else ''))
        users = svc.resource('/ip/hotspot/user')
        rows = users.get(name=voucher.code)
        if rows:
            users.set(id=rows[0]['id'], mac_address='00:00:00:00:00:00')
        svc.release_devices(voucher.code, list(macs))    # sessions, cookies, hosts; Wi-Fi reconnect shows the login page
    finally:
        svc.close()
    Voucher.objects.filter(pk=voucher.pk).update(mikrotik_id=str(item_id or ''), mikrotik_sync_status='Synced', mikrotik_sync_error='')
    claim(voucher)
    return 'Router user fixed: ' + ('enabled, any device, old sessions cleared' if online else 'kept off, device lock cleared')


# ─────────────────────────────── install ───────────────────────────────

def install():
    from . import agent
    from .models import Voucher
    if not getattr(agent.queue, '_taptap_fastlane', False):
        agent.queue = _wrapped_queue(agent.queue)      # linkops.send() calls agent.queue → the wrapped one
    post_init.connect(_snapshot, sender=Voucher, dispatch_uid='tt_fastlane_snapshot')
    post_save.connect(_changed_guarded, sender=Voucher, dispatch_uid='tt_fastlane_changed')
    logger.info('Voucher fast lane enabled: immediate per-voucher sync, latest-command-wins, real reset.')
