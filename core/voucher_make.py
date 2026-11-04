"""Making a batch of vouchers — one function for the website's Generate page and the mobile app.

``make_batch()`` does what Generate always did, in the same order:

1. new unique codes in the chosen format (nothing is created when that fails);
2. in one transaction: the batch, its vouchers with serial numbers, the activity log line, and — when an agent
   holds the batch — handing it over on credit or paid upfront (core/finance.py);
3. sending just this batch to its router (not a full sync);
4. telling the people who track the plan (core/tracking.py) and, on their phones, the owner and admins when
   someone else on the team made the vouchers (core/mobile_push.py).

The caller turns the returned notes into messages (website) or JSON (app).
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger('taptap.vouchers')


def make_batch(business, plan, qty, fmt, *, router=None, name='', note='', agent=None, settlement='credit',
               pay_method='cash', reference='', user=None, serial=None, save_serial=False):
    """Create ``qty`` vouchers of ``plan`` in a new batch. Returns ``(batch, notes)``.

    ``fmt`` is a validated code format (utils.code_format / code_format_from_post). ``serial`` may hold
    ``format``, ``digits``, ``reset`` and ``start`` (missing = the business's saved serial settings).
    ``notes`` is a list of ``(level, text)`` with level ``'info'`` or ``'warning'``.
    Raises utils.CodeFormatError or serials.SerialError before anything is created."""
    from . import serials
    from .models import Voucher, VoucherBatch
    from .utils import describe_format, generate_codes, log

    serial = serial or {}
    sfmt, sdig, sreset = serials.clean(serial.get('format', business.serial_format),
                                       serial.get('digits', business.serial_digits),
                                       serial.get('reset', business.serial_reset))
    sstart = serial.get('start')
    codes = generate_codes(qty, fmt['length'], fmt['charset'], fmt['prefix'], fmt['suffix'], business, fmt['case'])
    name = (name or '').strip() or f'{plan.name} {timezone.localtime():%Y-%m-%d %H:%M}'
    notes = []
    with transaction.atomic():
        if save_serial:
            business.serial_format, business.serial_digits, business.serial_reset = sfmt, sdig, sreset
            business.save(update_fields=['serial_format', 'serial_digits', 'serial_reset'])
        batch = VoucherBatch.objects.create(business=business, name=name[:120], plan=plan, quantity=qty, note=(note or '')[:255])
        sns = serials.allocate(business, qty, fmt=sfmt, digits=sdig, reset=sreset, start=sstart, batch=batch, plan=plan.name)
        for c, sn in zip(codes, sns):
            Voucher.objects.create(business=business, batch=batch, router=router, code=c, serial=sn, plan_name=plan.name,
                                   price=plan.price, duration_minutes=plan.duration_minutes, max_devices=plan.max_devices,
                                   source='taptap')
        log(business, 'Voucher Generated', f'Batch {batch.name}: {qty} voucher(s), {describe_format(fmt)}, serials {sns[0]}–{sns[-1]}'
            + (f' for {agent.name}' if agent else ''))
        if agent:
            from .finance import assign_batch
            settlement = settlement if settlement in {'credit', 'prepaid'} else 'credit'
            _, sales = assign_batch(batch, agent, settlement, pay_method or 'cash', user, (reference or '')[:120])
            notes.append(('info', f'Batch issued to {agent.name}' + (
                f' — bought upfront for {business.currency}{sum(x.amount for x in sales):,.2f}.' if sales
                else ' on credit: each voucher is credited to them as it sells.')))
    if router:
        # Send just this batch to the router — not a full router sync.
        try:
            from .voucher_push import push_vouchers
            for msg in push_vouchers(list(batch.vouchers.all()), user).values():
                notes.append(('info', msg + '.'))
        except Exception as e:
            notes.append(('warning', f'Vouchers were created in TapTap; sending them to {router.name} failed for now ({e}). '
                                     'They go with the next sync.'))
    who = (user.get_full_name() or user.username) if getattr(user, 'is_authenticated', False) else ''
    try:
        from .tracking import notify as _track
        _track(business, 'vouchers_added', f'{qty} new voucher(s) of {plan.name} in batch {batch.name}' + (f' by {who}' if who else ''),
               batch=batch, plan=plan, actor=user, count=qty)
    except Exception:
        pass
    try:
        from . import mobile_push
        mobile_push.vouchers_made(business, user, f'{qty} voucher(s) of {plan.name}',
                                  f'Batch {batch.name}' + (f' · for {agent.name}' if agent else ''), batch=batch)
    except Exception:
        logger.exception('vouchers-made push for batch %s', batch.pk)
    return batch, notes
