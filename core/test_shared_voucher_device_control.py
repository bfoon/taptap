"""Regression tests for removing exactly one locked voucher device."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, RequestFactory
from django.utils import timezone

from . import device_lock
from . import shared_voucher_device_control as control
from .models import Business, Voucher, VoucherDeviceBinding
from .templatetags.taptap_extras import device_link


class SingleDeviceRemoveTests(TestCase):
    def setUp(self):
        cache.clear()
        control.install()
        self.user = User.objects.create_user(
            username='owner-device-remove@example.com',
            email='owner-device-remove@example.com',
            password='pw12345678',
        )
        self.business = Business.objects.create(
            user=self.user,
            business_name='Shared WiFi',
            owner_name='Owner',
            phone='2200000',
            trial_ends_at=timezone.now() + timedelta(days=30),
            is_unlimited=True,
            device_lock=True,
        )
        self.voucher = Voucher.objects.create(
            business=self.business,
            code='FAMILY-REMOVE-10',
            plan_name='Family 10',
            max_devices=10,
            duration_minutes=43200,
            status='active',
        )
        self.bindings = []
        for slot in range(1, 11):
            row = VoucherDeviceBinding.objects.create(
                business=self.business,
                voucher=self.voucher,
                slot_no=slot,
                current_mac=f'02:10:20:30:40:{slot:02X}',
                locked_by='router',
            )
            self.bindings.append(row)

    def test_remove_one_keeps_other_nine_bindings(self):
        target = self.bindings[3]
        target.previous_mac = '02:AA:BB:CC:DD:04'
        target.device_token_hash = 'phone-four-token'
        target.save(update_fields=['previous_mac', 'device_token_hash'])
        keep_ids = {row.pk for row in self.bindings if row.pk != target.pk}

        with mock.patch('core.device_lock._forget_mac') as forget, \
             mock.patch('core.voucher_history.record') as record:
            ok, result = control.remove_one(self.voucher, target.pk, self.user)

        self.assertTrue(ok)
        self.assertIn('slot 4', result)
        self.assertEqual(self.voucher.device_bindings.count(), 9)
        self.assertEqual(
            set(self.voucher.device_bindings.values_list('pk', flat=True)),
            keep_ids,
        )
        self.assertFalse(self.voucher.device_bindings.filter(slot_no=4).exists())
        forgotten = {call.args[1] for call in forget.call_args_list}
        self.assertEqual(
            forgotten,
            {'02:10:20:30:40:04', '02:AA:BB:CC:DD:04'},
        )
        self.assertEqual(record.call_args.args[1], 'device')
        self.assertEqual(record.call_args.kwargs['slot_no'], 4)

    def test_removed_mac_cannot_immediately_reclaim_freed_slot(self):
        target = self.bindings[4]
        old_mac = target.current_mac
        with mock.patch('core.device_lock._forget_mac'), \
             mock.patch('core.voucher_history.record'):
            control.remove_one(self.voucher, target.pk, self.user)

        result = device_lock.claim(
            self.voucher,
            mac=old_mac,
            source='router',
            hints={'online_macs': set(), 'router_id': None},
        )
        self.assertEqual(result.status, 'denied')
        self.assertEqual(self.voucher.device_bindings.count(), 9)
        self.assertFalse(self.voucher.device_bindings.filter(slot_no=5).exists())

    def test_freed_slot_is_reused_by_next_different_legitimate_device(self):
        target = self.bindings[5]
        with mock.patch('core.device_lock._forget_mac'), \
             mock.patch('core.voucher_history.record'):
            control.remove_one(self.voucher, target.pk, self.user)

        newcomer = '02:99:88:77:66:55'
        with mock.patch('core.voucher_history.record'):
            result = device_lock.claim(
                self.voucher,
                mac=newcomer,
                source='router',
                hints={'online_macs': set(), 'router_id': None},
            )

        self.assertEqual(result.status, 'new')
        self.assertEqual(result.binding.slot_no, 6)
        self.assertEqual(self.voucher.device_bindings.count(), 10)

    def test_binding_from_other_voucher_is_safe_noop(self):
        other = Voucher.objects.create(
            business=self.business,
            code='OTHER-VOUCHER',
            plan_name='Family 10',
            max_devices=10,
            duration_minutes=43200,
            status='active',
        )
        other_binding = VoucherDeviceBinding.objects.create(
            business=self.business,
            voucher=other,
            slot_no=1,
            current_mac='02:EE:EE:EE:EE:01',
        )

        ok, message = control.remove_one(self.voucher, other_binding.pk, self.user)
        self.assertTrue(ok)
        self.assertIn('No device was removed', message)
        self.assertTrue(VoucherDeviceBinding.objects.filter(pk=other_binding.pk).exists())
        self.assertEqual(self.voucher.device_bindings.count(), 10)

    def test_normal_reset_path_is_unchanged_and_clears_remove_guard(self):
        target = self.bindings[0]
        with mock.patch('core.device_lock._forget_mac'), \
             mock.patch('core.voucher_history.record'):
            control.remove_one(self.voucher, target.pk, self.user)

        self.assertTrue(control._is_temporarily_removed(self.voucher, mac=target.current_mac))

        original = mock.Mock(return_value=(True, 'all reset'))
        wrapped = control._wrapped_reset(original)
        result = wrapped(self.voucher, user=self.user, reason='Customer changed all phones')

        self.assertEqual(result, (True, 'all reset'))
        original.assert_called_once_with(
            self.voucher,
            user=self.user,
            reason='Customer changed all phones',
        )
        self.assertFalse(control._is_temporarily_removed(self.voucher, mac=target.current_mac))

    def test_remove_button_is_only_for_current_binding_mac(self):
        target = self.bindings[0]
        request = RequestFactory().get(f'/vouchers/{self.voucher.pk}/')
        request.user = self.user
        context = {
            'v': self.voucher,
            'd': target,
            'tt_perms': frozenset({'vouchers.support'}),
            'request': request,
        }

        html = str(device_link(context, target.current_mac))
        self.assertIn('Remove', html)
        self.assertIn(control.removal_reason(target.pk), html)
        self.assertIn('reset-mac', html)

        target.previous_mac = '02:AB:CD:EF:00:01'
        previous_html = str(device_link(context, target.previous_mac))
        self.assertNotIn('Remove', previous_html)

    def test_read_only_user_never_gets_remove_button(self):
        target = self.bindings[0]
        request = RequestFactory().get(f'/vouchers/{self.voucher.pk}/')
        request.user = self.user
        html = str(device_link({
            'v': self.voucher,
            'd': target,
            'tt_perms': frozenset({'vouchers.view'}),
            'request': request,
        }, target.current_mac))
        self.assertNotIn('Remove', html)
