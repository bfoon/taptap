import re

UNITS = [('minutes', 'Minutes'), ('hours', 'Hours'), ('days', 'Days'), ('months', 'Months'), ('unlimited', 'Unlimited')]
UNLIMITED = 'unlimited'
UNLIMITED_ROUTEROS = '0s'
MINUTES_PER = {'minutes': 1, 'hours': 60, 'days': 1440, 'months': 43200}
MAX_MINUTES = 60 * 24 * 366 * 2
DEFAULT_MINUTES = 1440


def to_minutes(value, unit):
    if unit == UNLIMITED:
        return 0
    if unit not in MINUTES_PER:
        raise ValueError('Choose minutes, hours, days, months or unlimited.')
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
    minutes = int(minutes or 0)
    for unit in ('months', 'days', 'hours'):
        if minutes and minutes % MINUTES_PER[unit] == 0:
            return unit
    return 'minutes'


def split(minutes, unit=None):
    minutes = int(minutes or 0)
    if minutes <= 0:
        return '', UNLIMITED
    if unit not in MINUTES_PER or minutes % MINUTES_PER[unit]:
        unit = best_unit(minutes)
    return minutes // MINUTES_PER[unit], unit


def text(minutes):
    minutes = int(minutes or 0)
    if minutes <= 0:
        return 'Unlimited'
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
    minutes = int(minutes or 0)
    if minutes <= 0:
        return '∞'
    if minutes % 43200 == 0:
        return f'{minutes // 43200}mo'
    d, rest = divmod(minutes, 1440)
    h, m = divmod(rest, 60)
    return ''.join(f'{v}{u}' for v, u in ((d, 'd'), (h, 'h'), (m, 'm')) if v)


def routeros(minutes):
    minutes = int(minutes or 0)
    if minutes <= 0:
        return UNLIMITED_ROUTEROS
    d, rest = divmod(minutes, 1440)
    h, m = divmod(rest, 60)
    return ''.join(f'{v}{u}' for v, u in ((d, 'd'), (h, 'h'), (m, 'm')) if v) or '1m'


def parse_routeros(value, default=None):
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


def router_limit(voucher, plan=None):
    # Unused TapTap vouchers follow the CURRENT plan at send time.
    if plan is not None and not voucher.used_at and not voucher.expires_at:
        return routeros(int(getattr(plan, 'duration_minutes', 0) or 0))

    # Once used or explicitly extended, preserve the voucher's own timing.
    if not voucher.duration_minutes and not voucher.expires_at:
        return UNLIMITED_ROUTEROS

    minutes = int(voucher.duration_minutes or DEFAULT_MINUTES)
    if voucher.expires_at and voucher.used_at:
        span = int((voucher.expires_at - voucher.used_at).total_seconds() + 59) // 60
        minutes = max(minutes, span)
    return routeros(minutes)
