"""Voucher batches above 500: allowed, but only after the owner confirms the exact amount."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import Business, VoucherPlan, Voucher


class BigBatch(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('owner', password='x')
        self.biz = Business.objects.create(user=self.user, business_name='B', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.plan = VoucherPlan.objects.create(business=self.biz, name='Day', price=10, duration_minutes=1440, max_devices=1, active=True)
        self.client.force_login(self.user)

    def post(self, qty, confirm=None):
        data = {'plan': self.plan.pk, 'quantity': qty, 'code_length': 8}
        if confirm is not None:
            data['confirm_qty'] = confirm
        return self.client.post('/vouchers/generate/', data)

    def test_up_to_500_needs_no_confirmation(self):
        r = self.post(500)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Voucher.objects.filter(business=self.biz).count(), 500)

    def test_above_500_asks_first_and_creates_nothing(self):
        r = self.post(1200)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'You are about to create 1200 vouchers')
        self.assertContains(r, 'name="confirm_qty" value="1200"')
        self.assertEqual(Voucher.objects.filter(business=self.biz).count(), 0)

    def test_confirmed_amount_goes_through(self):
        r = self.post(1200, confirm='1200')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Voucher.objects.filter(business=self.biz).count(), 1200)
        self.assertEqual(len(set(Voucher.objects.filter(business=self.biz).values_list('code', flat=True))), 1200)

    def test_confirmation_is_for_that_exact_amount(self):
        r = self.post(1500, confirm='600')     # confirmed 600, then changed the number
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Voucher.objects.filter(business=self.biz).count(), 0)

    @override_settings(VOUCHER_BATCH_MAX=1000)
    def test_technical_ceiling(self):
        r = self.post(1500, confirm='1500')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Voucher.objects.filter(business=self.biz).count(), 0)

    @override_settings(VOUCHER_CONFIRM_ABOVE=100)
    def test_threshold_is_configurable(self):
        r = self.post(150)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Voucher.objects.filter(business=self.biz).count(), 0)
