"""Fit can sync exactly what you name: a voucher, several, a batch, a plan, members, a router or everything."""
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from . import fit_actions, fit_sync, fit_tools
from .models import Business, Router, RouterHotspotUser, Voucher, VoucherBatch, VoucherPlan
from .models_fit import FitAction, FitSettings
from .permissions import ALL_PERMISSIONS


class SyncBase(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o@example.com', 'o@example.com', 'Pass-word-123', first_name='Omar')
        self.b = Business.objects.create(user=self.owner, business_name='Alpha', owner_name='A', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=30), is_unlimited=True)
        self.r = Router.objects.create(business=self.b, name='Tower', ip_address='1.1.1.1', username='a', password='b', status='Online')
        self.r2 = Router.objects.create(business=self.b, name='Market', ip_address='2.2.2.2', username='a', password='b', status='Online')
        self.plan = VoucherPlan.objects.create(business=self.b, name='Daily', price=25, duration_minutes=1440, max_devices=1,
                                               mikrotik_profile_name='daily')
        self.batch = VoucherBatch.objects.create(business=self.b, name='VC-009', plan=self.plan, quantity=3)
        mk = lambda code, **kw: Voucher.objects.create(**{**dict(business=self.b, router=self.r, code=code, plan_name='Daily',
                                                                 duration_minutes=1440, price=Decimal('25'), source='taptap'), **kw})
        self.a, self.c = mk('AAA11111', batch=self.batch), mk('CCC33333', batch=self.batch)
        self.member = mk('anna', login_type='member', max_devices=2)
        self.winbox = mk('WINBOX01', source='mikrotik')
        self.other = mk('OTH00001', router=self.r2)

    def ctx(self):
        s, _ = FitSettings.objects.get_or_create(business=self.b, defaults={'allowed': ['sync']})
        return fit_tools.FitContext(business=self.b, user=self.owner, perms=ALL_PERMISSIONS, settings=s, role_label='Owner')


class ResolveAndStatusTests(SyncBase):
    def test_resolve_scopes(self):
        self.assertEqual({v.code for v in fit_sync.resolve(self.b, 'voucher', 'aaa11111, anna')}, {'AAA11111', 'anna'})
        self.assertEqual({v.code for v in fit_sync.resolve(self.b, 'batch', batch='vc-009')}, {'AAA11111', 'CCC33333'})
        self.assertEqual({v.code for v in fit_sync.resolve(self.b, 'members')}, {'anna'})
        self.assertEqual({v.code for v in fit_sync.resolve(self.b, 'plan', plan='Daily', router='Market')}, {'OTH00001'})
        with self.assertRaises(fit_sync.SyncError) as e:
            fit_sync.resolve(self.b, 'voucher', 'AAA11111, NOPE')
        self.assertIn('NOPE', str(e.exception))
        with self.assertRaises(fit_sync.SyncError):
            fit_sync.resolve(self.b, 'batch', batch='VC-404')

    def test_status_names_the_differences(self):
        RouterHotspotUser.objects.create(business=self.b, router=self.r, username='AAA11111', profile='daily', disabled=True,
                                         limit_uptime='1d', uptime='0s')
        # CCC33333 has no router row: missing
        out = fit_sync.status(self.b, 'batch', batch='VC-009')
        self.assertEqual((out['checked'], out['out_of_sync']), (2, 2))
        diffs = {x['code']: ' '.join(x['differences']) for x in out['examples']}
        self.assertIn('disabled', diffs['AAA11111'])
        self.assertIn('missing', diffs['CCC33333'])
        self.assertEqual(out['routers'][0]['router'], 'Tower')

    def test_in_sync_voucher_has_no_differences(self):
        from .utils import voucher_profile
        prof = voucher_profile(self.a, self.plan)[0]
        RouterHotspotUser.objects.create(business=self.b, router=self.r, username='AAA11111', profile=prof, limit_uptime='1d', uptime='0s')
        self.assertEqual(fit_sync.status(self.b, 'voucher', 'AAA11111')['out_of_sync'], 0)


class RunTests(SyncBase):
    def test_voucher_member_and_router_made_each_take_the_right_path(self):
        with mock.patch('core.voucher_push.push_vouchers', return_value={self.r: '1 voucher(s) being sent to Tower'}) as push, \
                mock.patch('core.member_router_alignment.aligned_push_one', return_value=True) as member, \
                mock.patch('core.linkops.refresh', return_value=True) as refresh:
            msg = fit_sync.run(self.b, 'voucher', 'AAA11111, anna, WINBOX01', user=self.owner)
        self.assertEqual([v.code for v in push.call_args.args[0]], ['AAA11111'])
        self.assertEqual(member.call_args.args[0].code, 'anna')
        self.assertEqual(refresh.call_args.args[0], self.r)                 # router-made: read the router again
        self.assertIn('member', msg)
        self.assertIn('full sync started', msg)

    def test_expired_vouchers_are_not_resent(self):
        Voucher.objects.filter(pk=self.c.pk).update(status='expired')
        with mock.patch('core.voucher_push.push_vouchers', return_value={}) as push:
            msg = fit_sync.run(self.b, 'batch', batch='VC-009')
        self.assertEqual([v.code for v in push.call_args.args[0]], ['AAA11111'])
        self.assertIn('1 skipped', msg)

    def test_router_scopes(self):
        with mock.patch('core.linkops.refresh', return_value=True) as refresh:
            fit_sync.run(self.b, 'router', router='Market')
        self.assertEqual([c.args[0] for c in refresh.call_args_list], [self.r2])
        with mock.patch.object(fit_sync, '_time', return_value='t') as t, mock.patch.object(fit_sync, '_profiles', return_value='p'):
            fit_sync.run(self.b, 'router_time')
        self.assertEqual({c.args[0] for c in t.call_args_list}, {self.r, self.r2})
        with self.assertRaises(fit_sync.SyncError):
            fit_sync.run(self.b, 'router', router='Nowhere')

    def test_everything_on_one_router(self):
        with mock.patch.object(fit_sync, '_time', return_value='t'), mock.patch.object(fit_sync, '_profiles', return_value='p'), \
                mock.patch('core.voucher_push.push_vouchers', return_value={}) as push, \
                mock.patch('core.member_router_alignment.aligned_push_one', return_value=True), \
                mock.patch('core.linkops.refresh', return_value=True) as refresh:
            fit_sync.run(self.b, 'everything', router='Tower')
        self.assertEqual({v.code for v in push.call_args.args[0]}, {'AAA11111', 'CCC33333'})
        self.assertEqual(refresh.call_args.args[0], self.r)


class FitTests(SyncBase):
    def test_fit_proposes_then_owner_approves(self):
        ctx = self.ctx()
        card = fit_actions.propose(ctx, 'sync', {'scope': 'batch', 'batch': 'VC-009'})
        self.assertEqual(card['title'], 'Sync batch VC-009')
        self.assertIn('2 vouchers', card['detail'])
        with mock.patch('core.voucher_push.push_vouchers', return_value={self.r: '2 voucher(s) being sent to Tower'}) as push:
            ok, msg = fit_actions.run(FitAction.objects.get(pk=card['id']), self.owner, ALL_PERMISSIONS, ctx.settings)
        self.assertTrue(ok, msg)
        self.assertEqual(len(push.call_args.args[0]), 2)
        self.assertIn('Tower', msg)

    def test_codes_alone_mean_voucher_scope(self):
        card = fit_actions.propose(self.ctx(), 'sync', {'codes': 'AAA11111'})
        self.assertEqual(card['title'], 'Sync AAA11111')

    def test_bad_inputs_are_explained(self):
        with self.assertRaises(fit_actions.FitActionError):
            fit_actions.propose(self.ctx(), 'sync', {'scope': 'teleport'})
        with self.assertRaises(fit_actions.FitActionError) as e:
            fit_actions.propose(self.ctx(), 'sync', {'scope': 'batch', 'batch': 'nope'})
        self.assertIn('VC-009', str(e.exception))

    def test_needs_the_sync_group(self):
        ctx = self.ctx()
        ctx.settings.allowed = []
        card = fit_actions.propose(ctx, 'sync', {'codes': 'AAA11111'})
        ok, msg = fit_actions.run(FitAction.objects.get(pk=card['id']), self.owner, ALL_PERMISSIONS, ctx.settings)
        self.assertFalse(ok)
        self.assertIn('Sync with routers', msg)

    def test_status_tool(self):
        out = json.loads(fit_tools.call(self.ctx(), 'sync_status', {'scope': 'voucher', 'codes': 'AAA11111'}))
        self.assertEqual(out['out_of_sync'], 1)
        self.assertIn('kind=sync', out['hint'])
