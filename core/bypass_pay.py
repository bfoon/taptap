"""Paid bypass access (IP binding).

A *bypassed* IP binding gives a device internet without a voucher, so switching one on is
selling access. Before a bypassed binding can be turned on, the cash (or mobile money) has
to be entered; it is booked as a sale, so it shows in Finance and every report like a
voucher sale. "No charge" (staff phone, printer, CCTV…) is allowed, but needs a reason and
is kept in the activity log.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.utils import timezone

from .models import PAYMENT_METHODS, VoucherSale

PLAN_PREFIX = 'Bypass access'
METHODS = {k for k, _ in PAYMENT_METHODS if k != 'auto'}


class PaymentNeeded(ValueError):
    pass


def parse(data):
    """Payment fields from a form/POST. Returns dict or raises PaymentNeeded with a readable reason."""
    free = str(data.get('pay_free', '')).lower() in ('1', 'true', 'on', 'yes')
    reason = str(data.get('pay_free_reason', '')).strip()[:200]
    raw = str(data.get('pay_amount', '')).strip().replace(',', '')
    if free:
        if not reason:
            raise PaymentNeeded('Give a reason for free access (e.g. staff phone, printer).')
        return {'amount': Decimal('0'), 'method': 'other', 'note': str(data.get('pay_note', '')).strip()[:200], 'free': True, 'reason': reason}
    if not raw:
        raise PaymentNeeded('Enter the payment first — the amount the customer paid for this access.')
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        raise PaymentNeeded('The amount must be a number.')
    if amount <= 0:
        raise PaymentNeeded('Enter the amount paid, or tick “No charge” with a reason.')
    method = str(data.get('pay_method', 'cash'))
    if method not in METHODS:
        method = 'cash'
    return {'amount': amount.quantize(Decimal('0.01')), 'method': method, 'note': str(data.get('pay_note', '')).strip()[:200],
            'free': False, 'reason': ''}


def has_payment(data):
    return bool(str(data.get('pay_amount', '')).strip()) or str(data.get('pay_free', '')).lower() in ('1', 'true', 'on', 'yes')


def book(business, binding, pay, user=None, hours=None, activated=True):
    """Book one device's payment as a sale (Finance). Free access is logged, not booked."""
    from .utils import log
    what = f'{PLAN_PREFIX}' + (f' · {hours} h' if hours else (' · no end date' if activated else ''))
    who = binding.comment or binding.mac_address
    if pay['free']:
        log(business, 'Bypass Access', f'{who} ({binding.mac_address}) free access on {binding.router.name}: {pay["reason"]}')
        return None
    sale = VoucherSale.objects.create(
        business=business, voucher=None, router=binding.router, plan_name=what[:120], voucher_code='',
        amount=pay['amount'], payment_method=pay['method'], customer_name=(binding.comment or '')[:120],
        reference=(binding.mac_address or '')[:120], notes=('IP binding ' + (binding.address or '') + (' — ' + pay['note'] if pay['note'] else '')).strip()[:255],
        sold_at=timezone.now(), recorded_by=user if getattr(user, 'is_authenticated', False) else None)
    log(business, 'Bypass Payment', f'{business.currency}{pay["amount"]} {sale.get_payment_method_display()} for {who} ({binding.mac_address}) on {binding.router.name}'
        + (f', {hours} h' if hours else ''))
    return sale


def last_payments(business, macs):
    """{MAC: {'amount', 'method', 'at', 'by'}} — the latest bypass payment of each device."""
    out = {}
    if not macs:
        return out
    qs = (VoucherSale.objects.filter(business=business, voucher__isnull=True, plan_name__startswith=PLAN_PREFIX, reference__in=list(macs))
          .select_related('recorded_by').order_by('-sold_at'))
    for s in qs[:2000]:
        if s.reference not in out:
            out[s.reference] = {'amount': f'{business.currency}{s.amount:,.2f}', 'method': s.get_payment_method_display(),
                                'at': s.sold_at.isoformat(), 'by': (s.recorded_by.get_full_name() or s.recorded_by.username) if s.recorded_by else ''}
    return out
