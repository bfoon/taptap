"""Fit for investors — the same AI engines as Fit (core/fit_ai.complete), with a sandbox of its own.

What Fit can see is decided HERE, not by the model:
* every tool takes the signed-in ``Investor`` from the session, never an id from the model;
* deals are looked up with ``investor=<that investor>``; opportunities only through ``visible_opportunities()``;
* the scope's revenue / costs are only returned for deals where the owner left ``show_basis`` on;
* nothing else in TapTap (routers, vouchers, other investors, business finance) is reachable — the tool list
  below is the whole world Fit has.
"""
from __future__ import annotations

import json
import logging

from django.core.cache import cache
from django.utils import timezone

from . import investors as svc

logger = logging.getLogger('taptap.investor_fit')
RATE = (20, 600)          # questions per window (seconds) per investor
HISTORY = 12
MAX_ROUNDS = 5


def _deal(investor, ref):
    from .models_investors import Investment
    return (Investment.objects.filter(investor=investor, reference=str(ref or '').strip().upper())
            .exclude(status__in=['rejected', 'cancelled']).select_related('business', 'opportunity').first())


def t_my_portfolio(investor, args):
    d = svc.dashboard(investor)
    return {'currency': d['currency'], 'totals': d['totals'],
            'deals': [{k: c[k] for k in ('reference', 'title', 'status_label', 'terms_label', 'principal', 'share_pct', 'earned', 'today',
                                         'repaid', 'returned', 'paid_out', 'balance', 'payback_progress', 'split_capital_pct',
                                         'split_return_pct', 'total_due', 'start_date', 'scope_label')} for c in d['deals']]}


def t_deal_detail(investor, args):
    inv = _deal(investor, args.get('reference'))
    if not inv:
        return {'error': 'No investment with that reference belongs to you.'}
    d = svc.deal_detail(inv, with_projection=False)
    last = d['series']
    recent = [{'day': day, 'earned': round(c + r, 2)} for day, c, r in zip(last['labels'][-30:], last['capital'][-30:], last['return'][-30:])]
    out = {'deal': d['deal'], 'last_30_days': recent, 'payouts': d['payouts'][:20]}
    if d['basis']:
        b = d['basis']
        out['scope_last_30_days'] = {'revenue': round(sum(b['revenue'][-30:]), 2), 'costs': round(sum(b['costs'][-30:]), 2),
                                     'profit': round(sum(b['profit'][-30:]), 2)}
    else:
        out['scope_figures'] = 'The business keeps the revenue and cost figures of this investment private.'
    return out


def t_projection(investor, args):
    inv = _deal(investor, args.get('reference'))
    if not inv:
        return {'error': 'No investment with that reference belongs to you.'}
    p = svc.project(inv, horizon=730)
    return {k: v for k, v in p.items() if k not in ('labels',)} | {
        'scenarios': {n: {k: s[k] for k in ('monthly', 'earned_12m', 'payback_date', 'payback_months', 'complete_date')}
                      for n, s in p['scenarios'].items()}}


def t_opportunities(investor, args):
    return {'opportunities': [{'reference': o.reference, 'title': o.title, 'summary': o.summary, 'status': o.get_status_display(),
                               'terms': o.get_terms_display(), 'target': float(o.target_amount), 'raised': float(svc.committed(o)),
                               'still_open': float(svc.remaining(o)), 'min_ticket': float(o.min_ticket),
                               'share_pct_for_full_target': float(o.share_pct), 'fixed_return_pct': float(o.fixed_return_pct),
                               'closes': o.closes_at.isoformat() if o.closes_at else None, 'risks': o.risks[:600]}
                              for o in svc.visible_opportunities(investor)[:10]]}


def t_estimate(investor, args):
    o = svc.visible_opportunities(investor).filter(reference=str(args.get('reference') or '').strip().upper()).first()
    if not o:
        return {'error': 'That opportunity is not available to you.'}
    amount = svc.dec(args.get('amount'))
    if not amount or amount <= 0:
        return {'error': 'Give an amount to estimate.'}
    return svc.estimate(o, amount)


TOOLS = {
    'my_portfolio': (t_my_portfolio, 'All of the investor’s own investments with live totals: earned, today, capital repaid, return, paid out, balance.', {}),
    'deal_detail': (t_deal_detail, 'One of the investor’s own investments: terms, last 30 days of earnings, payouts, and scope figures if the business shares them.',
                    {'reference': {'type': 'string', 'description': 'Investment reference, e.g. INV-ABC1234'}}),
    'projection': (t_projection, 'Projection for one of the investor’s own investments: cautious / expected / strong scenarios, payback and completion dates.',
                   {'reference': {'type': 'string'}}),
    'opportunities': (t_opportunities, 'Investment opportunities the business has opened to this investor.', {}),
    'estimate': (t_estimate, 'What-if for an opportunity: pro-rata profit share, monthly earnings, payback time and 12-month return for an amount.',
                 {'reference': {'type': 'string', 'description': 'Opportunity reference, e.g. OPP-XYZ7890'}, 'amount': {'type': 'number'}}),
}


def specs():
    return [{'type': 'function', 'function': {'name': name, 'description': desc,
                                              'parameters': {'type': 'object', 'properties': props, 'required': list(props)}}}
            for name, (_, desc, props) in TOOLS.items()]


def call(investor, name, args):
    fn = TOOLS.get(name)
    if not fn:
        return json.dumps({'error': 'Unknown tool.'})
    try:
        return json.dumps(fn[0](investor, args or {}), default=str)[:12000]
    except Exception:
        logger.exception('investor fit tool %s', name)
        return json.dumps({'error': 'That lookup failed. Answer with what you have.'})


def system_prompt(investor):
    b = investor.business
    return (
        f'You are Fit, the investment assistant of {b.business_name}, a Wi-Fi hotspot business. You are talking to the investor '
        f'{investor.name}. Today is {timezone.localdate():%A %d %B %Y}. Currency: {b.currency}.\n'
        'You can ONLY see this investor’s own investments, payouts and the opportunities offered to them, through your tools. '
        'You know nothing else about the business — no other investors, no routers, no customers, no business-wide finance — '
        'and you must say so plainly if asked. Never guess numbers: look them up.\n'
        'Explain clearly: profit share = a % of the profit of the investment’s scope; while capital is not repaid, part of each '
        'earning repays capital and the rest is return. Fixed return = capital + an agreed % paid from a share of profit. '
        'Projections are estimates from past performance, not promises; always mention the scenario range and the confidence. '
        'Do not give personal financial, tax or legal advice. Be warm, brief and concrete; use the investor’s currency.'
    )


def rate_ok(investor):
    n, window = RATE
    key = f'invfit:{investor.pk}'
    c = int(cache.get(key, 0) or 0)
    if c >= n:
        return False
    cache.set(key, c + 1, window)
    return True


def ask(investor, text):
    from . import fit_ai
    from .models_investors import InvestorFitMessage
    text = (text or '').strip()[:1500]
    if not text:
        raise ValueError('Ask a question.')
    if not investor.allow_fit:
        raise ValueError('Fit is not enabled for your account.')
    if not rate_ok(investor):
        raise ValueError('That is a lot of questions — wait a few minutes and ask again.')
    past = list(InvestorFitMessage.objects.filter(investor=investor).order_by('-created_at')[:HISTORY])[::-1]
    InvestorFitMessage.objects.create(investor=investor, role='user', content=text)
    messages = [{'role': 'system', 'content': system_prompt(investor)}]
    messages += [{'role': m.role, 'content': m.content} for m in past if m.role in ('user', 'assistant')]
    messages.append({'role': 'user', 'content': text})
    reply = ''
    for _ in range(MAX_ROUNDS):
        data = fit_ai.complete(messages, specs())
        msg = (data.get('choices') or [{}])[0].get('message') or {}
        calls = fit_ai._tool_calls(msg.get('tool_calls') or [])
        if not calls:
            reply = (msg.get('content') or '').strip()
            break
        messages.append({'role': 'assistant', 'content': msg.get('content') or '', 'tool_calls': calls})
        for c in calls:
            try:
                args = json.loads(c['function']['arguments'] or '{}')
            except ValueError:
                args = {}
            messages.append({'role': 'tool', 'tool_call_id': c['id'], 'content': call(investor, c['function']['name'], args if isinstance(args, dict) else {})})
    reply = reply or 'I could not finish that one — try asking about a single investment.'
    InvestorFitMessage.objects.create(investor=investor, role='assistant', content=reply)
    return reply
