from decimal import Decimal
import re

from django.db import migrations, models
import django.db.models.deletion
from django.db.models import Count, Q, Sum


ZERO = Decimal('0.00')
PREPAID = re.compile(r'^Paid upfront for batch (.+?) \(', re.I)


def _d(value):
    return Decimal(value or 0).quantize(Decimal('0.01'))


def backfill(apps, schema_editor):
    CashCollection = apps.get_model('core', 'CashCollection')
    VoucherSale = apps.get_model('core', 'VoucherSale')
    VoucherBatch = apps.get_model('core', 'VoucherBatch')
    Voucher = apps.get_model('core', 'Voucher')
    Detail = apps.get_model('core', 'CashCollectionDetail')
    Allocation = apps.get_model('core', 'CashCollectionAllocation')

    agent_ids = (
        CashCollection.objects
        .values_list('agent_id', flat=True)
        .distinct()
    )

    for agent_id in agent_ids:
        collections = list(
            CashCollection.objects
            .filter(agent_id=agent_id)
            .order_by('collected_at', 'pk')
        )
        if not collections:
            continue

        business_id = collections[0].business_id

        sale_total = VoucherSale.objects.filter(
            business_id=business_id,
            agent_id=agent_id,
        ).aggregate(
            gross=Sum('amount'),
            commission=Sum('commission'),
        )
        total_owed = _d(
            _d(sale_total['gross'])
            - _d(sale_total['commission'])
        )

        batch_sales = (
            VoucherSale.objects
            .filter(
                business_id=business_id,
                agent_id=agent_id,
                voucher__batch_id__isnull=False,
            )
            .values('voucher__batch_id')
            .annotate(
                gross=Sum('amount'),
                commission=Sum('commission'),
            )
        )
        remaining = {
            row['voucher__batch_id']: _d(
                _d(row['gross']) - _d(row['commission'])
            )
            for row in batch_sales
        }

        batches = list(
            VoucherBatch.objects
            .filter(
                business_id=business_id,
                agent_id=agent_id,
            )
            .order_by('issued_at', 'created_at', 'pk')
        )
        by_id = {b.pk: b for b in batches}
        by_name = {}
        for b in batches:
            by_name.setdefault(b.name, b)

        counts = {
            row['batch_id']: row
            for row in (
                Voucher.objects
                .filter(
                    business_id=business_id,
                    batch_id__in=list(by_id),
                )
                .values('batch_id')
                .annotate(
                    total=Count('pk'),
                    sold=Count(
                        'pk',
                        filter=Q(sold_at__isnull=False),
                    ),
                )
            )
        }

        running_collected = ZERO

        for c in collections:
            if Detail.objects.filter(collection_id=c.pk).exists():
                running_collected += _d(c.amount)
                continue

            before = _d(total_owed - running_collected)
            after = _d(before - _d(c.amount))

            target = None
            m = PREPAID.search(c.note or '')
            if m:
                target = by_name.get(m.group(1).strip())

            detail = Detail.objects.create(
                collection_id=c.pk,
                scope='prepaid' if target else 'legacy',
                requested_batch_id=target.pk if target else None,
                requested_batch_name=target.name if target else '',
                agent_balance_before=before,
                agent_balance_after=after,
                unallocated_amount=_d(c.amount),
            )

            cash = _d(c.amount)
            sequence = 1

            ordered = [target] if target else []
            if not target:
                ordered = [
                    b for b in batches
                    if remaining.get(b.pk, ZERO) > ZERO
                ]

            for b in ordered:
                if cash <= ZERO:
                    break

                due = remaining.get(b.pk, ZERO)
                if due <= ZERO:
                    continue

                applied = min(cash, due)
                new_due = _d(due - applied)
                remaining[b.pk] = new_due

                cc = counts.get(
                    b.pk,
                    {'total': 0, 'sold': 0},
                )
                if b.settlement == 'prepaid':
                    result = 'prepaid'
                elif new_due <= ZERO:
                    result = (
                        'complete'
                        if cc['total'] and cc['sold'] >= cc['total']
                        else 'uptodate'
                    )
                else:
                    result = 'partial'

                Allocation.objects.create(
                    detail_id=detail.pk,
                    batch_id=b.pk,
                    batch_name=b.name,
                    amount=applied,
                    balance_before=due,
                    balance_after=new_due,
                    result=result,
                    sequence=sequence,
                )
                sequence += 1
                cash = _d(cash - applied)

            detail.unallocated_amount = cash
            detail.save(update_fields=['unallocated_amount'])
            running_collected += _d(c.amount)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0070_router_geolocation'),
    ]

    operations = [
        migrations.CreateModel(
            name='CashCollectionDetail',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'scope',
                    models.CharField(
                        choices=[
                            ('general', 'General agent balance'),
                            ('batch', 'Specific batch'),
                            ('prepaid', 'Paid upfront batch'),
                            ('legacy', 'Earlier collection'),
                        ],
                        default='general',
                        max_length=12,
                    ),
                ),
                (
                    'requested_batch_name',
                    models.CharField(
                        blank=True,
                        max_length=120,
                    ),
                ),
                (
                    'agent_balance_before',
                    models.DecimalField(
                        decimal_places=2,
                        default=0,
                        max_digits=12,
                    ),
                ),
                (
                    'agent_balance_after',
                    models.DecimalField(
                        decimal_places=2,
                        default=0,
                        max_digits=12,
                    ),
                ),
                (
                    'unallocated_amount',
                    models.DecimalField(
                        decimal_places=2,
                        default=0,
                        help_text='Part of a general collection covering non-batch agent sales / balance.',
                        max_digits=12,
                    ),
                ),
                (
                    'created_at',
                    models.DateTimeField(
                        auto_now_add=True,
                    ),
                ),
                (
                    'collection',
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='cash_detail',
                        to='core.cashcollection',
                    ),
                ),
                (
                    'requested_batch',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='requested_cash_collections',
                        to='core.voucherbatch',
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name='CashCollectionAllocation',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'batch_name',
                    models.CharField(max_length=120),
                ),
                (
                    'amount',
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=12,
                    ),
                ),
                (
                    'balance_before',
                    models.DecimalField(
                        decimal_places=2,
                        default=0,
                        max_digits=12,
                    ),
                ),
                (
                    'balance_after',
                    models.DecimalField(
                        decimal_places=2,
                        default=0,
                        max_digits=12,
                    ),
                ),
                (
                    'result',
                    models.CharField(
                        choices=[
                            ('due', 'Balance due'),
                            ('partial', 'Partially collected'),
                            ('uptodate', 'Up to date'),
                            ('complete', 'Collection complete'),
                            ('prepaid', 'Paid upfront'),
                        ],
                        default='partial',
                        max_length=12,
                    ),
                ),
                (
                    'sequence',
                    models.PositiveSmallIntegerField(
                        default=0,
                    ),
                ),
                (
                    'created_at',
                    models.DateTimeField(
                        auto_now_add=True,
                    ),
                ),
                (
                    'batch',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='cash_allocations',
                        to='core.voucherbatch',
                    ),
                ),
                (
                    'detail',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='allocations',
                        to='core.cashcollectiondetail',
                    ),
                ),
            ],
            options={
                'ordering': [
                    'detail_id',
                    'sequence',
                    'id',
                ],
            },
        ),
        migrations.AddIndex(
            model_name='cashcollectionallocation',
            index=models.Index(
                fields=['batch', 'created_at'],
                name='cashalloc_batch_date_idx',
            ),
        ),
        migrations.RunPython(
            backfill,
            migrations.RunPython.noop,
        ),
    ]
