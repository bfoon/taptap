from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def initials(name):
    parts = [p for p in str(name or '').replace('@', ' ').split() if p[:1].isalnum()]
    return (''.join(p[0] for p in parts[:2]) or '?').upper()


@register.filter
def heat(value, peak):
    """0 (nothing) … 4 (the busiest) for a heat-map cell."""
    try:
        v, p = Decimal(str(value or 0)), Decimal(str(peak or 0))
    except Exception:
        return 0
    if v <= 0 or p <= 0:
        return 0
    return max(1, min(4, int((v / p) * 4 + Decimal('0.999'))))
