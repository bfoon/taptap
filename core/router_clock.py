"""Router clock: see a MikroTik's date, time, time zone and NTP state, and correct them.

Why it matters: most MikroTik boards have no clock battery. After a power cut they start
on a date in the past until NTP corrects them, and with the wrong date HTTPS fails — the
router can no longer upload to TapTap (Link syncs, backups, probes), voucher times and
logs are wrong, and schedulers fire at the wrong moment.

Works over the RouterOS API (direct / TapTap Tunnel) and TapTap Link (fixed command
kind "clock", every value validated here). Over the Link the correct time is computed
when the command leaves TapTap (core/agent.wrap runs at the check-in), not when it was
queued, so a command that waited for the router never sets an old time.

The last reading is kept in the cache: ``tt:clock:<router id>``.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, available_timezones

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('taptap.router_clock')

CACHE_TTL = 7 * 86400
NTP_SERVERS = ('time.cloudflare.com', 'pool.ntp.org')     # fixed list: nothing typed by a user reaches the router
TZ_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}$')
MONTHS = ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec')
DRIFT_OK = 60            # seconds: closer than this counts as correct
SEND_LEAD = 1            # seconds: the router applies a Link command about a second after TapTap builds it


# ─────────────────────────────── validation ───────────────────────────────

def valid_tz(name):
    name = str(name or '').strip()
    return bool(name) and len(name) <= 64 and bool(TZ_RE.match(name)) and name in available_timezones()


def parse_manual(value):
    """'2026-10-15T14:30' or '2026-10-15T14:30:05' (router local time) → naive datetime, else None."""
    text = str(value or '').strip()
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return dt if 2020 <= dt.year <= 2099 else None
    return None


def validate_params(p):
    """Raise ValueError for anything that is not a plain clock read or correction."""
    action = p.get('action')
    if action not in ('read', 'set'):
        raise ValueError('Invalid clock action.')
    if action == 'set':
        if p.get('tz') and not valid_tz(p['tz']):
            raise ValueError('Unknown time zone.')
        if p.get('manual') and not parse_manual(p['manual']):
            raise ValueError('Invalid date and time.')
        if p.get('ntp') not in (None, True, False):
            raise ValueError('Invalid NTP setting.')
    return p


# ─────────────────────────────── time maths ───────────────────────────────

def parse_offset(value):
    """RouterOS gmt-offset: '+00:00' / '-05:30' (v7) or seconds such as '3600' (v6) → timedelta or None."""
    text = str(value or '').strip()
    m = re.fullmatch(r'([+-])?(\d{1,2}):(\d{2})', text)
    if m:
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        return -delta if m.group(1) == '-' else delta
    if re.fullmatch(r'[+-]?\d{1,6}', text):
        return timedelta(seconds=int(text))
    return None


def parse_router_datetime(date_text, time_text):
    """RouterOS date ('2026-10-15' on 7.10+, 'oct/15/2026' before) + 'HH:MM:SS' → naive datetime or None."""
    d, t = str(date_text or '').strip().lower(), str(time_text or '').strip()
    day = None
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', d):
            day = datetime.strptime(d, '%Y-%m-%d')
        else:
            m = re.fullmatch(r'([a-z]{3})/(\d{1,2})/(\d{4})', d)
            if m and m.group(1) in MONTHS:
                day = datetime(int(m.group(3)), MONTHS.index(m.group(1)) + 1, int(m.group(2)))
        tm = re.fullmatch(r'(\d{1,2}):(\d{2}):(\d{2})', t)
        if not day or not tm:
            return None
        return day.replace(hour=int(tm.group(1)), minute=int(tm.group(2)), second=int(tm.group(3)))
    except ValueError:
        return None


def zone_for(tz_name, offset):
    """The router's zone: its IANA name when TapTap knows it, else its fixed GMT offset, else UTC."""
    if valid_tz(tz_name):
        return ZoneInfo(tz_name)
    delta = parse_offset(offset)
    return dt_timezone(delta) if delta is not None else dt_timezone.utc


def correct_local(tz_name='', offset='', at=None):
    """The correct wall-clock time for the router (naive, in its own zone)."""
    now = at or timezone.now()
    return now.astimezone(zone_for(tz_name, offset)).replace(tzinfo=None)


def date_formats(dt):
    return dt.strftime('%Y-%m-%d'), f'{MONTHS[dt.month - 1]}/{dt.day:02d}/{dt.year}'


def iso_dates_first(version):
    """RouterOS 7.10 switched the clock to ISO dates. Unknown version → try ISO first."""
    from .routeros_analysis import parse_version
    v = parse_version(version)
    return v == (0, 0, 0) or v >= (7, 10, 0)


# ─────────────────────────────── stored reading ───────────────────────────────

def _key(router):
    return f'tt:clock:{router.pk}'


def load(router):
    return cache.get(_key(router)) or {}


def store(router, data):
    cache.set(_key(router), data, CACHE_TTL)
    return data


def build_reading(raw, via, at=None):
    """Turn what the router reported into what the Control Center shows."""
    at = at or timezone.now()
    tz_name = str(raw.get('tz') or '').strip()
    offset = str(raw.get('offset') or '').strip()
    router_dt = parse_router_datetime(raw.get('date'), raw.get('time'))
    correct = correct_local(tz_name, offset, at)
    drift = int(round((router_dt - correct).total_seconds())) if router_dt else None
    servers = [s for s in re.split(r'[;,\s]+', str(raw.get('servers') or '')) if s]
    ntp_enabled = str(raw.get('ntp') or '').strip().lower() in ('true', 'yes')
    if drift is None:
        verdict = 'unknown'
    elif abs(drift) <= DRIFT_OK:
        verdict = 'ok'
    elif abs(drift) <= 600:
        verdict = 'drift'
    else:
        verdict = 'wrong'
    return {
        'at': at.isoformat(), 'via': via, 'pending': False, 'error': '',
        'router_date': str(raw.get('date') or ''), 'router_time': str(raw.get('time') or ''),
        'router_local': router_dt.strftime('%Y-%m-%d %H:%M:%S') if router_dt else '',
        'correct_local': correct.strftime('%Y-%m-%d %H:%M:%S'),
        'tz': tz_name, 'tz_known': valid_tz(tz_name), 'offset': offset,
        'autodetect': str(raw.get('auto') or '').strip().lower() in ('true', 'yes'),
        'ntp_enabled': ntp_enabled, 'ntp_servers': servers, 'ntp_status': str(raw.get('status') or ''),
        'drift_seconds': drift, 'verdict': verdict,
    }


# ─────────────────────────────── TapTap Link ───────────────────────────────

def _try(code, fallback=''):
    """RouterOS: compile `code` at run time, so a property this RouterOS version lacks is a caught error."""
    from .agent import rs
    return f':do {{ :local f [:parse {rs(code)}]; $f }} on-error={{ {fallback} }}'


def _read_upload(url, check, nonce_value, cmd_id):
    from .agent import rs
    upload = rs(f'{url}/api/agent/v1/clock?c={cmd_id}&n={nonce_value}')
    return (
        ':local d [/system clock get date]; :local t [/system clock get time]; '
        ':local z ""; :local o ""; :local ad ""; :local ne ""; :local ns ""; :local st ""; '
        ':do { :set z [/system clock get time-zone-name] } on-error={}; '
        ':do { :set o [/system clock get gmt-offset] } on-error={}; '
        ':do { :set ad [[:parse ":return [/system clock get time-zone-autodetect]"]] } on-error={}; '
        ':do { :set ne [/system ntp client get enabled] } on-error={}; '
        ':do { :set ns [[:parse ":return [/system ntp client get servers]"]] } on-error={ '
        ':do { :set ns [[:parse ":return [/system ntp client get server-dns-names]"]] } on-error={} }; '
        ':do { :set st [[:parse ":return [/system ntp client get status]"]] } on-error={}; '
        f'/tool fetch url={upload} http-method=post http-header-field="Content-Type: text/plain" '
        'http-data=("date=" . $d . "\\ntime=" . $t . "\\ntz=" . $z . "\\noffset=" . [:tostr $o] . "\\nauto=" . [:tostr $ad] '
        '. "\\nntp=" . [:tostr $ne] . "\\nservers=" . [:tostr $ns] . "\\nstatus=" . [:tostr $st]) '
        f'output=none check-certificate={check} duration=15s idle-timeout=10s'
    )


def _set_lines(p, version, at=None):
    """RouterOS commands that correct the clock. Order matters: zone first, then the time in that zone."""
    from .agent import rs
    lines = []
    tz = p.get('tz') or ''
    if tz:
        lines.append(_try('/system clock set time-zone-autodetect=no'))
        lines.append(f'/system clock set time-zone-name={rs(tz)}')
    if p.get('ntp') is True:
        servers = ','.join(NTP_SERVERS)
        lines.append(_try(f'/system ntp client set enabled=yes servers="{servers}"',
                          _try(f'/system ntp client set enabled=yes mode=unicast server-dns-names="{servers}"')))
    elif p.get('ntp') is False:
        lines.append(_try('/system ntp client set enabled=no'))
    manual = parse_manual(p.get('manual'))
    if manual:
        target = manual
    else:
        last = p.get('last') or {}
        target = correct_local(tz or last.get('tz', ''), '' if tz else last.get('offset', ''), at) + timedelta(seconds=SEND_LEAD)
    iso, legacy = date_formats(target)
    first, second = (iso, legacy) if iso_dates_first(version) else (legacy, iso)
    t = target.strftime('%H:%M:%S')
    lines.append(f':do {{ /system clock set date={first} time={t} }} on-error={{ /system clock set date={second} time={t} }}')
    return lines


def link_body(cmd, url, check, nonce_value):
    """RouterOS body for a Link clock command (built when it is sent, so the time is fresh)."""
    p = validate_params(dict(cmd.params or {}))
    parts = []
    if p['action'] == 'set':
        version = ''
        try:
            version = cmd.router.agent.ros_version
        except Exception:
            pass
        parts += _set_lines(p, version)
    parts.append(_read_upload(url, check, nonce_value, cmd.pk))
    return '{ ' + '; '.join(parts) + ' }'


def parse_upload(body):
    text = (body or b'').decode('utf-8', 'replace') if isinstance(body, (bytes, bytearray)) else str(body or '')
    raw = {}
    for line in text.splitlines()[:20]:
        key, sep, value = line.partition('=')
        if sep and key in ('date', 'time', 'tz', 'offset', 'auto', 'ntp', 'servers', 'status'):
            raw[key] = value.strip()[:200]
    return raw


def receive_link(cmd, body):
    """The router's answer to a Link clock command."""
    reading = build_reading(parse_upload(body), 'TapTap Link')
    if (cmd.params or {}).get('action') == 'set':
        reading['corrected_at'] = timezone.now().isoformat()
    return store(cmd.router, reading)


# ─────────────────────────────── RouterOS API ───────────────────────────────

def _first(rows):
    return rows[0] if rows else {}


def _api_raw(svc):
    clock = _first(svc.resource('/system/clock').get())
    ntp = _first(svc.safe_get('/system/ntp/client'))
    return {
        'date': clock.get('date', ''), 'time': clock.get('time', ''),
        'tz': clock.get('time-zone-name', ''), 'offset': clock.get('gmt-offset', ''),
        'auto': clock.get('time-zone-autodetect', ''),
        'ntp': ntp.get('enabled', ''), 'servers': ntp.get('servers', '') or ntp.get('server-dns-names', ''),
        'status': ntp.get('status', ''),
    }


def api_read(router):
    from .mikrotik import MikroTikService
    with MikroTikService(router) as svc:
        return store(router, build_reading(_api_raw(svc), _via(router)))


def api_set(router, p):
    from .mikrotik import MikroTikService
    validate_params(p)
    with MikroTikService(router) as svc:
        clock = svc.resource('/system/clock')
        tz = p.get('tz') or ''
        if tz:
            try:
                clock.set(**{'time-zone-autodetect': 'no'})
            except Exception:
                pass                                   # RouterOS without zone auto-detection
            clock.set(**{'time-zone-name': tz})
        if p.get('ntp') is not None:
            ntp = svc.resource('/system/ntp/client')
            servers = ','.join(NTP_SERVERS)
            if p['ntp']:
                try:
                    ntp.set(enabled='yes', servers=servers)
                except Exception:
                    ntp.set(enabled='yes', mode='unicast', **{'server-dns-names': servers})
            else:
                ntp.set(enabled='no')
        raw = _api_raw(svc)
        manual = parse_manual(p.get('manual'))
        target = manual or correct_local(raw.get('tz', ''), raw.get('offset', ''))
        iso, legacy = date_formats(target)
        first, second = (iso, legacy) if iso_dates_first('.'.join(map(str, svc.ros_version()))) else (legacy, iso)
        t = target.strftime('%H:%M:%S')
        try:
            clock.set(date=first, time=t)
        except Exception:
            clock.set(date=second, time=t)
        reading = build_reading(_api_raw(svc), _via(router))
        reading['corrected_at'] = timezone.now().isoformat()
        return store(router, reading)


# ─────────────────────────────── entry points ───────────────────────────────

def _via(router):
    from .voucher_history import channel
    return channel(router)


def _on_link(router):
    return _via(router) == 'TapTap Link'


def _link_send(router, p, user, label):
    from .linkops import send
    cmd = send(router, 'clock', p, label=label, user=user, minutes=10)
    reading = dict(load(router))
    reading.update(pending=True, command_id=cmd.pk, requested_at=timezone.now().isoformat(), error='')
    store(router, reading)
    return reading


def read(router, user=None):
    """Read the clock now (API) or ask the router for it (Link). Returns the reading to show."""
    if not _on_link(router):
        try:
            return api_read(router)
        except Exception as exc:
            if getattr(router, 'connection_mode', 'api') != 'agent':
                raise ValueError(f'Could not read the clock of {router.name}: {exc}')
            logger.info('clock read over tunnel failed for %s, using the Link: %s', router, exc)
    return _link_send(router, {'action': 'read'}, user, 'Read the router clock')


def correct(router, user=None, tz='', manual='', ntp=None):
    """Correct the clock (and optionally the zone / NTP). Returns the reading to show."""
    p = {'action': 'set', 'tz': str(tz or '').strip(), 'manual': str(manual or '').strip(), 'ntp': ntp}
    validate_params(p)
    if not _on_link(router):
        try:
            return api_set(router, p)
        except Exception as exc:
            if getattr(router, 'connection_mode', 'api') != 'agent':
                raise ValueError(f'Could not set the clock of {router.name}: {exc}')
            logger.info('clock set over tunnel failed for %s, using the Link: %s', router, exc)
    last = load(router)
    p['last'] = {'tz': last.get('tz', ''), 'offset': last.get('offset', '')}   # the zone the router last reported
    what = 'Set the router clock' + (f' ({p["tz"]})' if p['tz'] else '') + (' and turn on NTP' if ntp is True else '')
    return _link_send(router, p, user, what)


def status(router):
    """The stored reading, with the state of a Link command still on its way."""
    from .models import AgentCommand
    reading = dict(load(router))
    cid = reading.get('command_id')
    if reading.get('pending') and cid:
        cmd = AgentCommand.objects.filter(pk=cid, router=router).only('status', 'result').first()
        if not cmd or cmd.status in ('failed', 'expired', 'cancelled'):
            reading.update(pending=False, error=(cmd.result if cmd and cmd.result else
                                                 'The router did not answer. If its date is far off, HTTPS uploads fail: '
                                                 'use the RouterOS command shown below once.'))
            store(router, reading)
        elif cmd.status == 'done' and not reading.get('router_time'):
            reading['pending'] = True               # acknowledged; its upload is a moment behind
    return reading


def manual_command(router):
    """One-time RouterOS command for a router that cannot reach TapTap because of its clock."""
    last = load(router)
    target = correct_local(last.get('tz', ''), last.get('offset', ''))
    iso, legacy = date_formats(target)
    servers = ','.join(NTP_SERVERS)
    return (f'/system clock set date={iso} time={target.strftime("%H:%M:%S")}\n'
            f'# RouterOS older than 7.10: /system clock set date={legacy} time={target.strftime("%H:%M:%S")}\n'
            f'/system ntp client set enabled=yes servers="{servers}"\n'
            f'# RouterOS 6: /system ntp client set enabled=yes mode=unicast server-dns-names="{servers}"')
