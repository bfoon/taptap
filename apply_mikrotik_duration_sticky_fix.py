#!/usr/bin/env python3
from pathlib import Path
import shutil

ROOT = Path.cwd()
AGENT = ROOT / 'core' / 'agent.py'
SYNC = ROOT / 'core' / 'sync.py'
DURATIONS = ROOT / 'core' / 'durations.py'

for p in (AGENT, SYNC, DURATIONS):
    if not p.exists():
        raise SystemExit(f'ERROR: {p} not found. Run beside manage.py.')

for p in (AGENT, SYNC, DURATIONS):
    shutil.copy2(p, p.with_name(p.name + '.bak-before-duration-sticky-fix'))

DURATIONS.write_text('import re\n\nUNITS = [(\'minutes\', \'Minutes\'), (\'hours\', \'Hours\'), (\'days\', \'Days\'), (\'months\', \'Months\'), (\'unlimited\', \'Unlimited\')]\nUNLIMITED = \'unlimited\'\nUNLIMITED_ROUTEROS = \'0s\'\nMINUTES_PER = {\'minutes\': 1, \'hours\': 60, \'days\': 1440, \'months\': 43200}\nMAX_MINUTES = 60 * 24 * 366 * 2\nDEFAULT_MINUTES = 1440\n\n\ndef to_minutes(value, unit):\n    if unit == UNLIMITED:\n        return 0\n    if unit not in MINUTES_PER:\n        raise ValueError(\'Choose minutes, hours, days, months or unlimited.\')\n    try:\n        amount = int(str(value).strip())\n    except (TypeError, ValueError):\n        raise ValueError(\'Duration must be a whole number.\')\n    if amount < 1:\n        raise ValueError(\'Duration must be at least 1.\')\n    minutes = amount * MINUTES_PER[unit]\n    if minutes > MAX_MINUTES:\n        raise ValueError(\'Duration can be at most 2 years.\')\n    return minutes\n\n\ndef best_unit(minutes):\n    minutes = int(minutes or 0)\n    for unit in (\'months\', \'days\', \'hours\'):\n        if minutes and minutes % MINUTES_PER[unit] == 0:\n            return unit\n    return \'minutes\'\n\n\ndef split(minutes, unit=None):\n    minutes = int(minutes or 0)\n    if minutes <= 0:\n        return \'\', UNLIMITED\n    if unit not in MINUTES_PER or minutes % MINUTES_PER[unit]:\n        unit = best_unit(minutes)\n    return minutes // MINUTES_PER[unit], unit\n\n\ndef text(minutes):\n    minutes = int(minutes or 0)\n    if minutes <= 0:\n        return \'Unlimited\'\n    def n(v, word):\n        return f\'{v} {word}{"" if v == 1 else "s"}\'\n    if minutes % 43200 == 0:\n        return n(minutes // 43200, \'month\')\n    if minutes % 1440 == 0:\n        days = minutes // 1440\n        if days % 7 == 0 and days < 28:\n            return n(days // 7, \'week\')\n        return n(days, \'day\')\n    if minutes % 60 == 0:\n        hours = minutes // 60\n        return n(hours, \'hour\') if hours < 48 else f\'{n(hours // 24, "day")} {n(hours % 24, "hour")}\'\n    if minutes < 60:\n        return n(minutes, \'minute\')\n    hours, rest = divmod(minutes, 60)\n    if hours >= 24:\n        return f\'{n(hours // 24, "day")} {n(hours % 24, "hour")} {n(rest, "minute")}\'.replace(\' 0 hours\', \'\')\n    return f\'{n(hours, "hour")} {n(rest, "minute")}\'\n\n\ndef short(minutes):\n    minutes = int(minutes or 0)\n    if minutes <= 0:\n        return \'∞\'\n    if minutes % 43200 == 0:\n        return f\'{minutes // 43200}mo\'\n    d, rest = divmod(minutes, 1440)\n    h, m = divmod(rest, 60)\n    return \'\'.join(f\'{v}{u}\' for v, u in ((d, \'d\'), (h, \'h\'), (m, \'m\')) if v)\n\n\ndef routeros(minutes):\n    minutes = int(minutes or 0)\n    if minutes <= 0:\n        return UNLIMITED_ROUTEROS\n    d, rest = divmod(minutes, 1440)\n    h, m = divmod(rest, 60)\n    return \'\'.join(f\'{v}{u}\' for v, u in ((d, \'d\'), (h, \'h\'), (m, \'m\')) if v) or \'1m\'\n\n\ndef parse_routeros(value, default=None):\n    raw = str(value or \'\').strip().lower()\n    if not raw or raw in {\'0\', \'0s\', \'none\', \'unlimited\'}:\n        return default\n    seconds = 0\n    for number, unit in re.findall(r\'(\\d+)(w|d|h|m|s)(?![a-z])\', raw):\n        seconds += int(number) * {\'w\': 604800, \'d\': 86400, \'h\': 3600, \'m\': 60, \'s\': 1}[unit]\n    clock = re.search(r\'(\\d{1,2}):(\\d{2}):(\\d{2})\', raw)\n    if clock:\n        h, m, s = (int(x) for x in clock.groups())\n        seconds += h * 3600 + m * 60 + s\n    if not seconds:\n        return default\n    return max(1, (seconds + 59) // 60)\n\n\ndef router_limit(voucher, plan=None):\n    # Unused TapTap vouchers follow the CURRENT plan at send time.\n    if plan is not None and not voucher.used_at and not voucher.expires_at:\n        return routeros(int(getattr(plan, \'duration_minutes\', 0) or 0))\n\n    # Once used or explicitly extended, preserve the voucher\'s own timing.\n    if not voucher.duration_minutes and not voucher.expires_at:\n        return UNLIMITED_ROUTEROS\n\n    minutes = int(voucher.duration_minutes or DEFAULT_MINUTES)\n    if voucher.expires_at and voucher.used_at:\n        span = int((voucher.expires_at - voucher.used_at).total_seconds() + 59) // 60\n        minutes = max(minutes, span)\n    return routeros(minutes)\n', encoding='utf-8')

a = AGENT.read_text(encoding='utf-8')
old = """    'binding_upsert', 'security_fix', 'bridge_port', 'wan_dhcp_nat', 'hotspot_user_extend', 'portal_install', 'portal_reset',
}"""
new = """    'binding_upsert', 'security_fix', 'bridge_port', 'wan_dhcp_nat', 'hotspot_user_extend', 'portal_install', 'portal_reset',
    'hotspot_sticky', 'hotspot_user_mac', 'hotspot_kick',
}"""
if "'hotspot_sticky', 'hotspot_user_mac', 'hotspot_kick'" not in a:
    if old not in a:
        raise SystemExit('ERROR: SAFE_KINDS marker not found in core/agent.py')
    a = a.replace(old, new, 1)

if "'lim': router_limit(v, plan)" not in a:
    if "'lim': router_limit(v)" not in a:
        raise SystemExit('ERROR: router_limit(v) marker not found in core/agent.py')
    a = a.replace("'lim': router_limit(v)", "'lim': router_limit(v, plan)", 1)

AGENT.write_text(a, encoding='utf-8')

s = SYNC.read_text(encoding='utf-8')
if 'limit_uptime=router_limit(voucher, plan),' not in s:
    if 'limit_uptime=router_limit(voucher),' not in s:
        raise SystemExit('ERROR: router_limit(voucher) marker not found in core/sync.py')
    s = s.replace('limit_uptime=router_limit(voucher),', 'limit_uptime=router_limit(voucher, plan),', 1)
SYNC.write_text(s, encoding='utf-8')

for p in (AGENT, SYNC, DURATIONS):
    compile(p.read_text(encoding='utf-8'), str(p), 'exec')

print('SUCCESS')
print('7 days -> 7d')
print('1 month -> 30d')
print('Unlimited -> 0s')
print('Sticky sessions, MAC lock and kick-device are allowed over TapTap Link')
print('No migration required')
