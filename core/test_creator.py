from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.utils import timezone

from .models import Business, Router, Voucher, VoucherBatch, VoucherPlan
from .models_team import TeamMember


class CreatorTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@x.com', 'pw12345678', first_name='Baboucarr', last_name='Foon')
        self.b = Business.objects.create(user=self.owner, business_name='Kairaba Net', owner_name='A', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.plan = VoucherPlan.objects.create(business=self.b, name='1 Day', price=25, duration_minutes=1440)
        self.staff = User.objects.create_user('awa', 'awa@x.com', 'pw12345678', first_name='Awa', last_name='Jallow')
        TeamMember.objects.create(business=self.b, user=self.staff, role='admin')

    def test_team_member_generating_a_batch_is_recorded(self):
        c = Client(); c.force_login(self.staff)
        r = c.post('/vouchers/generate/', {'plan': self.plan.pk, 'quantity': 2})
        self.assertEqual(r.status_code, 302)
        batch = VoucherBatch.objects.get()
        self.assertEqual(batch.created_by, self.staff)
        self.assertIn('Awa Jallow', batch.created_by_label)
        for v in batch.vouchers.all():
            self.assertEqual(v.created_by, self.staff)
        oc = Client(); oc.force_login(self.owner)
        for url in (f'/batches/{batch.pk}/', f'/vouchers/{batch.vouchers.first().pk}/', f'/plans/{self.plan.pk}/'):
            self.assertContains(oc.get(url), 'Awa Jallow')

    def test_router_import_and_background_rows(self):
        r = Router.objects.create(business=self.b, name='Kotu Tower', ip_address='1.1.1.1', username='a', password='b')
        v = Voucher.objects.create(business=self.b, router=r, code='ROUTER01', source='mikrotik', plan_name='x')
        self.assertEqual(v.created_by_label, 'Imported from router Kotu Tower')
        v2 = Voucher.objects.create(business=self.b, code='AUTO0001', plan_name='x')
        self.assertEqual(v2.created_by_label, 'TapTap (automatic)')
        self.assertIsNotNone(self.plan.created_at)

    def test_owner_creates_plan(self):
        c = Client(); c.force_login(self.owner)
        c.post('/plans/', {'name': 'Weekly', 'price': '100', 'duration_value': '7', 'duration_unit': 'days', 'max_devices': '2'})
        p = VoucherPlan.objects.get(name='Weekly')
        self.assertEqual(p.created_by, self.owner)
        self.assertIn('Baboucarr Foon', p.created_by_label)
