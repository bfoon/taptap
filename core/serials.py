"""Voucher serial numbers.

Each business chooses how its serials look, with a pattern made of any text plus tokens:

    {n}     running number, zero-padded to the chosen digits (000124)
    {bn}    number inside this batch: 1, 2, 3… (padded the same way)
    {batch} the batch's number in TapTap
    {yyyy} {yy} {mm} {dd}   date the voucher was made
    {plan}  plan initials, e.g. "1 Day Pass" → 1DP

Examples:  {n} → 000124   KN-{yy}{mm}-{n} → KN-2609-000124   {plan}/{batch}/{bn} → 1DP/42/007

The running number can restart every year, month or day, or every batch. A serial is
given once, when the voucher is created, and stored — reprints always show the same one.
"""
from __future__ import annotations

import json
import re

from django.db import transaction
from django.utils import timezone

TOKEN_RE = re.compile(r'\{(n|bn|batch|yyyy|yy|mm|dd|plan)\}')
ALLOWED_TEXT = re.compile(r'^[A-Za-z0-9 _./#:\-{}]*$')
RESETS = ('never', 'yearly', 'monthly', 'daily', 'batch')
MAX_LEN = 40


class SerialError(ValueError):
    pass


def plan_initials(name):
    words = re.findall(r'[A-Za-z0-9]+', name or '')
    out = ''.join(w[0] if not w.isdigit() else w for w in words).upper()
    return out[:5] or 'X'


def period_key(reset, now=None):
    now = timezone.localtime(now or timezone.now())
    return {'never': 'all', 'yearly': f'{now:%Y}', 'monthly': f'{now:%Y-%m}', 'daily': f'{now:%Y-%m-%d}'}.get(reset)


def render(fmt, n=1, digits=6, bn=None, batch=None, plan='', now=None):
    now = timezone.localtime(now or timezone.now())
    vals = {'n': str(n).zfill(digits), 'bn': str(bn if bn is not None else n).zfill(digits),
            'batch': str(batch or 0), 'yyyy': f'{now:%Y}', 'yy': f'{now:%y}', 'mm': f'{now:%m}', 'dd': f'{now:%d}',
            'plan': plan_initials(plan)}
    return TOKEN_RE.sub(lambda m: vals[m.group(1)], fmt)[:60]


def clean(fmt, digits, reset):
    """Validate a pattern; returns (fmt, digits, reset)."""
    fmt = (fmt or '').strip() or '{n}'
    if len(fmt) > MAX_LEN:
        raise SerialError(f'Keep the serial pattern under {MAX_LEN} characters.')
    if not ALLOWED_TEXT.match(fmt):
        raise SerialError('Use letters, numbers, spaces and - _ . / # : in the serial pattern.')
    unknown = [t for t in re.findall(r'\{([^}]*)\}', fmt) if t not in ('n', 'bn', 'batch', 'yyyy', 'yy', 'mm', 'dd', 'plan')]
    if unknown:
        raise SerialError('Unknown part in the serial pattern: {' + unknown[0] + '}. Use {n}, {bn}, {batch}, {yyyy}, {yy}, {mm}, {dd} or {plan}.')
    if '{n}' not in fmt and '{bn}' not in fmt:
        raise SerialError('The serial pattern needs {n} (running number) or {bn} (number in the batch), otherwise every voucher gets the same serial.')
    try:
        digits = int(digits)
    except (TypeError, ValueError):
        digits = 6
    digits = max(1, min(10, digits))
    reset = reset if reset in RESETS else 'never'
    return fmt, digits, reset


def next_number(business, reset=None, now=None):
    """The next running number, without taking it."""
    reset = reset or business.serial_reset
    if reset == 'batch':
        return 1
    return int((business.serial_counters or {}).get(period_key(reset, now), 0)) + 1


def allocate(business, count, *, fmt=None, digits=None, reset=None, start=None, batch=None, plan='', now=None):
    """Take `count` serials (in order). `start` jumps the running number to that value (never backwards
    for the saved counter, so numbers are not reused unless you ask for it). Must run in a transaction."""
    from .models import Business
    now = now or timezone.now()
    fmt, digits, reset = clean(fmt if fmt is not None else business.serial_format,
                               digits if digits is not None else business.serial_digits,
                               reset if reset is not None else business.serial_reset)
    count = max(0, int(count))
    batch_no = getattr(batch, 'pk', batch)
    with transaction.atomic():
        b = Business.objects.select_for_update().get(pk=business.pk)
        counters = dict(b.serial_counters or {})
        if reset == 'batch' and batch_no:
            first = int(start) if start else 1
            nums = list(range(first, first + count))
        else:
            key = period_key('never' if reset == 'batch' else reset, now)   # single vouchers (no batch) keep a running number
            first = int(start) if start else int(counters.get(key, 0)) + 1
            nums = list(range(first, first + count))
            if nums:
                counters[key] = max(int(counters.get(key, 0)), nums[-1])
                b.serial_counters = counters
                b.save(update_fields=['serial_counters'])
        business.serial_counters = b.serial_counters
    return [render(fmt, n=n, digits=digits, bn=i + 1, batch=batch_no, plan=plan, now=now) for i, n in enumerate(nums)]


def display(voucher):
    """Serial to print: the stored one, or the old id-based number for vouchers made before serials."""
    return voucher.serial or f'{voucher.pk:06d}'


def settings_ctx(business, data=None):
    data = data or {}
    return {'fmt': data.get('serial_format', business.serial_format) or '{n}',
            'digits': data.get('serial_digits', business.serial_digits),
            'reset': data.get('serial_reset', business.serial_reset),
            'start': data.get('serial_start', ''),
            'next': next_number(business), 'next_json': json.dumps({r: next_number(business, r) for r in RESETS}), 'resets': business._meta.get_field('serial_reset').choices,
            'counters': business.serial_counters or {}}
