"""Vouchers first on TapTap Link, and "sync this voucher now" (Fit, voucher page, app) that keeps trying until the
router confirms (core/voucher_priority.py)."""
import json
import time
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core import agent as link, mobile_auth, voucher_fastlane as fl, voucher_priority as vp, voucher_ready as vr
from core.models import AgentCommand, Business, Router, RouterAgent, Voucher, VoucherEvent
from core.tests import test_sync_turns

CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'vprio'}}
URL = 'https://t.example'


@override_settings(CACHES=CACHE)
class FairTurnsWithCache(test_sync_turns.FairTurns):
    """The fair-turn tests with a working cache (they need one to remember whose turn it is)."""


@override_settings(CACHES=CACHE, AUTH_EMAIL_OTP=False)
class Base(TestCase):
    def setUp(self):
        cache.clear()
        fl._local.pending = set()
        self.owner = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456')
        self.biz = Business.objects.create(user=self.owner, business_name='Kerr Sering', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.router = Router.objects.create(business=self.biz, name='KerrSering', connection_mode='agent', status='Online')
        self.agent = RouterAgent.objects.create(router=self.router, token_hash='p' * 64, last_seen_at=timezone.now(),
                                                enrolled_at=timezone.now(), script_version=9, ros_version='7.15')

    def voucher(self, code, **kw):
        data = dict(business=self.biz, router=self.router, code=code, plan_name='Day', duration_minutes=1440, status='active',
                    mikrotik_sync_status='Synced')
        data.update(kw)
        return Voucher.objects.create(**data)

    def sent(self):
        return list(AgentCommand.objects.filter(router=self.router, status='sent').order_by('sent_at', 'pk'))

    def answer_all(self, status='ok'):
        for c in self.sent():
            link.handle_ack(c.pk, link.nonce(c), status)
        AgentCommand.objects.filter(router=self.router, status='sent').update(sent_at=timezone.now() - timedelta(minutes=5))


class VouchersFirst(Base):
    def test_the_screenshot_vouchers_go_before_traffic_samples(self):
        old = timezone.now() - timedelta(minutes=3)
        for kind, params, label in (('disconnect', {'user': 'AFE3R'}, 'Disconnect AFE3R'),
                                    ('hotspot_users_disable', {'names': ['OLD1'], 'disabled': True, 'reason': 'expired'}, 'Switch off 1 voucher'),
                                    ('hotspot_users_remove', {'names': ['DEL1', 'DEL2']}, 'Remove 2 deleted voucher(s)')):
            c = link.queue(self.router, kind, params, label=label)
            AgentCommand.objects.filter(pk=c.pk).update(created_at=old)
        for k in ('traffic_dns', 'traffic_conns'):
            c = link.queue(self.router, 'inventory_piece', {'kind': k}, label='Traffic sample (apps & sites)', minutes=5)
            AgentCommand.objects.filter(pk=c.pk).update(created_at=old - timedelta(minutes=1))
        cache.set(f'tt:link:lastv:{self.router.pk}', 1, 600)     # "the other work's turn" — it used to put vouchers last
        link.build_response(self.router, URL)
        kinds = [c.kind for c in self.sent()]
        self.assertIn('disconnect', kinds)
        self.assertIn('hotspot_users_disable', kinds)
        self.assertIn('hotspot_users_remove', kinds)
        self.assertNotIn('inventory_piece', kinds, 'samples never ride while a voucher change waits')

    def test_small_voucher_changes_ride_along_on_the_other_works_turn(self):
        other = link.queue(self.router, 'ping', {}, label='waiting job')
        AgentCommand.objects.filter(pk=other.pk).update(created_at=timezone.now() - timedelta(minutes=1))
        link.queue(self.router, 'disconnect', {'user': 'AFE3R'}, label='Disconnect AFE3R')
        cache.set(f'tt:link:lastv:{self.router.pk}', 1, 600)
        script = link.build_response(self.router, URL)
        self.assertEqual({c.kind for c in self.sent()}, {'disconnect', 'ping'})
        self.assertLess(script.index('AFE3R'), script.index('test from TapTap'), 'the voucher change runs first')

    def test_no_new_samples_while_vouchers_wait(self):
        with mock.patch('core.linkops.uses_link', return_value=True):
            link.queue(self.router, 'disconnect', {'user': 'AFE3R'}, label='Disconnect AFE3R')
            link.link_traffic_samples(self.router)
            self.assertFalse(AgentCommand.objects.filter(kind='inventory_piece').exists())
            AgentCommand.objects.filter(kind='disconnect').update(status='done')
            link.link_traffic_samples(self.router)
            self.assertTrue(AgentCommand.objects.filter(kind='inventory_piece').exists())

    def test_customers_first_in_the_waiting_list(self):
        stock = [self.voucher(f'S{i:03}', mikrotik_sync_status='Pending') for i in range(30)]      # newest stock
        used = self.voucher('USED01', mikrotik_sync_status='Pending', used_at=timezone.now() - timedelta(hours=1))
        sold = self.voucher('SOLD01', mikrotik_sync_status='Pending', sold_at=timezone.now())
        AgentCommand.objects.all().delete()
        link.push_pending_vouchers(self.router, limit=3)
        names = [u['n'] for u in AgentCommand.objects.get(kind='hotspot_users').params['users']]
        self.assertEqual(names[:2], ['USED01', 'SOLD01'])
        self.assertEqual(names[2], stock[-1].code)

    def test_router_busy_with_last_batch_is_asked_back_in_2_seconds(self):
        c = link.queue(self.router, 'disconnect', {'user': 'A1'}, label='x')
        AgentCommand.objects.filter(pk=c.pk).update(status='sent', sent_at=timezone.now())
        link.queue(self.router, 'disconnect', {'user': 'A2'}, label='y')
        self.assertEqual(link.build_response(self.router, URL), link.TURBO_LINE)


class SendFirst(Base):
    def test_named_voucher_jumps_the_whole_line_and_is_watched_until_confirmed(self):
        backlog = [self.voucher(f'B{i:03}', mikrotik_sync_status='Pending') for i in range(60)]
        link.push_pending_vouchers(self.router)                  # a big batch already waiting
        for i in range(30):
            link.queue(self.router, 'disconnect', {'user': f'X{i}'}, label='old')
        v = self.voucher('AVA8E', used_at=timezone.now() - timedelta(hours=2), expires_at=timezone.now() + timedelta(hours=10))
        fl.unclaim([v.pk])
        lines = vp.prioritize([v], self.owner, 'customer waiting')
        self.assertIn('sent first', lines[0])
        self.assertTrue(vp.command_ids(self.router))
        link.build_response(self.router, URL)
        first = self.sent()[0]
        self.assertEqual(first.kind, 'hotspot_users')
        self.assertIn(v.pk, first.params['ids'], 'the priority voucher leads the batch')
        self.assertTrue(link.handle_ack(first.pk, link.nonce(first), 'ok'))
        vp.tick(self.router)
        self.assertEqual(vp.watching(self.router.pk), {}, 'confirmed → no longer watched')
        self.assertTrue(any('confirmed' in (e.detail or {}).get('text', '') for e in VoucherEvent.objects.filter(voucher=v, reason='Priority sync')))
        self.assertTrue(len(backlog) == 60)

    def test_keeps_trying_when_the_router_refuses_or_drops_it(self):
        v = self.voucher('AVA8E', used_at=timezone.now() - timedelta(hours=2), expires_at=timezone.now() + timedelta(hours=10))
        fl.unclaim([v.pk])
        vp.prioritize([v], self.owner)
        link.build_response(self.router, URL)
        cmd = self.sent()[0]
        link.handle_ack(cmd.pk, link.nonce(cmd), 'fail')            # the router failed it
        w = vp.watching(self.router.pk)
        w[str(v.pk)]['last'] = time.time() - 60
        cache.set(vp._wkey(self.router.pk), w, 600)
        vp.tick(self.router)
        again = AgentCommand.objects.filter(router=self.router, kind='hotspot_users', status='queued')
        self.assertTrue(any(v.pk in (c.params or {}).get('ids', []) for c in again), 'sent again')
        self.assertEqual(vp.watching(self.router.pk)[str(v.pk)]['tries'], 2)

    def test_gives_up_after_15_minutes_and_says_so(self):
        v = self.voucher('AVA8E', used_at=timezone.now() - timedelta(hours=2), expires_at=timezone.now() + timedelta(hours=10))
        vp.prioritize([v], self.owner)
        w = vp.watching(self.router.pk)
        w[str(v.pk)]['until'] = time.time() - 1
        cache.set(vp._wkey(self.router.pk), w, 600)
        vp.tick(self.router)
        self.assertEqual(vp.watching(self.router.pk), {})
        self.assertTrue(any('did not confirm' in (e.detail or {}).get('text', '') for e in VoucherEvent.objects.filter(voucher=v)))

    def test_expired_and_router_made_are_not_forced(self):
        a = self.voucher('GONE01', status='expired')
        b = self.voucher('MIKH01', source='mikrotik')
        lines = vp.prioritize([a, b], self.owner)
        self.assertTrue(any('time is over' in x for x in lines))
        self.assertTrue(any('made on the router' in x for x in lines))
        self.assertEqual(vp.watching(self.router.pk), {})

    def test_fit_sync_now_uses_send_first(self):
        from core import fit_sync
        v = self.voucher('AVA8E', used_at=timezone.now() - timedelta(hours=2), expires_at=timezone.now() + timedelta(hours=10))
        out = fit_sync.run(self.biz, 'voucher', codes='ava8e', user=self.owner)
        self.assertIn('sent first', out)
        self.assertIn(str(v.pk), vp.watching(self.router.pk))
        n, detail = fit_sync.describe(self.biz, 'voucher', codes='AVA8E')
        self.assertIn('go first', detail)

    def test_voucher_page_and_app(self):
        v = self.voucher('AVA8E', used_at=timezone.now() - timedelta(hours=2), expires_at=timezone.now() + timedelta(hours=10))
        self.client.force_login(self.owner)
        page = self.client.get(f'/vouchers/{v.pk}/')
        self.assertContains(page, 'Send to the router first')
        r = self.client.post(f'/vouchers/{v.pk}/first/')
        self.assertEqual(r.status_code, 302)
        self.assertIn(str(v.pk), vp.watching(self.router.pk))
        cache.clear()
        _dev, token = mobile_auth.create_device(self.owner, name='Pixel', platform='android')
        h = {'HTTP_AUTHORIZATION': f'Bearer {token}'}
        d = self.client.get(f'/api/mobile/v1/vouchers/{v.pk}/', **h).json()
        self.assertTrue(d['voucher']['can']['first'])
        d = self.client.post(f'/api/mobile/v1/vouchers/{v.pk}/', json.dumps({'action': 'first'}), content_type='application/json', **h).json()
        self.assertTrue(d['ok'], d)
        self.assertIn(str(v.pk), vp.watching(self.router.pk))


@override_settings(CACHES=CACHE)
class DirectApi(TestCase):
    def setUp(self):
        cache.clear()
        owner = User.objects.create_user('a@x.gm', 'a@x.gm', 'pw-123456')
        self.biz = Business.objects.create(user=owner, business_name='B', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.router = Router.objects.create(business=self.biz, name='R', ip_address='10.0.0.1', username='a', password='p')
        self.v = Voucher.objects.create(business=self.biz, router=self.router, code='API001', plan_name='Day', duration_minutes=60)

    def test_pushed_now_and_retried_until_it_works(self):
        with mock.patch('core.voucher_ready.push_exact', return_value=False):
            vp.prioritize([self.v])
        self.assertIn(str(self.v.pk), vp.watching(self.router.pk))
        w = vp.watching(self.router.pk)
        w[str(self.v.pk)]['last'] = time.time() - 60
        cache.set(vp._wkey(self.router.pk), w, 600)

        def ok(pk, unpin=False):
            vr.confirm([pk])
            return True
        with mock.patch('core.voucher_ready.push_exact', side_effect=ok):
            vp.tick_api()
        cache.delete(f'tt:vprio:api:{self.router.pk}')
        vp.tick_api()
        self.assertEqual(vp.watching(self.router.pk), {})
