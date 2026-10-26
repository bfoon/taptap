"""Redis cache that never takes a page down.

Django's RedisCache raises when Redis is slow or unreachable (TimeoutError, ConnectionError). TapTap calls
the cache in ~270 places — chat polling, live ticks, TapTap Link check-ins, voucher checks — so a Redis pause
of just over the socket timeout (a background-save fork, a restart, a stale pooled connection) turned into
"Server Error (500)" that disappeared on refresh.

This backend treats the cache as what it is — an optimisation:
    get / get_many / has_key        → behave like a cache miss
    set / set_many / touch / delete → report "not stored"
    add                             → True  (go ahead: doing the work twice beats not doing it)
    incr / decr                     → 0
Circuit breaker: after a failure this process skips Redis for BREAK_SECONDS and answers "no cache"
instantly, so a long Redis outage makes pages a little less cached — not slow. Then it tries again.

Failures are logged at most once every 30 seconds per process (visible in `docker compose logs web`).
Programming errors (bad arguments, unpicklable values) are not hidden.
"""
import functools
import logging
import os
import threading
import time

from django.core.cache.backends.base import DEFAULT_TIMEOUT
from django.core.cache.backends.redis import RedisCache

try:
    from redis.exceptions import RedisError
except Exception:  # pragma: no cover
    RedisError = Exception

logger = logging.getLogger('taptap.cache')
CACHE_ERRORS = (RedisError, ConnectionError, TimeoutError, OSError)
BREAK_SECONDS = float(os.getenv('CACHE_BREAK_SECONDS', '5'))
LOG_EVERY = 30.0

_lock = threading.Lock()
_state = {'down_until': 0.0, 'last_log': 0.0, 'suppressed': 0}


def _is_down():
    return time.monotonic() < _state['down_until']


def _failed(op, exc):
    now = time.monotonic()
    with _lock:
        _state['down_until'] = now + BREAK_SECONDS
        if now - _state['last_log'] < LOG_EVERY:
            _state['suppressed'] += 1
            return
        extra = f' ({_state["suppressed"]} more since the last message)' if _state['suppressed'] else ''
        _state['last_log'], _state['suppressed'] = now, 0
    logger.warning('Cache unavailable during %s: %s: %s — serving without cache for %ss%s',
                   op, type(exc).__name__, exc, int(BREAK_SECONDS), extra)


def _safe(fallback):
    """Run the real cache call; on a Redis failure (or while the breaker is open) return fallback(*args)."""
    def deco(method):
        @functools.wraps(method)
        def wrapper(self, *args, **kwargs):
            if _is_down():
                return fallback(*args, **kwargs)
            try:
                return method(self, *args, **kwargs)
            except CACHE_ERRORS as exc:
                _failed(method.__name__, exc)
                return fallback(*args, **kwargs)
        return wrapper
    return deco


def _default(key, default=None, version=None):
    return default


class ResilientRedisCache(RedisCache):
    @_safe(_default)
    def get(self, key, default=None, version=None):
        return super().get(key, default, version)

    @_safe(lambda keys, version=None: {})
    def get_many(self, keys, version=None):
        return super().get_many(keys, version)

    @_safe(lambda *a, **k: False)
    def has_key(self, key, version=None):
        return super().has_key(key, version)

    @_safe(lambda *a, **k: False)
    def set(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        return super().set(key, value, timeout, version)

    @_safe(lambda data, *a, **k: list(data))      # Django convention: the keys that were NOT stored
    def set_many(self, data, timeout=DEFAULT_TIMEOUT, version=None):
        return super().set_many(data, timeout, version)

    @_safe(lambda *a, **k: True)
    def add(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        return super().add(key, value, timeout, version)

    @_safe(lambda *a, **k: False)
    def touch(self, key, timeout=DEFAULT_TIMEOUT, version=None):
        return super().touch(key, timeout, version)

    @_safe(lambda *a, **k: 0)
    def incr(self, key, delta=1, version=None):
        return super().incr(key, delta, version)

    @_safe(lambda *a, **k: False)
    def delete(self, key, version=None):
        return super().delete(key, version)

    @_safe(lambda *a, **k: None)
    def delete_many(self, keys, version=None):
        return super().delete_many(keys, version)

    @_safe(lambda *a, **k: None)
    def clear(self):
        return super().clear()
