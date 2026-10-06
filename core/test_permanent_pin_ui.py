from datetime import timedelta

from django.contrib.auth.models import User
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.urls import resolve
from django.utils import timezone

from . import permanent_device
from .models import Business, Voucher, VoucherDeviceBinding
from .models_permanent_device import PermanentVoucherDevice


class PermanentPinUiRegressionTests(TestCase):
    def setUp(self):
        self.rf = RequestFactory()
        self.user = User.objects.create_user(
            username="pin-owner@example.com",
            email="pin-owner@example.com",
            password="test",
        )
        self.business = Business.objects.create(
            user=self.user,
            business_name="Permanent Pin Test",
            owner_name="Owner",
            phone="7000000",
            trial_ends_at=timezone.now() + timedelta(days=30),
            is_unlimited=True,
        )
        self.voucher = Voucher.objects.create(
            business=self.business,
            code="PIN001",
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
            device_token_hash="stable-pin-device",
            current_mac="AA:BB:CC:DD:EE:01",
            label="Customer phone",
            locked_by="portal",
        )

    def request(self):
        request = self.rf.get(f"/vouchers/{self.voucher.pk}/")
        request.user = self.user
        request.tt_business = self.business
        request.tt_perms = frozenset({"vouchers.support"})
        request.resolver_match = resolve(f"/vouchers/{self.voucher.pk}/")
        return request

    def response(self):
        # This mirrors the current Oct-2026 Locked devices header shape.
        return HttpResponse(
            """
            <html><body>
            <section class="panel vd-card">
              <div class="hd"><h3><i class="bi bi-phone-vibrate"></i> Locked devices</h3>
                <button class="btn btn-sm btn-outline-secondary"
                        data-bs-toggle="modal" data-bs-target="#resetModal">Reset</button>
              </div>
              <p>1 of 1 device locked.</p>
            </section>
            </body></html>
            """,
            content_type="text/html",
        )

    def test_current_layout_shows_permanent_pin_button(self):
        result = permanent_device._inject_voucher_ui(
            self.request(),
            self.response(),
        )
        html = result.content.decode()
        self.assertIn('id="tt-permanent-device-control"', html)
        self.assertIn("Permanent Pin", html)
        self.assertIn("permanent-device", html)

    def test_pinned_device_shows_remove_pin(self):
        PermanentVoucherDevice.objects.create(
            binding=self.binding,
            created_by=self.user,
        )
        result = permanent_device._inject_voucher_ui(
            self.request(),
            self.response(),
        )
        html = result.content.decode()
        self.assertIn("Permanent Pin", html)
        self.assertIn("Remove Permanent Pin", html)
        self.assertIn("Only this device can use the voucher", html)
