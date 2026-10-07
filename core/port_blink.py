"""Flash a port's lights on the real router so a technician can find it ("which port is ether3?").

RouterOS: /interface ethernet blink <port> duration=<s>. Works over the RouterOS API (direct / TapTap Tunnel) and
TapTap Link (fixed command kind "port_blink", built from a validated port name). Ethernet and SFP ports only —
Wi-Fi interfaces have no port lights to blink.
"""
from __future__ import annotations

import logging
import re
import threading

logger = logging.getLogger('taptap.port_blink')
SECONDS = 15
PORT_RE = re.compile(r'^(ether|sfp|combo|qsfp)[\w.\-+]{0,40}$', re.I)


def can_blink(name):
    return bool(PORT_RE.match(str(name or '')))


def link_body(params):
    from .agent import rs
    name, secs = str(params.get('port', '')), int(params.get('seconds') or SECONDS)
    return f'/interface ethernet blink [find where name={rs(name)}] duration={secs}s'


def _api_blink(router, name, secs):
    from .mikrotik import MikroTikService
    try:
        with MikroTikService(router) as svc:
            res = svc.resource('/interface/ethernet')
            rows = res.get(name=name)
            if not rows:
                raise ValueError(f'{name} is not an Ethernet port on {router.name}.')
            res.call('blink', {'.id': rows[0].get('id') or rows[0].get('.id'), 'duration': f'{secs}s'})
    except Exception as exc:                              # the blink runs for the whole duration: never block the page
        logger.info('blink %s on %s: %s', name, router.name, exc)


def blink(router, name, user=None, seconds=SECONDS):
    """Start blinking. Returns a message. Raises ValueError for a port that cannot blink."""
    from .voucher_history import channel
    if not can_blink(name):
        raise ValueError('Only Ethernet and SFP ports have lights to flash.')
    if not router.interfaces.filter(name=name, is_present=True).exists():
        raise ValueError(f'{router.name} has no port called {name}.')
    seconds = max(5, min(60, int(seconds)))
    if channel(router) == 'TapTap Link':
        from .linkops import send
        send(router, 'port_blink', {'port': name, 'seconds': seconds}, label=f'Flash {name}', user=user, minutes=5)
        return f'{name} will flash for {seconds} s when {router.name} next checks in (TapTap Link).'
    threading.Thread(target=_api_blink, args=(router, name, seconds), daemon=True).start()
    return f'{name} is flashing on {router.name} for {seconds} s — look for the blinking port.'
