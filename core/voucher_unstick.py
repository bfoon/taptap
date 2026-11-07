"""Unstick — a voucher TapTap allows but the router keeps cutting off or refusing, fixed in one go and checked.

What people saw
---------------
"Some vouchers disconnect, and when they try to connect again the router does not allow them. Removing the device
slots and a priority sync does not help."

Why
---
TapTap wrote the router's ``limit-uptime`` as an absolute value worked out from ITS copy of the router's uptime
counter. On a busy TapTap Link router that copy can be hours old (an inventory of thousands of users arrives in
pieces, a check-in lists at most 300 sessions), so the limit could land at or below the router's real counter:
the session ended early ("uptime limit reached"), the next login was refused, and every re-send — Reset devices,
priority sync, the login self-heal — wrote the same too-small value again. Nobody could see why: TapTap said
"active, 5 hours left", the router said no.

What this module does
---------------------
* The root cause is fixed in core/agent.py (``rel_limit``): for used vouchers the ROUTER now adds TapTap's time
  left to its own live counter, so the limit is always above what it has counted.
* ``findings(v)`` — "Why can't it connect?": every reason TapTap knows, in plain words — switched off or time up
  in TapTap, router copy missing / disabled / out of time / pinned to another phone, all device slots taken, a
  phone locked out for wrong codes or blocked for Internet sharing, an old disable still on its way, the router's
  own refusal from the login page.
* ``unstick(v, user)`` — the "Fix it" button: Reset devices (slots, warning, locks, router user) PLUS one router
  command that switches the user on, frees any MAC, writes the limit from the router's own counter, ends stuck
  sessions, cookies and hosts, and then READS THE ROUTER BACK. The page shows what the router really has now.
* ``auto_from_portal`` — when the router refuses the same voucher again at the login page, TapTap runs the router
  part by itself (no devices forgotten, no one signed out) once every 10 minutes.

Nothing here lets a voucher online that TapTap would not: deleted, disabled, frozen by staff and time-up vouchers
are reported, never "fixed". Blacklisted phones and blocked IP bindings are reported for a person to decide.
"""
from __future__ import annotations

import logging
import re
import threading

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.utils import timezone

logger = logging.getLogger('taptap.unstick')

ANY_MAC = '00:00:00:00:00:00'
REFUSE_WINDOW = 15 * 60      # refusals counted over this window
REFUSE_TTL = 6 * 3600        # the last refusal is shown this long on the voucher page
AUTO_AFTER = 2               # the router refused this many times in the window → TapTap unsticks by itself
AUTO_EVERY = 10 * 60         # at most one automatic unstick per voucher in this time
LATE_QUEUE_SECONDS = 15 * 60  # a Link router seen this recently is only late: the fix waits for its check-in
SEP = '~'
MAC_RE = re.compile(r'^[0-9A-F]{2}(:[0-9A-F]{2}){5}$')
RUN_INLINE = False           # tests: run the automatic Direct API fix in the request thread


# ─────────────────────────────── the router's refusals (from the login page) ───────────────────────────────

def _rkey(v):
    return f'tt:unstick:refused:{v.pk}'


def note_refusal(v, error, mac=''):
    """The login page told TapTap the router refused this voucher. Returns refusals in the last 15 minutes."""
    now = timezone.now()
    try:
        old = cache.get(_rkey(v)) or {}
        recent = [t for t in old.get('times') or [] if now.timestamp() - float(t) < REFUSE_WINDOW]
        recent.append(now.timestamp())
        cache.set(_rkey(v), {'error': str(error or '')[:200], 'mac': _mac(mac), 'at': now.isoformat(),
                             'times': recent[-10:]}, REFUSE_TTL)
        return len(recent)
    except Exception:
        logger.debug('refusal note for %s', v.code, exc_info=True)
        return 1


def last_refusal(v):
    try:
        return cache.get(_rkey(v))
    except Exception:
        return None


def _mac(m):
    m = re.sub(r'[^0-9A-Fa-f]', '', str(m or '')).upper()
    if len(m) != 12:
        return ''
    m = ':'.join(m[i:i + 2] for i in range(0, 12, 2))
    return '' if m == ANY_MAC else m


def known_macs(v):
    """Phones TapTap knows on this voucher: its device slots and the phone the router last refused."""
    out = []
    try:
        out += [x for x in v.device_bindings.values_list('current_mac', flat=True)]
    except Exception:
        pass
    r = last_refusal(v) or {}
    out.append(r.get('mac'))
    seen, macs = set(), []
    for m in out:
        m = _mac(m)
        if m and m not in seen:
            seen.add(m)
            macs.append(m)
    return macs[:8]


# ─────────────────────────────── "Why can't it connect?" ───────────────────────────────

def _f(level, key, title, text='', fix=''):
    """level: bad (stops it) · warn (can stop it) · ok. fix: auto (the Fix button does it) · you (a person decides)."""
    return {'level': level, 'key': key, 'title': title, 'text': text, 'fix': fix}


def _ago(dt, now):
    s = max(0, int((now - dt).total_seconds()))
    return f'{s} s' if s < 90 else f'{s // 60} min' if s < 5400 else f'{s // 3600} h' if s < 172800 else f'{s // 86400} days'


def _human(seconds):
    from .router_time import routeros_text
    return routeros_text(seconds) if seconds else '0s'


def blocking(v, now=None):
    """The TapTap reason this voucher may not go online, or '' (then the router part can be fixed)."""
    from .voucher_history import time_is_up
    now = now or timezone.now()
    if v.deleted_at:
        return 'it is in the bin — restore it first'
    if v.frozen_at and getattr(v, 'freeze_kind', '') != 'warning':
        return 'it is frozen by staff — press Unfreeze first'
    if v.status in ('disabled', 'archived'):
        return f'it is {v.status} in TapTap — press Enable first'
    if v.status == 'expired' or time_is_up(v, now):
        return 'its time is up — add time first'
    if not v.router_id:
        return 'it has no router'
    return ''


def findings(v, now=None):
    """Everything that stops (or can stop) this voucher from getting online, worst first."""
    from .voucher_history import channel, time_is_up
    now = now or timezone.now()
    out = []

    # ── TapTap itself ──
    if v.deleted_at:
        out.append(_f('bad', 'deleted', 'In the bin', 'Deleted vouchers cannot connect. Restore it from the bin first.', 'you'))
    if v.frozen_at:
        if getattr(v, 'freeze_kind', '') == 'warning':
            out.append(_f('bad', 'warned', 'Warning — internet paused', 'The customer was warned (device sharing). '
                          'Fix it lifts the warning; its time continues.', 'auto'))
        else:
            out.append(_f('bad', 'frozen', 'Frozen by staff', 'Its clock is paused. Press Unfreeze so it can connect.', 'you'))
    if v.status in ('disabled', 'archived'):
        out.append(_f('bad', 'disabled', f'{v.status.capitalize()} in TapTap', 'Press Enable so it can connect.', 'you'))
    elif v.status == 'expired' or time_is_up(v, now):
        out.append(_f('bad', 'time_up', 'Time ran out', 'Add time so it can connect.', 'you'))
    if not v.router_id:
        out.append(_f('bad', 'router', 'No router', 'Choose the router this voucher works on.', 'you'))
        return _sorted(out)

    router = v.router
    via = channel(router)
    try:
        if via == 'TapTap Link':
            from .linklive import link_state
            online, why = link_state(router)
            if not online:
                out.append(_f('warn', 'link', 'Router is not checking in', f'{why} The fix is sent at its next check-in.', ''))
    except Exception:
        pass

    # ── TapTap's copy of the router ──
    state = v.mikrotik_sync_status or 'Pending'
    if state == 'Error':
        out.append(_f('bad', 'sync', 'The router refused to save it', (v.mikrotik_sync_error or 'Sync error')[:200], 'auto'))
    elif state in ('Pending', ''):
        out.append(_f('warn', 'sync', 'Not on the router yet', 'Fix it sends it straight away.', 'auto'))
    elif state == 'Queued':
        out.append(_f('warn', 'sync', 'On its way to the router', 'Waiting for the router to confirm.', 'auto'))
    try:
        from .models import RouterHotspotUser
        from . import router_time as rt
        m = RouterHotspotUser.objects.filter(router_id=v.router_id, username__iexact=v.code).first()
        if m is not None:
            age = f' (router copy from {_ago(m.last_seen_at or m.updated_at, now)} ago)' if (m.last_seen_at or m.updated_at) else ''
            if not m.is_present:
                out.append(_f('bad', 'missing', 'Missing on the router', 'The router does not have this code' + age + '.', 'auto'))
            else:
                if m.disabled:
                    out.append(_f('bad', 'router_off', 'Switched off on the router', 'TapTap allows it, the router has it disabled' + age + '.', 'auto'))
                used, limit = rt.secs(m.uptime), rt.secs(m.limit_uptime)
                if limit and used >= limit and not time_is_up(v, now):
                    out.append(_f('bad', 'router_time', "The router's time counter is used up",
                                  f'Router: used {m.uptime or "0s"} of {m.limit_uptime}{age}. TapTap still has time left. '
                                  'Fix it sets the limit from the router\'s own counter.', 'auto'))
                elif v.status == 'active' and not time_is_up(v, now) and rt.platform_owned(v):
                    plan = _plan(v)
                    if not rt.acceptable(v, m.limit_uptime, m.uptime, plan, now):
                        out.append(_f('warn', 'router_time', 'Router time does not match TapTap',
                                      rt.why(v, m.limit_uptime, m.uptime, plan, now)[:220] + age + '.', 'auto'))
                mac = _mac(m.mac_address)
                if mac:
                    out.append(_f('warn', 'pinned', f'Locked to one phone on the router ({mac})',
                                  'Another phone is refused ("invalid mac address"). Fix it frees it for any phone.', 'auto'))
    except Exception:
        logger.debug('router copy check for %s', v.code, exc_info=True)

    # ── devices ──
    try:
        from . import device_lock
        if device_lock.enabled(v):
            s = device_lock.summary(v)
            if s['total'] and s['used'] >= s['total']:
                out.append(_f('warn', 'slots', f'All {s["total"]} device slot{"s" if s["total"] != 1 else ""} taken',
                              'A new phone is refused. Fix it frees the slots (Reset devices).', 'auto'))
    except Exception:
        pass
    macs = known_macs(v)
    try:
        from .models_voucher_entry import VoucherEntryDevice
        n = VoucherEntryDevice.objects.filter(business=v.business, mac_address__in=macs, blocked_until__gt=now).count() if macs else 0
        if n:
            out.append(_f('bad', 'entry', f'{n} phone{"s" if n != 1 else ""} locked out for wrong codes',
                          'Fix it unblocks them.', 'auto'))
    except Exception:
        pass
    try:
        from .sharing import blocked_message
        blocked = [m for m in macs if blocked_message(v.business, m)]
        if blocked:
            out.append(_f('bad', 'sharing', 'Blocked for Internet sharing', f'{", ".join(blocked)} is blocked for a while. '
                          'Fix it lifts the block for this voucher\'s own phones (a person pressing Fix decides that).', 'auto'))
    except Exception:
        pass
    try:
        from .blacklist import check_login
        bl = [m for m in macs if check_login(v, m, '')]
        if bl:
            out.append(_f('bad', 'blacklist', 'Phone is on the blacklist', f'{", ".join(bl)} — remove it from Security › Blacklist if '
                          'that was a mistake. Fix it does not change the blacklist.', 'you'))
    except Exception:
        pass

    # ── commands still on their way ──
    try:
        from .models import AgentCommand
        pend = 0
        for c in AgentCommand.objects.filter(router_id=v.router_id, status__in=['queued', 'sent'],
                                              kind__in=['hotspot_user_set', 'hotspot_users_disable', 'disconnect']).only('kind', 'params'):
            p = c.params or {}
            if p.get('superseded'):
                continue
            names = [p.get('name'), p.get('user')] + list(p.get('names') or [])
            if any(str(x or '').upper() == v.code.upper() for x in names) and (c.kind == 'disconnect' or p.get('disabled')):
                pend += 1
        if pend:
            out.append(_f('warn', 'pending', f'{pend} old disable / sign-out still on its way to the router',
                          'Fix it cancels them, so they cannot switch it off again.', 'auto'))
    except Exception:
        pass
    try:
        from .models import SessionIncident
        inc = SessionIncident.objects.filter(router_id=v.router_id, username__iexact=v.code, status='open').first()
        if inc:
            out.append(_f('warn', 'incident', 'Session check open', f'{inc.get_reason_display()}: {inc.detail}'[:200], ''))
    except Exception:
        pass

    # ── what the router itself said at the login page ──
    r = last_refusal(v)
    if r and r.get('error'):
        try:
            when = _ago(timezone.datetime.fromisoformat(r['at']), now)
        except Exception:
            when = 'a while'
        n = len(r.get('times') or [])
        out.append(_f('bad', 'refused', f'The router refused the login {when} ago' + (f' ({n}× in 15 min)' if n > 1 else ''),
                      f'Router said: "{r["error"][:120]}"' + (f' · phone {r["mac"]}' if r.get('mac') else ''), 'auto'))
    return _sorted(out)


def _sorted(out):
    order = {'bad': 0, 'warn': 1, 'ok': 2}
    return sorted(out, key=lambda f: order.get(f['level'], 3))


# ─────────────────────────────── the router command (TapTap Link) ───────────────────────────────

def _plan(v):
    return v.business.plans.filter(name__iexact=v.plan_name or '').first() if v.plan_name else None


def params_for(v, free=True, any_mac=True, macs=()):
    from .durations import router_limit
    from .router_time import add_seconds
    from .utils import voucher_profile
    plan = _plan(v)
    prof = ''
    try:
        prof = voucher_profile(v, plan)[0] if v.source == 'taptap' else ''
    except Exception:
        prof = ''
    p = {'name': v.code, 'lim': router_limit(v, plan), 'free': bool(free), 'any': bool(any_mac),
         'macs': [m for m in (_mac(x) for x in macs) if m][:8]}
    if prof:
        p['prof'] = prof
    add = add_seconds(v, plan)
    if add:
        p['add'] = add
    return p


def validate(p):
    from .agent import NAME_RE
    from .plan_limits import LIM_RE
    if not NAME_RE.match(str(p.get('name', ''))):
        raise ValueError('Invalid voucher name.')
    if p.get('lim') and not LIM_RE.match(str(p['lim'])):
        raise ValueError('Invalid time limit.')
    if p.get('prof') and not re.match(r'^[\w .@:+/-]{1,120}$', str(p['prof'])):
        raise ValueError('Invalid profile.')
    if not isinstance(p.get('macs') or [], list) or not all(MAC_RE.match(str(m)) for m in p.get('macs') or []):
        raise ValueError('Invalid MAC list.')
    if p.get('add') is not None:
        int(p['add'])


def link_body(cmd, url, check, nonce_value):
    """One RouterOS script: switch the user on, free it, write its limit from the router's own counter, end stuck
    sessions, then report what the router really has now (read back, not assumed)."""
    from .agent import DROP_DEVICE, free_login, rel_add, rel_limit, rs
    p = cmd.params or {}
    n = rs(p['name'])
    sets = 'disabled=no' + (f' mac-address={ANY_MAC}' if p.get('any') else '') + \
        (f' profile={rs(p["prof"])}' if p.get('prof') else '') + (f' limit-uptime={rs(p["lim"])}' if p.get('lim') else '')
    add = rel_add(p)
    macs = ';'.join(rs(m) for m in p.get('macs') or [])
    report = rs(f'{url}/api/agent/v1/ack?c={cmd.pk}&n={nonce_value}&s=ok&r=')
    free = ''
    if p.get('free'):
        # this voucher's sessions, cookies and hosts, and the phones TapTap knows on it: they start clean
        free = (f':set fr [:len [/ip hotspot active find user={n}]]; {free_login(n)}; '
                ':foreach m in=$ms do={ $drop m=$m }; ')
    return (
        '{ ' + DROP_DEVICE + ':local out "u=0"; :local fr 0; :local bl 0; :local sh ""; '
        + (f':local ms {{{macs}}}; ' if macs else ':local ms [:toarray ""]; ') +
        f':foreach a in=[/ip hotspot active find user={n}] do={{ :set ms ($ms, [/ip hotspot active get $a mac-address]) }}; '
        f':foreach c in=[/ip hotspot cookie find user={n}] do={{ :set ms ($ms, [/ip hotspot cookie get $c mac-address]) }}; '
        ':foreach m in=$ms do={ :do { :set bl ($bl + [:len [/ip hotspot ip-binding find mac-address=$m type=blocked]]) } on-error={} }; '
        f':local ids [/ip hotspot user find name={n}]; '
        ':if ([:len $ids] > 0) do={ :local i [:pick $ids 0]; '
        f'/ip hotspot user set $i {sets}; '
        + (f'{rel_limit(n, add)}; ' if add else '')
        + free +
        ':delay 1s; '
        ':do { :set sh [/ip hotspot user profile get [find name=[/ip hotspot user get $i profile]] shared-users] } on-error={}; '
        f':set out ("u=1{SEP}up=" . [/ip hotspot user get $i uptime] . "{SEP}lim=" . [/ip hotspot user get $i limit-uptime] . '
        f'"{SEP}dis=" . [/ip hotspot user get $i disabled] . "{SEP}mac=" . [/ip hotspot user get $i mac-address] . '
        f'"{SEP}act=" . [:len [/ip hotspot active find user={n}]] . "{SEP}fr=" . $fr . "{SEP}bl=" . $bl . "{SEP}sh=" . $sh) }}; '
        f':do {{ /tool fetch url=({report} . $out) output=none check-certificate={check} duration=8s idle-timeout=5s }} on-error={{}} }}'
    )


def read_state(text):
    """The router's read-back ("u=1~up=…~lim=…") → dict, or None when the command carried no report."""
    text = str(text or '')
    if not text.startswith('u='):
        return None
    out = {}
    for part in text.split(SEP):
        k, _, val = part.partition('=')
        out[k.strip()] = val.strip()
    return out


def verdict(v, state, now=None):
    """(ok, lines) — plain words about what the router has now."""
    from . import router_time as rt
    from .voucher_history import time_is_up
    now = now or timezone.now()
    if not state:
        return None, ['The router ran the fix but did not send its read-back.']
    if state.get('u') != '1':
        return False, ['The router does not have this code. TapTap is sending it again — try the login in a minute.']
    lines, ok = [], True
    if state.get('dis') in ('true', 'yes'):
        ok = False
        lines.append('Still switched off on the router.')
    else:
        lines.append('Switched on.')
    mac = _mac(state.get('mac'))
    lines.append(f'Locked to {mac}.' if mac else 'Any phone may use it.')
    up, lim = rt.secs(state.get('up')), rt.secs(state.get('lim'))
    if lim <= 0:
        lines.append('No time limit on the router (TapTap still ends it on time).')
    elif up >= lim:
        ok = False
        lines.append(f'The router\'s time is used up ({state.get("up")} of {state.get("lim")}).')
    else:
        left = lim - up
        try:
            sl = rt.slots(v)
        except Exception:
            sl = 1
        lines.append(f'The router allows {_human(left)} more' + (f' across {sl} devices' if sl > 1 else '') +
                     f' (used {state.get("up") or "0s"} of {state.get("lim")}).')
        rem = rt.remaining_s(v, None, now)
        if rem and not time_is_up(v, now) and left + rt.TOLERANCE_S < rem:
            ok = False
            lines.append(f'That is less than TapTap\'s {_human(rem)} left — press Fix it again.')
    try:
        fr, act, bl = int(state.get('fr') or 0), int(state.get('act') or 0), int(state.get('bl') or 0)
    except ValueError:
        fr = act = bl = 0
    if fr:
        lines.append(f'{fr} stuck session{"s" if fr != 1 else ""} ended — the phone signs in fresh.')
    if act:
        lines.append(f'{act} session{"s" if act != 1 else ""} online now.')
    if state.get('sh'):
        lines.append(f'Its router profile allows {state["sh"]} device{"s" if state["sh"] != "1" else ""} at once.')
    if bl:
        ok = False
        lines.append(f'{bl} of its phones is BLOCKED in Hotspot › IP Bindings on the router — remove that binding if it was a mistake.')
    return ok, lines


# ─────────────────────────────── Direct API / TapTap Tunnel ───────────────────────────────

def _api(v, free=True, any_mac=True, macs=()):
    """Same fix over the RouterOS API; returns the read-back in the Link report's shape."""
    from . import router_time as rt
    from .mikrotik import MikroTikService
    from .utils import voucher_profile
    p = params_for(v, free, any_mac, macs)
    plan = _plan(v)
    svc = MikroTikService(v.router, timeout=getattr(settings, 'MIKROTIK_LIVE_TIMEOUT', 5)).connect()
    try:
        users = svc.resource('/ip/hotspot/user')
        rows = users.get(name=v.code)
        if not rows:
            profile, shared, rate = voucher_profile(v, plan)
            svc.ensure_hotspot_profile(profile, shared, rate)
            svc.upsert_voucher(v.code, profile, limit_uptime=p['lim'], password=v.login_password, disabled=False,
                               comment=f'TapTap {"member" if v.is_member else "voucher"} {v.code}')
            rows = users.get(name=v.code)
        if not rows:
            return {'u': '0'}
        row = rows[0]
        active = svc.safe_get('/ip/hotspot/active', user=v.code) or []
        data = {'disabled': 'no'}
        if p.get('any'):
            data['mac_address'] = ANY_MAC
        if p.get('prof'):
            data['profile'] = p['prof']
        if p.get('add'):     # the router's own counter, read just now, + time left
            counted = rt.secs(row.get('uptime', '')) + sum(rt.secs(a.get('uptime', '')) for a in active)
            data['limit_uptime'] = rt.routeros_text(counted + int(p['add']))
        elif p.get('lim'):
            data['limit_uptime'] = p['lim']
        users.set(id=row['id'], **data)
        want = {_mac(a.get('mac-address')) for a in active} | set(p.get('macs') or [])
        want.discard('')
        bl = 0
        try:
            bl = sum(1 for b in svc.safe_get('/ip/hotspot/ip-binding') or []
                     if str(b.get('type', '')) == 'blocked' and _mac(b.get('mac-address')) in want)
        except Exception:
            pass
        fr = 0
        if p.get('free'):
            fr = len(active)
            svc.release_devices(v.code, list(want))
        row = (users.get(name=v.code) or [row])[0]
        sh = ''
        try:
            pr = svc.safe_get('/ip/hotspot/user/profile', name=row.get('profile', '')) or []
            sh = str(pr[0].get('shared-users', '')) if pr else ''
        except Exception:
            pass
        rt.note_uptime(v.router_id, v.code, row.get('uptime', ''))
        return {'u': '1', 'up': str(row.get('uptime', '') or '0s'), 'lim': str(row.get('limit-uptime', '') or '0s'),
                'dis': str(row.get('disabled', 'false')), 'mac': str(row.get('mac-address', '') or ANY_MAC),
                'act': str(len(svc.safe_get('/ip/hotspot/active', user=v.code) or [])), 'fr': str(fr), 'bl': str(bl), 'sh': sh}
    finally:
        svc.close()


# ─────────────────────────────── the fix ───────────────────────────────

def _send_link(v, params, label, user=None):
    """Queue the router part on TapTap Link. A router that is only late still gets it (runs at its check-in)."""
    from . import agent as link
    from .linkops import send
    try:
        cmd = send(v.router, 'voucher_unstick', params, label=label, user=user, minutes=30)
    except ValueError as exc:
        a = getattr(v.router, 'agent', None)
        if not a or a.revoked or not a.last_seen_at or (timezone.now() - a.last_seen_at).total_seconds() > LATE_QUEUE_SECONDS:
            raise
        cmd = link.queue(v.router, 'voucher_unstick', params, label=label, user=user, minutes=30)
        logger.info('unstick %s queued for a late router: %s', v.code, exc)
    link.mark_urgent(v.router)
    return cmd


def _lift_sharing(v, user, macs):
    """A person pressed Fix: lift Internet-sharing blocks on this voucher's own phones."""
    lifted = []
    try:
        from .models_sharing import SharingCase
        from .sharing import blocked_message
        for m in macs:
            if blocked_message(v.business, m):
                cache.delete(f'tt:share:block:{v.business_id}:{m}')
                SharingCase.objects.filter(business=v.business, mac=m, status='blocked').update(
                    status='cleared', action_taken='lifted (voucher fix)',
                    handled_by=user if getattr(user, 'is_authenticated', False) else None)
                lifted.append(m)
    except Exception:
        logger.exception('lift sharing block for %s', v.code)
    return lifted


def unstick(v, user=None, reason=''):
    """The Fix-it button (a person). Returns {ok, via, cmd, state, lines, notes, findings}."""
    from . import voucher_history as vh
    from .utils import log
    before = findings(v)
    stop = blocking(v)
    if stop:
        return {'ok': False, 'via': vh.channel(v.router), 'message': f'{v.code} cannot be fixed yet: {stop}.',
                'findings': before, 'lines': [], 'notes': []}
    macs = known_macs(v)                         # read before Reset devices forgets the slots
    notes = []
    lifted = _lift_sharing(v, user, macs)
    if lifted:
        notes.append(f'sharing block lifted for {", ".join(lifted)}')
    # 1. Reset devices, done properly (core/voucher_fastlane.heal): slots, warning, wrong-code locks, old commands,
    #    router user re-sent (with the limit from the router's own counter).
    ok, result = vh.reset_devices(v, user, (reason or 'Voucher fix — could not connect')[:255])
    v.refresh_from_db()
    if result:
        notes.append(result)
    via = vh.channel(v.router)
    cache.delete(_rkey(v))
    try:
        from .voucher_ready import unconfirm
        unconfirm([v.pk])
    except Exception:
        pass
    out = {'ok': ok, 'via': via, 'notes': notes, 'findings': before, 'lines': [], 'state': None, 'cmd': None}
    if blocking(v):                               # still not allowed (e.g. a staff freeze found by the reset)
        out.update(ok=False, message=f'{v.code}: {blocking(v)}.')
        return out
    # 2. The router part, read back.
    params = params_for(v, free=True, any_mac=not _permanent(v), macs=macs)
    if via == 'TapTap Link':
        try:
            cmd = _send_link(v, params, f'Fix {v.code}: switch on, free, time from the router\'s counter, check', user)
            out.update(cmd=cmd.pk, waiting=True, message=f'Fixing {v.code} — the router answers at its next check-in.')
        except ValueError as exc:
            out.update(ok=False, message=f'{v.code} was reset in TapTap, but the router cannot be reached: {exc}')
    else:
        try:
            state = _api(v, free=True, any_mac=params['any'], macs=macs)
            good, lines = verdict(v, state)
            out.update(state=state, lines=lines, ok=bool(good), message=f'{v.code}: ' + ('fixed on the router.' if good else 'see below.'))
            vh.record(v, 'note', user=user, via=via, reason='Voucher fix', router_result=' '.join(lines)[:255],
                      text='Fix it: router user checked and set right')
        except Exception as exc:                  # noqa: BLE001 — router API errors come in many types
            out.update(ok=False, message=f'{v.code} was reset in TapTap, but the router could not be reached: {exc}'[:300])
    log(v.business, 'Voucher Fixed', f'{v.code}: {out.get("message", "")}'[:250], router=v.router)
    return out


def _permanent(v):
    try:
        from .voucher_ready import _permanent as perm
        return perm(v)
    except Exception:
        return False


def status(v, cmd_id):
    """The page polls this while a TapTap Link fix waits for the router."""
    from .models import AgentCommand
    cmd = AgentCommand.objects.filter(pk=cmd_id, router_id=v.router_id, kind='voucher_unstick').first()
    if cmd is None:
        return {'status': 'gone', 'lines': ['That fix is no longer on the TapTap Link page.']}
    if cmd.status in ('queued', 'sent'):
        return {'status': 'waiting', 'lines': ['Waiting for the router to check in…' if cmd.status == 'queued' else 'The router is running the fix…']}
    if cmd.status != 'done':
        return {'status': 'failed', 'lines': [f'The router did not run it ({cmd.status}): {cmd.result or "no answer"}'[:240]]}
    state = read_state(cmd.result)
    good, lines = verdict(v, state)
    if state and cache.add(f'tt:unstick:noted:{cmd.pk}', 1, 86400):
        from .voucher_history import record
        record(v, 'note', user=cmd.created_by, via='TapTap Link', reason='Voucher fix', router_result=' '.join(lines)[:255],
               text='Fix it: the router read back')
        try:
            from .router_time import note_uptime
            note_uptime(v.router_id, v.code, state.get('up', ''))
        except Exception:
            pass
    return {'status': 'done', 'ok': good, 'state': state, 'lines': lines}


def auto_from_portal(v, error, mac='', kind='user'):
    """The router refused a login TapTap allows. On the second refusal in 15 minutes (or a used-up router
    counter at once) TapTap runs the router part by itself. Nobody is signed out, no device is forgotten.
    Returns True when a fix was started."""
    count = note_refusal(v, error, mac)
    text = str(error or '').lower()
    counter = 'uptime' in text or 'limit' in text
    if kind in ('radius', 'busy') or (count < AUTO_AFTER and not counter) or blocking(v):
        return False
    if not cache.add(f'tt:unstick:auto:{v.pk}', 1, AUTO_EVERY):
        return False
    from .voucher_history import channel
    macs = known_macs(v)
    params = params_for(v, free=False, any_mac=(kind == 'mac' and not _permanent(v)), macs=macs)
    via = channel(v.router)
    if via == 'TapTap Link':
        try:
            _send_link(v, params, f'Login fix {v.code}: router refused it {count}× — time from the router\'s counter')
        except ValueError:
            return False
    else:
        inline = RUN_INLINE or getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False)

        def run():
            try:
                _api(v, free=False, any_mac=params['any'], macs=macs)
            except Exception as exc:          # noqa: BLE001
                logger.info('auto unstick %s: %s', v.code, exc)
            finally:
                if not inline:
                    connection.close()
        if inline:
            run()
        else:
            threading.Thread(target=run, name=f'tt-unstick-{v.pk}', daemon=True).start()
    try:
        from .voucher_history import record
        record(v, 'note', source='auto', via=via, reason='Ready to browse',
               text=f'Router refused the login {count}× ("{str(error)[:60]}") — TapTap set its time from the router\'s own '
                    'counter and switched it on')
    except Exception:
        pass
    return True
