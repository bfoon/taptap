"""MikroTik → Hotspot profiles page."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import router_profiles as rp


@login_required
def router_profiles(request):
    business = request.user.business
    router = business.routers.filter(pk=request.GET.get('router') or 0).first()
    groups = rp.gather(business, router, request.GET.get('q', ''))
    return render(request, 'core/router_profiles.html', {
        'groups': groups, 'routers': business.routers.order_by('name'), 'router': router, 'q': request.GET.get('q', ''),
        'missing': rp.plans_missing(business, groups) if not request.GET.get('q') else [],
        'total': sum(len(g['profiles']) for g in groups),
        'can_manage': 'plans.manage' in getattr(request, 'tt_perms', set()),
        'can_sync': 'network.manage' in getattr(request, 'tt_perms', set()),
        'check': _check(business),
    })


def _check(business):
    from .profile_time import audit
    try:
        a = audit(business)
    except Exception:
        return None
    a['total'] = len(a['plans']) + len(a['vouchers']) + len(a['drift']) + len(a['devices']) + len(a.get('profile_devices', [])) + len(a.get('plan_devices', []))
    return a


@login_required
@require_POST
def router_profile_import(request, pk):
    """Make a TapTap plan from a router profile (price and validity read from Mikhmon / the comment)."""
    from .sync import _profile_to_plan
    business = request.user.business
    router = get_object_or_404(business.routers, pk=pk)
    name = request.POST.get('name', '')
    row = rp.find_row(router, name)
    if not row:
        messages.error(request, f'{name} is not on {router.name} any more. Refresh the router and try again.')
        return redirect(f'/routers/profiles/?router={router.pk}')
    summary = {'duplicate_plans_skipped': 0, 'pulled_plans': 0}
    plan = _profile_to_plan(router, row, summary, timezone.now())
    if plan is None:
        messages.warning(request, f'A plan named {name} is in the Bin, so it was not brought back.')
    elif summary['pulled_plans']:
        messages.success(request, f'Plan {plan.name} created from {router.name}' + (f' at {business.currency}{plan.price}.' if plan.price else ' — set its price on the Plans page.'))
    else:
        messages.info(request, f'{name} already belongs to the plan {plan.name}.')
    return redirect(request.POST.get('next') or '/routers/profiles/')



@login_required
@require_POST
def profile_check_fix(request):
    """Make TapTap and the routers agree: profile lengths, vouchers without a time limit, drifted profiles."""
    from .profile_time import apply
    from .utils import log
    business = request.user.business
    r = apply(business, request.user)
    log(business, 'Profiles Fixed', f"{r['plans']} plan(s), {r['vouchers']} voucher(s) given their profile's length, {r['drift']} put back on TapTap's profile")
    messages.success(request, f"Done: {r['plans']} plan(s) and {r['vouchers']} voucher(s) now follow their profile's length; "
                              f"{r.get('profile_devices', 0)} profile(s) now allow the devices their name says; {r['devices']} voucher(s) now allow their plan's devices; {r['drift']} voucher(s) put back on the right profile on the router. " + ' '.join(r['messages']))
    return redirect('/routers/profiles/')
