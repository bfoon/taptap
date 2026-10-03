"""How your MikroTiks are connected to each other — found automatically, and placed by hand.

**Suggestions** ("Hotel CCR looks connected to Serrekunda hAP on ether5"), from what TapTap already reads:
  * B's network card is among A's devices (bridge/ARP/DHCP) → B hangs from A, on that port (strongest);
  * B's default gateway is one of A's own addresses → B is behind A;
  * A's neighbour list (MNDP/LLDP/CDP) names B → on that interface.
The owner accepts or ignores each; ignored ones are not suggested again.

**Placement** (Topology → Detail: drag a router onto another, or "Connected to"): a MikroTik can be placed on
the Internet, under another MikroTik's port, or under a router TapTap does not manage (TP-Link…). The map
and the designed diagram follow it. Loops (A under B under A) are refused.
"""
from __future__ import annotations

import ipaddress
import re

PORT_RE = re.compile(r'^[\w./:+@<>-]{1,120}$')


def _norm(mac):
    h = ''.join(c for c in str(mac or '').upper() if c in '0123456789ABCDEF')
    return ':'.join(h[i:i + 2] for i in range(0, 12, 2)) if len(h) == 12 else ''


def _snapshot(router):
    from .models import RouterConfigSnapshot
    s = RouterConfigSnapshot.objects.filter(router=router).first()
    return (s.sections or {}) if s else {}


def _networks(sec):
    from .routeros_analysis import g
    out = []
    for row in (sec.get('IP addresses') or {}).get('rows', []):
        try:
            out.append(ipaddress.ip_interface(str(g(row, 'address'))))
        except ValueError:
            pass
    return out


def _gateways(sec):
    from .routeros_analysis import g
    out = []
    for row in (sec.get('Routes') or {}).get('rows', []):
        if str(g(row, 'dst-address')) in ('0.0.0.0/0', '') and g(row, 'gateway'):
            for gw in re.split(r'[,%]', str(g(row, 'gateway'))):
                try:
                    out.append(ipaddress.ip_address(gw.strip()))
                except ValueError:
                    pass
    return out


def suggestions(business):
    """[{child, parent, port, reasons, score}] — best guess per MikroTik not placed by hand."""
    from .models import RouterDevice, RouterInterface, RouterNeighbor
    from .site_routers import physical_port
    routers = list(business.routers.all())
    if len(routers) < 2:
        return []
    macs = {r.pk: {_norm(m) for m in RouterInterface.objects.filter(router=r).values_list('mac_address', flat=True)} - {''} for r in routers}
    secs = {r.pk: _snapshot(r) for r in routers}
    nets = {r.pk: _networks(secs[r.pk]) for r in routers}
    gws = {r.pk: _gateways(secs[r.pk]) for r in routers}
    found = {}
    for child in routers:
        if child.uplink_router_id or child.uplink_site_id or child.uplink_internet:
            continue                                        # placed by hand: the owner decided
        ignored = set(child.uplink_ignored or [])
        for parent in routers:
            if parent.pk == child.pk or parent.pk in ignored:
                continue
            reasons, port, score = [], '', 0
            seen = [d for d in RouterDevice.objects.filter(router=parent, mac_address__in=list(macs[child.pk]))]
            if seen:
                d = max(seen, key=lambda x: x.last_seen_at)
                port = physical_port(d) or port
                reasons.append(f'{child.name}’s network card {_norm(d.mac_address)} is seen on {parent.name}’s {port or "network"}')
                score += 60
            for gw in gws[child.pk]:
                if any(gw == n.ip for n in nets[parent.pk]):
                    reasons.append(f'{child.name}’s default gateway {gw} is {parent.name}’s own address')
                    score += 40
                    break
            n = RouterNeighbor.objects.filter(router=parent).filter(
                mac_address__in=[m for m in macs[child.pk]] + [m.lower() for m in macs[child.pk]]).first() \
                or RouterNeighbor.objects.filter(router=parent, identity__iexact=child.name).first()
            if n:
                port = port or n.interface_name
                reasons.append(f'{parent.name}’s neighbour list shows {child.name} on {n.interface_name or "a port"}')
                score += 30
            if score and (child.pk not in found or score > found[child.pk]['score']):
                found[child.pk] = {'child': child, 'parent': parent, 'port': port, 'reasons': reasons, 'score': score}
    return sorted(found.values(), key=lambda s: -s['score'])


def parent_of(router):
    """('internet'|'router'|'site'|'', id, port) as placed by hand ('' = automatic)."""
    if router.uplink_site_id:
        return 'site', router.uplink_site_id, ''
    if router.uplink_router_id:
        return 'router', router.uplink_router_id, router.uplink_port
    if router.uplink_internet:
        return 'internet', None, ''
    return '', None, ''


def _ancestors(business, kind, pk, guard=50):
    """Every node above (kind, pk), following manual placements and site-router links."""
    from .models import Router, SiteRouter
    seen = []
    while kind in ('router', 'site') and pk and guard:
        guard -= 1
        seen.append((kind, pk))
        if kind == 'router':
            r = Router.objects.filter(business=business, pk=pk).first()
            if not r:
                break
            kind, pk, _ = parent_of(r)
        else:
            s = SiteRouter.objects.filter(business=business, pk=pk).first()
            if not s:
                break
            kind, pk = ('site', s.parent_id) if s.parent_id else ('router', s.router_id)
    return seen


def place(router, target, port='', user=None):
    """target: 'internet' | 'auto' | 'router:<id>' | 'site:<id>'. Raises ValueError for loops / bad input."""
    from .models import Router
    business = router.business
    port = str(port or '').strip()
    if port and not PORT_RE.match(port):
        raise ValueError('That port name is not valid.')
    fields = {'uplink_router': None, 'uplink_site': None, 'uplink_port': '', 'uplink_internet': False}
    if target == 'internet':
        fields['uplink_internet'] = True
    elif target == 'auto':
        pass
    elif target.startswith('router:') and target[7:].isdigit():
        parent = business.routers.filter(pk=target[7:]).first()
        if not parent or parent.pk == router.pk:
            raise ValueError('Choose another of your MikroTiks.')
        if ('router', router.pk) in _ancestors(business, 'router', parent.pk):
            raise ValueError(f'{parent.name} is already connected through {router.name} — that would make a loop.')
        fields.update(uplink_router=parent, uplink_port=port)
    elif target.startswith('site:') and target[5:].isdigit():
        site = business.site_routers.filter(pk=target[5:], status='confirmed').first()
        if not site:
            raise ValueError('Choose one of your confirmed routers.')
        if ('router', router.pk) in _ancestors(business, 'site', site.pk):
            raise ValueError(f'{site} is connected through {router.name} — that would make a loop.')
        fields['uplink_site'] = site
    else:
        raise ValueError('Choose where it is connected.')
    Router.objects.filter(pk=router.pk).update(**{k + ('_id' if k in ('uplink_router', 'uplink_site') else ''): (v.pk if hasattr(v, 'pk') else v)
                                                   for k, v in fields.items()})
    from .utils import log
    where = 'the Internet' if target == 'internet' else ('found automatically' if target == 'auto' else
            (f'{fields["uplink_router"].name}' + (f' › {port}' if port else '') if fields['uplink_router'] else str(fields['uplink_site'])))
    log(business, 'Topology', f'{router.name} placed: {where}')
    return where


def ignore(router, parent_id):
    from .models import Router
    ids = list(router.uplink_ignored or [])
    if int(parent_id) not in ids:
        ids.append(int(parent_id))
    Router.objects.filter(pk=router.pk).update(uplink_ignored=ids)


def site_place_check(business, site, target):
    """A site router moved under a MikroTik / site router must not end up under itself (loop)."""
    kind, pk = target
    if ('site', site.pk) in _ancestors(business, kind, pk):
        raise ValueError(f'That would put {site} below itself.')
