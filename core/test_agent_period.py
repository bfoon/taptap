"""Agent page periods: today, 7 days, this/last month, last 6 months (default), a month, a year, from–to.

Run:  DB_ENGINE=sqlite python manage.py test core.test_agent_period
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .finance import _month_start, agent_period, agent_period_stats
from .models import Agent, Business, CashCollection, Voucher, VoucherBatch, VoucherSale


def sale(b, agent, amount, comm, days_ago):
    return VoucherSale.objects.create(business=b, agent=agent, plan_name='24 HOURS', amount=Decimal(amount), commission=Decimal(comm),
                                      sold_at=timezone.now() - timedelta(days=days_ago))


class PeriodTests(TestCase):
    def test_presets(self):
        today = timezone.localdate()
        p = agent_period({})
        self.assertEqual((p.preset, timezone.localtime(p.start).date()), ('6m', _month_start(today, 5)))
        self.assertEqual(p.bucket, 'month'); self.assertIn('Last 6 months', p.label)
        self.assertEqual(agent_period({'range': 'today'}).bucket, 'hour')
        self.assertEqual(agent_period({'range': '7d'}).days, 7)
        m = agent_period({'range': 'bymonth', 'month': '2026-02'})
        self.assertEqual((timezone.localtime(m.start).date().isoformat(), m.label), ('2026-02-01', 'February 2026'))
        self.assertEqual(timezone.localtime(m.end).date().isoformat(), '2026-03-01')
        y = agent_period({'range': 'byyear', 'year': '2025'})
        self.assertEqual((timezone.localtime(y.start).date().isoformat(), timezone.localtime(y.end).date().isoformat(), y.label),
                         ('2025-01-01', '2026-01-01', '2025'))
        c = agent_period({'range': 'custom', 'start': '2026-09-10', 'end': '2026-09-01'})          # reversed dates are swapped
        self.assertEqual(timezone.localtime(c.start).date().isoformat(), '2026-09-01')
        from datetime import date
        self.assertEqual(_month_start(date(2025, 12, 15), -1), date(2026, 1, 1))     # December → January
        self.assertEqual(_month_start(date(2026, 3, 9), 5), date(2025, 10, 1))
        for bad in ({'range': 'nonsense'}, {'range': 'bymonth', 'month': 'x'}, {'range': 'byyear', 'year': 'x'},
                    {'range': 'custom', 'start': 'x'}):
            self.assertIn(agent_period(bad).preset, ('6m', 'bymonth', 'byyear', '30d'))


@override_settings(AUTH_EMAIL_OTP=False)
class AgentPageTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@x.gm', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='TapTap KerrSering', owner_name='B', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.a = Agent.objects.create(business=self.b, name='Abdou Salam', commission_percent=10)
        sale(self.b, self.a, 100, 10, 1); sale(self.b, self.a, 200, 20, 3)          # recent
        sale(self.b, self.a, 400, 40, 400)                                          # over a year ago
        CashCollection.objects.create(business=self.b, agent=self.a, amount=Decimal('150'), collected_at=timezone.now() - timedelta(days=2))
        CashCollection.objects.create(business=self.b, agent=self.a, amount=Decimal('360'), collected_at=timezone.now() - timedelta(days=390))
        now = timezone.now()
        self.recent = VoucherBatch.objects.create(business=self.b, name='Recent batch', agent=self.a, issued_at=now - timedelta(days=5))
        self.old_open = VoucherBatch.objects.create(business=self.b, name='Old open batch', agent=self.a, issued_at=now - timedelta(days=300))
        Voucher.objects.create(business=self.b, code='OLD1', plan_name='24 HOURS', price=Decimal('25'), agent=self.a, batch=self.old_open)
        self.old_done = VoucherBatch.objects.create(business=self.b, name='Old finished batch', agent=self.a, issued_at=now - timedelta(days=300))
        self.client.force_login(self.owner)
        self.url = reverse('agent_detail', args=[self.a.pk])

    def test_stats(self):
        s = agent_period_stats(self.b, self.a, agent_period({}))
        self.assertEqual((s['sold'], s['gross'], s['commission'], s['handed'], s['handed_n']), (2, Decimal('300'), Decimal('30'), Decimal('150'), 1))
        self.assertEqual(len(s['points']), 6)                                       # one bar per month
        self.assertEqual(sum(p['sales'] for p in s['points']), Decimal('300'))
        self.assertEqual(s['handed_rate'], round(150 / 270 * 100, 1))
        week = agent_period_stats(self.b, self.a, agent_period({'range': '7d'}))
        self.assertEqual(len(week['points']), 7); self.assertEqual(week['best']['sales'], Decimal('200'))
        y = timezone.localdate().year - 1
        old = agent_period_stats(self.b, self.a, agent_period({'range': 'byyear', 'year': str(y)}))
        if (timezone.now() - timedelta(days=400)).year == y:
            self.assertEqual(old['gross'], Decimal('400'))

    def test_page_default_is_last_six_months(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('Showing <b>Last 6 months', html)
        self.assertIn('class="on" href="?range=6m"', html)
        self.assertIn('D300 in sales', html); self.assertIn('All time: 3 · D700', html)
        self.assertIn('Recent batch', html); self.assertIn('Old open batch', html); self.assertIn('Older · still open', html)
        self.assertNotIn('Old finished batch', html)
        self.assertIn('Sales and cash handed in', html)

    def test_every_preset_renders(self):
        for q in ('range=today', 'range=7d', 'range=month', 'range=last_month', 'range=6m', 'range=bymonth&month=2026-01',
                  f'range=byyear&year={timezone.localdate().year}', 'range=custom&start=2026-01-01&end=2026-03-31', 'range=bad'):
            r = self.client.get(f'{self.url}?{q}')
            self.assertEqual(r.status_code, 200, q)
        html = self.client.get(self.url + '?range=today').content.decode()
        self.assertIn('No sales in this period (Today)', html)
        self.assertIn('Old open batch', html)                                       # still open: never hidden
        self.assertNotIn('Recent batch</b>', html)

    def test_lists_follow_the_period(self):
        html = self.client.get(self.url + '?range=custom&start=2020-01-01&end=' + timezone.localdate().isoformat()).content.decode()
        self.assertIn('Old finished batch', html)
        self.assertIn('All time', html)
