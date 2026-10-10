"""Staff pay: fixed, % of sales, % of profit, min/cap, site deals, payments as expenses, guards, pages.

Run:  DB_ENGINE=sqlite python manage.py test core.test_payroll
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Business, Expense, Router, VoucherSale
from .models_payroll import StaffPay, StaffPayout
from .models_team import TeamMember
from .payroll import compute, month_start, payroll_month, prev_month, record_payout, void_payout

D = Decimal


def at(day, hour=12):
    return timezone.make_aware(timezone.datetime(day.year, day.month, day.day, hour))


class Base(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@x.gm', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='TapTap Brikama', owner_name='B', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.router = Router.objects.create(business=self.b, name='Westfield', ip_address='10.0.0.1', username='admin', password='x')
        self.last = prev_month(month_start())          # a completed month, so results are final
        self.users = {}

    def member(self, name, role='voucher_creator'):
        u = User.objects.create_user(name, f'{name}@x.gm', 'pw12345678', first_name=name.title())
        self.users[name] = u
        return TeamMember.objects.create(business=self.b, user=u, role=role)

    def sale(self, amount, day, comm=0, router=None):
        return VoucherSale.objects.create(business=self.b, plan_name='24 HOURS', amount=D(amount), commission=D(comm),
                                          sold_at=at(day), router=router)

    def expense(self, amount, day, router=None, category='bandwidth'):
        return Expense.objects.create(business=self.b, category=category, description='x', amount=D(amount),
                                      paid_at=at(day), router=router)


class EngineTests(Base):
    def test_fixed_salary_full_and_prorated(self):
        m = self.member('awa')
        pay = StaffPay.objects.create(business=self.b, member=m, pay_type='fixed', amount=D('3000'))
        self.assertEqual(compute(pay, self.last)['earned'], D('3000.00'))
        # Joined on the 16th of a 30/31-day month → paid for the days on the payroll.
        pay.starts_on = self.last.replace(day=16)
        c = compute(pay, self.last)
        days = c['days']
        self.assertEqual(c['days_on'], days - 15)
        self.assertEqual(c['earned'], (D('3000') * D(days - 15) / D(days)).quantize(D('0.01')))
        # Before they started: nothing.
        pay.starts_on = month_start()
        self.assertFalse(compute(pay, self.last)['started'])

    def test_percent_of_sales_with_basic_min_and_cap(self):
        m = self.member('lamin')
        self.sale(1000, self.last.replace(day=3)); self.sale(500, self.last.replace(day=10))
        self.sale(9999, month_start())                                          # other month — ignored
        pay = StaffPay.objects.create(business=self.b, member=m, pay_type='sales', percent=D('10'), amount=D('200'))
        c = compute(pay, self.last)
        self.assertEqual((c['basis_value'], c['share'], c['earned']), (D('1500'), D('150.00'), D('350.00')))
        pay.minimum = D('500')
        c = compute(pay, self.last)
        self.assertEqual(c['earned'], D('500.00')); self.assertTrue(c['topped_up'])
        pay.minimum, pay.cap = D('0'), D('300')
        c = compute(pay, self.last)
        self.assertEqual(c['earned'], D('300.00')); self.assertTrue(c['capped'])

    def test_percent_of_profit_ignores_staff_pay_and_never_negative(self):
        boss = self.member('fatou', role='finance')
        seller = self.member('ebrima')
        self.sale(2000, self.last.replace(day=2), comm=200)
        self.expense(800, self.last.replace(day=5))
        StaffPay.objects.create(business=self.b, member=seller, pay_type='fixed', amount=D('500'))
        p = StaffPay.objects.create(business=self.b, member=boss, pay_type='profit', percent=D('20'))
        # Paying the seller creates a Staff & wages expense, which must NOT reduce Fatou's profit share.
        record_payout(self.b, seller, self.last, 'salary', D('500'))
        c = compute(p, self.last)
        self.assertEqual(c['basis_value'], D('1000'))                 # 2000 − 200 commission − 800 bills
        self.assertEqual(c['share'], D('200.00'))
        self.expense(5000, self.last.replace(day=6))                  # now a loss
        self.assertEqual(compute(p, self.last)['share'], D('0'))

    def test_site_deal_counts_only_that_router(self):
        m = self.member('isatou')
        self.sale(1000, self.last.replace(day=4), router=self.router)
        self.sale(3000, self.last.replace(day=4))                    # another site
        self.expense(100, self.last.replace(day=4), router=self.router)
        self.expense(900, self.last.replace(day=4))                  # whole-business bill
        p = StaffPay.objects.create(business=self.b, member=m, pay_type='profit', percent=D('50'), site=self.router)
        self.assertEqual(compute(p, self.last)['basis_value'], D('900'))

    def test_month_in_progress_projects(self):
        m = self.member('modou')
        today = timezone.localdate()
        self.sale(600, today)
        p = StaffPay.objects.create(business=self.b, member=m, pay_type='sales', percent=D('10'))
        c = compute(p, month_start())
        self.assertTrue(c['in_progress']); self.assertEqual(c['share'], D('60.00'))
        if today.day < c['days']:
            self.assertGreater(c['projected'], c['earned'])

    def test_payroll_balances_advances_bonus_deduction_and_void(self):
        m = self.member('binta')
        StaffPay.objects.create(business=self.b, member=m, pay_type='fixed', amount=D('4000'))
        record_payout(self.b, m, self.last, 'advance', D('1000'))
        record_payout(self.b, m, self.last, 'bonus', D('500'))
        record_payout(self.b, m, self.last, 'deduction', D('200'))
        pr = payroll_month(self.b, self.last)
        r = pr['rows'][0]
        self.assertEqual((r['owed'], r['paid'], r['balance']), (D('4300.00'), D('1500'), D('2800.00')))
        self.assertEqual(pr['due'], D('2800.00'))
        # Cash payments are Staff & wages expenses; the deduction is not.
        self.assertEqual(Expense.objects.filter(business=self.b, category='salaries').count(), 2)
        sal = record_payout(self.b, m, self.last, 'salary', D('2800'))
        self.assertEqual(payroll_month(self.b, self.last)['rows'][0]['status'], 'paid')
        # Voiding removes the expense, and deleting the expense from Finance voids the payment.
        void_payout(sal)
        self.assertFalse(Expense.objects.filter(description__startswith='Salary').exists())
        adv = StaffPayout.objects.get(kind='advance')
        adv.expense.delete()
        self.assertFalse(StaffPayout.objects.filter(kind='advance').exists())

    def test_removed_member_history_survives(self):
        m = self.member('sainey')
        StaffPay.objects.create(business=self.b, member=m, pay_type='fixed', amount=D('1000'))
        record_payout(self.b, m, self.last, 'salary', D('1000'))
        m.delete()
        pr = payroll_month(self.b, self.last)
        self.assertEqual([(r['name'], r['status'], r['paid']) for r in pr['rows']], [('Sainey', 'removed', D('1000.00'))])


@override_settings(AUTH_EMAIL_OTP=False)
class PageTests(Base):
    def setUp(self):
        super().setUp()
        self.seller = self.member('kebba')
        self.admin = self.member('ndey', role='admin')
        self.client.force_login(self.owner)

    def test_owner_sets_pay_and_pays_from_finance(self):
        r = self.client.post(reverse('team_pay_save'), {'member': self.seller.pk, 'pay_type': 'sales', 'percent': '12.5',
                                                         'amount': '500', 'site': self.router.pk, 'cap': '5000', 'pay_day': '25'})
        self.assertEqual(r.status_code, 302)
        pay = StaffPay.objects.get(member=self.seller)
        self.assertEqual((pay.percent, pay.amount, pay.site, pay.pay_day), (D('12.5'), D('500'), self.router, 25))
        html = self.client.get(reverse('team')).content.decode()
        self.assertIn('12.5% of sales at Westfield', html)
        self.assertIn('payModal', html)

        self.sale(4000, self.last.replace(day=3), router=self.router)
        url = reverse('finance') + f'?tab=payroll&pm={self.last:%Y-%m}'
        html = self.client.get(url).content.decode()
        self.assertIn('Kebba', html); self.assertIn('Pay everyone', html)   # 500 + 12.5% × 4000 = 1000 due
        r = self.client.post(reverse('payroll_pay_all'), {'month': f'{self.last:%Y-%m}'})
        self.assertEqual(r.status_code, 302)
        e = Expense.objects.get(category='salaries')
        self.assertEqual((e.amount, e.router), (D('1000.00'), self.router))
        # It shows up in normal Finance expenses on the day it was paid (cash basis), labelled with the pay month.
        html = self.client.get(reverse('finance') + '?tab=expenses&range=month').content.decode()
        self.assertIn(f'Salary — Kebba ({self.last:%B %Y})', html)
        # The pay month itself no longer shows anything still to pay.
        html = self.client.get(reverse('finance') + '?tab=overview&range=last_month').content.decode()
        self.assertNotIn('Staff pay still to pay', html)
        # Payslip renders.
        r = self.client.get(reverse('payroll_payslip', args=[self.seller.pk, f'{self.last:%Y-%m}']))
        self.assertContains(r, 'PAID IN FULL')

    def test_unpaid_pay_shows_in_profit_and_loss(self):
        StaffPay.objects.create(business=self.b, member=self.seller, pay_type='fixed', amount=D('700'))
        html = self.client.get(reverse('finance') + '?tab=overview&range=last_month').content.decode()
        self.assertIn('Staff pay still to pay', html)
        self.assertIn('Profit after all staff pay', html)
        self.assertIn('Staff pay ·', html)                 # overview KPI (current month)

    def test_guards(self):
        # Nobody pays themselves; an admin cannot touch another admin's pay.
        self.client.force_login(self.users['ndey'])
        self.client.post(reverse('team_pay_save'), {'member': self.admin.pk, 'pay_type': 'fixed', 'amount': '9999'})
        self.assertFalse(StaffPay.objects.filter(member=self.admin).exists())
        other_admin = self.member('omar', role='admin')
        self.client.post(reverse('team_pay_save'), {'member': other_admin.pk, 'pay_type': 'fixed', 'amount': '9999'})
        self.assertFalse(StaffPay.objects.filter(member=other_admin).exists())
        # …but an admin can set a cashier's pay.
        self.client.post(reverse('team_pay_save'), {'member': self.seller.pk, 'pay_type': 'fixed', 'amount': '1500'})
        self.assertTrue(StaffPay.objects.filter(member=self.seller).exists())
        # A voucher creator has no payroll access at all, but can open My pay.
        self.client.force_login(self.users['kebba'])
        r = self.client.post(reverse('payroll_pay'), {'member': self.seller.pk, 'month': f'{self.last:%Y-%m}', 'amount': '10'})
        self.assertNotEqual(r.status_code, 200)
        self.assertFalse(StaffPayout.objects.exists())
        r = self.client.get(reverse('my_pay'))
        self.assertContains(r, 'D1,500')
        r = self.client.get(reverse('my_payslip', args=[f'{month_start():%Y-%m}']))
        self.assertContains(r, 'Kebba')

    def test_validation(self):
        self.client.post(reverse('team_pay_save'), {'member': self.seller.pk, 'pay_type': 'sales', 'percent': '0'})
        self.assertFalse(StaffPay.objects.exists())
        self.client.post(reverse('team_pay_save'), {'member': self.seller.pk, 'pay_type': 'fixed', 'amount': '0'})
        self.assertFalse(StaffPay.objects.exists())
        self.client.post(reverse('team_pay_save'), {'member': self.seller.pk, 'pay_type': 'sales', 'percent': '10',
                                                     'minimum': '900', 'cap': '500'})
        self.assertFalse(StaffPay.objects.exists())
        self.client.post(reverse('team_pay_save'), {'member': self.seller.pk, 'pay_type': 'fixed', 'amount': '100'})
        self.client.post(reverse('team_pay_save'), {'member': self.seller.pk, 'action': 'remove'})
        self.assertFalse(StaffPay.objects.exists())
