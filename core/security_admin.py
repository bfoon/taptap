"""Security → "Default "admin" account is active": set a strong password on it, or disable it — safely.

Two ways to fix it, chosen in a dialog:

* **Set a new password** (always offered). If TapTap itself logs in to the router as ``admin``, TapTap
  stores the new password too and checks it works, so TapTap never locks itself out.
* **Disable admin** — only when that cannot lock anyone out:
  - TapTap does not log in as ``admin`` (else it would cut itself off);
  - another enabled full-rights account exists, or the dialog creates one first (name + password);
  - the router is reached over the RouterOS API. On TapTap Link routers TapTap's scheduled Link
    script runs with the rights of the account that installed it — usually admin — so disabling it
    could stop TapTap Link. There, only the password option is offered.

Passwords are never logged; a password sent through TapTap Link is wiped from TapTap's command
list as soon as the router confirms it.
"""
from __future__ import annotations

import re

from django.core.cache import cache

NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$')
WEAK_WORDS = ('admin', 'password', 'mikrotik', 'taptap', '123456', 'qwerty')


class AdminFixError(ValueError):
    pass


def check_password(password, again):
    pw = str(password or '')
    if pw != str(again or ''):
        raise AdminFixError('The two passwords are not the same.')
    if len(pw) < 12:
        raise AdminFixError('Use at least 12 characters.')
    if len(pw) > 64 or any(ord(c) < 32 for c in pw):
        raise AdminFixError('The password has a character that cannot be used, or is longer than 64 characters.')
    kinds = sum(bool(re.search(rx, pw)) for rx in (r'[a-z]', r'[A-Z]', r'\d', r'[^A-Za-z0-9]'))
    if kinds < 3:
        raise AdminFixError('Mix at least three of: small letters, capital letters, numbers, symbols.')
    if any(w in pw.lower() for w in WEAK_WORDS):
        raise AdminFixError('Do not use words like “admin”, “password” or “mikrotik” in the password.')
    return pw


def _users_from_snapshot(router):
    from .routeros_analysis import g, truthy
    try:
        rows = ((router.config_snapshot.sections or {}).get('Users') or {}).get('rows', [])
    except Exception:
        rows = []
    return [{'name': str(g(r, 'name')), 'group': str(g(r, 'group')), 'disabled': truthy(g(r, 'disabled', default='no'))} for r in rows]


def state(router):
    """What the dialog may offer for this router."""
    from .linkops import uses_link
    users = _users_from_snapshot(router)
    others = [u['name'] for u in users if u['name'] != 'admin' and u['group'] == 'full' and not u['disabled']]
    taptap_is_admin = str(router.username or '').lower() == 'admin'
    link = uses_link(router)
    why_not = ('TapTap itself logs in to this router as admin — set a strong password instead, or give TapTap its own '
               'account first (see “TapTap connects with the admin account”).' if taptap_is_admin else
               'This router is reached through TapTap Link, whose script runs with the rights of the account that installed it '
               '(usually admin). Disabling admin could stop TapTap Link, so set a strong password instead.' if link else '')
    return {'router_id': router.pk, 'router': router.name, 'taptap_is_admin': taptap_is_admin, 'link': link,
            'other_full_users': others, 'can_disable': not why_not, 'why_not_disable': why_not,
            'needs_new_user': not others}


def _refresh_snapshot(svc, router):
    from .models import RouterConfigSnapshot
    cfg = svc.configuration_snapshot()
    existing = RouterConfigSnapshot.objects.filter(router=router).first()
    if existing and existing.sections.get('_identity'):
        cfg['sections']['_identity'] = existing.sections['_identity']
    RouterConfigSnapshot.objects.update_or_create(router=router, defaults={'sections': cfg['sections'], 'load_balancing': cfg['load_balancing'],
                                                                           'captured_at': cfg['captured_at']})


def _audit(router, user, operation, fields, ok=True, error=''):
    from .models import RouterConfigChange
    from .utils import log
    RouterConfigChange.objects.create(business=router.business, router=router, actor=user if getattr(user, 'is_authenticated', False) else None,
                                      resource_path='/user', operation=operation, target_id='admin', fields=fields,
                                      status='success' if ok else 'failed', error=error[:500])
    if ok:
        log(router.business, 'Security Fix', f'{router.name}: {fields.get("what", operation)}')


def set_password(router, password, again, user=None):
    """Give the admin account a new strong password. Returns a message."""
    from .linkops import send, uses_link
    from .mikrotik import MikroTikService
    from .models import Router
    pw = check_password(password, again)
    taptap_is_admin = str(router.username or '').lower() == 'admin'
    if uses_link(router):
        cmd = send(router, 'admin_password', {'password': pw}, label='New password for the admin account', user=user)
        if taptap_is_admin and cmd is not None:   # TapTap stores it once the router confirms
            cache.set(f'tt:adminpw:{getattr(cmd, "pk", cmd)}', pw, 7 * 86400)
        _audit(router, user, 'admin-password', {'what': 'New password for the admin account (TapTap Link)', 'via': 'TapTap Link'})
        return 'Queued — the router changes the admin password at its next check-in.'
    svc = MikroTikService(router).connect()
    try:
        svc.system_user_set_password('admin', pw)
    except Exception as exc:
        svc.close()
        _audit(router, user, 'admin-password', {'what': 'New password for the admin account'}, ok=False, error=str(exc))
        raise AdminFixError(f'The router refused: {exc}')
    svc.close()
    note = ''
    if taptap_is_admin:   # TapTap logs in as admin: store the new password and prove it works
        Router.objects.filter(pk=router.pk).update(password=pw)
        router.password = pw
        try:
            check = MikroTikService(router).connect()
            _refresh_snapshot(check, router)
            check.close()
            note = ' TapTap now logs in with the new password.'
        except Exception as exc:
            note = f' TapTap saved the new password but could not log in with it yet ({exc}). Check the router.'
    else:
        try:
            with MikroTikService(router) as s2:
                _refresh_snapshot(s2, router)
        except Exception:
            pass
    _audit(router, user, 'admin-password', {'what': 'New password for the admin account'})
    return 'The admin account now has your new password. Keep it somewhere safe.' + note


def disable_admin(router, user=None, new_name='', new_password='', new_again=''):
    """Disable admin — after making sure another full-rights account exists (creating one if asked)."""
    from .mikrotik import MikroTikService
    from .routeros_analysis import truthy
    st = state(router)
    if not st['can_disable']:
        raise AdminFixError(st['why_not_disable'])
    svc = MikroTikService(router).connect()
    try:
        users = svc.system_users()
        others = [u for u in users if str(u.get('name')) != 'admin' and str(u.get('group')) == 'full' and not truthy(u.get('disabled', 'no'))]
        created = ''
        if not others:
            name = str(new_name or '').strip()
            if not NAME_RE.match(name) or name.lower() == 'admin':
                raise AdminFixError('There is no other full-rights account. Choose a name for your own account (3–32 letters or numbers, not “admin”).')
            pw = check_password(new_password, new_again)
            svc.system_user_add(name, pw, group='full')
            created = name
        svc.system_user_disable('admin')
        try:
            _refresh_snapshot(svc, router)
        except Exception:
            pass
    except AdminFixError:
        raise
    except Exception as exc:
        _audit(router, user, 'admin-disable', {'what': 'Disable the admin account'}, ok=False, error=str(exc))
        raise AdminFixError(f'The router refused: {exc}')
    finally:
        svc.close()
    _audit(router, user, 'admin-disable', {'what': 'Disable the admin account' + (f' (created full-rights user {created})' if created else '')})
    return ('Admin is disabled.' + (f' Log in to the router as “{created}” from now on.' if created else
                                     f' Log in with {", ".join(o["name"] for o in others[:3]) if others else "your own account"} from now on.'))


def link_password_ack(cmd):
    """TapTap Link confirmed the new admin password: if TapTap logs in as admin, store it."""
    from .models import Router
    key = f'tt:adminpw:{cmd.pk}'
    pw = cache.get(key)
    if pw and str(cmd.router.username or '').lower() == 'admin':
        Router.objects.filter(pk=cmd.router_id).update(password=pw)
    cache.delete(key)
