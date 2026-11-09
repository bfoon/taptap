"""In-app calls from the Wi-Fi pages (core/portal_call.py).

Customer side (public, under /p/ — no TapTap account):
    /p/<slug>/call/       the call screen (HTTPS — browsers only allow the microphone on secure pages, so the
                          router's own http:// login page opens this page instead of calling inline)
    /p/<slug>/call/api/   status · start · offer · poll · reoffer · end · callback (no microphone: "call me back")      (text/plain JSON: no CORS pre-check,
                          so the router page can ask "is anybody there?" before the customer taps)
Staff side:
    /studio/portal/calls/ who answers (the owner picks people; nobody picked = the owner)
"""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.csrf import csrf_exempt

from . import chat, chat_call, portal_call
from .online_portal import _client_ip, _page


def _cors(resp):
    resp['Access-Control-Allow-Origin'] = '*'
    resp['Cache-Control'] = 'no-store'
    return resp


def _safe_back(url):
    url = str(url or '')[:600]
    return url if url.startswith(('http://', 'https://')) else ''


@xframe_options_sameorigin                       # the hosted Wi-Fi page shows it in an overlay
def portal_call_page(request, slug):
    page = _page(slug)
    business = page.business
    theme = (page.config or {}).get('theme') or {}
    from .views_studio import business_ctx
    biz = business_ctx(business)
    r = render(request, 'core/portal_call.html', {
        'page': page, 'biz': biz, 'business': business,
        'accent': theme.get('accent') or biz['brand'], 'accent_text': theme.get('accent_text') or '#ffffff',
        'embedded': request.GET.get('embed') == '1', 'back': _safe_back(request.GET.get('back')),
        'cfg': {'api': f'/p/{page.slug}/call/api/', 'voice': f'/p/{page.slug}/call/voice/', 'business': biz['name'], 'phone': biz['phone'], 'logo': biz['logo'],
                'name': (request.GET.get('name') or '')[:60], 'embedded': request.GET.get('embed') == '1'},
    })
    r['Permissions-Policy'] = 'microphone=(self), camera=(), autoplay=(self)'
    r['Cache-Control'] = 'no-store'
    r['Referrer-Policy'] = 'no-referrer'
    r['X-Robots-Tag'] = 'noindex'
    return r


@csrf_exempt
def portal_call_api(request, slug):
    if request.method == 'OPTIONS':
        resp = _cors(JsonResponse({'ok': True}))
        resp['Access-Control-Allow-Methods'] = 'POST'
        resp['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp
    if request.method != 'POST' or len(request.body or b'') > 60000:
        return _cors(JsonResponse({'ok': False, 'message': 'Bad request.'}, status=400))
    try:
        d = json.loads(request.body or b'{}')
        if not isinstance(d, dict):
            raise ValueError
    except ValueError:
        return _cors(JsonResponse({'ok': False, 'message': 'Bad request.'}, status=400))
    page = _page(slug)
    act = d.get('action')
    try:
        if act == 'status':
            return _cors(JsonResponse({'ok': True, **portal_call.status(page.business)}))
        if act == 'callback':                   # no microphone on this phone: "call me back" instead
            portal_call.callback_request(page, name=d.get('name'), phone=d.get('phone'), ip=_client_ip(request),
                                         why=str(d.get('why') or '')[:20])
            return _cors(JsonResponse({'ok': True, 'message': 'Thank you — we will call you back on that number.'}))
        if act == 'start':
            call, secret = portal_call.start(page, name=d.get('name'), phone=d.get('phone'), ip=_client_ip(request))
            return _cors(JsonResponse({'ok': True, 'id': call['id'], 'secret': secret,
                                       'ice': chat_call.ice_config(portal_call.Guest(call['caller'])),
                                       'call': portal_call.guest_state(call)}))
        call = portal_call.guest_call(d.get('id', ''), d.get('secret', ''))
        if call is None or call['guest']['business'] != page.business_id:
            return _cors(JsonResponse({'ok': True, 'call': {'id': d.get('id', ''), 'state': 'ended', 'reason': 'gone'}}))
        me = portal_call.Guest(call['caller'])
        if act == 'poll':
            return _cors(JsonResponse({'ok': True, 'call': portal_call.guest_state(call)}))
        if act == 'offer':
            chat_call.post_offer(call, me, d.get('desc'))
        elif act == 'reoffer':
            chat_call.post_reoffer(call, me, d.get('desc'), d.get('gen'))
        elif act == 'end':
            ringing = chat_call.state(call)[0] == 'ringing'
            reason = d.get('reason') if d.get('reason') in ('lost', 'failed') else ('cancelled' if ringing else 'ended')
            chat_call.finish(call, reason)
        else:
            raise chat.ChatError('Unknown call action.')
    except (chat.ChatError, ValueError) as e:
        return _cors(JsonResponse({'ok': False, 'message': str(e)}, status=400))
    return _cors(JsonResponse({'ok': True}))


@csrf_exempt
def portal_call_voice(request, slug):
    """A voice message from the customer's page: the raw recording as the body (sent as text/plain so the router's
    http:// page needs no CORS pre-check), with ?name=&phone=&secs= in the address."""
    if request.method == 'OPTIONS':
        resp = _cors(JsonResponse({'ok': True}))
        resp['Access-Control-Allow-Methods'] = 'POST'
        resp['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp
    from . import chat_media
    if request.method != 'POST':
        return _cors(JsonResponse({'ok': False, 'message': 'Bad request.'}, status=400))
    try:
        size = int(request.META.get('CONTENT_LENGTH') or 0)
    except ValueError:
        size = 0
    if size > chat_media.VOICE_MAX_BYTES:
        return _cors(JsonResponse({'ok': False, 'message': 'That voice message is too long (up to '
                                   f'{chat_media.VOICE_SECONDS} seconds).'}, status=400))
    page = _page(slug)
    try:
        portal_call.voice_message(page, request.body or b'', name=request.GET.get('name', ''),
                                  phone=request.GET.get('phone', ''), secs=request.GET.get('secs'),
                                  ip=_client_ip(request))
    except chat.ChatError as e:
        return _cors(JsonResponse({'ok': False, 'message': str(e)}, status=400))
    return _cors(JsonResponse({'ok': True, 'message': 'Sent — the team will listen and get back to you.'}))


# ─────────────────────────────── owner: who answers ───────────────────────────────

@login_required
def portal_call_settings(request):
    business = request.user.business
    s = portal_call.settings_for(business)
    people = portal_call.candidates(business)
    if request.method == 'POST':
        allowed = {u.pk for u in people}
        picked = [int(x) for x in request.POST.getlist('handlers') if str(x).isdigit() and int(x) in allowed]
        s.handlers.set(picked)
        s.online_first = request.POST.get('online_first') == 'on'
        s.updated_by = request.user
        s.save(update_fields=['online_first', 'updated_by', 'updated_at'])
        names = [chat.display_name(u).split(' ')[0] for u in people if u.pk in picked]
        messages.success(request, ('Wi-Fi calls now ring ' + ', '.join(names) + '.') if names
                         else 'Nobody picked — Wi-Fi calls ring the owner.')
        from .utils import log
        log(business, 'Wi-Fi Calls', 'Answered by: ' + (', '.join(names) or 'owner'))
        return redirect('/studio/portal/#portal-calls')
    return redirect('/studio/portal/#portal-calls')


def settings_ctx(business):
    """For the Portal Studio page: the people, who is picked, who is online, and whether calls are on."""
    s = portal_call.settings_for(business)
    picked = set(s.handlers.values_list('pk', flat=True))
    rows = [{'user': u, 'name': chat.display_name(u), 'initials': chat.initials(u), 'picked': u.pk in picked,
             'owner': u.pk == business.user_id, 'online': chat.online(u.pk)} for u in portal_call.candidates(business)]
    return {'rows': rows, 'online_first': s.online_first, 'enabled': portal_call.calls_enabled(business),
            'answering': [chat.display_name(u).split(' ')[0] for u in portal_call.handlers(business)]}
