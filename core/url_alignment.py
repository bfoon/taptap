"""Make every add-on's view wrapper take effect, whatever order the add-ons were installed in.

The bug
-------
Add-ons change a view by replacing the module attribute (``views_ads.portal_state = wrapped``). Django keeps the
function object it found when the URLconf was first imported. During startup an add-on (member_arrears) reads
``get_resolver().url_patterns`` — that imports the URLconf there and then, and from that moment every later
replacement changed the module attribute only: real requests still reached the ORIGINAL view.

On the current code this left three views without their add-ons:

* ``/p/<slug>/check/`` and ``/p/<slug>/state/`` (both login paths): the expired-voucher page, the wrong-code
  protection (tries left / timed lock) and the login guarantee never ran for customers;
* ``/security/``: the voucher-entry protection settings.

The fix
-------
After every add-on is installed, each core URL pattern whose callback is no longer what its module exports is
pointed at the current function — but only when the stored callback is not itself a decorator around the current
function (``login_required(view)`` and the like stay exactly as they are). Nothing else changes.
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger('taptap.urls')


def _chain(fn, limit=20):
    """fn and every function it wraps (functools.wraps keeps them in __wrapped__)."""
    out = []
    while fn is not None and len(out) < limit:
        out.append(fn)
        fn = getattr(fn, '__wrapped__', None)
    return out


def _patterns(patterns):
    from django.urls import URLPattern, URLResolver
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from _patterns(p.url_patterns)
        elif isinstance(p, URLPattern):
            yield p


def stale(patterns=None):
    """[(pattern, current_function)] for core URL patterns that still hold a replaced view."""
    from django.urls import get_resolver
    out = []
    for p in _patterns(patterns if patterns is not None else get_resolver().url_patterns):
        cb = p.callback
        modname, name = getattr(cb, '__module__', '') or '', getattr(cb, '__name__', '') or ''
        if not modname.startswith('core.') or not name:
            continue
        mod = sys.modules.get(modname)
        current = getattr(mod, name, None) if mod else None
        if current is None or current is cb or not callable(current):
            continue
        if any(f is current for f in _chain(cb)):
            continue          # the URL holds a decorator around the current view (login_required…): leave it
        out.append((p, current))
    return out


def realign():
    """Point stale core URL patterns at the current (wrapped) view. Returns the URL names changed."""
    changed = []
    try:
        for p, current in stale():
            p.callback = current
            if hasattr(p, '_callback'):
                p._callback = current
            changed.append(p.name or p.pattern.describe())
    except Exception:
        logger.exception('URL realignment failed')
        return changed
    if changed:
        logger.info('Add-on views now active on their URLs: %s', ', '.join(changed))
    return changed


def install():
    realign()
