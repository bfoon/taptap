"""Platform console › Traffic: totals across every business — never individual customers.

Everything here is aggregated: data per category, app, CDN, business size band, day and hour, and
devices counted by type / brand / model / system. No voucher, MAC, IP or person appears.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import timedelta

from django.db.models import Count, Sum
from django.db.models.functions import TruncDate, ExtractHour
from django.utils import timezone

PERIODS = {'24h': ('Last 24 hours', 1), '7d': ('Last 7 days', 7), '30d': ('Last 30 days', 30), '90d': ('Last 90 days', 90)}

BRANDS = [
    ('Apple', r'iphone|ipad|ipod|mac ?os|macintosh|\bios\b|apple'),
    ('Samsung', r'samsung|\bsm-[a-z0-9]+|galaxy'),
    ('Tecno', r'tecno|\b(?:ck|kg|kh|ki|kj|bg|bd|ld|lh|li)\d{1,2}[a-z]?\b'),
    ('Infinix', r'infinix|\bx6\d{2}[a-z]?\b'),
    ('itel', r'\bitel\b|\bit\d{4}\b|\ba\d{3}l\b'),
    ('Xiaomi / Redmi / Poco', r'xiaomi|redmi|\bpoco\b|\bmi \d|\bm\d{4}[a-z]\d+'),
    ('Huawei / Honor', r'huawei|honor|\b(?:ane|ele|vog|jny|mar|stk|lld)-'),
    ('Oppo / Realme / OnePlus', r'oppo|realme|oneplus|\bcph\d{4}\b|\brmx\d{4}\b'),
    ('Vivo', r'\bvivo\b|\bv\d{4}[a-z]?\b'),
    ('Nokia', r'nokia|\bta-\d{4}\b'),
    ('Motorola', r'motorola|\bmoto\b|\bxt\d{4}'),
    ('Google Pixel', r'pixel'),
    ('Windows PC', r'windows'),
    ('Linux / Chromebook', r'linux|cros|chromebook'),
]


def brand_of(model='', os='', ua=''):
    text = f'{model} {os} {ua}'.lower()
    for name, rx in BRANDS:
        if re.search(rx, text):
            return name
    if 'android' in text:
        return 'Other Android'
    return 'Unknown'


def _period(key):
    key = key if key in PERIODS else '7d'
    label, days = PERIODS[key]
    end = timezone.now()
    return key, label, end - timedelta(days=days), end


def analysis(period_key='7d'):
    from .cdn_report import KIND, cdns, others
    from .models import AppUsage, Business, DeviceSignature
    key, label, start, end = _period(period_key)
    qs = AppUsage.objects.filter(hour__gte=start, hour__lt=end)
    tot = qs.aggregate(d=Sum('download'), u=Sum('upload'))
    down, up = tot['d'] or 0, tot['u'] or 0
    total = down + up

    def pct(b):
        return round(b * 100 / total, 1) if total else 0

    cats = [{'name': KIND.get(r['category'], r['category']), 'category': r['category'], 'bytes': (r['d'] or 0) + (r['u'] or 0)}
            for r in qs.values('category').annotate(d=Sum('download'), u=Sum('upload'))]
    merged = defaultdict(int)
    for c in cats:
        merged[c['name']] += c['bytes']
    categories = [{'name': k, 'bytes': v, 'pct': pct(v)} for k, v in sorted(merged.items(), key=lambda kv: -kv[1])]
    apps = [{'name': r['app'], 'category': KIND.get(r['category'], r['category']), 'bytes': (r['d'] or 0) + (r['u'] or 0),
             'businesses': r['nb']}
            for r in qs.exclude(app__in=['Other', 'Other sites', 'Other secure websites', 'Other websites'])
            .values('app', 'category').annotate(d=Sum('download'), u=Sum('upload'), nb=Count('business', distinct=True))]
    apps.sort(key=lambda a: -a['bytes'])
    for a in apps:
        a['pct'] = pct(a['bytes'])
    days = [{'day': r['day'], 'bytes': (r['d'] or 0) + (r['u'] or 0)}
            for r in qs.annotate(day=TruncDate('hour')).values('day').annotate(d=Sum('download'), u=Sum('upload')).order_by('day')]
    hours = defaultdict(int)
    for r in qs.annotate(h=ExtractHour('hour')).values('h').annotate(d=Sum('download'), u=Sum('upload')):
        hours[r['h']] += (r['d'] or 0) + (r['u'] or 0)
    active_biz = qs.values('business').distinct().count()

    # devices: counted, never listed (active in the period)
    devs = DeviceSignature.objects.filter(last_seen__gte=start).values_list('model', 'os', 'user_agent', 'device_type')
    by_brand, by_model, by_os, by_type = Counter(), Counter(), Counter(), Counter()
    n_dev = 0
    for model, os_name, ua, dtype in devs.iterator():
        n_dev += 1
        b = brand_of(model, os_name, ua)
        by_brand[b] += 1
        by_model[(b, (model or 'Unknown model').strip()[:60])] += 1
        by_os[(os_name or 'Unknown').split(' ')[0] or 'Unknown'] += 1
        by_type[(dtype or 'unknown').title()] += 1
    min_group = 3      # a model shown only when at least 3 devices have it (no single person can be picked out)
    models = [{'brand': b, 'model': m, 'count': c} for (b, m), c in by_model.most_common() if c >= min_group][:40]
    small = sum(c for (b, m), c in by_model.items() if c < min_group)
    return {
        'key': key, 'label': label, 'start': start, 'end': end, 'periods': PERIODS,
        'total': total, 'down': down, 'up': up, 'businesses': active_biz, 'all_businesses': Business.objects.count(),
        'categories': categories, 'apps': apps[:30], 'cdns': cdns(qs, top=16), 'others': others(qs, top=30),
        'days': days, 'hours': [{'hour': h, 'bytes': hours.get(h, 0)} for h in range(24)],
        'devices': {'count': n_dev, 'brands': [{'name': k, 'count': v, 'pct': round(v * 100 / n_dev, 1) if n_dev else 0} for k, v in by_brand.most_common()],
                    'models': models, 'small_models': small, 'os': [{'name': k, 'count': v} for k, v in by_os.most_common(12)],
                    'types': [{'name': k, 'count': v} for k, v in by_type.most_common()], 'min_group': min_group},
    }


def csv_rows(data, kind):
    """(header, rows) for a download — aggregated only."""
    mb = lambda b: round((b or 0) / 1048576, 1)
    if kind == 'apps':
        return ['App / service', 'Type', 'Businesses', 'Data (MB)', 'Share %'], [[a['name'], a['category'], a['businesses'], mb(a['bytes']), a['pct']] for a in data['apps']]
    if kind == 'categories':
        return ['Type', 'Data (MB)', 'Share %'], [[c['name'], mb(c['bytes']), c['pct']] for c in data['categories']]
    if kind == 'cdns':
        rows = []
        for c in data['cdns']:
            for s in c['services']:
                rows.append([c['name'], mb(c['bytes']), s['name'], s['kind'], mb(s['bytes'])])
        return ['CDN', 'CDN total (MB)', 'Service', 'Type', 'Service data (MB)'], rows
    if kind == 'devices':
        d = data['devices']
        rows = [['Brand', x['name'], x['count'], x['pct']] for x in d['brands']]
        rows += [['Model', f"{x['brand']} — {x['model']}", x['count'], ''] for x in d['models']]
        rows += [['System', x['name'], x['count'], ''] for x in d['os']] + [['Type', x['name'], x['count'], ''] for x in d['types']]
        return ['Group', 'Name', 'Devices', 'Share %'], rows
    if kind == 'daily':
        return ['Day', 'Data (MB)'], [[str(x['day']), mb(x['bytes'])] for x in data['days']]
    if kind == 'hourly':
        return ['Hour of day', 'Data (MB)'], [[f"{x['hour']:02d}:00", mb(x['bytes'])] for x in data['hours']]
    return ['Measure', 'Value'], [['Period', data['label']], ['From', data['start'].isoformat()], ['To', data['end'].isoformat()],
                                  ['Data (MB)', mb(data['total'])], ['Download (MB)', mb(data['down'])], ['Upload (MB)', mb(data['up'])],
                                  ['Businesses with traffic', data['businesses']], ['Devices seen', data['devices']['count']]]
