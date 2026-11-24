"""TapTap plans as the owner sees them — Free, Basic, Pro and Enterprise (modules per plan: core/features.py).

    Free        vouchers only, forever free; also where a business lands when its trial or paid plan ends
    Basic       every tool except team accounts and Fit (the AI assistant); priced at PLAN_BASIC_RATIO of Pro
    Pro         everything; the existing TapTap price list (pricing.py) is the Pro price
    Enterprise  everything, plus features TapTap builds for the business (EnterpriseFeature) — agreed with TapTap

Choosing Basic or Pro creates a payment request (Subscription with ``tier``); the plan starts when TapTap confirms the
payment (subscriptions.activate_subscription). Choosing Free takes effect at once when no paid time is running.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.utils import timezone

from . import features as ft
from . import pricing

ICONS = {'free': 'bi-ticket-perforated', 'basic': 'bi-shop', 'pro': 'bi-rocket-takeoff', 'enterprise': 'bi-gem'}


def basic_ratio():
    try:
        return min(Decimal('1'), max(Decimal('0.1'), Decimal(str(getattr(settings, 'PLAN_BASIC_RATIO', '0.6')))))
    except Exception:
        return Decimal('0.6')


def _round(amount):
    """Round a derived price to a tidy number (nearest 10, or nearest 1 under 100)."""
    step = Decimal('10') if amount >= 100 else Decimal('1')
    return (amount / step).quantize(Decimal('1'), ROUND_HALF_UP) * step


def prices(business, tier):
    """The durations offered for a paid plan, with this business's country prices."""
    rows = [dict(r) for r in pricing.price_list(business) if r['offered']]
    if tier == 'basic':
        ratio = basic_ratio()
        for r in rows:
            r['amount'] = _round(r['amount'] * ratio)
            months = Decimal(r['days']) / Decimal(30)
            r['per_month'] = (r['amount'] / months).quantize(Decimal('1'), ROUND_HALF_UP) if months else r['amount']
    return rows


def price_for(business, tier, code):
    return next((r for r in prices(business, tier) if r['code'] == code), None)


def comparison():
    """Feature groups with a tick per plan — the table under the plan cards."""
    groups = []
    for g, (label, icon) in ft.GROUPS.items():
        items = [f for f in ft.FEATURES.values() if f.group == g]
        if items:
            groups.append({'label': label, 'icon': icon,
                           'rows': [{'f': f, 'in': [f.key in ft.TIERS[t][2] for t in ft.TIERS]} for f in items]})
    return groups


def cards(business):
    """One card per plan, with where this business stands."""
    current = ft.effective_tier(business)
    chosen = ft.tier_of(business)
    order = list(ft.TIERS)
    out = []
    for key, (name, line, modules, extras) in ft.TIERS.items():
        rows = prices(business, key) if key in ft.PAID_TIERS else []
        out.append({
            'key': key, 'name': name, 'line': line, 'extras': extras, 'icon': ICONS.get(key, 'bi-box'),
            'modules': len(modules), 'total': len(ft.FEATURES), 'prices': rows, 'from': min(rows, key=lambda r: r['per_month']) if rows else None,
            'current': key == current, 'chosen': key == chosen and key != current,
            'up': order.index(key) > order.index(current),
        })
    return out


def standing(business):
    """A sentence about the business's plan for the top of the Subscription page."""
    now = timezone.now()
    current, chosen = ft.effective_tier(business), ft.tier_of(business)
    name = ft.TIERS[current][0]
    if business.is_unlimited:
        return {'title': f'{name} · unlimited', 'line': 'TapTap gave this account unlimited access.', 'tier': current}
    exp = business.access_expires_at()
    if exp and exp > now:
        days = max(0, (exp - now).days + 1)
        trial = business.subscription_status != 'active'
        return {'title': f'{name} · {days} day{"s" if days != 1 else ""} left' + (' of your trial' if trial else ''),
                'line': f'When it ends on {timezone.localtime(exp):%d %b %Y} you move to Free: vouchers keep working, the rest locks until you choose a plan.'
                if ft.free_fallback() else f'It ends on {timezone.localtime(exp):%d %b %Y}.', 'tier': current, 'days': days}
    if current == 'free':
        was = '' if chosen == 'free' else f'Your {ft.TIERS[chosen][0]} plan has ended. '
        return {'title': 'Free plan', 'line': was + 'Vouchers, batches, plans and routers keep working. Choose a plan to unlock the rest.',
                'tier': current}
    return {'title': f'{name} · ended', 'line': 'Choose a plan to restore access.', 'tier': current}


def choose_free(business, user=None):
    """Move to Free now (only when no paid time is running). Returns (ok, message)."""
    if business.paid_access and ft.tier_of(business) != 'free' and not business.is_unlimited:
        exp = business.access_expires_at()
        return False, (f'Your {ft.TIERS[ft.tier_of(business)][0]} time runs until {timezone.localtime(exp):%d %b %Y}. '
                       'You move to Free by yourself when it ends — nothing to do.')
    if business.feature_tier != 'free':
        business.feature_tier = 'free'
        business.save(update_fields=['feature_tier'])
    return True, 'You are on the Free plan: vouchers, batches, plans and routers. Upgrade any time.'


def request_enterprise(business, title, details='', contact='', user=None):
    """An owner asks TapTap to build something. Returns the EnterpriseFeature."""
    from .models_plans import EnterpriseFeature
    title = (title or '').strip()[:140]
    if not title:
        raise ValueError('Say in a few words what you need.')
    item = EnterpriseFeature.objects.create(business=business, title=title, details=(details or '').strip()[:4000],
                                            contact=(contact or '').strip()[:60],
                                            requested_by=user if getattr(user, 'is_authenticated', False) else None)
    try:
        from .utils import log
        log(business, 'Enterprise request', f'Asked TapTap to build: {title}')
    except Exception:
        pass
    return item
