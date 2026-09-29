"""Live chat engine — who can see which conversation, sending, unread counts, presence.

Everyone in a business (the owner and active team members) shares one Team room, can
message each other directly, and has one Support conversation with TapTap. The support
team (superusers + users a superuser marks as support agents) sees every business's
Support conversation in one inbox.
"""
from __future__ import annotations

import re

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db.models import Max, Q
from django.utils import timezone

from .models_chat import ChatMessage, ChatPrefs, ChatRead, ChatThread, SupportAgent

ONLINE_SECONDS = 70
TYPING_SECONDS = 6


class ChatError(ValueError):
    pass


# ─────────────────────────────── people ───────────────────────────────

def is_agent(user):
    if not user or not user.is_authenticated:
        return False
    return user.is_superuser or SupportAgent.objects.filter(user=user, active=True).exists()


def agents():
    ids = set(User.objects.filter(is_superuser=True, is_active=True).values_list('id', flat=True))
    ids |= set(SupportAgent.objects.filter(active=True, user__is_active=True).values_list('user_id', flat=True))
    return User.objects.filter(id__in=ids)


def people_of(business):
    """Owner + active team members of a business."""
    from .models_team import TeamMember
    ids = [business.user_id] + list(TeamMember.objects.filter(business=business, is_active=True).values_list('user_id', flat=True))
    return User.objects.filter(id__in=ids, is_active=True)


def display_name(u):
    if not u:
        return 'Someone'
    return (u.get_full_name() or u.email or u.username or 'User').strip()


def initials(u):
    n = display_name(u)
    parts = [p for p in re.split(r'[\s@._-]+', n) if p]
    return ((parts[0][0] + (parts[1][0] if len(parts) > 1 else '')) if parts else '?').upper()


def prefs(user):
    p, _ = ChatPrefs.objects.get_or_create(user=user)
    return p


# ─────────────────────────────── presence / typing ───────────────────────────────

def touch(user):
    cache.set(f'chat:seen:{user.pk}', timezone.now().timestamp(), 86400)


def last_seen(uid):
    return cache.get(f'chat:seen:{uid}')


def online(uid):
    t = last_seen(uid)
    return bool(t and timezone.now().timestamp() - t < ONLINE_SECONDS)


def set_typing(thread_id, user):
    cache.set(f'chat:typing:{thread_id}:{user.pk}', display_name(user).split(' ')[0], TYPING_SECONDS)
    key = f'chat:typers:{thread_id}'
    ids = set(cache.get(key) or []); ids.add(user.pk)
    cache.set(key, list(ids), 60)


def typing_names(thread_id, exclude_id):
    out = []
    for uid in cache.get(f'chat:typers:{thread_id}') or []:
        if uid == exclude_id:
            continue
        n = cache.get(f'chat:typing:{thread_id}:{uid}')
        if n:
            out.append(n)
    return out


def support_online():
    return any(online(u.pk) for u in agents())


# ─────────────────────────────── threads ───────────────────────────────

def team_thread(business):
    t, _ = ChatThread.objects.get_or_create(business=business, kind='team')
    return t


def support_thread(business):
    t, _ = ChatThread.objects.get_or_create(business=business, kind='support')
    return t


def direct_thread(business, a, b):
    if a.pk == b.pk:
        raise ChatError('Pick someone else to talk to.')
    ids = set(people_of(business).values_list('id', flat=True))
    if a.pk not in ids or b.pk not in ids:
        raise ChatError('You can only message people in your business.')
    lo, hi = sorted([a, b], key=lambda u: u.pk)
    t, _ = ChatThread.objects.get_or_create(business=business, kind='direct', user_a=lo, user_b=hi)
    return t


def can_access(user, thread, business):
    if thread.kind == 'support':
        return (business is not None and thread.business_id == business.pk) or is_agent(user)
    if business is None or thread.business_id != business.pk:
        return False
    if thread.kind == 'team':
        return True
    return user.pk in (thread.user_a_id, thread.user_b_id)


def accessible_threads(user, business):
    q = Q(pk__in=[])
    if business is not None:
        team_thread(business); support_thread(business)
        q |= Q(business=business, kind__in=['team', 'support'])
        q |= Q(business=business, kind='direct') & (Q(user_a=user) | Q(user_b=user))
    if is_agent(user):
        q |= Q(kind='support', last_at__isnull=False)
    return ChatThread.objects.filter(q).select_related('business', 'user_a', 'user_b', 'assigned_to').distinct()


def participants(thread):
    """Users who should hear about a new message in this thread."""
    if thread.kind == 'team':
        return list(people_of(thread.business))
    if thread.kind == 'direct':
        return [u for u in (thread.user_a, thread.user_b) if u]
    ppl = list(people_of(thread.business))
    if thread.assigned_to_id:
        ppl.append(thread.assigned_to)
    else:
        ppl += list(agents())
    seen, out = set(), []
    for u in ppl:
        if u.pk not in seen:
            seen.add(u.pk); out.append(u)
    return out


def title_for(thread, user, business):
    """(title, subtitle) as this person sees the conversation."""
    if thread.kind == 'team':
        return f'{thread.business.business_name} team', 'Everyone in your business'
    if thread.kind == 'support':
        if business is not None and thread.business_id == business.pk:
            return 'TapTap Support', 'Online now' if support_online() else 'Leave a message — we reply soon'
        st = thread.get_support_status_display() if thread.support_status else 'New'
        who = f' · {display_name(thread.assigned_to).split(" ")[0]}' if thread.assigned_to_id else ''
        return thread.business.business_name, f'Support · {st}{who}'
    other = thread.user_b if thread.user_a_id == user.pk else thread.user_a
    return display_name(other), 'Online' if other and online(other.pk) else ''


# ─────────────────────────────── messages ───────────────────────────────

def mentioned(thread, body):
    """@FirstName or @email in a team/support message → user ids."""
    tags = {t.lower() for t in re.findall(r'@([\w.\-]+)', body or '')}
    if not tags:
        return []
    out = []
    for u in participants(thread):
        names = {(u.first_name or '').lower(), (u.username or '').lower(), (u.email or '').split('@')[0].lower()}
        if tags & {n for n in names if n}:
            out.append(u.pk)
    return out


def post(thread, user, body, page_url='', page_title='', system=False):
    body = (body or '').strip()
    if not body and not page_url:
        raise ChatError('Type a message first.')
    if len(body) > 4000:
        raise ChatError('Messages can be up to 4000 characters.')
    key = f'chat:rate:{user.pk}'
    n = cache.get(key, 0)
    if n > 40:
        raise ChatError('You are sending messages very fast. Wait a moment.')
    cache.set(key, n + 1, 60)
    m = ChatMessage.objects.create(thread=thread, sender=user, body=body, page_url=(page_url or '')[:300],
                                   page_title=(page_title or '')[:120], mentions=mentioned(thread, body), system=system)
    thread.last_at = m.created_at
    fields = ['last_at']
    if thread.kind == 'support':
        if is_agent(user) and user.pk not in set(people_of(thread.business).values_list('id', flat=True)):
            if not thread.assigned_to_id:
                thread.assigned_to = user; fields.append('assigned_to')
        elif thread.support_status != 'open':
            thread.support_status = 'open'; fields.append('support_status')
    thread.save(update_fields=fields)
    mark_read(thread, user, m.pk)
    cache.delete(f'chat:typing:{thread.pk}:{user.pk}')
    return m


def mark_read(thread, user, upto):
    r, _ = ChatRead.objects.get_or_create(thread=thread, user=user)
    if upto > r.last_read:
        r.last_read = upto
        r.save(update_fields=['last_read'])
    return r


def agent_ids():
    return set(agents().values_list('id', flat=True))


def message_json(m, me_id, agent_set=None):
    s = m.sender
    agent_set = agent_ids() if agent_set is None else agent_set
    is_ag = bool(s and m.thread.kind == 'support' and s.pk in agent_set and s.pk != m.thread.business.user_id)
    return {'id': m.pk, 'thread': m.thread_id, 'body': m.body, 'mine': bool(s and s.pk == me_id),
            'sender': ('TapTap · ' + display_name(s)) if is_ag else (display_name(s) if s else 'TapTap'),
            'initials': initials(s) if s else 'TT', 'agent': is_ag,
            'at': timezone.localtime(m.created_at).isoformat(), 'page_url': m.page_url, 'page_title': m.page_title,
            'mention_me': me_id in (m.mentions or []), 'system': m.system}


def unread_map(user, threads):
    reads = {r.thread_id: r for r in ChatRead.objects.filter(user=user, thread__in=threads)}
    out = {}
    for t in threads:
        r = reads.get(t.pk)
        out[t.pk] = (ChatMessage.objects.filter(thread=t, id__gt=r.last_read if r else 0)
                     .exclude(sender=user).count(), bool(r and r.muted))
    return out


def colleagues(user, business):
    if business is None:
        return []
    return [u for u in people_of(business) if u.pk != user.pk]


# ─────────────────────────────── missed-message emails ───────────────────────────────

def _member_business_id(user):
    from .models_team import TeamMember
    return TeamMember.objects.filter(user=user).values_list('business_id', flat=True).first()


def email_missed(now=None):
    """Email people the chat messages they have not read after their chosen delay (default 10 min),
    at most one email per person every 30 minutes, only while they are away. Run every minute."""
    from datetime import timedelta
    from django.conf import settings
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string
    now = now or timezone.now()
    window = now - timedelta(hours=24)
    recent = (ChatMessage.objects.filter(created_at__gte=window, system=False)
              .select_related('thread', 'thread__business', 'thread__user_a', 'thread__user_b', 'thread__assigned_to', 'sender'))
    per_user = {}
    for m in recent:
        for u in participants(m.thread):
            if not u.email or (m.sender_id == u.pk):
                continue
            per_user.setdefault(u.pk, (u, []))[1].append(m)
    sent = 0
    site = (getattr(settings, 'SITE_URL', '') or '').rstrip('/')
    for uid, (u, msgs) in per_user.items():
        p = prefs(u)
        if not p.email_missed or online(uid):
            continue
        if p.last_email_at and now - p.last_email_at < timedelta(minutes=30):
            continue
        delay = timedelta(minutes=p.email_after_minutes or 10)
        reads = {r.thread_id: r for r in ChatRead.objects.filter(user=u, thread_id__in={m.thread_id for m in msgs})}
        todo = []
        for m in msgs:
            r = reads.get(m.thread_id)
            if r and (m.pk <= r.last_read or m.pk <= r.emailed_upto or (r.muted and uid not in (m.mentions or []))):
                continue
            if now - m.created_at < delay:
                continue
            todo.append(m)
        if not todo:
            continue
        groups = {}
        for m in todo:
            own = m.thread.business if m.thread.business.user_id == u.pk or _member_business_id(u) == m.thread.business_id else None
            title = title_for(m.thread, u, own)[0]
            groups.setdefault(m.thread_id, {'title': title, 'items': []})['items'].append(
                {'who': display_name(m.sender), 'body': (m.body or m.page_title)[:500], 'when': timezone.localtime(m.created_at)})
        ctx = {'name': display_name(u).split(' ')[0], 'groups': list(groups.values()), 'count': len(todo), 'site': site}
        try:
            html = render_to_string('core/email/chat_missed.html', ctx)
            text = render_to_string('core/email/chat_missed.txt', ctx)
            msg = EmailMultiAlternatives(subject=f'{len(todo)} unread message{"s" if len(todo) > 1 else ""} on TapTap chat',
                                         body=text, from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None), to=[u.email])
            msg.attach_alternative(html, 'text/html')
            msg.send()
        except Exception:
            continue
        for tid in groups:
            r, _ = ChatRead.objects.get_or_create(thread_id=tid, user=u)
            r.emailed_upto = max(m.pk for m in todo if m.thread_id == tid)
            r.save(update_fields=['emailed_upto'])
        p.last_email_at = now; p.save(update_fields=['last_email_at'])
        sent += 1
    return sent
