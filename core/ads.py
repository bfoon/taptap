"""Adverts and device signatures — shared helpers for views, portal export and print."""
import re

from django.db.models import F
from django.utils import timezone

from .models import Advert, AdStat, DeviceSignature

MAX_LIST = 30  # how many MACs / IPs / vouchers a signature remembers


def live_ads(business, placement):
    today = timezone.localdate()
    return [a for a in business.adverts.filter(active=True) if placement in (a.placements or []) and a.is_live(today)]


def ad_public(ad, base_url=''):
    """What a portal page or printed card needs. Click tracking goes through TapTap when it is reachable."""
    click = f'{base_url}/p/ad/{ad.pk}/go/' if base_url and ad.link else (ad.link or '')
    return {'id': ad.pk, 'headline': ad.headline, 'body': ad.body, 'cta': ad.cta or 'Learn more', 'link': ad.link,
            'click': click, 'image': ad.image, 'advertiser': ad.advertiser, 'weight': max(1, ad.weight or 1),
            'theme': ad.theme or {}, 'beacon': f'{base_url}/p/ad/{ad.pk}/seen/' if base_url else ''}


def ads_for(business, placement, base_url=''):
    return [ad_public(a, base_url) for a in live_ads(business, placement)]


def count(ad_id, field):
    today = timezone.localdate()
    Advert.objects.filter(pk=ad_id).update(**{field: F(field) + 1})
    stat, _ = AdStat.objects.get_or_create(advert_id=ad_id, day=today)
    AdStat.objects.filter(pk=stat.pk).update(**{field: F(field) + 1})


# ─────────────────────────── device signatures ───────────────────────────
MAC_RE = re.compile(r'^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$')


def parse_user_agent(ua):
    ua = str(ua or '')
    info = {'device_type': 'desktop', 'os': '', 'os_version': '', 'browser': '', 'model': ''}
    m = re.search(r'Android (\d+(?:\.\d+)?)', ua)
    if m:
        info.update(os='Android', os_version=m.group(1), device_type='tablet' if 'Mobile' not in ua else 'phone')
        mm = re.search(r'Android [^;)]*;(?: [a-z]{2}[-_][a-z]{2};)? ?([^;)]+?)(?: Build/|\))', ua)
        if mm and mm.group(1).strip().lower() not in {'k', 'linux', 'u', 'wv'}:
            info['model'] = mm.group(1).strip()[:80]
    elif re.search(r'iPhone|iPad|iPod', ua):
        dev = re.search(r'(iPhone|iPad|iPod)', ua).group(1)
        v = re.search(r'OS (\d+[_\d]*)', ua)
        info.update(os='iOS' if dev != 'iPad' else 'iPadOS', os_version=(v.group(1).replace('_', '.') if v else ''),
                    device_type='tablet' if dev == 'iPad' else 'phone', model=dev)
    elif 'Windows NT' in ua:
        v = re.search(r'Windows NT ([\d.]+)', ua)
        info.update(os='Windows', os_version={'10.0': '10/11', '6.3': '8.1', '6.1': '7'}.get(v.group(1), v.group(1)) if v else '')
    elif 'Mac OS X' in ua:
        v = re.search(r'Mac OS X ([\d_]+)', ua)
        info.update(os='macOS', os_version=v.group(1).replace('_', '.') if v else '')
    elif 'CrOS' in ua:
        info['os'] = 'ChromeOS'
    elif 'Linux' in ua:
        info['os'] = 'Linux'
    for name, pat in (('Samsung Internet', r'SamsungBrowser/'), ('Opera', r'OPR/|Opera'), ('Edge', r'Edg/'), ('Firefox', r'Firefox/|FxiOS/'),
                      ('Chrome', r'Chrome/|CriOS/'), ('Safari', r'Safari/'), ('Captive portal', r'CaptiveNetworkSupport|wispr')):
        if re.search(pat, ua):
            info['browser'] = name
            break
    return info


def _push(lst, value):
    value = str(value or '').strip()
    if not value:
        return lst
    lst = [x for x in (lst or []) if x != value]
    lst.insert(0, value)
    return lst[:MAX_LIST]


def record_device(business, fingerprint, components, ua, mac='', ip='', code='', portal=None):
    fingerprint = re.sub(r'[^0-9a-f]', '', str(fingerprint).lower())[:64]
    if len(fingerprint) < 16:
        return None
    mac = str(mac or '').upper().replace('-', ':') if MAC_RE.match(str(mac or '')) else ''
    ip = str(ip or '')[:64] if re.match(r'^[0-9a-fA-F:.]{3,64}$', str(ip or '')) else ''
    code = re.sub(r'[^A-Za-z0-9_-]', '', str(code or ''))[:40]
    info = parse_user_agent(ua)
    # Keep only small, known-safe keys from the browser.
    keep = {k: str(v)[:160] for k, v in (components or {}).items()
            if k in {'platform', 'lang', 'tz', 'screen', 'dpr', 'depth', 'cores', 'mem', 'touch', 'canvas', 'gpu', 'vendor'}}
    now = timezone.now()
    sig, created = DeviceSignature.objects.get_or_create(business=business, fingerprint=fingerprint, defaults={
        'components': keep, 'user_agent': str(ua)[:500], 'first_seen': now, 'last_seen': now, 'portal': portal, **info})
    if not created:
        sig.visits += 1
        sig.last_seen = now
        sig.user_agent = str(ua)[:500] or sig.user_agent
        for k, v in info.items():
            if v:
                setattr(sig, k, v)
        sig.components = {**(sig.components or {}), **keep}
        if portal:
            sig.portal = portal
    if mac:
        sig.macs = _push(sig.macs, mac); sig.last_mac = mac
        dev = None
        from .models import RouterDevice
        dev = RouterDevice.objects.filter(router__business=business, mac_address__iexact=mac).select_related('router').order_by('-last_seen_at').first()
        if dev:
            sig.router = dev.router
    if ip:
        sig.ips = _push(sig.ips, ip); sig.last_ip = ip
    if code:
        sig.vouchers = _push(sig.vouchers, code.upper())
    sig.save()
    return sig


def shared_vouchers(business, limit=200):
    """Voucher codes seen on more distinct device signatures than the voucher allows."""
    seen = {}
    for sig in business.device_signatures.exclude(vouchers=[]).only('id', 'vouchers', 'model', 'os', 'label', 'fingerprint'):
        for code in sig.vouchers or []:
            seen.setdefault(code, []).append(sig)
    if not seen:
        return []
    allowed = dict(business.vouchers.filter(code__in=list(seen.keys())).values_list('code', 'max_devices'))
    out = [(code, sigs, allowed.get(code, 1)) for code, sigs in seen.items() if len(sigs) > (allowed.get(code) or 1)]
    out.sort(key=lambda x: -len(x[1]))
    return out[:limit]
