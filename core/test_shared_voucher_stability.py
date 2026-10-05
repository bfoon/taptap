"""Regression tests for shared-voucher device stability.

Run:
    python manage.py test core.test_shared_voucher_stability
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from . import device_lock
from . import shared_voucher_stability as stability
from .models import Business, Voucher, VoucherDeviceBinding


def mac(n):
    """Locally-administered/private MAC, unique by n."""
    return f"02:11:22:33:{n // 256:02X}:{n % 256:02X}"


class SharedVoucherStabilityTests(TestCase):
    def setUp(self):
        stability.install()

        self.owner = User.objects.create_user(
            username="owner@example.com",
            email="owner@example.com",
            password="pw12345678",
        )
        self.business = Business.objects.create(
            user=self.owner,
            business_name="Kombo WiFi",
            owner_name="Owner",
            phone="2200000",
            trial_ends_at=timezone.now() + timedelta(days=30),
            is_unlimited=True,
            device_lock=True,
        )

    def voucher(self, code, devices):
        return Voucher.objects.create(
            business=self.business,
            code=code,
            plan_name=f"{devices} devices",
            max_devices=devices,
            duration_minutes=43200,
            status="active",
        )

    def bind(self, voucher, slot, address, label=""):
        return VoucherDeviceBinding.objects.create(
            business=self.business,
            voucher=voucher,
            slot_no=slot,
            current_mac=address,
            label=label,
            locked_by="router",
        )

    def test_family_10_unknown_private_mac_cannot_steal_offline_slot(self):
        voucher = self.voucher("FAMILY10", 10)
        original = []
        for slot in range(1, 11):
            address = mac(slot)
            original.append(address)
            self.bind(voucher, slot, address)

        newcomer = mac(200)
        online = set(original[:4])  # six genuine family devices are sleeping

        with mock.patch("core.device_lock._host", return_value=""), \
             mock.patch("core.device_lock._swaps_today", return_value=0):
            result = device_lock.claim(
                voucher,
                mac=newcomer,
                source="router",
                hints={
                    "online_macs": online,
                    "router_id": None,
                },
            )

        self.assertEqual(result.status, "denied")
        self.assertEqual(voucher.device_bindings.count(), 10)
        self.assertFalse(
            voucher.device_bindings.filter(current_mac=newcomer).exists()
        )
        self.assertEqual(
            list(
                voucher.device_bindings
                .order_by("slot_no")
                .values_list("current_mac", flat=True)
            ),
            original,
        )

    def test_family_10_tenth_device_can_take_last_free_slot(self):
        voucher = self.voucher("FAMILYFREE", 10)
        for slot in range(1, 10):
            self.bind(voucher, slot, mac(slot))

        newcomer = mac(150)
        with mock.patch("core.voucher_history.record"):
            result = device_lock.claim(
                voucher,
                mac=newcomer,
                source="router",
                hints={
                    "online_macs": {mac(i) for i in range(1, 10)},
                    "router_id": None,
                },
            )

        self.assertEqual(result.status, "new")
        self.assertEqual(result.binding.slot_no, 10)
        self.assertEqual(voucher.device_bindings.count(), 10)
        self.assertTrue(
            voucher.device_bindings.filter(current_mac=newcomer).exists()
        )

    def test_existing_family_device_remains_allowed_when_full(self):
        voucher = self.voucher("FAMILYKNOWN", 10)
        known = []
        for slot in range(1, 11):
            address = mac(slot)
            known.append(address)
            self.bind(voucher, slot, address)

        result = device_lock.claim(
            voucher,
            mac=known[6],
            source="router",
            hints={
                "online_macs": set(known),
                "router_id": None,
            },
        )

        self.assertEqual(result.status, "known")
        self.assertTrue(result.allowed)
        self.assertEqual(result.binding.slot_no, 7)
        self.assertEqual(voucher.device_bindings.count(), 10)

    def test_shared_plan_does_not_guess_from_hostname(self):
        voucher = self.voucher("FAMILYHOST", 10)
        old = []
        for slot in range(1, 11):
            address = mac(slot)
            old.append(address)
            self.bind(voucher, slot, address)

        newcomer = mac(170)
        target_old = old[7]
        online = set(old) - {target_old}

        def host_name(_router_id, address):
            address = device_lock.norm_mac(address)
            if address in {target_old, newcomer}:
                return "fatou-phone"
            return ""

        # Even an apparently matching hostname is not strong enough evidence to
        # take a permanent slot on a full shared voucher.
        with mock.patch("core.device_lock._host", side_effect=host_name):
            result = device_lock.claim(
                voucher,
                mac=newcomer,
                source="router",
                hints={
                    "online_macs": online,
                    "router_id": None,
                },
            )

        self.assertEqual(result.status, "denied")
        self.assertTrue(
            voucher.device_bindings.filter(current_mac=target_old).exists()
        )
        self.assertFalse(
            voucher.device_bindings.filter(current_mac=newcomer).exists()
        )

    def test_single_device_keeps_original_private_mac_recovery(self):
        voucher = self.voucher("SINGLE1", 1)
        old = mac(1)
        newcomer = mac(190)
        self.bind(voucher, 1, old)

        with mock.patch("core.device_lock._host", return_value=""), \
             mock.patch("core.device_lock._swaps_today", return_value=0), \
             mock.patch("core.device_lock._forget_mac"), \
             mock.patch("core.voucher_history.record"):
            result = device_lock.claim(
                voucher,
                mac=newcomer,
                source="router",
                hints={
                    "online_macs": set(),
                    "router_id": None,
                },
            )

        self.assertEqual(result.status, "moved")
        result.binding.refresh_from_db()
        self.assertEqual(result.binding.current_mac, newcomer)
        self.assertEqual(result.binding.previous_mac, old)

    def test_retained_device_token_can_move_shared_slot_safely(self):
        voucher = self.voucher("FAMILYTOKEN", 10)
        rows = []
        for slot in range(1, 11):
            row = self.bind(voucher, slot, mac(slot))
            rows.append(row)

        target = rows[4]
        target.device_token_hash = "device-token-5"
        target.save(update_fields=["device_token_hash"])
        newcomer = mac(210)

        with mock.patch("core.device_lock._forget_mac"), \
             mock.patch("core.voucher_history.record"):
            result = device_lock.claim(
                voucher,
                mac=newcomer,
                fp="device-token-5",
                source="router",
                hints={
                    "online_macs": {
                        row.current_mac
                        for row in rows
                        if row.pk != target.pk
                    },
                    "router_id": None,
                },
            )

        self.assertTrue(result.allowed)
        self.assertEqual(result.status, "moved")
        result.binding.refresh_from_db()
        self.assertEqual(result.binding.pk, target.pk)
        self.assertEqual(result.binding.current_mac, newcomer)
        self.assertEqual(voucher.device_bindings.count(), 10)

    def test_known_previous_mac_can_return_if_current_mac_is_offline(self):
        voucher = self.voucher("FAMILYPREV", 10)
        target = self.bind(voucher, 1, mac(101))
        target.previous_mac = mac(1)
        target.save(update_fields=["previous_mac"])

        for slot in range(2, 11):
            self.bind(voucher, slot, mac(slot))

        result = device_lock.claim(
            voucher,
            mac=mac(1),
            source="router",
            hints={
                "online_macs": {
                    mac(slot)
                    for slot in range(2, 11)
                },
                "router_id": None,
            },
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.status, "moved")
        result.binding.refresh_from_db()
        self.assertEqual(result.binding.current_mac, mac(1))
        self.assertEqual(result.binding.previous_mac, mac(101))
        self.assertEqual(voucher.device_bindings.count(), 10)
