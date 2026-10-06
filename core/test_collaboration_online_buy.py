from datetime import timedelta
from decimal import Decimal
import hashlib
import hmac
import os
import time
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from . import business_collaboration as collab
from . import online_buy
from .models import Business, Router, VoucherPlan, VoucherSale
from .models_collaboration_buy import (
    BusinessCollaboration,
    OnlineStorefront,
    OnlineVoucherPurchase,
)
from .models_team import TeamMember
from .team import accessible_businesses


class BusinessCollaborationTests(TestCase):
    def setUp(self):
        self.u1 = User.objects.create_user(
            username="one@example.com",
            email="one@example.com",
            password="test",
        )
        self.u2 = User.objects.create_user(
            username="two@example.com",
            email="two@example.com",
            password="test",
        )
        self.b1 = Business.objects.create(
            user=self.u1,
            business_name="Business One",
            owner_name="One",
            phone="7000001",
            trial_ends_at=timezone.now() + timedelta(days=30),
            is_unlimited=True,
        )
        self.b2 = Business.objects.create(
            user=self.u2,
            business_name="Business Two",
            owner_name="Two",
            phone="7000002",
            trial_ends_at=timezone.now() + timedelta(days=30),
            is_unlimited=True,
        )

    def test_accept_creates_scoped_reciprocal_memberships(self):
        row = BusinessCollaboration.objects.create(
            source_business=self.b1,
            target_business=self.b2,
            source_grants=["dashboard.view", "vouchers.view"],
            invited_by=self.u1,
        )
        collab._activate(
            row,
            self.u2,
            ["dashboard.view", "reports.view"],
        )
        row.refresh_from_db()
        self.assertEqual(row.status, "active")

        into_one = TeamMember.objects.get(
            business=self.b1,
            user=self.u2,
        )
        into_two = TeamMember.objects.get(
            business=self.b2,
            user=self.u1,
        )
        self.assertEqual(into_one.role, "collaborator")
        self.assertEqual(
            set(into_one.extra_permissions),
            {"dashboard.view", "vouchers.view"},
        )
        self.assertNotIn("settings.manage", into_one.permissions)
        self.assertNotIn("team.manage", into_one.permissions)

        ids = {business.pk for business, _ in accessible_businesses(self.u2)}
        self.assertIn(self.b1.pk, ids)
        self.assertIn(self.b2.pk, ids)

    def test_end_removes_only_collaboration_memberships(self):
        row = BusinessCollaboration.objects.create(
            source_business=self.b1,
            target_business=self.b2,
            source_grants=["dashboard.view"],
            invited_by=self.u1,
        )
        collab._activate(row, self.u2, ["dashboard.view"])
        row.refresh_from_db()
        a = row.source_membership_id
        b = row.target_membership_id

        self.assertTrue(collab._end(row, self.u1))
        self.assertFalse(TeamMember.objects.filter(pk=a).exists())
        self.assertFalse(TeamMember.objects.filter(pk=b).exists())

    def test_existing_manual_team_membership_blocks_collaboration(self):
        TeamMember.objects.create(
            business=self.b1,
            user=self.u2,
            role="viewer",
            created_by=self.u1,
        )
        row = BusinessCollaboration.objects.create(
            source_business=self.b1,
            target_business=self.b2,
            source_grants=["dashboard.view"],
            invited_by=self.u1,
        )
        with self.assertRaises(ValueError):
            collab._activate(row, self.u2, ["dashboard.view"])


class OnlineBuyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="shop@example.com",
            email="shop@example.com",
            password="test",
        )
        self.business = Business.objects.create(
            user=self.user,
            business_name="Online Shop",
            owner_name="Owner",
            phone="7000010",
            trial_ends_at=timezone.now() + timedelta(days=30),
            is_unlimited=True,
        )
        self.router = Router.objects.create(
            business=self.business,
            name="Shop Router",
            ip_address="192.168.88.1",
            username="admin",
            password="secret",
        )
        self.plan = VoucherPlan.objects.create(
            business=self.business,
            name="Daily",
            price=Decimal("25.00"),
            duration_minutes=1440,
            duration_unit="days",
            max_devices=1,
        )
        self.store = OnlineStorefront.objects.create(
            business=self.business,
            enabled=True,
            router=self.router,
            plan_ids=[self.plan.pk],
        )

    @mock.patch("core.online_buy._after_issue")
    def test_verified_paid_purchase_creates_voucher_and_finance_sale(self, _after):
        purchase = OnlineVoucherPurchase.objects.create(
            business=self.business,
            storefront=self.store,
            plan=self.plan,
            router=self.router,
            provider="wave",
            amount=Decimal("25.00"),
            currency="GMD",
            customer_name="Customer",
            customer_phone="+2207000000",
            status="paid",
            paid_at=timezone.now(),
            provider_transaction_id="WAVE-TXN-1",
        )
        voucher = online_buy.issue_paid_purchase(purchase)
        purchase.refresh_from_db()

        self.assertEqual(purchase.status, "issued")
        self.assertEqual(purchase.voucher_id, voucher.pk)
        self.assertEqual(voucher.plan_name, "Daily")
        sale = VoucherSale.objects.get(voucher=voucher)
        self.assertEqual(sale.amount, Decimal("25.00"))
        self.assertEqual(sale.payment_method, "wave")
        self.assertEqual(sale.reference, "WAVE-TXN-1")

    @mock.patch("core.online_buy._after_issue")
    def test_provider_event_is_idempotent(self, _after):
        purchase = OnlineVoucherPurchase.objects.create(
            business=self.business,
            storefront=self.store,
            plan=self.plan,
            router=self.router,
            provider="wave",
            amount=Decimal("25.00"),
            currency="GMD",
        )
        event = {
            "reference": purchase.reference,
            "amount": "25",
            "currency": "GMD",
            "transaction_id": "TX1",
            "provider_session_id": "cos-test",
            "paid": True,
            "raw": {"type": "checkout.session.completed"},
        }
        first = online_buy.settle_provider_event("wave", event)
        second = online_buy.settle_provider_event("wave", event)
        self.assertEqual(first.voucher_id, second.voucher_id)
        self.assertEqual(
            VoucherSale.objects.filter(voucher_id=first.voucher_id).count(),
            1,
        )

    def test_amount_mismatch_goes_to_review(self):
        purchase = OnlineVoucherPurchase.objects.create(
            business=self.business,
            storefront=self.store,
            plan=self.plan,
            router=self.router,
            provider="wave",
            amount=Decimal("25.00"),
            currency="GMD",
        )
        with self.assertRaises(ValueError):
            online_buy.settle_provider_event(
                "wave",
                {
                    "reference": purchase.reference,
                    "amount": "20",
                    "currency": "GMD",
                    "transaction_id": "TX2",
                    "provider_session_id": "cos-test2",
                    "paid": True,
                    "raw": {},
                },
            )
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, "review")
        self.assertIsNone(purchase.voucher_id)

    def test_wave_webhook_signature_verification(self):
        secret = "wave_sn_WHS_testsecret"
        provider = online_buy.WaveProvider()
        raw = b'{"type":"checkout.session.completed"}'
        timestamp = str(int(time.time()))
        digest = hmac.new(
            secret.encode(),
            timestamp.encode() + raw,
            hashlib.sha256,
        ).hexdigest()
        header = f"t={timestamp},v1={digest}"

        with mock.patch.dict(
            os.environ,
            {"TAPTAP_WAVE_WEBHOOK_SECRET": secret},
            clear=False,
        ):
            self.assertTrue(provider._verify_signature(raw, header))

    def test_wave_is_off_until_currency_and_credentials_explicit(self):
        provider = online_buy.WaveProvider()
        with mock.patch.dict(os.environ, {}, clear=True):
            ok, reason = provider.configured_for(self.business)
        self.assertFalse(ok)
        self.assertTrue(reason)
