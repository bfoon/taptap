"""Finance → Agents & cash: totals (handed in, owed, commission, agents) and the top-agent spotlight.

Run:  DB_ENGINE=sqlite python manage.py test core.test_agent_overview
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .finance import Period, agent_balances, agent_overview
from .models import Agent, Business, CashCollection, Voucher, VoucherSale


def sale(b, agent, amount, comm, days_ago=0):
    return VoucherSale.objects.create(business=b, agent=agent, plan_name='24 HOURS', amount=Decimal(amount), commission=Decimal(comm),
                                      sold_at=timezone.now() - timedelta(days=days_ago))


@override_settings(AUTH_EMAIL_OTP=False)
class AgentOverviewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@x.gm', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='TapTap KerrSering', owner_name='B', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.salam = Agent.objects.create(business=self.b, name='Abdou Salam', commission_percent=10)
        self.cherno = Agent.objects.create(business=self.b, name='Cherno', commission_percent=10)
        self.rahim = Agent.objects.create(business=self.b, name='Abdou Rahim', commission_percent=10)
        self.old = Agent.objects.create(business=self.b, name='Old shop', active=False)
        # Salam: 1000 sold this week (100 comm), handed in 900 → owes 0, reliable
        sale(self.b, self.salam, 600, 60, 1); sale(self.b, self.salam, 400, 40, 2)
        CashCollection.objects.create(business=self.b, agent=self.salam, amount=Decimal('900'), collected_at=timezone.now() - timedelta(days=1))
        # Cherno: 1500 sold this week, never handed in → owes 1350, slow
        sale(self.b, self.cherno, 1500, 150, 1)
        # Rahim: 500 sold long ago, handed in 200 twenty days ago → owes 250, slow; nothing this period
        sale(self.b, self.rahim, 500, 50, 40)
        CashCollection.objects.create(business=self.b, agent=self.rahim, amount=Decimal('200'), collected_at=timezone.now() - timedelta(days=20))
        Voucher.objects.create(business=self.b, code='HELD1', plan_name='24 HOURS', price=Decimal('25'), agent=self.salam)
        now = timezone.now()
        self.period = Period(now - timedelta(days=7), now + timedelta(minutes=1), 'last7', 'Last 7 days')
        self.client.force_login(self.owner)

    def test_totals(self):
        o = agent_overview(self.b, self.period)
        self.assertEqual((o['gross'], o['commission'], o['handed'], o['outstanding']),
                         (Decimal('3000'), Decimal('300'), Decimal('1100'), Decimal('1600')))
        self.assertEqual((o['count'], o['active'], o['inactive'], o['holding'], o['holding_value']), (4, 3, 1, 1, Decimal('25')))
        self.assertEqual(o['owing_count'], 2)
        self.assertEqual({r['agent'].name for r in o['slow']}, {'Cherno', 'Abdou Rahim'})
        self.assertAlmostEqual(o['collection_rate'], round(1100 / 2700 * 100, 1))
        # money flow: commission + handed + still out = all agent sales
        self.assertAlmostEqual(o['flow']['commission'] + o['flow']['handed'] + o['flow']['outstanding'], 100.0, delta=0.2)
        self.assertEqual((o['period_handed'], o['period_commission'], o['period_gross']), (Decimal('900'), Decimal('250'), Decimal('2500')))

    def test_leaderboard_and_badges(self):
        o = agent_overview(self.b, self.period)
        self.assertEqual([x['agent'].name for x in o['board']], ['Cherno', 'Abdou Salam'])         # Rahim sold nothing this period
        top = o['top']
        self.assertEqual((top['agent'].name, top['gross'], top['sold'], top['share'], top['bar']), ('Cherno', Decimal('1500'), 1, 60.0, 100.0))
        self.assertEqual(o['reliable'].get('agent').name, 'Abdou Salam')
        self.assertEqual(o['biggest_debt']['agent'].name, 'Cherno')
        self.assertEqual(o['board'][1]['collection_rate'], 100.0)

    def test_ties_go_to_the_better_payer(self):
        sale(self.b, self.salam, 500, 50, 1)                                                      # Salam now 1500 too
        o = agent_overview(self.b, self.period)
        self.assertEqual(o['top']['agent'].name, 'Abdou Salam')

    def test_matches_the_agent_cards(self):
        cards = agent_balances(self.b)
        o = agent_overview(self.b, self.period, cards)
        self.assertEqual(o['outstanding'], sum(r['outstanding'] for r in cards if r['outstanding'] > 0))
        self.assertEqual(o['handed'], sum(r['collected'] for r in cards))

    def test_page(self):
        html = self.client.get(reverse('finance') + '?tab=agents&preset=last7').content.decode()
        for text in ('Where agents’ sales went', 'Cash handed in', 'Cash still owed', 'Commission earned', 'Top agent',
                     'Most reliable', 'Biggest balance', 'Cherno', 'class="crown"', 'D3,000'):
            self.assertIn(text, html, text)

    def test_no_sales_in_period_and_no_agents(self):
        now = timezone.now()
        empty = Period(now + timedelta(days=1), now + timedelta(days=2), 'custom', 'Tomorrow')
        o = agent_overview(self.b, empty)
        self.assertIsNone(o['top']); self.assertEqual(o['board'], [])
        other = User.objects.create_user('x', 'x@x.gm', 'pw12345678')
        b2 = Business.objects.create(user=other, business_name='X', owner_name='X', phone='1', trial_ends_at=now + timedelta(days=9), is_unlimited=True)
        o2 = agent_overview(b2, self.period)
        self.assertEqual((o2['count'], o2['gross'], o2['collection_rate']), (0, Decimal('0'), 100.0))
        self.client.force_login(other)
        self.assertNotIn('Top agent', self.client.get(reverse('finance') + '?tab=agents').content.decode())   # no agents: no summary
