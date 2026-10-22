"""The saved load-balancing analysis of a router, always made by the current analysis code.

TapTap Link routers are analysed when an inventory sync arrives; the live view and every page then
read the saved result (``RouterConfigSnapshot.load_balancing``). After an update to the analysis code
(e.g. recursive gateways such as 1.1.1.1 / 8.8.8.8 resolving to the real ISP port) a saved result would
stay wrong until the next full sync. ``current_load_balancing()`` notices an analysis made by older code
and redoes it from the configuration already stored for the router — no router round-trip needed.
"""
import logging

from .routeros_analysis import ANALYSIS_VERSION, analyze_wan

logger = logging.getLogger('taptap.wan')


def _rows(sections, label):
    sec = (sections or {}).get(label) or {}
    rows = sec.get('rows') if isinstance(sec, dict) else None
    return rows if isinstance(rows, list) else []


def declared_wans(router):
    if not getattr(router, 'pk', None):
        return []
    return list(router.interface_roles.filter(role='wan').values_list('interface_name', flat=True))


def check_host_hints(router):
    """{check host: port} from the owner's saved Internet Lines setup (8.8.8.8 → ether1, 1.1.1.1 → ether3)."""
    try:
        links = (router.wan_setup.config or {}).get('links') or []
    except Exception:
        return {}
    return {str(l.get('check')): str(l.get('interface')) for l in links
            if l.get('check') and l.get('interface') and l.get('enabled', True)}


def dhcp_rows(sections):
    """DHCP clients, with the gateway filled in from the explicit reading when `print as-value` left it out."""
    rows = [dict(r) for r in _rows(sections, 'DHCP clients')]
    explicit = {str(r.get('interface')): r for r in _rows(sections, 'DHCP client gateways') if r.get('interface')}
    for r in rows:
        x = explicit.get(str(r.get('interface')))
        if x:
            if not r.get('gateway') and x.get('gateway'):
                r['gateway'] = x['gateway']
            if not r.get('dhcp-server') and x.get('server'):
                r['dhcp-server'] = x['server']
            if not r.get('status') and x.get('status'):
                r['status'] = x['status']
    known = {str(r.get('interface')) for r in rows}
    rows += [{'interface': i, 'gateway': x.get('gateway', ''), 'dhcp-server': x.get('server', ''), 'status': x.get('status', '')}
             for i, x in explicit.items() if i not in known]
    return rows


def analyze_sections(router, sections, interfaces=None):
    """Analysis from a stored configuration (the same inputs a Link inventory sync uses)."""
    from .mikrotik import redact
    if interfaces is None:
        from .agent_inventory import merge_interface_stats
        interfaces = merge_interface_stats(_rows(sections, 'Interfaces'), _rows(sections, 'Interface counters'))
    return redact(analyze_wan(
        routes=_rows(sections, 'Routes'),
        mangle=_rows(sections, 'Firewall mangle'),
        filter_rules=_rows(sections, 'Firewall filter'),
        bonding=_rows(sections, 'Bonding'),
        routing_tables=_rows(sections, 'Routing tables'),
        routing_rules=_rows(sections, 'Routing rules'),
        addresses=_rows(sections, 'IP addresses'),
        dhcp_clients=dhcp_rows(sections),
        interfaces=interfaces,
        hotspot_servers=_rows(sections, 'HotSpot servers'),
        declared_wans=declared_wans(router),
        dns=_rows(sections, 'DNS'),
        arp=_rows(sections, 'ARP'),
        hints=check_host_hints(router),
    ))


def is_current(lb):
    return bool(lb) and (lb or {}).get('version') == ANALYSIS_VERSION


def current_load_balancing(snap, router=None):
    """The snapshot's analysis; redone (and saved) once when it was made by older analysis code."""
    if snap is None:
        return {}
    lb = snap.load_balancing or {}
    if is_current(lb) or not _rows(snap.sections, 'Routes'):
        return lb
    try:
        fresh = analyze_sections(router or snap.router, snap.sections)
    except Exception:
        logger.exception('WAN analysis upgrade failed for router %s', getattr(snap, 'router_id', '?'))
        return lb
    from .models import RouterConfigSnapshot
    RouterConfigSnapshot.objects.filter(pk=snap.pk).update(load_balancing=fresh)
    snap.load_balancing = fresh
    try:
        from django.core.cache import cache
        cache.delete(f'tt:lb:{snap.router_id}')        # the live view must not keep serving the old answer
    except Exception:
        pass
    logger.info('WAN analysis of router %s upgraded to version %s', snap.router_id, ANALYSIS_VERSION)
    return fresh


def for_router(router):
    """Current analysis of a router ({} when it has never been synchronised)."""
    from .models import RouterConfigSnapshot
    snap = RouterConfigSnapshot.objects.filter(router=router).first()
    return current_load_balancing(snap, router) if snap else {}
