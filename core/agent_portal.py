"""Agent voucher checker — the page an agent opens by scanning their QR code with the phone camera.

Made for people who may not read well: one big box, one big button, and an answer in colour,
picture and sound:
  GREEN  ✓ + 🤝  not used yet — safe to sell / take back; the helping hand calls TapTap support staff
  YELLOW ✋ ✗   in use — do NOT take it back; the customer should call the business (number shown, tap to call)
  RED    ✋ END  finished — the voucher is over
Only what the agent needs is shown (no customer names, no other agents). Each agent has their own
secret link; checks are limited per link and per phone so codes cannot be guessed.
"""
from __future__ import annotations

import re

from django.core.cache import cache

CHECKS_PER_HOUR = 120
CODE_RE = re.compile(r'[A-Za-z0-9]{4,24}')


def find_voucher(business, text):
    """The voucher a typed code (or a scanned voucher QR, which may hold a whole link) refers to."""
    from .models import Voucher, VoucherCodeAlias
    raw = str(text or '').strip()
    if not raw:
        return None
    tokens = [raw.replace(' ', '').replace('-', '')] + CODE_RE.findall(raw)
    seen = []
    for t in tokens:
        t = t.upper()
        if t in seen or len(t) < 4:
            continue
        seen.append(t)
        v = Voucher.all_objects.filter(business=business, code__iexact=t).select_related('router').first()
        if v:
            return v
        a = VoucherCodeAlias.objects.filter(business=business, code__iexact=t).select_related('voucher').first()
        if a:
            return a.voucher
    return None


def verdict(v):
    """(colour, key, words) for the agent."""
    from .voucher_history import display_state, ends_at
    if v is None:
        return 'grey', 'unknown', 'We do not know this code. Check the letters and try again.'
    if v.deleted_at:
        return 'red', 'finished', 'This voucher is finished.'
    key, _ = display_state(v)
    if key in ('expired', 'archived', 'disabled'):
        return 'red', 'finished', 'This voucher is finished.'
    if key in ('used', 'warned', 'frozen'):
        return 'yellow', 'in_use', 'This voucher is being used. Do not take it back.'
    return 'green', 'unused', 'This voucher is not used yet.'


def allowed(agent, ip):
    """Rate limit per agent link and per phone address."""
    out = True
    for key in (f'ap:{agent.pk}', f'ap:ip:{ip}'):
        n = cache.get(key, 0) + 1
        cache.set(key, n, 3600)
        if n > CHECKS_PER_HOUR:
            out = False
    return out


def ask_for_help(agent, voucher, request=None):
    """The agent pressed the helping hand: tell the business's staff — bell (with sound + desktop pop-up),
    the team chat room, and the voucher's history — with a link to the voucher."""
    from .models_events import EventAlert
    from .voucher_history import record
    business = agent.business
    link = f'/vouchers/{voucher.pk}/'
    title = f'Agent {agent.name} needs help with voucher {voucher.code}'
    body = f'{agent.name}' + (f' ({agent.phone})' if agent.phone else '') + f' is asking about {voucher.code} ({voucher.plan_name}). Open the voucher and call them.'
    EventAlert.objects.create(business=business, kind='agent_help', level='warning', title=title[:160], body=body[:400], link=link, sound=True, desktop=True)
    try:
        from . import chat
        team = chat.team_thread(business)
        chat.post(team, business.user, f'🤝 {title}. Open it: {link}', page_url=link, page_title=f'Voucher {voucher.code}', system=True)
    except Exception:
        pass
    record(voucher, 'note', text=f'Agent {agent.name} asked for help from the voucher checker')
    try:
        from .notify import notify
        notify(business, 'rule_alert', title, body, link=link, key=f'agenthelp:{agent.pk}:{voucher.pk}')
    except Exception:
        pass



def order_placed(agent, order):
    """Tell the team: bell (sound + pop-up) and the team chat, with a link that opens Generate ready to fill it."""
    from .models_events import EventAlert
    business = agent.business
    link = f'/vouchers/generate/?agent={agent.pk}&plan={order.plan_id or ""}&quantity={order.quantity}&order={order.pk}'
    title = f'🛒 Order from {agent.name}: {order.quantity} × {order.plan_name}'
    body = (f'{agent.name}' + (f' ({agent.phone})' if agent.phone else '') + f' ordered {order.quantity} {order.plan_name} voucher(s).'
            + (f' Note: {order.note}' if order.note else '') + ' Open it to make the batch for them.')
    EventAlert.objects.create(business=business, kind='agent_order', level='warning', title=title[:160], body=body[:400], link=link, sound=True, desktop=True)
    try:
        from . import chat
        chat.post(chat.team_thread(business), business.user, f'{title}. Make it here: {link}', page_url=link, page_title='Make the order', system=True)
    except Exception:
        pass
    try:
        from .notify import notify
        notify(business, 'rule_alert', title, body, link=link, key=f'agentorder:{order.pk}')
    except Exception:
        pass
