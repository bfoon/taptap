"""Regression tests for member arrears/payment accounting."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from . import member_arrears
from .models import Business, Voucher
from .models_member_arrears import MemberBalancePayment
from .models_member_plans import MemberPlan, MemberPlanAssignment, MemberRenewal


class MemberArrearsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="arrears@example.com",
            email="arrears@example.com",
            password="pw12345678",
        )
        self.business = Business.objects.create(
            user=self.user,
            business_name="Arrears Test",
            owner_name="Owner",
            phone="2200000",
            trial_ends_at=timezone.now() + timedelta(days=30),
            is_unlimited=True,
        )
        self.plan = MemberPlan.objects.create(
            business=self.business,
            name="Monthly",
            price=Decimal("1000.00"),
            duration_minutes=43200,
            duration_unit="months",
            max_devices=2,
            speed_limit="5M/5M",
        )
        self.member = Voucher.objects.create(
            business=self.business,
            code="jimmy",
            login_type="member",
            plan_name=self.plan.name,
            price=self.plan.price,
            duration_minutes=43200,
            max_devices=2,
            rate_limit="5M/5M",
            source="taptap",
            status="active",
            used_at=timezone.now() - timedelta(days=4),
            expires_at=timezone.now() + timedelta(days=26),
        )
        MemberPlanAssignment.objects.create(
            member=self.member,
            plan=self.plan,
            assigned_by=self.user,
        )
        self.renewal = MemberRenewal.objects.create(
            business=self.business,
            member=self.member,
            plan=self.plan,
            member_username=self.member.code,
            customer_name="Jimmy",
            plan_name=self.plan.name,
            plan_price=Decimal("1000.00"),
            amount_collected=Decimal("600.00"),
            currency="D",
            payment_method="cash",
            duration_minutes=43200,
            max_devices=2,
            speed_limit="5M/5M",
            old_expires_at=timezone.now(),
            new_expires_at=self.member.expires_at,
            recorded_by=self.user,
        )

    def test_partial_renewal_becomes_arrears(self):
        self.assertEqual(
            member_arrears.total_arrears(self.member),
            Decimal("400.00"),
        )

    @mock.patch("core.finance.record_sale", return_value=None)
    @mock.patch("core.voucher_history.record")
    def test_balance_collection_does_not_change_expiry(self, _record, _sale):
        before = self.member.expires_at
        payment = member_arrears.collect_balance(
            self.member,
            amount="250",
            method="cash",
            user=self.user,
        )
        self.member.refresh_from_db()
        self.assertEqual(self.member.expires_at, before)
        self.assertEqual(payment.balance_before, Decimal("400.00"))
        self.assertEqual(payment.balance_after, Decimal("150.00"))
        self.assertEqual(
            member_arrears.total_arrears(self.member),
            Decimal("150.00"),
        )

    @mock.patch("core.finance.record_sale", return_value=None)
    @mock.patch("core.voucher_history.record")
    def test_final_balance_clears_arrears(self, _record, _sale):
        member_arrears.collect_balance(
            self.member,
            amount="400",
            user=self.user,
        )
        self.assertEqual(
            member_arrears.total_arrears(self.member),
            Decimal("0.00"),
        )
        self.assertEqual(
            MemberBalancePayment.objects.filter(member=self.member).count(),
            1,
        )

    def test_cannot_over_collect(self):
        with self.assertRaises(member_arrears.BalanceError):
            member_arrears.collect_balance(
                self.member,
                amount="401",
                user=self.user,
            )

    def test_progress_bar_uses_one_plan_period(self):
        summary = member_arrears.summaries_for_members(
            [self.member],
            now=timezone.now(),
        )[self.member.pk]
        self.assertIsNotNone(summary["progress"])
        self.assertGreaterEqual(summary["progress"], 80)
        self.assertLessEqual(summary["progress"], 100)
