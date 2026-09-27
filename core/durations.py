"""Plan and voucher durations.

Everything is stored in whole minutes. Plans also remember the unit the owner
chose (minutes, hours, days or months) so the form shows "3 days", not "4320".

RouterOS has no month unit, so a month is 30 days (the usual Wi-Fi voucher
convention; "1 month" on the router becomes limit-uptime 30d).
"""
import re

UNITS = [('minutes', 'Minutes'), ('hours', 'Hours'), ('days', 'Days'), ('months', 'Months')]
MINUTES_PER = {'minutes': 1, 'hours': 60, 'days': 1440, 'months': 43200}
MAX_MINUTES = 60 * 24 * 366 * 2          # two years
DEFAULT_MINUTES = 1440                   # 1 day


def to_minutes(value, unit):
    """(3, 'days') -> 4320. Raises ValueError on bad input."""
    if unit not in MINUTES_PER:
        raise ValueError('Choose minutes, hours, days or months.')
    try:
        amount = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError('Duration must be a whole number.')
    if amount < 1:
        raise ValueError('Duration must be at least 1.')
    minutes = amount * MINUTES_PER[unit]
    if minutes > MAX_MINUTES:
        raise ValueError('Duration can be at most 2 years.')
    return minutes


def best_unit(minutes):
    """Largest unit that divides the duration exactly: 4320 -> 'days', 90 -> 'minutes'."""
    minutes = int(minutes or 0)
    for unit in ('months', 'days', 'hours'):
        if minutes and minutes % MINUTES_PER[unit] == 0:
            return unit
    return 'minutes'


def split(minutes, unit=None):
    """(value, unit) for a form. Uses ``unit`` when it divides exactly."""
    minutes = int(minutes or 0)
    if unit not in MINUTES_PER or minutes % MINUTES_PER[unit]:
        unit = best_unit(minutes)
    return minutes // MINUTES_PER[unit], unit


def text(minutes):
    """Human text: '30 minutes', '1 hour 30 minutes', '3 days', '1 month', '1 week'."""
    minutes = int(minutes or 0)
    if minutes <= 0:
        return '—'
    def n(v, word):
        return f'{v} {word}{"" if v == 1 else "s"}'
    if minutes % 43200 == 0:
        return n(minutes // 43200, 'month')
    if minutes % 1440 == 0:
        days = minutes // 1440
        if days % 7 == 0 and days < 28:
            return n(days // 7, 'week')
        return n(days, 'day')
    if minutes % 60 == 0:
        hours = minutes // 60
        return n(hours, 'hour') if hours < 48 else f'{n(hours // 24, "day")} {n(hours % 24, "hour")}'
    if minutes < 60:
        return n(minutes, 'minute')
    hours, rest = divmod(minutes, 60)
    if hours >= 24:
        return f'{n(hours // 24, "day")} {n(hours % 24, "hour")} {n(rest, "minute")}'.replace(' 0 hours', '')
    return f'{n(hours, "hour")} {n(rest, "minute")}'


def short(minutes):
    """Compact text for tables: '30m', '12h', '3d', '1mo', '1h30m'."""
    minutes = int(minutes or 0)
    if minutes <= 0:
        return '—'
    if minutes % 43200 == 0:
        return f'{minutes // 43200}mo'
    d, rest = divmod(minutes, 1440)
    h, m = divmod(rest, 60)
    return ''.join(f'{v}{u}' for v, u in ((d, 'd'), (h, 'h'), (m, 'm')) if v)


def routeros(minutes):
    """RouterOS time value: 90 -> '1h30m', 43200 -> '30d'."""
    minutes = max(1, int(minutes or 0))
    d, rest = divmod(minutes, 1440)
    h, m = divmod(rest, 60)
    return ''.join(f'{v}{u}' for v, u in ((d, 'd'), (h, 'h'), (m, 'm')) if v) or '1m'


def parse_routeros(value, default=None):
    """RouterOS duration -> minutes: '1w2d3h4m', '30m', '12:30:00', '1d 02:00:00'.
    Seconds round up to the next minute. Empty / 'none' / '0' -> ``default``."""
    raw = str(value or '').strip().lower()
    if not raw or raw in {'0', '0s', 'none', 'unlimited'}:
        return default
    seconds = 0
    for number, unit in re.findall(r'(\d+)(w|d|h|m|s)(?![a-z])', raw):
        seconds += int(number) * {'w': 604800, 'd': 86400, 'h': 3600, 'm': 60, 's': 1}[unit]
    clock = re.search(r'(\d{1,2}):(\d{2}):(\d{2})', raw)
    if clock:
        h, m, s = (int(x) for x in clock.groups())
        seconds += h * 3600 + m * 60 + s
    if not seconds:
        return default
    return max(1, (seconds + 59) // 60)


def router_limit(voucher):
    """limit-uptime to put on the router for a voucher, in RouterOS form.

    Normally the plan duration. When time was added (expires_at moved past
    used_at + duration), the limit grows to match, so a later full sync never
    shrinks it back and cuts the customer off.
    """
    minutes = int(voucher.duration_minutes or DEFAULT_MINUTES)
    if voucher.expires_at and voucher.used_at:
        span = int((voucher.expires_at - voucher.used_at).total_seconds() + 59) // 60
        minutes = max(minutes, span)
    return routeros(minutes)
