"""Business alerts — rules about stock, sales, routers and customers.

Each rule is checked every live-sync cycle (and by the page heartbeat when no scheduler runs).
It *fires* when its condition becomes true: a bell alert (with sound and a desktop pop-up if
chosen) and optionally an email through your notification settings. It stays quiet while the
condition remains true, unless "remind every N hours" is set, and re-arms once the condition
is false again — so "stock low" fires once, and "restocked" fires when stock comes back.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Sum
from django.utils import timezone

from .models_events import EventAlert, EventRule

logger = logging.getLogger('taptap.alerts')

CHECK_EVERY = 60   # seconds between checks of one business


def _int(v, default=0, lo=0, hi=10 ** 9):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _dec(v):
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal('0')


def plan_names(params):
    """The plans a rule watches: a list of names, or [] for all plans ('plan' = rules saved before)."""
    names = params.get('plans')
    if names is None and params.get('plan'):
        names = [params['plan']]
    return [str(n) for n in (names or []) if str(n).strip()]


def plans_text(params, empty='all plans'):
    names = plan_names(params)
    if not names:
        return empty
    return ', '.join(names[:3]) + (f' +{len(names) - 3}' if len(names) > 3 else '')


def stock_qs(business, params):
    qs = business.vouchers.filter(status='active', sold_at__isnull=True, used_at__isnull=True, frozen_at__isnull=True,
                                  agent__isnull=True).exclude(login_type='member')
    if plan_names(params):
        qs = qs.filter(plan_name__in=plan_names(params))
    if params.get('router'):
        qs = qs.filter(router_id=_int(params['router']))
    return qs


def _where(business, params):
    bits = []
    if plan_names(params):
        bits.append(plans_text(params))
    if params.get('router'):
        r = business.routers.filter(pk=_int(params['router'])).first()
        if r:
            bits.append(f'on {r.name}')
    return ' '.join(bits) or 'all plans'


# Each check returns (condition_true, value_text, title, body, link)

def _stock(business, rule, now, restock=False):
    p = rule.params
    n = stock_qs(business, p).count()
    th = _int(p.get('threshold'), 10)
    where = _where(business, p)
    low = n < th
    if restock:
        # true while stock is back at/above the level after having been low (re-arms when it goes low again)
        was_low = cache.get(f'tt:ev:low:{rule.pk}')
        if low:
            cache.set(f'tt:ev:low:{rule.pk}', 1, 86400 * 30)
            return False, str(n), '', '', ''
        if was_low:
            cache.delete(f'tt:ev:low:{rule.pk}')
            return True, str(n), f'Restocked: {where}', f'{n} voucher{"s" if n != 1 else ""} in stock again (level {th}).', '/vouchers/?state=stock'
        return False, str(n), '', '', ''
    return low, str(n), f'Voucher stock low: {where}', f'Only {n} unsold voucher{"s" if n != 1 else ""} left (alert below {th}). Generate more.', '/vouchers/generate/'


def _no_sales(business, rule, now):
    p = rule.params
    hours = _int(p.get('hours'), 3, 1, 168)
    h_from, h_to = p.get('open_from'), p.get('open_to')
    hr = timezone.localtime(now).hour
    if h_from not in (None, '') and h_to not in (None, ''):
        a, b = _int(h_from, 0, 0, 23), _int(h_to, 23, 0, 24)
        if not (a <= hr < b if a <= b else (hr >= a or hr < b)):
            return False, '', '', '', ''        # outside opening hours: no alarm
    last = business.sales.order_by('-sold_at').values_list('sold_at', flat=True).first()
    quiet = not last or now - last >= timedelta(hours=hours)
    since = f'since {timezone.localtime(last):%d %b %H:%M}' if last else 'yet'
    return quiet, last.isoformat() if last else '', f'No sales for {hours} h', f'No voucher sold {since}. Check your routers, agents and stock.', '/sales/today/'


def _daily_revenue(business, rule, now):
    amount = _dec(rule.params.get('amount') or 0)
    start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    total = business.sales.filter(sold_at__gte=start).aggregate(v=Sum('amount'))['v'] or Decimal('0')
    day = f'{start:%Y-%m-%d}'
    hit = amount > 0 and total >= amount
    if hit and rule.last_value == day:
        return True, day, '', '', ''           # already celebrated today
    return hit, day if hit else '', f'Sales target reached: {business.currency}{total:,.2f} today', \
        f'Today’s sales passed {business.currency}{amount:,.2f}. 🎉', '/sales/today/'


def _router_offline(business, rule, now):
    minutes = _int(rule.params.get('minutes'), 5, 0, 1440)
    down = []
    for r in business.routers.all():
        if r.status == 'Online':
            continue
        seen = r.last_watch_at
        if not seen or now - seen >= timedelta(minutes=minutes):
            down.append(r.name)
    names = ', '.join(sorted(down))
    return bool(down), names, f'Router offline: {names}', f'{len(down)} router{"s" if len(down) != 1 else ""} not reachable for {minutes}+ min. Customers may be offline.', '/routers/'


def _online_high(business, rule, now):
    th = _int(rule.params.get('threshold'), 50, 1)
    n = 0
    for rid in business.routers.values_list('id', flat=True):
        snap = cache.get(f'tt:tr:users:{rid}') or {}
        n += len([u for u in (snap.get('users') or {}) if not str(u).upper().startswith('BYPASS:')])
    return n >= th, str(n), f'{n} customers online', f'{n} customers are online at once (alert at {th}). Check your bandwidth.', '/traffic/'


def agent_ids(params):
    """The agents a rule watches: a set of ids, or None for every agent.
    New rules store 'agents' (a list); rules saved before keep their single 'agent'."""
    ids = params.get('agents')
    if ids is None and params.get('agent'):
        ids = [params['agent']]
    ids = {_int(x) for x in (ids or []) if _int(x)}
    return ids or None


def _agent_debt(business, rule, now):
    from .finance import agent_balances
    amount = _dec(rule.params.get('amount') or 0)
    ids = agent_ids(rule.params)
    over = [r for r in agent_balances(business) if amount > 0 and r['outstanding'] >= amount and (ids is None or r['agent'].pk in ids)]
    names = ', '.join(f'{r["agent"].name} ({business.currency}{r["outstanding"]:,.0f})' for r in over[:5])
    return bool(over), names, f'Agent debt above {business.currency}{amount:,.0f}', f'{names} — collect the cash.', '/finance/?tab=agents'


def _agents(business, params):
    qs = business.agents.filter(active=True) if hasattr(business.agents.model, 'active') else business.agents.all()
    ids = agent_ids(params)
    if ids is not None:
        qs = qs.filter(pk__in=ids)
    return list(qs.order_by('name'))


def _agent_stock_low(business, rule, now):
    """Agents holding fewer unsold vouchers than the level (optionally of one plan)."""
    p = rule.params
    th = _int(p.get('threshold'), 10)
    low = []
    for a in _agents(business, p):
        qs = business.vouchers.filter(agent=a, status='active', sold_at__isnull=True, used_at__isnull=True, frozen_at__isnull=True)
        if plan_names(p):
            qs = qs.filter(plan_name__in=plan_names(p))
        n = qs.count()
        if n < th:
            low.append((a, n))
    names = ', '.join(f'{a.name} ({n})' for a, n in low[:6]) + ('…' if len(low) > 6 else '')
    what = plans_text(p, 'vouchers')
    return bool(low), ','.join(str(a.pk) for a, _ in low)[:120], \
        (f'{low[0][0].name} is running out of {what}' if len(low) == 1 else f'{len(low)} agents running out of {what}'), \
        f'{names} — fewer than {th} left. Issue a new batch to them.', '/vouchers/generate/'


def _agent_collection_due(business, rule, now):
    """Agents who owe money and have not handed in cash for N days."""
    from .finance import agent_balances
    p = rule.params
    days = _int(p.get('days'), 7, 1, 365)
    floor = _dec(p.get('amount') or 0)
    ids = {a.pk for a in _agents(business, p)}
    due = []
    for r in agent_balances(business):
        a = r['agent']
        if a.pk not in ids or r['outstanding'] <= 0 or r['outstanding'] < floor:
            continue
        last = r['last_collection']
        if not last or now - last >= timedelta(days=days):
            due.append((a, r['outstanding'], last))
    c = business.currency
    names = ', '.join(f'{a.name} owes {c}{o:,.0f}' + (f' (last {timezone.localtime(l):%d %b})' if l else ' (never paid in)') for a, o, l in due[:5])
    return bool(due), ','.join(str(a.pk) for a, _, _ in due)[:120], \
        (f'Collect cash from {due[0][0].name}' if len(due) == 1 else f'Collect cash from {len(due)} agents'), \
        f'{names} — no hand-in for {days}+ days.', '/finance/?tab=agents'


def _agent_collected(business, rule, now):
    """New cash hand-ins since the last check (one alert per hand-in)."""
    from .models import CashCollection
    p = rule.params
    floor = _dec(p.get('amount') or 0)
    last_id = _int(str(rule.last_value).split('#')[0], 0)
    qs = CashCollection.objects.filter(business=business, pk__gt=last_id).select_related('agent').order_by('pk')
    ids = agent_ids(p)
    if ids is not None:
        qs = qs.filter(agent_id__in=ids)
    rows = list(qs[:20])
    top = CashCollection.objects.filter(business=business).order_by('-pk').values_list('pk', flat=True).first() or 0
    if rule.last_value == '':           # first check: start from now, don't replay old hand-ins
        return False, str(top), '', '', ''
    hits = [c for c in rows if c.amount >= floor]
    if not hits:
        return False, str(max(top, last_id)), '', '', ''
    cur = business.currency
    total = sum(c.amount for c in hits)
    title = (f'{hits[0].agent.name} handed in {cur}{hits[0].amount:,.2f}' if len(hits) == 1
             else f'{len(hits)} cash hand-ins: {cur}{total:,.2f}')
    body = '; '.join(f'{c.agent.name} {cur}{c.amount:,.2f} {c.get_payment_method_display()}' for c in hits[:5])
    return True, f'{max(top, last_id)}#new', title, body, '/finance/?tab=agents'


def _members_expiring(business, rule, now):
    from .voucher_history import ends_at
    hours = _int(rule.params.get('hours'), 24, 1, 720)
    soon = []
    for v in business.vouchers.filter(login_type='member', status='active', frozen_at__isnull=True)[:3000]:
        end = ends_at(v)
        if end and now < end <= now + timedelta(hours=hours):
            soon.append(v.code)
    codes = ', '.join(soon[:8]) + ('…' if len(soon) > 8 else '')
    return bool(soon), ','.join(sorted(soon))[:120], f'{len(soon)} member{"s" if len(soon) != 1 else ""} expiring within {hours} h', \
        f'{codes} — remind them to renew.', '/members/'


def _fup_slowed(business, rule, now):
    from .models_fup import FairUsageState
    th = _int(rule.params.get('threshold'), 5, 1)
    n = FairUsageState.objects.filter(policy__business=business, policy__active=True, tier__gt=0, updated_at__gte=now - timedelta(minutes=30)).count()
    return n >= th, str(n), f'{n} customers slowed by fair usage', f'{n} customers are on reduced speed (alert at {th}).', '/security/fair-usage/slowed/'


CHECKS = {'stock_low': lambda b, r, n: _stock(b, r, n), 'stock_restocked': lambda b, r, n: _stock(b, r, n, restock=True),
          'no_sales': _no_sales, 'daily_revenue': _daily_revenue, 'router_offline': _router_offline, 'online_high': _online_high,
          'agent_debt': _agent_debt, 'members_expiring': _members_expiring, 'fup_slowed': _fup_slowed,
          'agent_stock_low': _agent_stock_low, 'agent_collection_due': _agent_collection_due, 'agent_collected': _agent_collected}


def quiet_now(rule, now):
    if rule.quiet_from is None or rule.quiet_to is None:
        return False
    h = timezone.localtime(now).hour
    a, b = rule.quiet_from, rule.quiet_to
    return a <= h < b if a <= b else (h >= a or h < b)


def fire(rule, title, body, link, now, test=False):
    quiet = quiet_now(rule, now)
    a = EventAlert.objects.create(business=rule.business, rule=None if test else rule, kind=rule.kind, level=rule.level,
                                  title=(('Test: ' if test else '') + title)[:160], body=body[:400], link=link[:200],
                                  sound=rule.sound and not quiet, desktop=rule.desktop and not quiet,
                                  read_at=None if rule.bell else now)
    if rule.email and not test:
        try:
            from .notify import notify
            notify(rule.business, 'rule_alert', title, body, link=link, key=f'rule:{rule.pk}:{now:%Y%m%d%H%M}')
        except Exception:
            logger.exception('alert email for rule %s', rule.pk)
    return a


def evaluate(business, now=None, force=False):
    """Check every enabled rule of a business. Returns the number of alerts fired."""
    now = now or timezone.now()
    if not force and not cache.add(f'tt:ev:check:{business.pk}', 1, CHECK_EVERY):
        return 0
    fired = 0
    for rule in business.event_rules.filter(enabled=True):
        check = CHECKS.get(rule.kind)
        if not check:
            continue
        try:
            ok, value, title, body, link = check(business, rule, now)
        except Exception:
            logger.exception('alert rule %s', rule.pk)
            continue
        fields = ['last_checked_at']
        rule.last_checked_at = now
        if rule.kind == 'agent_collected':
            if ok and title:
                fire(rule, title, body, link, now); rule.last_fired_at = now; fields.append('last_fired_at'); fired += 1
            rule.last_value = value.split('#')[0][:120]; rule.firing = False
            rule.save(update_fields=list(dict.fromkeys(fields + ['last_value', 'firing'])))
            continue
        if ok:
            changed = rule.kind in ('router_offline', 'members_expiring', 'agent_stock_low', 'agent_collection_due') and value and \
                (not rule.last_value or not set(value.split(',')) <= set(rule.last_value.split(',')))
            due = rule.repeat_hours and rule.last_fired_at and now - rule.last_fired_at >= timedelta(hours=rule.repeat_hours)
            if title and (not rule.firing or changed or due):
                fire(rule, title, body, link, now)
                rule.last_fired_at = now; fields.append('last_fired_at'); fired += 1
            rule.firing = True
        else:
            rule.firing = False
        if value != rule.last_value:
            rule.last_value = value[:120]; fields.append('last_value')
        fields.append('firing')
        rule.save(update_fields=list(dict.fromkeys(fields)))
    return fired


def unread(business, since_id=0):
    """Business alerts for the bell (merged with device alerts by the heartbeat)."""
    qs = business.event_alerts.filter(read_at__isnull=True)
    fresh = [{'id': a.id, 'type': 'event', 'level': a.level, 'title': a.title, 'body': a.body, 'link': a.link,
              'sound': a.sound, 'desktop': a.desktop, 'at': a.created_at.isoformat()}
             for a in qs.filter(id__gt=since_id).order_by('-id')[:8]]
    return {'unread': qs.count(), 'fresh': fresh, 'last_id': qs.order_by('-id').values_list('id', flat=True).first() or since_id}


PRESETS = [
    # kind, name, params, level, repeat
    ('stock_low', 'Stock below 20 vouchers', {'threshold': 20}, 'warning', 6),
    ('stock_restocked', 'Stock is back', {'threshold': 20}, 'info', 0),
    ('router_offline', 'Router offline 5 min', {'minutes': 5}, 'danger', 1),
    ('no_sales', 'No sale for 3 hours (08:00–22:00)', {'hours': 3, 'open_from': 8, 'open_to': 22}, 'warning', 0),
    ('agent_stock_low', 'Agent has fewer than 10 vouchers', {'threshold': 10}, 'warning', 12),
    ('agent_collection_due', 'Agent cash not handed in for 7 days', {'days': 7}, 'warning', 24),
    ('agent_collected', 'Agent handed in cash', {}, 'info', 0),
]
