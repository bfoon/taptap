from .permissions import ROLES


def business_context(request):
    b = getattr(request.user, 'business', None) if getattr(request, 'user', None) and request.user.is_authenticated else None
    role = getattr(request, 'tt_role', None)
    return {'current_business': b, 'tt_perms': getattr(request, 'tt_perms', frozenset()), 'tt_role': role,
            'tt_role_label': ROLES[role][0] if role in ROLES else '', 'tt_member': getattr(request, 'tt_member', None),
            'tt_view_as': getattr(request, 'tt_view_as', None)}
