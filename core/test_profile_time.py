from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.utils import timezone

from . import profile_time as pt
from .models import Business, Router, RouterHotspotProfile, RouterHotspotUser, Voucher, VoucherPlan


class NameTests(TestCase):
    def test_names(self):
        for name, minutes in [('24H', 1440), ('1-Day', 1440), ('30DAYS', 43200), ('Weekly', 10080), ('Monthly', 43200), ('3hrs', 180),
                              ('30 min', 30), ('2 weeks', 20160), ('daily-pass', 1440), ('VIP', 0), ('unlimited', 0), ('default', 0), ('Kairaba', 0)]:
            self.assertEqual(pt.name_minutes(name), minutes, name)

    def test_profile_sources_in_order(self):
        self.assertEqual(pt.profile_minutes({'name': '24h', 'session-timeout': '2h'}), (120, 'session-timeout'))
        self.assertEqual(pt.profile_minutes({'name': 'x', 'on-login': ':put (",rem,0,30d,0,,Disable,");'}), (43200, 'Mikhmon validity'))
        self.assertEqual(pt.profile_minutes({'name': 'x', 'comment': 'validity: 7d'}), (10080, 'comment'))
        self.assertEqual(pt.profile_minutes({'name': 'MONTHLY 400'}), (43200, 'name'))


class Base(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('o', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='K', owner_name='A', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=self.b, name='R', ip_address='1.1.1.1', username='a', password='b')
        RouterHotspotProfile.objects.create(business=self.b, router=self.r, name='30DAYS', shared_users=1, raw_data={})
        self.c = Client(); self.c.force_login(self.owner)


class AuditApplyTests(Base):
    def test_unlimited_plan_and_vouchers_follow_their_profile(self):
        plan = VoucherPlan.objects.create(business=self.b, name='30DAYS', price=400, duration_minutes=0, duration_unit='unlimited', source='mikrotik', mikrotik_profile_name='30DAYS')
        used = Voucher.objects.create(business=self.b, router=self.r, code='MKH00001', plan_name='30DAYS', duration_minutes=0, source='mikrotik',
                                      used_at=timezone.now() - timedelta(days=3))
        fresh = Voucher.objects.create(business=self.b, router=self.r, code='TT000001', plan_name='30DAYS', duration_minutes=0, source='taptap')
        a = pt.audit(self.b)
        self.assertEqual([x['minutes'] for x in a['plans']], [43200])
        with mock.patch('core.voucher_push.push_vouchers', return_value={}) as push:
            r = pt.apply(self.b, self.owner)
        plan.refresh_from_db(); used.refresh_from_db(); fresh.refresh_from_db()
        self.assertEqual((plan.duration_minutes, used.duration_minutes, fresh.duration_minutes), (43200, 43200, 43200))
        self.assertIsNotNone(used.used_at)                                         # start time kept
        self.assertEqual([v.code for v in push.call_args.args[0]], ['TT000001'])   # TapTap's voucher re-sent to the router
        self.assertEqual(r['vouchers'], 2)

    def test_drift_is_put_back_after_sync(self):
        VoucherPlan.objects.create(business=self.b, name='1 Day', price=25, duration_minutes=1440, mikrotik_profile_name='daily')
        v = Voucher.objects.create(business=self.b, router=self.r, code='TT000002', plan_name='1 Day', duration_minutes=1440, source='taptap')
        RouterHotspotUser.objects.create(business=self.b, router=self.r, username='TT000002', profile='30DAYS', is_present=True)   # changed in WinBox
        with mock.patch('core.voucher_push.push_vouchers') as push:
            self.assertEqual(pt.reconcile_router(self.r), 1)
        self.assertEqual(push.call_args.args[0], [v])

    def test_deleted_profile_shown_on_voucher_not_in_profiles(self):
        v = Voucher.objects.create(business=self.b, router=self.r, code='OLD00001', plan_name='Gone1h', duration_minutes=60, source='mikrotik',
                                   used_at=timezone.now())
        RouterHotspotUser.objects.create(business=self.b, router=self.r, username='OLD00001', profile='Gone1h', is_present=True)
        self.assertEqual(pt.profile_state(v), ('deleted', 'Gone1h'))
        self.assertContains(self.c.get(f'/vouchers/{v.pk}/'), 'deleted on the router')
        page = self.c.get('/routers/profiles/')
        self.assertContains(page, 'Profile check'); self.assertNotContains(page, '<span class="pf-name">Gone1h</span>')

    def test_fix_all_button(self):
        VoucherPlan.objects.create(business=self.b, name='30DAYS', price=400, duration_minutes=0, duration_unit='unlimited', source='mikrotik', mikrotik_profile_name='30DAYS')
        with mock.patch('core.voucher_push.push_vouchers', return_value={}):
            self.c.post('/routers/profiles/check/')
        self.assertEqual(VoucherPlan.objects.get(name='30DAYS').duration_minutes, 43200)


class ImportTests(Base):
    def test_import_reads_length_from_profile_name(self):
        from .sync import _profile_to_plan
        plan = _profile_to_plan(self.r, {'name': 'WEEKLY-150', 'shared-users': '1'}, {'duplicate_plans_skipped': 0, 'pulled_plans': 0}, timezone.now())
        self.assertEqual(plan.duration_minutes, 10080)

    def test_unlimited_imported_plan_corrected_on_next_sync(self):
        from .sync import _profile_to_plan
        VoucherPlan.objects.create(business=self.b, name='24HRS', price=25, duration_minutes=0, duration_unit='unlimited', source='mikrotik',
                                   mikrotik_profile_name='24HRS', imported_from_router=self.r)
        plan = _profile_to_plan(self.r, {'name': '24HRS', 'shared-users': '1'}, {'duplicate_plans_skipped': 0, 'pulled_plans': 0}, timezone.now())
        plan.refresh_from_db(); self.assertEqual(plan.duration_minutes, 1440)


class DeviceCountTests(Base):
    def test_vouchers_get_their_plans_devices(self):
        RouterHotspotProfile.objects.create(business=self.b, router=self.r, name='FAMILY10', shared_users=10, raw_data={})
        VoucherPlan.objects.create(business=self.b, name='Family', price=500, duration_minutes=43200, max_devices=10, mikrotik_profile_name='FAMILY10')
        tt = Voucher.objects.create(business=self.b, router=self.r, code='FAM00001', plan_name='Family', duration_minutes=43200, max_devices=1, source='taptap')
        mk = Voucher.objects.create(business=self.b, router=self.r, code='MKF00001', plan_name='Old name', duration_minutes=43200, max_devices=1, source='mikrotik')
        RouterHotspotUser.objects.create(business=self.b, router=self.r, username='MKF00001', profile='FAMILY10', is_present=True)
        a = pt.audit(self.b)
        self.assertEqual(sorted((x['voucher'].code, x['devices']) for x in a['devices']), [('FAM00001', 10), ('MKF00001', 10)])
        with mock.patch('core.voucher_push.push_vouchers', return_value={}) as push:
            pt.apply(self.b, self.owner)
        tt.refresh_from_db(); mk.refresh_from_db()
        self.assertEqual((tt.max_devices, mk.max_devices), (10, 10))
        self.assertIn(tt, push.call_args.args[0])

    def test_import_without_plan_uses_profile_shared_users(self):
        RouterHotspotProfile.objects.create(business=self.b, router=self.r, name='SHARE5', shared_users=5, raw_data={})
        self.assertEqual(pt.profile_devices(self.r, 'SHARE5'), 5)
        self.assertEqual(pt.profile_devices(self.r, 'nope'), 1)

    def test_changing_plan_devices_updates_its_vouchers(self):
        plan = VoucherPlan.objects.create(business=self.b, name='Family', price=500, duration_minutes=43200, max_devices=1)
        v = Voucher.objects.create(business=self.b, router=self.r, code='FAM00002', plan_name='Family', duration_minutes=43200, max_devices=1, source='taptap',
                                   used_at=timezone.now())
        with mock.patch('core.voucher_push.push_vouchers', return_value={}):
            self.c.post(f'/plans/{plan.pk}/update/', {'price': '500', 'max_devices': '10', 'active': '1'})
        v.refresh_from_db(); self.assertEqual(v.max_devices, 10)
