"""Leave a business you were added to as team — yourself, without asking the owner.

    website   the avatar menu → "Leave <business>…" → a page that says what happens → type the business name
    app       More → Leave this business (POST /api/mobile/v1/business/leave/)

What leaving does:
    * your membership of that business is switched off (exactly what the owner's "Switch off" does), so your pay
      records, the sales you made and your chat messages stay with the business — nothing is deleted
    * your TapTap login and every other business you work in stay as they are
    * the owner hears about it: a line in the team room, a bell alert (and a push to their phone) and the Activity log
    * the owner can let you back in later from the Team page

An owner cannot leave their own business (there would be nobody left to run it).
"""
from __future__ import annotations

import logging

from django.db import transaction

logger = logging.getLogger('taptap.team_leave')


class LeaveError(ValueError):
    pass


def membership(user, business_id):
    """The active team membership this person may leave, or None."""
    from .models_team import TeamMember
    try:
        bid = int(business_id)
    except (TypeError, ValueError):
        return None
    return TeamMember.objects.select_related('business', 'user').filter(user=user, business_id=bid, is_active=True).first()


def leave(user, business_id, confirm_name):
    """Leave the business. Returns (business, remaining_businesses). Raises LeaveError with a sentence."""
    from .team import accessible_businesses
    m = membership(user, business_id)
    if m is None:
        raise LeaveError('You are not on the team of that business (or you already left).')
    b = m.business
    if b.user_id == user.pk:
        raise LeaveError('You own this business — an owner cannot leave it.')
    if ' '.join(str(confirm_name or '').split()).lower() != ' '.join(b.business_name.split()).lower():
        raise LeaveError(f'Type the business name “{b.business_name}” to confirm.')
    from . import chat
    name = chat.display_name(user)
    with transaction.atomic():
        type(m).objects.filter(pk=m.pk).update(is_active=False)
    _tell_owner(b, user, name, m)
    remaining = [x for x, _mm in accessible_businesses(user) if x.pk != b.pk]
    return b, remaining


def _tell_owner(b, user, name, m):
    from .utils import log
    try:
        log(b, 'Team', f'{name} ({user.email}) left the team themselves (was {m.role_label}). '
                       'Their records stay; switch them on again on the Team page to let them back.')
    except Exception:
        logger.exception('team leave log')
    try:
        from . import chat
        from .models_chat import ChatMessage
        thread = chat.team_thread(b)
        msg = ChatMessage.objects.create(thread=thread, sender=None, body=f'👋 {name} left the team.', system=True)
        type(thread).objects.filter(pk=thread.pk).update(last_at=msg.created_at)
    except Exception:
        logger.exception('team leave chat note')
    try:
        from .models_events import EventAlert
        EventAlert.objects.create(business=b, user_id=b.user_id, kind='team', level='info', title=f'{name} left your team'[:160],
                                  body=f'{name} ({user.email}) removed themselves from {b.business_name}. '
                                       'Their records stay. You can let them back in on the Team page.'[:400],
                                  link='/team/', sound=True, desktop=True)
    except Exception:
        logger.exception('team leave alert')
