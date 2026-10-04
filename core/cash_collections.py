"""Agent cash collection, batch allocation, balances and printable receipts.

The accounting rule is deliberately simple and auditable:

* CashCollection is the receipt/payment event.
* CashCollectionDetail stores the collection scope and before/after agent balance.
* CashCollectionAllocation stores every amount applied to a batch.
* A general hand-in is applied FIFO to the oldest batch balance first.
* Any remainder is a general/non-batch agent balance payment.
* A batch-specific hand-in can never exceed the currently collectible balance.
* A credit batch is "Up to date" when all *currently sold* proceeds have been
  handed in. It is "Collection complete" only when every voucher in that batch
  has been sold and all resulting proceeds have been handed in.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from .models import Agent, CashCollection, PAYMENT_METHODS, VoucherBatch
from .models_cash import CashCollectionAllocation, CashCollectionDetail


ZERO = Decimal('0.00')
TWOPLACES = Decimal('0.01')
PREPAID_NOTE = re.compile(r'^Paid upfront for batch (.+?) \(', re.I)


def money(value):
    try:
        return Decimal(value or 0).quantize(TWOPLACES)
    except (InvalidOperation, TypeError, ValueError):
        return ZERO


def agent_balance(agent):
    """All-time amount the agent owes, collected, and still outstanding."""
    business = agent.business
    sale = business.sales.filter(agent=agent).aggregate(
        gross=Sum('amount'),
        commission=Sum('commission'),
    )
    collected = business.collections.filter(agent=agent).aggregate(
        total=Sum('amount')
    )['total']

    gross = money(sale['gross'])
    commission = money(sale['commission'])
    owed = money(gross - commission)
    collected = money(collected)
    outstanding = money(owed - collected)

    return {
        'gross': gross,
        'commission': commission,
        'owed': owed,
        'collected': collected,
        'outstanding': outstanding,
    }


def _batch_raw(batch, agent=None):
    """Numbers used for allocation, without prepaid display shortcuts."""
    agent = agent or batch.agent

    if not agent:
        return {
            'due': ZERO,
            'allocated': ZERO,
            'remaining': ZERO,
            'sales': 0,
            'sold': 0,
            'total': batch.vouchers.count(),
        }

    sale = batch.business.sales.filter(
        agent=agent,
        voucher__batch=batch,
    ).aggregate(
        gross=Sum('amount'),
        commission=Sum('commission'),
        n=Count('pk'),
    )

    allocated = CashCollectionAllocation.objects.filter(
        batch=batch,
        detail__collection__agent=agent,
    ).aggregate(total=Sum('amount'))['total']

    counts = batch.vouchers.aggregate(
        total=Count('pk'),
        sold=Count('pk', filter=Q(sold_at__isnull=False)),
    )

    due = money(money(sale['gross']) - money(sale['commission']))
    allocated = money(allocated)
    remaining = money(max(ZERO, due - allocated))

    return {
        'due': due,
        'allocated': allocated,
        'remaining': remaining,
        'sales': sale['n'] or 0,
        'sold': counts['sold'] or 0,
        'total': counts['total'] or 0,
    }


def _expected_full(batch, agent):
    """Expected net remittance if every live voucher in the batch is sold."""
    if not agent:
        return ZERO

    from .finance import commission_for

    prices = list(
        batch.vouchers.values_list('price', flat=True)
    )
    gross = sum((money(p) for p in prices), ZERO)
    commission = sum(
        (money(commission_for(agent, p)) for p in prices),
        ZERO,
    )
    return money(gross - commission)


def batch_progress(batch, agent=None):
    """Financial collection state for one agent batch."""
    agent = agent or batch.agent
    raw = _batch_raw(batch, agent)

    expected = _expected_full(batch, agent) if agent else ZERO

    # A prepaid issue is settled at issue time. Normally its automatic
    # CashCollection has an allocation too, but this fallback keeps the UI
    # truthful even before an on_commit callback has run.
    if agent and batch.settlement == 'prepaid':
        collected = max(raw['allocated'], raw['due'])
        remaining = ZERO
        status = 'prepaid'
        label = 'Paid upfront'
    else:
        collected = raw['allocated']
        remaining = raw['remaining']

        if raw['due'] <= ZERO and raw['sold'] <= 0:
            status = 'waiting'
            label = 'No cash due yet'
        elif remaining <= ZERO:
            if raw['total'] and raw['sold'] >= raw['total']:
                status = 'complete'
                label = 'Collection complete'
            else:
                status = 'uptodate'
                label = 'Up to date'
        elif collected > ZERO:
            status = 'partial'
            label = 'Partially collected'
        else:
            status = 'due'
            label = 'Balance due'

    rate = (
        min(100.0, round(float(collected / raw['due'] * 100), 1))
        if raw['due'] > ZERO
        else 100.0
    )

    future = money(max(ZERO, expected - raw['due']))

    return {
        **raw,
        'collected': money(collected),
        'remaining': money(remaining),
        'expected': expected,
        'future': future,
        'status': status,
        'label': label,
        'rate': rate,
        'can_collect': bool(
            agent
            and batch.settlement != 'prepaid'
            and remaining > ZERO
        ),
    }


def _ordered_batches(agent):
    rows = list(
        agent.batches.select_related('plan').all()
    )
    rows.sort(
        key=lambda b: (
            b.issued_at or b.created_at,
            b.pk,
        )
    )
    return rows


def outstanding_batches(agent):
    """Oldest-first collectible batch balances for a general hand-in preview."""
    out = []
    for batch in _ordered_batches(agent):
        p = batch_progress(batch, agent)
        if (
            batch.settlement != 'prepaid'
            and p['remaining'] > ZERO
        ):
            out.append({
                'batch': batch,
                **p,
            })
    return out


def _result_after(batch, agent):
    p = batch_progress(batch, agent)

    if p['status'] == 'complete':
        return 'complete'
    if p['status'] == 'uptodate':
        return 'uptodate'
    if p['status'] == 'prepaid':
        return 'prepaid'
    if p['collected'] > ZERO:
        return 'partial'
    return 'due'


def _allocate(detail, batch, amount, *, source='general', sequence=0):
    """Apply up to ``amount`` to one batch. Returns amount actually applied."""
    collection = detail.collection
    agent = collection.agent
    raw = _batch_raw(batch, agent)
    before = raw['remaining']

    if before <= ZERO:
        return ZERO

    applied = money(min(money(amount), before))
    if applied <= ZERO:
        return ZERO

    allocation = CashCollectionAllocation.objects.create(
        detail=detail,
        batch=batch,
        batch_name=batch.name,
        amount=applied,
        balance_before=before,
        balance_after=money(before - applied),
        result='partial',
        sequence=sequence,
    )

    allocation.result = _result_after(batch, agent)
    allocation.save(update_fields=['result'])
    return applied


def _allocate_general(detail, amount):
    remaining_cash = money(amount)
    sequence = 1

    for row in outstanding_batches(detail.collection.agent):
        if remaining_cash <= ZERO:
            break
        applied = _allocate(
            detail,
            row['batch'],
            remaining_cash,
            source='general',
            sequence=sequence,
        )
        remaining_cash = money(remaining_cash - applied)
        if applied:
            sequence += 1

    detail.unallocated_amount = money(remaining_cash)
    detail.save(update_fields=['unallocated_amount'])
    return money(amount - remaining_cash)


def _prepaid_batch_from_note(collection):
    m = PREPAID_NOTE.search(collection.note or '')
    if not m:
        return None

    return collection.business.batches.filter(
        agent=collection.agent,
        name=m.group(1).strip(),
    ).order_by('-pk').first()


def ensure_collection_detail(collection_or_pk):
    """Create detail/allocation for a CashCollection made by an older code path."""
    pk = (
        collection_or_pk.pk
        if isinstance(collection_or_pk, CashCollection)
        else collection_or_pk
    )

    if not pk:
        return None

    existing = CashCollectionDetail.objects.filter(
        collection_id=pk
    ).first()
    if existing:
        return existing

    with transaction.atomic():
        collection = (
            CashCollection.objects
            .select_for_update()
            .select_related('agent', 'business')
            .filter(pk=pk)
            .first()
        )
        if collection is None:
            return None

        existing = CashCollectionDetail.objects.filter(
            collection=collection
        ).first()
        if existing:
            return existing

        # collection is already included in agent_balance(), so add it back to
        # obtain the exact pre-collection balance snapshot.
        after = agent_balance(collection.agent)['outstanding']
        before = money(after + collection.amount)

        prepaid = _prepaid_batch_from_note(collection)
        detail = CashCollectionDetail.objects.create(
            collection=collection,
            scope='prepaid' if prepaid else 'general',
            requested_batch=prepaid,
            requested_batch_name=prepaid.name if prepaid else '',
            agent_balance_before=before,
            agent_balance_after=after,
            unallocated_amount=collection.amount,
        )

        if prepaid:
            applied = _allocate(
                detail,
                prepaid,
                collection.amount,
                source='batch',
                sequence=1,
            )
            detail.unallocated_amount = money(
                collection.amount - applied
            )
            detail.save(update_fields=['unallocated_amount'])
        else:
            _allocate_general(detail, collection.amount)

        return detail


def collect(
    *,
    agent,
    amount,
    method='cash',
    reference='',
    note='',
    collected_at=None,
    user=None,
    batch=None,
):
    """Record a new general or batch-specific agent cash collection."""
    amount = money(amount)
    if amount <= ZERO:
        raise ValueError('Enter an amount above zero.')

    if method not in dict(PAYMENT_METHODS) or method == 'auto':
        method = 'cash'

    with transaction.atomic():
        agent = (
            Agent.objects
            .select_for_update()
            .select_related('business')
            .get(pk=agent.pk)
        )
        business = agent.business
        before = agent_balance(agent)['outstanding']

        if before <= ZERO:
            raise ValueError(
                f'{agent.name} has no outstanding cash balance to collect.'
            )

        if amount > before:
            raise ValueError(
                f'The most currently owed by {agent.name} is '
                f'{business.currency}{before:,.2f}.'
            )

        locked_batch = None
        if batch is not None:
            locked_batch = (
                VoucherBatch.objects
                .select_for_update()
                .filter(
                    pk=batch.pk,
                    business=business,
                    agent=agent,
                )
                .first()
            )
            if locked_batch is None:
                raise ValueError(
                    'That batch is no longer held by this agent.'
                )

            p = batch_progress(locked_batch, agent)
            if not p['can_collect']:
                raise ValueError(
                    f'{locked_batch.name} has no batch balance to collect right now.'
                )
            if amount > p['remaining']:
                raise ValueError(
                    f'{locked_batch.name} only has '
                    f'{business.currency}{p["remaining"]:,.2f} remaining to collect.'
                )

        default_note = (
            f'Cash collection for batch {locked_batch.name}'
            if locked_batch
            else 'General agent cash collection'
        )

        collection = CashCollection.objects.create(
            business=business,
            agent=agent,
            amount=amount,
            payment_method=method,
            reference=str(reference or '').strip()[:120],
            note=str(note or '').strip()[:255] or default_note,
            collected_at=collected_at or timezone.now(),
            recorded_by=user,
        )

        detail = CashCollectionDetail.objects.create(
            collection=collection,
            scope='batch' if locked_batch else 'general',
            requested_batch=locked_batch,
            requested_batch_name=locked_batch.name if locked_batch else '',
            agent_balance_before=before,
            agent_balance_after=money(before - amount),
            unallocated_amount=amount,
        )

        if locked_batch:
            applied = _allocate(
                detail,
                locked_batch,
                amount,
                source='batch',
                sequence=1,
            )
            detail.unallocated_amount = money(amount - applied)
            detail.save(update_fields=['unallocated_amount'])
        else:
            _allocate_general(detail, amount)

        allocations = list(
            detail.allocations.select_related('batch').order_by(
                'sequence',
                'pk',
            )
        )

        return {
            'collection': collection,
            'detail': detail,
            'allocations': allocations,
            'completed': [
                a for a in allocations
                if a.result in ('complete', 'uptodate', 'prepaid')
            ],
        }


def recent_batch_allocations(batch, limit=30):
    return (
        CashCollectionAllocation.objects
        .filter(batch=batch)
        .select_related(
            'detail__collection__agent',
            'detail__collection__recorded_by',
        )
        .order_by(
            '-detail__collection__collected_at',
            '-pk',
        )[:limit]
    )


def collection_receipt_context(collection):
    """Stable snapshot data for the printable cash receipt."""
    detail = ensure_collection_detail(collection)

    detail = (
        CashCollectionDetail.objects
        .select_related(
            'collection__agent',
            'collection__business',
            'collection__recorded_by',
            'requested_batch',
        )
        .prefetch_related('allocations__batch')
        .get(pk=detail.pk)
    )

    c = detail.collection
    allocations = list(
        detail.allocations.all().order_by('sequence', 'pk')
    )
    dt = timezone.localtime(c.collected_at)

    from .finance import amount_in_words

    return {
        'number': f'CRC-{dt:%Y%m%d}-{c.pk:06d}',
        'collection': c,
        'detail': detail,
        'allocations': allocations,
        'amount_words': amount_in_words(
            c.amount,
            c.business.currency,
        ),
        'completed': [
            a for a in allocations
            if a.result in ('complete', 'uptodate', 'prepaid')
        ],
        'scope_label': detail.get_scope_display(),
    }
