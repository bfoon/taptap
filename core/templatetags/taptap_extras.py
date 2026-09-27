from decimal import Decimal
from django import template
from django.utils.html import format_html

register = template.Library()


@register.filter
def money(value, currency='D'):
    try: v = Decimal(str(value or 0))
    except Exception: return value
    sign = '−' if v < 0 else ''
    v = abs(v)
    body = f'{v:,.0f}' if v == v.to_integral_value() else f'{v:,.2f}'
    return f'{sign}{currency or "D"}{body}'


@register.filter
def delta(value, invert=False):
    """Change-vs-previous-period pill. invert=True for costs (up is bad)."""
    if value is None:
        return format_html('<span class="delta flat">new</span>') if value is None and invert == 'new' else format_html('<span class="delta flat">—</span>')
    v = float(value)
    cls = 'up' if v > 0 else ('down' if v < 0 else 'flat')
    arrow = '▲' if v > 0 else ('▼' if v < 0 else '•')
    return format_html('<span class="delta {}{}">{} {}%</span>', cls, ' inv' if invert else '', arrow, abs(round(v, 1)))


@register.filter
def pct_of(value, total):
    try: return max(0, min(100, round(float(value) / float(total) * 100))) if float(total) else 0
    except Exception: return 0


@register.filter
def neg(value):
    try: return -value
    except Exception: return value


@register.filter
def without(querydict, key):
    """QueryDict → urlencoded string minus one key (for pagination links)."""
    q = querydict.copy()
    q.pop(key, None)
    return q.urlencode()


@register.filter
def hours_text(hours):
    """168 -> '1 week', 24 -> '1 day', 3 -> '3 hours'."""
    from core.views_studio import duration_text
    try: return duration_text(int(hours))
    except (TypeError, ValueError): return hours


@register.filter
def duration_short(value):
    """timedelta -> '2d 4h', '3h 20m' or '12m'."""
    try:
        secs = int(value.total_seconds())
    except Exception:
        return ''
    d, rest = divmod(max(0, secs), 86400)
    h, rest = divmod(rest, 3600)
    m = rest // 60
    if d:
        return f'{d}d {h}h' if h else f'{d}d'
    if h:
        return f'{h}h {m}m' if m else f'{h}h'
    return f'{m}m'


@register.filter
def minutes_text(minutes):
    """Plan/voucher minutes -> '30 minutes', '12 hours', '3 days', '1 month'."""
    from core.durations import text
    return text(minutes)


@register.filter
def minutes_short(minutes):
    """Plan/voucher minutes -> '30m', '12h', '3d', '1mo'."""
    from core.durations import short
    return short(minutes)
