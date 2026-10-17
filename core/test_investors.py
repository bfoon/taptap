"""Investors (core/investors.py): the terms engine, daily accruals, the deal lifecycle, portal isolation and Fit's sandbox."""
import json
from datetime import timedelta
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import investor_fit
from . import investors as svc
from .finance import _day_start
from .models import Business, Expense, Router, VoucherSale
from .models_investors import Investment, InvestmentOpportunity, Investor, InvestorSummaryLog


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o@example.com', 'o@example.com', 'Pass-word-123')
        self.b = Business.objects.create(user=self.owner, business_name='Alpha WiFi', owner_name='A', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=30), is_unlimited=True)
        self.r1 = Router.objects.create(business=self.b, name='Brikama', ip_address='1.1.1.1', username='a', password='b')
        self.r2 = Router.objects.create(business=self.b, name='Serrekunda', ip_address='1.1.1.2', username='a', password='b')
        self.alice = Investor.objects.create(business=self.b, name='Alice', email='alice@example.com')
        self.bob = Investor.objects.create(business=self.b, name='Bob', email='bob@example.com')
        self.today = timezone.localdate()

    def sale(self, day, amount, router=None, commission=0):
        return VoucherSale.objects.create(business=self.b, router=router or self.r1, plan_name='Day', amount=D(amount),
                                          commission=D(commission), sold_at=_day_start(day) + timedelta(hours=10))

    def cost(self, day, amount, router=None):
        return Expense.objects.create(business=self.b, router=router, description='x', amount=D(amount),
                                      paid_at=_day_start(day) + timedelta(hours=11))

    def deal(self, investor=None, **kw):
        kw.setdefault('principal', D('10000'))
        kw.setdefault('share_pct', D('20'))
        kw.setdefault('payback_pct', D('70'))
        kw.setdefault('start_date', self.today - timedelta(days=5))
        kw.setdefault('status', 'active')
        return Investment.objects.create(business=self.b, investor=investor or self.alice, reference=svc.new_reference('INV'),
                                         title='Deal', **kw)


class TermsEngineTests(Base):
    def test_profit_share_split_while_capital_is_out(self):
        inv = self.deal()
        st = svc.State()
        r = svc.step(inv, st, D('1000'))
        self.assertEqual((r['earned'], r['to_principal'], r['to_return']), (D('200.00'), D('140.00'), D('60.00')))

    def test_after_payback_everything_is_profit_and_share_steps_down(self):
        inv = self.deal(principal=D('100'), after_payback_share_pct=D('10'))
        st = svc.State()
        svc.step(inv, st, D('1000'))                       # earns 200: 100 repays all capital (capped), 100 return
        self.assertEqual(st.repaid, D('100.00'))
        r = svc.step(inv, st, D('1000'))
        self.assertEqual(r['share_pct'], D('10'))
        self.assertEqual((r['earned'], r['to_principal'], r['to_return']), (D('100.00'), D('0'), D('100.00')))

    def test_fixed_return_split_and_exact_completion(self):
        inv = self.deal(terms='fixed', fixed_return_pct=D('30'), share_pct=D('50'))
        self.assertEqual(inv.total_due, D('13000.00'))
        self.assertEqual(inv.split_now, (D('76.9'), D('23.1')))
        st = svc.State()
        for _ in range(30):
            svc.step(inv, st, D('1000'))                   # 500 a day
        self.assertEqual(st.earned_total, D('13000.00'))
        self.assertEqual(st.repaid, D('10000.00'))
        self.assertEqual(st.returned, D('3000.00'))
        self.assertTrue(svc.is_complete(inv, st))
        self.assertEqual(svc.step(inv, st, D('1000'))['earned'], D('0'))

    def test_cap_ends_a_profit_share(self):
        inv = self.deal(principal=D('100'), cap_pct=D('150'), payback_pct=D('100'))
        st = svc.State()
        for _ in range(10):
            svc.step(inv, st, D('1000'))
        self.assertEqual(st.earned_total, D('150.00'))

    def test_losses_are_carried_forward(self):
        inv = self.deal()
        st = svc.State()
        self.assertEqual(svc.step(inv, st, D('-500'))['earned'], D('0'))
        r = svc.step(inv, st, D('1000'))
        self.assertEqual(r['loss_offset'], D('500.00'))
        self.assertEqual(r['earned'], D('100.00'))
        inv.carry_losses = False
        st = svc.State()
        svc.step(inv, st, D('-500'))
        self.assertEqual(svc.step(inv, st, D('1000'))['earned'], D('200.00'))


class AccrualTests(Base):
    def test_site_scope_counts_only_its_sales_and_costs_net_of_commission(self):
        inv = self.deal(scope='sites')
        inv.routers.set([self.r1])
        d = self.today - timedelta(days=2)
        self.sale(d, 1000, self.r1, commission=100)        # counts: 900
        self.sale(d, 5000, self.r2)                        # other site: ignored
        self.cost(d, 400, self.r1)                         # counts
        self.cost(d, 9999, None)                           # unassigned cost: not a site cost
        svc.accrue(inv)
        inv.refresh_from_db()
        a = inv.accruals.get(day=d)
        self.assertEqual((a.revenue, a.costs, a.profit, a.earned), (D('900.00'), D('400.00'), D('500.00'), D('100.00')))
        self.assertEqual(inv.accrued_through, self.today - timedelta(days=1))
        self.assertEqual(inv.accruals.count(), 5)

    def test_accrual_is_idempotent_and_today_is_live_only(self):
        inv = self.deal()
        self.sale(self.today - timedelta(days=1), 1000)
        self.sale(self.today, 500)
        svc.accrue(inv)
        svc.accrue(inv)
        inv.refresh_from_db()
        self.assertEqual(inv.earned_total, D('200.00'))
        self.assertEqual(svc.today_live(inv)['earned'], D('100.00'))
        self.assertFalse(inv.accruals.filter(day=self.today).exists())

    def test_recalculate_picks_up_backdated_changes(self):
        inv = self.deal()
        svc.accrue(inv)
        self.sale(self.today - timedelta(days=3), 1000)
        svc.recalculate(inv)
        inv.refresh_from_db()
        self.assertEqual(inv.earned_total, D('200.00'))

    def test_fixed_deal_completes_and_notifies(self):
        inv = self.deal(principal=D('100'), terms='fixed', fixed_return_pct=D('20'), share_pct=D('50'))
        self.sale(self.today - timedelta(days=3), 1000)
        svc.accrue(inv)
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'completed')
        self.assertEqual((inv.earned_total, inv.principal_repaid), (D('120.00'), D('100.00')))
        self.assertTrue(any('complete' in m.subject for m in mail.outbox))


class LifecycleTests(Base):
    def opp(self, **kw):
        kw.setdefault('target_amount', D('100000'))
        kw.setdefault('share_pct', D('40'))
        return InvestmentOpportunity.objects.create(business=self.b, reference=svc.new_reference('OPP'), title='New site',
                                                    summary='s', status='open', **kw)

    def test_offer_gets_pro_rata_share_and_runs_to_live(self):
        o = self.opp(min_ticket=D('1000'))
        inv = svc.place_offer(self.alice, o, '25000', 'happy to help')
        self.assertEqual(inv.share_pct, D('10.000'))                     # a quarter of the money → a quarter of 40 %
        self.assertTrue(any('New investment offer' in m.subject for m in mail.outbox))
        svc.decide(inv, True, 'welcome', self.owner)
        self.assertEqual(inv.status, 'awaiting_payment')
        svc.mark_paid(inv, 'Wave', 'WV-123')
        self.assertEqual(inv.status, 'payment_review')
        svc.activate(inv, self.owner)
        inv.refresh_from_db()
        self.assertEqual((inv.status, inv.start_date), ('active', self.today + timedelta(days=1)))
        self.assertEqual(svc.committed(o), D('25000.00'))

    def test_offer_limits(self):
        o = self.opp(min_ticket=D('5000'), max_ticket=D('50000'))
        for amount, msg in (('100', 'minimum'), ('60000', 'maximum')):
            with self.assertRaisesRegex(ValueError, msg):
                svc.place_offer(self.alice, o, amount)
        first = svc.place_offer(self.alice, o, '50000')
        svc.decide(first, True)
        second = svc.place_offer(self.bob, o, '50000')
        svc.decide(second, True)
        o.refresh_from_db()
        self.assertEqual(o.status, 'funded')
        with self.assertRaisesRegex(ValueError, 'not open'):
            svc.place_offer(self.alice, o, '5000')

    def test_partial_accept_rescales_the_share(self):
        o = self.opp()
        inv = svc.place_offer(self.alice, o, '50000')
        svc.decide(inv, True, amount='25000')
        self.assertEqual((inv.principal, inv.share_pct), (D('25000.00'), D('10.000')))

    def test_invited_only_opportunity_is_hidden_from_others(self):
        o = self.opp(visibility='invited')
        o.invited.set([self.alice])
        self.assertTrue(svc.visible_opportunities(self.alice).filter(pk=o.pk).exists())
        self.assertFalse(svc.visible_opportunities(self.bob).filter(pk=o.pk).exists())
        with self.assertRaises(ValueError):
            svc.place_offer(self.bob, o, '1000')

    def test_payout_cannot_exceed_what_was_earned(self):
        inv = self.deal()
        self.sale(self.today - timedelta(days=1), 1000)
        with self.assertRaisesRegex(ValueError, 'more than'):
            svc.record_payout(inv, '500')
        svc.record_payout(inv, '150', 'Wave', 'P1')
        self.assertEqual(svc.paid_out(inv), D('150.00'))

    def test_direct_deal_with_past_start_is_backfilled(self):
        self.sale(self.today - timedelta(days=10), 1000)
        inv = svc.create_direct(self.b, self.alice, {'title': 'Old deal', 'principal': '5000', 'share_pct': '10', 'terms': 'profit_share',
                                                      'scope': 'business', 'start_date': (self.today - timedelta(days=20)).isoformat(),
                                                      'already_paid': True, 'carry_losses': True, 'show_basis': True}, self.owner)
        inv.refresh_from_db()
        self.assertEqual((inv.status, inv.earned_total), ('active', D('100.00')))

    @override_settings(INVESTOR_PAYMENT_WEBHOOK_SECRET='s3cret', INVESTOR_PAYMENT_CHECKOUT_URL='https://pay.example/checkout')
    def test_webhook_activates_only_with_matching_amount_and_signature(self):
        import hashlib
        import hmac
        inv = self.deal(status='awaiting_payment', start_date=None)
        body = json.dumps({'reference': inv.reference, 'status': 'paid', 'amount': '10000.00'}).encode()
        url = reverse('investor_payment_webhook')
        self.assertEqual(self.client.post(url, body, content_type='application/json', HTTP_X_TAPTAP_SIGNATURE='bad').status_code, 401)
        sig = hmac.new(b's3cret', body, hashlib.sha256).hexdigest()
        self.assertEqual(self.client.post(url, body, content_type='application/json', HTTP_X_TAPTAP_SIGNATURE=sig).status_code, 200)
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'active')


class PortalTests(Base):
    def login(self, investor):
        url = svc.issue_link(investor)
        token = url.rstrip('/').split('/')[-1]
        r = self.client.get(reverse('investor_access', args=[token]))
        self.assertEqual(r.status_code, 302)

    def test_magic_link_is_single_use(self):
        url = svc.issue_link(self.alice)
        token = url.rstrip('/').split('/')[-1]
        self.assertEqual(self.client.get(reverse('investor_access', args=[token])).status_code, 302)
        self.client.post(reverse('investor_logout'))
        self.assertEqual(self.client.get(reverse('investor_access', args=[token])).status_code, 400)

    def test_request_link_never_reveals_who_is_an_investor(self):
        r1 = self.client.post(reverse('investor_request_link'), {'email': 'alice@example.com'})
        r2 = self.client.post(reverse('investor_request_link'), {'email': 'nobody@example.com'})
        self.assertEqual(r1['Location'], r2['Location'])
        self.assertEqual(len(mail.outbox), 1)

    def test_investor_only_sees_own_deals(self):
        mine, theirs = self.deal(self.alice), self.deal(self.bob)
        self.login(self.alice)
        self.assertEqual(self.client.get(reverse('investor_investment', args=[mine.reference])).status_code, 200)
        self.assertEqual(self.client.get(reverse('investor_investment', args=[theirs.reference])).status_code, 404)
        self.assertEqual(self.client.get(reverse('investor_investment_data', args=[theirs.reference])).status_code, 404)
        live = self.client.get(reverse('investor_live')).json()
        self.assertEqual([d['reference'] for d in live['deals']], [mine.reference])

    def test_signed_out_everywhere_kills_the_session(self):
        self.login(self.alice)
        self.assertEqual(self.client.get(reverse('investor_live')).status_code, 200)
        svc.sign_out_everywhere(self.alice)
        self.assertEqual(self.client.get(reverse('investor_live')).status_code, 401)

    def test_portal_pages_render(self):
        inv = self.deal()
        o = InvestmentOpportunity.objects.create(business=self.b, reference='OPP-TEST001', title='T', summary='s', status='open',
                                                 target_amount=D('1000'), share_pct=D('10'))
        self.sale(self.today - timedelta(days=1), 1000)
        self.login(self.alice)
        for url in (reverse('investor_portal'), reverse('investor_investment', args=[inv.reference]), reverse('investor_opportunity', args=[o.reference])):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        est = self.client.get(reverse('investor_estimate', args=[o.reference]) + '?amount=500').json()
        self.assertEqual(est['share_pct'], 5.0)
        self.assertIn('expected', est['scenarios'])

    def test_owner_pages_render(self):
        inv = self.deal()
        self.client.force_login(self.owner)
        for tab in ('overview', 'investors', 'opportunities', 'deal', 'settings'):
            self.assertEqual(self.client.get(reverse('investors_admin') + f'?tab={tab}').status_code, 200, tab)
        self.assertEqual(self.client.get(reverse('investment_admin', args=[inv.pk])).status_code, 200)
        self.assertIn('basis', self.client.get(reverse('investment_admin_data', args=[inv.pk])).json())


class FitSandboxTests(Base):
    def test_tools_never_reach_another_investors_deal(self):
        theirs = self.deal(self.bob)
        out = json.loads(investor_fit.call(self.alice, 'deal_detail', {'reference': theirs.reference}))
        self.assertIn('error', out)
        out = json.loads(investor_fit.call(self.alice, 'projection', {'reference': theirs.reference}))
        self.assertIn('error', out)
        portfolio = json.loads(investor_fit.call(self.alice, 'my_portfolio', {}))
        self.assertEqual(portfolio['deals'], [])

    def test_hidden_basis_stays_hidden(self):
        inv = self.deal(show_basis=False)
        out = json.loads(investor_fit.call(self.alice, 'deal_detail', {'reference': inv.reference}))
        self.assertNotIn('scope_last_30_days', out)

    def test_ask_runs_tools_as_the_investor(self):
        mine = self.deal()
        replies = [
            {'choices': [{'message': {'content': '', 'tool_calls': [{'id': 'c1', 'function': {'name': 'deal_detail', 'arguments': json.dumps({'reference': mine.reference})}}]}}]},
            {'choices': [{'message': {'content': 'You are doing well.'}}]},
        ]
        with mock.patch('core.fit_ai.complete', side_effect=replies) as m:
            self.assertEqual(investor_fit.ask(self.alice, 'how am I doing?'), 'You are doing well.')
        tool_msg = m.call_args_list[1][0][0][-1]
        self.assertEqual(tool_msg['role'], 'tool')
        self.assertIn(mine.reference, tool_msg['content'])


class SummaryTests(Base):
    def test_daily_summary_sent_once_after_the_hour(self):
        self.deal()
        self.sale(self.today - timedelta(days=1), 1000)
        svc.accrue_all()
        s = svc.settings_for(self.b)
        s.summary_hour = 0
        s.save()
        self.assertEqual(svc.send_due_summaries(), 1)
        self.assertEqual(svc.send_due_summaries(), 0)
        msg = [m for m in mail.outbox if 'summary' in m.subject][0]
        self.assertIn('200.00', msg.body)
        self.assertEqual(InvestorSummaryLog.objects.get(investor=self.alice).status, 'sent')
        self.assertEqual(InvestorSummaryLog.objects.get(investor=self.bob).status, 'skipped')   # nothing live for Bob
