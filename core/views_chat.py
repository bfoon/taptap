"""Live chat endpoints (JSON, polled by static/js/chat.js) and the support-team admin page."""
import json
import time
from datetime import datetime, timezone as dt_timezone

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

# what message_json reads (sender, thread, the quoted message of a reply) — one query, not one per message
MSG_RELATED = ('sender', 'thread', 'thread__business', 'reply_to', 'reply_to__sender')


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
    chat_media.sweep_voice_soon()              # listened voice messages go on time even without Celery beat
    threads = list(chat.accessible_threads(user, biz))
    ids = [t.pk for t in threads]
    raw = request.GET.get('since', '')
    since = int(raw) if raw.lstrip('-').isdigit() else -1      # -1 = first load: no "new" messages to announce
    agent_set = chat.agent_ids()
    new = []
    if since >= 0:
        rows = chat_media.prime(list(ChatMessage.objects.filter(thread_id__in=ids, id__gt=since)
                                     .select_related(*MSG_RELATED).order_by('id')[:60]))
        for m in rows:
            d = chat.message_json(m, user.pk, agent_set)
            d['notify'] = not d['mine'] and not d['deleted']
            new.append(d)
    # edited / deleted messages since the last poll (?chg= the "now" it got back), so every open chat updates them
    now = time.time()
    changed = []
    try:
        chg = float(request.GET.get('chg') or 0)
    except ValueError:
        chg = 0
    if chg > 0:
        after = datetime.fromtimestamp(max(chg, now - 3600) - 2, tz=dt_timezone.utc)   # 2 s overlap: never miss one
        rows = chat_media.prime(list(ChatMessage.objects.filter(thread_id__in=ids, changed_at__gt=after)
                                     .select_related(*MSG_RELATED).order_by('changed_at')[:100]))
        changed = [chat.message_json(m, user.pk, agent_set) for m in rows]
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
                         'changed': changed, 'now': now,
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
    qs = t.messages.select_related(*MSG_RELATED)
    if before:
        qs = qs.filter(id__lt=before)
    rows = chat_media.prime(list(qs.order_by('-id')[:40])[::-1])
    agent_set = chat.agent_ids()
    # ?peek=1: the phone saving a conversation in the background — the person has not seen it yet, so it stays unread
    if rows and not before and request.GET.get('peek') != '1':
        chat.mark_read(t, request.user, rows[-1].pk)
    title, sub = chat.title_for(t, request.user, _biz(request))
    return JsonResponse({'ok': True, 'thread': {'id': t.pk, 'kind': t.kind, 'title': title, 'sub': sub, 'status': t.support_status,
                                                'agent_view': t.kind == 'support' and chat.is_agent(request.user) and not (_biz(request) and _biz(request).pk == t.business_id),
                                                'can_call': t.kind in ('direct', 'support'), 'can_photo': True,
                                                'photo_days': chat_media.DAYS,
                                                'video_mb': chat_media.VIDEO_MAX_BYTES // (1024 * 1024)},
                         'messages': [chat.message_json(m, request.user.pk, agent_set) for m in rows], 'more': len(rows) == 40})


@login_required
@require_POST
def chat_send(request):
    d = _body(request)
    try:
        t = _thread(request, int(d.get('thread') or 0))
        m = chat.post(t, request.user, d.get('body', ''), d.get('page_url', ''), d.get('page_title', ''),
                      reply_to=d.get('reply_to') or None)
    except (chat.ChatError, ValueError) as e:
        return _err(e)
    return JsonResponse({'ok': True, 'message': chat.message_json(m, request.user.pk)})


@login_required
@require_POST
def chat_message(request):
    """Edit or delete your own message: {id, action: 'edit', body} or {id, action: 'delete'}."""
    d = _body(request)
    try:
        m = ChatMessage.objects.select_related(*MSG_RELATED).filter(pk=int(d.get('id') or 0)).first()
        if m is None:
            raise chat.ChatError('That message is no longer there.')
        _thread(request, m.thread_id)                  # you must still be in that conversation
        action = d.get('action')
        if action == 'edit':
            chat.edit(m, request.user, d.get('body', ''))
        elif action == 'delete':
            chat.delete(m, request.user)
        else:
            raise chat.ChatError('Unknown action.')
    except (chat.ChatError, ValueError) as e:
        return _err(e)
    chat_media.prime([m])
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
    """Send a photo, a short video clip or a voice message
    (multipart: thread, file, body, once | kind=video, w, h | kind=voice, secs, reply_to).
    Every chat file is kept CHAT_IMAGE_DAYS (7) days, then deleted for good; a voice message 1 minute after it
    is first played."""
    try:
        t = _thread(request, int(request.POST.get('thread') or 0))
        f = request.FILES.get('file')
        if request.POST.get('kind') == 'voice':
            m = chat_media.save_voice(t, request.user, f, request.POST.get('secs'), request.POST.get('reply_to') or None)
        elif request.POST.get('kind') == 'video' or (f is not None and (f.content_type or '').startswith('video/')):
            m = chat_media.save_video(t, request.user, f, request.POST.get('body', ''),
                                      request.POST.get('w'), request.POST.get('h'))
        else:
            m = chat_media.save_image(t, request.user, f, request.POST.get('body', ''),
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
    got = chat_media.open_image(m, request.user, listen=request.GET.get('listen') == '1')
    if got is None:
        return HttpResponse('This file has expired.', status=410, content_type='text/plain')
    if got == 'nolisten':                      # an older app showing it as a picture: never counts as listening
        return HttpResponse('Update TapTap to play voice messages.', status=409, content_type='text/plain')
    if got == 'sender':
        return HttpResponse('View-once photos cannot be reopened.', status=403, content_type='text/plain')
    data, mime, once = got
    status, total, extra = 200, len(data), {}
    av = mime.startswith(('video/', 'audio/'))
    rng = request.META.get('HTTP_RANGE', '') if av else ''
    if rng.startswith('bytes=') and ',' not in rng:          # media players (Safari above all) seek with Range
        a, _, b = rng[6:].strip().partition('-')
        try:
            if a == '':
                start, end = max(0, total - int(b)), total - 1
            else:
                start, end = int(a), min(int(b) if b else total - 1, total - 1)
        except ValueError:
            start, end = 0, total - 1
        if start >= total or start > end:
            r = HttpResponse(status=416)
            r['Content-Range'] = f'bytes */{total}'
            return r
        data, status = data[start:end + 1], 206
        extra['Content-Range'] = f'bytes {start}-{end}/{total}'
    r = HttpResponse(data, content_type=mime, status=status)
    for k, v in extra.items():
        r[k] = v
    if av:
        r['Accept-Ranges'] = 'bytes'
    what = 'video' if mime.startswith('video/') else 'voice' if mime.startswith('audio/') else 'photo'
    r['X-Content-Type-Options'] = 'nosniff'
    r['Content-Security-Policy'] = "default-src 'none'; sandbox"
    r['Content-Disposition'] = f'inline; filename="taptap-{what}-{pk}.{mime.split("/")[1]}"'
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
        # Studio name tag: "TapTap Support" for agents, else the business name (editable in the call)
        'me_title': 'TapTap Support' if chat.is_agent(request.user) else getattr(_biz(request), 'business_name', '') or '',
        'brand': getattr(_biz(request), 'brand_color', '') or '',
        'image_days': chat_media.DAYS, 'video_secs': chat_media.VIDEO_SECONDS,
        'video_mb': chat_media.VIDEO_MAX_BYTES // (1024 * 1024)})
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
    """start · offer · accept · answer · decline · end · reply (decline with a quick message) · log (remote-control note)
    · reoffer / reanswer / reask (reconnect a dropped call with an ICE restart — see core/chat_call.py)."""
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
        elif act == 'reoffer':
            chat_call.post_reoffer(call, user, d.get('desc'), d.get('gen'))
        elif act == 'reanswer':
            chat_call.post_reanswer(call, user, d.get('desc'), d.get('gen'))
        elif act == 'reask':
            chat_call.ask_restart(call, user)
        elif act in ('decline', 'reply'):
            chat_call.decline(call, user)
            text = (d.get('text') or '').strip()[:300]
            if act == 'reply' and text:
                from .models_chat import ChatThread
                chat.post(ChatThread.objects.select_related('business').get(pk=call['thread']), user, text)
        elif act == 'log':
            if d.get('event') == 'control':
                chat_call.log_control(call, user, d.get('secs'), d.get('summary'))
        elif act == 'end':
            chat_call.finish(call, 'cancelled' if user.pk == call['caller'] and chat_call.state(call)[0] == 'ringing' else d.get('reason') or 'ended')
        else:
            raise chat.ChatError('Unknown call action.')
    except (chat.ChatError, ValueError) as e:
        return _err(e)
    return JsonResponse({'ok': True})
