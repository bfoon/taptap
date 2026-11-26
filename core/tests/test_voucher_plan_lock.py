"""Owner locks a voucher plan: no NEW vouchers anywhere; everything already made keeps working."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core import voucher_plan_lock as vpl
from core.models import Agent, Business, Router, TeamMember, Voucher, VoucherPlan
from core.utils import code_format_from_post
from core.voucher_make import make_batch


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.now = timezone.now()
        self.owner = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456')
        self.b = Business.objects.create(user=self.owner, business_name='Kerr Wi-Fi', owner_name='O', phone='7000001',
                                         trial_ends_at=self.now + timedelta(days=30), feature_tier='pro')
        self.router = Router.objects.create(business=self.b, name='Hap', ip_address='10.9.8.7', username='u', password='p')
        self.day = VoucherPlan.objects.create(business=self.b, name='Day', price=50, duration_minutes=1440)
        self.week = VoucherPlan.objects.create(business=self.b, name='Week', price=250, duration_minutes=10080)
        self.admin = User.objects.create_user('a@x.gm', 'a@x.gm', 'pw-123456')
        TeamMember.objects.create(business=self.b, user=self.admin, role='admin')
        self.fmt = code_format_from_post({}, self.b)

    def req(self, user):
        r = mock.Mock(); r.user = user; r.tt_role = 'owner' if user == self.owner else 'admin'; r.tt_view_as = None
        return r

    def lock(self, plan=None, **kw):
        return vpl.lock(plan or self.day, self.req(self.owner), **kw)


class Rules(Base):
    @mock.patch('core.portal_deploy.schedule_redeploy')
    def test_only_owner_locks_and_nothing_new_is_made(self, _r):
        with self.assertRaises(vpl.PlanClosedError):
            vpl.lock(self.day, self.req(self.admin))
        batch, _ = make_batch(self.b, self.day, 3, self.fmt, router=self.router)       # made before the lock
        self.lock(reason='Price changing', replacement=self.week)
        self.day.refresh_from_db()
        self.assertTrue(vpl.is_closed(self.day))
        with self.assertRaises(vpl.PlanClosedError) as e:
            make_batch(self.b, self.day, 2, self.fmt, router=self.router)
        self.assertIn('Use “Week” instead', str(e.exception))
        self.assertEqual(Voucher.objects.filter(plan_name='Day').count(), 3)                # the old ones are untouched
        self.assertEqual(set(Voucher.objects.filter(plan_name='Day').values_list('status', flat=True)), {'active'})
        self.assertEqual(vpl.sunset(self.day)['stock'], 3)
        self.assertEqual(list(vpl.open_plans(self.b.plans.all())), [self.week])
        make_batch(self.b, self.week, 1, self.fmt, router=self.router)                      # other plans carry on

    @mock.patch('core.portal_deploy.schedule_redeploy')
    def test_reopen_date_and_unlock(self, _r):
        with self.assertRaises(vpl.PlanClosedError):
            self.lock(until=self.now - timedelta(hours=1))
        self.lock(until=self.now + timedelta(days=2))
        self.assertTrue(vpl.is_closed(self.day))
        self.assertFalse(vpl.is_closed(self.day, now=self.now + timedelta(days=3)))       # reopens by itself
        self.assertEqual(vpl.reopen_due(self.b, now=self.now + timedelta(days=3)), 1)
        self.day.refresh_from_db()
        self.assertEqual(self.day.sale_lock, '')
        self.lock()
        with self.assertRaises(vpl.PlanClosedError):
            vpl.unlock(self.day, self.req(self.admin))
        vpl.unlock(self.day, self.req(self.owner))
        self.assertFalse(vpl.is_closed(self.day))

    @mock.patch('core.portal_deploy.schedule_redeploy')
    def test_seal_needs_name_and_freezes_the_plan(self, _r):
        with self.assertRaises(vpl.PlanClosedError):
            self.lock(kind='sealed', confirm='nope')
        self.lock(kind='sealed', confirm='day')
        with self.assertRaises(vpl.PlanClosedError):
            vpl.unlock(self.day, self.req(self.owner))
        with self.assertRaises(vpl.PlanClosedError):
            vpl.check_edit(self.day)
        from core.voucher_bin import can_delete_plan
        ok, why = can_delete_plan(self.day, {'plans.manage', 'plans.delete_used'})
        self.assertFalse(ok); self.assertIn('sealed', why)

    @mock.patch('core.portal_deploy.schedule_redeploy')
    def test_moving_vouchers_onto_a_locked_plan_is_refused(self, _r):
        make_batch(self.b, self.week, 2, self.fmt, router=self.router)
        self.lock()
        from core.plan_move import move_vouchers
        with self.assertRaises(ValueError):
            move_vouchers(self.week, self.day)
        self.assertEqual(Voucher.objects.filter(plan_name='Week').count(), 2)

    @mock.patch('core.portal_deploy.schedule_redeploy')
    def test_agents_cannot_order_a_locked_plan(self, _r):
        a = Agent.objects.create(business=self.b, name='Awa')
        self.assertEqual(set(a.orderable_plans()), {self.day, self.week})
        self.lock()
        self.assertEqual(list(a.orderable_plans()), [self.week])
        a.order_plans.set([self.day])                    # the only plan ticked for this agent is locked
        self.assertEqual(list(a.orderable_plans()), [])

    @mock.patch('core.portal_deploy.schedule_redeploy')
    def test_bonanza_does_not_draw_a_locked_voucher_prize(self, _r):
        from core import bonanza
        from core.models import Bonanza, BonanzaPrize
        bz = Bonanza.objects.create(business=self.b, name='Spin', slug='spin-x')
        p1 = BonanzaPrize.objects.create(bonanza=bz, label='Free day', kind='voucher', plan=self.day, weight=100)
        p2 = BonanzaPrize.objects.create(bonanza=bz, label='Nothing', kind='none', weight=1)
        self.lock()
        for _ in range(30):
            self.assertEqual(bonanza.pick([BonanzaPrize.objects.get(pk=p1.pk), p2]), p2)


class Pages(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    @mock.patch('core.portal_deploy.schedule_redeploy')
    def test_owner_locks_from_plans_page_and_generate_refuses(self, _r):
        r = self.client.get(reverse('plans'))
        self.assertContains(r, 'id="planLock"')
        r = self.client.post(reverse('plan_sale_lock', args=[self.day.pk]),
                             {'kind': 'locked', 'reason': 'Holiday', 'replacement': str(self.week.pk)}, follow=True)
        self.assertContains(r, 'is locked')
        self.assertContains(r, 'Locked plans — winding down')
        self.day.refresh_from_db()
        self.assertEqual((self.day.sale_lock, self.day.sale_replacement), ('locked', self.week))
        r = self.client.get(reverse('generate_vouchers'))
        self.assertContains(r, 'locked by the owner')
        self.assertNotContains(r, 'Day — ')
        n = Voucher.objects.count()
        r = self.client.post(reverse('generate_vouchers'), {'plan': self.day.pk, 'quantity': 2, 'router': self.router.pk})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Voucher.objects.count(), n)
        r = self.client.post(reverse('plan_sale_lock', args=[self.day.pk]), {'action': 'unlock'}, follow=True)
        self.assertContains(r, 'open again')

    @mock.patch('core.portal_deploy.schedule_redeploy')
    def test_staff_cannot_lock(self, _r):
        self.client.force_login(self.admin)
        r = self.client.get(reverse('plans'))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'id="planLock"')
        r = self.client.post(reverse('plan_sale_lock', args=[self.day.pk]), {'kind': 'locked'}, follow=True)
        self.assertContains(r, 'Only the business owner')
        self.day.refresh_from_db()
        self.assertEqual(self.day.sale_lock, '')

    @mock.patch('core.portal_deploy.schedule_redeploy')
    def test_sealed_plan_cannot_be_edited(self, _r):
        self.lock(kind='sealed', confirm='Day')
        r = self.client.post(reverse('plan_update', args=[self.day.pk]), {'price': '999', 'active': '1'}, follow=True)
        self.assertContains(r, 'is sealed')
        self.day.refresh_from_db()
        self.assertEqual(int(self.day.price), 50)

    @mock.patch('core.portal_deploy.schedule_redeploy')
    def test_single_voucher_page_refuses(self, _r):
        self.lock()
        r = self.client.get(reverse('single_voucher'))
        self.assertContains(r, 'data-locked-plans')
        n = Voucher.objects.count()
        self.client.post(reverse('single_voucher'), {'mode': 'plan', 'plan': self.day.pk, 'code_mode': 'random'})
        self.assertEqual(Voucher.objects.count(), n)


class Doors(Base):
    """Every other place a new voucher could come from leaves the locked plan out."""
    @mock.patch('core.portal_deploy.schedule_redeploy')
    def test_portal_price_list_and_online_shop(self, _r):
        from core.models_collaboration_buy import OnlineStorefront
        from core.online_buy import _store_plans
        from core.views_studio import plans_ctx
        store = OnlineStorefront.objects.create(business=self.b)
        self.lock()
        self.assertEqual([p['name'] for p in plans_ctx(self.b)], ['Week'])
        self.assertEqual(list(_store_plans(store)), [self.week])

    @mock.patch('core.portal_deploy.schedule_redeploy')
    def test_market_listing_refused(self, _r):
        from core import market
        self.lock()
        with self.assertRaises(market.MarketError) as e:
            market.create_listing(self.b, self.owner, {'router': str(self.router.pk), 'plan': str(self.day.pk), 'quantity': '5',
                                                       'price': '100'})
        self.assertIn('locked by the owner', str(e.exception))


@mock.patch('core.portal_deploy.schedule_redeploy')
class Mobile(Base):
    def test_app_lists_open_plans_and_refuses_locked(self, _r):
        import json
        from django.test import override_settings
        with override_settings(AUTH_EMAIL_OTP=False):
            r = self.client.post('/api/mobile/v1/auth/login/', json.dumps({'email': 'o@x.gm', 'password': 'pw-123456', 'device': 'Pixel'}),
                                 content_type='application/json')
            tok = json.loads(r.content)['token']; self.client.cookies.clear()
            self.lock()
            h = {'HTTP_AUTHORIZATION': f'Bearer {tok}'}
            d = json.loads(self.client.get('/api/mobile/v1/vouchers/new/', **h).content)
            self.assertEqual([p['name'] for p in d['plans']], ['Week'])
            r = self.client.post('/api/mobile/v1/vouchers/new/', json.dumps({'kind': 'batch', 'plan': self.day.pk, 'quantity': 3}),
                                 content_type='application/json', **h)
            self.assertEqual(r.status_code, 400)
            self.assertIn('locked by the owner', json.loads(r.content)['message'])
            self.assertEqual(Voucher.objects.count(), 0)
