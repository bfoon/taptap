from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.utils import timezone

from .finance import Period, stock_movement
from .models import Agent, Business, Voucher


class StockMovementTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('o', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1', currency='D',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        tz = timezone.get_current_timezone()
        self.start = timezone.make_aware(datetime(2026, 9, 1), tz); self.end = timezone.make_aware(datetime(2026, 10, 1), tz)
        self.before, self.during = self.start - timedelta(days=5), self.start + timedelta(days=10)
        self.agent = Agent.objects.create(business=self.b, name='Awa')

    def v(self, code, created, plan='1 Day', price=25, **kw):
        v = Voucher.objects.create(business=self.b, code=code, plan_name=plan, price=price, **kw)
        Voucher.all_objects.filter(pk=v.pk).update(created_at=created)
        return v

    def test_roll_forward(self):
        self.v('OLD00001', self.before)                                         # carried forward, still unsold
        self.v('OLD00002', self.before, agent=self.agent)                       # carried forward, held by an agent
        self.v('OLD00003', self.before, sold_at=self.during)                    # carried forward, sold this month
        self.v('OLD00004', self.before, sold_at=self.before)                    # sold last month: not carried
        self.v('OLD00005', self.before, used_at=self.during)                    # carried forward, used this month
        self.v('NEW00001', self.during, plan='1 Week', price=100)               # generated this month
        self.v('NEW00002', self.during, deleted_at=self.during + timedelta(days=1))   # generated then deleted
        self.v('MEM00001', self.before, login_type='member')                    # members not counted
        m = stock_movement(self.b, Period(self.start, self.end, 'month', 'September 2026'))
        self.assertEqual((m['opening']['n'], m['opening_agents']['n'], m['opening']['value']), (4, 1, Decimal('100')))
        self.assertEqual((m['added']['n'], m['left']['n'], m['removed']['n']), (2, 2, 1))
        self.assertEqual(m['closing']['n'], m['opening']['n'] + m['added']['n'] - m['left']['n'] - m['removed']['n'])   # 3
        week = next(r for r in m['plans'] if r['plan'] == '1 Week')
        self.assertEqual((week['added'], week['closing'], week['closing_value']), (1, 1, Decimal('100')))

    def test_finance_page_shows_it(self):
        self.v('OLD00001', timezone.now() - timedelta(days=60))
        r = Client(); r.force_login(self.owner)
        page = r.get('/finance/?range=month')
        self.assertContains(page, 'Carried forward'); self.assertContains(page, 'Voucher stock this period')
