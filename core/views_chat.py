"""Live chat endpoints (JSON, polled by static/js/chat.js) and the support-team admin page."""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db.models import Max
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST

from . import chat, chat_media
from .models_chat import ChatMessage, ChatRead, ChatThread, SupportAgent


def _biz(request):
    return getattr(request, 'tt_business', None)


def _body(request):
    try:
        return json.loads(request.body or '{}')
    except ValueError:
        return {}


def _thread(request, pk):
    t = get_object_or_404(ChatThread.objects.select_related('business', 'user_a', 'user_b', 'assigned_to'), pk=pk)
    if not chat.can_access(request.user, t, _biz(request)):
        raise chat.ChatError('You cannot open this conversation.')
    return t


def _err(e, status=400):
    return JsonResponse({'ok': False, 'message': str(e)}, status=status)


def _threads_json(request, threads):
    user, biz = request.user, _biz(request)
    un = chat.unread_map(user, threads)
    last_ids = ChatMessage.objects.filter(thread__in=threads).values('thread').annotate(m=Max('id')).values_list('m', flat=True)
    last = {m.thread_id: m for m in ChatMessage.objects.filter(id__in=list(last_ids)).select_related('sender')}
    out = []
    for t in threads:
        title, sub = chat.title_for(t, user, biz)
        m = last.get(t.pk)
        unread, muted = un.get(t.pk, (0, False))
        other = None
        if t.kind == 'direct':
            other = t.user_b if t.user_a_id == user.pk else t.user_a
        own_support = t.kind == 'support' and biz is not None and t.business_id == biz.pk
        out.append({'id': t.pk, 'kind': t.kind, 'title': title, 'sub': sub, 'unread': unread, 'muted': muted,
                    'initials': chat.initials(other) if other else ('TT' if own_support else ''.join(w[0] for w in title.split()[:2]).upper()),
                    'online': chat.online(other.pk) if other else (chat.support_online() if own_support else False),
                    'inbox': t.kind == 'support' and not own_support, 'status': t.support_status,
                    'last': (('You: ' if m.sender_id == user.pk else '') + chat.preview_text(m))[:80] if m else '',
                    'last_at': timezone.localtime(m.created_at).isoformat() if m else '', 'last_id': m.pk if m else 0})
    order = {'team': 0, 'support': 1, 'direct': 2}
    out.sort(key=lambda x: (x['inbox'], -(x['last_id'] if x['inbox'] or x['kind'] == 'direct' else 10 ** 12), order.get(x['kind'], 3)))
    return out


@login_required
def chat_state(request):
    """The poll: conversations with unread counts, new messages since ?since=, typing and read receipts."""
    user, biz = request.user, _biz(request)
    chat.touch(user)
    threads = list(chat.accessible_threads(user, biz))
    ids = [t.pk for t in threads]
    raw = request.GET.get('since', '')
    since = int(raw) if raw.lstrip('-').isdigit() else -1      # -1 = first load: no "new" messages to announce
    agent_set = chat.agent_ids()
    new = []
    if since >= 0:
        rows = chat_media.prime(list(ChatMessage.objects.filter(thread_id__in=ids, id__gt=since)
                                     .select_related('sender', 'thread', 'thread__business').order_by('id')[:60]))
        for m in rows:
            d = chat.message_json(m, user.pk, agent_set)
            d['notify'] = not d['mine']
            new.append(d)
    top = ChatMessage.objects.filter(thread_id__in=ids).order_by('-id').values_list('id', flat=True).first() or 0
    open_id = int(request.GET.get('open') or 0)
    extra = {}
    if open_id in ids:
        t = next(x for x in threads if x.pk == open_id)
        if request.GET.get('typing') == '1':
            chat.set_typing(open_id, user)
        extra['typing'] = chat.typing_info(open_id, user.pk)
        if t.kind == 'direct':
            other = t.user_b_id if t.user_a_id == user.pk else t.user_a_id
            r = ChatRead.objects.filter(thread=t, user_id=other).first()
            extra['seen_upto'] = r.last_read if r else 0
    p = chat.prefs(user)
    people = [{'id': u.pk, 'name': chat.display_name(u), 'initials': chat.initials(u), 'online': chat.online(u.pk)}
              for u in chat.colleagues(user, biz)]
    return JsonResponse({'ok': True, 'me': {'id': user.pk, 'name': chat.display_name(user), 'agent': chat.is_agent(user)},
                         'threads': _threads_json(request, threads), 'new': new, 'top': top, 'people': people,
                         'typing_threads': chat.typing_map(ids, user.pk),
                         'prefs': {'sound': p.sound, 'popups': p.popups, 'email_missed': p.email_missed, 'email_after': p.email_after_minutes},
                         **extra})


@login_required
def chat_history(request, pk):
    try:
        t = _thread(request, pk)
    except chat.ChatError as e:
        return _err(e, 403)
    before = int(request.GET.get('before') or 0)
    qs = t.messages.select_related('sender', 'thread', 'thread__business')
    if before:
        qs = qs.filter(id__lt=before)
    rows = chat_media.prime(list(qs.order_by('-id')[:40])[::-1])
    agent_set = chat.agent_ids()
    if rows and not before:
        chat.mark_read(t, request.user, rows[-1].pk)
    title, sub = chat.title_for(t, request.user, _biz(request))
    return JsonResponse({'ok': True, 'thread': {'id': t.pk, 'kind': t.kind, 'title': title, 'sub': sub, 'status': t.support_status,
                                                'agent_view': t.kind == 'support' and chat.is_agent(request.user) and not (_biz(request) and _biz(request).pk == t.business_id),
                                                'can_call': t.kind in ('direct', 'support'), 'can_photo': True,
                                                'photo_days': chat_media.DAYS},
                         'messages': [chat.message_json(m, request.user.pk, agent_set) for m in rows], 'more': len(rows) == 40})


@login_required
@require_POST
def chat_send(request):
    d = _body(request)
    try:
        t = _thread(request, int(d.get('thread') or 0))
        m = chat.post(t, request.user, d.get('body', ''), d.get('page_url', ''), d.get('page_title', ''))
    except (chat.ChatError, ValueError) as e:
        return _err(e)
    return JsonResponse({'ok': True, 'message': chat.message_json(m, request.user.pk)})


@login_required
@require_POST
def chat_read(request):
    d = _body(request)
    try:
        t = _thread(request, int(d.get('thread') or 0))
    except (chat.ChatError, ValueError) as e:
        return _err(e, 403)
    chat.mark_read(t, request.user, int(d.get('upto') or 0))
    return JsonResponse({'ok': True})


@login_required
@require_POST
def chat_direct(request):
    d = _body(request)
    biz = _biz(request)
    other = User.objects.filter(pk=int(d.get('user') or 0), is_active=True).first()
    if not biz or not other:
        return _err('Pick someone from your team.')
    try:
        t = chat.direct_thread(biz, request.user, other)
    except chat.ChatError as e:
        return _err(e)
    return JsonResponse({'ok': True, 'thread': t.pk})


@login_required
@require_POST
def chat_settings(request):
    d = _body(request)
    p = chat.prefs(request.user)
    for k in ('sound', 'popups', 'email_missed'):
        if k in d:
            setattr(p, k, bool(d[k]))
    if 'email_after' in d:
        try: p.email_after_minutes = max(2, min(240, int(d['email_after'])))
        except (TypeError, ValueError): pass
    p.save()
    if 'mute' in d:
        try:
            t = _thread(request, int(d.get('thread') or 0))
            r, _ = ChatRead.objects.get_or_create(thread=t, user=request.user)
            r.muted = bool(d['mute']); r.save(update_fields=['muted'])
        except (chat.ChatError, ValueError) as e:
            return _err(e)
    return JsonResponse({'ok': True})


@login_required
@require_POST
def chat_support_action(request):
    """Support agents: mark a conversation solved / reopen / take it."""
    if not chat.is_agent(request.user):
        return _err('Only the TapTap support team can do this.', 403)
    d = _body(request)
    t = get_object_or_404(ChatThread, pk=int(d.get('thread') or 0), kind='support')
    act = d.get('action')
    if act == 'solve':
        t.support_status = 'solved'
        chat.post(t, request.user, '✅ Marked as solved. Write here any time if you need more help.', system=True)
    elif act == 'reopen':
        t.support_status = 'open'
    elif act == 'take':
        t.assigned_to = request.user
    else:
        return _err('Unknown action.')
    t.save(update_fields=['support_status', 'assigned_to'])
    return JsonResponse({'ok': True})


@login_required
def chat_page(request):
    """Full-screen chat (the support inbox for agents)."""
    # business people: the normal app; superusers: the platform console; other support agents: a plain chat screen
    base = 'core/base.html' if _biz(request) is not None else ('core/platform/base.html' if request.user.is_superuser else 'core/chat_standalone.html')
    return render(request, 'core/chat_page.html', {'is_agent': chat.is_agent(request.user), 'base_template': base})


# ─────────────────────────────── platform: support team ───────────────────────────────

@login_required
def platform_support_team(request):
    if request.method == 'POST':
        act = request.POST.get('action')
        if act == 'add':
            email = request.POST.get('email', '').strip().lower()
            u = User.objects.filter(email__iexact=email).first() or User.objects.filter(username__iexact=email).first()
            if not u:
                messages.error(request, f'No user with the email {email}. Ask them to register first, or create the login in Django admin.')
            else:
                a, created = SupportAgent.objects.get_or_create(user=u, defaults={'added_by': request.user})
                a.active = True; a.title = request.POST.get('title', '').strip()[:60] or a.title or 'TapTap Support'; a.save()
                messages.success(request, f'{chat.display_name(u)} is on the support team.')
        elif act in ('pause', 'resume', 'remove'):
            a = get_object_or_404(SupportAgent, pk=request.POST.get('id'))
            if act == 'remove':
                a.delete(); messages.success(request, 'Removed from the support team.')
            else:
                a.active = act == 'resume'; a.save(update_fields=['active'])
        return redirect('platform_support_team')
    supers = User.objects.filter(is_superuser=True, is_active=True)
    rows = SupportAgent.objects.select_related('user', 'added_by')
    open_threads = ChatThread.objects.filter(kind='support', support_status='open').count()
    return render(request, 'core/platform/support_team.html', {'agents': rows, 'supers': supers, 'open_threads': open_threads,
                                                              'online': {u.pk: chat.online(u.pk) for u in list(supers) + [a.user for a in rows]}})


# ─────────────────────────────── photos ───────────────────────────────

@login_required
@require_POST
def chat_upload(request):
    """Send a photo (multipart: thread, file, body, once). Kept CHAT_IMAGE_DAYS (7) days, then deleted for good."""
    try:
        t = _thread(request, int(request.POST.get('thread') or 0))
        m = chat_media.save_image(t, request.user, request.FILES.get('file'), request.POST.get('body', ''),
                                  request.POST.get('once') == '1')
    except (chat.ChatError, ValueError) as e:
        return _err(e)
    chat_media.prime([m])
    return JsonResponse({'ok': True, 'message': chat.message_json(m, request.user.pk)})


@login_required
def chat_image(request, pk):
    from django.http import HttpResponse, HttpResponseNotFound
    m = get_object_or_404(ChatMessage.objects.select_related('thread', 'thread__business'), pk=pk)
    if not chat.can_access(request.user, m.thread, _biz(request)):
        return HttpResponseNotFound()
    got = chat_media.open_image(m, request.user)
    if got is None:
        return HttpResponse('This photo has expired.', status=410, content_type='text/plain')
    if got == 'sender':
        return HttpResponse('View-once photos cannot be reopened.', status=403, content_type='text/plain')
    data, mime, once = got
    r = HttpResponse(data, content_type=mime)
    r['X-Content-Type-Options'] = 'nosniff'
    r['Content-Security-Policy'] = "default-src 'none'; sandbox"
    r['Content-Disposition'] = f'inline; filename="taptap-photo-{pk}.{mime.split("/")[1]}"'
    r['Cache-Control'] = 'no-store' if once else 'private, max-age=86400'
    r['Referrer-Policy'] = 'no-referrer'
    return r


# ─────────────────────────────── calls & screen sharing ───────────────────────────────

@login_required
@xframe_options_sameorigin                     # the pop-up-blocked overlay frames this page
def chat_call_page(request):
    """The call window (a pop-out, or an overlay when pop-ups are blocked). Start: ?thread=&video=  Join: ?join="""
    r = render(request, 'core/chat_call.html', {
        'thread': int(request.GET.get('thread') or 0), 'video': request.GET.get('video') == '1',
        'join': (request.GET.get('join') or '')[:40], 'embedded': request.GET.get('embed') == '1',
        'me': chat.display_name(request.user), 'me_initials': chat.initials(request.user),
        'image_days': chat_media.DAYS})
    r['Permissions-Policy'] = 'camera=(self), microphone=(self), display-capture=(self), autoplay=(self)'
    r['Cache-Control'] = 'no-store'
    r['Referrer-Policy'] = 'no-referrer'
    return r




@login_required
def chat_call_poll(request):
    """Cheap poll (cache only). No ?id: calls ringing for me. With ?id: that call's state (members only)."""
    from . import chat_call
    cid = request.GET.get('id', '')
    if not cid:
        return JsonResponse({'ok': True, 'rings': chat_call.rings_for(request.user)})
    call = chat_call.get(cid)
    if not call or not chat_call.is_member(call, request.user.pk):
        return JsonResponse({'ok': True, 'call': {'id': cid, 'state': 'ended', 'reason': 'gone'}})
    return JsonResponse({'ok': True, 'call': chat_call.public_state(call, request.user.pk)})


@login_required
@require_POST
def chat_call_act(request):
    """start · offer · accept · answer · decline · end · reply (decline with a quick message)."""
    from . import chat_call
    d = _body(request) if request.content_type == 'application/json' else request.POST.dict()
    act, user = d.get('action'), request.user
    try:
        if act == 'start':
            t = _thread(request, int(d.get('thread') or 0))
            call = chat_call.start(t, user, video=bool(d.get('video')))
            title, _ = chat.title_for(t, user, _biz(request))
            return JsonResponse({'ok': True, 'call': chat_call.public_state(call, user.pk), 'title': title,
                                 'ice': chat_call.ice_config(user)})
        call = chat_call.get(d.get('id', ''))
        if not call or not (chat_call.is_member(call, user.pk) or user.pk in call['targets']):
            raise chat.ChatError('This call has ended.')
        if act == 'offer':
            chat_call.post_offer(call, user, d.get('desc'))
        elif act == 'accept':
            offer = chat_call.accept(call, user)
            return JsonResponse({'ok': True, 'offer': offer, 'ice': chat_call.ice_config(user),
                                 'call': chat_call.public_state(call, user.pk), 'from': call['label']})
        elif act == 'answer':
            chat_call.post_answer(call, user, d.get('desc'))
        elif act in ('decline', 'reply'):
            chat_call.decline(call, user)
            text = (d.get('text') or '').strip()[:300]
            if act == 'reply' and text:
                from .models_chat import ChatThread
                chat.post(ChatThread.objects.select_related('business').get(pk=call['thread']), user, text)
        elif act == 'end':
            chat_call.finish(call, 'cancelled' if user.pk == call['caller'] and chat_call.state(call)[0] == 'ringing' else d.get('reason') or 'ended')
        else:
            raise chat.ChatError('Unknown call action.')
    except (chat.ChatError, ValueError) as e:
        return _err(e)
    return JsonResponse({'ok': True})
