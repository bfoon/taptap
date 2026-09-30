"""Members: customers who log in with a username and a password instead of a voucher code.

A member is a ``Voucher`` with ``login_type='member'``:

* the username is stored in ``Voucher.code`` — so freezing, adding time, disabling, device
  locks, fair usage, live enforcement, router sync, sales and reports all work for members
  exactly as they do for vouchers;
* ``Voucher.password`` holds the member's own password. Empty means "same as the username",
  so the password follows the username like a voucher code does;
* on the MikroTik the member is a normal ``/ip hotspot user`` with ``name=<username>`` and
  ``password=<password>`` (see ``MikroTikService.upsert_voucher`` and the TapTap Link
  ``hotspot_users`` command, which both send the member password).

Two kinds of member, both chosen with the plan:
* **Paying** — any priced plan. The first payment can be recorded when the member is created;
  a renewal adds the plan's time again and books a new sale.
* **Free / unlimited** — no charge and no time limit (a free unlimited plan, or the built-in
  "Free — unlimited" choice). They are never flagged as missing a sale.

Usernames and voucher codes share one namespace (they are all hotspot users on the router),
so a username can never be the same as any voucher code, deleted voucher or old code.
"""
from __future__ import annotations

import re
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import Voucher, VoucherCodeAlias, VoucherSale

USERNAME_RE = re.compile(r'^[a-z0-9][a-z0-9._@-]{2,31}$')
PASSWORD_MIN, PASSWORD_MAX = 4, 64
FREE_PLAN_NAME = 'Member — free, unlimited'


class MemberError(ValueError):
    """A problem the person can fix; the message is shown as is."""


def clean_username(value):
    """Usernames are lower-case, without spaces, so phones that capitalise the first letter still work."""
    return re.sub(r'\s+', '', str(value or '')).lower()


def check_password(password):
    pw = str(password or '')
    if pw != pw.strip() or re.search(r'\s', pw):
        raise MemberError('The password cannot contain spaces.')
    if not PASSWORD_MIN <= len(pw) <= PASSWORD_MAX:
        raise MemberError(f'The password must be {PASSWORD_MIN}–{PASSWORD_MAX} characters.')
    if any(ord(ch) < 32 for ch in pw):
        raise MemberError('The password contains a character that cannot be used.')
    return pw


def username_taken(username, exclude=None):
    qs = Voucher.all_objects.filter(code__iexact=username)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    return qs.exists() or VoucherCodeAlias.objects.filter(code__iexact=username).exists()


def check_username(username, exclude=None):
    name = clean_username(username)
    if not USERNAME_RE.match(name):
        raise MemberError('A username must be 3–32 characters: small letters, numbers, dot, dash, underscore or @, '
                          'starting with a letter or number.')
    if username_taken(name, exclude=exclude):
        raise MemberError(f'“{name}” is already used by another member or voucher code. Choose another username.')
    return name


def stored_password(username, password, same):
    """What to keep in Voucher.password: '' when it is the same as the username."""
    if same or str(password or '') == username:
        return ''
    return check_password(password)


def plan_choice(business, value):
    """(plan or None, is_free_unlimited) from the form's plan value ('free' or a plan id)."""
    if value == 'free':
        return None, True
    plan = business.plans.filter(active=True, pk=value or 0).first() if str(value or '').isdigit() else None
    if not plan:
        raise MemberError('Choose a plan, or “Free — unlimited”.')
    return plan, bool(plan.is_free and plan.is_unlimited)


def kind_of(voucher):
    """'free' for members that are never charged and never run out, else 'paid'."""
    return 'free' if not voucher.duration_minutes and not voucher.expires_at and not voucher.price else 'paid'


@transaction.atomic
def create_member(business, *, username, password='', same=False, plan_value='', devices=1, rate_limit='',
                  router=None, agent=None, customer_name='', customer_phone='', note='',
                  paid=False, method='cash', reference='', user=None):
    """Create a member in TapTap. The caller pushes it to the router (views_members.push)."""
    from .finance import record_sale
    from .serials import allocate
    name = check_username(username)
    pw = stored_password(name, password, same)
    plan, free = plan_choice(business, plan_value)
    if plan:
        plan_name, price, minutes, max_devices, rate = plan.name, plan.price, plan.duration_minutes, plan.max_devices, ''
    else:
        try:
            max_devices = max(1, min(20, int(devices or 1)))
        except (TypeError, ValueError):
            max_devices = 1
        rate = (rate_limit or '').strip()[:50]
        if rate and not re.fullmatch(r'\d+[kKmM]?(/\d+[kKmM]?)?', rate):
            raise MemberError('Speed looks wrong — use a form like 5M/5M or 2M, or leave it empty for full speed.')
        plan_name, price, minutes = FREE_PLAN_NAME, Decimal('0'), 0
    v = Voucher.objects.create(
        business=business, router=router, code=name, password=pw, login_type='member',
        serial=allocate(business, 1, plan=plan_name)[0], plan_name=plan_name, price=price,
        duration_minutes=minutes, max_devices=max_devices, rate_limit=rate, source='taptap', agent=agent,
        customer_name=(customer_name or '').strip()[:120], customer_phone=(customer_phone or '').strip()[:60],
        note=(note or '').strip()[:255], mikrotik_sync_status='Pending')
    if paid and price and price > 0:
        record_sale(business, v, method=method, agent=agent, customer_name=v.customer_name,
                    customer_phone=v.customer_phone, reference=(reference or '')[:120], user=user,
                    notes=f'Member {name}: first payment')
    return v


def _push_password(voucher, user=None):
    """Send a changed password to the router. Returns (via, result, ok)."""
    from .voucher_history import channel
    router = voucher.router
    via = channel(router)
    if not router:
        return via, 'No router assigned — changed in TapTap only', True
    if via == 'TapTap Link':
        from .agent import push_pending_vouchers
        from .linkops import send
        Voucher.objects.filter(pk=voucher.pk).update(mikrotik_sync_status='Pending', mikrotik_sync_error='')
        push_pending_vouchers(router)       # the hotspot_users command sets the member password on update
        try:
            send(router, 'disconnect', {'user': voucher.code}, label=f'Sign out member {voucher.code}', user=user)
        except ValueError:
            pass
        return via, 'Queued — the router applies it at its next check-in', True
    from .mikrotik import MikroTikService
    try:
        svc = MikroTikService(router).connect()
        try:
            if svc.set_user_password(voucher.code, voucher.login_password):
                return via, 'Password changed on the router; the member was signed out', True
        finally:
            svc.close()
    except Exception as exc:
        Voucher.objects.filter(pk=voucher.pk).update(mikrotik_sync_status='Pending', mikrotik_sync_error=str(exc)[:500])
        return via, f'Router update failed: {exc}. TapTap sends it at the next sync.', False
    Voucher.objects.filter(pk=voucher.pk).update(mikrotik_sync_status='Pending', mikrotik_sync_error='')
    return via, 'Not on the router yet — it is added with the new password at the next sync', True


def change_password(voucher, password='', same=False, user=None, reason=''):
    """Give a member a new password (or make it the same as the username). Returns (ok, result)."""
    from .voucher_history import record
    from .utils import log
    if not voucher.is_member:
        raise MemberError('Only members have a password. A voucher logs in with its code.')
    if voucher.deleted_at:
        raise MemberError('Members in the bin cannot be changed.')
    pw = stored_password(voucher.code, password, same)
    Voucher.objects.filter(pk=voucher.pk).update(password=pw)
    voucher.password = pw
    via, result, ok = _push_password(voucher, user=user)
    record(voucher, 'password_changed', user=user, reason=(reason or '')[:255], via=via, router_result=result,
           status_before=voucher.status, status_after=voucher.status,
           text='Password is now the same as the username' if not pw else 'New password set')
    log(voucher.business, 'Member Password Changed', voucher.code)
    return ok, result


def renew(voucher, *, amount=None, method='cash', reference='', agent=None, user=None):
    """A paying member pays again: add the plan's time and book the payment as a sale.

    The first payment is the voucher's own sale; renewals are separate sales that carry the
    username in ``voucher_code`` (a voucher can only be linked to one sale)."""
    from .finance import d, record_sale
    from . import voucher_history as vh
    if not voucher.is_member:
        raise MemberError('Only members can be renewed.')
    plan = voucher.business.plans.filter(name=voucher.plan_name).first()
    minutes = (plan.duration_minutes if plan else 0) or voucher.duration_minutes
    if not minutes:
        raise MemberError(f'{voucher.code} has no time limit, so there is nothing to renew.')
    price = d(amount) if amount not in (None, '') else d(plan.price if plan else voucher.price)
    if price < 0:
        raise MemberError('The amount cannot be negative.')
    ok, result = vh.enable(voucher, user=user, reason='Member renewal', add_minutes=minutes)
    sale = None
    if price > 0:
        sale = record_sale(voucher.business, None, plan_name=voucher.plan_name, amount=price, method=method,
                           agent=agent, customer_name=voucher.customer_name, customer_phone=voucher.customer_phone,
                           reference=(reference or '')[:120], user=user, notes=f'Member {voucher.code}: renewal')
        VoucherSale.objects.filter(pk=sale.pk).update(voucher_code=voucher.code)
        sale.voucher_code = voucher.code
    return ok, result, sale, minutes


def member_stats(business):
    qs = business.vouchers.filter(login_type='member')
    now = timezone.now()
    free = qs.filter(duration_minutes=0, expires_at__isnull=True, price=0).count()
    return {'total': qs.count(), 'free': free, 'paid': qs.count() - free,
            'disabled': qs.filter(status='disabled').count(),
            'online_ready': qs.filter(status='active', frozen_at__isnull=True).count(), 'now': now}
