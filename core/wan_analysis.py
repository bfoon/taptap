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


def analyze_sections(router, sections, interfaces=None):
    """Analysis from a stored configuration (the same inputs a Link inventory sync uses)."""
    from .mikrotik import redact
    if interfaces is None:
        from .agent_inventory import merge_interface_stats
        interfaces = merge_interface_stats(_rows(sections, 'Interfaces'), _rows(sections, 'Interface counters'))
    return redact(analyze_wan(
        routes=_rows(sections, 'Routes'),
        mangle=_rows(sections, 'Firewall mangle'),
        bonding=_rows(sections, 'Bonding'),
        routing_tables=_rows(sections, 'Routing tables'),
        routing_rules=_rows(sections, 'Routing rules'),
        addresses=_rows(sections, 'IP addresses'),
        dhcp_clients=_rows(sections, 'DHCP clients'),
        interfaces=interfaces,
        hotspot_servers=_rows(sections, 'HotSpot servers'),
        declared_wans=declared_wans(router),
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
