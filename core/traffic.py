"""Traffic & consumption collection — runs inside every live-sync pass.

Three sources, cheapest first:
1. Interface byte counters (WAN links) → 5-minute throughput buckets with peak rates.
2. Hotspot session byte counters → consumption per voucher/device per hour, plus a
   "right now" list of who is using the most bandwidth.
3. Once a minute, the router's connection table + its DNS cache → which apps and
   sites the traffic goes to (YouTube, TikTok, WhatsApp calls, updates, …).

All counters are cumulative on the router, so TapTap stores deltas between passes
(the last reading lives in the cache). Counter resets and gaps are skipped, never
guessed.
"""
import ipaddress
import logging
import re
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db.models import F
from django.db.models.functions import Greatest
from django.utils import timezone

from .models import AppUsage, DeviceAppUsage, TrafficSample, UsageRecord

logger = logging.getLogger('taptap.traffic')
BUCKET_MIN = 5
MAX_GAP = 900          # seconds: longer gaps are not counted (router was unreachable)
APP_EVERY = int(getattr(settings, 'TRAFFIC_APP_SECONDS', 60))
MAX_CONNS = int(getattr(settings, 'TRAFFIC_MAX_CONNECTIONS', 40000))

# ───────────────────────── app recognition ─────────────────────────
# (app, category, domain fragments)
APP_RULES = [
    ('YouTube', 'Video', ['youtube', 'googlevideo', 'ytimg', 'youtu.be']),
    ('TikTok', 'Video', ['tiktok', 'tiktokv', 'tiktokcdn', 'byteoversea', 'ibyteimg', 'ibytedtos', 'musical.ly', 'bytefcdn']),
    ('Netflix', 'Video', ['netflix', 'nflxvideo', 'nflximg', 'nflxext']),
    ('Facebook', 'Social', ['facebook', 'fbcdn', 'fb.com', 'fbsbx', 'messenger.com']),
    ('Instagram', 'Social', ['instagram', 'cdninstagram']),
    ('WhatsApp', 'Messaging & calls', ['whatsapp', 'wa.me']),
    ('Snapchat', 'Social', ['snapchat', 'sc-cdn', 'snapkit']),
    ('X / Twitter', 'Social', ['twitter', 'twimg', 'x.com']),
    ('Telegram', 'Messaging & calls', ['telegram', 't.me']),
    ('Zoom', 'Messaging & calls', ['zoom.us', 'zoom.com']),
    ('Microsoft Teams', 'Messaging & calls', ['teams.microsoft', 'skype', 'lync']),
    ('Spotify', 'Music', ['spotify', 'scdn.co']),
    ('Audiomack / Boomplay', 'Music', ['audiomack', 'boomplay']),
    ('Windows Update', 'Updates & downloads', ['windowsupdate', 'update.microsoft', 'delivery.mp.microsoft', 'dl.delivery.mp', 'tlu.dl.delivery']),
    ('Apple updates & iCloud', 'Updates & downloads', ['apple.com', 'icloud', 'mzstatic', 'cdn-apple', 'aaplimg']),
    ('Google Play & Android', 'Updates & downloads', ['play.googleapis', 'android.clients', 'gvt1', 'gvt2', 'play-lh', 'googleusercontent']),
    ('Steam & games', 'Gaming', ['steam', 'valve', 'epicgames', 'riotgames', 'playstation', 'xboxlive', 'pubg', 'garena', 'freefire', 'roblox', 'activision']),
    ('Pinterest', 'Social', ['pinterest', 'pinimg']),
    ('Reddit', 'Social', ['reddit', 'redd.it', 'redditmedia', 'redditstatic']),
    ('Twitch', 'Video', ['twitch', 'jtvnw', 'ttvnw']),
    ('Disney+', 'Video', ['disneyplus', 'disney-plus', 'bamgrid', 'dssott']),
    ('Prime Video', 'Video', ['primevideo', 'aiv-cdn', 'aiv-delivery', 'pv-cdn']),
    ('Showmax / DStv', 'Video', ['showmax', 'dstv', 'multichoice']),
    ('Vimeo', 'Video', ['vimeo', 'vimeocdn']),
    ('Dailymotion', 'Video', ['dailymotion', 'dmcdn']),
    ('Apple TV & Music', 'Video', ['tv.apple', 'music.apple', 'itunes', 'aod.itunes', 'hls.apple']),
    ('Deezer', 'Music', ['deezer', 'dzcdn']),
    ('SoundCloud', 'Music', ['soundcloud', 'sndcdn']),
    ('Shazam', 'Music', ['shazam']),
    ('Imo / Viber / Signal', 'Messaging & calls', ['imo.im', 'viber', 'signal.org']),
    ('Discord', 'Messaging & calls', ['discord']),
    ('Mobile games', 'Gaming', ['supercell', 'clashofclans', 'king.com', 'unity3d', 'unityads', 'applovin', 'miniclip', 'moonton', 'mobilelegends', 'callofduty']),
    ('App downloads (Samsung, Huawei, Xiaomi)', 'Updates & downloads', ['samsungapps', 'galaxystore', 'samsungcloud', 'dbankcdn', 'hicloud', 'appgallery', 'xiaomi', 'miui', 'mi-img']),
    ('Firefox & Mozilla', 'Updates & downloads', ['mozilla', 'firefox']),
    ('GitHub', 'Updates & downloads', ['github', 'githubusercontent']),
    ('Ads & tracking', 'Web & search', ['doubleclick', 'googlesyndication', 'adservice', 'adsrvr', 'criteo', 'taboola', 'outbrain', 'adnxs', 'scorecardresearch', 'app-measurement']),
    ('News & media', 'Web & search', ['bbc', 'cnn', 'aljazeera', 'foroyaa', 'thepoint.gm', 'standard.gm', 'nytimes', 'guardian', 'reuters']),
    ('Google', 'Web & search', ['google', 'gstatic', 'googleapis', 'gmail']),
    ('Microsoft & Office', 'Web & search', ['microsoft', 'office', 'live.com', 'outlook', 'bing', 'msn']),
    ('Amazon & AWS', 'Web & search', ['amazonaws', 'amazon', 'cloudfront']),
    ('Cloudflare', 'Web & search', ['cloudflare']),
    ('Akamai CDN', 'Web & search', ['akamai', 'akamaized', 'akamaiedge']),
]
PORT_RULES = {53: ('DNS', 'System'), 123: ('Time sync', 'System'), 853: ('DNS', 'System'),
              3478: ('Voice & video calls', 'Messaging & calls'), 3479: ('Voice & video calls', 'Messaging & calls'),
              5222: ('WhatsApp', 'Messaging & calls'), 1935: ('Live streaming', 'Video'), 5228: ('Google Play & Android', 'Updates & downloads'),
              443: ('Other secure websites', 'Web & search'), 80: ('Other websites', 'Web & search')}
CATEGORY_ORDER = ['Video', 'Social', 'Messaging & calls', 'Updates & downloads', 'Music', 'Gaming', 'Web & search', 'System', 'Other']


# ───────────────────────── CDNs: who delivered it, and for which service ─────────────────────────
# A big share of traffic comes from content delivery networks. The connection only shows the CDN's
# address; the router's DNS cache tells TapTap which name the phone asked for (CNAME chain), so the
# service behind the CDN (Pinterest via Fastly, a game via Akamai…) can be named.
CDN_RULES = [
    ('Fastly', ['fastly', 'fastlylb', 'fastly-edge']),
    ('Akamai', ['akamai', 'akamaized', 'akamaiedge', 'akamaihd', 'edgekey', 'edgesuite', 'akadns', 'akamaitechnologies']),
    ('Cloudflare', ['cloudflare', 'cdn.cloudflare', 'cloudflare-dns', 'cf-ipfs', 'workers.dev', 'pages.dev']),
    ('Amazon CloudFront', ['cloudfront']),
    ('Amazon AWS', ['amazonaws', 'awsglobalaccelerator', 'elb.amazonaws']),
    ('Google Cloud CDN', ['googleusercontent', '1e100.net', 'gvt1', 'gvt2', 'googlehosted', 'c.docs.google']),
    ('Microsoft Azure CDN', ['azureedge', 'msecnd', 'azurefd', 'trafficmanager', 'azure', 'footprint.net']),
    ('Edgio / Limelight', ['llnwd', 'llnwi', 'edgecastcdn', 'systemcdn', 'edgio', 'lldns']),
    ('Bunny CDN', ['b-cdn', 'bunnycdn']),
    ('StackPath', ['stackpathdns', 'stackpathcdn', 'hwcdn', 'netdna']),
    ('jsDelivr', ['jsdelivr']),
    ('CDN77', ['cdn77']),
    ('Alibaba CDN', ['alicdn', 'alikunlun', 'kunlun', 'aliyuncs']),
    ('Tencent CDN', ['tcdn', 'qcloud', 'dnsv1', 'cdntip']),
    ('ByteDance CDN', ['bytecdn', 'byteimg', 'ibyteimg', 'bytefcdn', 'bytedns', 'tiktokcdn']),
    ('Meta CDN', ['fbcdn', 'cdninstagram', 'whatsapp.net']),
    ('Apple CDN', ['aaplimg', 'cdn-apple', 'mzstatic']),
]
GENERIC_APPS = {'Other', 'Other secure websites', 'Other websites', 'Akamai CDN', 'Cloudflare', 'Amazon & AWS'}


def cdn_of(host):
    low = str(host or '').lower()
    for name, frags in CDN_RULES:
        if any(f in low for f in frags):
            return name
    return ''


def service_hint(cdn_host):
    """Name a CDN host may carry inside it: dualstack.pinterest.map.fastly.net → pinterest,
    www.example.com.edgekey.net → www.example.com, abc.akamaized.net → abc."""
    h = str(cdn_host or '').lower().rstrip('.')
    m = re.match(r'^(?:dualstack\.)?([a-z0-9-]+)\.(?:map|global|freetls|ssl)\.fastly\.net$', h)
    if m:
        return m.group(1)
    m = re.match(r'^(.+?)\.(?:edgekey|edgesuite|akamaized|akamaihd)\.net$', h)
    if m:
        return m.group(1)
    return ''


def hint_category(name):
    low = str(name or '').lower()
    if any(w in low for w in ('video', 'vod', 'hls', 'dash', 'stream', 'live', 'media', 'tv')):
        return 'Video'
    if any(w in low for w in ('audio', 'music', 'radio', 'podcast', 'sound')):
        return 'Music'
    if any(w in low for w in ('update', 'download', 'dl.', 'apps', 'store', 'apk', 'cdn-dl', 'patch', 'game')):
        return 'Updates & downloads'
    return ''


def registered_domain(name):
    parts = [p for p in str(name or '').lower().strip('.').split('.') if p]
    if len(parts) <= 2:
        return '.'.join(parts)
    # keep 3 labels for country second-level domains (co.uk, com.gm, …)
    if len(parts[-1]) == 2 and parts[-2] in {'co', 'com', 'net', 'org', 'gov', 'edu', 'ac'}:
        return '.'.join(parts[-3:])
    return '.'.join(parts[-2:])


def classify(name, port=None, proto=''):
    low = str(name or '').lower()
    if low:
        for app, cat, frags in APP_RULES:
            if any(f in low for f in frags):
                return app, cat, registered_domain(low)
    if proto == 'udp' and port and 50000 <= port <= 65535:
        return 'Voice & video calls', 'Messaging & calls', registered_domain(low) or 'unresolved'
    if port in PORT_RULES:
        app, cat = PORT_RULES[port]
        return app, cat, registered_domain(low) or 'unresolved'
    return ('Other', 'Other', registered_domain(low) or 'unresolved')


def classify_via(value, port=None, proto=''):
    """(app, category, domain, cdn) for a DNS-map value ("name" or "name<TAB>cdn-host")."""
    name, _, cdn_host = str(value or '').partition('\t')
    via = cdn_of(cdn_host or name)
    app, cat, domain = classify(name, port, proto)
    if via and app in GENERIC_APPS:
        hint = service_hint(cdn_host or name)
        if hint:
            a2, c2, _ = classify(hint, port, proto)
            if a2 not in GENERIC_APPS:
                app, cat = a2, c2
        if app in GENERIC_APPS:
            # still unknown: keep the site the phone asked for, and guess what it is from its name
            app = 'Other sites'
            cat = hint_category(name + ' ' + cdn_host) or 'Web & search'
            domain = registered_domain(name) if name and not cdn_of(name) else (hint or registered_domain(name))
    return app, cat, domain, via


# ───────────────────────── helpers ─────────────────────────
def _int(v):
    try:
        return int(str(v or 0).strip() or 0)
    except ValueError:
        return 0


def floor_bucket(dt):
    return dt.replace(minute=dt.minute - dt.minute % BUCKET_MIN, second=0, microsecond=0)


def floor_hour(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def _is_private(ip):
    try:
        a = ipaddress.ip_address(ip)
        return a.is_private or a.is_link_local or ipaddress.ip_address(ip) in ipaddress.ip_network('100.64.0.0/10')
    except ValueError:
        return False


def _split_addr(value):
    s = str(value or '')
    if s.count(':') == 1:
        ip, _, port = s.partition(':')
        return ip, _int(port)
    m = re.match(r'^\[?([0-9a-fA-F:]+)\]?:(\d+)$', s)
    return (m.group(1), int(m.group(2))) if m else (s, None)


def wan_interfaces(router):
    try:
        lb = router.config_snapshot.load_balancing or {}
    except Exception:
        lb = {}
    names = [l.get('interface') for l in lb.get('wan_links', []) if l.get('interface')]
    if not names:
        names = list(router.interface_roles.filter(role='wan').values_list('interface_name', flat=True))
    return [n for n in dict.fromkeys(names) if n]


def _add_sample(router, interface, bucket, rx, tx, rx_bps, tx_bps):
    obj, created = TrafficSample.objects.get_or_create(router=router, interface=interface, bucket=bucket,
                                                       defaults={'rx_bytes': rx, 'tx_bytes': tx, 'rx_peak_bps': rx_bps, 'tx_peak_bps': tx_bps, 'samples': 1})
    if not created:
        TrafficSample.objects.filter(pk=obj.pk).update(rx_bytes=F('rx_bytes') + rx, tx_bytes=F('tx_bytes') + tx,
                                                        rx_peak_bps=Greatest('rx_peak_bps', rx_bps), tx_peak_bps=Greatest('tx_peak_bps', tx_bps),
                                                        samples=F('samples') + 1)


# ───────────────────────── collection ─────────────────────────
def collect(router, svc, active, now=None):
    """Called by live sync after it has read the active sessions. Returns a small summary."""
    now = now or timezone.now()
    out = {'wan_bytes': 0, 'user_bytes': 0, 'apps': 0}
    bucket, hour = floor_bucket(now), floor_hour(now)
    ts = now.timestamp()

    # 1) WAN interface counters
    wans = wan_interfaces(router)
    if wans:
        key = f'tt:tr:if:{router.pk}'
        prev = cache.get(key) or {}
        cur = {}
        try:
            for row in svc.interfaces():
                name = str(row.get('name', ''))
                if name in wans:
                    cur[name] = (_int(row.get('rx-byte')), _int(row.get('tx-byte')), ts)
        except Exception as exc:
            logger.info('interface read %s: %s', router, exc)
        for name, (rx, tx, t) in cur.items():
            p = prev.get(name)
            if p and 3 <= t - p[2] <= MAX_GAP and rx >= p[0] and tx >= p[1]:
                drx, dtx, dt = rx - p[0], tx - p[1], t - p[2]
                _add_sample(router, name, bucket, drx, dtx, int(drx * 8 / dt), int(dtx * 8 / dt))
                out['wan_bytes'] += drx + dtx
        cache.set(key, cur, 3600)

    # 2) hotspot sessions → per user consumption + "right now"
    out['user_bytes'] = collect_sessions(router, active, now)

    # 3) apps & sites, once a minute
    if cache.add(f'tt:tr:app:gate:{router.pk}', 1, APP_EVERY):
        try:
            out['apps'] = collect_apps(router, svc, now)
        except Exception as exc:
            logger.info('app sampling %s: %s', router, exc)
    return out


def _dns_map(router, svc):
    key = f'tt:tr:dns:{router.pk}'
    m = cache.get(key)
    if m is not None:
        return m
    m = {}
    try:
        m = dns_map_from_rows(svc.safe_get('/ip/dns/cache'))
    except Exception as exc:
        logger.info('dns cache %s: %s', router, exc)
    cache.set(key, m, 600)
    return m


def _connections(svc):
    res = svc.resource('/ip/firewall/connection')
    try:
        rows = res.call('print', {'.proplist': '.id,src-address,dst-address,reply-src-address,protocol,orig-bytes,repl-bytes'})
        if rows is not None:
            return rows
    except Exception:
        pass
    return res.get() or []


def collect_sessions(router, active, now=None):
    """Per-user consumption and the "right now" list from hotspot session counters.
    Used by live sync (API routers) and TapTap Link (routers behind NAT). Returns bytes counted."""
    now = now or timezone.now()
    bucket, hour = floor_bucket(now), floor_hour(now)
    ts = now.timestamp()
    skey = f'tt:tr:ss:{router.pk}'
    prev = cache.get(skey) or {}
    first_pass = not prev
    cur, per_user, now_list = {}, defaultdict(lambda: [0, 0, 0]), []
    tot_down = tot_up = 0
    dt_all = None
    from .sync import _routeros_seconds
    for s in active:
        sid = str(s.get('id', ''))
        user = str(s.get('user', '')).strip() or '(unknown)'
        mac = str(s.get('mac-address', '')).upper()
        b_up, b_down = _int(s.get('bytes-in')), _int(s.get('bytes-out'))   # router's view: in = from customer
        cur[sid] = (b_up, b_down, ts, user, mac)
        p = prev.get(sid)
        d_up = d_down = 0
        rate_down = rate_up = 0
        if p and p[3] == user and b_up >= p[0] and b_down >= p[1]:
            # Every byte between two readings is real use — counted even after a long gap (fair usage must
            # not miss data because a sync was late). Speed is only worked out over a normal gap.
            d_up, d_down = b_up - p[0], b_down - p[1]
            dt = ts - p[2]
            if 3 <= dt <= MAX_GAP:
                dt_all = dt
                rate_down, rate_up = int(d_down * 8 / dt), int(d_up * 8 / dt)
        elif p:
            # Same session id but the counters went back (router reused the id): all of it is new.
            d_up, d_down = b_up, b_down
        elif not first_pass or (not s.get('bypass') and _routeros_seconds(s.get('uptime')) <= MAX_GAP):
            # (bypass hosts have no reliable uptime: on the very first reading only their baseline is taken)
            # A session TapTap has not seen before: everything it used is new.
            d_up, d_down = b_up, b_down
        if d_up or d_down:
            agg = per_user[(user, mac)]
            agg[0] += d_down; agg[1] += d_up; agg[2] = max(agg[2], rate_down)
            tot_down += d_down; tot_up += d_up
        now_list.append({'user': user, 'mac': mac, 'ip': str(s.get('address', '')), 'sid': sid, 'down_bps': rate_down, 'up_bps': rate_up,
                         'session_down': b_down, 'session_up': b_up, 'uptime': str(s.get('uptime', ''))})
    # Keep the last reading of sessions missing from this read for an hour: a read that briefly
    # misses a session must not make TapTap count that session's whole history again.
    for sid, p in prev.items():
        if sid not in cur and ts - p[2] < 3600:
            cur[sid] = p
    cache.set(skey, cur, 2 * 86400)
    ipmap = {x['ip']: [x['mac'], x['user']] for x in now_list if x['ip']}
    if ipmap:
        cache.set(f'tt:tr:ipmap:{router.pk}', ipmap, 3600)
    # every session's live speed, by voucher code (the "now" list above keeps only the top 25)
    by_user = defaultdict(list)
    for x in now_list:
        by_user[x['user'].upper()].append({k: x[k] for k in ('ip', 'mac', 'down_bps', 'up_bps', 'session_down', 'session_up', 'uptime')})
    cache.set(f'tt:tr:users:{router.pk}', {'at': now.isoformat(), 'users': dict(by_user)}, 600)
    for (user, mac), (dn, up, peak) in per_user.items():
        obj, created = UsageRecord.objects.get_or_create(router=router, username=user, mac_address=mac, hour=hour,
                                                         defaults={'business': router.business, 'download': dn, 'upload': up, 'peak_bps': peak})
        if not created:
            UsageRecord.objects.filter(pk=obj.pk).update(download=F('download') + dn, upload=F('upload') + up, peak_bps=Greatest('peak_bps', peak))
    if tot_down or tot_up:
        _add_sample(router, '*users', bucket, tot_down, tot_up, int(tot_down * 8 / dt_all) if dt_all else 0, int(tot_up * 8 / dt_all) if dt_all else 0)
    now_list.sort(key=lambda x: -(x['down_bps'] + x['up_bps']))
    cache.set(f'tt:tr:now:{router.pk}', {'at': now.isoformat(), 'sessions': now_list[:25], 'count': len(now_list),
                                         'down_bps': sum(x['down_bps'] for x in now_list), 'up_bps': sum(x['up_bps'] for x in now_list)}, 600)
    return tot_down + tot_up


def collect_apps(router, svc, now):
    try:
        tracking = svc.safe_get('/ip/firewall/connection/tracking')
        total = _int(tracking[0].get('total-entries')) if tracking else 0
        if total > MAX_CONNS:
            logger.info('%s has %s connections — app sampling skipped', router, total)
            return 0
    except Exception:
        pass
    return ingest_connections(router, _connections(svc), _dns_map(router, svc), now)


def dns_map_from_rows(rows):
    """address → name from RouterOS DNS cache rows (v6: address, v7: type/data).

    The name is the one the phone asked for: CNAME chains are followed back, so an address of
    dualstack.pinterest.map.fastly.net maps to i.pinimg.com. When a CDN is in the chain the value
    is "asked-for-name<TAB>cdn-host"."""
    rev, a_rows = {}, []
    for r in rows or []:
        name = str(r.get('name', '')).lower().rstrip('.')
        typ = str(r.get('type', 'A')).upper()
        if typ == 'CNAME':
            target = str(r.get('data', '')).lower().rstrip('.')
            if name and target:
                rev.setdefault(target, name)
            continue
        addr = str(r.get('address') or (r.get('data') if typ in {'A', 'AAAA'} else '') or '')
        if name and addr:
            a_rows.append((addr, name))
    m = {}
    for addr, name in a_rows:
        if addr in m:
            continue
        origin, chain, hops = name, [name], 0
        while origin in rev and hops < 6:
            origin = rev[origin]; chain.append(origin); hops += 1
        cdn_host = next((h for h in chain if cdn_of(h)), '')
        m[addr] = f'{origin}\t{cdn_host}' if cdn_host and cdn_host != origin else origin
    return m


def ingest_connections(router, conns, dns, now):
    """Turn two connection-table samples into per-app / per-site traffic (API, tunnel and TapTap Link)."""
    key = f'tt:tr:ct:{router.pk}'
    prev = cache.get(key)
    cur = {}
    agg = defaultdict(lambda: [0, 0])
    per_dev = defaultdict(lambda: [0, 0])     # (client ip, app, category, domain) → [down, up]
    busy = defaultdict(int)                    # busy connections per customer (Internet sharing signal)
    for c in conns:
        cid = str(c.get('id') or c.get('.id') or '')
        src_ip, _ = _split_addr(c.get('src-address'))
        dst_ip, port = _split_addr(c.get('dst-address'))
        if not cid or not _is_private(src_ip) or _is_private(dst_ip):
            continue  # only customer → Internet traffic
        busy[src_ip] += 1
        up, down = _int(c.get('orig-bytes')), _int(c.get('repl-bytes'))
        if up + down < 1024:
            continue
        cur[cid] = (up, down)
        if prev is None:
            continue  # first sample only sets the baseline
        p = prev.get(cid)
        if p:
            if up < p[0] or down < p[1]:
                continue
            d_up, d_down = up - p[0], down - p[1]
        else:
            d_up, d_down = up, down  # connection opened since the last sample
        if d_up + d_down <= 0:
            continue
        app, cat, domain, via = classify_via(dns.get(dst_ip, ''), port, str(c.get('protocol', '')).lower())
        a = agg[(app, cat, domain, via)]
        a[0] += d_down; a[1] += d_up
        d = per_dev[(src_ip, app, cat, domain, via)]
        d[0] += d_down; d[1] += d_up
    cache.set(key, cur, 3600)
    if not agg:
        return 0
    hour = floor_hour(now)
    try:
        _store_per_device(router, per_dev, hour)
    except Exception as exc:   # the per-router report must never suffer from this
        logger.info('device app usage %s: %s', router, exc)
    try:
        from .sharing import note_connections
        note_connections(router, busy)
    except Exception:
        pass
    rows = sorted(agg.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))
    keep, rest = rows[:60], rows[60:]
    if rest:
        other = [sum(v[0] for _, v in rest), sum(v[1] for _, v in rest)]
        keep.append((('Other', 'Other', 'many sites', ''), other))
    for (app, cat, domain, via), (dn, up) in keep:
        obj, created = AppUsage.objects.get_or_create(router=router, hour=hour, app=app, domain=domain[:120],
                                                      defaults={'business': router.business, 'category': cat, 'via': via[:40], 'download': dn, 'upload': up})
        if not created:
            AppUsage.objects.filter(pk=obj.pk).update(download=F('download') + dn, upload=F('upload') + up,
                                                      **({'via': via[:40]} if via and not obj.via else {}))
    return len(keep)


DEVICE_TOP = 25   # sites kept per device per sample; the rest is summed as "Other"


def _store_per_device(router, per_dev, hour):
    """Save the sample split by device: IP → MAC and voucher from the live hotspot sessions."""
    ipmap = cache.get(f'tt:tr:ipmap:{router.pk}') or {}
    by_ip = defaultdict(list)
    for (ip, app, cat, domain, via), v in per_dev.items():
        by_ip[ip].append(((app, cat, domain, via), v))
    for ip, rows in by_ip.items():
        mac, user = (ipmap.get(ip) or ['', ''])
        rows.sort(key=lambda kv: -(kv[1][0] + kv[1][1]))
        keep, rest = rows[:DEVICE_TOP], rows[DEVICE_TOP:]
        if rest:
            keep.append((('Other', 'Other', 'many sites', ''), [sum(v[0] for _, v in rest), sum(v[1] for _, v in rest)]))
        for (app, cat, domain, via), (dn, up) in keep:
            obj, created = DeviceAppUsage.objects.get_or_create(
                router=router, hour=hour, mac=(mac or '').upper()[:32], ip=ip[:45], app=app, domain=domain[:120],
                defaults={'business': router.business, 'category': cat, 'via': via[:40], 'username': (user or '')[:120], 'download': dn, 'upload': up})
            if not created:
                DeviceAppUsage.objects.filter(pk=obj.pk).update(download=F('download') + dn, upload=F('upload') + up,
                                                                **({'via': via[:40]} if via and not obj.via else {}))


def prune():
    """Keep the tables small: 5-minute detail for 90 days, hourly usage for 180 days."""
    now = timezone.now()
    TrafficSample.objects.filter(bucket__lt=now - timedelta(days=90)).delete()
    DeviceAppUsage.objects.filter(hour__lt=now - timedelta(days=45)).delete()   # per-device detail: 45 days
    AppUsage.objects.filter(hour__lt=now - timedelta(days=90)).delete()
    UsageRecord.objects.filter(hour__lt=now - timedelta(days=180)).delete()
