"""Locked Member Plans: the owner freezes a plan; nobody can use it, change it or unlock it (sealed: not even the
owner) — and the members already on it keep it exactly as it is."""
from datetime import timedelta
from types import SimpleNamespace

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core import members as mem
from core import plan_lock
from core.fit_actions import FitActionError
from core.member_plan_bin import MemberPlanBinError, can_delete, delete_member_plan, move_targets
from core.models import Business, Voucher
from core.models_member_plans import MemberPlan, MemberPlanAssignment
from core.models_team import TeamMember


def owner_request(user, **kw):
    return SimpleNamespace(user=user, tt_role=kw.get('role', 'owner'), tt_view_as=kw.get('view_as'))


class PlanLockTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user('owner', 'o@x.gm', 'pw-123456', first_name='Awa')
        self.biz = Business.objects.create(user=self.u, business_name='Kunda Net', owner_name='Awa', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.gold = MemberPlan.objects.create(business=self.biz, name='Gold 2024', price=300, duration_minutes=43200,
                                              max_devices=3, speed_limit='2M/2M')
        self.family = MemberPlan.objects.create(business=self.biz, name='Family Monthly', price=500,
                                                duration_minutes=43200, max_devices=5)
        self.awa = self.member('awa.k', self.gold)
        self.staff = User.objects.create_user('staff', 's@x.gm', 'pw-123456', first_name='Musa')
        TeamMember.objects.create(business=self.biz, user=self.staff, is_active=True, role='admin')

    def member(self, code, plan):
        m = Voucher.objects.create(business=self.biz, code=code, password='x', login_type='member', plan_name=plan.name,
                                   router_profile=plan.router_profile_name, source='taptap', price=plan.price,
                                   max_devices=plan.max_devices, rate_limit=plan.speed_limit)
        MemberPlanAssignment.objects.create(member=m, plan=plan)
        return m

    def lock(self, kind='locked', **kw):
        plan_lock.lock(self.gold, owner_request(self.u), kind=kind, **kw)
        self.gold.refresh_from_db()

    # ── who can lock and unlock ──
    def test_owner_locks_from_the_members_page(self):
        self.client.force_login(self.u)
        r = self.client.post(reverse('members'), {'action': 'lock_plan', 'plan_id': self.gold.pk, 'kind': 'locked',
                                                  'reason': 'Old price, kept for early members'}, follow=True)
        self.gold.refresh_from_db()
        self.assertEqual(self.gold.lock_kind, 'locked')
        self.assertEqual(self.gold.locked_by, self.u)
        self.assertEqual(self.gold.lock_reason, 'Old price, kept for early members')
        self.assertContains(r, 'keep it exactly as it is')

    def test_staff_cannot_lock_or_unlock(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('members'), {'action': 'lock_plan', 'plan_id': self.family.pk, 'kind': 'locked'})
        self.family.refresh_from_db()
        self.assertEqual(self.family.lock_kind, '')
        self.lock()
        r = self.client.post(reverse('members'), {'action': 'unlock_plan', 'plan_id': self.gold.pk}, follow=True)
        self.gold.refresh_from_db()
        self.assertEqual(self.gold.lock_kind, 'locked')
        self.assertContains(r, 'Only the business owner can unlock')

    def test_support_viewing_as_the_owner_cannot_unlock(self):
        self.lock()
        with self.assertRaises(plan_lock.PlanLocked):
            plan_lock.unlock(self.gold, owner_request(self.u, view_as=self.biz))
        with self.assertRaises(plan_lock.PlanLocked):
            plan_lock.unlock(self.gold, owner_request(self.staff))
        self.assertEqual(MemberPlan.objects.get(pk=self.gold.pk).lock_kind, 'locked')

    def test_owner_unlocks(self):
        self.lock()
        self.client.force_login(self.u)
        self.client.post(reverse('members'), {'action': 'unlock_plan', 'plan_id': self.gold.pk})
        self.gold.refresh_from_db()
        self.assertEqual(self.gold.lock_kind, '')
        self.assertIsNone(self.gold.locked_by)
        mem.assign_member_plan(self.member('new.one', self.family), self.gold)        # usable again

    def test_seal_needs_the_plan_name_and_can_never_be_undone(self):
        with self.assertRaises(plan_lock.PlanLocked):
            self.lock('sealed', confirm='gold')
        self.assertEqual(MemberPlan.objects.get(pk=self.gold.pk).lock_kind, '')
        self.lock('sealed', confirm='  gold 2024 ')
        self.assertEqual(self.gold.lock_kind, 'sealed')
        with self.assertRaises(plan_lock.PlanLocked) as e:
            plan_lock.unlock(self.gold, owner_request(self.u))
        self.assertIn('not even the owner', str(e.exception))
        with self.assertRaises(plan_lock.PlanLocked):                                  # cannot be "downgraded" to locked
            plan_lock.lock(self.gold, owner_request(self.u), kind='locked')
        self.client.force_login(self.u)
        self.client.post(reverse('members'), {'action': 'unlock_plan', 'plan_id': self.gold.pk})
        self.assertEqual(MemberPlan.objects.get(pk=self.gold.pk).lock_kind, 'sealed')

    # ── nobody uses it ──
    def test_nobody_is_put_on_a_locked_plan(self):
        self.lock()
        musa = self.member('musa.j', self.family)
        with self.assertRaises(mem.MemberError):
            mem.assign_member_plan(musa, self.gold, user=self.u)
        with self.assertRaises(mem.MemberError):
            mem.plan_choice(self.biz, str(self.gold.pk))
        with self.assertRaises(mem.MemberError):
            mem.create_member(self.biz, username='fatou.s', password='abc12', plan_value=str(self.gold.pk), user=self.u)
        self.assertFalse(Voucher.objects.filter(code='fatou.s').exists())
        self.assertEqual(musa.member_plan_assignment.plan, self.family)

    def test_members_on_it_cannot_be_moved_off(self):
        self.lock()
        with self.assertRaises(mem.MemberError) as e:
            mem.assign_member_plan(self.awa, self.family, user=self.u)
        self.assertIn('cannot be moved', str(e.exception))
        self.assertEqual(MemberPlanAssignment.objects.get(member=self.awa).plan, self.gold)

    def test_members_keep_it_exactly_as_it_is(self):
        self.lock()
        before = Voucher.objects.get(pk=self.awa.pk)
        mem.assign_member_plan(self.awa, self.gold, user=self.u)                       # re-confirming is fine
        a = MemberPlanAssignment.objects.get(member=self.awa)
        a.save()                                                                       # an ordinary save is fine
        after = Voucher.objects.get(pk=self.awa.pk)
        for f in ('plan_name', 'price', 'max_devices', 'rate_limit', 'router_profile', 'status'):
            self.assertEqual(getattr(after, f), getattr(before, f), f)
        self.assertEqual(a.plan, self.gold)

    def test_renewal_still_works_on_a_locked_plan(self):
        self.lock('sealed', confirm='Gold 2024')
        ok, result, sale, minutes = mem.renew(self.awa, user=self.u)
        self.assertTrue(ok, result)
        self.assertEqual(MemberPlanAssignment.objects.get(member=self.awa).plan, self.gold)

    def test_the_model_itself_refuses(self):
        """Any path that writes an assignment directly is stopped too."""
        self.lock()
        musa = Voucher.objects.create(business=self.biz, code='musa.j', password='x', login_type='member', source='taptap')
        with self.assertRaises(plan_lock.PlanLocked):
            MemberPlanAssignment.objects.create(member=musa, plan=self.gold)
        a = MemberPlanAssignment.objects.get(member=self.awa)
        a.plan = self.family
        with self.assertRaises(plan_lock.PlanLocked):
            a.save()
        self.assertEqual(MemberPlanAssignment.objects.get(member=self.awa).plan, self.gold)

    # ── nobody changes it ──
    def test_cannot_be_edited_switched_off_or_deleted(self):
        self.lock()
        with self.assertRaises(mem.MemberError):
            mem.save_member_plan(self.biz, {'name': 'Gold 2024', 'price': '1', 'max_devices': 3, 'speed_limit': '',
                                            'duration_unit': 'months', 'duration_value': 1}, plan=self.gold, user=self.u)
        with self.assertRaises(mem.MemberError):
            mem.set_plan_active(self.gold, False)
        self.gold.refresh_from_db()
        self.assertEqual(self.gold.price, 300)
        self.assertTrue(self.gold.active)
        ok, why = can_delete(self.gold)
        self.assertFalse(ok)

    def test_locked_plan_is_not_a_bin_target(self):
        self.lock()
        old = MemberPlan.objects.create(business=self.biz, name='Old', price=1, duration_minutes=60, active=False)
        self.member('x.y', old)
        self.assertNotIn(self.gold, list(move_targets(old)))
        with self.assertRaises(MemberPlanBinError):
            delete_member_plan(old, user=self.u, reason='merge', move_to=self.gold)

    def test_page_toggle_on_a_locked_plan_is_a_message_not_an_error(self):
        self.lock()
        self.client.force_login(self.u)
        r = self.client.post(reverse('members'), {'action': 'toggle_plan', 'plan_id': self.gold.pk}, follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'cannot be switched on or off')
        self.gold.refresh_from_db()
        self.assertTrue(self.gold.active)

    def test_member_page_change_plan_refused(self):
        self.lock()
        self.client.force_login(self.u)
        r = self.client.post(reverse('member_support', args=[self.awa.pk]), {'action': 'plan', 'plan': self.family.pk}, follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(MemberPlanAssignment.objects.get(member=self.awa).plan, self.gold)

    # ── pages ──
    def test_members_page_shows_the_lock(self):
        self.lock(reason='Early members only')
        self.client.force_login(self.u)
        r = self.client.get(reverse('members'))
        self.assertContains(r, 'plan-seal')
        self.assertContains(r, 'Early members only')
        self.assertContains(r, 'id="lockPlanModal"')
        self.assertContains(r, 'Locked by the owner — kept by their members')
        self.assertNotContains(r, f'<option value="{self.gold.pk}"')                  # not offered for a new member
        self.client.force_login(self.staff)
        r = self.client.get(reverse('members'))
        self.assertContains(r, 'plan-seal')
        self.assertNotContains(r, 'id="lockPlanModal"')                                # staff see it, cannot lock

    def test_member_detail_says_why_the_plan_cannot_change(self):
        self.lock()
        self.client.force_login(self.u)
        r = self.client.get(reverse('member_detail', args=[self.awa.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'stays on it exactly as it is')
        self.assertNotContains(r, 'Change plan</button>')

    # ── Fit ──
    def test_fit_refuses_before_asking_to_confirm(self):
        from core import fit_members as fm
        self.lock()
        with self.assertRaises(FitActionError):
            fm._check_toggle_plan(self.biz, {'plan': 'Gold 2024', 'active': False})
        with self.assertRaises(FitActionError):
            fm._check_change_plan(self.biz, {'code': 'awa.k', 'plan': 'Family Monthly'})
        musa = self.member('musa.j', self.family)
        with self.assertRaises(FitActionError):
            fm._check_change_plan(self.biz, {'code': musa.code, 'plan': 'Gold 2024'})
        with self.assertRaises(FitActionError):
            fm._check_save_plan(self.biz, {'plan': 'Gold 2024', 'price': '1'})
