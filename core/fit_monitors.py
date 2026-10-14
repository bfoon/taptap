"""Fit monitors — standing tasks that watch the network for you, alert you, and (if you chose) act.

Two kinds of monitor (models_fit.FitMonitor):

* Event monitors react the instant something happens, from inside device_lock.claim():
    slot_refused   a new device is refused because every slot of a voucher/member is taken
    slot_filled    a voucher/member just used its last free slot
* State monitors are checked every live cycle (core/live.py, and the page heartbeat when no scheduler runs):
    idle_slots     free slots held by devices not seen for N days (only on full vouchers, by default)
    voucher_online a given voucher/member comes online (optionally only once)
    sharing        more devices online on one voucher than it allows
    heavy_user     one customer downloads faster than N Mbit/s for M minutes
    online_drop    customers online on a router fall by N % within 15 minutes (outage symptom)
    router_offline a router stays offline for N minutes

What a monitor can do when it fires:
    always      → a bell alert in TapTap (only its creator, or the whole team) + a log line (FitMonitorEvent)
    email       → the business notification emails (event "fit_monitor", Notifications page)
    notify_member → an email to the member/customer it is about (when they have an email address)
    response    → "notify" only · "free_slot": free ONE idle slot (a device that is not online now and has not
                  been seen for min_idle_minutes) and let the refused device straight in · "disconnect": kick the
                  devices that are not locked to the voucher (sharing).

Safety: a device that is online right now is never removed; each voucher+device is handled once per cooldown;
everything is written to the voucher history by the existing services (remove_one, kick).
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from datetime import timedelta
from functools import wraps

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('taptap.fit.monitors')

EVENT_KINDS = ('slot_refused', 'slot_filled')
CHECK_EVERY = 60            # seconds between state checks of one business
SWEEP_EVERY = 600           # idle_slots: at most every 10 minutes
DROP_WINDOW = 15 * 60       # online_drop compares with the peak of the last 15 minutes

# kind: (label, what it needs, default params, responses it supports)
KINDS = OrderedDict([
    ('slot_refused', ('Device refused — slots full', 'plans / codes / members_only', {'min_idle_minutes': 20}, ('notify', 'free_slot'))),
    ('slot_filled', ('Last slot taken', 'plans / codes / members_only', {}, ('notify',))),
    ('idle_slots', ('Free idle slots', 'days (default 3), plans / codes, only_when_full', {'days': 3, 'only_when_full': True}, ('free_slot',))),
    ('voucher_online', ('Voucher comes online', 'codes (required), once', {'once': False}, ('notify',))),
    ('sharing', ('Sharing (too many devices)', 'plans / codes', {}, ('notify', 'disconnect'))),
    ('heavy_user', ('Heavy downloader', 'threshold Mbit/s (default 20), minutes (default 10)', {'threshold': 20, 'minutes': 10}, ('notify',))),
    ('online_drop', ('Customers suddenly drop', 'threshold % (default 50), min users (default 10), routers', {'threshold': 50, 'min_users': 10}, ('notify',))),
    ('router_offline', ('Router offline', 'minutes (default 5), routers', {'minutes': 5}, ('notify',))),
])


# ─────────────────────────────── matching ───────────────────────────────

def _names(v):
    if isinstance(v, str):
        v = v.split(',')
    return [str(x).strip() for x in (v or []) if str(x).strip()]


def plan_of(voucher):
    """Plan names a voucher answers to: its own plan name and, for members, the member plan's name."""
    names = {str(voucher.plan_name or '').strip().lower()}
    if getattr(voucher, 'login_type', '') == 'member':
        try:
            a = voucher.member_plan_assignment
            if a and a.plan_id:
                names.add(str(a.plan.name or '').strip().lower())
        except Exception:
            pass
    return names - {''}


def matches(monitor, voucher):
    p = monitor.params or {}
    if voucher.business_id != monitor.business_id:
        return False
    codes = {c.upper() for c in _names(p.get('codes'))}
    if codes and voucher.code.upper() not in codes:
        return False
    plans = {x.lower() for x in _names(p.get('plans'))}
    if plans and not (plan_of(voucher) & plans):
        return False
    if p.get('members_only') and getattr(voucher, 'login_type', '') != 'member':
        return False
    routers = [int(x) for x in (p.get('routers') or []) if str(x).isdigit()]
    if routers and voucher.router_id not in routers:
        return False
    return True


def live(monitors):
    now = timezone.now()
    return [m for m in monitors if m.active and not (m.expires_at and m.expires_at <= now)]


def _active(business_id, kinds):
    from .models_fit import FitMonitor
    return live(FitMonitor.objects.filter(business_id=business_id, kind__in=kinds, active=True).select_related('business', 'created_by'))


# ─────────────────────────────── telling people ───────────────────────────────

def _bell(monitor, title, body, link, level='warning'):
    from .models_events import EventAlert
    EventAlert.objects.create(business=monitor.business, user=monitor.created_by if monitor.audience == 'me' else None,
                              rule=None, kind='fit_monitor', level=level, title=title[:160], body=body[:400], link=link[:200],
                              sound=True, desktop=True)


def _email_team(monitor, title, body, link, key):
    if not monitor.email:
        return
    try:
        from .notify import notify
        notify(monitor.business, 'fit_monitor', title, body, link=link, key=key)
    except Exception:
        logger.exception('monitor %s email', monitor.pk)


def member_email(voucher):
    try:
        s = voucher.member_notification_settings
        return (s.email or '').strip()
    except Exception:
        return ''


def _send_member(to, subject, text):
    """Customer emails are sent off the request thread (the portal must answer at once)."""
    def go():
        try:
            from django.conf import settings
            from django.core.mail import EmailMultiAlternatives
            EmailMultiAlternatives(subject=subject[:180], body=text, from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
                                   to=[to]).send(fail_silently=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning('member email to %s failed: %s', to, exc)
    threading.Thread(target=go, daemon=True).start()


def fire(monitor, title, body, link='', voucher=None, mac='', action='', result='', member_text='', level='warning', key=''):
    """Bell + log (+ email, + member email). Returns the FitMonitorEvent."""
    from .models_fit import FitMonitorEvent
    now = timezone.now()
    emailed = False
    if member_text and monitor.notify_member and voucher is not None:
        to = member_email(voucher)
        if to:
            b = monitor.business
            sign = f'\n\n— {b.business_name}' + (f' · {b.phone}' if getattr(b, 'phone', '') else '')
            _send_member(to, f'{b.business_name}: about your Wi-Fi account {voucher.code}', member_text + sign)
            emailed = True
    full = body + (f' Result: {result}' if result else '') + (' The member was emailed.' if emailed else '')
    _bell(monitor, title, full, link, level)
    _email_team(monitor, title, full, link, key or f'mon:{monitor.pk}:{now:%Y%m%d%H%M}')
    ev = FitMonitorEvent.objects.create(business=monitor.business, monitor=monitor, kind=monitor.kind, title=title[:160],
                                        body=body[:500], link=link[:200], voucher_code=(voucher.code if voucher else '')[:64],
                                        mac=(mac or '')[:32], action=action[:20], result=(result or '')[:300], member_emailed=emailed)
    type(monitor).objects.filter(pk=monitor.pk).update(hits=monitor.hits + 1, last_fired_at=now)
    monitor.hits += 1
    monitor.last_fired_at = now
    try:
        from .live import push_event
        push_event(monitor.business_id, f'Monitor: {title}', 'info')
    except Exception:
        pass
    return ev


# ─────────────────────────────── freeing a slot ───────────────────────────────

def idle_binding(voucher, min_idle_minutes=20, now=None):
    """The device slot that can go: not online now, not seen for min_idle_minutes, the longest-unseen first.
    None when every device is online or was seen too recently (then nothing is freed)."""
    from . import device_lock
    now = now or timezone.now()
    online = {device_lock.norm_mac(m) for m in ((device_lock.online_hints(voucher) or {}).get('online_macs') or [])}
    cutoff = now - timedelta(minutes=max(0, int(min_idle_minutes or 0)))
    rows = [b for b in voucher.device_bindings.order_by('last_seen_at')
            if device_lock.norm_mac(b.current_mac) not in online and (not b.last_seen_at or b.last_seen_at <= cutoff)]
    return rows[0] if rows else None


def free_one(voucher, binding):
    from .shared_voucher_device_control import remove_one
    return remove_one(voucher, binding.pk, user=None)


# ─────────────────────────────── event monitors (inside claim) ───────────────────────────────

def _slots_full(voucher, outcome):
    from . import device_lock
    msg = str(getattr(outcome, 'message', '') or '')
    return outcome.status == 'denied' and msg.startswith('This voucher is already in use') and \
        voucher.device_bindings.count() >= device_lock.slots(voucher)


def _ref_key(business_id, plan, day=None):
    import hashlib
    digest = hashlib.sha1(str(plan).strip().lower().encode('utf-8')).hexdigest()[:16]
    return f'fitmon:ref:{business_id}:{day or timezone.localdate().isoformat()}:{digest}'


def count_refusal(voucher):
    """Refusals per plan per day, for the slots report (kept even without a monitor)."""
    for plan in plan_of(voucher) or {'(no plan)'}:
        key = _ref_key(voucher.business_id, plan)
        if cache.add(key, 1, 3 * 86400):
            continue
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, 3 * 86400)


def refusals_today(business_id, plan):
    return int(cache.get(_ref_key(business_id, plan)) or 0)


def _who(voucher):
    return ('Member ' if getattr(voucher, 'login_type', '') == 'member' else 'Voucher ') + voucher.code


def on_refused(voucher, mac, fp, original, call):
    """A device was refused (all slots taken). Returns a new Outcome when a monitor let it in, else None."""
    from . import device_lock
    count_refusal(voucher)
    who_dev = device_lock.norm_mac(mac) or (fp[:10] + '…' if fp else 'a device')
    admitted = None
    for m in _active(voucher.business_id, ('slot_refused',)):
        if not matches(m, voucher):
            continue
        if not cache.add(f'fitmon:cd:{m.pk}:{voucher.pk}:{device_lock.norm_mac(mac) or fp[:32]}', 1, max(1, m.cooldown_minutes) * 60):
            continue                     # this device was already handled within the cooldown
        n = device_lock.slots(voucher)
        link = f'/vouchers/{voucher.pk}/'
        title = f'{_who(voucher)}: all {n} slot{"s" if n != 1 else ""} full — {who_dev} refused'
        body = f'Plan {voucher.plan_name or "—"}. A new device tried to sign in but every device slot is taken.'
        member = (f'Hello,\n\nA new device ({who_dev}) tried to sign in to your Wi-Fi account {voucher.code}, '
                  f'but all {n} device slot{"s are" if n != 1 else " is"} in use.')
        if m.response == 'free_slot' and admitted is None:
            b = idle_binding(voucher, (m.params or {}).get('min_idle_minutes', 20))
            if b is None:
                fire(m, title, body, link, voucher, mac, action='nothing_idle',
                     result='Nothing freed: every locked device is online now or was used in the last '
                            f'{(m.params or {}).get("min_idle_minutes", 20)} min — this looks like sharing.',
                     member_text=member + ' All your devices are in use right now, so the new one was not let in. '
                                          'If you changed phone, sign out on an old device or contact us.')
                continue
            gone = b.label or b.current_mac or f'device {b.slot_no}'
            try:
                free_one(voucher, b)
                admitted = original(*call[0], **call[1])
            except Exception as exc:  # noqa: BLE001
                logger.exception('monitor %s free slot', m.pk)
                fire(m, title, body, link, voucher, mac, action='failed', result=f'Could not free a slot: {exc}')
                continue
            ok = admitted is not None and admitted.allowed
            fire(m, title.replace(' refused', ' let in' if ok else ' refused'), body, link, voucher, mac,
                 action='admitted' if ok else 'freed_slot',
                 result=f'Freed slot {b.slot_no} ({gone}, idle)' + (' and let the new device in.' if ok else '; the device can now sign in.'),
                 member_text=member + f' We freed the slot of an unused device ({gone}) so the new one could connect. '
                                      'If this was not you, contact us.')
        else:
            fire(m, title, body, link, voucher, mac, action='notified',
                 member_text=member + ' Nothing was changed. If you got a new phone, ask us to free a slot.')
    return admitted if admitted is not None and admitted.allowed else None


def on_new(voucher, mac, outcome):
    from . import device_lock
    if voucher.device_bindings.count() < device_lock.slots(voucher):
        return
    for m in _active(voucher.business_id, ('slot_filled',)):
        if not matches(m, voucher):
            continue
        n = device_lock.slots(voucher)
        label = getattr(outcome.binding, 'label', '') or device_lock.norm_mac(mac) or 'a device'
        fire(m, f'{_who(voucher)} is now full ({n}/{n} devices)', f'{label} took the last slot. The next new device will be refused.',
             f'/vouchers/{voucher.pk}/', voucher, mac, action='notified', level='info',
             member_text=f'Hello,\n\nYour Wi-Fi account {voucher.code} now uses all {n} of its device slots ({label} was the last). '
                         'A further new device will not be able to sign in.')


def _wrapped_claim(original):
    @wraps(original)
    def wrapped(voucher, mac='', fp='', source='portal', label='', hints=None):
        outcome = original(voucher, mac=mac, fp=fp, source=source, label=label, hints=hints)
        try:
            if outcome.status == 'denied' and _slots_full(voucher, outcome):
                better = on_refused(voucher, mac, fp or '', original,
                                    ((voucher,), {'mac': mac, 'fp': fp, 'source': source, 'label': label, 'hints': hints}))
                if better is not None:
                    return better
            elif outcome.status == 'new':
                on_new(voucher, mac, outcome)
        except Exception:     # a monitor must never break a sign-in
            logger.exception('fit monitor on claim for %s', getattr(voucher, 'code', '?'))
        return outcome
    wrapped._taptap_fit_monitors = True
    return wrapped


def install():
    from . import device_lock
    if not getattr(device_lock.claim, '_taptap_fit_monitors', False):
        device_lock.claim = _wrapped_claim(device_lock.claim)


# ─────────────────────────────── state monitors ───────────────────────────────

def _snapshots(business):
    out = {}
    for r in business.routers.all():
        out[r.pk] = (r, (cache.get(f'tt:tr:users:{r.pk}') or {}).get('users') or {})
    return out


def _vouchers_by_code(business, codes):
    from django.db.models.functions import Upper
    codes = [c for c in codes if c and not str(c).upper().startswith('BYPASS:')]
    if not codes:
        return {}
    qs = business.vouchers.annotate(uc=Upper('code')).filter(uc__in=[str(c).upper() for c in codes], deleted_at__isnull=True)
    return {v.code.upper(): v for v in qs.select_related('router')}


def _routers_ok(monitor, router_id):
    want = [int(x) for x in ((monitor.params or {}).get('routers') or []) if str(x).isdigit()]
    return not want or router_id in want


def check_voucher_online(m, snaps, now):
    codes = {c.upper() for c in _names((m.params or {}).get('codes'))}
    hits = {}
    for rid, (router, users) in snaps.items():
        if not _routers_ok(m, rid):
            continue
        for code in codes & {str(u).upper() for u in users}:
            rows = users.get(code) or next((v for k, v in users.items() if str(k).upper() == code), [])
            hits[code] = (f'{code} is online on {router.name}', f'{len(rows)} device{"s" if len(rows) != 1 else ""} online now.',
                          f'/active-users/', code, (rows[0].get('mac') if rows else ''))
    return hits


def check_sharing(m, snaps, now):
    from . import device_lock
    hits, actions = {}, []
    for rid, (router, users) in snaps.items():
        if not _routers_ok(m, rid):
            continue
        vs = _vouchers_by_code(m.business, list(users))
        for code, v in vs.items():
            if not matches(m, v):
                continue
            rows = next((r for k, r in users.items() if str(k).upper() == code), [])
            macs = {device_lock.norm_mac(r.get('mac')) for r in rows} - {''}
            allowed = device_lock.slots(v)
            if len(macs) > allowed:
                locked = {device_lock.norm_mac(x) for b in v.device_bindings.all() for x in (b.current_mac, b.previous_mac)} - {''}
                extra = sorted(macs - locked) if locked else []
                hits[code] = (f'{_who(v)}: {len(macs)} devices online, {allowed} allowed',
                              f'Plan {v.plan_name or "—"} on {router.name}.' + (f' Not locked to it: {", ".join(extra)}.' if extra else ''),
                              f'/vouchers/{v.pk}/', v, ','.join(extra))
                if m.response == 'disconnect' and extra:
                    actions.append((v, router, extra))
    return hits, actions


def check_heavy(m, snaps, now):
    p = m.params or {}
    limit = float(p.get('threshold') or 20) * 1_000_000
    minutes = int(p.get('minutes') or 10)
    since = dict((m.state or {}).get('heavy_since') or {})
    hits, seen = {}, {}
    for rid, (router, users) in snaps.items():
        if not _routers_ok(m, rid):
            continue
        for code, rows in users.items():
            if str(code).upper().startswith('BYPASS:'):
                continue
            bps = sum(int(r.get('down_bps') or 0) for r in rows)
            if bps < limit:
                continue
            k = str(code).upper()
            seen[k] = since.get(k) or now.timestamp()
            if now.timestamp() - seen[k] >= minutes * 60:
                hits[k] = (f'{k} downloading {bps / 1e6:.1f} Mbit/s for {minutes}+ min', f'On {router.name}. Limit you set: '
                           f'{limit / 1e6:.0f} Mbit/s.', '/traffic/', k, (rows[0].get('mac') if rows else ''))
    return hits, {'heavy_since': seen}


def check_drop(m, snaps, now):
    p = m.params or {}
    pct = max(5, min(95, int(p.get('threshold') or 50)))
    floor = max(1, int(p.get('min_users') or 10))
    hist = dict((m.state or {}).get('online_hist') or {})
    hits = {}
    t = now.timestamp()
    for rid, (router, users) in snaps.items():
        if not _routers_ok(m, rid):
            continue
        n = len([u for u in users if not str(u).upper().startswith('BYPASS:')])
        rows = [x for x in hist.get(str(rid), []) if t - x[0] <= DROP_WINDOW] + [[t, n]]
        hist[str(rid)] = rows[-30:]
        peak = max(x[1] for x in rows)
        if peak >= floor and n <= peak * (100 - pct) / 100:
            hits[f'r{rid}'] = (f'{router.name}: customers online fell from {peak} to {n}',
                               f'A {round(100 - n * 100 / peak)}% drop within 15 minutes — check the uplink, the AP chain or the ISP.',
                               f'/routers/{rid}/', None, '')
    return hits, {'online_hist': hist}


def check_router_offline(m, now):
    minutes = int((m.params or {}).get('minutes') or 5)
    hits = {}
    for r in m.business.routers.all():
        if not _routers_ok(m, r.pk) or r.status == 'Online':
            continue
        if not r.last_watch_at or now - r.last_watch_at >= timedelta(minutes=minutes):
            hits[f'r{r.pk}'] = (f'Router {r.name} offline', f'Not reachable for {minutes}+ minutes. Customers on it may be offline.',
                                f'/routers/{r.pk}/', None, '')
    return hits


def sweep_idle(m, now, dry=False):
    """idle_slots: free slots whose device has not been seen for N days. Returns [(voucher, binding)] handled."""
    from . import device_lock
    from .models import VoucherDeviceBinding
    p = m.params or {}
    days = max(1, int(p.get('days') or 3))
    cutoff = now - timedelta(days=days)
    stale = VoucherDeviceBinding.objects.filter(business=m.business, last_seen_at__lt=cutoff).select_related('voucher', 'voucher__router')
    done = []
    for b in stale[:400]:
        v = b.voucher
        if v.deleted_at or not matches(m, v):
            continue
        if p.get('only_when_full', True) and v.device_bindings.count() < device_lock.slots(v):
            continue
        online = {device_lock.norm_mac(x) for x in ((device_lock.online_hints(v) or {}).get('online_macs') or [])}
        if device_lock.norm_mac(b.current_mac) in online:
            continue
        if not dry:
            try:
                free_one(v, b)
            except Exception:
                logger.exception('idle sweep %s', v.code)
                continue
        done.append((v, b))
        if len(done) >= 50:
            break
    return done


def _fire_new(m, hits, now):
    """Fire for things that became true; things still true stay quiet (re-armed when they stop)."""
    before = set((m.state or {}).get('firing') or [])
    fired = 0
    for key, (title, body, link, obj, mac) in hits.items():
        if key in before:
            continue
        v = obj if obj is not None and hasattr(obj, 'device_bindings') else None
        fire(m, title, body, link, v, mac, action='notified', key=f'mon:{m.pk}:{key}:{now:%Y%m%d%H}')
        fired += 1
    return fired


def evaluate(business, now=None, force=False):
    """Check every active state monitor of one business. Returns how many alerts were sent."""
    from .models_fit import FitMonitor
    now = now or timezone.now()
    if not force and not cache.add(f'fitmon:check:{business.pk}', 1, CHECK_EVERY):
        return 0
    FitMonitor.objects.filter(business=business, active=True, expires_at__lte=now).update(active=False)
    monitors = [m for m in FitMonitor.objects.filter(business=business, active=True).exclude(kind__in=EVENT_KINDS)]
    if not monitors:
        return 0
    snaps = _snapshots(business)
    fired = 0
    for m in monitors:
        m.business = business
        state = dict(m.state or {})
        try:
            if m.kind == 'idle_slots':
                if now.timestamp() - float(state.get('last_sweep') or 0) >= SWEEP_EVERY or force:
                    done = sweep_idle(m, now)
                    state['last_sweep'] = now.timestamp()
                    if done:
                        names = ', '.join(f'{v.code} (slot {b.slot_no})' for v, b in done[:8])
                        fire(m, f'Freed {len(done)} idle slot{"s" if len(done) != 1 else ""}',
                             f'Devices not seen for {int((m.params or {}).get("days") or 3)}+ days: {names}'
                             + ('…' if len(done) > 8 else '') + '. New devices can use these slots now.',
                             '/vouchers/', action='freed_slot', level='info', key=f'mon:{m.pk}:{now:%Y%m%d%H%M}')
                        fired += 1
                hits = {}
            elif m.kind == 'voucher_online':
                hits = check_voucher_online(m, snaps, now)
            elif m.kind == 'sharing':
                hits, actions = check_sharing(m, snaps, now)
                for v, router, extra in actions:
                    from .device_lock import kick
                    key = f'{v.code.upper()}:{",".join(extra)}'
                    if key in set(state.get('kicked') or []):
                        continue
                    for mac in extra:
                        try:
                            kick(router, v, mac, quiet=True)
                        except Exception:
                            logger.exception('monitor kick %s', v.code)
                    state['kicked'] = (list(state.get('kicked') or []) + [key])[-50:]
                    fire(m, f'{_who(v)}: disconnected {len(extra)} extra device{"s" if len(extra) != 1 else ""}',
                         f'Not locked to the voucher: {", ".join(extra)}.', f'/vouchers/{v.pk}/', v, extra[0], action='disconnected',
                         member_text=f'Hello,\n\nYour Wi-Fi account {v.code} was in use on more devices than it allows. '
                                     'The extra devices were disconnected.')
                    fired += 1
            elif m.kind == 'heavy_user':
                hits, extra_state = check_heavy(m, snaps, now)
                state.update(extra_state)
            elif m.kind == 'online_drop':
                hits, extra_state = check_drop(m, snaps, now)
                state.update(extra_state)
            elif m.kind == 'router_offline':
                hits = check_router_offline(m, now)
            else:
                hits = {}
            m.state = state
            fired += _fire_new(m, hits, now)
            state['firing'] = sorted(hits)[:200]
            fields = {'state': state, 'firing': bool(hits), 'last_checked_at': now}
            if m.kind == 'voucher_online' and hits and (m.params or {}).get('once'):
                fields['active'] = False                       # "tell me once when … comes online"
            FitMonitor.objects.filter(pk=m.pk).update(**fields)
        except Exception:
            logger.exception('fit monitor %s', m.pk)
    return fired


def evaluate_all(now=None):
    from .models import Business
    from .models_fit import FitMonitor
    ids = FitMonitor.objects.filter(active=True).exclude(kind__in=EVENT_KINDS).values_list('business_id', flat=True).distinct()
    n = 0
    for b in Business.objects.filter(pk__in=list(ids)):
        if getattr(b, 'has_access', True):
            n += evaluate(b, now)
    return n


# ─────────────────────────────── for Fit and the page ───────────────────────────────

def describe(m):
    p = m.params or {}
    bits = []
    if _names(p.get('plans')):
        bits.append('plans ' + ', '.join(_names(p.get('plans'))))
    if _names(p.get('codes')):
        bits.append('codes ' + ', '.join(_names(p.get('codes'))))
    if p.get('members_only'):
        bits.append('members only')
    units = {'heavy_user': ' Mbit/s', 'online_drop': ' % drop'}
    for k, label, unit in (('threshold', '', units.get(m.kind, '')), ('minutes', 'for ', ' min'), ('days', 'unseen ', ' days'),
                           ('min_users', 'from ', ' users'), ('min_idle_minutes', 'idle ', '+ min')):
        if p.get(k) not in (None, '', False):
            bits.append(f'{label}{p[k]}{unit}')
    if p.get('once'):
        bits.append('once')
    then = dict(m.RESPONSES).get(m.response, m.response)
    who = ('me' if m.audience == 'me' else 'the team') + (' + email' if m.email else '') + (' + the member' if m.notify_member else '')
    return f'{KINDS.get(m.kind, (m.kind,))[0]}' + (f' ({"; ".join(bits)})' if bits else '') + f' → {then}; tell {who}'


def as_json(m):
    return {'id': m.pk, 'name': m.name, 'kind': m.kind, 'what': describe(m), 'active': m.active and not (m.expires_at and m.expires_at <= timezone.now()),
            'response': m.response, 'hits': m.hits, 'firing_now': m.firing,
            'last_fired': timezone.localtime(m.last_fired_at).strftime('%d %b %H:%M') if m.last_fired_at else None,
            'expires': timezone.localtime(m.expires_at).strftime('%d %b %H:%M') if m.expires_at else None}


def dry_run(m):
    """What this monitor would see right now — nothing is sent or changed."""
    now = timezone.now()
    snaps = _snapshots(m.business)
    if m.kind in EVENT_KINDS:
        from . import device_lock
        full = []
        for v in m.business.vouchers.filter(status='active', deleted_at__isnull=True, device_bindings__isnull=False).distinct()[:2000]:
            if matches(m, v) and v.device_bindings.count() >= device_lock.slots(v):
                idle = idle_binding(v, (m.params or {}).get('min_idle_minutes', 20)) if m.response == 'free_slot' else None
                full.append({'code': v.code, 'plan': v.plan_name, 'slots': device_lock.slots(v),
                             **({'idle_slot_to_free': (idle.label or idle.current_mac or f'slot {idle.slot_no}') if idle else None}
                                if m.response == 'free_slot' else {})})
        return {'kind': m.kind, 'full_now': len(full), 'examples': full[:10],
                'note': 'Fires when a new device tries to sign in on one of these (or the next voucher to become full).'}
    if m.kind == 'idle_slots':
        found = sweep_idle(m, now, dry=True)
        return {'kind': m.kind, 'would_free': len(found), 'examples': [f'{v.code} slot {b.slot_no} ({b.label or b.current_mac})' for v, b in found[:10]]}
    if m.kind == 'voucher_online':
        hits = check_voucher_online(m, snaps, now)
    elif m.kind == 'sharing':
        hits, _ = check_sharing(m, snaps, now)
    elif m.kind == 'heavy_user':
        hits, _ = check_heavy(m, snaps, now)
    elif m.kind == 'online_drop':
        hits, _ = check_drop(m, snaps, now)
    elif m.kind == 'router_offline':
        hits = check_router_offline(m, now)
    else:
        hits = {}
    return {'kind': m.kind, 'true_now': len(hits), 'examples': [h[0] for h in list(hits.values())[:10]]}


def slots_report(business, plans=None):
    """Per plan: active vouchers/members, how many are full now, the full ones, and devices refused today."""
    from django.db.models import Count
    from . import device_lock
    want = {x.lower() for x in _names(plans)}
    qs = business.vouchers.filter(status='active', deleted_at__isnull=True).annotate(n=Count('device_bindings'))
    rows = OrderedDict()
    for v in qs.only('id', 'code', 'plan_name', 'max_devices', 'login_type', 'business_id', 'router_id').iterator():
        names = plan_of(v) or {'(no plan)'}
        if want and not (names & want):
            continue
        plan = (v.plan_name or '(no plan)').strip()
        r = rows.setdefault(plan, {'plan': plan, 'active': 0, 'full': 0, 'full_codes': [], 'refused_today': 0})
        r['active'] += 1
        if v.n >= device_lock.slots(v):
            r['full'] += 1
            if len(r['full_codes']) < 8:
                r['full_codes'].append(v.code)
    for plan, r in rows.items():
        r['refused_today'] = refusals_today(business.pk, plan)
    missing = [p for p in _names(plans) if p.lower() not in {k.lower() for k in rows}]
    return {'plans': list(rows.values())[:40], **({'not_found_or_no_active': missing} if missing else {})}
