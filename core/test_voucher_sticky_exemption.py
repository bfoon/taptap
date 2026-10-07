"""Regression tests for per-voucher sticky-session exemption."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from . import voucher_sticky_exemption as sticky
from .models import (
    AgentCommand,
    Business,
    DeviceSignature,
    Router,
    Voucher,
    VoucherDeviceBinding,
)
from .models_voucher_sticky import VoucherStickyExemption


class VoucherStickyExemptionTests(TestCase):
    def setUp(self):
        sticky.install()

        self.user = User.objects.create_user(
            username="sticky-owner@example.com",
            email="sticky-owner@example.com",
            password="pw12345678",
        )
        self.business = Business.objects.create(
            user=self.user,
            business_name="Sticky Voucher Test",
            owner_name="Owner",
            phone="2200002",
            trial_ends_at=timezone.now() + timedelta(days=30),
            is_unlimited=True,
        )
        self.router = Router.objects.create(
            business=self.business,
            name="TapTap K",
            ip_address="",
            username="",
            password="",
            connection_mode="agent",
            status="Online",
        )
        self.voucher = Voucher.objects.create(
            business=self.business,
            router=self.router,
            code="STICKY-ONE",
            plan_name="Daily",
            price="25.00",
            duration_minutes=1440,
            max_devices=1,
            source="taptap",
            status="active",
        )

    def test_indefinite_exemption_is_active(self):
        sticky.set_exemption(
            self.voucher,
            user=self.user,
            reason="show portal every time",
        )
        self.voucher.refresh_from_db()
        self.assertTrue(sticky.is_exempt(self.voucher))
        self.assertIsNone(self.voucher.sticky_exemption.expires_at)
        self.assertEqual(
            self.voucher.sticky_exemption.reason,
            "show portal every time",
        )

    def test_temporary_exemption_expires_automatically(self):
        row = VoucherStickyExemption.objects.create(
            voucher=self.voucher,
            expires_at=timezone.now() - timedelta(seconds=1),
            created_by=self.user,
            updated_by=self.user,
        )
        self.voucher.refresh_from_db()
        self.assertFalse(sticky.is_exempt(self.voucher))

        row.expires_at = timezone.now() + timedelta(hours=1)
        row.save(update_fields=["expires_at", "updated_at"])
        self.voucher.refresh_from_db()
        self.assertTrue(sticky.is_exempt(self.voucher))

    def test_restore_returns_to_business_setting(self):
        sticky.set_exemption(self.voucher, user=self.user)
        self.assertTrue(sticky.restore_normal(self.voucher, user=self.user))
        self.assertFalse(
            VoucherStickyExemption.objects.filter(voucher=self.voucher).exists()
        )

    def test_link_command_removes_cookie_not_live_session(self):
        from .agent import command_body

        cmd = AgentCommand.objects.create(
            router=self.router,
            kind="hotspot_user_cookies_remove",
            params={"user": self.voucher.code},
            label="forget sticky",
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        body = command_body(cmd)
        self.assertIn("/ip hotspot cookie remove", body)
        self.assertNotIn("/ip hotspot active remove", body)

    @mock.patch("core.sticky_exclusions.clear_sticky_cookie.apply_async")
    def test_exempt_login_schedules_cookie_removal(self, apply_async):
        sticky.set_exemption(self.voucher, user=self.user)

        sig = DeviceSignature.objects.create(
            business=self.business,
            fingerprint="a" * 32,
            last_mac="AA:BB:CC:DD:EE:FF",
            macs=["AA:BB:CC:DD:EE:FF"],
            vouchers=[self.voucher.code],
        )

        from django.core.cache import cache
        cache.delete(
            f"voucher-sticky:{self.voucher.pk}:AA:BB:CC:DD:EE:FF"
        )
        sticky.schedule_after_login(sig)

        calls = [
            c for c in apply_async.call_args_list
            if c.kwargs.get("args") == [
                self.voucher.pk,
                "AA:BB:CC:DD:EE:FF",
            ]
        ]
        self.assertTrue(calls)
        self.assertEqual(
            sorted(c.kwargs["countdown"] for c in calls[-3:]),
            [5, 18, 45],
        )

    @mock.patch("core.sticky_exclusions.clear_sticky_cookie.apply_async")
    def test_normal_voucher_does_not_schedule_cookie_removal(self, apply_async):
        sig = DeviceSignature.objects.create(
            business=self.business,
            fingerprint="b" * 32,
            last_mac="11:22:33:44:55:66",
            macs=["11:22:33:44:55:66"],
            vouchers=[self.voucher.code],
        )
        apply_async.reset_mock()

        count = sticky.schedule_after_login(sig)

        self.assertEqual(count, 0)
        apply_async.assert_not_called()

    def test_exemption_never_releases_device_slot(self):
        binding = VoucherDeviceBinding.objects.create(
            business=self.business,
            voucher=self.voucher,
            slot_no=1,
            current_mac="AA:BB:CC:DD:EE:FF",
            locked_by="portal",
        )

        sticky.set_exemption(self.voucher, user=self.user)

        self.assertTrue(
            VoucherDeviceBinding.objects.filter(pk=binding.pk).exists()
        )
