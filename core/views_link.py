"""TapTap Link pages, public agent endpoints, agent registration and notifications."""
import hmac
import json
from datetime import time as dtime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import agent as link
from .models import AgentCommand, NotificationSettings, Router, RouterAgent
from .notify import EVENTS, email_configured, notify, prefs, recipients, send_test
from .utils import log


def _b(request):
    return request.user.business


def _client_ip(request):
    return request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', '')).split(',')[0].strip()


def _unauthorized():
    # RouterOS expects a WWW-Authenticate header on HTTP 401; without it Fetch
    # reports "401 should contain www-authenticate header" instead of a clean
    # authentication failure.
    response = HttpResponse('', status=401, content_type='text/plain')
    response['WWW-Authenticate'] = 'Bearer realm="TapTap Link"'
    return response


# ─────────────────────────── public agent endpoints ───────────────────────────
@csrf_exempt
def agent_poll(request):
    if request.method != 'POST':
        return HttpResponse(status=405)
    ip = _client_ip(request)
    auth = request.META.get('HTTP_AUTHORIZATION', '')
    token = auth[7:].strip() if auth.lower().startswith('bearer ') else ''
    agent = link.agent_for_token(token)
    if not agent:
        n = cache.get(f'tt:link:bad:{ip}', 0) + 1
        cache.set(f'tt:link:bad:{ip}', n, 600)
        revoked = RouterAgent.objects.select_related('router__business').filter(token_hash=link._hash(token), revoked=True).first() if token else None
        if revoked:
            notify(
                revoked.router.business,
                'link_rejected',
                f'Revoked TapTap Link token used for {revoked.router.name}',
                f'A router at {ip} tried to connect with the revoked token of {revoked.router.name}. It was refused.',
                key=f'link:revoked:{revoked.pk}',
            )
        return _unauthorized()
    if agent.pinned_ip and agent.pinned_ip != ip:
        notify(
            agent.router.business,
            'link_rejected',
            f'TapTap Link for {agent.router.name} refused from {ip}',
            f'{agent.router.name} is pinned to {agent.pinned_ip}, but a connection came from {ip}. It was refused.',
            key=f'link:pin:{agent.pk}:{ip}',
        )
        return HttpResponse('', status=403, content_type='text/plain')
    if not cache.add(f'tt:link:rate:{agent.pk}', 1, 2):
        return HttpResponse('', content_type='text/plain')
    if len(request.body or b'') > 200_000:
        return HttpResponse('', status=413, content_type='text/plain')
    data = request.POST if request.POST else {}
    if not data:
        from urllib.parse import parse_qsl
        data = dict(parse_qsl(request.body.decode('utf-8', 'ignore'), keep_blank_values=True))
    script = link.handle_poll(agent, data, ip, link.base_url(request))
    return HttpResponse(script, content_type='text/plain; charset=utf-8')


@csrf_exempt
def agent_ack(request):
    try:
        cid = int(request.GET.get('c', '0'))
    except ValueError:
        return HttpResponse(status=400)
    ok = link.handle_ack(cid, request.GET.get('n', ''), request.GET.get('s', ''), request.GET.get('r', '')[:400])
    return HttpResponse('ok' if ok else '', status=200 if ok else 404, content_type='text/plain')


@csrf_exempt
def agent_inventory(request):
    """Receive one signed/chunked RouterOS inventory section from TapTap Link."""
    if request.method != 'POST':
        return HttpResponse(status=405)
    if len(request.body or b'') > 70_000:
        return HttpResponse('payload too large', status=413, content_type='text/plain')
    try:
        cid = int(request.GET.get('c', '0'))
        part = max(0, int(request.GET.get('part', '0')))
    except ValueError:
        return HttpResponse(status=400)
    cmd = AgentCommand.objects.select_related('router__business').filter(pk=cid, kind='inventory_piece').first()
    if not cmd:
        return HttpResponse(status=404)
    if not hmac.compare_digest(link.nonce(cmd), str(request.GET.get('n', ''))):
        return HttpResponse(status=403)
    if cmd.status in ('failed', 'expired', 'cancelled') or cmd.expires_at < timezone.now():
        return HttpResponse(status=410)
    try:
        payload = json.loads((request.body or b'[]').decode('utf-8'))
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            raise ValueError('JSON array required')
        from .agent_inventory import receive_inventory_chunk
        result = receive_inventory_chunk(cmd, payload, part=part, final=request.GET.get('final') == '1')
        return JsonResponse({'ok': True, **result})
    except Exception as exc:
        return JsonResponse({'ok': False, 'error': str(exc)[:300]}, status=400)


# ─────────────────────────── owner pages ───────────────────────────
@login_required
@require_POST
def router_agent_register(request):
    """Create a router that is agent-only: no local/private IP is required."""
    name = (request.POST.get('router_name') or '').strip()[:120]
    if not name:
        messages.error(request, 'Enter a router name.')
        return redirect('routers')
    router = Router.objects.create(
        business=_b(request),
        name=name,
        ip_address='',
        username='',
        password='',
        api_port=8728,
        use_ssl=False,
        connection_mode='agent',
        status='Waiting for TapTap Link',
        last_error='',
    )
    token, _agent = link.new_token(router)
    request.session[f'link_token_{router.pk}'] = token
    try:
        from .tasks import enqueue_router_sync
        enqueue_router_sync(router, request.user)
    except Exception as exc:
        messages.warning(request, f'Router created, but initial inventory sync could not be queued yet: {exc}')
    log(router.business, 'TapTap Link', f'{router.name}: registered as an agent-only router')
    messages.success(request, 'Router created without an IP address. Copy the TapTap Link script into WinBox/New Terminal.')
    return redirect('router_link', pk=router.pk)


@login_required
def router_link(request, pk):
    router = get_object_or_404(_b(request).routers, pk=pk)
    try:
        agent = router.agent
    except RouterAgent.DoesNotExist:
        agent = None
    token = request.session.pop(f'link_token_{router.pk}', None)
    script = link.enrollment_script(router, token, request) if token else ''
    advanced_script = link.advanced_enrollment_script(router, token, request) if token else ''
    cmds = router.agent_commands.select_related('created_by').order_by('-created_at')[:40]
    return render(request, 'core/router_link.html', {
        'router': router,
        'agent': agent,
        'token': token,
        'script': script,
        'advanced_script': advanced_script,
        'commands': cmds,
        'site': link.base_url(request),
        'https': link.base_url(request).startswith('https://'),
    })


@login_required
@require_POST
def router_link_action(request, pk):
    router = get_object_or_404(_b(request).routers, pk=pk)
    action = request.POST.get('action')
    try:
        agent = router.agent
    except RouterAgent.DoesNotExist:
        agent = None

    if action in ('enroll', 'rotate'):
        token, agent = link.new_token(router)
        request.session[f'link_token_{router.pk}'] = token
        if action == 'enroll':
            # TapTap Link becomes authoritative. Never keep retrying a private IP
            # from the cloud after the owner has chosen agent mode.
            Router.objects.filter(pk=router.pk).update(
                connection_mode='agent', ip_address='', username='', password='', use_ssl=False,
                status='Waiting for TapTap Link', last_error='',
            )
            try:
                from .tasks import enqueue_router_sync
                enqueue_router_sync(router, request.user)
            except Exception as exc:
                messages.warning(request, f'Link created, but the initial full sync could not be queued yet: {exc}')
        log(router.business, 'TapTap Link', f'{router.name}: token {"created" if action == "enroll" else "rotated"}')
        messages.success(request, 'New setup script ready — paste the whole block into the router terminal. The token is shown only now.')

    elif action == 'revoke' and agent:
        agent.revoked = True
        agent.save(update_fields=['revoked'])
        router.agent_commands.filter(status__in=['queued', 'sent']).update(status='cancelled', done_at=timezone.now())
        Router.objects.filter(pk=router.pk).update(status='Not connected', last_error='TapTap Link revoked')
        log(router.business, 'TapTap Link', f'{router.name}: token revoked')
        messages.success(request, 'TapTap Link revoked. The router can no longer connect. Remove the taptap-link script/scheduler or rotate the token to reconnect.')

    elif action == 'settings' and agent:
        try:
            agent.poll_seconds = max(10, min(120, int(request.POST.get('poll_seconds') or 10)))
        except ValueError:
            pass
        ip = request.POST.get('pinned_ip', '').strip()
        agent.pinned_ip = ip or None
        agent.allow_scripts = request.POST.get('allow_scripts') == 'on'
        agent.save(update_fields=['poll_seconds', 'pinned_ip', 'allow_scripts'])
        messages.success(request, 'Saved. Rotate the token and reinstall the generated script if you changed the check-in interval.')

    elif action == 'mode':
        requested = request.POST.get('mode')
        if requested == 'api':
            if not router.ip_address or not router.username:
                messages.error(request, 'Direct API credentials were removed when TapTap Link became authoritative. Add the router again as Direct API if you want to switch transport.')
            else:
                Router.objects.filter(pk=router.pk).update(connection_mode='api')
                messages.success(request, 'This router will use the direct RouterOS API.')
        elif requested == 'agent' and agent and not agent.revoked:
            Router.objects.filter(pk=router.pk).update(connection_mode='agent', ip_address='', username='', password='', use_ssl=False)
            messages.success(request, 'This router now uses TapTap Link only.')

    elif action in ('ping', 'reboot', 'backup', 'script', 'cancel'):
        if not agent or agent.revoked:
            messages.error(request, 'Set up TapTap Link first.')
            return redirect('router_link', pk=pk)
        try:
            if action == 'cancel':
                router.agent_commands.filter(pk=request.POST.get('cmd'), status='queued').update(status='cancelled', done_at=timezone.now())
                messages.success(request, 'Command cancelled.')
            elif action == 'ping':
                link.queue(router, 'ping', label='Test connection', user=request.user)
                messages.success(request, 'Test queued — it shows as Done within one check-in.')
            elif action == 'reboot':
                if request.POST.get('confirm', '').strip() != router.name:
                    messages.error(request, f'Type the router name “{router.name}” to confirm.')
                    return redirect('router_link', pk=pk)
                link.queue(router, 'reboot', label='Reboot router', user=request.user, minutes=5)
                messages.success(request, 'Reboot queued. It runs at the next check-in (only if within 5 minutes).')
            elif action == 'backup':
                f = f'taptap-{router.name}-{timezone.localtime():%Y%m%d-%H%M}'.replace(' ', '-')[:60]
                link.queue(router, 'backup', {'file': f}, label='Back up configuration', user=request.user)
                messages.success(request, 'Backup queued.')
            elif action == 'script':
                link.queue(router, 'script', {'source': request.POST.get('source', '').strip()}, label='Custom script', user=request.user, minutes=10)
                messages.success(request, 'Script queued.')
        except ValueError as exc:
            messages.error(request, str(exc))
    return redirect('router_link', pk=pk)


@login_required
def router_test(request, pk):
    """Use the Link for agent routers; never fall back to their old local IP."""
    router = get_object_or_404(_b(request).routers, pk=pk)
    if router.connection_mode != 'agent':
        from . import views as direct_views
        return direct_views.router_test(request, pk)
    try:
        agent = router.agent
    except RouterAgent.DoesNotExist:
        messages.error(request, 'TapTap Link has not been created for this router.')
        return redirect('routers')
    if agent.revoked:
        messages.error(request, 'TapTap Link is revoked. Rotate/reinstall the token first.')
        return redirect('routers')
    link.queue(router, 'ping', label='Router connection test', user=request.user)
    if agent.online:
        messages.success(request, f'{router.name} is checking in through TapTap Link. A test command has been queued.')
    else:
        messages.warning(request, f'{router.name} has not checked in recently. The test will run when TapTap Link reconnects.')
    return redirect('routers')


@login_required
def router_link_status(request, pk):
    router = get_object_or_404(_b(request).routers, pk=pk)
    try:
        a = router.agent
    except RouterAgent.DoesNotExist:
        return JsonResponse({'enrolled': False})
    cmds = [
        {'id': c.id, 'label': c.label, 'status': c.status, 'status_label': c.get_status_display(), 'result': c.result,
         'at': c.created_at.isoformat()}
        for c in router.agent_commands.order_by('-created_at')[:15]
    ]
    return JsonResponse({
        'enrolled': bool(a.enrolled_at), 'online': a.online, 'revoked': a.revoked,
        'last_seen': a.last_seen_at.isoformat() if a.last_seen_at else None,
        'ip': a.last_ip, 'identity': a.identity, 'version': a.ros_version, 'board': a.board,
        'uptime': a.uptime, 'cpu': a.cpu_load, 'mem_free': a.memory_free, 'mem_total': a.memory_total,
        'sessions': a.active_sessions, 'polls': a.polls, 'commands': cmds,
    })


# ─────────────────────────── notifications ───────────────────────────
@login_required
def notifications(request):
    business = _b(request)
    s = prefs(business)
    if request.method == 'POST':
        s.enabled = request.POST.get('enabled') == 'on'
        s.daily_summary = request.POST.get('daily_summary') == 'on'
        try:
            s.summary_hour = max(0, min(23, int(request.POST.get('summary_hour') or 8)))
        except ValueError:
            pass
        s.extra_recipients = ', '.join(x.strip() for x in request.POST.get('extra_recipients', '').replace(';', ',').split(',') if '@' in x)[:500]

        def t(v):
            try:
                h, m = (v or '').split(':')[:2]
                return dtime(int(h), int(m))
            except (ValueError, TypeError):
                return None

        s.quiet_start, s.quiet_end = t(request.POST.get('quiet_start')), t(request.POST.get('quiet_end'))
        s.events = {k: request.POST.get(f'ev_{k}') for k in EVENTS if request.POST.get(f'ev_{k}') in ('instant', 'digest', 'off')}
        s.save()
        email = request.POST.get('business_email', '').strip()
        if email != business.email:
            business.email = email if '@' in email else ''
            business.save(update_fields=['email'])
        messages.success(request, 'Notification settings saved.')
        return redirect('notifications')
    groups = {}
    for key, (label, help_, default, sev, group) in EVENTS.items():
        groups.setdefault(group, []).append({'key': key, 'label': label, 'help': help_, 'mode': (s.events or {}).get(key) or default, 'severity': sev})
    return render(request, 'core/notifications.html', {
        's': s, 'groups': groups, 'to': recipients(business, s), 'configured': email_configured(),
        'recent': business.notifications.all()[:30], 'hours': range(24),
    })


@login_required
@require_POST
def notifications_test(request):
    try:
        to = send_test(_b(request))
        messages.success(request, f'Test email sent to {to}.')
    except Exception as exc:
        messages.error(request, f'Could not send the test email: {exc}')
    return redirect('notifications')


def notifications_off(request, token):
    """One-click 'turn off all emails' link from the email footer (no login needed)."""
    s = NotificationSettings.objects.filter(unsubscribe_token=token).select_related('business').first() if len(token) >= 20 else None
    if not s:
        return HttpResponse('This link is not valid any more.', status=404, content_type='text/plain')
    if request.method == 'POST':
        s.enabled = False
        s.save(update_fields=['enabled'])
        return HttpResponse(f'Email notifications for {s.business.business_name} are off. Turn them back on under Notifications in TapTap.', content_type='text/plain')
    return render(request, 'core/notifications_off.html', {'s': s})
