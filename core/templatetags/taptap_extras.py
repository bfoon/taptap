from decimal import Decimal
import json

from django import template
from django.middleware.csrf import get_token
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
    """timedelta (or seconds) -> '2d 4h', '3h 20m' or '12m'."""
    try:
        secs = int(value.total_seconds()) if hasattr(value, 'total_seconds') else int(value)
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


@register.filter
def pct(value):
    """A percentage without trailing zeros: 10.0000 → 10, 9.0900 → 9.09, 9.0909 → 9.0909."""
    try: v = Decimal(str(value))
    except Exception: return value
    text = format(v.normalize(), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


@register.filter
def data_size(value):
    """Bytes as 1.2 GB / 340 MB / 12 KB."""
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return value
    for unit, size in (('TB', 1024 ** 4), ('GB', 1024 ** 3), ('MB', 1024 ** 2), ('KB', 1024)):
        if v >= size:
            n = v / size
            return f'{n:.1f} {unit}' if n < 100 else f'{n:.0f} {unit}'
    return f'{int(v)} B'


@register.filter
def get_item(d, key):
    """{{ mydict|get_item:key }}"""
    try:
        return d.get(key)
    except AttributeError:
        return None


@register.filter
def guide_md(text):
    """Help-guide text: escape, then **bold** and `code`."""
    import re
    from django.utils.html import escape
    from django.utils.safestring import mark_safe
    t = escape(str(text or ''))
    t = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', t)
    t = re.sub(r'`(.+?)`', r'<code>\1</code>', t)
    return mark_safe(t)


@register.filter
def money2(value, currency='D'):
    """Money with two decimals always, for statements and receipts: D1,136.36, D0.00, −D50.00."""
    try: v = Decimal(str(value or 0))
    except Exception: return value
    sign = '−' if v < 0 else ''
    return f'{sign}{currency or "D"}{abs(v):,.2f}'



@register.simple_tag
def voucher_link(code, css=''):
    """<a> to a voucher's page from its code (works for any code shown anywhere)."""
    from django.urls import reverse
    from django.utils.html import format_html
    code = str(code or '').strip()
    if not code or code in ('—', '-'):
        return code or '—'
    return format_html('<a class="tt-vlink {}" href="{}" title="Open voucher {}">{}</a>', css, reverse('go_voucher', args=[code]), code, code)


@register.simple_tag(takes_context=True)
def device_link(context, mac, label='', css=''):
    """<a> to a device's page from its MAC.

    On the voucher-detail locked-device list, voucher-support users also get a
    small Remove button next to the CURRENT MAC.  It posts to the existing
    reset_mac action, so the normal server-side vouchers.support permission is
    still enforced. Previous-MAC links and every other device link stay plain.
    """
    from django.urls import reverse
    from django.utils.html import format_html

    mac = str(mac or '').strip()
    if not mac:
        return label or '—'

    link = format_html(
        '<a class="tt-dlink {}" href="{}" title="Open device {}">{}</a>',
        css,
        reverse('go_device', args=[mac]),
        mac,
        label or mac,
    )

    try:
        voucher = context.get('v')
        binding = context.get('d')
        perms = context.get('tt_perms') or ()
        request = context.get('request')

        if (
            voucher is None
            or binding is None
            or request is None
            or 'vouchers.support' not in perms
            or getattr(voucher, 'deleted_at', None)
            or getattr(binding, 'voucher_id', None) != getattr(voucher, 'pk', None)
            or str(getattr(binding, 'current_mac', '') or '').strip().upper() != mac.upper()
        ):
            return link

        # Prevent this voucher-specific control appearing if future templates
        # happen to use variables named v/d around a device_link call.
        template_name = getattr(getattr(context, 'template', None), 'name', '') or ''
        if template_name and template_name != 'core/voucher_detail.html':
            return link

        from core.shared_voucher_device_control import removal_reason

        csrf = get_token(request)
        action = reverse('reset_mac', args=[voucher.pk])
        marker = removal_reason(binding.pk)
        slot = int(getattr(binding, 'slot_no', 0) or 0)
        confirm_text = (
            f'Remove only device slot {slot} ({mac})? '
            'The other devices will stay connected and this slot will become free.'
        )
        onsubmit = 'return confirm(' + json.dumps(confirm_text) + ');'

        # voucher_detail.html currently wraps the current MAC tag in <code>.
        # Close that code span, render a real inline POST form, then reopen it
        # so the template's existing closing </code> remains balanced.
        return format_html(
            '{}'
            '</code>'
            '<form method="post" action="{}" class="d-inline-block ms-2" '
            'style="vertical-align:middle" onsubmit="{}">'
            '<input type="hidden" name="csrfmiddlewaretoken" value="{}">'
            '<input type="hidden" name="reason" value="{}">'
            '<button type="submit" class="btn btn-sm btn-outline-danger py-0 px-2" '
            'title="Remove only this device and free slot {}">'
            '<i class="bi bi-x-circle"></i> Remove'
            '</button></form>'
            '<code>',
            link,
            action,
            onsubmit,
            csrf,
            marker,
            slot,
        )
    except Exception:
        # A link to the device must never fail just because the optional
        # single-device control is unavailable.
        return link



@register.filter
def duration_minutes_text(minutes):
    from core.durations import text
    try:
        return text(int(minutes)) if int(minutes) else 'no limit'
    except (TypeError, ValueError):
        return ''


@register.filter
def kvlist(value):
    """"a:A,b:B" → [('a','A'), ('b','B')] (small fixed menus in templates)."""
    return [tuple(x.split(':', 1)) for x in str(value).split(',') if ':' in x]


@register.inclusion_tag('core/partials/track_button.html', takes_context=True)
def track_button(context, kind, object_id, label=''):
    """Track button + dialog for a plan, batch or voucher (core/tracking.py)."""
    from core import tracking
    request = context.get('request')
    user = getattr(request, 'user', None)
    w = tracking.watch_of(user, kind, object_id) if user is not None else None
    return {'kind': kind, 'object_id': object_id, 'label': label, 'w': w, 'events': tracking.events_for(kind),
            'chosen': set((w.events or []) if w else []), 'request': request, 'csrf_token': context.get('csrf_token'),
            'has_email': bool(getattr(user, 'email', '')), 'item': {'plan': 'plan', 'batch': 'batch', 'voucher': 'voucher'}[kind]}


@register.simple_tag
def map_tiles():
    """The map tile settings + loader (see static/js/tt-tiles.js). Use once on pages with a Leaflet map."""
    import json
    from django.conf import settings
    from django.templatetags.static import static
    from django.utils.html import format_html
    from django.utils.safestring import mark_safe
    data = {'url': settings.MAP_TILE_URL, 'attribution': settings.MAP_TILE_ATTRIBUTION, 'maxZoom': settings.MAP_TILE_MAX_ZOOM,
            'fallbackUrl': settings.MAP_TILE_FALLBACK_URL, 'fallbackAttribution': settings.MAP_TILE_FALLBACK_ATTRIBUTION}
    payload = json.dumps(data).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return format_html('<script id="ttMapTiles" type="application/json">{}</script><script src="{}"></script>', mark_safe(payload), static('js/tt-tiles.js'))
