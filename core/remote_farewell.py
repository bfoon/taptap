"""Close the last remote-admin paths left by the removed "Other routers & APs" module.

That module opened short-lived NAT rules on a MikroTik (comment ``TT-REMOTE <token>``) to reach a device's admin page,
and closed them when the session ended. When the module was removed (migration 0136), every session still open was
written here. Live housekeeping removes each rule — over TapTap Link (a queued ``remote_close`` command) or the
API — and forgets the row; a router that cannot be reached is tried again later (up to ``MAX_TRIES``).
Nothing is left on any router, and once this table is empty the check costs one tiny query.
"""
from __future__ import annotations

import logging

logger = logging.getLogger('taptap.remote')

MAX_TRIES = 50


def _close(router, comment):
    from .voucher_history import channel
    if channel(router) == 'TapTap Link':
        from .linkops import send
        send(router, 'remote_close', {'comment': comment}, label='Close a remote admin page', minutes=60 * 24)
        return
    from .mikrotik import MikroTikService
    with MikroTikService(router) as svc:
        nat = svc.resource('/ip/firewall/nat')
        for r in nat.get():
            if str(r.get('comment', '')) == comment and r.get('id'):
                nat.remove(id=r['id'])


def process(limit=20):
    from .models_farewell import RemoteFarewell
    done = 0
    for f in RemoteFarewell.objects.select_related('router').order_by('tries', 'pk')[:limit]:
        try:
            _close(f.router, f.comment)
        except Exception as exc:
            f.tries += 1
            f.last_error = str(exc)[:255]
            if f.tries >= MAX_TRIES:
                logger.warning('Remote admin rule %s on %s could not be removed: %s', f.comment, f.router, exc)
                f.delete()
            else:
                f.save(update_fields=['tries', 'last_error'])
            continue
        f.delete()
        done += 1
    return done
