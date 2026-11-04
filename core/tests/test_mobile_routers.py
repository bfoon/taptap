"""The mobile app's Routers screen: each MikroTik on its own card, actions, moving a router, Quick sell."""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import Business, Router, RouterAgent, Voucher, VoucherPlan, VoucherSale
from core.models_team import TeamMember

API = '/api/mobile/v1/'


@override_settings(AUTH_EMAIL_OTP=False,
                   CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'mobr'}})
class MobileRouters(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456', first_name='Awa')
        self.biz = Business.objects.create(user=self.owner, business_name='Kunda Net', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.plan = VoucherPlan.objects.create(business=self.biz, name='Day', price=25, duration_minutes=1440, active=True)
        self.a = Router.objects.create(business=self.biz, name='Serrekunda', connection_mode='agent', status='Online')
        self.b = Router.objects.create(business=self.biz, name='Brikama', connection_mode='agent', status='Offline')
        RouterAgent.objects.create(router=self.a, token_hash='e' * 64, enrolled_at=timezone.now(), last_seen_at=timezone.now())
        RouterAgent.objects.create(router=self.b, token_hash='f' * 64, enrolled_at=timezone.now())
        Voucher.objects.create(business=self.biz, router=self.a, code='INUSE001', plan_name='Day', used_at=timezone.now())
        v = Voucher.objects.create(business=self.biz, router=self.a, code='SOLD0001', plan_name='Day')
        VoucherSale.objects.create(business=self.biz, voucher=v, router=self.a, plan_name='Day', amount=25)
        cache.set(f'tt:linkactive:{self.a.pk}', [{'user': 'INUSE001', 'mac-address': 'AA:00:00:00:00:01', 'id': '*1'}], 300)
        self.tokens = {}

    def staff(self, email, role, business=None):
        u = User.objects.create_user(email, email, 'pw-123456')
        TeamMember.objects.create(business=business or self.biz, user=u, is_active=True, role=role)
        return u

    def token(self, email):
        if email not in self.tokens:
            r = self.client.post(API + 'auth/login/', json.dumps({'email': email, 'password': 'pw-123456', 'device': 'Pixel',
                                                                   'platform': 'android'}), content_type='application/json')
            self.tokens[email] = json.loads(r.content)['token']
            self.client.cookies.clear()
        return self.tokens[email]

    def get(self, path, email='o@x.gm', **q):
        r = self.client.get(API + path, q, HTTP_AUTHORIZATION=f'Bearer {self.token(email)}')
        return r.status_code, json.loads(r.content)

    def post(self, path, body, email='o@x.gm'):
        r = self.client.post(API + path, json.dumps(body), content_type='application/json',
                             HTTP_AUTHORIZATION=f'Bearer {self.token(email)}')
        return r.status_code, json.loads(r.content)

    def test_each_router_has_its_own_card(self):
        code, d = self.get('routers/')
        self.assertEqual(code, 200)
        cards = {c['name']: c for c in d['routers']}
        self.assertEqual(cards['Serrekunda']['users_online'], 1)
        self.assertEqual(cards['Serrekunda']['in_use'], 1)
        self.assertEqual(cards['Serrekunda']['sales_today'], 1)
        self.assertEqual(cards['Brikama']['users_online'], 0)
        self.assertTrue(any('never checked in' in w['text'] for w in cards['Brikama']['warnings']))
        self.assertTrue(d['can']['move'])

    def test_detail_shows_who_is_online(self):
        code, d = self.get(f'routers/{self.a.pk}/')
        self.assertEqual(code, 200)
        self.assertEqual([s['user'] for s in d['sessions']], ['INUSE001'])

    def test_me_tells_the_app(self):
        _c, d = self.get('me/')
        self.assertTrue(d['can']['routers'])
        self.assertTrue(d['can']['routers_manage'])

    def test_kick_on_link_router_queues_a_command(self):
        with mock.patch('core.linkops.send') as send:
            code, d = self.post(f'routers/{self.a.pk}/', {'action': 'kick', 'user': 'INUSE001'})
        self.assertEqual(code, 200, d)
        self.assertEqual(send.call_args[0][1], 'disconnect')

    def test_reboot_needs_the_name(self):
        code, _d = self.post(f'routers/{self.a.pk}/', {'action': 'reboot', 'confirm': 'nope'})
        self.assertEqual(code, 400)
        with mock.patch('core.agent.queue') as q:
            code, _d = self.post(f'routers/{self.a.pk}/', {'action': 'reboot', 'confirm': 'serrekunda'})
        self.assertEqual(code, 200)
        self.assertEqual(q.call_args[0][1], 'reboot')

    def test_sync(self):
        with mock.patch('core.tasks.enqueue_router_sync', return_value=(mock.Mock(progress=0), True)) as e:
            code, d = self.post(f'routers/{self.a.pk}/', {'action': 'sync'})
        self.assertEqual(code, 200, d)
        e.assert_called_once()

    def test_viewer_cannot_see_or_act(self):
        self.staff('v@x.gm', 'viewer')
        code, _d = self.get('routers/', email='v@x.gm')
        self.assertEqual(code, 403)

    def test_support_can_kick_but_not_reboot(self):
        self.staff('s@x.gm', 'voucher_support')
        code, _d = self.post(f'routers/{self.a.pk}/', {'action': 'reboot', 'confirm': 'Serrekunda'}, email='s@x.gm')
        self.assertEqual(code, 403)
        with mock.patch('core.linkops.send'):
            code, _d = self.post(f'routers/{self.a.pk}/', {'action': 'kick', 'user': 'INUSE001'}, email='s@x.gm')
        self.assertEqual(code, 200)

    def test_move_from_the_phone(self):
        other_owner = User.objects.create_user('n@x.gm', 'n@x.gm', 'pw-123456')
        target = Business.objects.create(user=other_owner, business_name='New Co', owner_name='N', phone='2',
                                         trial_ends_at=timezone.now() + timedelta(days=30))
        TeamMember.objects.create(business=target, user=self.owner, is_active=True, role='admin')
        code, d = self.get(f'routers/{self.a.pk}/move/', business_id=target.pk)
        self.assertEqual(code, 200)
        self.assertEqual(d['target']['id'], target.pk)
        self.assertEqual([t['id'] for t in d['targets']], [target.pk])
        code, d = self.post(f'routers/{self.a.pk}/move/', {'business_id': target.pk, 'confirm_name': 'Serrekunda'})
        self.assertEqual(code, 200, d)
        self.a.refresh_from_db()
        self.assertEqual(self.a.business_id, target.pk)
        self.assertEqual(Voucher.objects.get(code='INUSE001').business_id, target.pk)
        code, _d = self.get(f'routers/{self.a.pk}/')
        self.assertEqual(code, 404)                                  # gone from this business

    def test_quick_sell_is_a_paid_single_voucher_on_that_router(self):
        with mock.patch('core.views_agents.push_one', return_value=True):
            code, d = self.post('vouchers/new/', {'kind': 'single', 'plan': self.plan.pk, 'router': self.b.pk, 'paid': True,
                                                  'method': 'cash'})
        self.assertEqual(code, 200, d)
        v = Voucher.objects.get(code=d['voucher']['code'])
        self.assertEqual(v.router_id, self.b.pk)
        self.assertTrue(VoucherSale.objects.filter(voucher=v).exists())
