from datetime import timedelta, datetime

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.utils import timezone

from . import serials
from .models import Business, Voucher, VoucherBatch, VoucherPlan


class SerialTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user('owner', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.u, business_name='Kairaba Net', owner_name='A', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.plan = VoucherPlan.objects.create(business=self.b, name='1 Day Pass', price=25, duration_minutes=1440)
        self.c = Client(); self.c.force_login(self.u)

    def test_patterns(self):
        now = timezone.make_aware(datetime(2026, 9, 28, 10, 0))
        self.assertEqual(serials.render('{n}', 7, 6), '000007')
        self.assertEqual(serials.render('KN-{yy}{mm}-{n}', 124, 6, now=now), 'KN-2609-000124')
        self.assertEqual(serials.render('{plan}/{batch}/{bn}', 1, 3, bn=7, batch=42, plan='1 Day Pass'), '1DP/42/007')
        for bad in ['KN-', '{x}{n}', 'a' * 41, 'KN<{n}>']:
            with self.assertRaises(serials.SerialError):
                serials.clean(bad, 6, 'never')

    def test_running_number_never_repeats(self):
        a = serials.allocate(self.b, 3)
        b = serials.allocate(self.b, 2)
        self.assertEqual(a + b, ['000001', '000002', '000003', '000004', '000005'])
        c = serials.allocate(self.b, 2, start=100)
        self.assertEqual(c, ['000100', '000101'])
        self.assertEqual(serials.allocate(self.b, 1), ['000102'])

    def test_reset_per_batch_and_month(self):
        bt = VoucherBatch.objects.create(business=self.b, name='B', plan=self.plan, quantity=3)
        self.assertEqual(serials.allocate(self.b, 3, fmt='B{batch}-{bn}', digits=2, reset='batch', batch=bt), [f'B{bt.pk}-01', f'B{bt.pk}-02', f'B{bt.pk}-03'])
        sep = timezone.make_aware(datetime(2026, 9, 30)); octo = timezone.make_aware(datetime(2026, 10, 1))
        self.assertEqual(serials.allocate(self.b, 2, fmt='{yy}{mm}-{n}', digits=3, reset='monthly', now=sep), ['2609-001', '2609-002'])
        self.assertEqual(serials.allocate(self.b, 1, fmt='{yy}{mm}-{n}', digits=3, reset='monthly', now=octo), ['2610-001'])

    def test_generate_page_uses_pattern(self):
        r = self.c.post('/vouchers/generate/', {'plan': self.plan.pk, 'quantity': 3, 'serial_format': 'KN-{n}', 'serial_digits': 4,
                                                 'serial_reset': 'never', 'serial_save': '1'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(sorted(Voucher.objects.values_list('serial', flat=True)), ['KN-0001', 'KN-0002', 'KN-0003'])
        self.b.refresh_from_db(); self.assertEqual(self.b.serial_format, 'KN-{n}')
        r = self.c.post('/vouchers/generate/', {'plan': self.plan.pk, 'quantity': 1, 'serial_format': 'nothing'})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.c.get('/vouchers/', {'q': 'KN-0002'}).status_code, 200)
        self.assertIn(b'KN-0002', self.c.get('/vouchers/', {'q': 'KN-0002'}).content)

    def test_settings_page(self):
        self.assertEqual(self.c.get('/settings/').status_code, 200)
        self.c.post('/settings/', {'business_name': 'Kairaba Net', 'owner_name': 'A', 'phone': '1', 'currency': 'D', 'brand_color': '#1769e0',
                                   'serial_format': '{yyyy}-{n}', 'serial_digits': 5, 'serial_reset': 'yearly', 'serial_next': 50})
        self.b.refresh_from_db()
        self.assertEqual((self.b.serial_format, self.b.serial_digits, self.b.serial_reset), ('{yyyy}-{n}', 5, 'yearly'))
        self.assertEqual(serials.allocate(self.b, 1)[0], f'{timezone.localtime():%Y}-00050')
