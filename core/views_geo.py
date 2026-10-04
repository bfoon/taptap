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
    site = geomap.site_from_ip(business, client_ip(request))
    items = geomap.targets(business)
    if site:                                   # the site the phone is on first, then the rest
        items.sort(key=lambda t: (t.get('site_id') != site.pk, t['geo'] is not None, t['name'].lower()))
    else:
        items.sort(key=lambda t: (t['geo'] is not None, t['name'].lower()))
    chosen = request.GET.get('r', '')
    return render(request, 'core/topology_field.html', {
        'site': site, 'chosen': chosen if geomap.KEY_RE.match(chosen) else '',
        'field': {'items': items, 'chosen': chosen if geomap.KEY_RE.match(chosen) else '', 'site_id': site.pk if site else None,
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
