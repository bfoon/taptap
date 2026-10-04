"""Extra finance models for auditable agent cash collection.

CashCollection remains the actual receipt/payment event in core.models.
These models explain *where that money went* without changing the historical
CashCollection table or the rest of TapTap's finance totals.
"""
from django.db import models, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import CashCollection


class CashCollectionDetail(models.Model):
    SCOPE = [
        ('general', 'General agent balance'),
        ('batch', 'Specific batch'),
        ('prepaid', 'Paid upfront batch'),
        ('legacy', 'Earlier collection'),
    ]

    collection = models.OneToOneField(
        'core.CashCollection',
        on_delete=models.CASCADE,
        related_name='cash_detail',
    )
    scope = models.CharField(
        max_length=12,
        choices=SCOPE,
        default='general',
    )
    requested_batch = models.ForeignKey(
        'core.VoucherBatch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='requested_cash_collections',
    )
    requested_batch_name = models.CharField(
        max_length=120,
        blank=True,
    )
    agent_balance_before = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
    )
    agent_balance_after = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
    )
    unallocated_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text='Part of a general collection covering non-batch agent sales / balance.',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'core'

    def __str__(self):
        return f'Collection {self.collection_id} ({self.scope})'


class CashCollectionAllocation(models.Model):
    RESULT = [
        ('due', 'Balance due'),
        ('partial', 'Partially collected'),
        ('uptodate', 'Up to date'),
        ('complete', 'Collection complete'),
        ('prepaid', 'Paid upfront'),
    ]

    detail = models.ForeignKey(
        CashCollectionDetail,
        on_delete=models.CASCADE,
        related_name='allocations',
    )
    batch = models.ForeignKey(
        'core.VoucherBatch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cash_allocations',
    )
    batch_name = models.CharField(
        max_length=120,
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    balance_before = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
    )
    balance_after = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
    )
    result = models.CharField(
        max_length=12,
        choices=RESULT,
        default='partial',
    )
    sequence = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'core'
        ordering = ['detail_id', 'sequence', 'id']
        indexes = [
            models.Index(
                fields=['batch', 'created_at'],
                name='cashalloc_batch_date_idx',
            ),
        ]

    def __str__(self):
        return f'{self.batch_name}: {self.amount}'


@receiver(
    post_save,
    sender=CashCollection,
    dispatch_uid='taptap_cash_collection_auto_allocate',
)
def _auto_allocate_direct_collection(sender, instance, created, **kwargs):
    """Make old/direct CashCollection.create() calls participate in the new ledger.

    New collection forms create the detail themselves inside a transaction. For
    older code paths (including prepaid batch issue and the Finance tab's legacy
    form), this callback runs after commit and fills the detail automatically.
    """
    if not created:
        return

    pk = instance.pk

    def after_commit():
        from .cash_collections import ensure_collection_detail
        ensure_collection_detail(pk)

    transaction.on_commit(after_commit)
