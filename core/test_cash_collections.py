"""Agent cash collection allocation tests.

Run:
    docker compose exec web python manage.py test core.test_cash_collections core.test_statements
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.db import models
from django.urls import reverse
from django.utils import timezone

from .cash_collections import (
    batch_progress,
    collect,
    collection_receipt_context,
)
from .finance import record_sale
from .models import Agent, Business, CashCollection, Voucher, VoucherBatch, VoucherPlan
from .models_cash import CashCollectionAllocation, CashCollectionDetail


HTML = {'HTTP_ACCEPT': 'text/html'}


@override_settings(AUTH_EMAIL_OTP=False)
class CashCollectionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            'cash@example.com',
            'cash@example.com',
            'pw',
            first_name='Cashier',
        )
        self.biz = Business.objects.create(
            user=self.owner,
            business_name='TapTap',
            owner_name='Owner',
            phone='1',
            trial_ends_at=timezone.now() + timedelta(days=30),
            is_unlimited=True,
        )
        self.plan = VoucherPlan.objects.create(
            business=self.biz,
            name='Daily',
            price=Decimal('25.00'),
            duration_minutes=1440,
        )
        self.agent = Agent.objects.create(
            business=self.biz,
            name='Garage Agent',
            phone='7000000',
            commission_percent=Decimal('10.0000'),
        )
        self.client.force_login(self.owner)

    def make_batch(self, name, count, sold):
        batch = VoucherBatch.objects.create(
            business=self.biz,
            name=name,
            plan=self.plan,
            quantity=count,
            agent=self.agent,
            settlement='credit',
            issued_at=timezone.now(),
        )
        vouchers = []
        for i in range(count):
            vouchers.append(
                Voucher.objects.create(
                    business=self.biz,
                    batch=batch,
                    agent=self.agent,
                    code=f'{name}-{i}',
                    plan_name=self.plan.name,
                    price=self.plan.price,
                    duration_minutes=1440,
                )
            )
        for voucher in vouchers[:sold]:
            record_sale(
                self.biz,
                voucher,
                agent=self.agent,
                user=self.owner,
            )
        return batch

    def test_general_collection_fills_oldest_batch_then_next(self):
        first = self.make_batch('BATCH-A', 2, 2)
        second = self.make_batch('BATCH-B', 1, 1)

        # Net due is D22.50 per sold D25 voucher at 10% commission.
        result = collect(
            agent=self.agent,
            amount=Decimal('50.00'),
            method='cash',
            user=self.owner,
        )

        allocations = list(
            result['detail'].allocations.order_by('sequence')
        )
        self.assertEqual(len(allocations), 2)

        self.assertEqual(allocations[0].batch_id, first.id)
        self.assertEqual(allocations[0].amount, Decimal('45.00'))
        self.assertEqual(allocations[0].balance_after, Decimal('0.00'))
        self.assertEqual(allocations[0].result, 'complete')

        self.assertEqual(allocations[1].batch_id, second.id)
        self.assertEqual(allocations[1].amount, Decimal('5.00'))
        self.assertEqual(allocations[1].balance_after, Decimal('17.50'))
        self.assertEqual(allocations[1].result, 'partial')

        p1 = batch_progress(first)
        p2 = batch_progress(second)
        self.assertEqual(p1['status'], 'complete')
        self.assertEqual(p1['remaining'], Decimal('0.00'))
        self.assertEqual(p2['status'], 'partial')
        self.assertEqual(p2['remaining'], Decimal('17.50'))

    def test_batch_specific_collection_completes_batch(self):
        batch = self.make_batch('BATCH-C', 1, 1)

        result = collect(
            agent=self.agent,
            batch=batch,
            amount=Decimal('22.50'),
            method='wave',
            reference='WAVE-77',
            user=self.owner,
        )

        allocation = result['allocations'][0]
        self.assertEqual(allocation.batch_id, batch.id)
        self.assertEqual(allocation.result, 'complete')

        progress = batch_progress(batch)
        self.assertEqual(progress['collected'], Decimal('22.50'))
        self.assertEqual(progress['remaining'], Decimal('0.00'))
        self.assertEqual(progress['label'], 'Collection complete')

    def test_unsold_batch_can_be_up_to_date_without_being_complete(self):
        batch = self.make_batch('BATCH-D', 3, 1)

        collect(
            agent=self.agent,
            batch=batch,
            amount=Decimal('22.50'),
            user=self.owner,
        )

        progress = batch_progress(batch)
        self.assertEqual(progress['status'], 'uptodate')
        self.assertEqual(progress['label'], 'Up to date')
        self.assertEqual(progress['remaining'], Decimal('0.00'))
        self.assertGreater(progress['future'], Decimal('0.00'))

    def test_batch_collection_cannot_exceed_batch_balance(self):
        batch = self.make_batch('BATCH-E', 1, 1)

        with self.assertRaisesMessage(
            ValueError,
            'only has D22.50 remaining',
        ):
            collect(
                agent=self.agent,
                batch=batch,
                amount=Decimal('23.00'),
                user=self.owner,
            )

    def test_general_collection_cannot_exceed_agent_total_owed(self):
        self.make_batch('BATCH-F', 1, 1)

        with self.assertRaisesMessage(
            ValueError,
            'most currently owed',
        ):
            collect(
                agent=self.agent,
                amount=Decimal('30.00'),
                user=self.owner,
            )

    def test_cash_receipt_shows_allocation_and_balance(self):
        batch = self.make_batch('BATCH-G', 1, 1)
        result = collect(
            agent=self.agent,
            amount=Decimal('22.50'),
            method='cash',
            user=self.owner,
        )

        url = (
            reverse('agent_detail', args=[self.agent.pk])
            + f'?receipt={result["collection"].pk}'
        )
        response = self.client.get(url, **HTML)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Cash Collection Receipt')
        self.assertContains(response, 'BATCH-G')
        self.assertContains(response, 'Collection complete')
        self.assertContains(response, 'D22.50')
        self.assertContains(response, 'CRC-')

    def test_agent_page_posts_general_collection_and_opens_receipt(self):
        self.make_batch('BATCH-H', 1, 1)

        response = self.client.post(
            reverse('agent_detail', args=[self.agent.pk]),
            {
                'action': 'collect_cash',
                'amount': '10.00',
                'method': 'cash',
                'collected_at': timezone.localdate().isoformat(),
                'next': reverse('agent_detail', args=[self.agent.pk]),
            },
            **HTML,
        )

        self.assertEqual(response.status_code, 302)
        collection = CashCollection.objects.latest('pk')
        self.assertIn(
            f'receipt={collection.pk}',
            response['Location'],
        )
        self.assertTrue(
            CashCollectionDetail.objects.filter(
                collection=collection,
                scope='general',
            ).exists()
        )

    def test_agent_page_posts_batch_specific_collection(self):
        batch = self.make_batch('BATCH-I', 1, 1)

        response = self.client.post(
            reverse('agent_detail', args=[self.agent.pk]),
            {
                'action': 'collect_cash',
                'batch': str(batch.pk),
                'amount': '22.50',
                'method': 'cash',
                'next': reverse('batch_detail', args=[batch.pk]),
            },
            **HTML,
        )

        self.assertEqual(response.status_code, 302)
        allocation = CashCollectionAllocation.objects.latest('pk')
        self.assertEqual(allocation.batch_id, batch.id)
        self.assertEqual(allocation.result, 'complete')

    def test_batch_page_has_collection_controls(self):
        batch = self.make_batch('BATCH-J', 2, 1)

        response = self.client.get(
            reverse('batch_detail', args=[batch.pk]),
            **HTML,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Cash collection')
        self.assertContains(response, 'Collect from this batch')
        self.assertContains(response, 'Remaining to collect now')

    def test_direct_legacy_collection_auto_gets_detail(self):
        self.make_batch('BATCH-K', 1, 1)

        with self.captureOnCommitCallbacks(execute=True):
            collection = CashCollection.objects.create(
                business=self.biz,
                agent=self.agent,
                amount=Decimal('10.00'),
                payment_method='cash',
                recorded_by=self.owner,
            )

        self.assertTrue(
            CashCollectionDetail.objects.filter(
                collection=collection,
            ).exists()
        )
        self.assertEqual(
            CashCollectionAllocation.objects.filter(
                detail__collection=collection,
            ).aggregate(total=models.Sum('amount'))['total'],
            Decimal('10.00'),
        )
