"""Smart search for the Devices page.

Type words and/or filters, like a mail search box:

    tecno                      words: name, model, system, browser, note, MAC, IP, router,
                               voucher (old codes too) and the customer name/phone on its vouchers
    os:android  type:phone     system / kind of device
    mac:aa:bb  ip:10.5.50      part of a MAC (colons, dashes or none) or an IP
    voucher:KW7  plan:"1 Day"  agent:awa  router:main
    app:youtube  site:tiktok   used that app / site (last 30 days)
    used>2gb  used<300mb       data in the last 7 days
    seen:today  seen:3d  seen:week  seen:month
    is:online is:slowed is:shared is:random is:flagged is:new is:multimac
    sort:data sort:seen sort:visits sort:macs sort:vouchers
    -os:ios  -is:random        a minus in front excludes

Everything is combined with AND. Returns the matching devices with the reason each matched.
"""
from __future__ import annotations

import re
import shlex
from collections import defaultdict
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Q, Sum
from django.utils import timezone

KEYS = {'os', 'type', 'mac', 'ip', 'voucher', 'plan', 'agent', 'router', 'app', 'site', 'seen', 'is', 'sort', 'used'}
IS_VALUES = ['online', 'slowed', 'shared', 'random', 'flagged', 'new', 'multimac']
SORTS = {'seen': 'Last seen', 'data': 'Most data (7 days)', 'visits': 'Most visits', 'macs': 'Most MAC addresses', 'vouchers': 'Most vouchers'}
SEEN = {'today': 1, '24h': 1, 'week': 7, '7d': 7, 'month': 30, '30d': 30}
UNITS = {'b': 1, 'kb': 1024, 'mb': 1024 ** 2, 'gb': 1024 ** 3, 'tb': 1024 ** 4}
USED_RE = re.compile(r'^used\s*(>=|<=|>|<|=)\s*([\d.]+)\s*(tb|gb|mb|kb|b)?$', re.I)
HELP = [
    ('tecno', 'Words match name, model, MAC, IP, voucher, customer…'),
    ('os:android', 'System: android, ios, windows, mac, linux'),
    ('type:phone', 'phone, tablet or computer'),
    ('mac:3c:5a', 'Part of a MAC address'), ('ip:10.5.50', 'Part of an IP address'),
    ('voucher:KW7', 'Voucher code (old codes too)'), ('plan:"1 Day"', 'Used a voucher of this plan'),
    ('agent:awa', 'Voucher sold by this agent'), ('router:main', 'Last seen on this router'),
    ('app:youtube', 'Used this app (30 days)'), ('site:tiktok', 'Visited this site (30 days)'),
    ('used>2gb', 'Data in the last 7 days (>, <, =)'), ('seen:today', 'today, 3d, week, month'),
    ('is:online', 'Connected right now'), ('is:slowed', 'Slowed by fair usage'), ('is:shared', 'Its voucher is shared'),
    ('is:random', 'Uses random MAC addresses'), ('is:multimac', 'Seen with more than one MAC'),
    ('is:new', 'First seen in the last 24 hours'), ('is:flagged', 'Flagged by you'),
    ('sort:data', 'Sort: data, seen, visits, macs, vouchers'), ('-os:ios', 'A minus excludes'),
]


def norm_mac(s):
    return re.sub(r'[^0-9A-F]', '', str(s or '').upper())


def parse(q):
    """'tecno os:android -is:random used>1gb' → (words, [(key, value, negated)], errors)."""
    q = (q or '').strip()
    # "used > 2 gb" → "used>2gb" so it stays one token
    q = re.sub(r'\bused\s*(>=|<=|>|<|=)\s*([\d.]+)\s*(tb|gb|mb|kb|b)?\b', lambda m: 'used' + m.group(1) + m.group(2) + (m.group(3) or 'gb'), q, flags=re.I)
    try:
        tokens = shlex.split(q)
    except ValueError:   # unbalanced quote
        tokens = q.replace('"', ' ').split()
    words, filters, errors = [], [], []
    for t in tokens:
        neg = t.startswith('-') and len(t) > 1
        body = t[1:] if neg else t
        m = USED_RE.match(body)
        if m:
            filters.append(('used', (m.group(1), float(m.group(2)) * UNITS[(m.group(3) or 'gb').lower()]), neg))
            continue
        if ':' in body:
            key, val = body.split(':', 1)
            key = key.lower()
            if key in KEYS and val:
                if key == 'is' and val.lower() not in IS_VALUES:
                    errors.append(f'is:{val} is not known — try ' + ', '.join('is:' + x for x in IS_VALUES))
                    continue
                filters.append((key, val, neg))
                continue
            if key in KEYS:
                errors.append(f'{key}: needs a value, e.g. {next((h[0] for h in HELP if h[0].startswith(key + ":")), key + ":…")}')
                continue
        words.append(t)   # a MAC like aa:bb:cc also lands here and matches as a word
    return words, filters, errors


class Context:
    """Lazy lookups shared by all filters of one search."""
    def __init__(self, business, now):
        self.b, self.now = business, now
        self._usage7 = self._online = self._slowed = self._shared = None

    def usage7(self):
        """{MAC: bytes in the last 7 days}"""
        if self._usage7 is None:
            from .models import DeviceAppUsage, UsageRecord
            since = self.now - timedelta(days=7)
            per = defaultdict(int)
            for mac, dn, up in UsageRecord.objects.filter(business=self.b, hour__gte=since).exclude(mac_address='').values_list('mac_address').annotate(d=Sum('download'), u=Sum('upload')).values_list('mac_address', 'd', 'u'):
                per[norm_mac(mac)] += (dn or 0) + (up or 0)
            if not per:   # fall back to the app split
                for mac, dn, up in DeviceAppUsage.objects.filter(business=self.b, hour__gte=since).exclude(mac='').values_list('mac').annotate(d=Sum('download'), u=Sum('upload')).values_list('mac', 'd', 'u'):
                    per[norm_mac(mac)] += (dn or 0) + (up or 0)
            self._usage7 = per
        return self._usage7

    def used(self, d):
        u = self.usage7()
        return sum(u.get(norm_mac(m), 0) for m in set(d.macs or []) | ({d.last_mac} if d.last_mac else set()))

    def online(self):
        """MACs connected right now (from the last live sync of each router)."""
        if self._online is None:
            macs = set()
            for r in self.b.routers.all():
                snap = cache.get(f'tt:tr:users:{r.pk}') or {}
                for sessions in (snap.get('users') or {}).values():
                    macs.update(norm_mac(s.get('mac')) for s in sessions)
                ipmap = cache.get(f'tt:tr:ipmap:{r.pk}') or {}
                macs.update(norm_mac(v[0]) for v in ipmap.values() if v and v[0])
            self._online = {m for m in macs if m}
        return self._online

    def slowed(self):
        if self._slowed is None:
            try:
                from .models_fup import FairUsageState
                self._slowed = {c.upper() for c in FairUsageState.objects.filter(
                    voucher__business=self.b, tier__gt=0, updated_at__gte=self.now - timedelta(minutes=30),
                    policy__active=True).values_list('voucher__code', flat=True)}
            except Exception:
                self._slowed = set()
        return self._slowed

    def shared(self):
        if self._shared is None:
            try:
                from .shared_use import cases
                self._shared = {str(c['voucher'].code).upper() for c in cases(self.b, include_resolved=False, limit=500) if c.get('open')}
            except Exception:
                self._shared = set()
        return self._shared


def _codes(d):
    return {str(v).upper() for v in (d.vouchers or [])}


def _voucher_codes_where(business, q):
    """Codes (current and old) of this business's vouchers matching q."""
    from .models import Voucher, VoucherCodeAlias
    vs = Voucher.all_objects.filter(business=business).filter(q)
    codes = {c.upper() for c in vs.values_list('code', flat=True)}
    codes |= {c.upper() for c in VoucherCodeAlias.objects.filter(voucher__in=vs).values_list('code', flat=True)}
    return codes


def _app_macs(business, now, field, value):
    from .models import DeviceAppUsage
    qs = DeviceAppUsage.objects.filter(business=business, hour__gte=now - timedelta(days=30), **{f'{field}__icontains': value}).exclude(mac='')
    return {norm_mac(m): (a, t) for m, a, t in qs.values_list('mac', field).annotate(t=Sum('download') + Sum('upload')).values_list('mac', field, 't')}


def _word_hit(d, w, ctx):
    """Why a plain word matches this device, or ''."""
    lw, mw = w.lower(), norm_mac(w)
    for label, val in (('name', d.label), ('model', d.model), ('system', f'{d.os} {d.os_version}'), ('browser', d.browser),
                       ('note', d.note), ('router', d.router.name if d.router_id and d.router else '')):
        if val and lw in str(val).lower():
            return f'{label}: {val}'
    if len(mw) >= 4 and any(mw in norm_mac(m) for m in (d.macs or [])):
        return 'MAC ' + next(m for m in d.macs if mw in norm_mac(m))
    if any(lw in str(ip) for ip in (d.ips or [])):
        return 'IP ' + next(str(ip) for ip in d.ips if lw in str(ip))
    hit = next((c for c in _codes(d) if w.upper() in c), None)
    if hit:
        return f'voucher {hit}'
    if d.fingerprint.startswith(lw):
        return 'signature'
    extra = ctx.word_codes.get(lw)
    if extra and _codes(d) & extra[0]:
        return extra[1]
    return ''


def search(business, q, base, now=None, limit=300):
    """(devices, info). Each device gets .why (list of reasons) and .used7 (bytes, when data was asked for)."""
    now = now or timezone.now()
    words, filters, errors = parse(q)
    ctx = Context(business, now)
    qs = base
    sort = 'seen'
    post = []   # (test(d) -> reason or '', negated)

    for key, val, neg in filters:
        v = val if isinstance(val, tuple) else str(val)
        if key == 'sort':
            sort = v.lower() if v.lower() in SORTS else 'seen'
            if v.lower() not in SORTS:
                errors.append('sort: can be ' + ', '.join(SORTS))
            continue
        cond = None
        if key == 'os':
            cond = Q(os__icontains=v)
            if v.lower() in ('mac', 'macos'):
                cond = Q(os__icontains='mac')
        elif key == 'type':
            t = {'computer': 'desktop', 'laptop': 'desktop', 'pc': 'desktop'}.get(v.lower(), v.lower())
            cond = Q(device_type__iexact=t) if t != 'desktop' else ~Q(device_type__in=['phone', 'tablet'])
        elif key == 'router':
            cond = Q(router__name__icontains=v)
        elif key == 'seen':
            days = SEEN.get(v.lower())
            m = re.match(r'^(\d+)\s*([dh])$', v.lower())
            if m:
                days = int(m.group(1)) / (24 if m.group(2) == 'h' else 1)
            if days is None:
                errors.append('seen: can be today, 3d, 12h, week or month')
                continue
            cond = Q(last_seen__gte=now - timedelta(days=days))
        if cond is not None:
            qs = qs.exclude(cond) if neg else qs.filter(cond)
            continue
        if key == 'mac':
            n = norm_mac(v)
            post.append((lambda d, n=n: next(('MAC ' + m for m in (d.macs or []) if n in norm_mac(m)), ''), neg))
        elif key == 'ip':
            post.append((lambda d, v=v: next(('IP ' + str(i) for i in (d.ips or []) if v in str(i)), ''), neg))
        elif key == 'voucher':
            codes = _voucher_codes_where(business, Q(code__icontains=v) | Q(code_aliases__code__icontains=v)) | {v.upper()}
            post.append((lambda d, c=codes, v=v: next((f'voucher {x}' for x in _codes(d) if x in c or v.upper() in x), ''), neg))
        elif key in ('plan', 'agent'):
            f = Q(plan_name__icontains=v) if key == 'plan' else (Q(agent__name__icontains=v) | Q(batch__agent__name__icontains=v))
            codes = _voucher_codes_where(business, f)
            post.append((lambda d, c=codes, k=key, v=v: (f'{k} {v} (voucher {sorted(_codes(d) & c)[0]})' if _codes(d) & c else ''), neg))
        elif key in ('app', 'site'):
            hits = _app_macs(business, now, 'app' if key == 'app' else 'domain', v)
            def t(d, hits=hits):
                for m in d.macs or []:
                    h = hits.get(norm_mac(m))
                    if h:
                        return f'used {h[0]} ({_size(h[1])}, 30 days)'
                return ''
            post.append((t, neg))
        elif key == 'used':
            op, n = v
            cmp = {'>': lambda a: a > n, '<': lambda a: a < n, '>=': lambda a: a >= n, '<=': lambda a: a <= n, '=': lambda a: abs(a - n) < max(n * .05, 1)}[op]
            post.append((lambda d, c=cmp: (f'{_size(ctx.used(d))} in 7 days' if c(ctx.used(d)) else ''), neg))
        elif key == 'is':
            w = v.lower()
            tests = {
                'online': lambda d: 'online now' if {norm_mac(m) for m in (d.macs or [])} & ctx.online() else '',
                'slowed': lambda d: 'slowed: ' + ', '.join(sorted(_codes(d) & ctx.slowed())) if _codes(d) & ctx.slowed() else '',
                'shared': lambda d: 'shared voucher ' + ', '.join(sorted(_codes(d) & ctx.shared())) if _codes(d) & ctx.shared() else '',
                'random': lambda d: 'random MAC' if d.random_macs else '',
                'flagged': lambda d: 'flagged' if d.flagged else '',
                'new': lambda d: 'new today' if d.first_seen and d.first_seen >= now - timedelta(hours=24) else '',
                'multimac': lambda d: f'{len(d.macs)} MAC addresses' if len(d.macs or []) > 1 else '',
            }
            post.append((tests[w], neg))

    # plain words: cheap database match first, then the reason; customer names/phones go through vouchers
    ctx.word_codes = {}
    for w in words:
        from .models import Voucher
        extra = _voucher_codes_where(business, Q(customer_name__icontains=w) | Q(customer_phone__icontains=w)) if len(w) >= 3 else set()
        if extra:
            ctx.word_codes[w.lower()] = (extra, f'customer “{w}”')
        old = _voucher_codes_where(business, Q(code_aliases__code__icontains=w)) if len(w) >= 3 else set()
        if old:   # an old code (after a code change) finds the device that used the voucher
            extra = extra | old
            ctx.word_codes.setdefault(w.lower(), (old, f'old code {w.upper()}'))
        wq = (Q(label__icontains=w) | Q(model__icontains=w) | Q(os__icontains=w) | Q(browser__icontains=w) | Q(note__icontains=w)
              | Q(macs__icontains=w.upper()) | Q(ips__icontains=w) | Q(vouchers__icontains=w.upper()) | Q(fingerprint__startswith=w.lower())
              | Q(router__name__icontains=w))
        n = norm_mac(w)
        if len(n) >= 4 and ':' not in w and re.fullmatch(r'[0-9A-Fa-f:.\-]+', w):   # MAC typed with dashes, dots or nothing
            wq |= Q(macs__icontains=':'.join(n[i:i + 2] for i in range(0, len(n) - len(n) % 2, 2)))
        if extra:
            wq |= Q(pk__in=[d.pk for d in base if _codes(d) & extra])
        qs = qs.filter(wq)

    items = []
    for d in qs.select_related('router')[:3000]:
        why = []
        for w in words:
            r = _word_hit(d, w, ctx)
            if r:
                why.append(r)
        ok = True
        for test, neg in post:
            r = test(d)
            if bool(r) == neg:
                ok = False
                break
            if r and not neg:
                why.append(r)
        if ok:
            d.why = list(dict.fromkeys(why))[:4]
            items.append(d)

    want_data = sort == 'data' or any(k == 'used' for k, _, _ in filters)
    for d in items:
        d.used7 = ctx.used(d) if want_data else None
    keyf = {'seen': lambda d: d.last_seen, 'data': lambda d: d.used7 or 0, 'visits': lambda d: d.visits,
            'macs': lambda d: len(d.macs or []), 'vouchers': lambda d: len(d.vouchers or [])}[sort]
    items.sort(key=keyf, reverse=True)
    chips = [{'text': ('-' if n else '') + (f'{k}:{v}' if k != 'used' else f'used{v[0]}{_size(v[1]).replace(" ", "").lower()}'),
              'key': k} for k, v, n in filters] + [{'text': w, 'key': 'word'} for w in words]
    return items[:limit], {'errors': errors, 'sort': sort, 'chips': chips, 'total': len(items), 'data': want_data}


def _size(n):
    n = float(n or 0)
    for unit, size in (('TB', 1024 ** 4), ('GB', 1024 ** 3), ('MB', 1024 ** 2), ('KB', 1024)):
        if n >= size:
            return f'{n / size:.1f} {unit}'
    return f'{int(n)} B'


def suggestions(business):
    """Values for the search box's suggestions (known systems, apps, plans, agents, routers)."""
    from .models import DeviceAppUsage
    since = timezone.now() - timedelta(days=30)
    oses = sorted({(o or '').split()[0].lower() for o in business.device_signatures.values_list('os', flat=True).distinct()[:50] if o})
    apps = list(DeviceAppUsage.objects.filter(business=business, hour__gte=since).exclude(app='Other').values_list('app', flat=True)
                .annotate(t=Sum('download')).order_by('-t')[:25])
    q = lambda v: f'"{v}"' if ' ' in v else v
    out = [f'os:{o}' for o in oses] + ['type:phone', 'type:tablet', 'type:computer']
    out += [f'app:{q(a.lower())}' for a in dict.fromkeys(apps)]
    out += [f'plan:{q(p)}' for p in business.plans.values_list('name', flat=True)[:30]]
    out += [f'agent:{q(a)}' for a in business.agents.values_list('name', flat=True)[:30]]
    out += [f'router:{q(r)}' for r in business.routers.values_list('name', flat=True)[:20]]
    out += [f'is:{x}' for x in IS_VALUES] + ['seen:today', 'seen:week', 'used>1gb', 'used>5gb'] + [f'sort:{s}' for s in SORTS]
    return out
