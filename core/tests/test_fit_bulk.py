"""Fit group changes: Owner/Admin only, context and warnings first, the exact vouchers that were shown."""
import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from core import fit_actions, fit_bulk, fit_tools, voucher_freeze as vf
from core.models import AgentCommand, Business, Router, RouterAgent, Voucher
from core.models_fit import FitAction, FitSettings
from core.permissions import ROLES

ADMIN = ROLES['admin'][2] | {fit_bulk.ADMIN_PERM}
SUPPORT = ROLES['voucher_support'][2]


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(self._reset_fastlane)
        self.user = User.objects.create_user('owner', password='x')
        self.biz = Business.objects.create(user=self.user, business_name='B', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=7))
        self.router = Router.objects.create(business=self.biz, name='Tower', ip_address='10.0.0.1', username='a',
                                            password='p', connection_mode='agent')
        RouterAgent.objects.create(router=self.router, token_hash='h' * 64, last_seen_at=timezone.now(), enrolled_at=timezone.now())
        self.settings = FitSettings.objects.create(business=self.biz, allowed=['bulk', 'vouchers'])

    @staticmethod
    def _reset_fastlane():
        # A TestCase never commits, so on_commit callbacks queued by changes made outside captureOnCommitCallbacks
        # would leave the fast lane's per-thread "pending" set filled for the next test module.
        from core import voucher_fastlane
        voucher_fastlane._local.pending = set()

    def voucher(self, code, **kw):
        now = timezone.now()
        data = dict(business=self.biz, router=self.router, code=code, plan_name='Day', duration_minutes=1440, max_devices=1,
                    used_at=now - timedelta(hours=1), expires_at=now + timedelta(hours=23), mikrotik_sync_status='Synced')
        data.update(kw)
        with self.captureOnCommitCallbacks(execute=True):
            return Voucher.objects.create(**data)

    def frozen(self, code, **kw):
        v = self.voucher(code, **kw)
        vf.freeze([v], user=self.user, reason='test')
        v.refresh_from_db()
        return v

    def ctx(self, perms=ADMIN):
        return fit_tools.FitContext(business=self.biz, user=self.user, perms=frozenset(perms), settings=self.settings)


class OnlyOwnerAndAdmin(Base):
    def test_staff_cannot_see_or_propose_it(self):
        self.assertNotIn('bulk_vouchers', fit_actions.kinds_for(SUPPORT))
        self.frozen('F1')
        with self.assertRaises(fit_actions.FitActionError):
            fit_actions.propose(self.ctx(SUPPORT), 'bulk_vouchers', {'operation': 'disable', 'state': 'frozen'})

    def test_admin_can(self):
        self.assertIn('bulk_vouchers', fit_actions.kinds_for(ADMIN))

    def test_approval_refused_without_the_right(self):
        self.frozen('F1')
        card = fit_actions.propose(self.ctx(), 'bulk_vouchers', {'operation': 'disable', 'state': 'frozen'})
        a = FitAction.objects.get(pk=card['id'])
        ok, msg = fit_actions.run(a, self.user, SUPPORT, self.settings)
        self.assertFalse(ok)
        self.assertIn('Owner or an Admin', msg)

    def test_is_admin(self):
        class R:  # minimal request
            def __init__(self, role, su=False):
                self.tt_role = role
                self.user = type('U', (), {'is_superuser': su})()
        self.assertTrue(fit_bulk.is_admin(R('owner')))
        self.assertTrue(fit_bulk.is_admin(R('admin')))
        self.assertTrue(fit_bulk.is_admin(R('voucher_support', su=True)))
        self.assertFalse(fit_bulk.is_admin(R('voucher_support')))
        self.assertFalse(fit_bulk.is_admin(R('finance')))


class DisableAllFrozen(Base):
    def test_card_gives_context_and_warnings(self):
        self.frozen('F1'); self.frozen('F2', login_type='member'); self.voucher('A1')
        card = fit_actions.propose(self.ctx(), 'bulk_vouchers', {'operation': 'disable', 'state': 'frozen', 'reason': 'Cleanup'})
        self.assertIn('Disable 2 vouchers', card['title'])
        self.assertEqual(set(card['sample']), {'F1', 'F2'})
        facts = dict(card['facts'])
        self.assertEqual(facts['Will change'], '2')
        self.assertIn('Tower', facts['Routers'])
        text = ' '.join(card['warnings'])
        self.assertIn('frozen', text)
        self.assertIn('member', text)
        self.assertEqual(card['danger'], 'danger')

    def test_runs_on_the_shown_vouchers_only(self):
        self.frozen('F1'); self.frozen('F2')
        card = fit_actions.propose(self.ctx(), 'bulk_vouchers', {'operation': 'disable', 'state': 'frozen'})
        late = self.frozen('F3')                             # frozen after the card was shown
        a = FitAction.objects.get(pk=card['id'])
        ok, msg = fit_actions.run(a, self.user, ADMIN, self.settings)
        self.assertTrue(ok, msg)
        for code in ('F1', 'F2'):
            v = Voucher.objects.get(code=code)
            self.assertEqual(v.status, 'disabled')
            self.assertIsNone(v.frozen_at)
        late.refresh_from_db()
        self.assertIsNotNone(late.frozen_at)
        self.assertTrue(AgentCommand.objects.filter(router=self.router, kind='hotspot_users_disable').exists())
        self.assertIn('2 of 2', msg)

    def test_changed_meanwhile_is_skipped(self):
        self.frozen('F1'); self.frozen('F2')
        card = fit_actions.propose(self.ctx(), 'bulk_vouchers', {'operation': 'disable', 'state': 'frozen'})
        vf.unfreeze([Voucher.objects.get(code='F2')], user=self.user, reason='back')
        Voucher.objects.filter(code='F2').update(status='disabled')
        ok, msg = fit_actions.run(FitAction.objects.get(pk=card['id']), self.user, ADMIN, self.settings)
        self.assertTrue(ok, msg)
        self.assertIn('1 of 2', msg)
        self.assertIn('already disabled', msg)

    def test_owner_must_allow_the_group(self):
        self.settings.allowed = ['vouchers']; self.settings.save()
        self.frozen('F1')
        card = fit_actions.propose(self.ctx(), 'bulk_vouchers', {'operation': 'disable', 'state': 'frozen'})
        self.assertFalse(card['authorized'])
        ok, msg = fit_actions.run(FitAction.objects.get(pk=card['id']), self.user, ADMIN, self.settings)
        self.assertFalse(ok)
        self.assertIn('Bulk voucher changes', msg)


class Safety(Base):
    def test_never_without_a_filter(self):
        self.voucher('A1')
        with self.assertRaises(fit_actions.FitActionError) as e:
            fit_actions.propose(self.ctx(), 'bulk_vouchers', {'operation': 'disable'})
        self.assertIn('never changes', str(e.exception))

    def test_nothing_matches(self):
        with self.assertRaises(fit_actions.FitActionError):
            fit_actions.propose(self.ctx(), 'bulk_vouchers', {'operation': 'unfreeze', 'state': 'frozen'})

    def test_big_change_asks_for_the_number(self):
        for i in range(fit_bulk.CONFIRM_AT):
            self.voucher(f'D{i:03d}')
        card = fit_actions.propose(self.ctx(), 'bulk_vouchers', {'operation': 'freeze', 'state': 'in_use', 'reason': 'x'})
        self.assertEqual(card['confirm'], str(fit_bulk.CONFIRM_AT))
        self.assertEqual(fit_actions.confirm_needed(FitAction.objects.get(pk=card['id'])), str(fit_bulk.CONFIRM_AT))

    def test_bin_only_unused_and_warns_about_sales(self):
        self.voucher('U1', used_at=None, expires_at=None, sold_at=timezone.now(), price=50)
        self.voucher('USED')
        card = fit_actions.propose(self.ctx(), 'bulk_vouchers', {'operation': 'to_bin', 'router': 'Tower', 'reason': 'old'})
        self.assertEqual(card['sample'], ['U1'])
        self.assertTrue(any('finance' in w for w in card['warnings']))
        self.assertEqual(card['confirm'], '1')          # binning always asks for the number

    def test_other_business_is_invisible(self):
        other = User.objects.create_user('o2', password='x')
        b2 = Business.objects.create(user=other, business_name='B2', owner_name='O', phone='2', trial_ends_at=timezone.now())
        Voucher.objects.create(business=b2, code='THEIRS', plan_name='Day', frozen_at=timezone.now(), status='disabled')
        self.frozen('MINE')
        card = fit_actions.propose(self.ctx(), 'bulk_vouchers', {'operation': 'disable', 'state': 'frozen'})
        self.assertEqual(card['sample'], ['MINE'])


class CountTool(Base):
    def test_counts_by_state(self):
        self.frozen('F1'); self.voucher('A1'); self.voucher('U1', used_at=None, expires_at=None)
        out = json.loads(fit_tools.call(self.ctx(SUPPORT), 'count_vouchers', {'state': 'frozen'}))
        self.assertEqual(out['matched'], 1)
        self.assertFalse(out['can_change_in_bulk'])
        out = json.loads(fit_tools.call(self.ctx(), 'count_vouchers', {}))
        self.assertEqual(out['matched'], 3)
        self.assertTrue(out['can_change_in_bulk'])


class ApprovalEndpoint(Base):
    def test_typed_number_is_checked_by_the_server(self):
        for i in range(fit_bulk.CONFIRM_AT):
            self.voucher(f'E{i:03d}')
        card = fit_actions.propose(self.ctx(), 'bulk_vouchers', {'operation': 'freeze', 'state': 'in_use', 'reason': 'x'})
        self.client.force_login(self.user)
        url = f'/fit/action/{card["id"]}/'
        r = self.client.post(url, json.dumps({'decision': 'run', 'confirm': '3'}), content_type='application/json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('Type', r.json()['message'])
        self.assertEqual(FitAction.objects.get(pk=card['id']).status, 'proposed')
        r = self.client.post(url, json.dumps({'decision': 'run', 'confirm': str(fit_bulk.CONFIRM_AT)}), content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Voucher.objects.filter(business=self.biz, frozen_at__isnull=False).count(), fit_bulk.CONFIRM_AT)
