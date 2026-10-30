"""Member plans only work with members: they never become voucher plans, and no voucher can use one."""
import importlib
from datetime import timedelta

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from core import router_profiles, sync
from core.forms import PlanForm
from core.models import Business, Router, RouterHotspotProfile, Voucher, VoucherPlan
from core.models_member_plans import MemberPlan


class MemberPlansSeparateTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user('o', 'o@x.gm', 'pw')
        self.biz = Business.objects.create(user=self.u, business_name='Kunda Net', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.router = Router.objects.create(business=self.biz, name='hAP', ip_address='10.0.0.1', username='a', password='b')
        self.mp = MemberPlan.objects.create(business=self.biz, name='Family Monthly', price=500, duration_minutes=43200, max_devices=3)
        self.profile = self.mp.router_profile_name

    def summary(self):
        return {'duplicate_plans_skipped': 0, 'pulled_plans': 0, 'errors': []}

    def test_sync_keeps_member_profiles_out_of_voucher_plans(self):
        s = self.summary()
        got = sync._profile_to_plan(self.router, {'name': self.profile, 'shared-users': '3', 'comment': 'TapTap member plan · Family Monthly · price=500'}, s, timezone.now())
        self.assertIsNone(got)
        self.assertFalse(VoucherPlan.all_objects.filter(name__iexact=self.profile).exists())
        self.assertTrue(RouterHotspotProfile.objects.filter(router=self.router, name=self.profile).exists())   # still mirrored
        self.assertEqual(s['member_profiles_kept_out'], 1)

    def test_sync_still_imports_normal_profiles(self):
        got = sync._profile_to_plan(self.router, {'name': '1Day', 'shared-users': '1', 'comment': 'price: 25'}, self.summary(), timezone.now())
        self.assertIsNotNone(got)
        self.assertTrue(self.biz.plans.filter(name='1Day').exists())

    def test_voucher_plan_cannot_take_member_profile_name(self):
        with self.assertRaises(ValidationError):
            VoucherPlan.objects.create(business=self.biz, name=self.profile, price=10)
        form = PlanForm({'name': 'taptap-unlimited-member-plan-9', 'price': '10', 'duration_value': 1, 'duration_unit': 'days', 'max_devices': 1, 'active': True},
                        instance=VoucherPlan(business=self.biz))
        self.assertFalse(form.is_valid())
        self.assertIn('members', form.errors['name'][0])

    def test_no_voucher_on_a_member_plan(self):
        for kw in ({'plan_name': self.profile}, {'plan_name': '1Day', 'router_profile': self.profile}):
            with self.assertRaises(ValidationError):
                Voucher.objects.create(business=self.biz, router=self.router, code='ABC123', source='taptap', **kw)
        # members still use their plan's profile
        m = Voucher.objects.create(business=self.biz, router=self.router, code='awa', password='secret', login_type='member',
                                   plan_name=self.mp.name, router_profile=self.profile, source='taptap')
        self.assertTrue(m.pk)

    def test_router_profiles_page_marks_member_profiles(self):
        RouterHotspotProfile.objects.create(business=self.biz, router=self.router, name=self.profile, is_present=True,
                                            raw_data={'name': self.profile, 'shared-users': '3'}, last_seen_at=timezone.now())
        rows = router_profiles.gather(self.biz)[0]['profiles']
        row = next(p for p in rows if p['name'] == self.profile)
        self.assertTrue(row['member_profile'])
        self.assertEqual(row['member_plan'], self.mp)
        self.assertIsNone(row['plan'])                          # no "Make a plan" button

    def test_migration_bins_old_rows(self):
        VoucherPlan.objects.bulk_create([VoucherPlan(business=self.biz, name=self.profile, price=500, source='mikrotik'),
                                         VoucherPlan(business=self.biz, name='1Day', price=25)])     # bulk_create bypasses the guard, like old data
        mig = importlib.import_module('core.migrations.0107_member_plans_off_voucher_plans')
        mig.forwards(django_apps, None)
        self.assertFalse(self.biz.plans.filter(name=self.profile).exists())
        self.assertTrue(VoucherPlan.all_objects.binned().filter(name=self.profile, delete_info__auto='member-plan-separation').exists())
        self.assertTrue(self.biz.plans.filter(name='1Day').exists())
        mig.backwards(django_apps, None)
        self.assertTrue(VoucherPlan.all_objects.filter(name=self.profile, deleted_at__isnull=True).exists())
