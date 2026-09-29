from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.utils import timezone

from .models import Business, Router, SyncedIPBinding, VoucherSale


class BypassPaymentTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1', currency='D',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=self.b, name='R1', ip_address='', username='', password='', connection_mode='agent')
        self.bd = SyncedIPBinding.objects.create(business=self.b, router=self.r, mikrotik_id='*5', mac_address='AA:BB:CC:00:00:01',
                                                 binding_type='bypassed', disabled=True, comment='Tenant 19', is_present=True)
        self.c = Client(); self.c.force_login(self.owner)

    def post(self, **data):
        return self.c.post('/ip-bindings/set/', {'ids': [self.bd.pk], **data})

    def test_bypass_cannot_turn_on_without_payment(self):
        with mock.patch('core.linkops.send'):
            r = self.post(action='enable')
        self.assertEqual(r.status_code, 400); self.assertTrue(r.json()['need_payment'])
        self.bd.refresh_from_db(); self.assertTrue(self.bd.disabled)
        self.assertFalse(VoucherSale.objects.exists())

    def test_payment_is_booked_in_finance(self):
        with mock.patch('core.linkops.send'):
            r = self.post(action='timed', value='24', pay_amount='50', pay_method='wave', pay_note='rcpt 12')
        self.assertTrue(r.json()['ok'])
        s = VoucherSale.objects.get()
        self.assertEqual((s.amount, s.payment_method, s.reference), (Decimal('50.00'), 'wave', 'AA:BB:CC:00:00:01'))
        self.assertTrue(s.plan_name.startswith('Bypass access'))
        from .finance import finance_summary, resolve_period
        self.assertEqual(finance_summary(self.b, resolve_period({}))['revenue'], Decimal('50.00'))
        rows = self.c.get('/ip-bindings/data/').json()['rows']
        self.assertEqual(rows[0]['paid']['amount'], 'D50.00')

    def test_free_access_needs_a_reason_and_collect_only(self):
        with mock.patch('core.linkops.send'):
            self.assertEqual(self.post(action='enable', pay_free='1').status_code, 400)
            self.assertTrue(self.post(action='enable', pay_free='1', pay_free_reason='staff phone').json()['ok'])
        self.assertFalse(VoucherSale.objects.exists())
        r = self.post(action='collect', pay_amount='25', pay_method='cash')
        self.assertTrue(r.json()['ok']); self.assertEqual(VoucherSale.objects.get().amount, Decimal('25.00'))

    def test_turning_off_needs_no_payment(self):
        SyncedIPBinding.objects.filter(pk=self.bd.pk).update(disabled=False)
        with mock.patch('core.linkops.send'):
            self.assertTrue(self.post(action='disable').json()['ok'])
