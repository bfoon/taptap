"""Phones with random MACs must not be kicked: ghosts, MAC follow and strike-based kicks."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from . import device_lock as dl
from .models import Business, Router, Voucher

A, B, C = '02:11:22:33:44:01', '06:AA:BB:CC:DD:02', '00:1A:2B:3C:4D:03'   # A, B private (random); C a real hardware MAC


class RandomMacTests(TestCase):
    def setUp(self):
        cache.clear()
        owner = User.objects.create_user('o', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=owner, business_name='K', owner_name='A', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=self.b, name='R', ip_address='1.1.1.1', username='a', password='b')
        self.v = Voucher.objects.create(business=self.b, router=self.r, code='STICKY01', plan_name='x', max_devices=1)
        self.clock = 1_000_000.0

    def run_pass(self, sessions):
        """sessions: {mac: total_bytes}; returns kicked macs."""
        kicked = []
        rows = [(self.v, {'id': f'*{i}', 'mac-address': m, 'bytes-in': b, 'bytes-out': 0}) for i, (m, b) in enumerate(sessions.items())]
        with mock.patch('time.time', return_value=self.clock), \
             mock.patch('core.device_lock._forget_mac', side_effect=lambda v, mac: kicked.append((mac, True))), \
             mock.patch('core.device_lock.kick', side_effect=lambda r, v, mac, *a, **k: kicked.append((mac, k.get('quiet', False))) or True):
            dl.enforce_sessions(self.r, rows)
        self.clock += 60
        return kicked

    def test_phone_back_with_new_random_mac_is_not_kicked(self):
        self.run_pass({A: 1_000})                       # phone locked with MAC A
        self.run_pass({A: 900_000})                     # in use
        self.run_pass({A: 900_000})                     # phone left: A goes quiet …
        self.run_pass({A: 900_000})
        kicked = self.run_pass({A: 900_000, B: 50_000})  # … and comes back with random MAC B while A is still listed
        b = self.v.device_bindings.get()
        self.assertEqual((b.current_mac, b.previous_mac), (B, A))   # the slot followed the phone
        self.assertEqual(kicked, [(A, True)])                        # only the ghost was removed, quietly
        self.assertEqual(self.run_pass({B: 400_000}), [])            # stays connected

    def test_real_second_device_is_kicked_only_after_two_strikes(self):
        self.run_pass({A: 1_000})
        self.run_pass({A: 500_000, C: 1_000})            # C appears while A is busy: first strike, no kick yet
        kicked = self.run_pass({A: 900_000, C: 300_000})
        self.assertEqual([k for k in kicked if not k[1]], [(C, False)])
        self.assertEqual(self.v.device_bindings.get().current_mac, A)

    def test_ghost_does_not_take_its_slot_back(self):
        self.run_pass({A: 1_000}); self.run_pass({A: 800_000}); self.run_pass({A: 800_000}); self.run_pass({A: 800_000})
        self.run_pass({A: 800_000, B: 10_000})
        self.run_pass({A: 800_000, B: 300_000})          # ghost A still listed: must not flip the slot back
        self.assertEqual(self.v.device_bindings.get().current_mac, B)

    def test_portal_sees_quiet_session_as_gone(self):
        dl.claim(self.v, mac=A, fp='')
        mock.patch('core.device_lock._forget_mac').start(); self.addCleanup(mock.patch.stopall)
        cache.set(f'tt:tr:users:{self.r.pk}', {'users': {'STICKY01': [{'mac': A, 'down_bps': 0, 'up_bps': 0}]}}, 600)
        out = dl.claim(self.v, mac=B, fp='captive-window-id', hints=dl.online_hints(self.v))
        self.assertEqual(out.status, 'moved')                       # not "in use on another device"
