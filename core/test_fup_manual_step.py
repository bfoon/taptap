from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from . import fair_usage as fu
from . import fup_manual
from .models import Business, Router, Voucher, VoucherPlan
from .models_fup import FairUsagePolicy, FairUsageState
from .models_fup_manual import FairUsageManualStep


class FairUsageManualRollbackTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="fup-owner@example.com",
            email="fup-owner@example.com",
            password="test",
        )
        self.business = Business.objects.create(
            user=self.user,
            business_name="FUP Test",
            owner_name="Owner",
            phone="7000000",
            trial_ends_at=timezone.now() + timedelta(days=30),
            is_unlimited=True,
        )
        self.plan = VoucherPlan.objects.create(
            business=self.business,
            name="Daily",
            price=25,
            duration_minutes=1440,
            duration_unit="days",
            max_devices=1,
        )
        self.router = Router.objects.create(
            business=self.business,
            name="Test Router",
            ip_address="192.168.88.1",
            username="admin",
            password="secret",
        )
        self.voucher = Voucher.objects.create(
            business=self.business,
            router=self.router,
            code="FUPROLL1",
            plan_name="Daily",
            price=25,
            duration_minutes=1440,
            max_devices=1,
            status="active",
        )
        self.policy = FairUsagePolicy.objects.create(
            business=self.business,
            name="Strict",
            period="day",
            counts="total",
            tiers=[
                {"gb": 1, "down": 5, "up": 2},
                {"gb": 2, "down": 2, "up": 1},
                {"gb": 3, "down": 0.5, "up": 0.25},
            ],
        )
        self.policy.plans.add(self.plan)
        key = fu.window(self.policy, self.voucher)[1]
        self.state = FairUsageState.objects.create(
            voucher=self.voucher,
            policy=self.policy,
            period_key=key,
            used_bytes=3 * fu.GB,
            tier=3,
            device="",
        )

    def test_can_roll_back_step_3_to_step_2(self):
        override, auto = fup_manual.set_manual_step(
            self.state,
            2,
            self.user,
        )
        self.assertEqual(auto, 3)
        self.assertEqual(override.tier, 2)
        self.assertEqual(fup_manual.effective_tier(self.state), 2)
        self.state.refresh_from_db()
        self.assertEqual(self.state.used_bytes, 3 * fu.GB)

    def test_step_zero_is_full_speed_without_resetting_usage(self):
        fup_manual.set_manual_step(self.state, 0, self.user)
        self.assertEqual(fup_manual.effective_tier(self.state), 0)
        self.state.refresh_from_db()
        self.assertEqual(self.state.used_bytes, 3 * fu.GB)

    def test_cannot_choose_a_step_above_natural_step(self):
        self.state.used_bytes = int(1.2 * fu.GB)
        self.state.save(update_fields=["used_bytes"])
        with self.assertRaises(ValueError):
            fup_manual.set_manual_step(self.state, 2, self.user)

    def test_automatic_removes_override(self):
        fup_manual.set_manual_step(self.state, 1, self.user)
        self.assertTrue(
            FairUsageManualStep.objects.filter(state=self.state).exists()
        )
        fup_manual.clear_manual_step(self.state, self.user)
        self.assertFalse(
            FairUsageManualStep.objects.filter(state=self.state).exists()
        )
        self.assertEqual(fup_manual.effective_tier(self.state), 3)

    def test_step_one_replaces_automatic_router_cap(self):
        fup_manual.set_manual_step(self.state, 1, self.user)
        ip = "172.16.0.55"
        name = fu._qname(self.voucher.code, ip)
        automatic_caps = {
            name: (
                ip,
                f"{fu.kbps(0.25)}k/{fu.kbps(0.5)}k",
                self.voucher.code,
                3,
            )
        }
        result = fup_manual._apply_manual_caps(
            self.router,
            [
                {
                    "user": self.voucher.code,
                    "address": ip,
                    "mac-address": "AA:BB:CC:DD:EE:01",
                }
            ],
            automatic_caps,
        )
        self.assertEqual(result[name][3], 1)
        self.assertEqual(
            result[name][1],
            f"{fu.kbps(2)}k/{fu.kbps(5)}k",
        )

    def test_step_zero_removes_router_cap(self):
        fup_manual.set_manual_step(self.state, 0, self.user)
        ip = "172.16.0.55"
        name = fu._qname(self.voucher.code, ip)
        result = fup_manual._apply_manual_caps(
            self.router,
            [
                {
                    "user": self.voucher.code,
                    "address": ip,
                    "mac-address": "AA:BB:CC:DD:EE:01",
                }
            ],
            {
                name: (
                    ip,
                    f"{fu.kbps(0.25)}k/{fu.kbps(0.5)}k",
                    self.voucher.code,
                    3,
                )
            },
        )
        self.assertNotIn(name, result)

    def test_old_period_override_is_ignored(self):
        FairUsageManualStep.objects.create(
            state=self.state,
            period_key="d19990101",
            tier=1,
            set_by=self.user,
        )
        self.assertIsNone(
            fup_manual._valid_override(
                self.state,
                delete_stale=True,
            )
        )
        self.assertFalse(
            FairUsageManualStep.objects.filter(state=self.state).exists()
        )
