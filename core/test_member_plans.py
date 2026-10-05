"""Member Plans: reusable price/device/speed/validity for members."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import members as mem
from .models import Business, Router, Voucher, VoucherPlan, VoucherSale
from .models_member_plans import MemberPlan, MemberPlanAssignment
from .utils import voucher_profile


HTML = {'HTTP_ACCEPT': 'text/html'}


@override_settings(AUTH_EMAIL_OTP=False)
class MemberPlanTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            'owner@example.com',
            'owner@example.com',
            'pw12345678',
        )
        self.business = Business.objects.create(
            user=self.owner,
            business_name='Kombo WiFi',
            owner_name='Owner',
            phone='1',
            trial_ends_at=timezone.now() + timedelta(days=30),
            is_unlimited=True,
        )
        self.router = Router.objects.create(
            business=self.business,
            name='R1',
            ip_address='10.0.0.1',
            username='u',
            password='p',
        )
        self.month = MemberPlan.objects.create(
            business=self.business,
            name='Member Monthly',
            price=Decimal('500.00'),
            duration_minutes=43200,
            duration_unit='months',
            max_devices=2,
            speed_limit='10M/10M',
            created_by=self.owner,
        )
        self.free = MemberPlan.objects.create(
            business=self.business,
            name='Staff Free',
            price=Decimal('0.00'),
            duration_minutes=0,
            duration_unit='unlimited',
            max_devices=3,
            speed_limit='5M/5M',
            created_by=self.owner,
        )
        # A normal voucher plan must never appear as a Member Plan.
        self.voucher_plan = VoucherPlan.objects.create(
            business=self.business,
            name='Voucher Monthly',
            price=Decimal('400.00'),
            duration_minutes=43200,
            max_devices=1,
        )
        self.client.force_login(self.owner)

    def make(self, username='fatou.j', plan=None, **kwargs):
        return mem.create_member(
            self.business,
            username=username,
            password=kwargs.pop('password', 'Secret99'),
            plan_value=str((plan or self.month).pk),
            user=self.owner,
            **kwargs,
        )

    def test_new_member_requires_member_plan(self):
        with self.assertRaises(mem.MemberError):
            mem.create_member(
                self.business,
                username='fatou.j',
                password='Secret99',
                plan_value='',
            )
        with self.assertRaises(mem.MemberError):
            mem.create_member(
                self.business,
                username='fatou.j',
                password='Secret99',
                plan_value=str(self.voucher_plan.pk),
            )

    def test_member_inherits_price_devices_speed_and_profile(self):
        v = self.make()
        self.assertEqual(v.plan_name, 'Member Monthly')
        self.assertEqual(v.price, Decimal('500.00'))
        self.assertEqual(v.duration_minutes, 43200)
        self.assertEqual(v.max_devices, 2)
        self.assertEqual(v.rate_limit, '10M/10M')
        self.assertEqual(
            v.router_profile,
            self.month.router_profile_name,
        )
        self.assertEqual(
            MemberPlanAssignment.objects.get(member=v).plan,
            self.month,
        )
        # The router profile is plan-owned, not member-owned.
        self.assertEqual(
            voucher_profile(v, self.month),
            (
                self.month.router_profile_name,
                2,
                '10M/10M',
            ),
        )

    def test_member_form_cannot_override_plan_devices_or_speed(self):
        v = self.make(
            devices=19,
            rate_limit='1M/1M',
        )
        self.assertEqual(v.max_devices, 2)
        self.assertEqual(v.rate_limit, '10M/10M')

    def test_all_members_on_one_plan_share_router_profile(self):
        a = self.make(username='fatou.j')
        b = self.make(username='lamin.s')
        self.assertEqual(a.router_profile, b.router_profile)
        self.assertEqual(
            a.router_profile,
            self.month.router_profile_name,
        )

    def test_unlimited_member_plan_uses_safe_unlimited_profile(self):
        v = self.make(username='staff.one', plan=self.free)
        self.assertEqual(v.duration_minutes, 0)
        self.assertTrue(
            v.router_profile.startswith(
                'taptap-unlimited-member-plan-'
            )
        )
        self.assertEqual(
            voucher_profile(v, self.free),
            (
                self.free.router_profile_name,
                3,
                '5M/5M',
            ),
        )

    def test_first_payment_uses_member_plan_price(self):
        v = self.make(
            paid=True,
            method='wave',
            reference='W123',
        )
        sale = VoucherSale.objects.get(voucher=v)
        self.assertEqual(sale.amount, Decimal('500.00'))
        self.assertEqual(sale.payment_method, 'wave')

    def test_plan_edit_refreshes_assigned_member_limits(self):
        v = self.make()
        plan, refreshed = mem.save_member_plan(
            self.business,
            {
                'name': 'Member Monthly Plus',
                'price': '650',
                'max_devices': '4',
                'speed_limit': '20M/20M',
                'duration_value': '1',
                'duration_unit': 'months',
            },
            plan=self.month,
            user=self.owner,
        )
        self.assertEqual(refreshed, 1)
        v.refresh_from_db()
        self.assertEqual(v.plan_name, 'Member Monthly Plus')
        self.assertEqual(v.price, Decimal('650.00'))
        self.assertEqual(v.max_devices, 4)
        self.assertEqual(v.rate_limit, '20M/20M')
        self.assertEqual(v.router_profile, plan.router_profile_name)
        self.assertEqual(v.mikrotik_sync_status, 'Pending')

    def test_existing_legacy_member_can_be_assigned_to_plan(self):
        legacy = Voucher.objects.create(
            business=self.business,
            code='legacy.user',
            password='Secret99',
            login_type='member',
            plan_name='Old custom',
            price=99,
            duration_minutes=1440,
            max_devices=1,
            rate_limit='1M/1M',
        )
        mem.assign_member_plan(
            legacy,
            self.month,
            user=self.owner,
        )
        legacy.refresh_from_db()
        self.assertEqual(
            legacy.member_plan_assignment.plan,
            self.month,
        )
        self.assertEqual(legacy.max_devices, 2)
        self.assertEqual(legacy.rate_limit, '10M/10M')
        self.assertEqual(
            legacy.router_profile,
            self.month.router_profile_name,
        )

    def test_member_plan_is_separate_from_voucher_plans_on_page(self):
        response = self.client.get(reverse('members'), **HTML)
        self.assertContains(response, 'Member Monthly')
        self.assertContains(response, 'Staff Free')
        self.assertNotContains(response, 'Voucher Monthly')

    def test_page_create_member_uses_selected_plan(self):
        with mock.patch(
            'core.views_members.push_one',
            return_value=True,
        ):
            response = self.client.post(
                reverse('members'),
                {
                    'action': 'create_member',
                    'username': 'musa',
                    'password': 'Secret99',
                    'plan': str(self.month.pk),
                    'router': str(self.router.pk),
                    'paid': '1',
                    'method': 'cash',
                },
                **HTML,
            )
        self.assertEqual(response.status_code, 302)
        v = Voucher.objects.get(code='musa')
        self.assertEqual(v.member_plan_assignment.plan, self.month)
        self.assertEqual(v.max_devices, 2)
        self.assertEqual(v.rate_limit, '10M/10M')

    def test_create_member_plan_on_members_page(self):
        response = self.client.post(
            reverse('members'),
            {
                'action': 'save_plan',
                'name': 'Family Members',
                'price': '900',
                'max_devices': '5',
                'speed_limit': '15M/15M',
                'duration_value': '1',
                'duration_unit': 'months',
            },
            **HTML,
        )
        self.assertEqual(response.status_code, 302)
        p = MemberPlan.objects.get(
            business=self.business,
            name='Family Members',
        )
        self.assertEqual(p.price, Decimal('900.00'))
        self.assertEqual(p.max_devices, 5)
        self.assertEqual(p.speed_limit, '15M/15M')

    def test_renew_uses_current_member_plan_not_old_snapshot(self):
        v = self.make(paid=True)
        MemberPlan.objects.filter(pk=self.month.pk).update(
            price=Decimal('700.00')
        )
        self.month.refresh_from_db()

        Voucher.objects.filter(pk=v.pk).update(
            used_at=timezone.now() - timedelta(days=31),
        )
        v.refresh_from_db()

        ok, _, sale, minutes = mem.renew(
            v,
            method='cash',
            user=self.owner,
        )
        self.assertTrue(ok)
        self.assertEqual(minutes, 43200)
        self.assertEqual(sale.amount, Decimal('700.00'))
        v.refresh_from_db()
        self.assertEqual(v.price, Decimal('700.00'))

    def test_unlimited_plan_cannot_be_renewed(self):
        v = self.make(username='staff.one', plan=self.free)
        with self.assertRaises(mem.MemberError):
            mem.renew(v, user=self.owner)
