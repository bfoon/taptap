from django import template

from core.models_archive import VoucherArchivePolicy

register = template.Library()


def _context_business(context, supplied_business=None):
    """
    Resolve exactly the same active business used by TeamAccessMiddleware.

    Priority:
      1. explicitly supplied business
      2. request.tt_business
      3. request.user.business compatibility fallback
    """
    if supplied_business is not None:
        return supplied_business

    request = context.get('request')

    if request is None:
        return None

    business = getattr(
        request,
        'tt_business',
        None,
    )

    if business is not None:
        return business

    user = getattr(
        request,
        'user',
        None,
    )

    if (
        user is not None
        and getattr(
            user,
            'is_authenticated',
            False,
        )
    ):
        try:
            return user.business
        except Exception:
            pass

    return None


@register.simple_tag(takes_context=True)
def archive_policy(context, business=None):
    business = _context_business(
        context,
        business,
    )

    if business is None:
        return None

    policy, _created = VoucherArchivePolicy.objects.get_or_create(
        business=business,
        defaults={
            'enabled': True,
            'retention_days': 7,
        },
    )

    return policy
