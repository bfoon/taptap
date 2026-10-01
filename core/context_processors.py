from .permissions import ROLES
from .team import accessible_businesses


def business_context(request):
    authenticated = bool(
        getattr(request, 'user', None)
        and request.user.is_authenticated
    )

    business = (
        getattr(request, 'tt_business', None)
        if authenticated
        else None
    )

    # Fall back to the request-scoped user.business cache for compatibility.
    if authenticated and business is None:
        business = getattr(request.user, 'business', None)

    role = getattr(request, 'tt_role', None)

    choices = []
    if authenticated and not getattr(request, 'tt_view_as', None):
        for option_business, membership in accessible_businesses(request.user):
            option_role = 'owner' if membership is None else membership.role
            choices.append(
                {
                    'id': option_business.pk,
                    'name': option_business.business_name,
                    'role': option_role,
                    'role_label': (
                        ROLES[option_role][0]
                        if option_role in ROLES
                        else 'Staff'
                    ),
                    'current': bool(
                        business
                        and option_business.pk == business.pk
                    ),
                }
            )

    return {
        'current_business': business,
        'tt_perms': getattr(request, 'tt_perms', frozenset()),
        'tt_role': role,
        'tt_role_label': (
            ROLES[role][0]
            if role in ROLES
            else ''
        ),
        'tt_member': getattr(request, 'tt_member', None),
        'tt_view_as': getattr(request, 'tt_view_as', None),
        'tt_businesses': choices,
        'tt_can_switch_business': len(choices) > 1,
    }
