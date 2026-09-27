"""One place for "this router is on TapTap Link" decisions.

Views call these instead of opening a RouterOS connection: reads come from the
latest Link inventory, writes are queued as Link commands (applied at the router's
next check-in, a few seconds later).
"""
from django.utils import timezone

from . import agent as link
from .linklive import link_state

QUEUED = 'Sent through TapTap Link — the router applies it at its next check-in (a few seconds).'


def is_link(router):
    return bool(router) and getattr(router, 'connection_mode', 'api') == 'agent'


def ensure_online(router):
    online, why = link_state(router)
    if not online:
        raise ValueError(why)


def send(router, kind, params=None, label='', user=None, minutes=None):
    """Queue a command; raises ValueError with a readable reason if the Link is not usable."""
    ensure_online(router)
    return link.queue(router, kind, params or {}, label=label, user=user, minutes=minutes)


def refresh(router, user=None):
    """Start a full Link inventory sync (the Link equivalent of reading the router again)."""
    from .tasks import enqueue_router_sync
    ensure_online(router)
    job, created = enqueue_router_sync(router, user)
    return created


def snapshot_rows(router, path):
    """Rows of one RouterOS menu from the latest Link sync (for the resource explorer)."""
    from .models import RouterConfigSnapshot
    snap = RouterConfigSnapshot.objects.filter(router=router).first()
    want = '/' + str(path or '').strip().strip('/')
    for label, sec in ((snap.sections or {}).items() if snap else []):
        if isinstance(sec, dict) and sec.get('path') == want:
            return sec.get('rows') or [], label, snap.captured_at
    return None, None, snap.captured_at if snap else None


def session_user(router, item_id):
    """The username of an active session id reported by the Link heartbeat."""
    from .linklive import sessions
    for s in sessions(router):
        if str(s.get('id', '')) == str(item_id):
            return s.get('user', '')
    return ''


# ─────────────────────────── read-only service backed by the Link sync ───────────────────────────
from .mikrotik import MikroTikError, MikroTikService  # noqa: E402
from .routeros_analysis import parse_version  # noqa: E402

READ_ONLY = 'This router is on TapTap Link: this screen can read its synced configuration, but changes are sent only through the built-in Link actions.'


class _SnapshotResource:
    def __init__(self, svc, path):
        self.svc, self.path = svc, path

    def get(self, **filters):
        rows, _, _ = snapshot_rows(self.svc.router, self.path)
        rows = [dict(r) for r in (rows or [])]
        for k, v in filters.items():
            key = k.replace('_', '-')
            rows = [r for r in rows if str(r.get(key, r.get(k, ''))) == str(v)]
        return rows

    def add(self, **kw): raise MikroTikError(READ_ONLY)
    def set(self, **kw): raise MikroTikError(READ_ONLY)
    def remove(self, **kw): raise MikroTikError(READ_ONLY)
    def call(self, *a, **kw): raise MikroTikError(READ_ONLY)


class SnapshotService(MikroTikService):
    """Serves MikroTikService reads from the latest Link inventory; never opens a connection."""

    def connect(self):
        return self

    def close(self):
        pass

    def resource(self, path):
        return _SnapshotResource(self, path)

    def ros_version(self):
        try:
            return parse_version(self.router.agent.ros_version)
        except Exception:
            return (0, 0, 0)

    def live_traffic(self, names):
        from .linklive import rates
        return rates(self.router, set(names))
