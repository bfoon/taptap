"""Open the admin page of any router / access point behind your MikroTik — from anywhere, through TapTap.

TapTap opens a short-lived path on the MikroTik (NAT rules tagged ``TT-REMOTE``) and closes it again:

* **Through TapTap** — when the MikroTik has a TapTap Tunnel (RouterOS 7). The rule forwards a port on the
  tunnel address to the device; TapTap shows the device's own admin page inside TapTap (a proxy). Works
  behind NAT / CGNAT / 4G. Nothing is opened to the Internet.
* **Direct** — when the MikroTik has a public address (Direct API, or TapTap Link without a tunnel). The
  rule forwards a random port on the router's public address to the device, **only for your current IP
  address**, and you open it straight from your browser.

Every session expires after ``SESSION_MINUTES`` (or when you press Close) and its rules are removed.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import secrets
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger('taptap.netdev')

SESSION_MINUTES = 30
PORT_RANGE = (41000, 41999)
TAG = 'TT-REMOTE'


class RemoteError(ValueError):
    pass


def _channel(router):
    from .voucher_history import channel
    return channel(router)


def router_addresses(router):
    """IPv4 addresses configured on the MikroTik (from its last sync), or None when not known yet."""
    from .models import RouterConfigSnapshot
    snap = RouterConfigSnapshot.objects.filter(router=router).first()
    rows = (((snap.sections or {}).get('IP addresses') or {}).get('rows') if snap else None)
    if not rows:
        return None
    return {str(r.get('address', '')).split('/')[0] for r in rows if r.get('address')}


def router_networks(router):
    """IPv4 networks configured on the MikroTik (from its last sync), or None when not known yet."""
    from .models import RouterConfigSnapshot
    snap = RouterConfigSnapshot.objects.filter(router=router).first()
    rows = (((snap.sections or {}).get('IP addresses') or {}).get('rows') if snap else None)
    if not rows:
        return None
    nets = []
    for r in rows:
        try:
            nets.append(ipaddress.ip_interface(str(r.get('address', ''))).network)
        except ValueError:
            pass
    return nets


def device_problem(device, others=()):
    """Why the MikroTik itself cannot reach this device's admin page (None = fine)."""
    if device.router is None:
        return None
    if any(o.pk != device.pk and o.router_id == device.router_id and o.ip == device.ip for o in others):
        return (f'Another device behind {device.router.name} also uses {device.ip} — two devices cannot share one address. '
                f'Give each its own address (in its LAN / management settings), then edit it here.')
    nets = router_networks(device.router)
    try:
        ip = ipaddress.ip_address(device.ip)
    except ValueError:
        return None
    if nets and not any(ip in n for n in nets):
        shown = ', '.join(str(n) for n in nets if not n.is_loopback and n.prefixlen < 32)[:120]
        return (f'{device.ip} is not in any network of {device.router.name} ({shown}), so the MikroTik cannot reach it. '
                f'Use the address the device has on the MikroTik’s side (its WAN / management address), or set its '
                f'management address inside one of those networks.')
    return None


def link_tunnel(router):
    """The WireGuard tunnel TapTap Link set up for this router, if it is there and alive.

    Remote admin only needs packets to flow (a recent WireGuard handshake) — not the RouterOS API login the
    management view waits for — so a tunnel that TapTap Link created is used as soon as it answers."""
    try:
        from .tunnel import get_tunnel, handshake_timeout
        t = get_tunnel(router)
    except Exception:
        return None
    if not t or not t.tunnel_ip or not getattr(t, 'router_public_key', ''):
        return None
    hs = t.last_handshake_at
    if t.status == 'online' or (hs and timezone.now() - hs <= timedelta(seconds=max(handshake_timeout(), 300))):
        return t
    return None


def reach(router):
    """('proxy', tunnel_ip) / ('direct', public_ip) / (None, reason)."""
    if router is None:
        return None, 'Choose the MikroTik this device sits behind.'
    t = link_tunnel(router)
    if t is not None:
        return 'proxy', str(t.tunnel_ip)
    if router.connection_mode != 'agent' and router.ip_address:
        host = router.ip_address.split(':')[0]
    else:
        host = getattr(getattr(router, 'agent', None), 'last_ip', '') or ''
    try:
        if host and ipaddress.ip_address(host).is_global:
            mine = router_addresses(router)
            if mine is not None and host not in mine:
                # The address TapTap sees belongs to the modem / ISP in front of the MikroTik (NAT): a port opened
                # on the MikroTik is never reached from the Internet.
                return None, (f'{router.name} is behind another router or the ISP (its Internet address {host} is not on the '
                              f'MikroTik), so a direct path cannot reach it. Turn on the TapTap Tunnel on its TapTap Link page '
                              f'(RouterOS 7) — then the admin page opens inside TapTap.')
            return 'direct', host
    except ValueError:
        if host:
            return 'direct', host     # a DNS name
    return None, (f'{router.name} has no public address and no TapTap Tunnel yet. Turn on the TapTap Tunnel on its '
                  f'TapTap Link page (RouterOS 7), or give the router a public IP.')


def nat_rules(session, device):
    """The NAT rules for one session (RouterOS field names)."""
    c = session.comment
    to = {'action': 'dst-nat', 'to-addresses': device.ip, 'to-ports': str(device.web_port), 'protocol': 'tcp', 'dst-port': str(session.port)}
    if session.mode == 'proxy':
        from .tunnel import wg_server_ip
        dst = {'chain': 'dstnat', 'dst-address': session.target_host, **to, 'comment': c}
        src = {'chain': 'srcnat', 'action': 'masquerade', 'protocol': 'tcp', 'dst-address': device.ip, 'dst-port': str(device.web_port),
               'src-address': wg_server_ip(), 'comment': c}
    else:
        dst = {'chain': 'dstnat', 'dst-address-type': 'local', 'src-address': session.client_ip, **to, 'comment': c}
        src = {'chain': 'srcnat', 'action': 'masquerade', 'protocol': 'tcp', 'dst-address': device.ip, 'dst-port': str(device.web_port),
               'src-address': session.client_ip, 'comment': c}
    return [dst, src]


def _apply(router, rules, add=True, comment=''):
    if _channel(router) == 'TapTap Link':
        from .linkops import send
        if add:
            return send(router, 'remote_nat', {'rules': rules}, label='Open a remote admin page', minutes=10)
        return send(router, 'remote_close', {'comment': comment}, label='Close a remote admin page', minutes=60)
    from .mikrotik import MikroTikService
    with MikroTikService(router) as svc:
        nat = svc.resource('/ip/firewall/nat')
        if add:
            first = next((r for r in nat.get() if r.get('id')), None)
            for r in rules:
                fields = {k.replace('-', '_'): v for k, v in r.items()}
                nat.add(place_before=first['id'], **fields) if first else nat.add(**fields)
        else:
            for r in nat.get():
                if str(r.get('comment', '')) == comment and r.get('id'):
                    nat.remove(id=r['id'])


def open_session(device, user, client_ip):
    from .models_netdev import RemoteSession
    mode, host = reach(device.router)
    if mode is None:
        raise RemoteError(host)
    if mode == 'direct' and not client_ip:
        raise RemoteError('Your own address could not be read, so a direct path cannot be limited to you.')
    used = set(RemoteSession.objects.filter(device__router=device.router, closed_at__isnull=True,
                                            expires_at__gt=timezone.now()).values_list('port', flat=True))
    port = next(p for p in (secrets.randbelow(PORT_RANGE[1] - PORT_RANGE[0]) + PORT_RANGE[0] for _ in range(50)) if p not in used)
    s = RemoteSession.objects.create(device=device, user=user, token=secrets.token_urlsafe(24), mode=mode, port=port,
                                     target_host=host, client_ip=client_ip if mode == 'direct' else '',
                                     expires_at=timezone.now() + timedelta(minutes=SESSION_MINUTES))
    try:
        cmd = _apply(device.router, nat_rules(s, device))
        if cmd is not None and getattr(cmd, 'pk', None):
            s.command_id = cmd.pk; s.save(update_fields=['command_id'])
    except Exception as exc:
        s.closed_at = timezone.now(); s.save(update_fields=['closed_at'])
        raise RemoteError(f'{device.router.name} did not accept it: {exc}')
    from .utils import log
    log(device.business, 'Remote Admin Opened', f'{device.name} ({device.ip}) by {user.get_full_name() or user.username} — {s.get_mode_display().lower()}, {SESSION_MINUTES} min')
    return s


def close_session(s):
    if s.closed_at:
        return
    s.closed_at = timezone.now(); s.save(update_fields=['closed_at'])
    try:
        _apply(s.device.router, [], add=False, comment=s.comment)
    except Exception as exc:
        logger.info('remote close %s: %s', s.pk, exc)


def close_expired():
    from .models_netdev import RemoteSession
    for s in RemoteSession.objects.filter(closed_at__isnull=True, expires_at__lte=timezone.now()).select_related('device__router'):
        close_session(s)


def direct_url(s):
    scheme = 'https' if s.device.web_https else 'http'
    return f'{scheme}://{s.target_host}:{s.port}/'


# ─────────────────────────────── proxy rewriting ───────────────────────────────

def shim(prefix):
    """Keeps the device's own scripts working under /remote/<token>/: absolute paths in XHR / fetch / forms."""
    return ('<script>(function(P){function fx(u){return (typeof u==="string"&&u.charAt(0)==="/"&&u.charAt(1)!=="/"&&u.indexOf(P)!==0)?P+u.slice(1):u}'
            'var o=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(m,u){arguments[1]=fx(u);return o.apply(this,arguments)};'
            'if(window.fetch){var f=window.fetch;window.fetch=function(u,i){return f.call(this,fx(u),i)}}'
            'var a=window.open;window.open=function(u){arguments[0]=fx(u);return a.apply(this,arguments)};'
            'document.addEventListener("submit",function(e){var t=e.target;if(t&&t.getAttribute){var x=t.getAttribute("action");if(x)t.setAttribute("action",fx(x))}},true);'
            f'}})("{prefix}");</script>')


ABS_ATTR = re.compile(r'''((?:href|src|action|data-src)\s*=\s*["'])/(?!/)''', re.I)
CSS_URL = re.compile(r'''(url\(\s*["']?)/(?!/)''', re.I)
JS_PATH = re.compile(r'''(["'])/(cgi-bin|webpages|js|css|images|img|userRpm|locale|data|api|login|admin|luci-static)/''')


def rewrite(body, ctype, prefix):
    text = body.decode('utf-8', 'replace')
    text = ABS_ATTR.sub(lambda m: m.group(1) + prefix, text)
    text = CSS_URL.sub(lambda m: m.group(1) + prefix, text)
    if 'javascript' in ctype or 'html' in ctype:
        text = JS_PATH.sub(lambda m: f'{m.group(1)}{prefix}{m.group(2)}/', text)
    if 'html' in ctype:
        s = shim(prefix)
        text = re.sub(r'(<head[^>]*>)', r'\1' + s.replace('\\', '\\\\'), text, count=1, flags=re.I) if re.search(r'<head[^>]*>', text, re.I) else s + text
    return text.encode('utf-8')



def state(s):
    """(state, message) for the session page: 'ready' / 'waiting' / 'failed' / 'ended'."""
    if s.closed_at or s.expires_at <= timezone.now():
        return 'ended', 'This session has ended.'
    if not s.command_id:
        return 'ready', 'Ready.'
    from .models import AgentCommand
    cmd = AgentCommand.objects.filter(pk=s.command_id).first()
    st = getattr(cmd, 'status', '')
    if st == 'done':
        return 'ready', 'Ready — the router opened the path.'
    if st in ('failed', 'expired'):
        return 'failed', f'{s.device.router.name} did not open the path: {getattr(cmd, "result", "") or st}.'
    return 'waiting', f'Waiting for {s.device.router.name} to open the path (next check-in, a few seconds)…'


def use_client_ip(s, ip):
    """Your browser reaches the router from another address than TapTap saw (IPv6, mobile data, a proxy):
    move the direct path to that address."""
    ipaddress.ip_address(ip)          # raises ValueError
    if s.mode != 'direct' or ip == s.client_ip or s.closed_at:
        return False
    _apply(s.device.router, [], add=False, comment=s.comment)
    s.client_ip = ip
    s.save(update_fields=['client_ip'])
    cmd = _apply(s.device.router, nat_rules(s, s.device))
    if cmd is not None and getattr(cmd, 'pk', None):
        s.command_id = cmd.pk; s.save(update_fields=['command_id'])
    return True
