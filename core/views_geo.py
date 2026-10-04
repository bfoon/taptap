"""Field mapping pages (core/geomap.py): Map a router (phone), the Geo map data, printable map labels."""
import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import geomap


def _b(request):
    return request.user.business


@login_required
def topology_field(request):
    """Phone page: scan a router's label (or pick it), use the phone's location, save."""
    from .auth_security import client_ip
    business = _b(request)
    # Which MikroTik is giving this phone its address (the Wi-Fi access points only pass it on):
    #  1. the gateway the person typed from their Wi-Fi details (?gw=172.16.0.1), 2. the one MikroTik the phone's
    #  internet address points to, 3. when several MikroTiks share that address (one ISP modem), ask which.
    gw = request.GET.get('gw', '').strip()[:45]
    candidates = geomap.sites_from_ip(business, client_ip(request))
    site = geomap.site_from_gateway(business, gw) if gw else None
    gw_unknown = bool(gw) and site is None
    if site is None and request.GET.get('site', '').isdigit():
        site = business.routers.filter(pk=int(request.GET['site'])).first()
    if site is None and len(candidates) == 1:
        site = candidates[0]
    if site is not None and (gw or request.GET.get('site')):
        request.session['tt_field_site'] = site.pk          # remembered on this phone for the next router
    elif site is None and request.session.get('tt_field_site'):
        remembered = business.routers.filter(pk=request.session['tt_field_site']).first()
        if remembered is not None and (not candidates or remembered in candidates):
            site = remembered
    items = geomap.targets(business)
    if site:                                   # the site the phone is on first, then the rest
        items.sort(key=lambda t: (t.get('site_id') != site.pk, t['geo'] is not None, t['name'].lower()))
    else:
        items.sort(key=lambda t: (t['geo'] is not None, t['name'].lower()))
    chosen = request.GET.get('r', '')
    chosen = chosen if geomap.KEY_RE.match(chosen) else ''
    auto, why = '', ''
    if not chosen:
        auto, why = geomap.auto_pick(items, site)
    return render(request, 'core/topology_field.html', {
        'site': site, 'chosen': chosen or auto,
        'gw': gw, 'gw_unknown': gw_unknown, 'candidates': candidates if (site is None and len(candidates) > 1) else [],
        'field': {'items': items, 'chosen': chosen or auto, 'auto': bool(auto), 'auto_why': why, 'site_id': site.pk if site else None,
                  'save_url': reverse('topology_field_save')}})


@login_required
@require_POST
def topology_field_save(request):
    business = _b(request)
    try:
        data = json.loads(request.body or '{}')
    except ValueError:
        return JsonResponse({'success': False, 'message': 'Could not read the request.'}, status=400)
    try:
        if data.get('action') == 'clear':
            geomap.clear(business, str(data.get('key') or ''))
            return JsonResponse({'success': True, 'message': 'Location removed.', 'items': geomap.targets(business)})
        key, name = geomap.save(business, str(data.get('key') or ''), data.get('lat'), data.get('lng'), data.get('accuracy'),
                                data.get('note', ''), request.user)
    except ValueError as exc:
        return JsonResponse({'success': False, 'message': str(exc)}, status=400)
    return JsonResponse({'success': True, 'key': key, 'message': f'{name} is on the map.', 'items': geomap.targets(business)})


@login_required
def topology_geo(request):
    """Data of the Geo map tab."""
    return JsonResponse({'success': True, **geomap.payload(_b(request))})


@login_required
def topology_labels(request):
    """Printable QR labels: stick one on each router; scanning it opens Map a router with that router chosen."""
    business = _b(request)
    base = (getattr(settings, 'SITE_URL', '') or request.build_absolute_uri('/')).rstrip('/')
    field = base + reverse('topology_field')
    only = request.GET.get('only', '')
    items = [dict(t, url=f'{field}?r={t["key"]}') for t in geomap.targets(business)
             if not only or (only == 'unmapped' and not t['geo'])]
    return render(request, 'core/topology_labels.html', {'items': items, 'business': business})
