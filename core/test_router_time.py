"""TapTap's clock decides the router's limit-uptime (core/router_time.py)."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from . import router_time as rt
from .durations import router_limit
from .models import AgentCommand, Business, Router, RouterHotspotUser, Voucher


class RouterTimeBase(TestCase):
    def setUp(self):
        cache.clear()
        u = User.objects.create_user('o@example.com', 'o@example.com', 'Pass-word-123')
        self.b = Business.objects.create(user=u, business_name='Alpha', owner_name='A', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=30), is_unlimited=True)
        self.api = Router.objects.create(business=self.b, name='API', ip_address='1.1.1.1', username='a', password='b')
        self.now = timezone.now()

    def v(self, code='FAM00001', devices=3, left_h=10, used_h=2, **kw):
        kw.setdefault('router', self.api)
        return Voucher.objects.create(business=self.b, code=code, plan_name='Family', price=Decimal('100'), duration_minutes=720,
                                      max_devices=devices, used_at=self.now - timedelta(hours=used_h),
                                      expires_at=self.now + timedelta(hours=left_h) if left_h is not None else None, **kw)


class RuleTests(RouterTimeBase):
    def test_three_devices_get_three_times_the_time_left(self):
        v = self.v()
        # router counted 5h already (3 phones for ~2h), 10h left in TapTap
        s = rt.limit_seconds(v, uptime=5 * 3600, now=self.now)
        self.assertEqual(s, 5 * 3600 + 10 * 3600 * 3)

    def test_old_span_limit_is_drift_for_multi_device(self):
        v = self.v()
        span = '12h'                              # old rule: expires − first login
        self.assertFalse(rt.acceptable(v, span, '5h', now=self.now))
        self.assertTrue(rt.acceptable(v, rt.limit_for(v, uptime=5 * 3600, now=self.now), '5h', now=self.now))

    def test_used_up_on_router_while_taptap_has_time_is_drift(self):
        v = self.v(devices=1)
        self.assertFalse(rt.acceptable(v, '12h', '12h', now=self.now))

    def test_unlimited_and_no_limit(self):
        v = Voucher.objects.create(business=self.b, router=self.api, code='UNL00001', plan_name='Free', duration_minutes=0)
        self.assertEqual(rt.limit_for(v), '0s')
        self.assertTrue(rt.acceptable(v, '0s', '9h'))
        self.assertFalse(rt.acceptable(v, '1d', '9h'))
        finite = self.v(code='FIN00001')
        self.assertFalse(rt.acceptable(finite, '0s', '1h', now=self.now))     # "never expires" on the router is drift

    def test_expired_keeps_the_router_closed(self):
        v = self.v(left_h=-1)
        self.assertEqual(rt.limit_seconds(v, uptime=7200, now=self.now), 7200)
        self.assertTrue(rt.acceptable(v, '1h', '2h', now=self.now))

    def test_unused_voucher_follows_plan_times_slots(self):
        v = Voucher.objects.create(business=self.b, router=self.api, code='NEW00001', plan_name='Family', duration_minutes=60, max_devices=2)
        self.assertEqual(rt.limit_seconds(v, uptime=0), 2 * 3600)

    def test_unknown_counter_assumes_the_worst(self):
        v = self.v(devices=3, used_h=2, left_h=10)
        # no live cache, no mirror: 2h elapsed × 3 devices assumed already counted
        self.assertEqual(rt.limit_seconds(v, now=self.now), 2 * 3600 * 3 + 10 * 3600 * 3)

    def test_one_device_voucher_keeps_todays_value(self):
        v = self.v(devices=1, used_h=2, left_h=10)
        self.assertEqual(rt.limit_for(v, now=self.now), '12h')     # whole period, as before: no mass re-send

    def test_router_made_vouchers_keep_the_old_rule(self):
        v = self.v(code='MIK00001', source='mikrotik', devices=3)
        self.assertEqual(router_limit(v), '12h')

    def test_router_limit_uses_the_rule_for_taptap_vouchers(self):
        v = self.v()
        rt.note_uptime(self.api.pk, v.code, '5h')
        self.assertEqual(router_limit(v), rt.limit_for(v, uptime=5 * 3600))

    def test_member_matches_with_uptime(self):
        from .member_time import expected_limit, router_matches
        m = self.v(code='anna', login_type='member', devices=2)
        lim = expected_limit(m, uptime=3600)
        self.assertTrue(router_matches(m, lim, '1h'))
        self.assertFalse(router_matches(m, '12h', '11h'))


class ApiRepairTests(RouterTimeBase):
    def svc(self, rows):
        res = mock.MagicMock()
        svc = mock.MagicMock()
        svc.resource.return_value = res
        return svc, res

    def test_repairs_drifted_taptap_voucher_only(self):
        v = self.v()
        rows = [{'id': '*1', 'name': v.code, 'limit-uptime': '12h', 'uptime': '11h'},
                {'id': '*2', 'name': 'WINBOX1', 'limit-uptime': '1h', 'uptime': '1h'}]
        svc, res = self.svc(rows)
        self.assertEqual(rt.repair_api(self.api, svc, rows, self.now), 1)
        kw = res.set.call_args.kwargs
        self.assertEqual(kw['id'], '*1')
        self.assertGreaterEqual(rt.secs(kw['limit_uptime']), 11 * 3600 + 10 * 3600 * 3)
        from .models import VoucherEvent
        self.assertTrue(VoucherEvent.objects.filter(voucher=v, router_result__contains='12h →').exists())

    def test_repeat_guard(self):
        v = self.v()
        rows = [{'id': '*1', 'name': v.code, 'limit-uptime': '12h', 'uptime': '11h'}]
        svc, res = self.svc(rows)
        rt.repair_api(self.api, svc, rows, self.now)
        rt.repair_api(self.api, svc, rows, self.now)
        self.assertEqual(res.set.call_count, 1)

    def test_expiry_no_longer_expires_a_taptap_voucher_with_time_left(self):
        from .expiry import enforce_on_router
        v = self.v(devices=3)
        rows = [{'id': '*1', 'name': v.code, 'limit-uptime': '12h', 'uptime': '12h', 'disabled': 'false'}]
        svc, res = self.svc(rows)
        res.get.return_value = rows
        enforce_on_router(self.api, svc, rows, [], self.now)
        v.refresh_from_db()
        self.assertEqual(v.status, 'active')
        self.assertTrue(res.set.called)                      # limit raised instead

    def test_expiry_still_expires_router_made_vouchers(self):
        from .expiry import enforce_on_router
        v = Voucher.objects.create(business=self.b, router=self.api, code='MIK00002', plan_name='1h', duration_minutes=60,
                                   source='mikrotik', used_at=self.now - timedelta(minutes=30))
        rows = [{'id': '*3', 'name': v.code, 'limit-uptime': '1h', 'uptime': '1h', 'disabled': 'false'}]
        svc, res = self.svc(rows)
        res.get.return_value = rows
        enforce_on_router(self.api, svc, rows, [], self.now)
        v.refresh_from_db()
        self.assertEqual(v.status, 'expired')


class LinkRepairTests(RouterTimeBase):
    def test_queues_one_compact_command(self):
        link = Router.objects.create(business=self.b, name='Link', ip_address='', username='', password='', connection_mode='agent')
        a = self.v(code='FAM00002', router=link)
        ok = self.v(code='FAM00003', router=link)
        RouterHotspotUser.objects.create(business=self.b, router=link, username=a.code, limit_uptime='12h', uptime='12h')
        good = rt.limit_for(ok, uptime=3600, now=self.now)
        RouterHotspotUser.objects.create(business=self.b, router=link, username=ok.code, limit_uptime=good, uptime='1h')
        with mock.patch('core.agent.queue') as q:
            n = rt.repair_link(link, self.now)
        self.assertEqual(n, 1)
        params = q.call_args.args[2]
        self.assertEqual([u['n'] for u in params['users']], ['FAM00002'])
        self.assertEqual(q.call_args.args[1], 'hotspot_users_limit')
