"""Deleting a switched-off Member Plan moves it to the bin; its members move to another plan first."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from core.member_plan_bin import MemberPlanBinError, delete_member_plan
from core.models import Business, Voucher
from core.models_member_plans import MemberPlan, MemberPlanAssignment
from core.profile_groups import classify


class MemberPlanBinTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user('owner', 'o@x.gm', 'pw')
        self.biz = Business.objects.create(user=self.u, business_name='Kunda Net', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.old = MemberPlan.objects.create(business=self.biz, name='Old Family', price=300, duration_minutes=43200,
                                             max_devices=3, active=False)
        self.new = MemberPlan.objects.create(business=self.biz, name='Family Monthly', price=500, duration_minutes=43200,
                                             max_devices=5)
        self.client.force_login(self.u)

    def member(self, code, plan, **kw):
        m = Voucher.objects.create(business=self.biz, code=code, password='x', login_type='member', plan_name=plan.name,
                                   router_profile=plan.router_profile_name, source='taptap', **kw)
        MemberPlanAssignment.objects.create(member=m, plan=plan)
        return m

    def test_empty_inactive_plan_goes_to_the_bin(self):
        delete_member_plan(self.old, user=self.u, reason='Replaced')
        self.assertFalse(self.biz.member_plans.filter(pk=self.old.pk).exists())          # hidden everywhere
        binned = MemberPlan.all_objects.get(pk=self.old.pk)                               # but kept
        self.assertEqual(binned.delete_reason, 'Replaced')
        self.assertEqual(binned.deleted_by, self.u)
        self.assertEqual(binned.delete_info['devices'], 3)
        # its router profile is now "plan deleted" — nobody can pick it
        self.assertEqual(classify(self.biz, [binned.router_profile_name])[binned.router_profile_name][0], 'deleted')

    def test_active_plan_cannot_be_deleted(self):
        with self.assertRaises(MemberPlanBinError) as e:
            delete_member_plan(self.new, user=self.u, reason='x')
        self.assertIn('inactive first', str(e.exception))

    def test_reason_required(self):
        with self.assertRaises(MemberPlanBinError):
            delete_member_plan(self.old, user=self.u, reason='  ')

    def test_members_must_move_first(self):
        self.member('awa', self.old)
        with self.assertRaises(MemberPlanBinError) as e:
            delete_member_plan(self.old, user=self.u, reason='x')
        self.assertIn('1 member', str(e.exception))
        self.assertIsNone(MemberPlan.all_objects.get(pk=self.old.pk).deleted_at)

    def test_members_move_and_plan_is_binned(self):
        a = self.member('awa', self.old)
        b = self.member('musa', self.old)
        r = delete_member_plan(self.old, user=self.u, reason='Merged', move_to=self.new)
        self.assertEqual(sorted(r['moved']), ['awa', 'musa'])
        for m in (a, b):
            m.refresh_from_db()
            self.assertEqual(m.member_plan_assignment.plan, self.new)
            self.assertEqual(m.plan_name, 'Family Monthly')
            self.assertEqual(m.max_devices, 5)
        info = MemberPlan.all_objects.get(pk=self.old.pk).delete_info
        self.assertEqual((info['members_moved'], info['moved_to']), (2, 'Family Monthly'))

    def test_cannot_move_to_inactive_plan(self):
        self.member('awa', self.old)
        off = MemberPlan.objects.create(business=self.biz, name='Other off', price=100, active=False)
        with self.assertRaises(MemberPlanBinError):
            delete_member_plan(self.old, user=self.u, reason='x', move_to=off)

    def test_members_in_the_bin_do_not_block(self):
        gone = self.member('gone', self.old)
        Voucher.all_objects.filter(pk=gone.pk).update(deleted_at=timezone.now())
        delete_member_plan(self.old, user=self.u, reason='Old')
        self.assertIsNotNone(MemberPlan.all_objects.get(pk=self.old.pk).deleted_at)

    def test_name_can_be_used_again(self):
        delete_member_plan(self.old, user=self.u, reason='Old')
        MemberPlan.objects.create(business=self.biz, name='Old Family', price=350)
        self.assertEqual(self.biz.member_plans.filter(name='Old Family').count(), 1)

    def test_already_binned(self):
        delete_member_plan(self.old, user=self.u, reason='Old')
        with self.assertRaises(MemberPlanBinError):
            delete_member_plan(MemberPlan.all_objects.get(pk=self.old.pk), user=self.u, reason='again')

    def test_page_delete_and_bin_tab(self):
        self.member('awa', self.old)
        html = self.client.get('/members/').content.decode()
        self.assertIn('id="deletePlanModal"', html)
        self.assertIn(f'data-plan-id="{self.old.pk}" data-plan-name="Old Family" data-members="1"', html)
        r = self.client.post('/members/', {'action': 'delete_plan', 'plan_id': self.old.pk, 'reason': 'Merged',
                                           'move_to': self.new.pk})
        self.assertEqual(r.status_code, 302)
        self.assertIsNotNone(MemberPlan.all_objects.get(pk=self.old.pk).deleted_at)
        html = self.client.get('/bin/?tab=member_plans').content.decode()
        self.assertIn('Old Family', html)
        self.assertIn('moved to <b class="text-dark">Family Monthly</b>', html)

    def test_page_refuses_active_plan(self):
        self.client.post('/members/', {'action': 'delete_plan', 'plan_id': self.new.pk, 'reason': 'x'})
        self.assertIsNone(MemberPlan.all_objects.get(pk=self.new.pk).deleted_at)
