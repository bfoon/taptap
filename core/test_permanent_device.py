from unittest.mock import patch
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from . import device_lock, permanent_device, shared_use, voucher_history
from .models import Business, Voucher, VoucherDeviceBinding
from .models_permanent_device import PermanentVoucherDevice


class PermanentVoucherDeviceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="permanent-owner@example.com",
            email="permanent-owner@example.com",
            password="test",
        )
        self.business = Business.objects.create(
            user=self.user,
            business_name="Permanent Device Test",
            owner_name="Owner",
            phone="7000000",
            trial_ends_at=timezone.now() + timedelta(days=30),
            is_unlimited=True,
        )
        self.voucher = Voucher.objects.create(
            business=self.business,
            code="PERM001",
            plan_name="Daily",
            price=25,
            duration_minutes=1440,
            max_devices=1,
            status="active",
        )
        self.binding = VoucherDeviceBinding.objects.create(
            business=self.business,
            voucher=self.voucher,
            slot_no=1,
            device_token_hash="stable-phone-id",
            current_mac="AA:BB:CC:DD:EE:01",
            label="Customer phone",
            locked_by="portal",
        )

    def make_permanent(self):
        with patch("core.permanent_device._pin", return_value="AA:BB:CC:DD:EE:01"):
            return permanent_device.make_permanent(self.voucher, self.user)

    def test_make_permanent_marks_existing_binding(self):
        binding, created = self.make_permanent()
        self.assertTrue(created)
        self.assertEqual(binding.pk, self.binding.pk)
        self.assertTrue(
            PermanentVoucherDevice.objects.filter(binding=self.binding).exists()
        )
        self.assertTrue(permanent_device.is_permanent(self.voucher))

    def test_only_single_device_vouchers_can_be_permanent(self):
        self.voucher.max_devices = 5
        self.voucher.save(update_fields=["max_devices"])
        with self.assertRaises(ValueError):
            permanent_device.make_permanent(self.voucher, self.user)

    def test_unknown_device_is_denied_before_normal_slot_replacement(self):
        self.make_permanent()
        outcome = device_lock.claim(
            self.voucher,
            mac="AA:BB:CC:DD:EE:99",
            fp="another-phone-id",
            source="portal",
        )
        self.assertFalse(outcome.allowed)
        self.assertEqual(outcome.status, "denied")
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.current_mac, "AA:BB:CC:DD:EE:01")

    def test_matching_stable_device_id_can_follow_private_mac_change(self):
        self.make_permanent()
        with patch("core.device_lock._router_mac") as router_mac:
            outcome = device_lock.claim(
                self.voucher,
                mac="AA:BB:CC:DD:EE:02",
                fp="stable-phone-id",
                source="portal",
            )
        self.assertTrue(outcome.allowed)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.current_mac, "AA:BB:CC:DD:EE:02")
        self.assertEqual(self.binding.previous_mac, "AA:BB:CC:DD:EE:01")
        router_mac.assert_called_with(self.voucher, "AA:BB:CC:DD:EE:02")

    def test_shared_use_warning_check_is_bypassed_while_permanent(self):
        self.make_permanent()
        self.assertFalse(shared_use.check_after_login(self.business, self.voucher))

    def test_reset_devices_cannot_remove_permanent_binding(self):
        self.make_permanent()
        ok, message = voucher_history.reset_devices(
            self.voucher,
            self.user,
            "normal reset",
        )
        self.assertFalse(ok)
        self.assertIn("permanent", message.lower())
        self.assertTrue(
            VoucherDeviceBinding.objects.filter(pk=self.binding.pk).exists()
        )
        self.assertTrue(permanent_device.is_permanent(self.voucher))

    def test_direct_release_all_is_also_protected(self):
        self.make_permanent()
        removed = device_lock.release_all(self.voucher)
        self.assertEqual(removed, [])
        self.assertTrue(
            VoucherDeviceBinding.objects.filter(pk=self.binding.pk).exists()
        )

    def test_remove_permanent_keeps_normal_sticky_binding(self):
        self.make_permanent()
        with patch("core.device_lock.unlock_on_router") as unlock:
            binding, changed = permanent_device.remove_permanent(
                self.voucher,
                self.user,
            )
        self.assertTrue(changed)
        self.assertEqual(binding.pk, self.binding.pk)
        self.assertFalse(permanent_device.is_permanent(self.voucher))
        self.assertTrue(
            VoucherDeviceBinding.objects.filter(pk=self.binding.pk).exists()
        )
        unlock.assert_called_once_with(self.voucher)

    def test_same_operation_is_idempotent(self):
        self.make_permanent()
        with patch("core.permanent_device._pin", return_value="AA:BB:CC:DD:EE:01"):
            _, created = permanent_device.make_permanent(
                self.voucher,
                self.user,
            )
        self.assertFalse(created)
        self.assertEqual(
            PermanentVoucherDevice.objects.filter(binding=self.binding).count(),
            1,
        )
