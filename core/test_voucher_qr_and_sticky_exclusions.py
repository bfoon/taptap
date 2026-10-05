from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import sticky_exclusions
from .models import (
    Business,
    DeviceSignature,
    PortalPage,
    Voucher,
    VoucherDeviceBinding,
)
from .models_sticky_exclusions import StickyExclusionRule


class VoucherQrAndStickyExclusionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="owner@example.com",
            email="owner@example.com",
            password="test",
        )
        self.business = Business.objects.create(
            user=self.user,
            business_name="TapTap Test",
            owner_name="Owner",
            phone="7000000",
            trial_ends_at=timezone.now() + timedelta(days=30),
            is_unlimited=True,
        )
        self.voucher = Voucher.objects.create(
            business=self.business,
            code="QRIPHONE1",
            plan_name="Daily",
            price=25,
            duration_minutes=1440,
            max_devices=1,
            status="active",
        )

    def test_customer_qr_redirects_to_current_published_portal(self):
        page = PortalPage.objects.create(
            business=self.business,
            name="Customer login",
            slug="customer-login-test",
            kind="login",
            config={},
            is_published=True,
            is_default=True,
        )
        response = self.client.get(
            reverse("voucher_customer_portal"),
            {"v": self.voucher.code},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/p/{page.slug}/", response["Location"])
        self.assertIn("username=QRIPHONE1", response["Location"])

    def test_customer_qr_404_when_no_published_portal(self):
        response = self.client.get(
            reverse("voucher_customer_portal"),
            {"v": self.voucher.code},
        )
        self.assertEqual(response.status_code, 404)
        self.assertContains(
            response,
            "customer portal is not published",
            status_code=404,
        )

    def test_rule_normalises_and_deduplicates_case_insensitively(self):
        n = sticky_exclusions.save_rules(
            self.business,
            [
                {
                    "match_field": "model",
                    "value": "iPhone",
                    "skip_device_lock": True,
                    "skip_sticky_sessions": True,
                    "enabled": True,
                },
                {
                    "match_field": "model",
                    "value": " iphone ",
                    "skip_device_lock": True,
                    "skip_sticky_sessions": True,
                    "enabled": True,
                },
            ],
        )
        self.assertEqual(n, 1)
        self.assertEqual(self.business.sticky_exclusion_rules.count(), 1)

    def test_iphone_model_matches_but_android_does_not(self):
        StickyExclusionRule.objects.create(
            business=self.business,
            match_field="model",
            value="iPhone",
            skip_device_lock=True,
            skip_sticky_sessions=True,
        )
        iphone = DeviceSignature.objects.create(
            business=self.business,
            fingerprint="a" * 32,
            device_type="phone",
            os="iOS",
            model="iPhone",
        )
        android = DeviceSignature.objects.create(
            business=self.business,
            fingerprint="b" * 32,
            device_type="phone",
            os="Android",
            model="SM-A155F",
        )
        self.assertTrue(sticky_exclusions.matching_rules(iphone, "device_lock"))
        self.assertFalse(sticky_exclusions.matching_rules(android, "device_lock"))

    def test_matching_signature_releases_only_its_binding(self):
        StickyExclusionRule.objects.create(
            business=self.business,
            match_field="model",
            value="iPhone",
            skip_device_lock=True,
            skip_sticky_sessions=False,
        )
        b1 = VoucherDeviceBinding.objects.create(
            business=self.business,
            voucher=self.voucher,
            slot_no=1,
            device_token_hash="a" * 32,
            current_mac="AA:BB:CC:DD:EE:01",
            label="iPhone",
            locked_by="portal",
        )
        other = Voucher.objects.create(
            business=self.business,
            code="OTHERPHONE1",
            plan_name="Daily",
            price=25,
            duration_minutes=1440,
            max_devices=1,
            status="active",
        )
        b2 = VoucherDeviceBinding.objects.create(
            business=self.business,
            voucher=other,
            slot_no=1,
            device_token_hash="b" * 32,
            current_mac="AA:BB:CC:DD:EE:02",
            label="Android",
            locked_by="portal",
        )

        with mock.patch.object(
            sticky_exclusions,
            "_schedule_cookie_clears",
            return_value=0,
        ):
            sig = DeviceSignature.objects.create(
                business=self.business,
                fingerprint="a" * 32,
                device_type="phone",
                os="iOS",
                model="iPhone",
                last_mac="AA:BB:CC:DD:EE:01",
                macs=["AA:BB:CC:DD:EE:01"],
                vouchers=[self.voucher.code],
            )
            sticky_exclusions.enforce_signature(sig)

        self.assertFalse(VoucherDeviceBinding.objects.filter(pk=b1.pk).exists())
        self.assertTrue(VoucherDeviceBinding.objects.filter(pk=b2.pk).exists())

    def test_agent_has_safe_cookie_remove_command(self):
        from . import agent

        self.assertIn("hotspot_cookie_remove", agent.SAFE_KINDS)
        cmd = mock.Mock(
            kind="hotspot_cookie_remove",
            params={
                "user": self.voucher.code,
                "mac": "AA:BB:CC:DD:EE:01",
            },
        )
        body = agent.command_body(cmd)
        self.assertIn("/ip hotspot cookie", body)
        self.assertIn("remove", body)
