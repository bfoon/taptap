"""Network traffic & consumption report (reads what core.traffic collected)."""
from collections import defaultdict

from django.core.cache import cache
from django.db.models import Count, Max, Q, Sum
from django.utils import timezone

from .finance import _bucket_key, _buckets, pct_change
from .models import AppUsage, DeviceSignature, TrafficSample, UsageRecord
from .traffic import CATEGORY_ORDER

DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def human_bytes(n):
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:.0f} {unit}' if unit in ('B', 'KB') else f'{n:.1f} {unit}' if n < 100 else f'{n:.0f} {unit}'
        n /= 1024


def human_bps(v):
    v = float(v or 0)
    if v >= 1e9: return f'{v / 1e9:.2f} Gb/s'
    if v >= 1e6: return f'{v / 1e6:.1f} Mb/s'
    if v >= 1e3: return f'{v / 1e3:.0f} kb/s'
    return f'{v:.0f} b/s'


def windows(hours):
    """[19,20,21,22,0] → ['19:00–23:00', '00:00–01:00'] (contiguous, wrapping past midnight)."""
    hs = sorted(set(hours))
    if not hs:
        return []
    runs, start, prev = [], hs[0], hs[0]
    for h in hs[1:]:
        if h == prev + 1:
            prev = h; continue
        runs.append([start, prev]); start = prev = h
    runs.append([start, prev])
    if len(runs) > 1 and runs[0][0] == 0 and runs[-1][1] == 23:
        runs[0][0] = runs[-1][0] - 24; runs.pop()
    return [f'{a % 24:02d}:00–{(b + 1) % 24:02d}:00' for a, b in runs]


def _samples(business, start, end, router_id=None):
    qs = TrafficSample.objects.filter(router__business=business, bucket__gte=start, bucket__lt=end)
    if router_id:
        qs = qs.filter(router_id=router_id)
    wan_routers = set(qs.exclude(interface='*users').values_list('router_id', flat=True).distinct())
    # Per router: the WAN counters when TapTap knows the WAN, otherwise the total of hotspot sessions.
    return qs.filter((Q(router_id__in=wan_routers) & ~Q(interface='*users')) | (~Q(router_id__in=wan_routers) & Q(interface='*users'))), wan_routers


def report(business, period, router_id=None):
    qs, wan_routers = _samples(business, period.start, period.end, router_id)
    prev_qs, _ = _samples(business, period.prev_start, period.prev_end, router_id)
    rows = list(qs.values('bucket', 'router_id', 'interface', 'rx_bytes', 'tx_bytes', 'rx_peak_bps', 'tx_peak_bps'))
    buckets = _buckets(period)
    idx = {k: i for i, (k, _) in enumerate(buckets)}
    down = [0] * len(buckets); up = [0] * len(buckets); peak_dn = [0] * len(buckets); peak_up = [0] * len(buckets)
    hour_tot = [0] * 24; heat = [[0] * 24 for _ in range(7)]; days_seen = set()
    peak = {'bps': 0, 'at': None, 'dir': 'download'}
    per_wan = defaultdict(lambda: [0, 0, 0])
    # Peak rate per 5-min slot must add interfaces up (two WANs peaking together = more total).
    slot_peak = defaultdict(lambda: [0, 0])
    for r in rows:
        local = timezone.localtime(r['bucket'])
        k = _bucket_key(local, period.bucket)
        if k in idx:
            i = idx[k]; down[i] += r['rx_bytes']; up[i] += r['tx_bytes']
        hour_tot[local.hour] += r['rx_bytes'] + r['tx_bytes']
        heat[local.weekday()][local.hour] += r['rx_bytes'] + r['tx_bytes']
        days_seen.add(local.date())
        sp = slot_peak[r['bucket']]; sp[0] += r['rx_peak_bps']; sp[1] += r['tx_peak_bps']
        if r['interface'] != '*users':
            w = per_wan[(r['router_id'], r['interface'])]; w[0] += r['rx_bytes']; w[1] += r['tx_bytes']; w[2] = max(w[2], r['rx_peak_bps'])
    for b, (pdn, pup) in slot_peak.items():
        k = _bucket_key(timezone.localtime(b), period.bucket)
        if k in idx:
            i = idx[k]; peak_dn[i] = max(peak_dn[i], pdn); peak_up[i] = max(peak_up[i], pup)
        if pdn > peak['bps']:
            peak = {'bps': pdn, 'at': b, 'dir': 'download'}

    total_down, total_up = sum(down), sum(up)
    prev_agg = prev_qs.aggregate(a=Sum('rx_bytes'), b=Sum('tx_bytes'))
    prev_total = (prev_agg['a'] or 0) + (prev_agg['b'] or 0)
    # Only compare with the previous period when it was measured for at least half its length.
    prev_days = len({timezone.localtime(b).date() for b in prev_qs.values_list('bucket', flat=True)[:50000]})
    if prev_days < max(1, period.days / 2):
        prev_total = 0
    ndays = max(1, len(days_seen))
    per_hour_avg = [h / ndays for h in hour_tot]

    # Peak / off-peak bands from the hour-of-day profile.
    active_hours = [v for v in per_hour_avg if v > 0]
    bands = ['none'] * 24
    peak_hours, off_hours = [], []
    if len(active_hours) >= 4:
        srt = sorted(per_hour_avg)
        q25, q75 = srt[6], srt[17]
        for h, v in enumerate(per_hour_avg):
            if v >= q75 and v > 0:
                bands[h] = 'peak'; peak_hours.append(h)
            elif v <= q25:
                bands[h] = 'off'; off_hours.append(h)
            else:
                bands[h] = 'shoulder'
    busiest = max(range(24), key=lambda h: per_hour_avg[h]) if any(per_hour_avg) else None
    quietest = min(range(24), key=lambda h: per_hour_avg[h]) if any(per_hour_avg) else None

    # Throughput chart: average Mb/s per bucket + peak Mb/s.
    secs = {'hour': 3600, 'day': 86400, 'month': 86400 * 30}.get(period.bucket, 86400)
    spans = [secs] * len(buckets)
    now = timezone.now()
    if buckets and period.start <= now < period.end:
        # The current hour/day/month is not over: average it over the time that has passed.
        loc = timezone.localtime(now)
        start = {'hour': loc.replace(minute=0, second=0, microsecond=0), 'month': loc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)}.get(
            period.bucket, loc.replace(hour=0, minute=0, second=0, microsecond=0))
        k = _bucket_key(loc, period.bucket)
        if k in idx:
            spans[idx[k]] = max(60, (now - start).total_seconds())
    mbps_dn = [round(b * 8 / spans[i] / 1e6, 3) for i, b in enumerate(down)]
    mbps_up = [round(b * 8 / spans[i] / 1e6, 3) for i, b in enumerate(up)]

    # Who is using it
    uq = UsageRecord.objects.filter(business=business, hour__gte=period.start, hour__lt=period.end)
    if router_id:
        uq = uq.filter(router_id=router_id)
    utotal = uq.aggregate(d=Sum('download'), u=Sum('upload'))
    user_total = (utotal['d'] or 0) + (utotal['u'] or 0)
    top = list(uq.values('username', 'mac_address').annotate(dn=Sum('download'), up=Sum('upload'), peak=Max('peak_bps'), hours=Count('hour', distinct=True))
               .order_by('-dn')[:25])
    macs = [t['mac_address'] for t in top if t['mac_address']]
    sigs = {}
    if macs:
        for s in DeviceSignature.objects.filter(business=business, last_mac__in=macs).only('last_mac', 'label', 'model', 'os'):
            sigs[s.last_mac] = s.label or ' '.join(x for x in (s.model, s.os) if x)
    plans = dict(business.vouchers.filter(code__in=[t['username'] for t in top]).values_list('code', 'plan_name'))
    for t in top:
        t['total'] = (t['dn'] or 0) + (t['up'] or 0)
        t['share'] = round(t['total'] * 100 / user_total, 1) if user_total else 0
        t['device'] = sigs.get(t['mac_address'], '')
        t['plan'] = plans.get(t['username'], '')
        t['dn_h'], t['up_h'], t['total_h'], t['peak_h'] = human_bytes(t['dn']), human_bytes(t['up']), human_bytes(t['total']), human_bps(t['peak'])
    users_count = uq.values('username', 'mac_address').distinct().count()
    top5_share = round(sum(t['total'] for t in top[:5]) * 100 / user_total, 1) if user_total else 0

    # What it is
    aq = AppUsage.objects.filter(business=business, hour__gte=period.start, hour__lt=period.end)
    if router_id:
        aq = aq.filter(router_id=router_id)
    app_rows = list(aq.values('app', 'category').annotate(dn=Sum('download'), up=Sum('upload')).order_by('-dn'))
    app_total = sum((r['dn'] or 0) + (r['up'] or 0) for r in app_rows)
    for r in app_rows:
        r['total'] = (r['dn'] or 0) + (r['up'] or 0)
        r['share'] = round(r['total'] * 100 / app_total, 1) if app_total else 0
        r['total_h'] = human_bytes(r['total'])
    cats = defaultdict(int)
    for r in app_rows:
        cats[r['category']] += r['total']
    categories = [{'name': c, 'total': cats[c], 'share': round(cats[c] * 100 / app_total, 1) if app_total else 0, 'total_h': human_bytes(cats[c])}
                  for c in sorted(cats, key=lambda c: -cats[c])]
    domains = list(aq.exclude(domain__in=['unresolved', 'many sites']).values('app', 'domain').annotate(dn=Sum('download'), up=Sum('upload')).order_by('-dn')[:20])
    for d in domains:
        d['total_h'] = human_bytes((d['dn'] or 0) + (d['up'] or 0))
    app_hours = defaultdict(lambda: [0] * 24)
    for r in aq.filter(app__in=[a['app'] for a in app_rows[:5]]).values('app', 'hour', 'download', 'upload'):
        app_hours[r['app']][timezone.localtime(r['hour']).hour] += r['download'] + r['upload']

    routers = {r.id: r.name for r in business.routers.all()}
    wans = [{'router': routers.get(rid, '?'), 'interface': iface, 'down': v[0], 'up': v[1], 'down_h': human_bytes(v[0]), 'up_h': human_bytes(v[1]),
             'peak_h': human_bps(v[2]), 'share': round((v[0] + v[1]) * 100 / (total_down + total_up), 1) if total_down + total_up else 0}
            for (rid, iface), v in sorted(per_wan.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))]

    # Insights, in plain words
    ins = []
    pw, ow = windows(peak_hours), windows(off_hours)
    if pw:
        ins.append(('peak', f"Peak time is {', '.join(pw)} — about {human_bytes(sum(per_hour_avg[h] for h in peak_hours))} a day moves in those hours."))
    if ow:
        ins.append(('off', f"Off-peak is {', '.join(ow)}. Night or early-morning bundles priced lower here fill capacity you already pay for."))
    if peak['bps']:
        ins.append(('rate', f"Highest download speed reached: {human_bps(peak['bps'])} on {timezone.localtime(peak['at']):%a %d %b at %H:%M}."))
    if top5_share >= 40 and len(top) >= 5:
        ins.append(('users', f"The 5 heaviest users took {top5_share}% of all customer data — a data cap or speed limit on big plans would share the line more fairly."))
    if app_rows:
        a = app_rows[0]
        ins.append(('app', f"{a['app']} is the biggest consumer at {a['share']}% of identified traffic ({a['total_h']})."))
    upd = next((c for c in categories if c['name'] == 'Updates & downloads'), None)
    if upd and upd['share'] >= 10:
        ins.append(('updates', f"Phone and computer updates used {upd['share']}% ({upd['total_h']}). Blocking or slowing update sites during peak hours frees the line for paying customers."))
    vid = next((c for c in categories if c['name'] == 'Video'), None)
    if vid and vid['share'] >= 35:
        ins.append(('video', f"Video streaming is {vid['share']}% of traffic. A lower speed on cheap plans (e.g. 2 Mb/s) keeps video watchable while protecting everyone else."))
    change = pct_change(total_down + total_up, prev_total) if prev_total else None
    if change is not None:
        ins.append(('trend', f"Total traffic is {'up' if change >= 0 else 'down'} {abs(change)}% on the previous {period.days} days."))

    return {
        'has_data': bool(rows) or bool(user_total) or bool(app_rows),
        'source': 'wan' if wan_routers else 'users',
        'kpis': {'down': total_down, 'up': total_up, 'down_h': human_bytes(total_down), 'up_h': human_bytes(total_up),
                 'total_h': human_bytes(total_down + total_up), 'per_day_h': human_bytes((total_down + total_up) / ndays),
                 'change': change, 'peak_bps': peak['bps'], 'peak_h': human_bps(peak['bps']),
                 'peak_at': timezone.localtime(peak['at']).strftime('%a %d %b %H:%M') if peak['at'] else '',
                 'busiest': f'{busiest:02d}:00' if busiest is not None else '—', 'quietest': f'{quietest:02d}:00' if quietest is not None else '—',
                 'users': users_count, 'per_user_h': human_bytes(user_total / users_count) if users_count else '—', 'top5_share': top5_share},
        'peak_windows': pw, 'off_windows': ow,
        'chart': {'labels': [l for _, l in buckets], 'down_gb': [round(b / 1024 ** 3, 3) for b in down], 'up_gb': [round(b / 1024 ** 3, 3) for b in up],
                  'mbps_dn': mbps_dn, 'mbps_up': mbps_up, 'peak_dn': [round(v / 1e6, 2) for v in peak_dn], 'peak_up': [round(v / 1e6, 2) for v in peak_up]},
        'hours': {'avg_gb': [round(v / 1024 ** 3, 3) for v in per_hour_avg], 'bands': bands},
        'heat': {'rows': heat, 'max': max((max(r) for r in heat), default=0) or 1, 'days': DAYS},
        'users': top, 'apps': app_rows[:15], 'categories': categories, 'domains': domains,
        'app_hours': {'labels': [f'{h:02d}' for h in range(24)], 'series': [{'label': k, 'data': [round(x / 1024 ** 2, 1) for x in v]} for k, v in app_hours.items()]},
        'wans': wans, 'insights': ins, 'category_order': CATEGORY_ORDER,
    }


def right_now(business, router_id=None):
    out = {'sessions': [], 'down_bps': 0, 'up_bps': 0, 'count': 0, 'at': None}
    for r in business.routers.all():
        if router_id and str(r.id) != str(router_id):
            continue
        d = cache.get(f'tt:tr:now:{r.pk}')
        if not d:
            continue
        out['down_bps'] += d['down_bps']; out['up_bps'] += d['up_bps']; out['count'] += d['count']
        out['at'] = max(out['at'] or d['at'], d['at'])
        for s in d['sessions']:
            out['sessions'].append({**s, 'router': r.name, 'router_id': r.id})
    out['sessions'].sort(key=lambda x: -(x['down_bps'] + x['up_bps']))
    out['sessions'] = out['sessions'][:12]
    macs = [s['mac'] for s in out['sessions'] if s['mac']]
    sigs = {s.last_mac: (s.label or ' '.join(x for x in (s.model, s.os) if x)) for s in
            DeviceSignature.objects.filter(business=business, last_mac__in=macs).only('last_mac', 'label', 'model', 'os')} if macs else {}
    total = out['down_bps'] + out['up_bps']
    for s in out['sessions']:
        s['device'] = sigs.get(s['mac'], '')
        s['down_h'], s['up_h'] = human_bps(s['down_bps']), human_bps(s['up_bps'])
        s['session_h'] = human_bytes(s['session_down'] + s['session_up'])
        s['share'] = round((s['down_bps'] + s['up_bps']) * 100 / total) if total else 0
    out['down_h'], out['up_h'] = human_bps(out['down_bps']), human_bps(out['up_bps'])
    return out
