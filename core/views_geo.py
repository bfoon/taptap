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
    """Phone page: identify the router/AP carrying this phone, use GPS, and map it."""
    from .auth_security import client_ip

    business = _b(request)

    # Existing site-level hints:
    #   1. gateway/router from Wi-Fi details (?gw=172.16.0.1)
    #   2. one public address matching exactly one TapTap router/agent
    #   3. explicit site choice
    #   4. remembered site for this authenticated browser session
    gw = request.GET.get('gw', '').strip()[:45]
    public_candidates = geomap.sites_from_ip(business, client_ip(request))
    site = geomap.site_from_gateway(business, gw) if gw else None
    gw_unknown = bool(gw) and site is None

    if site is None and request.GET.get('site', '').isdigit():
        site = business.routers.filter(pk=int(request.GET['site'])).first()

    if site is None and len(public_candidates) == 1:
        site = public_candidates[0]

    if site is None and request.session.get('tt_field_site'):
        remembered = business.routers.filter(
            pk=request.session['tt_field_site']
        ).first()
        if remembered is not None and (
            not public_candidates or remembered in public_candidates
        ):
            site = remembered

    # New exact-path hint:
    # The page's JavaScript makes a best-effort attempt to discover the phone's private
    # Wi-Fi IPv4 and reloads once with ?lip=. The server then correlates that address
    # against RouterDevice rows synchronized from RouterOS. The browser value alone is
    # never trusted to choose a router.
    local_ip = request.GET.get('lip', '').strip()[:45]
    phone = geomap.phone_target(business, local_ip, site=site)

    # A unique RouterDevice match can identify the site even when several MikroTiks share
    # the same Internet/public address.
    if phone.get('site') is not None:
        site = phone['site']

    if site is not None and (
        gw
        or request.GET.get('site')
        or phone.get('key')
        or phone.get('ip')
    ):
        request.session['tt_field_site'] = site.pk

    forced = {phone['key']} if phone.get('key') else set()
    items = geomap.targets(business, include_keys=forced)

    if site:
        # The current site's nodes first, unmapped before mapped, then alphabetical.
        items.sort(
            key=lambda t: (
                t.get('site_id') != site.pk,
                t['geo'] is not None,
                t['name'].lower(),
            )
        )
    else:
        items.sort(key=lambda t: (t['geo'] is not None, t['name'].lower()))

    # QR/manual ?r= always wins. Exact phone-path detection is next. Site fallback is last.
    chosen = request.GET.get('r', '')
    chosen = chosen if geomap.KEY_RE.match(chosen) else ''

    auto = ''
    why = ''
    exact_phone = False
    auto_add = False

    if not chosen and phone.get('key'):
        auto = phone['key']
        why = phone.get('why') or 'TapTap matched this phone to the router carrying its connection.'
        exact_phone = True
        auto_add = bool(phone.get('auto_add'))

    if not chosen and not auto:
        auto, why = geomap.auto_pick(items, site)

    candidates = (
        public_candidates
        if site is None and len(public_candidates) > 1
        else []
    )

    return render(
        request,
        'core/topology_field.html',
        {
            'site': site,
            'chosen': chosen or auto,
            'gw': gw,
            'gw_unknown': gw_unknown,
            'candidates': candidates,
            'field': {
                'items': items,
                'chosen': chosen or auto,
                'auto': bool(auto),
                'auto_why': why,
                'auto_exact_phone': exact_phone,
                'auto_add': auto_add,
                'site_id': site.pk if site else None,
                'phone_ip': phone.get('ip') or '',
                'phone_port': phone.get('port') or '',
                'phone_ambiguous': bool(phone.get('ambiguous')),
                'has_manual_choice': bool(chosen),
                'save_url': reverse('topology_field_save'),
            },
        },
    )


@login_required
@require_POST
def topology_field_save(request):
    business = _b(request)

    try:
        data = json.loads(request.body or '{}')
    except ValueError:
        return JsonResponse(
            {'success': False, 'message': 'Could not read the request.'},
            status=400,
        )

    try:
        if data.get('action') == 'clear':
            geomap.clear(business, str(data.get('key') or ''))
            return JsonResponse({
                'success': True,
                'message': 'Location removed.',
                'items': geomap.targets(business),
            })

        key, name = geomap.save(
            business,
            str(data.get('key') or ''),
            data.get('lat'),
            data.get('lng'),
            data.get('accuracy'),
            data.get('note', ''),
            request.user,
        )
    except ValueError as exc:
        return JsonResponse(
            {'success': False, 'message': str(exc)},
            status=400,
        )

    return JsonResponse({
        'success': True,
        'key': key,
        'message': f'{name} is on the map.',
        'items': geomap.targets(business),
    })


@login_required
def topology_geo(request):
    """Data of the Geo map tab."""
    return JsonResponse({
        'success': True,
        **geomap.payload(_b(request)),
    })


@login_required
def topology_labels(request):
    """Printable QR labels: scanning one opens Map a router with that router chosen."""
    business = _b(request)
    base = (
        getattr(settings, 'SITE_URL', '')
        or request.build_absolute_uri('/')
    ).rstrip('/')
    field = base + reverse('topology_field')
    only = request.GET.get('only', '')

    items = [
        dict(t, url=f'{field}?r={t["key"]}')
        for t in geomap.targets(business)
        if not only or (only == 'unmapped' and not t['geo'])
    ]

    return render(
        request,
        'core/topology_labels.html',
        {
            'items': items,
            'business': business,
        },
    )
