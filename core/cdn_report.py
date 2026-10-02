"""CDNs and "other" traffic, explained: which CDN delivered how much, and what each one was
serving (video, apps, music, websites) — the top services per CDN with their MB/GB."""
from collections import defaultdict

from django.db.models import Sum

from .traffic import cdn_of

OTHER_APPS = {'Other', 'Other sites', 'Other secure websites', 'Other websites'}
KIND = {'Video': 'Video', 'Social': 'Social', 'Messaging & calls': 'Calls & chat', 'Music': 'Audio', 'Updates & downloads': 'Apps & updates',
        'Gaming': 'Games', 'Web & search': 'Websites', 'System': 'System', 'Other': 'Websites'}


def _label(app, domain):
    return domain if app in OTHER_APPS and domain and domain != 'many sites' else app


def cdns(qs, top=14):
    """[{name, bytes, down, up, kinds:{kind: bytes}, services:[{name, kind, bytes, domain}]}] biggest first."""
    out = {}
    for r in (qs.exclude(via='').values('via', 'app', 'category', 'domain')
              .annotate(d=Sum('download'), u=Sum('upload')).order_by()):
        c = out.setdefault(r['via'], {'name': r['via'], 'bytes': 0, 'down': 0, 'up': 0, 'kinds': defaultdict(int), 'svc': defaultdict(lambda: [0, '', ''])})
        b = (r['d'] or 0) + (r['u'] or 0)
        kind = KIND.get(r['category'], 'Websites')
        c['bytes'] += b; c['down'] += r['d'] or 0; c['up'] += r['u'] or 0
        c['kinds'][kind] += b
        s = c['svc'][_label(r['app'], r['domain'])]
        s[0] += b; s[1] = s[1] or kind; s[2] = s[2] or r['domain']
    result = []
    for c in sorted(out.values(), key=lambda x: -x['bytes']):
        svcs = sorted(({'name': k, 'bytes': v[0], 'kind': v[1], 'domain': v[2]} for k, v in c['svc'].items()), key=lambda x: -x['bytes'])
        rest = svcs[top:]
        svcs = svcs[:top]
        if rest:
            svcs.append({'name': f'{len(rest)} more', 'bytes': sum(x['bytes'] for x in rest), 'kind': 'Other', 'domain': ''})
        result.append({'name': c['name'], 'bytes': c['bytes'], 'down': c['down'], 'up': c['up'],
                       'kinds': dict(sorted(c['kinds'].items(), key=lambda kv: -kv[1])), 'services': svcs})
    return result


def others(qs, top=25):
    """What hides in "Other": the top sites with a guessed kind and the CDN that served them."""
    rows = (qs.filter(app__in=OTHER_APPS).exclude(domain__in=['many sites', 'unresolved', ''])
            .values('domain', 'category', 'via').annotate(d=Sum('download'), u=Sum('upload')).order_by())
    agg = {}
    for r in rows:
        a = agg.setdefault(r['domain'], {'domain': r['domain'], 'bytes': 0, 'kind': KIND.get(r['category'], 'Websites'), 'via': r['via'] or cdn_of(r['domain'])})
        a['bytes'] += (r['d'] or 0) + (r['u'] or 0)
    items = sorted(agg.values(), key=lambda x: -x['bytes'])
    unknown = qs.filter(app__in=OTHER_APPS, domain__in=['many sites', 'unresolved', '']).aggregate(d=Sum('download'), u=Sum('upload'))
    return {'sites': items[:top], 'more': len(items) - top if len(items) > top else 0,
            'unnamed': (unknown['d'] or 0) + (unknown['u'] or 0)}
