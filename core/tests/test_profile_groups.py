"""Profile lists: deleted plans' profiles are left out, and voucher plans / member plans sit under their own headers."""
import re
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from core.models import Business, Router, RouterHotspotProfile, Voucher, VoucherPlan
from core.models_member_plans import MemberPlan
from core.profile_groups import voucher_picker


class ProfileGroupsTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user('owner', 'o@x.gm', 'pw')
        self.biz = Business.objects.create(user=self.u, business_name='Kunda Net', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.r = Router.objects.create(business=self.biz, name='TapTap K', ip_address='10.0.0.1', username='a', password='b')
        self.live = MemberPlan.objects.create(business=self.biz, name='Shop Monthly', price=500, duration_minutes=43200, max_devices=10)
        self.off = MemberPlan.objects.create(business=self.biz, name='Old Family', price=300, duration_minutes=43200, max_devices=5, active=False)
        VoucherPlan.objects.create(business=self.biz, name='1Day', price=25, mikrotik_profile_name='1Day')
        gone = VoucherPlan.objects.create(business=self.biz, name='Weekly', price=100)
        gone.deleted_at = timezone.now(); gone.save()
        names = [self.live.router_profile_name, self.off.router_profile_name, 'taptap-member-plan-999',      # 999: deleted
                 'taptap-unlimited-member-plan-998', '1Day', 'Weekly', 'taptap-unlimited-10-devices-2M-2M', 'default']
        for n in names:
            RouterHotspotProfile.objects.create(business=self.biz, router=self.r, name=n, is_present=True, shared_users=10,
                                                rate_limit='2M/2M', raw_data={'name': n}, last_seen_at=timezone.now())
        self.member = Voucher.objects.create(business=self.biz, router=self.r, code='sshopkeeper', password='x', login_type='member',
                                             plan_name='Shop Monthly', router_profile=self.live.router_profile_name, source='taptap')
        self.voucher = Voucher.objects.create(business=self.biz, router=self.r, code='ABC123', plan_name='1Day', source='taptap')
        self.client.force_login(self.u)

    def test_member_picker(self):
        p = voucher_picker(self.biz, self.member)
        self.assertEqual([mp.name for _, mp in p['member_plans']], ['Shop Monthly'])          # switched-off + deleted left out
        others = [r['name'] for r in p['others']]
        self.assertNotIn('taptap-member-plan-999', others); self.assertNotIn('taptap-unlimited-member-plan-998', others)
        self.assertNotIn('Weekly', others)                                                     # voucher plan in the Bin
        self.assertIn('taptap-unlimited-10-devices-2M-2M', others)
        self.assertEqual(p['voucher_plans'], [])                                               # members don't move to voucher plans
        self.assertEqual(p['hidden'], 4)

    def test_switched_off_plan_stays_visible_for_the_member_on_it(self):
        self.member.router_profile = self.off.router_profile_name; self.member.save(update_fields=['router_profile'])
        names = [mp.name for _, mp in voucher_picker(self.biz, self.member)['member_plans']]
        self.assertIn('Old Family', names)

    def test_voucher_picker_has_no_member_plans(self):
        p = voucher_picker(self.biz, self.voucher)
        self.assertEqual(p['member_plans'], [])
        self.assertEqual([x.name for x in p['voucher_plans']], ['1Day'])
        self.assertFalse(any('member-plan' in r['name'] for r in p['others']))

    def test_page_shows_bold_headers(self):
        html = self.client.get(f'/vouchers/{self.member.pk}/').content.decode()
        modal = re.search(r'id="profileModal".*?</form>', html, re.S).group(0)
        self.assertIn('label="MEMBER PLANS — members only"', modal)
        self.assertIn('label="OTHER PROFILES ON TAPTAP K"', modal)
        self.assertNotIn('taptap-member-plan-999', modal)
        self.assertIn('not listed', modal)
        html = self.client.get(f'/vouchers/{self.voucher.pk}/').content.decode()
        modal = re.search(r'id="profileModal".*?</form>', html, re.S).group(0)
        self.assertIn('label="VOUCHER PLANS', modal); self.assertNotIn('MEMBER PLANS', modal)

    def test_deleted_profile_refused_on_save(self):
        self.client.post(f'/vouchers/{self.voucher.pk}/profile/', {'profile': 'router:Weekly'})
        self.voucher.refresh_from_db()
        self.assertEqual(self.voucher.router_profile, '')

    def test_vouchers_filter_groups(self):
        html = self.client.get('/vouchers/').content.decode()
        sel = re.search(r'name="plan".*?</select>', html, re.S).group(0)
        self.assertLess(sel.index('VOUCHER PLANS'), sel.index('1Day'))
        self.assertLess(sel.index('MEMBER PLANS'), sel.index('Shop Monthly'))
        self.assertLess(sel.index('1Day'), sel.index('MEMBER PLANS'))

    def test_profiles_page_sections(self):
        html = self.client.get(f'/routers/profiles/?router={self.r.pk}').content.decode()
        heads = re.findall(r'class="pf-group pf-group-(\w+)"', html)
        self.assertEqual(heads, ['voucher', 'member', 'other', 'deleted'])
