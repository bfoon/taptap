"""Changing a voucher's code.

The voucher stays the SAME record: its sale, agent, batch, device bindings, usage and
history all stay attached, so nothing is counted twice. Only the code changes:

* the old code is kept as a ``VoucherCodeAlias`` — it can never be issued again, and
  searching for it still finds the voucher;
* the hotspot user on the MikroTik is RENAMED (``set name=``), which keeps its used
  uptime, so the customer does not get the time again. A voucher logs in with its code
  as BOTH username and password, so the password is changed to the new code as well
  (TapTap vouchers always; router-made ones when their password was the old code);
* the change is written to the voucher history (who, when, why, old → new, router answer);
* router sync keeps it that way: if the old name is seen on a router it is renamed
  again, never imported as a second voucher.
"""
from __future__ import annotations

import logging
import re

from django.core.cache import cache
from django.db import transaction

from .models import (RouterHotspotUser, SessionIncident, UsageRecord, Voucher, VoucherCodeAlias,
                     VoucherSale)

logger = logging.getLogger('taptap.vouchers')

CODE_RE = re.compile(r'^[A-Z0-9]{4,20}$')
HEAL_EVERY = 600


class CodeChangeError(ValueError):
    pass


def clean_code(value):
    return re.sub(r'[\s-]', '', str(value or '')).upper()


def code_taken(code, exclude_voucher=None):
    """True when a code is used by any voucher (bin included) or was ever used before."""
    qs = Voucher.all_objects.filter(code__iexact=code)
    if exclude_voucher is not None:
        qs = qs.exclude(pk=exclude_voucher.pk)
    return qs.exists() or VoucherCodeAlias.objects.filter(code__iexact=code).exists()


def aliases(business):
    """{OLD CODE: voucher} for this business."""
    return {a.code.upper(): a.voucher for a in
            VoucherCodeAlias.objects.filter(business=business).select_related('voucher', 'voucher__router')}


def _force(voucher):
    """TapTap made this voucher, so its password IS its code: always set it with the name.
    A member with a password of their own keeps it when the username changes."""
    return getattr(voucher, 'source', '') == 'taptap' and not getattr(voucher, 'password', '')


def _password_follows_code(voucher, row, old):
    """Should the router password change with the code? TapTap vouchers: always. Members with their
    own password: never. Router-made vouchers: only when the password was the old code (or empty),
    so a separate password is kept."""
    if getattr(voucher, 'password', ''):
        return False
    if _force(voucher):
        return True
    pw = row.get('password')
    return pw is None or str(pw).strip().upper() in ('', str(old).upper())


def _rename_on_router(voucher, old, new, user=None):
    """(via, result, ok) — rename the hotspot user, keeping its used uptime."""
    from .voucher_history import channel
    router = voucher.router
    via = channel(router)
    if not router:
        return via, 'No router assigned — changed in TapTap only', True
    if via == 'TapTap Link':
        from .linkops import send
        try:
            send(router, 'hotspot_user_rename', {'name': old, 'new_name': new, 'password': _force(voucher)},
                 label=f'Rename voucher {old} → {new}', user=user, minutes=60 * 24 * 3)
            return via, 'Queued — the router renames it at its next check-in', True
        except ValueError as exc:
            return via, f'Not sent yet: {exc}. TapTap renames it at the next sync.', False
    from .mikrotik import MikroTikService
    try:
        svc = MikroTikService(router).connect()
        try:
            users = svc.resource('/ip/hotspot/user')
            rows = users.get(name=old)
            if rows:
                fields = {'name': new}
                if _password_follows_code(voucher, rows[0], old):
                    fields['password'] = new
                users.set(id=rows[0]['id'], **fields)
                return via, 'Renamed on the router (used time kept)' + (', password updated' if 'password' in fields else ''), True
            rows = users.get(name=new)
            if rows:
                if _password_follows_code(voucher, rows[0], old):
                    users.set(id=rows[0]['id'], password=new)
                return via, 'Already renamed on the router', True
        finally:
            svc.close()
    except Exception as exc:
        return via, f'Router update failed: {exc}. TapTap renames it at the next sync.', False
    # Not on the router at all: let the normal voucher push create it under the new code.
    Voucher.objects.filter(pk=voucher.pk).update(mikrotik_sync_status='Pending', mikrotik_sync_error='')
    return via, 'Not found on the router — it will be added with the new code at the next sync', True


def change_code(voucher, new_code, user=None, reason=''):
    """Give a voucher a new code. Returns (ok, router_result)."""
    from .voucher_history import record
    from .utils import log
    new = clean_code(new_code)
    reason = (reason or '').strip()[:255]
    if voucher.deleted_at:
        raise CodeChangeError('Vouchers in the bin cannot be changed.')
    if not reason:
        raise CodeChangeError('Give a reason for the change — it is kept in the voucher history.')
    if voucher.login_type == 'member':
        from .members import clean_username, USERNAME_RE
        new = clean_username(new_code)
        if not USERNAME_RE.match(new):
            raise CodeChangeError('A username must be 3–32 characters: small letters, numbers, dot, dash, underscore or @.')
    elif not CODE_RE.match(new):
        raise CodeChangeError('The new code must be 4–20 letters or numbers.')
    old = voucher.code
    if new.upper() == old.upper():
        raise CodeChangeError('That is already the code of this voucher.')
    if code_taken(new, exclude_voucher=voucher):
        raise CodeChangeError(f'{new} is already used by another voucher, or was used before. Choose another code.')

    with transaction.atomic():
        v = Voucher.objects.select_for_update().get(pk=voucher.pk)
        VoucherCodeAlias.objects.create(business=v.business, voucher=v, code=old, reason=reason,
                                        changed_by=user if getattr(user, 'is_authenticated', False) else None)
        v.code = new
        v.save(update_fields=['code'])
        # Keep every record attached to the voucher under its new code.
        VoucherSale.objects.filter(voucher=v).update(voucher_code=new)
        if v.router_id:
            RouterHotspotUser.objects.filter(router_id=v.router_id, username=old).update(username=new)
            UsageRecord.objects.filter(router_id=v.router_id, username=old).update(username=new)
        SessionIncident.objects.filter(voucher=v).update(username=new)
    voucher.code = new
    via, result, ok = _rename_on_router(voucher, old, new, user=user)
    record(voucher, 'code_changed', user=user, reason=reason, via=via, router_result=result,
           status_before=voucher.status, status_after=voucher.status, old_code=old, new_code=new,
           text=f'{old} → {new}')
    log(voucher.business, 'Voucher Code Changed', f'{old} → {new}: {reason}')
    return ok, result


# ─────────────────────────────── router sync ───────────────────────────────

def rename_with_service(svc, pairs, present):
    """Over an open API connection: rename old → new for each pair, or remove the old
    entry when the new one is already there. `present` = upper-case names on the router."""
    users = svc.resource('/ip/hotspot/user')
    for old, new in pairs:
        rows = users.get(name=old)
        if not rows:
            continue
        if new.upper() in present:
            users.remove(id=rows[0]['id'])
        else:
            pw = rows[0].get('password')
            fields = {'name': new}
            if pw is None or str(pw).strip().upper() in ('', str(old).upper()):
                fields['password'] = new
            users.set(id=rows[0]['id'], **fields)


def heal(router, pairs, present=()):
    """Router sync saw old codes on this router: put the current codes back, at most once
    every HEAL_EVERY seconds per router."""
    if not pairs:
        return
    key = f'code-heal:{router.pk}'
    if cache.get(key):
        return
    cache.set(key, 1, HEAL_EVERY)
    from .voucher_history import channel
    present = {p.upper() for p in present}
    if channel(router) == 'TapTap Link':
        from .linkops import send
        from .models import AgentCommand
        if AgentCommand.objects.filter(router=router, kind='hotspot_user_rename', status__in=['queued', 'sent']).exists():
            return
        for old, new in pairs[:50]:
            try:
                if new.upper() in present:
                    send(router, 'hotspot_user_remove', {'name': old}, label=f'Remove old code {old}', minutes=60 * 24)
                else:
                    send(router, 'hotspot_user_rename', {'name': old, 'new_name': new}, label=f'Rename voucher {old} → {new}', minutes=60 * 24)
            except ValueError:
                return
        return
    from .mikrotik import MikroTikService
    try:
        svc = MikroTikService(router).connect()
        try:
            rename_with_service(svc, pairs, present)
        finally:
            svc.close()
    except Exception:
        logger.exception('Could not rename old voucher codes on %s', router.name)


# ─────────────────────── repair: codes changed before the password fix ───────────────────────

REPAIR_EVERY = 600


def stale_passwords(business, router, rows):
    """Current codes on this router whose password is still an OLD code — vouchers whose code was
    changed while only the name was renamed, so the new code could not log in."""
    olds = {}
    for a in (VoucherCodeAlias.objects.filter(business=business, voucher__router=router, voucher__deleted_at__isnull=True)
              .exclude(voucher__password__gt='')      # members with their own password: never reset it
              .select_related('voucher')):
        entry = olds.setdefault(a.voucher.code.upper(), [set(), a.voucher.source])
        entry[0].add(a.code.upper())
    out = []
    for r in rows or []:
        name = str(r.get('name', '')).strip()
        entry = olds.get(name.upper())
        if not entry:
            continue
        pw = r.get('password')
        if pw is None or str(pw).startswith('•'):
            continue   # password not readable: the rename fix will not guess
        pw = str(pw).strip().upper()
        if pw != name.upper() and (pw in entry[0] or pw == '' or entry[1] == 'taptap'):
            out.append(name)
    return out


def repair_passwords(router, names, svc=None):
    """Set password = code for these names. Direct API with an open `svc`, else a TapTap Link command."""
    names = [n for n in dict.fromkeys(names or []) if n][:500]
    if not names:
        return 0
    if svc is not None:
        users = svc.resource('/ip/hotspot/user')
        for n in names:
            rows = users.get(name=n)
            if rows:
                users.set(id=rows[0]['id'], password=n)
        return len(names)
    key = f'code-repass:{router.pk}'
    if cache.get(key):
        return 0
    cache.set(key, 1, REPAIR_EVERY)
    from .linkops import send
    for i in range(0, len(names), 100):
        try:
            send(router, 'hotspot_users_repass', {'names': names[i:i + 100]},
                 label=f'Fix login of {len(names[i:i + 100])} voucher(s) whose code was changed', minutes=60 * 24)
        except ValueError:
            break
    return len(names)
