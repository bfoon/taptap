"""Fair usage enforcement.

After every live sync (Direct API) and every TapTap Link session report, for each connected
customer:
  1. find the policy for its plan (a policy that names the plan wins over an "all plans" one);
  2. add up the data it used in the current period (day / week / whole voucher) from the
     hourly usage TapTap already records — skipping the policy's free night hours;
  3. pick the tier (how many steps of the staircase it has passed);
  4. put a speed cap on each of its session IPs on the router (a simple queue named
     "TTFUP-<code>-<ip>", placed at the top so it wins over the hotspot's own queue),
     or take it off.
Caps exist only while the session is online: when a session ends the cap on that IP is
removed, so a DHCP address given to someone else is never slowed by mistake. At the start
of a new period everyone is back to full speed.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, time, timedelta

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('taptap.fup')

GB = 1024 ** 3
IP_RE = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')
QNAME_RE = re.compile(r'^TTFUP-[A-Za-z0-9._@-]{1,80}$')
LIMIT_RE = re.compile(r'^\d{2,7}k/\d{2,7}k$')
MIN_KBPS = 32
STATE_TTL = 86400


# ─────────────────────────── policy & usage ───────────────────────────

def clean_tiers(rows):
    """[{gb, down, up}] with numbers, positive, sorted by gb, at most 6 steps."""
    out = []
    for r in rows or []:
        try:
            gb, down = float(r.get('gb')), float(r.get('down'))
            up = float(r.get('up') or down)
        except (TypeError, ValueError, AttributeError):
            continue
        if gb > 0 and down > 0 and up > 0:
            out.append({'gb': round(gb, 3), 'down': round(down, 3), 'up': round(up, 3)})
    out.sort(key=lambda t: t['gb'])
    return out[:6]


def kbps(mbps):
    return max(MIN_KBPS, int(round(float(mbps) * 1000)))


def speed_text(mbps):
    mbps = float(mbps)
    return f'{mbps:g} Mb/s' if mbps >= 1 else f'{int(round(mbps * 1000))} kb/s'


def policies(business):
    return list(business.fair_usage_policies.filter(active=True).prefetch_related('plans'))


def policy_for(voucher, pols):
    """The plan's own policy first, then an 'all plans' policy."""
    plan = (voucher.plan_name or '').lower()
    for p in pols:
        if any(x.name.lower() == plan for x in p.plans.all()):
            return p
    for p in pols:
        if not p.plans.all():
            return p
    return None


def window(policy, voucher, now=None):
    """(start, period_key, resets_at) of the current period."""
    now = timezone.localtime(now or timezone.now())
    tz = timezone.get_current_timezone()
    if policy.period == 'day':
        start = timezone.make_aware(datetime.combine(now.date(), time.min), tz)
        return start, f'd{now:%Y%m%d}', start + timedelta(days=1)
    if policy.period == 'week':
        monday = now.date() - timedelta(days=now.weekday())
        start = timezone.make_aware(datetime.combine(monday, time.min), tz)
        return start, f'w{monday:%Y%m%d}', start + timedelta(days=7)
    from .voucher_history import ends_at
    return voucher.used_at or voucher.created_at, 'v', ends_at(voucher)


def codes_of(voucher):
    from .models import VoucherCodeAlias
    return [voucher.code] + list(VoucherCodeAlias.objects.filter(voucher=voucher).values_list('code', flat=True))


def used_bytes(voucher, policy, now=None):
    """Data this voucher used in the current period (free night hours left out)."""
    from .models import UsageRecord
    start, _, _ = window(policy, voucher, now)
    start = start.replace(minute=0, second=0, microsecond=0)   # usage is stored per hour
    rows = UsageRecord.objects.filter(business_id=voucher.business_id, username__in=codes_of(voucher), hour__gte=start)
    total = 0
    for hour, down, up in rows.values_list('hour', 'download', 'upload'):
        if policy.is_free_hour(timezone.localtime(hour).hour):
            continue
        total += down + (up if policy.counts == 'total' else 0)
    return total


def tier_for(used, tiers):
    n = 0
    for t in tiers:
        if used >= t['gb'] * GB:
            n += 1
    return n


def status(voucher, now=None):
    """Everything the voucher page shows, or None when no policy covers it."""
    from .models_fup import FairUsageState
    pol = policy_for(voucher, policies(voucher.business))
    if not pol or not pol.tiers:
        return None
    now = now or timezone.now()
    used = used_bytes(voucher, pol, now)
    start, key, resets = window(pol, voucher, now)
    st = FairUsageState.objects.filter(voucher=voucher, policy=pol).first()
    lifted = bool(st and st.lifted_until and st.lifted_until > now)
    tier = 0 if lifted else tier_for(used, pol.tiers)
    nxt = pol.tiers[tier] if tier < len(pol.tiers) else None
    sessions = live_sessions(voucher)
    down_bps, up_bps = sum(s.get('down_bps', 0) for s in sessions), sum(s.get('up_bps', 0) for s in sessions)
    cap = pol.tiers[tier - 1] if tier else None
    cap_down = kbps(cap['down']) * 1000 * max(1, len(sessions)) if cap else 0
    live = {'online': bool(sessions), 'devices': len(sessions), 'down_now': bps_text(down_bps), 'up_now': bps_text(up_bps),
            'down_pct': min(100, round(down_bps * 100 / cap_down)) if cap_down else 0,
            'sessions': [{**s, 'down': bps_text(s.get('down_bps')), 'up': bps_text(s.get('up_bps'))} for s in sessions],
            'peak': bps_text(_peak_bps(voucher, start))}
    return {'live': live, 'policy': pol, 'used': used, 'used_gb': used / GB, 'tier': tier, 'lifted': lifted, 'lifted_until': st.lifted_until if lifted else None,
            'speed': {'down': speed_text(pol.tiers[tier - 1]['down']), 'up': speed_text(pol.tiers[tier - 1]['up'])} if tier else None,
            'next': {'gb': nxt['gb'], 'down': speed_text(nxt['down'])} if nxt else None, 'resets_at': resets, 'period_start': start,
            'steps': [{**t, 'down_text': speed_text(t['down']), 'up_text': speed_text(t['up']), 'reached': used >= t['gb'] * GB,
                       'current': i + 1 == tier} for i, t in enumerate(pol.tiers)]}


def lift(voucher, user=None, now=None):
    """Staff give full speed back until the period resets (a whole-voucher policy: for 24 hours)."""
    from .models_fup import FairUsageState
    from .voucher_history import record
    now = now or timezone.now()
    pol = policy_for(voucher, policies(voucher.business))
    if not pol:
        return None
    _, key, resets = window(pol, voucher, now)
    until = resets if pol.period != 'voucher' or not resets else min(resets, now + timedelta(days=1))
    until = until or now + timedelta(days=1)
    st, _ = FairUsageState.objects.get_or_create(voucher=voucher, policy=pol, defaults={'period_key': key})
    st.lifted_until, st.lifted_by = until, user if getattr(user, 'is_authenticated', False) else None
    st.save(update_fields=['lifted_until', 'lifted_by', 'updated_at'])
    record(voucher, 'fup_lifted', user=user, reason='Full speed given back', text=f'Until {timezone.localtime(until):%d %b %H:%M} ({pol.name})')
    return until


def unlift(voucher, user=None):
    from .models_fup import FairUsageState
    from .voucher_history import record
    n = FairUsageState.objects.filter(voucher=voucher, lifted_until__isnull=False).update(lifted_until=None, lifted_by=None)
    if n:
        record(voucher, 'fup_restored', user=user, reason='Fair usage limits back on')
    return n


# ─────────────────────────── enforcement ───────────────────────────

def _qname(code, ip):
    return f'TTFUP-{re.sub(r"[^A-Za-z0-9._@-]", "", code)[:40]}-{ip}'


def desired_caps(router, active, now=None):
    """{queue name: (ip, 'upk/downk', code, tier)} for this router's connected customers."""
    from .models import Voucher
    from .models_fup import FairUsageState
    from .voucher_history import record
    now = now or timezone.now()
    pols = policies(router.business)
    if not pols:
        return {}
    by_code = {}
    for s in active:
        code, ip = str(s.get('user', '')).strip(), str(s.get('address', '')).strip()
        if code and IP_RE.match(ip):
            by_code.setdefault(code.upper(), set()).add(ip)
    if not by_code:
        return {}
    vouchers = {v.code.upper(): v for v in Voucher.objects.filter(business=router.business, code__in=[c for c in by_code] + [c.lower() for c in by_code])}
    from .models import VoucherCodeAlias
    for a in VoucherCodeAlias.objects.filter(business=router.business, code__in=list(by_code)).select_related('voucher'):
        vouchers.setdefault(a.code.upper(), a.voucher)
    caps = {}
    for code, ips in by_code.items():
        v = vouchers.get(code)
        if not v or v.deleted_at:
            continue
        pol = policy_for(v, pols)
        if not pol or not pol.tiers:
            continue
        used = used_bytes(v, pol, now)
        _, key, _ = window(pol, v, now)
        st, _ = FairUsageState.objects.get_or_create(voucher=v, policy=pol, defaults={'period_key': key})
        if st.period_key != key:     # new day / week: full speed, lift forgotten
            st.period_key, st.lifted_until, st.lifted_by = key, None, None
        lifted = bool(st.lifted_until and st.lifted_until > now)
        tier = 0 if lifted else tier_for(used, pol.tiers)
        if tier != st.tier:
            if tier > st.tier:
                t = pol.tiers[tier - 1]
                record(v, 'fup_slowed', source='auto', reason=pol.name,
                       text=f'Used {used / GB:.2f} GB — speed now {speed_text(t["down"])} down / {speed_text(t["up"])} up')
            else:
                record(v, 'fup_restored', source='auto', reason=pol.name, text='Back to full speed' if not tier else f'Step {tier}')
            st.tier, st.changed_at = tier, now
        st.used_bytes = used
        st.save()
        if tier:
            t = pol.tiers[tier - 1]
            for ip in sorted(ips):
                caps[_qname(v.code, ip)] = (ip, f'{kbps(t["up"])}k/{kbps(t["down"])}k', v.code, tier)
    return caps


def enforce(router, active, now=None, svc=None):
    """Bring the router's fair-usage caps in line with who is online now. Returns a small summary."""
    want = desired_caps(router, active, now)
    key = f'tt:fup:{router.pk}'
    had = cache.get(key) or {}
    to_set = {n: c for n, c in want.items() if list(had.get(n) or []) != [c[0], c[1]]}
    to_remove = [n for n in had if n not in want]
    if not to_set and not to_remove:
        return {'capped': len(want)}
    try:
        if svc is not None:
            _apply_api(svc, to_set, to_remove)
        else:
            _apply_link(router, to_set, to_remove)
    except Exception as exc:
        logger.info('fair usage on %s: %s', router, exc)
        return {'capped': len(had), 'error': str(exc)[:200]}
    cache.set(key, {n: [c[0], c[1]] for n, c in want.items()}, STATE_TTL)
    return {'capped': len(want), 'set': len(to_set), 'removed': len(to_remove)}


def _apply_api(svc, to_set, to_remove):
    queues = svc.resource('/queue/simple')
    for name, (ip, limit, code, tier) in to_set.items():
        found = queues.get(name=name)
        if found:
            queues.set(id=found[0]['id'], target=f'{ip}/32', max_limit=limit)
            continue
        fields = {'name': name, 'target': f'{ip}/32', 'max_limit': limit, 'comment': f'TapTap fair usage: {code} step {tier}'}
        first = queues.get()
        if first:
            try:
                queues.add(place_before=first[0]['id'], **fields)   # top of the list: wins over the hotspot queue
                continue
            except Exception:
                pass
        queues.add(**fields)
    for name in to_remove:
        for row in queues.get(name=name):
            queues.remove(id=row['id'])


def _apply_link(router, to_set, to_remove):
    from .linkops import send
    sets = [[n, c[0], c[1]] for n, c in to_set.items()]
    for i in range(0, max(len(sets), len(to_remove), 1), 40):
        chunk_set, chunk_rm = sets[i:i + 40], to_remove[i:i + 40]
        if chunk_set or chunk_rm:
            send(router, 'fup_queues', {'set': chunk_set, 'remove': chunk_rm},
                 label=f'Fair usage: {len(chunk_set)} slowed, {len(chunk_rm)} back to full speed', minutes=10)


def clear_router(router, svc=None):
    """Remove every fair-usage cap from a router (policy switched off or deleted)."""
    key = f'tt:fup:{router.pk}'
    had = cache.get(key) or {}
    if had:
        (_apply_api(svc, {}, list(had)) if svc is not None else _apply_link(router, {}, list(had)))
        cache.delete(key)


def link_script(p):
    """RouterOS commands for a TapTap Link 'fup_queues' command (validated before sending)."""
    from .agent import rs
    out = []
    for name, ip, limit in p.get('set', []):
        n, t, l = rs(name), f'{ip}/32', rs(limit)
        out.append(f':if ([:len [/queue simple find name={n}]] = 0) do={{ '
                   f':do {{ /queue simple add name={n} target={t} max-limit={l} comment="TapTap fair usage" place-before=0 }} '
                   f'on-error={{ /queue simple add name={n} target={t} max-limit={l} comment="TapTap fair usage" }} }} '
                   f'else={{ /queue simple set [find name={n}] target={t} max-limit={l} }}')
    if p.get('remove'):
        names = ';'.join(rs(n) for n in p['remove'])
        out.append(f':foreach n in={{{names}}} do={{ :do {{ /queue simple remove [find name=$n] }} on-error={{}} }}')
    return '\n'.join(out) or ':log info "TapTap fair usage: nothing to change"'


def validate_link_params(p):
    for row in p.get('set', []):
        if not (isinstance(row, (list, tuple)) and len(row) == 3 and QNAME_RE.match(str(row[0]))
                and IP_RE.match(str(row[1])) and LIMIT_RE.match(str(row[2]))):
            raise ValueError('Invalid fair usage cap.')
    if not all(QNAME_RE.match(str(n)) for n in p.get('remove', [])):
        raise ValueError('Invalid fair usage queue name.')
    if len(p.get('set', [])) > 40 or len(p.get('remove', [])) > 40:
        raise ValueError('Too many changes in one command.')


# ─────────────────────────── who is slowed, and how fast they go ───────────────────────────

def bps_text(bps):
    bps = float(bps or 0)
    if bps >= 1_000_000:
        return f'{bps / 1_000_000:.1f} Mb/s'
    if bps >= 1000:
        return f'{bps / 1000:.0f} kb/s'
    return f'{int(bps)} b/s'


def live_sessions(voucher, routers=None):
    """Sessions of this voucher online now with their current speed, from the last live sync."""
    from .models import VoucherCodeAlias
    codes = {voucher.code.upper()} | {c.upper() for c in VoucherCodeAlias.objects.filter(voucher=voucher).values_list('code', flat=True)}
    out = []
    for r in routers if routers is not None else voucher.business.routers.all():
        snap = cache.get(f'tt:tr:users:{r.pk}') or {}
        for code in codes:
            for s in (snap.get('users') or {}).get(code, []):
                out.append({**s, 'router': r.name, 'at': snap.get('at')})
    return out


def _peak_bps(voucher, since):
    from django.db.models import Max
    from .models import UsageRecord
    return UsageRecord.objects.filter(business_id=voucher.business_id, username__in=codes_of(voucher), hour__gte=since).aggregate(m=Max('peak_bps'))['m'] or 0


def bandwidth(voucher, st, now=None):
    """Cap, live speed against the cap, and peak for one slowed voucher."""
    pol = st.policy
    t = pol.tiers[st.tier - 1] if 0 < st.tier <= len(pol.tiers) else None
    sessions = live_sessions(voucher)
    down = sum(s.get('down_bps', 0) for s in sessions)
    up = sum(s.get('up_bps', 0) for s in sessions)
    cap_down = kbps(t['down']) * 1000 * max(1, len(sessions)) if t else 0   # the cap is per device
    cap_up = kbps(t['up']) * 1000 * max(1, len(sessions)) if t else 0
    start, _, resets = window(pol, voucher, now)
    return {
        'cap': {'down': speed_text(t['down']), 'up': speed_text(t['up'])} if t else None,
        'online': bool(sessions), 'sessions': [{**s, 'down': bps_text(s.get('down_bps')), 'up': bps_text(s.get('up_bps'))} for s in sessions],
        'down_now': bps_text(down), 'up_now': bps_text(up),
        'down_pct': min(100, round(down * 100 / cap_down)) if cap_down else 0, 'up_pct': min(100, round(up * 100 / cap_up)) if cap_up else 0,
        'peak': bps_text(_peak_bps(voucher, start)), 'resets_at': resets,
    }


def slowed(business, now=None):
    """Every voucher slowed down right now, with its policy step, cap, live speed and data used."""
    from datetime import timedelta
    from .models_fup import FairUsageState
    now = now or timezone.now()
    rows = []
    qs = (FairUsageState.objects.filter(voucher__business=business, voucher__deleted_at__isnull=True, tier__gt=0,
                                        updated_at__gte=now - timedelta(minutes=30), policy__active=True)
          .select_related('voucher', 'voucher__router', 'voucher__agent', 'policy').order_by('-tier', '-used_bytes'))
    for st in qs:
        if st.lifted_until and st.lifted_until > now:
            continue
        v, pol = st.voucher, st.policy
        if st.tier > len(pol.tiers):
            continue
        nxt = pol.tiers[st.tier] if st.tier < len(pol.tiers) else None
        rows.append({'state': st, 'voucher': v, 'policy': pol, 'tier': st.tier, 'steps': len(pol.tiers),
                     'used': st.used_bytes, 'used_gb': st.used_bytes / GB, 'since': st.changed_at,
                     'next': {'gb': nxt['gb'], 'down': speed_text(nxt['down'])} if nxt else None,
                     **bandwidth(v, st, now)})
    return rows
