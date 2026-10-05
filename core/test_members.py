"""Members after the reusable Member Plan refactor.

Run:
  DB_ENGINE=sqlite python manage.py test core.test_members core.test_member_plans
"""
import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import members as mem
from .agent import command_body, push_pending_vouchers
from .models import (
    AgentCommand,
    Business,
    PortalPage,
    Router,
    Voucher,
    VoucherCodeAlias,
    VoucherEvent,
    VoucherSale,
)
from .models_member_plans import MemberPlan, MemberPlanAssignment


HTML = {'HTTP_ACCEPT': 'text/html'}


@override_settings(AUTH_EMAIL_OTP=False)
class MemberTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            'm@example.com',
            'm@example.com',
            'pw12345678',
        )
        self.biz = Business.objects.create(
            user=self.owner,
            business_name='Kombo WiFi',
            owner_name='O',
            phone='1',
            trial_ends_at=timezone.now() + timedelta(days=7),
            is_unlimited=True,
        )
        self.router = Router.objects.create(
            business=self.biz,
            name='R1',
            ip_address='10.0.0.1',
            username='u',
            password='p',
        )
        self.month = MemberPlan.objects.create(
            business=self.biz,
            name='Member Monthly',
            price=Decimal('500'),
            duration_minutes=43200,
            duration_unit='months',
            max_devices=2,
            speed_limit='10M/10M',
            created_by=self.owner,
        )
        self.free = MemberPlan.objects.create(
            business=self.biz,
            name='Staff',
            price=Decimal('0'),
            duration_minutes=0,
            duration_unit='unlimited',
            max_devices=3,
            speed_limit='5M/5M',
            created_by=self.owner,
        )
        self.client.force_login(self.owner)

    def make(self, **kw):
        plan = kw.pop('plan', self.free)
        base = dict(
            username='fatou.j',
            password='Kombo2026',
            plan_value=str(plan.pk),
            user=self.owner,
        )
        base.update(kw)
        return mem.create_member(self.biz, **base)

    def test_username_and_password_can_be_different(self):
        v = self.make(username='Fatou.J')
        self.assertEqual(
            (v.code, v.password, v.login_password, v.login_type),
            ('fatou.j', 'Kombo2026', 'Kombo2026', 'member'),
        )
        self.assertTrue(v.is_member)
        self.assertEqual(v.member_plan_assignment.plan, self.free)

    def test_username_and_password_can_be_the_same(self):
        v = self.make(
            username='lamin',
            same=True,
        )
        self.assertEqual(
            (v.password, v.login_password),
            ('', 'lamin'),
        )

    def test_plan_controls_member_limits(self):
        v = self.make(
            plan=self.month,
            devices='19',
            rate_limit='1M/1M',
        )
        self.assertEqual(v.plan_name, self.month.name)
        self.assertEqual(v.price, Decimal('500'))
        self.assertEqual(v.duration_minutes, 43200)
        self.assertEqual(v.max_devices, 2)
        self.assertEqual(v.rate_limit, '10M/10M')
        self.assertEqual(v.router_profile, self.month.router_profile_name)

    def test_free_member_is_free_because_its_plan_is_free(self):
        v = self.make(plan=self.free)
        self.assertEqual(mem.kind_of(v), 'free')
        self.assertEqual(v.price, 0)
        self.assertEqual(v.duration_minutes, 0)
        Voucher.objects.filter(pk=v.pk).update(
            used_at=timezone.now()
        )
        from .views_live import missing_sales
        self.assertEqual(missing_sales(self.biz), ([], 0))

    def test_paying_member_records_first_payment(self):
        v = self.make(
            plan=self.month,
            paid=True,
            method='wave',
            reference='W123',
        )
        sale = VoucherSale.objects.get(voucher=v)
        self.assertEqual(sale.amount, Decimal('500'))
        self.assertEqual(sale.payment_method, 'wave')

    def test_member_requires_real_member_plan(self):
        with self.assertRaises(mem.MemberError):
            mem.create_member(
                self.biz,
                username='musa',
                password='Secret99',
                plan_value='free',
            )
        with self.assertRaises(mem.MemberError):
            mem.create_member(
                self.biz,
                username='musa',
                password='Secret99',
                plan_value='',
            )

    def test_username_rules(self):
        Voucher.objects.create(
            business=self.biz,
            code='ABCD1234',
            plan_name='Day',
        )
        v = Voucher.objects.create(
            business=self.biz,
            code='NEW1',
            plan_name='Day',
        )
        VoucherCodeAlias.objects.create(
            business=self.biz,
            voucher=v,
            code='OLDCODE',
        )
        for bad in (
            'abcd1234',
            'OldCode',
            'ab',
            '-dash',
            'x' * 40,
            'semi;colon',
        ):
            with self.assertRaises(mem.MemberError, msg=bad):
                self.make(username=bad)

    def test_password_rules(self):
        for bad in ('abc', 'has space', 'x' * 65):
            with self.assertRaises(mem.MemberError, msg=bad):
                self.make(
                    username='ok.user',
                    password=bad,
                )

    def test_page_creates_member_from_selected_member_plan(self):
        with mock.patch(
            'core.views_members.push_one',
            return_value=True,
        ):
            r = self.client.post(
                reverse('members'),
                {
                    'action': 'create_member',
                    'username': 'Musa',
                    'password': 'Secret99',
                    'plan': str(self.month.pk),
                    'router': self.router.pk,
                    'customer_name': 'Musa J',
                },
                **HTML,
            )
        self.assertEqual(r.status_code, 302)
        v = Voucher.objects.get(code='musa')
        self.assertEqual(v.member_plan_assignment.plan, self.month)
        self.assertEqual(v.max_devices, 2)
        self.assertEqual(v.rate_limit, '10M/10M')
        self.assertEqual(v.router_profile, self.month.router_profile_name)
        page = self.client.get(r['Location'], **HTML)
        self.assertContains(page, 'Secret99')
        self.assertContains(page, 'Member Monthly')

    def test_link_command_sets_member_password(self):
        body = command_body(
            SimpleNamespace(
                kind='hotspot_users',
                pk=1,
                params={
                    'profiles': [],
                    'users': [
                        {
                            'n': 'fatou.j',
                            'pw': 'Kombo2026',
                            'prof': self.free.router_profile_name,
                            'lim': '0s',
                            'dis': False,
                        },
                    ],
                },
            )
        )
        self.assertIn(
            'add name="fatou.j" password="Kombo2026"',
            body,
        )
        self.assertIn(
            'password="Kombo2026"',
            body,
        )

    def test_link_push_uses_member_plan_profile_and_password(self):
        v = self.make(
            username='fatou.j',
            password='Kombo2026',
            router=self.router,
            plan=self.month,
        )
        push_pending_vouchers(self.router)
        cmd = AgentCommand.objects.get(kind='hotspot_users')
        row = next(
            x
            for x in cmd.params['users']
            if x['n'] == v.code
        )
        profile = next(
            x
            for x in cmd.params['profiles']
            if x['name'] == row['prof']
        )
        self.assertEqual(row['pw'], 'Kombo2026')
        self.assertEqual(row['prof'], self.month.router_profile_name)
        self.assertEqual(profile['shared'], 2)
        self.assertEqual(profile['rate'], '10M/10M')

    def test_change_password_and_history(self):
        v = self.make()
        with mock.patch(
            'core.members._push_password',
            return_value=('TapTap only', 'saved', True),
        ):
            ok, _ = mem.change_password(
                v,
                'NewPass1',
                user=self.owner,
                reason='forgot',
            )
        self.assertTrue(ok)
        v.refresh_from_db()
        self.assertEqual(v.password, 'NewPass1')
        self.assertTrue(
            VoucherEvent.objects.filter(
                voucher=v,
                event='password_changed',
            ).exists()
        )

    def test_renew_uses_assigned_plan(self):
        v = self.make(
            plan=self.month,
            paid=True,
        )
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
        self.assertEqual(sale.amount, Decimal('500'))

    def test_legacy_member_must_be_assigned_before_renewal(self):
        v = Voucher.objects.create(
            business=self.biz,
            code='legacy',
            password='Secret99',
            login_type='member',
            plan_name='Old',
            price=100,
            duration_minutes=1440,
        )
        with self.assertRaises(mem.MemberError):
            mem.renew(v, user=self.owner)
        mem.assign_member_plan(
            v,
            self.month,
            user=self.owner,
        )
        self.assertEqual(
            MemberPlanAssignment.objects.get(member=v).plan,
            self.month,
        )

    def portal(self):
        return PortalPage.objects.create(
            business=self.biz,
            name='Login',
            slug='kombo',
            kind='login',
            is_published=True,
        )

    def check(self, page, **data):
        return self.client.post(
            reverse(
                'portal_check',
                args=[page.slug],
            ),
            json.dumps(data),
            content_type='application/json',
        )

    def test_portal_member_login(self):
        page = self.portal()
        self.make(
            username='fatou.j',
            password='Kombo2026',
        )
        ok = self.check(
            page,
            code='Fatou.J',
            password='Kombo2026',
            member=True,
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(
            (ok.json()['code'], ok.json()['member']),
            ('fatou.j', True),
        )
        self.assertEqual(
            self.check(
                page,
                code='fatou.j',
                password='wrong',
                member=True,
            ).status_code,
            404,
        )

    def test_members_page_filters_unassigned_legacy_members(self):
        self.make(username='planned')
        Voucher.objects.create(
            business=self.biz,
            code='legacy',
            login_type='member',
            password='Secret99',
            plan_name='Old custom',
        )
        r = self.client.get(
            reverse('members') + '?kind=unassigned',
            **HTML,
        )
        self.assertContains(r, 'legacy')
        self.assertNotContains(r, 'planned')
        self.assertContains(r, 'NEEDS MEMBER PLAN')
