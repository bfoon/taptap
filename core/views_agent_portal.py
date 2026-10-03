"""Public voucher checker for agents (/ag/<secret>/) — opened by scanning the agent's QR code."""
from django.core.cache import cache
from django.http import Http404
from django.shortcuts import render
from django.views.decorators.http import require_POST

from . import agent_portal as ap


def _agent(token):
    from .models import Agent
    a = Agent.objects.filter(portal_token=token, active=True).select_related('business').first() if token and len(token) >= 16 else None
    if not a or not a.business.has_access:
        raise Http404
    return a


def _ip(request):
    return (request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR', ''))


def _ctx(agent, v=None, code='', **extra):
    colour, key, words = ap.verdict(v) if (v is not None or code) else ('', '', '')
    from .voucher_history import ends_at
    return {'agent': agent, 'business': agent.business, 'code': code, 'v': v, 'colour': colour, 'key': key, 'words': words,
            'phone': agent.help_number(), 'ends': ends_at(v) if v is not None and v.used_at else None, **extra}


def agent_portal(request, token):
    agent = _agent(token)
    code = (request.GET.get('code') or '').strip()[:300]
    if not code:
        return render(request, 'core/agent_portal.html', _ctx(agent))
    if not ap.allowed(agent, _ip(request)):
        return render(request, 'core/agent_portal.html', _ctx(agent, code='', limited=True), status=429)
    v = ap.find_voucher(agent.business, code)
    if v is not None and cache.add(f'ap:seen:{agent.pk}:{v.pk}', 1, 3600):
        from .voucher_history import record
        record(v, 'note', text=f'Checked by agent {agent.name} on the voucher checker')
    return render(request, 'core/agent_portal.html', _ctx(agent, v, (v.code if v else code.upper()[:24])))


@require_POST
def agent_portal_help(request, token):
    agent = _agent(token)
    v = ap.find_voucher(agent.business, request.POST.get('code', ''))
    sent = False
    if v is not None and ap.verdict(v)[1] == 'unused':
        if cache.add(f'ap:help:{agent.pk}:{v.pk}', 1, 600):      # once per voucher every 10 minutes
            ap.ask_for_help(agent, v, request)
        sent = True
    return render(request, 'core/agent_portal.html', _ctx(agent, v, v.code if v else '', help_sent=sent))
