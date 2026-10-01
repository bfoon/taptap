"""Security page: Fix dialogs for findings that need more than one switch.

* plan-limits   — "N hotspot user(s) never expire": give router users their plan's time (core/plan_limits.py)
* sharing       — "N voucher(s) used on more devices than paid for": turn sticky vouchers on, kick extras now
* admin-account — "Default admin account is active": strong password, or disable admin safely (core/security_admin.py)
"""
import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from .utils import log


def _b(request):
    return request.user.business


def _err(msg, status=400):
    return JsonResponse({'success': False, 'message': msg}, status=status)


@login_required
def security_plan_limits(request, pk):
    from . import plan_limits
    business = _b(request)
    router = get_object_or_404(business.routers, pk=pk)
    if request.method == 'GET':
        return JsonResponse({'success': True, 'router': router.name, 'groups': plan_limits.preview(router),
                             'auto': business.auto_plan_limits})
    try:
        data = json.loads(request.body or '{}')
    except ValueError:
        return _err('Could not read the request.')
    if 'auto' in data and bool(data['auto']) != business.auto_plan_limits:
        type(business).objects.filter(pk=business.pk).update(auto_plan_limits=bool(data['auto']))
        business.auto_plan_limits = bool(data['auto'])
        log(business, 'Security', f'Automatic plan time for router users {"on" if business.auto_plan_limits else "off"}')
    try:
        res = plan_limits.apply(router, user=request.user, overrides=data.get('overrides') or {})
    except ValueError as exc:            # TapTap Link not reachable
        return _err(str(exc))
    except Exception as exc:
        return _err(f'The router refused: {exc}')
    return JsonResponse({'success': True, **res, 'auto': business.auto_plan_limits})


@login_required
@require_POST
def security_sharing(request):
    """Stop vouchers being used on more devices than paid for: sticky vouchers on, extras kicked now."""
    business = _b(request)
    turned_on = False
    if not business.device_lock:
        type(business).objects.filter(pk=business.pk).update(device_lock=True)
        business.device_lock = turned_on = True
        log(business, 'Security', 'Sticky vouchers switched on (stop voucher sharing)')
    from .live import watch_business
    results = watch_business(business, force=True)        # locks devices and disconnects foreign ones right now
    kicked = sum(int(r.get('locked_out') or 0) for r in results if isinstance(r, dict))
    msg = ('Sticky vouchers are on. ' if turned_on else 'Sticky vouchers were already on. ') + \
          (f'{kicked} extra device{"s" if kicked != 1 else ""} disconnected now. ' if kicked else '') + \
          'From now on each code stays on the devices that first used it; a phone that only changes its MAC keeps working.'
    return JsonResponse({'success': True, 'message': msg, 'kicked': kicked, 'turned_on': turned_on})


@login_required
def security_admin_account(request, pk):
    from . import security_admin as sa
    router = get_object_or_404(_b(request).routers, pk=pk)
    if request.method == 'GET':
        return JsonResponse({'success': True, **sa.state(router)})
    try:
        data = json.loads(request.body or '{}')
    except ValueError:
        return _err('Could not read the request.')
    try:
        if data.get('action') == 'password':
            msg = sa.set_password(router, data.get('password'), data.get('password2'), user=request.user)
        elif data.get('action') == 'disable':
            msg = sa.disable_admin(router, user=request.user, new_name=data.get('new_name', ''),
                                   new_password=data.get('new_password', ''), new_again=data.get('new_password2', ''))
        else:
            return _err('Unknown action.')
    except sa.AdminFixError as exc:
        return _err(str(exc))
    except ValueError as exc:            # TapTap Link not reachable
        return _err(str(exc))
    except Exception as exc:
        return _err(f'Could not reach the router: {exc}')
    return JsonResponse({'success': True, 'message': msg})
