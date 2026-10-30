"""Agent benefits: commission modes, refunds of unsold vouchers, free internet."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from core import agent_benefits as ab
from core.cash_collections import agent_balance
from core.finance import assign_batch, commission_for, record_sale
from core.models import Agent, Business, Voucher, VoucherBatch, VoucherPlan, VoucherSale
from core.models_agent_benefits import AgentRefund

D = Decimal


class Base(TestCase):
    def setUp(self):
        self.u = User.objects.create_user('owner', 'o@x.gm', 'pw', first_name='Owner')
        self.biz = Business.objects.create(user=self.u, business_name='Kunda Net', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.plan = VoucherPlan.objects.create(business=self.biz, name='1Day', price=25)
        self.agent = Agent.objects.create(business=self.biz, name='Fatou Ceesay', phone='7712345', commission_percent=10)

    def batch(self, n, settlement, days_ago, code='A'):
        b = VoucherBatch.objects.create(business=self.biz, name=f'{code}-batch', plan=self.plan, quantity=n)
        for i in range(n):
            Voucher.objects.create(business=self.biz, batch=b, code=f'{code}{i:04d}', plan_name='1Day', price=25, source='taptap')
        assign_batch(b, self.agent, settlement, user=self.u)
        VoucherBatch.objects.filter(pk=b.pk).update(issued_at=timezone.now() - timedelta(days=days_ago))
        return b


class CommissionTests(Base):
    def test_modes(self):
        self.assertEqual(commission_for(self.agent, 25), D('2.50'))
        self.agent.commission_mode, self.agent.commission_amount = 'fixed', D('5')
        self.assertEqual(commission_for(self.agent, 25), D('5.00'))
        self.assertEqual(commission_for(self.agent, 3), D('3.00'))              # never more than the sale
        self.agent.commission_enabled = False
        self.assertEqual(commission_for(self.agent, 25), D('0'))
        self.assertEqual(self.agent.commission_label, 'No commission')

    def test_change_applies_to_new_sales_only(self):
        v = Voucher.objects.create(business=self.biz, code='X1', plan_name='1Day', price=25, source='taptap', agent=self.agent)
        s1 = record_sale(self.biz, v)
        ab.save_commission(self.agent, {'commission_enabled': '1', 'commission_mode': 'fixed', 'commission_amount': '4'}, self.u)
        v2 = Voucher.objects.create(business=self.biz, code='X2', plan_name='1Day', price=25, source='taptap', agent=self.agent)
        s2 = record_sale(self.biz, v2)
        s1.refresh_from_db()
        self.assertEqual((s1.commission, s2.commission), (D('2.50'), D('4.00')))
        self.assertIn('Commission: 10% commission → D4 per voucher', self.agent.benefit.history[0]['text'])

    def test_fixed_needs_an_amount(self):
        with self.assertRaises(ab.BenefitError):
            ab.save_commission(self.agent, {'commission_enabled': '1', 'commission_mode': 'fixed', 'commission_amount': '0'}, self.u)


class RefundTests(Base):
    def test_candidates(self):
        self.batch(3, 'prepaid', 40, 'P')
        self.batch(2, 'credit', 40, 'C')
        self.batch(2, 'credit', 3, 'N')                                    # too new
        sold = Voucher.objects.get(code='C0001'); record_sale(self.biz, sold)   # sold to a customer by the agent
        Voucher.objects.filter(code='P0002').update(used_at=timezone.now())       # used
        c = ab.refund_candidates(self.agent, 30)
        codes = sorted(x['voucher'].code for x in c)
        self.assertEqual(codes, ['C0000', 'P0000', 'P0001'])
        vals = {x['voucher'].code: x['value'] for x in c}
        self.assertEqual(vals['P0000'], D('22.50'))                        # paid 25 − 2.50 commission
        self.assertEqual(vals['C0000'], D('0'))

    def test_cash_refund_disables_and_balances_net_to_zero(self):
        self.batch(4, 'prepaid', 45, 'P')
        self.assertEqual(agent_balance(self.agent)['outstanding'], D('0.00'))
        ids = [x['voucher'].pk for x in ab.refund_candidates(self.agent, 30)][:3]
        r = ab.refund(self.agent, ids, min_days=30, action='disable', payout='cash', user=self.u)
        self.assertEqual((r.count, r.amount), (3, D('67.50')))
        self.assertEqual(Voucher.objects.filter(pk__in=ids, status='disabled', sold_at__isnull=True).count(), 3)
        self.assertFalse(VoucherSale.objects.filter(voucher_id__in=ids).exists())
        self.assertEqual(r.collection.amount, D('-67.50'))
        self.assertEqual(agent_balance(self.agent)['outstanding'], D('0.00'))      # still square
        self.assertTrue(r.number.startswith('RFD-'))

    def test_credit_refund_leaves_money_paid_ahead(self):
        self.batch(2, 'prepaid', 45, 'P')
        ids = [x['voucher'].pk for x in ab.refund_candidates(self.agent, 30)]
        ab.refund(self.agent, ids, min_days=30, action='return', payout='credit', user=self.u)
        self.assertEqual(agent_balance(self.agent)['outstanding'], D('-45.00'))     # paid ahead, absorbed by future sales
        self.assertEqual(Voucher.objects.filter(pk__in=ids, agent__isnull=True, status='active').count(), 2)

    def test_credit_batch_refund_moves_no_money(self):
        self.batch(2, 'credit', 45, 'C')
        ids = [x['voucher'].pk for x in ab.refund_candidates(self.agent, 30)]
        r = ab.refund(self.agent, ids, min_days=30, action='disable', payout='cash', user=self.u)
        self.assertEqual(r.amount, D('0')); self.assertIsNone(r.collection)

    def test_refused_when_not_eligible(self):
        self.batch(1, 'prepaid', 5, 'N')
        v = Voucher.objects.get(code='N0000')
        with self.assertRaises(ab.BenefitError):
            ab.refund(self.agent, [v.pk], min_days=30, action='disable', payout='cash', user=self.u)


class FreeInternetTests(Base):
    def test_on_off_on_and_new_password(self):
        plan = ab.create_agent_plan(self.biz, self.u)
        self.assertEqual((plan.name, plan.price, plan.duration_minutes), ('Agent Wi-Fi', D('0'), 0))
        self.assertEqual(ab.default_plan(self.biz), plan)
        m, created, pw = ab.enable_free_internet(self.agent, user=self.u)          # default plan picked
        self.assertTrue(created); self.assertEqual(len(pw), 5)
        self.assertEqual((m.code, m.login_type, m.password, m.customer_name), ('fatou.ceesay', 'member', pw, 'Fatou Ceesay'))
        self.assertIsNone(m.agent_id)                                              # not the agent's stock
        ab.disable_free_internet(self.agent, self.u)
        m.refresh_from_db(); self.assertEqual(m.status, 'disabled')
        m2, created2, pw2 = ab.enable_free_internet(self.agent, user=self.u)
        self.assertEqual((m2.pk, created2, pw2), (m.pk, False, None))
        m2.refresh_from_db(); self.assertEqual(m2.status, 'active')
        with mock.patch('core.members._push_password', return_value=('API', 'ok', True)):
            new, ok, _ = ab.reset_password(self.agent, self.u)
        m2.refresh_from_db(); self.assertEqual((len(new), m2.password), (5, new))

    def test_username_is_unique(self):
        Voucher.objects.create(business=self.biz, code='fatou.ceesay', plan_name='1Day', source='taptap')
        self.assertEqual(ab.suggested_username(self.agent), 'fatou.ceesay2')


class PageTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.u)
        self.url = f'/finance/agents/{self.agent.pk}/benefits/'

    def test_page_and_actions(self):
        html = self.client.get(self.url).content.decode()
        for bit in ('id="commission"', 'id="refunds"', 'id="internet"', 'fatou.ceesay', 'Live preview'):
            self.assertIn(bit, html)
        self.client.post(self.url, {'action': 'commission', 'commission_enabled': '1', 'commission_mode': 'fixed', 'commission_amount': '3'})
        self.agent.refresh_from_db(); self.assertEqual(self.agent.commission_label, 'D3 per voucher')
        self.client.post(self.url, {'action': 'refund_settings', 'refund_enabled': '1', 'refund_after_days': '14', 'refund_action': 'disable', 'refund_payout': 'credit'})
        self.assertEqual(self.agent.benefit.refund_after_days, 14)
        r = self.client.post(self.url, {'action': 'free_on', 'plan': 'new'}, follow=True)
        html = r.content.decode()
        self.assertIn('wifi-card', html); self.assertIn('shown <b>only once</b>', html)
        pw = Voucher.objects.get(code='fatou.ceesay').password
        self.assertIn(pw, html)
        self.assertNotIn(pw, self.client.get(self.url).content.decode())         # shown once only

    def test_overview_has_tabs(self):
        html = self.client.get(f'/finance/agents/{self.agent.pk}/').content.decode()
        self.assertIn('class="ag-tabs"', html); self.assertIn(self.url, html)


class RefundPageTests(PageTests):
    def test_refund_through_the_page(self):
        self.batch(3, 'prepaid', 40, 'P')
        self.client.post(self.url, {'action': 'refund_settings', 'refund_enabled': '1', 'refund_after_days': '30', 'refund_action': 'disable', 'refund_payout': 'cash'})
        html = self.client.get(self.url).content.decode()
        self.assertIn('class="rf-group"', html); self.assertIn('P0000', html); self.assertIn('paid upfront', html)
        ids = list(Voucher.objects.filter(code__in=['P0000', 'P0001']).values_list('pk', flat=True))
        r = self.client.post(self.url, {'action': 'refund', 'min_days': '30', 'voucher': ids, 'method': 'cash', 'reference': 'R1'}, follow=True)
        html = r.content.decode()
        ref = AgentRefund.objects.get()
        self.assertEqual((ref.count, ref.amount), (2, D('45.00')))
        self.assertIn(ref.number, html); self.assertIn('Refund slip', html); self.assertIn('P0000', html)
