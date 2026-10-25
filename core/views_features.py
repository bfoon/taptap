"""Settings → Features: the owner switches optional modules of their package on or off."""
from django.contrib import messages
from django.shortcuts import redirect, render

from . import features as ft
from .utils import log


def business_features(request):
    business = request.user.business
    if request.method == 'POST':
        avail = ft.available(business)
        wanted_on = set(request.POST.getlist('on'))
        hidden = sorted(k for k, f in ft.FEATURES.items()
                        if f.owner_can_hide and k in avail and k not in wanted_on)
        before = set(business.features_off or [])
        # Keep hidden flags for modules the package doesn't include right now, so they stay hidden
        # if the package later includes them again.
        keep = sorted(k for k in before if k in ft.FEATURES and k not in avail)
        business.features_off = sorted(set(hidden) | set(keep))
        business.save(update_fields=['features_off'])
        turned_off = [ft.FEATURES[k].label for k in set(hidden) - before]
        turned_on = [ft.FEATURES[k].label for k in (before - set(hidden)) if k in ft.FEATURES and k in avail]
        if turned_off or turned_on:
            log(business, 'Features changed', ('On: ' + ', '.join(turned_on) + '. ' if turned_on else '') + ('Off: ' + ', '.join(turned_off) if turned_off else ''))
            messages.success(request, 'Features saved. ' + (f'Switched on: {", ".join(turned_on)}. ' if turned_on else '') + (f'Switched off: {", ".join(turned_off)}.' if turned_off else ''))
        else:
            messages.info(request, 'Nothing changed.')
        return redirect('business_features')
    tier = ft.tier_of(business)
    rows = ft.state_rows(business)
    on = sum(1 for g in rows for i in g['items'] if i['on'])
    return render(request, 'core/business_features.html', {
        'groups': rows, 'tier': ft.TIERS[tier], 'tier_key': tier, 'count_on': on, 'count_all': len(ft.FEATURES),
    })
