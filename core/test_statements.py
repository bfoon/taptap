"""Agent cash-flow statement and batch receipts.

Run:  DB_ENGINE=sqlite python manage.py test core.test_statements
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .finance import (agent_statement, amount_in_words, assign_batch, batch_receipt, record_sale, resolve_period)
from .models import Agent, Business, CashCollection, Voucher, VoucherBatch, VoucherPlan
from .models_team import TeamMember

HTML = {'HTTP_ACCEPT': 'text/html'}


@override_settings(AUTH_EMAIL_OTP=False)
class StatementTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('s@example.com', 's@example.com', 'pw', first_name='Abdou')
        self.biz = Business.objects.create(user=self.owner, business_name='TapTap', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=7))
        self.plan = VoucherPlan.objects.create(business=self.biz, name='Daily - 1 Device', price=Decimal('25'), duration_minutes=1440)
        self.agent = Agent.objects.create(business=self.biz, name='Garage', phone='7000000', commission_percent=Decimal('9.0909'))
        self.client.force_login(self.owner)

    def batch(self, n=4, agent=True, settlement='credit'):
        b = VoucherBatch.objects.create(business=self.biz, name=f'VC-{VoucherBatch.objects.count() + 9:03d}', plan=self.plan, quantity=n)
        for i in range(n):
            Voucher.objects.create(business=self.biz, batch=b, code=f'{b.pk}C{i:04d}', serial=f'{b.pk}{i:03d}', plan_name=self.plan.name, price=Decimal('25'))
        if agent:
            assign_batch(b, self.agent, settlement, 'cash', self.owner)
        return b

    def test_cash_flow_adds_up(self):
        b = self.batch(4)
        now = timezone.now()
        old = now - timedelta(days=60)
        vs = list(b.vouchers.order_by('id'))
        record_sale(self.biz, vs[0], when=old)                                     # before the period: opening balance
        CashCollection.objects.create(business=self.biz, agent=self.agent, amount=Decimal('10'), collected_at=old)
        record_sale(self.biz, vs[1]); record_sale(self.biz, vs[2])                 # in the period
        CashCollection.objects.create(business=self.biz, agent=self.agent, amount=Decimal('30'), payment_method='wave', reference='W77')
        st = agent_statement(self.biz, self.agent, resolve_period({'range': '30d'}))
        comm = Decimal('2.27')                                                     # 25 × 9.0909 %
        self.assertEqual(st['opening'], Decimal('25') - comm - Decimal('10'))
        self.assertEqual((st['gross'], st['commission'], st['collected']), (Decimal('50'), comm * 2, Decimal('30')))
        self.assertEqual(st['closing'], st['opening'] + st['net'] - st['collected'])
        self.assertEqual(st['lines'][-1]['balance'], st['closing'])                # running balance ends at the closing
        self.assertEqual(st['status'][0], 'due')
        self.assertEqual([m['label'] for m in st['methods']], ['Wave'])
        self.assertEqual(st['lines'][-1]['ref'], 'W77')
        self.assertEqual(st['sold_n'], 2)
        self.assertEqual(st['held_n'], 1)

    def test_settled_and_in_credit(self):
        st = agent_statement(self.biz, self.agent, resolve_period({'range': 'month'}))
        self.assertEqual(st['status'][0], 'settled')
        CashCollection.objects.create(business=self.biz, agent=self.agent, amount=Decimal('5'))
        st = agent_statement(self.biz, self.agent, resolve_period({'range': 'month'}))
        self.assertEqual((st['status'][0], st['closing']), ('credit', Decimal('-5')))

    def test_statement_page(self):
        b = self.batch(2)
        record_sale(self.biz, b.vouchers.first())
        c = CashCollection.objects.create(business=self.biz, agent=self.agent, amount=Decimal('20'))
        r = self.client.get(reverse('agent_statement', args=[self.agent.pk]), **HTML)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Agent Cash Flow Statement')
        self.assertContains(r, f'STM-{self.agent.pk:04d}-')
        self.assertContains(r, f'COL-{timezone.localtime(c.collected_at):%Y%m%d}-{c.pk:06d}')
        self.assertContains(r, 'D20.00')
        r = self.client.get(reverse('agent_statement', args=[self.agent.pk]) + '?range=custom&start=2026-01-01&end=2026-01-31', **HTML)
        self.assertEqual(r.status_code, 200)

    def test_finance_role_can_open_statement(self):
        u = User.objects.create_user('fin@x.com', 'fin@x.com', 'pw')
        TeamMember.objects.create(business=self.biz, user=u, role='finance')
        self.client.force_login(u)
        self.assertEqual(self.client.get(reverse('agent_statement', args=[self.agent.pk]), **HTML).status_code, 200)

    # ── batch receipt ──
    def test_amount_in_words(self):
        self.assertEqual(amount_in_words(Decimal('1136.36')), 'One thousand one hundred and thirty-six dalasi and thirty-six butut only')
        self.assertEqual(amount_in_words(Decimal('1005')), 'One thousand and five dalasi only')
        self.assertEqual(amount_in_words(0), 'Zero dalasi only')

    def test_receipt_numbers_credit_batch(self):
        b = self.batch(50)
        rc = batch_receipt(b)
        self.assertEqual((rc['count'], rc['value'], rc['unit']), (50, Decimal('1250'), Decimal('25')))
        self.assertEqual(rc['commission'], Decimal('113.50'))                     # 50 × D2.27, booked per voucher like every sale
        self.assertEqual(rc['expected'], Decimal('1136.50'))
        self.assertFalse(rc['prepaid'])
        self.assertTrue(rc['number'].startswith('BRC-') and rc['number'].endswith(f'{b.pk:06d}'))
        self.assertEqual(rc['serial_first'], f'{b.pk}000')

    def test_receipt_prepaid_batch_has_no_balance(self):
        b = self.batch(4, settlement='prepaid')
        rc = batch_receipt(b)
        self.assertTrue(rc['prepaid'])
        self.assertEqual(rc['balance'], Decimal('0'))
        self.assertEqual(rc['paid'], rc['expected'])

    def test_receipt_page_formats_and_codes(self):
        b = self.batch(3)
        url = reverse('batch_receipt', args=[b.pk])
        r = self.client.get(url, **HTML)
        self.assertContains(r, 'Batch Issue Receipt'); self.assertContains(r, 'AGENT COPY')
        self.assertNotContains(r, f'{b.pk}C0000')                                  # codes only when asked
        r = self.client.get(url + '?codes=1&format=thermal', **HTML)
        self.assertContains(r, f'{b.pk}C0000'); self.assertContains(r, 'class="thermal"')
        shop = self.batch(2, agent=False)
        self.assertContains(self.client.get(reverse('batch_receipt', args=[shop.pk]), **HTML), 'SHOP STOCK')

    def test_generate_can_open_receipt(self):
        r = self.client.post(reverse('generate_vouchers'), {'plan': self.plan.pk, 'quantity': 3, 'owner': self.agent.pk,
                                                            'settlement': 'credit', 'print_receipt': '1', 'print_after': '1',
                                                            'code_length': 8, 'code_charset': 'mixed'}, **HTML)
        self.assertEqual(r.status_code, 302)
        b = VoucherBatch.objects.latest('id')
        self.assertTrue(r['Location'].startswith(reverse('batch_receipt', args=[b.pk]) + '?autoprint=1&next='))
        self.assertIn('studio/vouchers/print', r['Location'])

    def test_voucher_creator_can_open_receipt_but_not_statement(self):
        b = self.batch(2)
        u = User.objects.create_user('vc@x.com', 'vc@x.com', 'pw')
        TeamMember.objects.create(business=self.biz, user=u, role='voucher_creator')
        self.client.force_login(u)
        self.assertEqual(self.client.get(reverse('batch_receipt', args=[b.pk]), **HTML).status_code, 200)
        self.assertNotEqual(self.client.get(reverse('agent_statement', args=[self.agent.pk]), **HTML).status_code, 200)
