"""Traffic → Idle vouchers and Slow vouchers.

**Idle**: vouchers in use that carry no traffic. Two groups, both read from what live sync already collects:

* *Online, no traffic* — a hotspot session is open but nothing moves through it. ``Quiet`` remembers, per session,
  since when it has been silent (under 1 kb/s) and since when it has been calm (under 10 kb/s: only the phone's
  background chatter). It is updated on every live-sync pass by core.traffic.collect_sessions.
* *Time running, not online* — the voucher was started, its time is running out, and no device is signed in.
  These customers paid and are getting nothing: asleep, gone home, or unable to connect.

**Slow**: vouchers whose speed is being held down — by fair usage, by a speed rule, or because they use all of
their plan's speed — with the speed they do now against the limit. A router scan (core/conn_lab ``slowscan``)
adds what the limiter itself reports: packets dropped per customer.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.db.models import Max, Q, Sum
from django.utils import timezone

SILENT_BPS = 1000
CALM_BPS = 10_000
FRESH = 300            # seconds: a session list older than this says nothing about who is online now
AT_LIMIT = 85          # % of the limit that counts as "using all of it"


class Quiet:
    """Per session: since when it is silent / calm. Lives in the cache next to the session counters."""

    def __init__(self, router, ts):
        self.key = f'tt:tr:quiet:{router.pk}'
        self.prev = cache.get(self.key) or {}
        self.cur = {}
        self.ts = ts

    def note(self, sid, p, user, b_up, b_down, d_up, d_down, uptime):
        """One session on one pass → (silent since, calm since) as timestamps (None = it is moving data)."""
        silent, calm = self.prev.get(sid) or (None, None)
        if p and p[3] == user and b_up >= p[0] and b_down >= p[1]:
            span = self.ts - p[2]
            if span >= 3:
                bps = (d_up + d_down) * 8 / span
                silent = (silent or p[2]) if bps < SILENT_BPS else None
                calm = (calm or p[2]) if bps < CALM_BPS else None
        elif not p:
            # First sight: a session that has moved almost nothing since it logged in has been quiet all along.
            from .sync import _routeros_seconds
            secs = _routeros_seconds(uptime) or 0
            silent = calm = None
            if secs >= 120:
                bps = (b_up + b_down) * 8 / secs
                start = self.ts - secs
                silent = start if bps < SILENT_BPS / 4 else None
                calm = start if bps < CALM_BPS / 4 else None
        else:
            silent = calm = None                      # the router reused the session id
        if silent or calm:
            self.cur[sid] = (silent, calm)
        return silent, calm

    def save(self):
        cache.set(self.key, self.cur, 7200)


def human_secs(s):
    s = int(max(0, s or 0))
    if s < 90:
        return f'{s} s'
    if s < 5400:
        return f'{round(s / 60)} min'
    if s < 172800:
        h, m = divmod(round(s / 60), 60)
        return f'{h} h {m:02d}' if m else f'{h} h'
    return f'{round(s / 86400)} days'


def _routers(business, router_id):
    qs = business.routers.all()
    return list(qs.filter(pk=router_id) if router_id else qs)


def _snap(router, now):
    """(sessions by code, age in seconds or None) of a router's last session list."""
    snap = cache.get(f'tt:tr:users:{router.pk}') or {}
    try:
        age = (now - timezone.datetime.fromisoformat(snap['at'])).total_seconds()
    except Exception:
        return {}, None
    return snap.get('users') or {}, age


def _vouchers(business, codes):
    from .models import Voucher
    out = {}
    codes = [c for c in codes if c and not c.startswith(('BYPASS:', '('))]
    for i in range(0, len(codes), 400):
        for v in Voucher.objects.filter(business=business, code__in=codes[i:i + 400]).only(
                'pk', 'code', 'plan_name', 'expires_at', 'used_at', 'duration_minutes', 'rate_limit', 'router_profile', 'max_devices',
                'status', 'frozen_at', 'customer_name'):
            out[v.code.upper()] = v
    missing = [c for c in codes if c.upper() not in out]
    if missing and len(missing) <= 400:                 # router users in another letter case than TapTap's code
        q = Q()
        for c in missing:
            q |= Q(code__iexact=c)
        for v in Voucher.objects.filter(q, business=business):
            out[v.code.upper()] = v
    return out


def _labels(business, macs):
    from .models import DeviceSignature
    macs = [m for m in macs if m]
    if not macs:
        return {}
    return {s.last_mac.upper(): (s.pk, s.label or ' '.join(x for x in (s.model, s.os) if x))
            for s in DeviceSignature.objects.filter(business=business, last_mac__in=macs).only('pk', 'last_mac', 'label', 'model', 'os')}


def _ends(v, now):
    try:
        from .voucher_history import ends_at
        end = ends_at(v)
    except Exception:
        end = v.expires_at
    return end, (int((end - now).total_seconds()) if end and end > now else None)


def _person(v, code):
    return {'code': v.code if v else code, 'id': v.pk if v else None, 'plan': v.plan_name if v else '',
            'name': (v.customer_name if v else '') or '', 'bypass': code.upper().startswith('BYPASS:') or code.startswith('(')}


# ─────────────────────────────── idle ───────────────────────────────

def idle(business, router_id=None, minutes=10, level='calm', now=None):
    """Both idle groups for the Traffic page."""
    from . import line_steer as ls
    from .traffic_report import human_bps, human_bytes
    now = now or timezone.now()
    need = max(1, int(minutes)) * 60
    field = 'quiet' if level == 'silent' else 'calm'
    rows, online_codes, stale, watched = [], set(), [], []
    sessions_total = 0
    per_router = []
    for r in _routers(business, router_id):
        users, age = _snap(r, now)
        if age is None or age > FRESH:
            stale.append(r.name)
            continue
        watched.append(r.pk)
        ts_at = now.timestamp() - age
        info = ls.lanes(r)
        lane_ips = (ls.seen(r).get('ips') or {}) if len(info['lanes']) > 1 else {}
        for code, sessions in users.items():
            online_codes.add(code.upper())
            for s in sessions:
                sessions_total += 1
                since = s.get(field)
                if not since or ts_at - since < need:
                    continue
                d = ls.describe(info, lane_ips.get(s.get('ip'), {})) if lane_ips else None
                per_router.append((r, code, s, int(now.timestamp() - since), d))
    vouchers = _vouchers(business, [c for _r, c, _s, _i, _d in per_router])
    labels = _labels(business, [s.get('mac') for _r, _c, s, _i, _d in per_router])
    for r, code, s, idle_s, d in per_router:
        v = vouchers.get(code.upper())
        end, left = _ends(v, now) if v else (None, None)
        dev = labels.get((s.get('mac') or '').upper())
        rows.append({**_person(v, code), 'router': r.name, 'router_id': r.pk, 'ip': s.get('ip', ''), 'mac': s.get('mac', ''),
                     'device': dev[1] if dev else '', 'device_id': dev[0] if dev else None,
                     'idle_s': idle_s, 'idle': human_secs(idle_s), 'uptime': s.get('uptime', ''),
                     'data': human_bytes((s.get('session_down') or 0) + (s.get('session_up') or 0)),
                     'now': human_bps((s.get('down_bps') or 0) + (s.get('up_bps') or 0)),
                     'left_s': left, 'left': human_secs(left) if left else ('' if not v else 'no end'),
                     'line': d['label'] if d and d['state'] != 'none' else ''})
    rows.sort(key=lambda x: -x['idle_s'])
    return {'online': rows[:400], 'online_total': len(rows), 'sessions': sessions_total, 'stale': stale,
            'offline': offline(business, watched, online_codes, now) if watched else {'rows': [], 'total': 0},
            'minutes': int(minutes), 'level': level, 'at': now.isoformat()}


def offline(business, router_ids, online_codes, now, limit=150):
    """Started vouchers whose time is running while nobody is signed in with them."""
    from .models import UsageRecord, Voucher
    from .traffic_report import human_bytes
    qs = (Voucher.objects.filter(business=business, router_id__in=router_ids, status='active', used_at__isnull=False,
                                 expires_at__gt=now, frozen_at__isnull=True, deleted_at__isnull=True)
          .select_related('router').order_by('expires_at'))
    picked, total = [], 0
    for v in qs.only('pk', 'code', 'plan_name', 'expires_at', 'used_at', 'customer_name', 'router__name', 'router_id')[:3000]:
        if v.code.upper() in online_codes:
            continue
        total += 1
        if len(picked) < limit:
            picked.append(v)
    seen = {}
    if picked:
        for row in (UsageRecord.objects.filter(business=business, username__in=[v.code for v in picked], hour__gte=now - timedelta(days=14))
                    .values('username').annotate(last=Max('hour'), dn=Sum('download'), up=Sum('upload'))):
            seen[row['username'].upper()] = row
    rows = []
    for v in picked:
        u = seen.get(v.code.upper())
        last = u['last'] + timedelta(hours=1) if u else v.used_at          # usage is stored per hour: the hour's end
        last = min(last, now)
        away = int((now - last).total_seconds())
        left = int((v.expires_at - now).total_seconds())
        whole = max(1, int((v.expires_at - v.used_at).total_seconds()))
        rows.append({'code': v.code, 'id': v.pk, 'plan': v.plan_name, 'name': v.customer_name or '', 'router': v.router.name if v.router_id else '',
                     'router_id': v.router_id, 'away_s': away, 'away': human_secs(away), 'left_s': left, 'left': human_secs(left),
                     'used_pct': min(100, round((whole - left) * 100 / whole)),
                     'data': human_bytes((u['dn'] or 0) + (u['up'] or 0)) if u else 'none', 'never': not u})
    return {'rows': rows, 'total': total}


# ─────────────────────────────── slow ───────────────────────────────

def remember_scan(router, result):
    """Keep a router's limiter-drop scan for the Slow tab (10 minutes)."""
    from .conn_lab import _target_ip
    by_ip = {}
    for q in result.get('queues') or []:
        ip = _target_ip(q.get('target'))
        if not ip:
            continue
        cur = by_ip.get(ip)
        if cur is None:
            by_ip[ip] = dict(q)
        else:
            cur['drop_down'] += q['drop_down']
            cur['drop_up'] += q['drop_up']
            cur['queued'] += q['queued']
            if q.get('max_down') and (not cur.get('max_down') or q['max_down'] < cur['max_down']):
                cur.update(max_down=q['max_down'], max_up=q['max_up'], rate_down=q['rate_down'], rate_up=q['rate_up'],
                           drop_pct=q['drop_pct'], kind=q['kind'], name=q['name'])
    cache.set(f'tt:slowscan:{router.pk}', {'at': timezone.now().isoformat(), 'ips': by_ip, 'readable': result.get('readable', True),
                                           'total': result.get('total', 0)}, 600)


def _plan_limits(business, vouchers):
    """code → (up bps, down bps) from the plan's (or the voucher's own) speed."""
    from .conn_lab import parse_rate
    plans = {p.name.lower(): p for p in business.plans.all()}
    out = {}
    for code, v in vouchers.items():
        plan = plans.get((v.plan_name or '').lower())
        out[code] = parse_rate((plan.speed_limit if plan else '') or v.rate_limit or '')
    return out


def slow(business, router_id=None, now=None):
    from . import fair_usage as fu
    from .conn_lab import bps_text
    now = now or timezone.now()
    live, stale, scans = [], [], {}
    for r in _routers(business, router_id):
        users, age = _snap(r, now)
        if age is None or age > FRESH:
            stale.append(r.name)
            continue
        scan = cache.get(f'tt:slowscan:{r.pk}')
        if scan:
            scans[r.pk] = scan
        active = []
        for code, sessions in users.items():
            for s in sessions:
                live.append((r, code, s))
                active.append({'user': code, 'address': s.get('ip'), 'mac-address': s.get('mac')})
        try:
            from .traffic_speed import desired_caps
            rules = {v[0]: (v[1] * 1000, v[2] * 1000, v[4]) for v in desired_caps(r, active).values()}
        except Exception:
            rules = {}
        for item in live:
            if item[0] is r:
                item[2]['_rule'] = rules.get(item[2].get('ip'))
    vouchers = _vouchers(business, [c for _r, c, _s in live])
    limits = _plan_limits(business, vouchers)
    fup = {}
    try:
        for row in fu.slowed(business, now):
            t = row['policy'].tiers[row['tier'] - 1]
            fup[row['voucher'].pk] = {'down': fu.kbps(t['down']) * 1000, 'up': fu.kbps(t['up']) * 1000,
                                      'text': f'step {row["tier"]} of {row["steps"]} · {row["used_text"]} used', 'policy': row['policy'].name}
    except Exception:
        fup = {}
    labels = _labels(business, [s.get('mac') for _r, _c, s in live])
    rows = []
    for r, code, s in live:
        v = vouchers.get(code.upper())
        down, up = s.get('down_bps') or 0, s.get('up_bps') or 0
        reasons, limit, source = [], 0, ''
        plan_up, plan_down = limits.get(code.upper(), (0, 0))
        if plan_down:
            limit, source = plan_down, 'plan'
        rule = s.get('_rule')
        if rule and rule[1] and (not limit or rule[1] <= limit):
            limit, source = rule[1], 'rule'
            reasons.append({'key': 'rule', 'text': f'Speed rule “{rule[2]}”: {bps_text(rule[1])}'})
        f = fup.get(v.pk) if v else None
        if f and (not limit or f['down'] <= limit):
            limit, source = f['down'], 'fup'
        if f:
            reasons.append({'key': 'fup', 'text': f'Fair usage “{f["policy"]}” ({f["text"]}): {bps_text(f["down"])}'})
        q = ((scans.get(r.pk) or {}).get('ips') or {}).get(s.get('ip'))
        if q and q.get('max_down') and (not limit or q['max_down'] < limit):
            limit, source = q['max_down'], source or q.get('kind', 'plan')
        pct = min(100, round(down * 100 / limit)) if limit else None
        if pct is not None and pct >= AT_LIMIT and source == 'plan':
            reasons.append({'key': 'limit', 'text': f'Using all of its plan speed ({bps_text(down)} of {bps_text(limit)})'})
        if q and (q['drop_down'] or q['drop_up']):
            reasons.append({'key': 'drops', 'text': f'The limiter dropped {q["drop_down"] + q["drop_up"]:,} packets'
                                                    + (f' ({q["drop_pct"]}% of its download)' if q.get('drop_pct') else '')})
        if not reasons:
            continue
        dev = labels.get((s.get('mac') or '').upper())
        score = (q['drop_pct'] if q else 0) * 4 + (pct or 0) + (60 if f else 0) + (40 if rule else 0)
        rows.append({**_person(v, code), 'router': r.name, 'router_id': r.pk, 'ip': s.get('ip', ''), 'mac': s.get('mac', ''),
                     'device': dev[1] if dev else '', 'device_id': dev[0] if dev else None,
                     'down_bps': down, 'up_bps': up, 'down': bps_text(down), 'up': bps_text(up),
                     'limit_bps': limit, 'limit': bps_text(limit) if limit else '', 'source': source, 'pct': pct,
                     'drops': (q['drop_down'] + q['drop_up']) if q else None, 'drop_pct': q.get('drop_pct') if q else None,
                     'queued': q.get('queued') if q else None, 'reasons': reasons, 'score': score, 'can_lift': bool(f)})
    rows.sort(key=lambda x: -x['score'])
    return {'rows': rows[:300], 'total': len(rows), 'sessions': len(live), 'stale': stale,
            'scans': {str(k): {'at': v['at'], 'readable': v.get('readable', True), 'found': len(v.get('ips') or {}), 'total': v.get('total', 0)}
                      for k, v in scans.items()},
            'counts': {k: sum(1 for x in rows if any(z['key'] == k for z in x['reasons'])) for k in ('fup', 'rule', 'limit', 'drops')},
            'at': now.isoformat()}


def device_limits(business, v, code, ip, router):
    """Everything TapTap itself knows that limits one device: plan, fair usage, speed rule (for the Connection lab)."""
    from . import fair_usage as fu
    from .conn_lab import bps_text, parse_rate
    out = {'limit_down': 0, 'limit_up': 0, 'limit_source': '', 'limits': []}

    def add(src, label, up, down):
        if not down:
            return
        out['limits'].append({'source': src, 'label': label, 'down': bps_text(down), 'up': bps_text(up) if up else ''})
        if not out['limit_down'] or down < out['limit_down']:
            out.update(limit_down=down, limit_up=up, limit_source=src)
    if v is not None:
        plan = business.plans.filter(name__iexact=v.plan_name or '').first() if v.plan_name else None
        up, down = parse_rate((plan.speed_limit if plan else '') or v.rate_limit or '')
        add('plan', f'Plan {v.plan_name}' if plan else 'This voucher', up, down)
        try:
            for row in fu.slowed(business):
                if row['voucher'].pk == v.pk:
                    t = row['policy'].tiers[row['tier'] - 1]
                    add('fup', f'Fair usage “{row["policy"].name}”, step {row["tier"]} of {row["steps"]}', fu.kbps(t['up']) * 1000, fu.kbps(t['down']) * 1000)
                    out['fup'] = True
                    break
        except Exception:
            pass
    if router is not None and ip:
        try:
            from .traffic_speed import desired_caps
            for cap in desired_caps(router, [{'user': code, 'address': ip}]).values():
                add('rule', f'Speed rule “{cap[4]}”', cap[1] * 1000, cap[2] * 1000)
        except Exception:
            pass
    return out
