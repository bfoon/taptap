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
    from .models import AgentCheck
    AgentCheck.objects.create(business=agent.business, agent=agent, entered=(v.code if v else code)[:60], voucher=v, result=ap.verdict(v)[1])
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
            from .models import AgentHelp
            AgentHelp.objects.create(business=agent.business, agent=agent, voucher=v)
            ap.ask_for_help(agent, v, request)
        sent = True
    return render(request, 'core/agent_portal.html', _ctx(agent, v, v.code if v else '', help_sent=sent))



def agent_portal_order(request, token):
    """Order vouchers: big plan cards, + / − for how many, one button. The team gets it at once."""
    agent = _agent(token)
    business = agent.business
    plans = list(agent.orderable_plans())           # only the plans you allow this agent to order
    from .models import AgentOrder
    if request.method == 'POST':
        plan = next((p for p in plans if str(p.pk) == request.POST.get('plan')), None)
        try:
            qty = max(1, min(500, int(request.POST.get('quantity') or 0)))
        except ValueError:
            qty = 0
        if not plan or not qty:
            return render(request, 'core/agent_order.html', {'agent': agent, 'business': business, 'plans': plans, 'error': True})
        if not cache.add(f'ap:order:{agent.pk}:{plan.pk}:{qty}', 1, 120):      # a double tap is one order
            o = AgentOrder.objects.filter(agent=agent).order_by('-created_at').first()
        else:
            o = AgentOrder.objects.create(business=business, agent=agent, plan=plan, plan_name=plan.name, quantity=qty,
                                          note=request.POST.get('note', '')[:255])
            ap.order_placed(agent, o)
        return render(request, 'core/agent_order.html', {'agent': agent, 'business': business, 'plans': plans, 'order': o})
    recent = AgentOrder.objects.filter(agent=agent).order_by('-created_at')[:5]
    return render(request, 'core/agent_order.html', {'agent': agent, 'business': business, 'plans': plans, 'recent': recent})
