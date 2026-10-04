"""TP-Link Omada controller — access points and their Wi-Fi customers, through the Omada Open API.

Set up once in Omada: Settings › Platform Integration › Open API › Add New App (mode: Client), copy the
**Client ID**, **Client Secret** and the controller's **Omada ID** (shown in the same page), and give
TapTap the controller's address (local, e.g. https://192.168.88.10:8043, reachable from your TapTap server,
or the Omada Cloud northbound address).

What TapTap does with it: list access points (status, customers, load), list Wi-Fi customers, block /
unblock a customer, reboot an access point — and block a blacklisted device on the Wi-Fi too.
Paths follow the Omada Open API v1; any error text from the controller is shown as it is.
"""
from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('taptap.omada')
TIMEOUT = 12


class OmadaError(RuntimeError):
    pass


def _ctx(c):
    if c.verify_ssl:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _call(c, method, path, body=None, token=None):
    url = c.base_url.rstrip('/') + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('Authorization', f'AccessToken={token}')
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ctx(c)) as r:
            out = json.loads(r.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as exc:
        raise OmadaError(f'Omada answered {exc.code}: {exc.read()[:200].decode("utf-8", "replace")}')
    except Exception as exc:
        raise OmadaError(f'Omada not reachable at {c.base_url}: {exc}')
    if out.get('errorCode', 0) not in (0, '0'):
        raise OmadaError(f'Omada: {out.get("msg") or out.get("errorCode")}')
    return out.get('result') if isinstance(out, dict) else out


def token(c, fresh=False):
    key = f'tt:omada:tok:{c.business_id}'
    if not fresh:
        t = cache.get(key)
        if t:
            return t
    res = _call(c, 'POST', '/openapi/authorize/token?grant_type=client_credentials',
                {'omadacId': c.omadac_id, 'client_id': c.client_id, 'client_secret': c.get_secret()})
    t = (res or {}).get('accessToken')
    if not t:
        raise OmadaError('Omada did not give a token — check the Client ID, Client Secret and Omada ID.')
    cache.set(key, t, max(60, int((res or {}).get('expiresIn', 3600)) - 120))
    return t


def api(c, method, path, body=None):
    try:
        return _call(c, method, f'/openapi/v1/{c.omadac_id}{path}', body, token(c))
    except OmadaError as exc:
        if '401' in str(exc) or 'token' in str(exc).lower():
            return _call(c, method, f'/openapi/v1/{c.omadac_id}{path}', body, token(c, fresh=True))
        raise


def _pages(c, path):
    out, page = [], 1
    while page <= 20:
        res = api(c, 'GET', f'{path}{"&" if "?" in path else "?"}page={page}&pageSize=100') or {}
        rows = res.get('data', []) if isinstance(res, dict) else (res or [])
        out += rows
        if len(rows) < 100:
            break
        page += 1
    return out


def test(c):
    """Connect, pick the first site if none chosen. Returns the sites."""
    sites = _pages(c, '/sites')
    if not c.site_id and sites:
        c.site_id, c.site_name = sites[0].get('siteId', ''), sites[0].get('name', '')
    c.last_ok_at, c.last_error = timezone.now(), ''
    c.save(update_fields=['site_id', 'site_name', 'last_ok_at', 'last_error'])
    return sites


def devices(c):
    key = f'tt:omada:dev:{c.business_id}'
    rows = cache.get(key)
    if rows is None:
        rows = _pages(c, f'/sites/{c.site_id}/devices')
        cache.set(key, rows, 60)
    return rows


def clients(c):
    key = f'tt:omada:cli:{c.business_id}'
    rows = cache.get(key)
    if rows is None:
        rows = _pages(c, f'/sites/{c.site_id}/clients')
        cache.set(key, rows, 60)
    return rows


def _fresh(c):
    cache.delete(f'tt:omada:dev:{c.business_id}'); cache.delete(f'tt:omada:cli:{c.business_id}')


def mac_api(mac):
    """Omada writes MACs as AA-BB-CC-DD-EE-FF."""
    h = ''.join(ch for ch in str(mac).upper() if ch in '0123456789ABCDEF')
    return '-'.join(h[i:i + 2] for i in range(0, 12, 2)) if len(h) == 12 else ''


def block_client(c, mac, block=True):
    api(c, 'POST', f'/sites/{c.site_id}/clients/{mac_api(mac)}/{"block" if block else "unblock"}')
    _fresh(c)


def reboot_device(c, mac):
    api(c, 'POST', f'/sites/{c.site_id}/devices/{mac_api(mac)}/reboot')
    _fresh(c)


def for_business(business):
    from .models_netdev import OmadaController
    c = OmadaController.objects.filter(business=business).first()
    return c if c and c.base_url and c.client_id and c.site_id else None


def blacklist_hook(business, macs, block=True):
    """Blacklisted devices are blocked on the Omada Wi-Fi too (best effort)."""
    c = for_business(business)
    if not c:
        return 0
    n = 0
    for m in macs:
        try:
            block_client(c, m, block); n += 1
        except Exception as exc:
            logger.info('omada blacklist %s: %s', m, exc)
    return n
