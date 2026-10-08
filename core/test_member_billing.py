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
