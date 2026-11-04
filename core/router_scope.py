"""One business, several MikroTiks: which router a page shows.

Every network page (Topology, Active users, IP bindings, Traffic, the dashboard's activity) shows a strip of
router tabs once a business has more than one router: *All routers* plus one tab per MikroTik.

* ``?router=<id>`` picks one router, ``?router=all`` shows every router;
* the choice is remembered per business in the session, so moving from Topology to Active users keeps the
  same router selected;
* an id that is not a router of this business is ignored (never another business's router).

Views call ``scope(request, business)`` and filter their own querysets with the router it returns.
"""
from __future__ import annotations

from urllib.parse import urlencode

SESSION_KEY = 'tt_router_scope'


def _session(request):
    return getattr(request, 'session', None)


def scope(request, business):
    """(selected router or None, all routers of the business ordered by name)."""
    routers = list(business.routers.order_by('name', 'pk'))
    by_id = {r.pk: r for r in routers}
    session = _session(request)
    store = dict(session.get(SESSION_KEY) or {}) if session is not None else {}
    key = str(business.pk)
    raw = request.GET.get('router')
    if raw is not None:
        raw = str(raw).strip()
        if raw.isdigit() and int(raw) in by_id:
            store[key] = int(raw)
        else:
            store.pop(key, None)            # "all", empty or not one of ours
        if session is not None:
            session[SESSION_KEY] = store
            session.modified = True
    selected = by_id.get(store.get(key)) if len(routers) > 1 else None
    return selected, routers


def _keep_query(request):
    """The page's own query string without ``router`` (filters such as ?period=7d survive a tab change)."""
    pairs = [(k, v) for k in request.GET for v in request.GET.getlist(k) if k != 'router']
    return (urlencode(pairs) + '&') if pairs else ''


def _status(router):
    s = (router.status or '').lower()
    if s == 'online':
        return 'online'
    if s == 'offline':
        return 'offline'
    return 'waiting'


def tabs(request, business, selected=None, routers=None, label='Routers'):
    """Context for ``core/partials/router_tabs.html``."""
    if routers is None:
        selected, routers = scope(request, business)
    keep = _keep_query(request)
    items = [{'id': r.pk, 'name': r.name, 'status': _status(r), 'href': f'?{keep}router={r.pk}',
              'on': bool(selected and selected.pk == r.pk),
              'via': 'TapTap Link' if r.connection_mode == 'agent' else 'Direct API'} for r in routers]
    return {
        'show': len(routers) > 1,
        'label': label,
        'all_href': f'?{keep}router=all',
        'all_on': selected is None,
        'items': items,
        'selected': selected,
        'count': len(routers),
        'online': sum(1 for i in items if i['status'] == 'online'),
    }


def page_context(request, business, label='Routers'):
    """(selected router or None, routers, template context with ``router_tabs``)."""
    selected, routers = scope(request, business)
    return selected, routers, {'router_tabs': tabs(request, business, selected, routers, label)}
