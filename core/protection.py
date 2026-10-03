"""Security → Protection: DDoS prevention and intrusion detection/prevention on the MikroTik, one click each.

A MikroTik has no packet-inspection IDS engine (no Suricata/Snort). What it does very well — and what this
switches on — is the firewall defence that *detects attack patterns and blocks the attacker* by itself:

**DDoS protection** (chain ``taptap-ddos``, for new/invalid connections TO the router)
  * TCP SYN cookies (``/ip settings tcp-syncookies=yes``) — SYN floods cannot fill the router's tables;
  * invalid packets dropped;
  * a source opening more than 100 TCP connections to the router at once is blocked for 1 hour.

**Intrusion detection & prevention** (chains ``taptap-ips`` and ``taptap-ips-fwd``)
  * port scans (``psd``) against the router — the scanner is blocked for a day;
  * password guessing on WinBox, SSH, API, Telnet, FTP — the 4th new login connection within about a
    minute puts the source on the block list for 1 hour (stages ``taptap-bf-1/2/3``);
  * hotspot clients that port-scan or flood mail servers (spam bots) — blocked for 1 hour.
  Every detection is logged with the prefix "TapTap IPS" / "TapTap DDoS".

Safety
  * TapTap's own address and the TapTap Tunnel network are on ``taptap-trusted`` and always skip the checks,
    so TapTap can never lock itself out; established connections are never touched.
  * Everything carries a "TapTap DDoS" / "TapTap IPS" comment and lives in its own chains: **Disable** removes
    exactly those rules and lists, nothing else. **Unblock all** empties the block lists.
Works over the RouterOS API (direct / tunnel) and TapTap Link (same rules, as a RouterOS script).
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger('taptap.protection')
MGMT_PORTS = '21,22,23,8291,8728,8729'
RULES_VERSION = 'v2'   # v2: hotspot customers are never caught by the DDoS / scan checks (see rules())
FEATURES = {
    'ddos': {'label': 'DDoS protection', 'tag': 'TapTap DDoS', 'lists': ['taptap-ddos-blocked']},
    'ips': {'label': 'Intrusion prevention (IDS/IPS)', 'tag': 'TapTap IPS',
            'lists': ['taptap-ips-blocked', 'taptap-bf-1', 'taptap-bf-2', 'taptap-bf-3', 'taptap-spam']},
}


def trusted_addresses():
    """TapTap's own address(es) and the tunnel network: never inspected, never blocked."""
    out = []
    host = urlparse(getattr(settings, 'SITE_URL', '') or '').hostname
    extra = getattr(settings, 'PROTECTION_TRUSTED', '') or ''
    for a in [x.strip() for x in extra.split(',') if x.strip()]:
        out.append(a)
    if host:
        try:
            ipaddress.ip_address(host); out.append(host)
        except ValueError:
            try:
                old = socket.getdefaulttimeout(); socket.setdefaulttimeout(3)
                out += sorted({i[4][0] for i in socket.getaddrinfo(host, 443, socket.AF_INET)})
            except OSError:
                pass
            finally:
                socket.setdefaulttimeout(old)
    try:
        from .tunnel import tunnel_enabled, wg_network
        if tunnel_enabled():
            out.append(str(wg_network()))
    except Exception:
        pass
    return [a for i, a in enumerate(out) if a not in out[:i]]


def rules(feature):
    """[(menu, {field: value}, top_of_chain)] — the RouterOS rows of one feature, in order."""
    t = FEATURES[feature]['tag']
    if feature == 'ddos':
        # Hotspot customers are left out: before they log in, EVERY web connection of every app on their phone
        # is redirected to the router, so a normal phone looked like a flood and got blocked for an hour —
        # the login page vanished and the phone "could not connect".
        return [
            ('/ip/firewall/filter', {'chain': 'input', 'action': 'jump', 'jump-target': 'taptap-ddos', 'connection-state': 'new,invalid',
                                     'hotspot': '!from-client', 'comment': f'{t}: check new connections to the router ({RULES_VERSION})'}, True),
            ('/ip/firewall/filter', {'chain': 'taptap-ddos', 'action': 'return', 'src-address-list': 'taptap-trusted', 'comment': f'{t}: TapTap itself'}, False),
            ('/ip/firewall/filter', {'chain': 'taptap-ddos', 'action': 'drop', 'connection-state': 'invalid', 'comment': f'{t}: invalid packets'}, False),
            ('/ip/firewall/filter', {'chain': 'taptap-ddos', 'action': 'drop', 'src-address-list': 'taptap-ddos-blocked', 'comment': f'{t}: blocked sources'}, False),
            ('/ip/firewall/filter', {'chain': 'taptap-ddos', 'action': 'add-src-to-address-list', 'protocol': 'tcp', 'tcp-flags': 'syn',
                                     'connection-limit': '100,32', 'address-list': 'taptap-ddos-blocked', 'address-list-timeout': '1h',
                                     'log': 'yes', 'log-prefix': 'TapTap DDoS', 'comment': f'{t}: connection flood - block 1h'}, False),
        ]
    return [
        # Hotspot customers: only password guessing on the management ports is checked — their apps opening
        # many ports while logging in is normal and must not look like a port scan.
        ('/ip/firewall/filter', {'chain': 'input', 'action': 'jump', 'jump-target': 'taptap-ips', 'connection-state': 'new',
                                 'hotspot': '!from-client', 'comment': f'{t}: inspect new connections to the router ({RULES_VERSION})'}, True),
        ('/ip/firewall/filter', {'chain': 'input', 'action': 'jump', 'jump-target': 'taptap-ips', 'connection-state': 'new', 'protocol': 'tcp',
                                 'dst-port': MGMT_PORTS, 'hotspot': 'from-client', 'comment': f'{t}: hotspot customers - management ports only'}, True),
        # Clients: logged-in hotspot customers and other LAN devices; never phones still on the login page
        ('/ip/firewall/filter', {'chain': 'forward', 'action': 'jump', 'jump-target': 'taptap-ips-fwd', 'connection-state': 'new',
                                 'hotspot': 'auth', 'comment': f'{t}: inspect new connections of logged-in customers'}, True),
        ('/ip/firewall/filter', {'chain': 'forward', 'action': 'jump', 'jump-target': 'taptap-ips-fwd', 'connection-state': 'new',
                                 'hotspot': '!from-client', 'comment': f'{t}: inspect new connections of other clients'}, True),
        # to the router
        ('/ip/firewall/filter', {'chain': 'taptap-ips', 'action': 'return', 'src-address-list': 'taptap-trusted', 'comment': f'{t}: TapTap itself'}, False),
        ('/ip/firewall/filter', {'chain': 'taptap-ips', 'action': 'drop', 'src-address-list': 'taptap-ips-blocked', 'comment': f'{t}: blocked sources'}, False),
        ('/ip/firewall/filter', {'chain': 'taptap-ips', 'action': 'add-src-to-address-list', 'protocol': 'tcp', 'psd': '21,3s,3,1',
                                 'address-list': 'taptap-ips-blocked', 'address-list-timeout': '1d', 'log': 'yes', 'log-prefix': 'TapTap IPS portscan',
                                 'comment': f'{t}: port scan - block 1 day'}, False),
        ('/ip/firewall/filter', {'chain': 'taptap-ips', 'action': 'add-src-to-address-list', 'protocol': 'tcp', 'dst-port': MGMT_PORTS,
                                 'src-address-list': 'taptap-bf-3', 'address-list': 'taptap-ips-blocked', 'address-list-timeout': '1h',
                                 'log': 'yes', 'log-prefix': 'TapTap IPS bruteforce', 'comment': f'{t}: password guessing - block 1h'}, False),
        ('/ip/firewall/filter', {'chain': 'taptap-ips', 'action': 'add-src-to-address-list', 'protocol': 'tcp', 'dst-port': MGMT_PORTS,
                                 'src-address-list': 'taptap-bf-2', 'address-list': 'taptap-bf-3', 'address-list-timeout': '1m', 'comment': f'{t}: login attempt 3'}, False),
        ('/ip/firewall/filter', {'chain': 'taptap-ips', 'action': 'add-src-to-address-list', 'protocol': 'tcp', 'dst-port': MGMT_PORTS,
                                 'src-address-list': 'taptap-bf-1', 'address-list': 'taptap-bf-2', 'address-list-timeout': '1m', 'comment': f'{t}: login attempt 2'}, False),
        ('/ip/firewall/filter', {'chain': 'taptap-ips', 'action': 'add-src-to-address-list', 'protocol': 'tcp', 'dst-port': MGMT_PORTS,
                                 'address-list': 'taptap-bf-1', 'address-list-timeout': '1m', 'comment': f'{t}: login attempt 1'}, False),
        # clients (hotspot): scanners and spam bots
        ('/ip/firewall/filter', {'chain': 'taptap-ips-fwd', 'action': 'return', 'src-address-list': 'taptap-trusted', 'comment': f'{t}: TapTap itself'}, False),
        ('/ip/firewall/filter', {'chain': 'taptap-ips-fwd', 'action': 'drop', 'src-address-list': 'taptap-ips-blocked', 'comment': f'{t}: blocked sources'}, False),
        ('/ip/firewall/filter', {'chain': 'taptap-ips-fwd', 'action': 'add-src-to-address-list', 'protocol': 'tcp', 'psd': '21,3s,3,1',
                                 'address-list': 'taptap-ips-blocked', 'address-list-timeout': '1h', 'log': 'yes', 'log-prefix': 'TapTap IPS client scan',
                                 'comment': f'{t}: client port scan - block 1h'}, False),
        ('/ip/firewall/filter', {'chain': 'taptap-ips-fwd', 'action': 'add-src-to-address-list', 'protocol': 'tcp', 'dst-port': '25',
                                 'connection-limit': '10,32', 'address-list': 'taptap-spam', 'address-list-timeout': '1h', 'log': 'yes',
                                 'log-prefix': 'TapTap IPS spam', 'comment': f'{t}: mail flood (spam bot)'}, False),
        ('/ip/firewall/filter', {'chain': 'taptap-ips-fwd', 'action': 'drop', 'protocol': 'tcp', 'dst-port': '25', 'src-address-list': 'taptap-spam',
                                 'comment': f'{t}: block spam bots on port 25'}, False),
    ]


# ─────────────────────────── status (from TapTap's copy of the router) ───────────────────────────
def status(router):
    """{'ddos': {'on', 'rules', 'blocked'}, 'ips': {...}, 'pending': feature|None, 'checked_at'}"""
    from django.core.cache import cache
    from .routeros_analysis import g
    from .models import RouterConfigSnapshot
    snap = RouterConfigSnapshot.objects.filter(router=router).first()      # always the latest copy
    sec, checked = ((snap.sections or {}), snap.captured_at) if snap else ({}, None)
    filt = (sec.get('Firewall filter') or {}).get('rows') or []
    lists = (sec.get('Firewall address lists') or {}).get('rows') or []
    out = {'checked_at': checked}
    for key, f in FEATURES.items():
        n = sum(1 for r in filt if str(g(r, 'comment') or '').startswith(f['tag']))
        blocked = sum(1 for r in lists if str(g(r, 'list')) == f['lists'][0])
        out[key] = {'key': key, 'on': n > 0, 'rules': n, 'blocked': blocked, 'label': f['label'],
                    'pending': cache.get(f'tt:protect:{router.pk}:{key}')}
    out['features'] = [out[k] for k in FEATURES]
    # rules from before v2 can block hotspot customers on the login page: they are updated automatically
    out['outdated'] = [k for k, f in FEATURES.items() if out[k]['on'] and not any(
        str(g(r, 'comment') or '').startswith(f['tag']) and RULES_VERSION in str(g(r, 'comment') or '') for r in filt)]
    return out


def upgrade(router):
    """Re-apply protection that is on with old rules (once per router; clears their block lists too)."""
    from django.core.cache import cache
    key = f'tt:protect:upgraded:{router.pk}:{RULES_VERSION}'
    if cache.get(key):
        return []
    done = []
    try:
        st = status(router)
    except Exception:
        return []
    for feature in st.get('outdated', []):
        try:
            apply(router, feature, 'enable')
            done.append(feature)
        except Exception as exc:
            logger.info('protection upgrade %s %s: %s', router, feature, exc)
            return done
    cache.set(key, 1, 86400 * 30)
    return done


# ─────────────────────────── apply ───────────────────────────
def _audit(router, user, op, feature, ok=True, error=''):
    from .models import RouterConfigChange
    from .utils import log
    RouterConfigChange.objects.create(business=router.business, router=router, actor=user if getattr(user, 'is_authenticated', False) else None,
                                      resource_path='/ip/firewall/filter', operation=op, target_id=feature,
                                      fields={'feature': FEATURES[feature]['label']}, status='success' if ok else 'failed', error=error[:500])
    if ok:
        log(router.business, 'Security Protection', f'{router.name}: {FEATURES[feature]["label"]} {op}')


def _refresh_snapshot(svc, router):
    from .models import RouterConfigSnapshot
    snap, _ = RouterConfigSnapshot.objects.get_or_create(router=router)
    sections = dict(snap.sections or {})
    for label, path in (('Firewall filter', '/ip/firewall/filter'), ('Firewall address lists', '/ip/firewall/address-list')):
        rows = svc.safe_get(path)
        sections[label] = {'path': path, 'rows': [dict(x) for x in rows[:500]], 'count': len(rows)}
    snap.sections = sections
    snap.save(update_fields=['sections', 'updated_at'])


def _api_remove(svc, tag, lists):
    for path in ('/ip/firewall/filter', '/ip/firewall/raw'):
        res = svc.resource(path)
        for row in res.get():
            if str(row.get('comment', '')).startswith(tag):
                res.remove(id=row['id'])
    res = svc.resource('/ip/firewall/address-list')
    for row in res.get():
        if row.get('list') in lists:
            res.remove(id=row['id'])


def _api_enable(svc, feature):
    """Remove any older copy, make sure TapTap is trusted, then add the rules (jumps at the top of their chain)."""
    f = FEATURES[feature]
    _api_remove(svc, f['tag'], f['lists'])
    alist = svc.resource('/ip/firewall/address-list')
    have = {(r.get('list'), r.get('address')) for r in alist.get()}
    for a in trusted_addresses():
        if ('taptap-trusted', a) not in have:
            alist.add(list='taptap-trusted', address=a, comment='TapTap: never inspected or blocked')
    if feature == 'ddos':
        try:
            svc.resource('/ip/settings').call('set', {'tcp_syncookies': 'yes'})
        except Exception:
            pass
    filt = svc.resource('/ip/firewall/filter')
    for _path, fields, top in rules(feature):
        data = {k.replace('-', '_'): v for k, v in fields.items()}
        if top:
            first = next((r['id'] for r in filt.get() if r.get('chain') == fields['chain'] and not str(r.get('comment', '')).startswith(f['tag'])), None)
            if first:
                data['place_before'] = first
        filt.add(**data)


def _ros_value(v):
    from .agent import rs
    return rs(v) if any(c in str(v) for c in ' :;"$') else str(v)


def link_script(feature, action):
    """RouterOS script for TapTap Link: the same rules (enable), or removal (disable / unblock)."""
    from .agent import rs
    f = FEATURES[feature]
    lists = ';'.join(rs(x) for x in f['lists'])
    remove = (f':foreach r in=[/ip firewall filter find where comment~{rs("^" + f["tag"])}] do={{ /ip firewall filter remove $r }}; '
              f':foreach l in={{{lists}}} do={{ /ip firewall address-list remove [find where list=$l] }}')
    if action == 'unblock':
        return f':foreach l in={{{lists}}} do={{ /ip firewall address-list remove [find where list=$l] }}'
    if action == 'disable':
        return remove
    lines = [remove]
    for a in trusted_addresses():
        lines.append(f':if ([:len [/ip firewall address-list find where list="taptap-trusted" and address={rs(a)}]] = 0) do={{ '
                     f'/ip firewall address-list add list="taptap-trusted" address={rs(a)} comment="TapTap: never inspected or blocked" }}')
    if feature == 'ddos':
        lines.append(':do { /ip settings set tcp-syncookies=yes } on-error={}')
    for i, (_path, fields, top) in enumerate(rules(feature)):
        args = ' '.join(f'{k}={_ros_value(v)}' for k, v in fields.items())
        if top:
            ch, var = fields['chain'], f'top{i}'          # one name per chain: a :local cannot be declared twice
            lines.append(f':local {var} [/ip firewall filter find where chain={ch} and !(comment~{rs("^" + f["tag"])})]; '
                         f':if ([:len ${var}] > 0) do={{ /ip firewall filter add {args} place-before=[:pick ${var} 0] }} else={{ /ip firewall filter add {args} }}')
        else:
            lines.append(f'/ip firewall filter add {args}')
    return '{ ' + '; '.join(lines) + ' }'


def apply(router, feature, action, user=None):
    """action: enable | disable | unblock. Returns a message. Raises ValueError when the router cannot be reached."""
    from django.core.cache import cache
    from .voucher_history import channel
    if feature not in FEATURES or action not in ('enable', 'disable', 'unblock'):
        raise ValueError('Unknown protection.')
    label = FEATURES[feature]['label']
    if channel(router) == 'TapTap Link':
        from .linkops import send
        send(router, 'protection', {'feature': feature, 'action': action}, label=f'{label}: {action}', user=user)
        cache.set(f'tt:protect:{router.pk}:{feature}', action, 600)
        _audit(router, user, action, feature)
        return f'{label}: {action} queued — the router applies it at its next check-in.'
    from .mikrotik import MikroTikService
    try:
        with MikroTikService(router) as svc:
            if action == 'enable':
                _api_enable(svc, feature)
            elif action == 'disable':
                _api_remove(svc, FEATURES[feature]['tag'], FEATURES[feature]['lists'])
            else:
                res = svc.resource('/ip/firewall/address-list')
                for row in res.get():
                    if row.get('list') in FEATURES[feature]['lists']:
                        res.remove(id=row['id'])
            try:
                _refresh_snapshot(svc, router)
            except Exception:
                pass
    except Exception as exc:
        _audit(router, user, action, feature, ok=False, error=str(exc))
        raise ValueError(f'{router.name} could not be updated: {exc}')
    _audit(router, user, action, feature)
    return {'enable': f'{label} is on for {router.name}.', 'disable': f'{label} is off for {router.name}.',
            'unblock': f'Everyone blocked by {label.lower()} on {router.name} can connect again.'}[action]
