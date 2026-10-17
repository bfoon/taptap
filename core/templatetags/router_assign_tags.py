"""{% put_on_router voucher=v %} / {% put_on_router batch=batch %}: the "put it on a router" box (core/router_assign.py)."""
from django import template

register = template.Library()


@register.inclusion_tag('core/_put_on_router.html', takes_context=True)
def put_on_router(context, voucher=None, batch=None):
    from ..router_assign import waiting
    perms = context.get('tt_perms') or ()
    request = context.get('request')
    out = {'show': False, 'csrf_token': context.get('csrf_token'), 'next': getattr(request, 'get_full_path', lambda: '')()}
    if not ({'vouchers.create', 'vouchers.support'} & set(perms)):
        return out
    if voucher is not None:
        if voucher.router_id or voucher.source != 'taptap' or voucher.deleted_at or voucher.status in ('expired', 'archived'):
            return out
        business, n, label = voucher.business, 1, f'voucher {voucher.code}'
        out.update(codes=voucher.code, ask=f'Put voucher {voucher.code} on a router — which router fits it best?')
    elif batch is not None:
        business = batch.business
        n = waiting(business).filter(batch=batch).count()
        if not n:
            return out
        label = f'{n} voucher{"s" if n != 1 else ""} of this batch'
        out.update(batch=batch.pk, ask=f'Batch "{batch.name}" has {n} TapTap-only vouchers. Which router should they go on? Put them there.')
    else:
        return out
    routers = list(business.routers.order_by('name'))
    out.update(show=True, n=n, label=label, routers=routers, fit=bool(getattr(business, 'pk', None)))
    return out
