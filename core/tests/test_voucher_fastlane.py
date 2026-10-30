"""Voucher fast lane: reset really unblocks, newest Link command wins, changes reach the router at once."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from core import agent, shared_use, voucher_fastlane as fl, voucher_freeze as vf, voucher_history as vh
from core.live import voucher_problem
from core.models import AgentCommand, Business, DeviceSignature, Router, RouterAgent, Voucher

URL = 'https://taptap.example'


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('owner', password='x')
        self.biz = Business.objects.create(user=self.user, business_name='B', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=7), shared_warning_mode='auto')
        self.router = Router.objects.create(business=self.biz, name='R1', ip_address='10.0.0.1', username='a',
                                            password='p', connection_mode='agent')
        RouterAgent.objects.create(router=self.router, token_hash='h' * 64, last_seen_at=timezone.now(),
                                   enrolled_at=timezone.now())
        self.assertEqual(vh.channel(self.router), 'TapTap Link')

    def voucher(self, code='ABC123', **kw):
        now = timezone.now()
        data = dict(business=self.biz, router=self.router, code=code, plan_name='Day', duration_minutes=1440,
                    max_devices=1, used_at=now - timedelta(hours=1), expires_at=now + timedelta(hours=23),
                    mikrotik_sync_status='Synced')
        data.update(kw)
        with self.captureOnCommitCallbacks(execute=True):
            return Voucher.objects.create(**data)

    def pending(self, **kw):
        return AgentCommand.objects.filter(router=self.router, status__in=['queued', 'sent'], **kw)

    def shared_warn(self, v):
        for i in range(2):
            DeviceSignature.objects.create(business=self.biz, fingerprint=f'fp{i}', vouchers=[v.code],
                                           macs=[f'AA:BB:CC:00:00:0{i}'], last_mac=f'AA:BB:CC:00:00:0{i}')
        with self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(shared_use.check_after_login(self.biz, v))
        v.refresh_from_db()
        self.assertTrue(v.frozen_at)
        return v

    def reset(self, v):
        with self.captureOnCommitCallbacks(execute=True):
            ok, msg = vh.reset_devices(v, self.user, 'customer cannot connect')
        v.refresh_from_db()
        return ok, msg


class ResetReallyUnblocks(Base):
    def test_reset_lifts_shared_use_warning_and_customer_can_connect(self):
        v = self.shared_warn(self.voucher())
        ok, msg = self.reset(v)
        self.assertTrue(ok)
        self.assertEqual(v.status, 'active')
        self.assertIsNone(v.frozen_at)
        self.assertIsNone(voucher_problem(v, timezone.now()), 'live loop would still kick the customer')
        self.assertIsNone(vf.portal_block(v))
        self.assertIn('warning lifted', msg)
        # router gets: user enabled + heal (any MAC, old sessions/cookies cleared)
        self.assertTrue(self.pending(kind='hotspot_users_heal').exists())

    def test_reset_forgets_old_devices_so_next_login_is_not_rewarned(self):
        v = self.shared_warn(self.voucher())
        self.reset(v)
        self.assertEqual(shared_use._signatures(self.biz, v.code).get(v.code, []), [])
        DeviceSignature.objects.create(business=self.biz, fingerprint='new-phone', vouchers=[v.code])
        self.assertFalse(shared_use.check_after_login(self.biz, v))
        v.refresh_from_db()
        self.assertIsNone(v.frozen_at)

    def test_reset_keeps_a_freeze_put_on_by_staff_and_says_so(self):
        v = self.voucher()
        with self.captureOnCommitCallbacks(execute=True):
            vf.freeze([v], user=self.user, reason='unpaid', kind='freeze')
        v.refresh_from_db()
        ok, msg = self.reset(v)
        self.assertTrue(v.frozen_at)
        self.assertIn('FROZEN', msg)
        self.assertFalse(self.pending(kind='hotspot_users_heal').exists(), 'must not enable a frozen voucher')

    def test_reset_unblocks_phones_locked_out_for_mistyped_codes(self):
        from core.models_voucher_entry import VoucherEntryDevice
        v = self.shared_warn(self.voucher())
        row = VoucherEntryDevice.objects.create(business=self.biz, device_key='k1', mac_address='AA:BB:CC:00:00:01',
                                                attempts=5, blocked_at=timezone.now(),
                                                blocked_until=timezone.now() + timedelta(minutes=30))
        self.reset(v)
        row.refresh_from_db()
        self.assertFalse(row.is_blocked)
        self.assertEqual(row.attempts, 0)

    def test_devices_page_reset_also_lifts_warning(self):
        v = self.shared_warn(self.voucher())
        with self.captureOnCommitCallbacks(execute=True):
            shared_use.resolve(v, 'reset', self.user, '')
        v.refresh_from_db()
        self.assertIsNone(v.frozen_at)
        self.assertEqual(v.status, 'active')


class NewestLinkCommandWins(Base):
    def test_unanswered_disable_is_not_resent_after_reset(self):
        v = self.shared_warn(self.voucher())
        old = self.pending(kind='hotspot_users_disable').get()
        AgentCommand.objects.filter(pk=old.pk).update(status='sent', sent_at=timezone.now() - timedelta(minutes=5))
        self.reset(v)
        agent.build_response(self.router, URL)          # ACK timeout passed: previously re-queued and re-sent
        old.refresh_from_db()
        self.assertEqual(old.status, 'cancelled')

    def test_enable_cancels_a_disable_still_waiting(self):
        v = self.voucher()
        agent.queue(self.router, 'hotspot_user_set', {'name': v.code, 'disabled': True}, label='old disable')
        agent.queue(self.router, 'hotspot_user_set', {'name': v.code, 'disabled': False}, label='enable')
        kinds = list(self.pending(kind='hotspot_user_set').values_list('params', flat=True))
        self.assertEqual(kinds, [{'name': v.code, 'disabled': False}])

    def test_batch_disable_only_loses_the_reenabled_voucher(self):
        a, b = self.voucher('AAA111'), self.voucher('BBB222')
        agent.queue(self.router, 'hotspot_users_disable', {'names': [a.code, b.code], 'disabled': True})
        agent.queue(self.router, 'hotspot_user_set', {'name': a.code, 'disabled': False})
        batch = self.pending(kind='hotspot_users_disable').get()
        self.assertEqual(batch.params['names'], [b.code])

    def test_disable_cancels_pending_extend(self):
        v = self.voucher()
        agent.queue(self.router, 'hotspot_user_extend', {'name': v.code, 'seconds': 3600})
        agent.queue(self.router, 'hotspot_user_set', {'name': v.code, 'disabled': True})
        self.assertFalse(self.pending(kind='hotspot_user_extend').exists())


class StuckQueuedVouchers(Base):
    def test_expired_batch_releases_its_vouchers(self):
        v = self.voucher(mikrotik_sync_status='Queued')
        cmd = agent.queue(self.router, 'hotspot_users', {'users': [{'n': v.code, 'prof': 'p', 'lim': '1d', 'dis': False}],
                                                          'profiles': [], 'ids': [v.pk]})
        AgentCommand.objects.filter(pk=cmd.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        agent.build_response(self.router, URL)
        v.refresh_from_db()
        self.assertEqual(v.mikrotik_sync_status, 'Pending')

    def test_batch_that_never_got_an_answer_releases_its_vouchers(self):
        v = self.voucher(mikrotik_sync_status='Queued')
        cmd = agent.queue(self.router, 'hotspot_users', {'users': [], 'profiles': [], 'ids': [v.pk]})
        AgentCommand.objects.filter(pk=cmd.pk).update(status='sent', attempts=2,
                                                      sent_at=timezone.now() - timedelta(hours=1))
        agent.build_response(self.router, URL)
        v.refresh_from_db()
        self.assertEqual(v.mikrotik_sync_status, 'Pending')

    def test_orphaned_queued_voucher_is_found_and_resent(self):
        v = self.voucher()
        AgentCommand.objects.filter(router=self.router).delete()                  # e.g. pruned / lost
        Voucher.objects.filter(pk=v.pk).update(mikrotik_sync_status='Queued')     # no command carries it
        self.assertEqual(fl.release_stuck(self.router, force=True), 1)
        agent.push_pending_vouchers(self.router)
        self.assertTrue(any(v.pk in (c.params.get('ids') or []) for c in self.pending(kind='hotspot_users')))


class VoucherFastLane(Base):
    def test_voucher_commands_go_while_a_sync_piece_is_still_running(self):
        v = self.voucher()
        AgentCommand.objects.filter(router=self.router).delete()
        piece = agent.queue(self.router, 'inventory_piece', {'kind': 'traffic_dns'})
        AgentCommand.objects.filter(pk=piece.pk).update(status='sent', sent_at=timezone.now())
        agent.queue(self.router, 'interface_set', {'name': 'ether2', 'enabled': True})
        agent.queue(self.router, 'hotspot_user_set', {'name': v.code, 'disabled': False})
        body = agent.build_response(self.router, URL)
        self.assertIn(v.code, body)
        self.assertTrue(AgentCommand.objects.filter(kind='hotspot_user_set', status='sent').exists())
        self.assertTrue(AgentCommand.objects.filter(kind='interface_set', status='queued').exists(), 'others still wait')

    def test_voucher_commands_keep_their_order(self):
        v = self.voucher()
        AgentCommand.objects.filter(router=self.router).delete()
        first = agent.queue(self.router, 'disconnect', {'user': v.code})
        AgentCommand.objects.filter(pk=first.pk).update(status='sent', sent_at=timezone.now())
        agent.queue(self.router, 'hotspot_user_extend', {'name': v.code, 'seconds': 600})
        self.assertEqual(agent.build_response(self.router, URL), '')   # waits for the voucher batch in flight

    def test_new_batch_not_held_back_by_one_the_router_already_has(self):
        a = self.voucher('AAA111')
        AgentCommand.objects.filter(router=self.router).delete()
        sent = agent.queue(self.router, 'hotspot_users', {'users': [], 'profiles': [], 'ids': [a.pk]})
        AgentCommand.objects.filter(pk=sent.pk).update(status='sent', sent_at=timezone.now())
        b = self.voucher('BBB222')
        AgentCommand.objects.filter(router=self.router, status='queued').delete()   # b waits for the normal path
        Voucher.objects.filter(pk=b.pk).update(mikrotik_sync_status='Pending')
        cache.clear()
        self.assertEqual(agent.push_pending_vouchers(self.router), 1)

    def test_frozen_voucher_is_sent_disabled(self):
        v = self.voucher()
        Voucher.objects.filter(pk=v.pk).update(frozen_at=timezone.now(), status='active')
        v.refresh_from_db()
        row, _ = fl.user_row(v)
        self.assertTrue(row['dis'])


class ImmediateSyncOnChange(Base):
    def test_new_voucher_is_queued_for_the_router_at_once(self):
        v = self.voucher(mikrotik_sync_status='Pending')
        cmd = self.pending(kind='hotspot_users').get()
        self.assertEqual(cmd.params['ids'], [v.pk])
        v.refresh_from_db()
        self.assertEqual(v.mikrotik_sync_status, 'Queued')

    def test_status_change_is_sent_at_once_and_noop_save_is_not(self):
        v = self.voucher()
        AgentCommand.objects.all().delete()
        cache.clear()
        with self.captureOnCommitCallbacks(execute=True):
            v.save()
        self.assertFalse(self.pending().exists())
        with self.captureOnCommitCallbacks(execute=True):
            v.status = 'disabled'
            v.save(update_fields=['status'])
        row = self.pending(kind='hotspot_users').get().params['users'][0]
        self.assertTrue(row['dis'])

    def test_rename_is_left_to_the_rename_path(self):
        v = self.voucher()
        AgentCommand.objects.all().delete()
        with self.captureOnCommitCallbacks(execute=True):
            v.code = 'NEW999'
            v.save()
        self.assertFalse(self.pending(kind='hotspot_users').exists())

    def test_push_vouchers_right_after_creation_does_not_send_twice(self):
        from core.voucher_push import push_vouchers
        v = self.voucher(mikrotik_sync_status='Pending')
        push_vouchers([v])
        self.assertEqual(self.pending(kind='hotspot_users').count(), 1)

    def test_a_whole_batch_in_one_transaction_is_one_send(self):
        from django.db import transaction
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                for i in range(30):
                    Voucher.objects.create(business=self.biz, router=self.router, code=f'BATCH{i:03d}', plan_name='Day',
                                           duration_minutes=60)
        cmds = list(self.pending(kind='hotspot_users'))
        self.assertEqual(len(cmds), 1)
        self.assertEqual(len(cmds[0].params['users']), fl.LINK_CHUNK)
        self.assertEqual(Voucher.objects.filter(mikrotik_sync_status='Pending').count(), 30 - fl.LINK_CHUNK)

    def test_direct_api_router_gets_the_change_pushed(self):
        Router.objects.filter(pk=self.router.pk).update(connection_mode='api')
        self.router.refresh_from_db()
        with mock.patch('core.voucher_push.push_now', return_value=(1, 0)) as push:
            v = self.voucher(mikrotik_sync_status='Pending')
        push.assert_called_once()
        self.assertEqual(push.call_args[0][1], [v.pk])


class DirectApiReset(Base):
    def test_reset_on_api_router_enables_unlocks_and_clears_sessions(self):
        Router.objects.filter(pk=self.router.pk).update(connection_mode='api')
        self.router.refresh_from_db()
        with mock.patch('core.voucher_push.push_now', return_value=(1, 0)):
            v = self.voucher()
        Voucher.objects.filter(pk=v.pk).update(frozen_at=timezone.now(), freeze_kind='warning', status='disabled',
                                               frozen_left=3600)
        v.refresh_from_db()
        svc = mock.MagicMock()
        svc.connect.return_value = svc
        svc.upsert_voucher.return_value = ('updated', '*1')
        users = mock.MagicMock()
        users.get.return_value = [{'id': '*1'}]
        svc.resource.return_value = users
        with mock.patch('core.mikrotik.MikroTikService', return_value=svc), \
             mock.patch('core.voucher_push.push_now', return_value=(1, 0)), \
             mock.patch('core.voucher_freeze._apply_on_routers', return_value=[(True, 'ok')]):
            ok, msg = self.reset(v)
        self.assertTrue(ok, msg)
        self.assertFalse(svc.upsert_voucher.call_args.kwargs['disabled'])
        users.set.assert_any_call(id='*1', mac_address='00:00:00:00:00:00')
        svc.release_devices.assert_called_once()
        self.assertEqual(v.status, 'active')


class RolledBackTransactionDoesNotSilenceTheFastLane(TestCase):
    """A voucher saved in a transaction that rolls back must not stop later vouchers from being sent at once."""
    def test_flush_is_queued_again_after_a_rollback(self):
        from django.db import transaction
        from core import voucher_fastlane as fl
        fl._local.pending = set()
        try:
            with transaction.atomic():
                fl.schedule(999001)
                raise RuntimeError('member creation failed halfway')
        except RuntimeError:
            pass
        self.assertTrue(fl._local.pending)                        # the stale id is still there…
        with self.captureOnCommitCallbacks(execute=False) as cbs:
            fl.schedule(999002)
        self.assertTrue(any(cb is fl._flush for cb in cbs))        # …but a flush is queued again
        fl._local.pending = set()
