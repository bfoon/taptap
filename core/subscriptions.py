"""Activating and extending TapTap subscriptions (used by Django admin and the platform console)."""
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

PLAN_DAYS = {'1 Month': 30, '2 Months': 60, '3 Months': 90, '6 Months': 180, '1 Year': 365}


def extend_business(business, days):
    """Push the business's paid access forward by `days` from whichever is later: now or its current expiry."""
    now = timezone.now()
    current = business.subscription_expires_at if business.subscription_status == 'active' else None
    base = max(now, current or now)
    business.subscription_status = 'active'
    business.subscription_expires_at = base + timedelta(days=days)
    business.save(update_fields=['subscription_status', 'subscription_expires_at'])
    return base, business.subscription_expires_at


def apply_tier(sub):
    """A paid subscription puts the business on the plan it bought. One without a plan (requests from before plans,
    payments the platform records by hand) keeps the business's plan — and lifts a business that was on Free to Pro,
    since paying for TapTap was always paying for everything."""
    from . import features as ft
    business = sub.business
    tier = sub.tier if sub.tier in ft.TIERS else ''
    if not tier:
        tier = ft.tier_of(business)
        if tier == 'free':
            tier = ft.DEFAULT_TIER
    if business.feature_tier != tier:
        business.feature_tier = tier
        business.save(update_fields=['feature_tier'])
    if sub.tier != tier:
        sub.tier = tier
        sub.save(update_fields=['tier'])


@transaction.atomic
def activate_subscription(sub, days=None, method=None, reference=None):
    """Mark a pending subscription as paid and give the business access. Returns False if it was already paid."""
    if sub.payment_status == 'Paid':
        return False
    days = days or PLAN_DAYS.get(sub.plan)
    if not days:
        return False
    starts, expires = extend_business(sub.business, days)
    apply_tier(sub)
    sub.payment_status = 'Paid'
    sub.starts_at, sub.expires_at = starts, expires
    if method:
        sub.payment_method = method[:60]
    if reference:
        sub.transaction_id = reference[:120]
    sub.save(update_fields=['payment_status', 'starts_at', 'expires_at', 'payment_method', 'transaction_id'])
    return True


def record_paid_subscription(business, plan, amount, days, method='Manual', reference='', tier=''):
    """Create an already-paid subscription (e.g. cash received by the platform owner) and activate it."""
    from .models import Subscription
    sub = Subscription.objects.create(business=business, plan=plan[:60], amount=Decimal(amount), payment_method=method[:60],
                                      payment_status='Pending', transaction_id=reference[:120], tier=tier or '')
    activate_subscription(sub, days=days)
    return sub
