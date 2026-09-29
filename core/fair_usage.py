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
Shared vouchers (more than one device) are measured PER DEVICE: every device gets the policy's
allowance, and only the device that used it is slowed — the other devices on the voucher keep
full speed. A device is followed across MAC changes through its sticky-voucher slot.
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
    for p in pols:   # by plan name, or by the router profile the plan maps to (vouchers imported from the router)
        if any(plan and plan in {x.name.lower(), (x.mikrotik_profile_name or '').lower()} for x in p.plans.all()):
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


def used_bytes(voucher, policy, now=None, macs=None):
    """Data this voucher used in the current period (free night hours left out).
    With `macs`, only what those device MAC addresses used (one device of a shared voucher)."""
    from .models import UsageRecord
    start, _, _ = window(policy, voucher, now)
    start = start.replace(minute=0, second=0, microsecond=0)   # usage is stored per hour
    rows = UsageRecord.objects.filter(business_id=voucher.business_id, username__in=codes_of(voucher), hour__gte=start)
    if macs is not None:
        rows = rows.filter(mac_address__in=[m.upper() for m in macs if m])
    total = 0
    for hour, down, up in rows.values_list('hour', 'download', 'upload'):
        if policy.is_free_hour(timezone.localtime(hour).hour):
            continue
        total += down + (up if policy.counts == 'total' else 0)
    return total


def shared(voucher):
    return int(voucher.max_devices or 1) > 1


def device_of(voucher, mac, bindings=None):
    """(key, label, macs) for one device of a shared voucher. The sticky-voucher slot keeps the
    same device together when its MAC changes; otherwise the MAC is the device."""
    mac = (mac or '').upper()
    for b in bindings if bindings is not None else voucher.device_bindings.all():
        if mac and mac in {(b.current_mac or '').upper(), (b.previous_mac or '').upper()}:
            return f'slot{b.slot_no}', (b.label or b.current_mac or f'Device {b.slot_no}'), [m for m in {b.current_mac, b.previous_mac} if m]
    return (mac or 'unknown'), mac or 'Unknown device', [mac] if mac else []


def device_usage(voucher, policy, now=None):
    """[{key, label, used, tier}] — data per device of a shared voucher in the current period."""
    from .models import UsageRecord
    start, _, _ = window(policy, voucher, now)
    start = start.replace(minute=0, second=0, microsecond=0)
    bindings = list(voucher.device_bindings.all())
    per = {}
    for hour, mac, down, up in UsageRecord.objects.filter(business_id=voucher.business_id, username__in=codes_of(voucher),
                                                          hour__gte=start).values_list('hour', 'mac_address', 'download', 'upload'):
        if policy.is_free_hour(timezone.localtime(hour).hour):
            continue
        key, label, _ = device_of(voucher, mac, bindings)
        d = per.setdefault(key, {'key': key, 'label': label, 'used': 0})
        d['used'] += down + (up if policy.counts == 'total' else 0)
    for d in per.values():
        d['used_gb'] = d['used'] / GB
        d['tier'] = tier_for(d['used'], policy.tiers)
    return sorted(per.values(), key=lambda d: -d['used'])


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
    st = FairUsageState.objects.filter(voucher=voucher, policy=pol, device='').first()
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
    devices = []
    if shared(voucher):
        # each device is measured on its own: the voucher-wide speed only reflects the heaviest device
        states = {x.device: x for x in FairUsageState.objects.filter(voucher=voucher, policy=pol).exclude(device='')}
        for d in device_usage(voucher, pol, now):
            dst = states.get(d['key'])
            dl = bool(dst and dst.lifted_until and dst.lifted_until > now) or lifted
            t = 0 if dl else d['tier']
            devices.append({**d, 'tier': t, 'speed': speed_text(pol.tiers[t - 1]['down']) if t else '',
                            'next_gb': pol.tiers[t]['gb'] if t < len(pol.tiers) else None})
        tier = max([d['tier'] for d in devices] or [0])
        used = max([d['used'] for d in devices] or [0])
        nxt = pol.tiers[tier] if tier < len(pol.tiers) else None
    return {'live': live, 'policy': pol, 'used': used, 'used_gb': used / GB, 'tier': tier, 'lifted': lifted, 'lifted_until': st.lifted_until if lifted else None,
            'per_device': shared(voucher), 'devices': devices,
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
    st, _ = FairUsageState.objects.get_or_create(voucher=voucher, policy=pol, device='', defaults={'period_key': key})
    who = user if getattr(user, 'is_authenticated', False) else None
    st.lifted_until, st.lifted_by = until, who
    st.save(update_fields=['lifted_until', 'lifted_by', 'updated_at'])
    FairUsageState.objects.filter(voucher=voucher, policy=pol).exclude(device='').update(lifted_until=until, lifted_by=who)   # every device
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
            by_code.setdefault(code.upper(), []).append((ip, str(s.get('mac-address', '')).upper()))
    if not by_code:
        return {}
    vouchers = {v.code.upper(): v for v in Voucher.objects.filter(business=router.business, code__in=[c for c in by_code] + [c.lower() for c in by_code])}
    from .models import VoucherCodeAlias
    for a in VoucherCodeAlias.objects.filter(business=router.business, code__in=list(by_code)).select_related('voucher'):
        vouchers.setdefault(a.code.upper(), a.voucher)
    caps = {}

    def step(v, pol, used, key, device='', label=''):
        """Update the state of one voucher (or one device) and return its tier."""
        st, _ = FairUsageState.objects.get_or_create(voucher=v, policy=pol, device=device, defaults={'period_key': key, 'device_label': label[:120]})
        if st.period_key != key:     # new day / week: full speed, lift forgotten
            st.period_key, st.lifted_until, st.lifted_by = key, None, None
        whole = st if not device else FairUsageState.objects.filter(voucher=v, policy=pol, device='').first()
        lifted = bool(st.lifted_until and st.lifted_until > now) or bool(whole and whole.lifted_until and whole.lifted_until > now)
        tier = 0 if lifted else tier_for(used, pol.tiers)
        who = f'{label} ' if device else ''
        if tier != st.tier:
            if tier > st.tier:
                t = pol.tiers[tier - 1]
                record(v, 'fup_slowed', source='auto', reason=pol.name,
                       text=(f'Device {who}— ' if device else '') + f'used {used / GB:.2f} GB — speed now {speed_text(t["down"])} down / {speed_text(t["up"])} up'
                            + (' (the other devices keep their speed)' if device else ''))
            else:
                record(v, 'fup_restored', source='auto', reason=pol.name,
                       text=(f'Device {who}— ' if device else '') + ('back to full speed' if not tier else f'step {tier}'))
            st.tier, st.changed_at = tier, now
        st.used_bytes, st.device_label = used, (label or st.device_label)[:120]
        st.save()
        return tier

    for code, sess in by_code.items():
        v = vouchers.get(code)
        if not v or v.deleted_at:
            continue
        pol = policy_for(v, pols)
        if not pol or not pol.tiers:
            continue
        _, key, _ = window(pol, v, now)
        if not shared(v):
            tier = step(v, pol, used_bytes(v, pol, now), key)
            if tier:
                t = pol.tiers[tier - 1]
                for ip in sorted({ip for ip, _ in sess}):
                    caps[_qname(v.code, ip)] = (ip, f'{kbps(t["up"])}k/{kbps(t["down"])}k', v.code, tier)
            continue
        # Shared voucher: judge each device on its own data; cap only that device's addresses.
        bindings = list(v.device_bindings.all())
        devices = {}
        for ip, mac in sess:
            dkey, label, macs = device_of(v, mac, bindings)
            devices.setdefault(dkey, {'label': label, 'macs': macs, 'ips': set()})['ips'].add(ip)
        for dkey, d in devices.items():
            tier = step(v, pol, used_bytes(v, pol, now, macs=d['macs']) if d['macs'] else 0, key, device=dkey, label=d['label'])
            if tier:
                t = pol.tiers[tier - 1]
                for ip in sorted(d['ips']):
                    caps[_qname(v.code, ip)] = (ip, f'{kbps(t["up"])}k/{kbps(t["down"])}k', v.code, tier)
    return caps


FULL_EVERY = 300      # seconds: resend every cap to TapTap Link routers this often, even without changes
LIST = 'TTFUP'        # address list of capped devices: kept out of FastTrack so the caps really apply


def enforce(router, active, now=None, svc=None):
    """Bring the router's fair-usage caps in line with who is online now. Returns a small summary.

    Strict: Direct API / Tunnel routers are checked against what is really on the router every
    time (a cap someone deleted, or lost in a reboot, is put back at once). TapTap Link routers get
    the full set again every 5 minutes. Caps are moved above the hotspot's own queues and their
    devices are kept out of FastTrack, which would otherwise skip every queue."""
    want = desired_caps(router, active, now)
    key = f'tt:fup:{router.pk}'
    had = cache.get(key) or {}
    try:
        if svc is not None:
            out = _reconcile_api(svc, want)
        else:
            full = not cache.get(f'tt:fup:full:{router.pk}')
            to_set = want if full else {n: c for n, c in want.items() if list(had.get(n) or []) != [c[0], c[1]]}
            to_remove = [n for n in had if n not in want]
            if to_set or to_remove or (full and want):
                _apply_link(router, to_set, to_remove)
            if full:
                cache.set(f'tt:fup:full:{router.pk}', 1, FULL_EVERY)
            out = {'set': len(to_set), 'removed': len(to_remove)}
    except Exception as exc:
        logger.info('fair usage on %s: %s', router, exc)
        return {'capped': len(had), 'error': str(exc)[:200]}
    cache.set(key, {n: [c[0], c[1]] for n, c in want.items()}, STATE_TTL)
    return {'capped': len(want), **out}


def _reconcile_api(svc, want):
    """Make the router's TTFUP queues exactly `want`, read back from the router itself."""
    queues = svc.resource('/queue/simple')
    rows = queues.get()
    have = {str(r.get('name', '')): r for r in rows if str(r.get('name', '')).startswith('TTFUP-')}
    changed = removed = 0
    top = next((r for r in rows if r.get('id')), None)
    for name, (ip, limit, code, tier) in want.items():
        r = have.get(name)
        target = f'{ip}/32'
        if r:
            if str(r.get('target', '')) != target or str(r.get('max-limit', '')) != limit or str(r.get('disabled', 'false')) == 'true':
                queues.set(id=r['id'], target=target, max_limit=limit, disabled='no'); changed += 1
            continue
        fields = {'name': name, 'target': target, 'max_limit': limit, 'comment': f'TapTap fair usage: {code} step {tier}'}
        try:
            queues.add(place_before=top['id'], **fields) if top else queues.add(**fields)
        except Exception:
            queues.add(**fields)
        changed += 1
    for name, r in have.items():
        if name not in want:
            queues.remove(id=r['id']); removed += 1
    # Above the hotspot's dynamic queues (a device that logs in again gets a new one on top).
    if want:
        try:
            rows = queues.get()
            ours = [r['id'] for r in rows if str(r.get('name', '')).startswith('TTFUP-')]
            first = next((r for r in rows if r.get('id')), None)
            if ours and first and first['id'] not in ours:
                queues.call('move', {'numbers': ','.join(ours), 'destination': first['id']})
        except Exception as exc:
            logger.info('fair usage move: %s', exc)
    _fasttrack_guard_api(svc, {c[0] for c in want.values()})
    return {'set': changed, 'removed': removed}


def _fasttrack_guard_api(svc, ips):
    """Keep capped devices out of FastTrack: address list TTFUP + two accept rules above the fasttrack rule."""
    try:
        al = svc.resource('/ip/firewall/address-list')
        cur = {str(r.get('address', '')): r for r in al.get(list=LIST)}
        for ip in ips:
            if ip in cur:
                al.set(id=cur[ip]['id'], timeout='30m')
            else:
                al.add(list=LIST, address=ip, timeout='30m', comment='TapTap fair usage')
        for ip, r in cur.items():
            if ip not in ips and r.get('id'):
                al.remove(id=r['id'])
        if not ips:
            return
        flt = svc.resource('/ip/firewall/filter')
        rules = flt.get()
        mine = [r for r in rules if str(r.get('comment', '')) == 'TapTap fair usage: no fasttrack']
        ft = next((r for r in rules if str(r.get('action', '')) == 'fasttrack-connection' and str(r.get('disabled', 'false')) != 'true'), None)
        if ft and len(mine) < 2:
            for direction in ('src_address_list', 'dst_address_list'):
                if not any(r.get(direction.replace('_', '-')) == LIST for r in mine):
                    flt.add(chain='forward', action='accept', place_before=ft['id'], comment='TapTap fair usage: no fasttrack', **{direction: LIST})
    except Exception as exc:
        logger.info('fair usage fasttrack guard: %s', exc)


def _apply_api(svc, to_set, to_remove):
    """Kept for callers that remove caps (clear_router)."""
    queues = svc.resource('/queue/simple')
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
        (_reconcile_api(svc, {}) if svc is not None else _apply_link(router, {}, list(had)))
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
                   f'else={{ /queue simple set [find name={n}] target={t} max-limit={l} disabled=no }}')
        # keep the device out of FastTrack (which skips queues)
        out.append(f':do {{ :if ([:len [/ip firewall address-list find list={LIST} address={ip}]] = 0) do={{ '
                   f'/ip firewall address-list add list={LIST} address={ip} timeout=30m comment="TapTap fair usage" }} '
                   f'else={{ /ip firewall address-list set [find list={LIST} address={ip}] timeout=30m }} }} on-error={{}}')
    if p.get('remove'):
        names = ';'.join(rs(n) for n in p['remove'])
        out.append(f':foreach n in={{{names}}} do={{ :do {{ /queue simple remove [find name=$n] }} on-error={{}} }}')
    if p.get('set'):
        # caps above the hotspot's own queues, and FastTrack skipped for capped devices
        out.append(':do { /queue simple move [find name~"^TTFUP-"] destination=[:pick [/queue simple find] 0] } on-error={}')
        out.append(':do { :local ft [/ip firewall filter find action=fasttrack-connection disabled=no]; '
                   ':if ([:len $ft] > 0 and [:len [/ip firewall filter find comment="TapTap fair usage: no fasttrack"]] = 0) do={ '
                   f'/ip firewall filter add chain=forward action=accept src-address-list={LIST} comment="TapTap fair usage: no fasttrack" place-before=[:pick $ft 0]; '
                   f'/ip firewall filter add chain=forward action=accept dst-address-list={LIST} comment="TapTap fair usage: no fasttrack" place-before=[:pick $ft 0] }} }} on-error={{}}')
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
        rows.append({'state': st, 'voucher': v, 'policy': pol, 'tier': st.tier, 'steps': len(pol.tiers), 'device': st.device_label if st.device else '',
                     'used': st.used_bytes, 'used_gb': st.used_bytes / GB, 'since': st.changed_at,
                     'next': {'gb': nxt['gb'], 'down': speed_text(nxt['down'])} if nxt else None,
                     **bandwidth(v, st, now)})
    return rows


def refresh(business):
    """A policy changed: the next sync sends every router its full set of caps again."""
    for rid in business.routers.values_list('id', flat=True):
        cache.delete(f'tt:fup:full:{rid}')
