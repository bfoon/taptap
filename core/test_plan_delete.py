"""Deleting plans to the bin.

Run:  DB_ENGINE=sqlite python manage.py test core.test_plan_delete
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import PlanForm
from .models import Business, Router, Voucher, VoucherBatch, VoucherPlan, VoucherSale
from .models_team import TeamMember

HTML = {'HTTP_ACCEPT': 'text/html'}


@override_settings(AUTH_EMAIL_OTP=False)
class PlanDeleteTests(TestCase):
    def setUp(self):
        now = timezone.now()
        self.owner = User.objects.create_user('pd@example.com', 'pd@example.com', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='B', owner_name='O', phone='1', trial_ends_at=now + timedelta(days=7))
        self.fresh = VoucherPlan.objects.create(business=self.biz, name='Promo', price=Decimal('5'), duration_minutes=60)
        self.busy = VoucherPlan.objects.create(business=self.biz, name='Day', price=Decimal('40'), duration_minutes=1440)
        self.batch = VoucherBatch.objects.create(business=self.biz, name='Promo batch', plan=self.fresh, quantity=2)
        for i in range(2):
            Voucher.objects.create(business=self.biz, batch=self.batch, code=f'P{i}', plan_name='Promo', price=5, duration_minutes=60)
        self.used = Voucher.objects.create(business=self.biz, code='D-USED', plan_name='Day', price=40, duration_minutes=1440, used_at=now)
        self.sold = Voucher.objects.create(business=self.biz, code='D-SOLD', plan_name='Day', price=40, duration_minutes=1440, sold_at=now)
        VoucherSale.objects.create(business=self.biz, voucher=self.sold, plan_name='Day', amount=Decimal('40'))

    def member(self, role, extra=()):
        u = User.objects.create_user(f'{role}@pd.com', f'{role}@pd.com', 'pw')
        TeamMember.objects.create(business=self.biz, user=u, role=role, extra_permissions=list(extra))
        return u

    def delete(self, plan, reason='discontinued'):
        return self.client.post(reverse('plan_delete', args=[plan.pk]), {'reason': reason}, **HTML)

    def empty(self, plan):
        """Only an empty plan can be deleted: its vouchers end (history) so it has no live voucher left."""
        Voucher.objects.filter(plan_name=plan.name).update(status='expired')

    def test_plan_with_vouchers_cannot_be_deleted_until_empty(self):
        self.client.force_login(self.owner)
        self.delete(self.fresh)                                         # still has 2 live vouchers: refused
        self.assertIsNone(VoucherPlan.all_objects.get(pk=self.fresh.pk).deleted_at)
        self.client.post(reverse('plan_move', args=[self.fresh.pk]), {'to': self.busy.pk}, **HTML)   # move them away
        self.assertEqual(Voucher.objects.filter(plan_name='Promo').count(), 0)
        self.delete(self.fresh)
        p = VoucherPlan.all_objects.get(pk=self.fresh.pk)
        self.assertEqual((p.delete_reason, p.deleted_by, p.active), ('discontinued', self.owner, False))
        self.assertFalse(self.biz.plans.filter(pk=p.pk).exists())
        self.assertEqual(VoucherBatch.objects.get(pk=self.batch.pk).plan, self.busy)   # the batch moved with its vouchers

    def test_reason_required(self):
        self.client.force_login(self.owner)
        self.delete(self.fresh, reason='')
        self.assertIsNone(VoucherPlan.all_objects.get(pk=self.fresh.pk).deleted_at)

    def test_used_plan_owner_and_admin_can_delete_used_kept(self):
        self.empty(self.busy)
        for who in (self.owner, self.member('admin')):
            VoucherPlan.all_objects.filter(pk=self.busy.pk).update(deleted_at=None)
            self.client.force_login(who)
            self.delete(self.busy)
            self.assertIsNotNone(VoucherPlan.all_objects.get(pk=self.busy.pk).deleted_at, who)
        used = Voucher.objects.get(pk=self.used.pk)
        self.assertEqual(used.plan_name, 'Day')  # history kept

    def test_used_plan_not_deletable_by_staff_with_plans_manage_extra(self):
        u = self.member('voucher_creator', extra=['plans.manage', 'plans.delete_used'])  # the second extra is ignored
        self.empty(self.busy); self.empty(self.fresh)
        self.client.force_login(u)
        self.delete(self.busy)
        self.assertIsNone(VoucherPlan.all_objects.get(pk=self.busy.pk).deleted_at)
        self.delete(self.fresh)  # but a never-used plan is fine for anyone who manages plans
        self.assertIsNotNone(VoucherPlan.all_objects.get(pk=self.fresh.pk).deleted_at)

    def test_roles_without_plans_manage_cannot_delete(self):
        for role in ('voucher_support', 'finance', 'viewer'):
            self.client.force_login(self.member(role))
            self.delete(self.fresh)
            self.assertIsNone(VoucherPlan.all_objects.get(pk=self.fresh.pk).deleted_at, role)

    def test_plans_page_buttons(self):
        self.client.force_login(self.owner)
        r = self.client.get(reverse('plans'), **HTML)
        self.assertContains(r, 'data-bs-target="#planDelete"', count=0)          # both still have vouchers
        self.assertContains(r, 'data-bs-target="#planMove"', count=2)
        self.assertContains(r, 'to another plan first')
        self.empty(self.fresh); self.empty(self.busy)
        r = self.client.get(reverse('plans'), **HTML)
        self.assertContains(r, 'data-bs-target="#planDelete"', count=2)
        self.assertContains(r, reverse('plan_detail', args=[self.fresh.pk]))  # plan details link is back
        self.client.force_login(self.member('voucher_creator', extra=['plans.manage']))
        r = self.client.get(reverse('plans'), **HTML)
        self.assertContains(r, 'data-bs-target="#planDelete"', count=1)
        self.assertContains(r, 'only an Owner or Admin can delete it')

    def test_bin_shows_deleted_plans(self):
        self.client.force_login(self.owner)
        self.empty(self.fresh)
        self.delete(self.fresh)
        r = self.client.get(reverse('voucher_bin'), {'tab': 'plans'}, **HTML)
        self.assertContains(r, 'Promo')
        self.assertContains(r, 'discontinued')

    def test_name_can_be_reused_and_sync_does_not_bring_it_back(self):
        from .sync import _profile_to_plan
        self.client.force_login(self.owner)
        self.empty(self.fresh)
        self.delete(self.fresh)
        router = Router.objects.create(business=self.biz, name='R', ip_address='10.0.0.1', username='u', password='p')
        summary = {'duplicate_plans_skipped': 0, 'pulled_plans': 0}
        self.assertIsNone(_profile_to_plan(router, {'name': 'Promo', 'shared-users': '1'}, summary, timezone.now()))
        self.assertEqual(summary['deleted_plans_skipped'], 1)
        f = PlanForm({'name': 'Promo', 'price': '6', 'duration_value': 1, 'duration_unit': 'hours', 'max_devices': 1, 'active': 'on'},
                     instance=VoucherPlan(business=self.biz))
        self.assertTrue(f.is_valid(), f.errors)
        f.save()
        self.assertEqual(self.biz.plans.get(name='Promo').price, Decimal('6'))
        dup = PlanForm({'name': 'Promo', 'price': '7', 'duration_value': 1, 'duration_unit': 'hours', 'max_devices': 1},
                       instance=VoucherPlan(business=self.biz))
        self.assertFalse(dup.is_valid())  # two live plans with one name are still refused

    def test_deleted_plan_detail_is_gone(self):
        self.client.force_login(self.owner)
        self.empty(self.fresh)
        self.delete(self.fresh)
        self.assertEqual(self.client.get(reverse('plan_detail', args=[self.fresh.pk]), **HTML).status_code, 404)
