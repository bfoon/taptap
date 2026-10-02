"""Extended TapTap Link RouterOS system-health heartbeat.

TapTap Link already reports a small heartbeat. This module extends that heartbeat
without replacing the large core/agent.py file.

It collects additional /system/resource fields and every /system/health key that
RouterOS exposes, then makes those values available through the existing
linkops.snapshot_rows() interface.

No schema migration is required. Extra data is stored in cache because the
router sends it every heartbeat.
"""
from __future__ import annotations

import logging

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('taptap.link.health')

VERSION = 6
CACHE_PREFIX = 'tt:link:system-health:'


def _cache_key(router_id):
    return f'{CACHE_PREFIX}{router_id}'


def _to_int(value, default=0):
    try:
        return int(float(str(value or '').strip()))
    except (TypeError, ValueError):
        return default


def _parse_health(raw):
    """Parse RouterOS map rows encoded as k=v;k=v; by the heartbeat."""
    out = {}
    for part in str(raw or '').split(';'):
        if not part or '=' not in part:
            continue
        key, value = part.split('=', 1)
        key = str(key or '').strip()
        if not key:
            continue
        out[key] = str(value or '').strip()
    return out


def _health_payload(agent, data):
    resource = {
        'version': str(data.get('ver', '') or ''),
        'uptime': str(data.get('up', '') or ''),
        'cpu-load': _to_int(data.get('cpu')),
        'free-memory': _to_int(data.get('mf')),
        'total-memory': _to_int(data.get('mt')),
        'board-name': str(data.get('board', '') or ''),
        'total-hdd-space': _to_int(data.get('td')),
        'free-hdd-space': _to_int(data.get('fd')),
        'cpu-count': _to_int(data.get('cc')),
        'cpu-frequency': _to_int(data.get('cf')),
        'architecture-name': str(data.get('arch', '') or ''),
        'platform': str(data.get('plat', '') or ''),
        'bad-blocks': str(data.get('bad', '') or ''),
        'write-sect-since-reboot': _to_int(data.get('ws')),
        'write-sect-total': _to_int(data.get('wt')),
        'factory-software': str(data.get('factory', '') or ''),
        'build-time': str(data.get('build', '') or ''),
    }

    resource = {
        key: value
        for key, value in resource.items()
        if value not in ('', None)
    }

    return {
        'router_id': agent.router_id,
        'at': timezone.now(),
        'resource': resource,
        'health': _parse_health(data.get('hs', '')),
    }


def _enhance_agent_script(original):
    """Wrap core.agent.agent_script by injecting extra fields into its script."""
    def enhanced(url, token, check):
        text = original(url, token, check)

        marker = '  :local board [/system resource get board-name]\n'
        if marker not in text:
            logger.warning('TapTap Link health: board marker not found in heartbeat script')
            return text

        extra = r'''  :local td 0
  :local fd 0
  :local cc 0
  :local cf 0
  :local arch ""
  :local plat ""
  :local bad ""
  :local ws 0
  :local wt 0
  :local factory ""
  :local build ""
  :local hs ""
  :do { :set td [/system resource get total-hdd-space] } on-error={}
  :do { :set fd [/system resource get free-hdd-space] } on-error={}
  :do { :set cc [/system resource get cpu-count] } on-error={}
  :do { :set cf [/system resource get cpu-frequency] } on-error={}
  :do { :set arch [/system resource get architecture-name] } on-error={}
  :do { :set plat [/system resource get platform] } on-error={}
  :do { :set bad [/system resource get bad-blocks] } on-error={}
  :do { :set ws [/system resource get write-sect-since-reboot] } on-error={}
  :do { :set wt [/system resource get write-sect-total] } on-error={}
  :do { :set factory [/system resource get factory-software] } on-error={}
  :do { :set build [/system resource get build-time] } on-error={}
  :do {
    :foreach hr in=[/system health print as-value] do={
      :foreach hk,hv in=$hr do={
        :set hs ($hs . $hk . "=" . $hv . ";")
      }
    }
  } on-error={}
'''

        text = text.replace(marker, marker + extra, 1)

        old = '"&board=" . $board . "&n="'
        new = (
            '"&board=" . $board . '
            '"&td=" . $td . "&fd=" . $fd . '
            '"&cc=" . $cc . "&cf=" . $cf . '
            '"&arch=" . $arch . "&plat=" . $plat . '
            '"&bad=" . $bad . "&ws=" . $ws . "&wt=" . $wt . '
            '"&factory=" . $factory . "&build=" . $build . '
            '"&hs=" . $hs . "&n="'
        )

        if old not in text:
            logger.warning('TapTap Link health: heartbeat body marker not found')
            return text

        return text.replace(old, new, 1)

    return enhanced


def install():
    """Install v6 heartbeat collection and Link snapshot integration."""
    from . import agent as link
    from . import linkops

    if getattr(link, '_taptap_full_health_installed', False):
        return

    original_agent_script = link.agent_script
    original_handle_poll = link.handle_poll
    original_snapshot_rows = linkops.snapshot_rows

    link.SCRIPT_VERSION = max(int(getattr(link, 'SCRIPT_VERSION', 1)), VERSION)
    link.agent_script = _enhance_agent_script(original_agent_script)

    def handle_poll(agent, data, ip, url):
        try:
            payload = _health_payload(agent, data)
            ttl = max(300, int(getattr(agent, 'poll_seconds', 10) or 10) * 30)
            cache.set(_cache_key(agent.router_id), payload, ttl)
        except Exception:
            logger.exception(
                'Could not store extended TapTap Link health for router %s',
                agent.router_id,
            )

        return original_handle_poll(agent, data, ip, url)

    def snapshot_rows(router, path):
        want = '/' + str(path or '').strip().strip('/')

        if want in ('/system/resource', '/system/health'):
            payload = cache.get(_cache_key(router.pk))
            if payload:
                if want == '/system/resource':
                    rows = [dict(payload.get('resource') or {})]
                    label = 'System resource (TapTap Link heartbeat)'
                else:
                    health = dict(payload.get('health') or {})
                    rows = [health] if health else []
                    label = 'System health (TapTap Link heartbeat)'

                return rows, label, payload.get('at') or timezone.now()

        return original_snapshot_rows(router, path)

    link.handle_poll = handle_poll
    linkops.snapshot_rows = snapshot_rows
    link._taptap_full_health_installed = True

    logger.info(
        'TapTap Link full system-health heartbeat v%s installed',
        VERSION,
    )
