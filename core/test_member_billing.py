"""Member page billing: arrears shown, amounts collected recorded, renewal blocked while money is owed.

Run:  DB_ENGINE=sqlite python manage.py test core.test_member_billing
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .member_arrears import total_arrears
from .models import Business, Voucher
from .models_member_arrears import MemberBalancePayment
from .models_member_plans import MemberPlan, MemberPlanAssignment, MemberRenewal
from .views_member_detail import _ledger

DAY = 1440


@override_settings(AUTH_EMAIL_OTP=False)
class MemberBillingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@x.gm', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='Kairaba Net', owner_name='A', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=30), is_unlimited=True)
        self.plan = MemberPlan.objects.create(business=self.b, name='Monthly', price=Decimal('1000'), duration_minutes=30 * DAY,
                                              duration_unit='months', max_devices=2)
        now = timezone.now()
        self.m = Voucher.objects.create(business=self.b, code='jimmy', login_type='member', plan_name='Monthly', price=Decimal('1000'),
                                        duration_minutes=30 * DAY, status='active', customer_name='Jimmy Sowe',
                                        used_at=now - timedelta(days=20), expires_at=now + timedelta(days=10))
        MemberPlanAssignment.objects.create(member=self.m, plan=self.plan, assigned_by=self.owner)
        self.r = MemberRenewal.objects.create(business=self.b, member=self.m, plan=self.plan, member_username='jimmy', customer_name='Jimmy Sowe',
                                              plan_name='Monthly', plan_price=Decimal('1000'), amount_collected=Decimal('700'),
                                              duration_minutes=30 * DAY, renewed_at=now - timedelta(days=20))
        self.client.force_login(self.owner)
        self.url = reverse('member_detail', args=[self.m.pk])

    def pay(self, amount, **extra):
        return self.client.post(reverse('member_renew', args=[self.m.pk]),
                                {'mode': 'balance', 'amount': amount, 'method': 'cash', 'next': self.url + '#billing', **extra})

    def test_arrears_are_shown_with_a_payment_form(self):
        html = self.client.get(self.url).content.decode()
        for text in ('Arrears — still owed', 'D300', 'Owes D300', 'Record payment &amp; receipt', 'name="mode" value="balance"',
                     'tap to record a payment', 'Collect the D300 arrears first', self.r.receipt_number, 'owes D300'):
            self.assertIn(text, html, text)
        self.assertIn('max="300.00"', html)

    def test_record_part_then_the_rest(self):
        r = self.pay('200', reference='WAVE-1')
        p = MemberBalancePayment.objects.get(member=self.m)
        self.assertRedirects(r, reverse('members') + f'?balance_receipt={p.pk}&next=%2Fmembers%2F{self.m.pk}%2F%23billing', fetch_redirect_response=False)
        self.assertEqual((p.amount_collected, p.balance_before, p.balance_after), (Decimal('200'), Decimal('300'), Decimal('100')))
        self.assertEqual(total_arrears(self.m), Decimal('100'))
        html = self.client.get(self.url).content.decode()
        self.assertIn('Balance payment', html); self.assertIn('owes D100', html); self.assertIn('WAVE-1', html)
        self.pay('100')
        self.assertEqual(total_arrears(self.m), Decimal('0'))
        html = self.client.get(self.url).content.decode()
        self.assertIn('Nothing owed', html); self.assertNotIn('name="mode" value="balance"', html)
        self.assertIn('settled', html)

    def test_cannot_pay_more_than_owed_or_nothing(self):
        self.pay('500'); self.pay('0'); self.pay('abc')
        self.assertEqual(total_arrears(self.m), Decimal('300'))
        self.assertFalse(MemberBalancePayment.objects.exists())

    def test_renewal_blocked_while_owed_then_partial_renewal_creates_arrears(self):
        self.client.post(reverse('member_renew', args=[self.m.pk]), {'amount': '1000', 'method': 'cash', 'next': self.url})
        self.assertEqual(MemberRenewal.objects.filter(member=self.m).count(), 1)              # refused: D300 owed
        self.pay('300')
        self.client.post(reverse('member_renew', args=[self.m.pk]), {'amount': '600', 'method': 'cash', 'next': self.url})
        self.assertEqual(MemberRenewal.objects.filter(member=self.m).count(), 2)
        self.assertEqual(total_arrears(self.m), Decimal('400'))
        self.assertIn('Arrears — still owed', self.client.get(self.url).content.decode())

    def test_ledger_running_balance(self):
        self.pay('100')
        rows, paid, charged = _ledger(self.m)
        self.assertEqual([(x['kind'], x['balance']) for x in rows], [('payment', Decimal('200')), ('renewal', Decimal('300'))])
        self.assertEqual((paid, charged), (Decimal('800'), Decimal('1000')))

    def test_roles_without_create_rights_see_no_payment_form(self):
        from .models_team import TeamMember
        u = User.objects.create_user('sup', 's@x.gm', 'pw12345678')
        TeamMember.objects.create(business=self.b, user=u, role='voucher_support')
        self.client.force_login(u)
        html = self.client.get(self.url).content.decode()
        if 'Arrears' in html:                                                                     # role can open the page
            self.assertNotIn('name="mode" value="balance"', html)
        self.pay('100')
        self.assertEqual(total_arrears(self.m), Decimal('300'))


class ArrearsWiringTests(TestCase):
    def test_members_urls_resolve_to_the_arrears_wrappers(self):
        """Regression: the wrapper used to be set on URLPattern._callback, which Django 5 ignores."""
        from django.urls import resolve
        self.assertEqual(resolve(reverse('member_renew', args=[1])).func.__module__, 'core.member_arrears')
        self.assertEqual(resolve(reverse('members')).func.__module__, 'core.member_arrears')


@override_settings(AUTH_EMAIL_OTP=False)
class ChargeTests(MemberBillingTests):
    """Late fees and other charges: no renewal, no time added, paid now or later, waivable."""

    def charge(self, **data):
        base = {'action': 'charge', 'kind': 'late_fee', 'amount': '150', 'description': 'Paid 6 days late'}
        base.update(data)
        return self.client.post(reverse('member_support', args=[self.m.pk]), base)

    def test_late_fee_paid_later_shows_as_owed(self):
        before = Voucher.objects.get(pk=self.m.pk).expires_at
        r = self.charge()
        self.assertRedirects(r, self.url + '#billing', fetch_redirect_response=False)
        self.assertEqual(total_arrears(self.m), Decimal('450'))
        self.assertEqual(Voucher.objects.get(pk=self.m.pk).expires_at, before)                    # no time added
        self.assertEqual(MemberRenewal.objects.filter(member=self.m).count(), 1)                  # not a renewal
        html = self.client.get(self.url).content.decode()
        self.assertIn('Late fee — Paid 6 days late: D150', html); self.assertIn('D450', html); self.assertIn('Waive D150', html)

    def test_paying_now_goes_to_the_fee_first(self):
        r = self.charge(pay_now='1', method='wave', reference='WV-1')
        p = MemberBalancePayment.objects.get(member=self.m)
        self.assertIn(f'balance_receipt={p.pk}', r['Location'])
        self.assertEqual(p.allocations[0]['charge_id'], self.m.member_charges.get().pk)
        self.assertEqual((p.amount_collected, p.balance_before, p.balance_after), (Decimal('150'), Decimal('450'), Decimal('300')))
        self.assertEqual(total_arrears(self.m), Decimal('300'))                                   # the old renewal balance is untouched
        receipt = self.client.get(r['Location']).content.decode()
        self.assertIn('Late fee — Paid 6 days late', receipt)
        from .models import VoucherSale
        self.assertEqual(VoucherSale.objects.get(pk=p.sale_id).amount, Decimal('150'))            # booked as income when paid

    def test_part_payment_and_checks(self):
        self.charge(pay_now='1', paid_amount='100')
        self.assertEqual(total_arrears(self.m), Decimal('350'))                                   # 300 + 50 left on the fee
        n = self.m.member_charges.count()
        self.charge(pay_now='1', paid_amount='200')                                               # more than the fee
        self.charge(kind='other', description='')                                                 # must say what for
        self.charge(amount='0')
        self.assertEqual(self.m.member_charges.count(), n)

    def test_waive(self):
        self.charge()
        c = self.m.member_charges.get()
        self.client.post(reverse('member_support', args=[self.m.pk]), {'action': 'waive', 'charge': c.pk})          # needs a reason
        self.assertEqual(total_arrears(self.m), Decimal('450'))
        self.client.post(reverse('member_support', args=[self.m.pk]), {'action': 'waive', 'charge': c.pk, 'reason': 'First time'})
        self.assertEqual(total_arrears(self.m), Decimal('300'))
        html = self.client.get(self.url).content.decode()
        self.assertIn('Waived · Late fee', html); self.assertIn('D150 no longer owed', html)

    def test_part_paid_fee_waives_only_the_rest(self):
        self.charge(pay_now='1', paid_amount='100')
        c = self.m.member_charges.get()
        self.client.post(reverse('member_support', args=[self.m.pk]), {'action': 'waive', 'charge': c.pk, 'reason': 'Goodwill'})
        self.assertEqual(total_arrears(self.m), Decimal('300'))
        rows, paid, charged = _ledger(self.m)
        self.assertEqual(rows[0]['kind'], 'waived'); self.assertEqual(rows[0]['charged'], Decimal('-50'))
        self.assertEqual(rows[0]['balance'], Decimal('300'))

    def test_members_list_and_renewal_rule_count_charges(self):
        from .member_arrears import summaries_for_members
        self.pay('300')                                                                            # old renewal settled
        self.charge()
        self.assertEqual(summaries_for_members([self.m])[self.m.pk]['arrears'], Decimal('150'))
        self.client.post(reverse('member_renew', args=[self.m.pk]), {'amount': '1000', 'method': 'cash', 'next': self.url})
        self.assertEqual(MemberRenewal.objects.filter(member=self.m).count(), 1)                  # collect the fee first
        self.pay('150')
        self.client.post(reverse('member_renew', args=[self.m.pk]), {'amount': '1000', 'method': 'cash', 'next': self.url})
        self.assertEqual(MemberRenewal.objects.filter(member=self.m).count(), 2)

    def test_charge_with_nothing_else_owed(self):
        self.pay('300')
        self.charge(amount='75', kind='reconnection', description='', pay_now='1')
        self.assertEqual(total_arrears(self.m), Decimal('0'))
        self.assertIn('Reconnection fee', self.client.get(self.url).content.decode())

    def test_roles_without_create_rights_cannot_charge(self):
        from .models_team import TeamMember
        u = User.objects.create_user('sup2', 's2@x.gm', 'pw12345678')
        TeamMember.objects.create(business=self.b, user=u, role='voucher_support')
        self.client.force_login(u)
        self.charge()
        self.assertFalse(self.m.member_charges.exists())
