"""Members whose time is not finished are never signed out by TapTap, and stay signed in while idle.

Run:  DB_ENGINE=sqlite CELERY_EAGER=1 python manage.py test core.test_member_stay_signed_in
"""
import time
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from . import device_lock as dl, sticky
from .models import AgentCommand, Business, Router, RouterAgent, RouterHotspotProfile, Voucher


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('owner', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='Kairaba Net', owner_name='A', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        now = timezone.now()
        self.member = Voucher.objects.create(business=self.b, code='fatou', plan_name='Monthly', max_devices=1,
                                             login_type='member', status='active', used_at=now - timedelta(days=3),
                                             expires_at=now + timedelta(days=27), duration_minutes=30 * 1440)
        self.voucher = Voucher.objects.create(business=self.b, code='ONEDEV01', plan_name='x', max_devices=1)

    def svc(self, removed, user, mac):
        class Res:
            def get(s, **k): return [{'id': '*7', 'user': user, 'mac-address': mac}]
            def remove(s, id): removed.append(id)
        class Svc:
            def resource(s, p): return Res()
        return Svc()


class DeviceLockTests(Base):
    def test_member_with_time_left_is_protected(self):
        self.assertTrue(dl.protected_member(self.member))
        self.assertFalse(dl.protected_member(self.voucher))
        self.member.expires_at = timezone.now() - timedelta(minutes=1)
        self.assertFalse(dl.protected_member(self.member))           # time finished: normal rules again
        self.member.expires_at = timezone.now() + timedelta(days=1)
        self.member.frozen_at = timezone.now()
        self.assertFalse(dl.protected_member(self.member))           # frozen by the owner: may be signed out

    def test_live_sync_never_kicks_a_member_with_time_left(self):
        r = Router.objects.create(business=self.b, name='R1', ip_address='1.1.1.1', username='a', password='b')
        dl.claim(self.member, mac='AA:BB:CC:DD:EE:01')
        removed = []
        def rows(b):
            return [(self.member, {'id': '*7', 'mac-address': 'AA:BB:CC:DD:EE:02', 'bytes-in': b, 'bytes-out': 0}),
                    (self.member, {'id': '*8', 'mac-address': 'AA:BB:CC:DD:EE:01', 'bytes-in': b, 'bytes-out': 0})]
        for b in (10_000, 500_000, 900_000):
            self.assertEqual(dl.enforce_sessions(r, rows(b), svc=self.svc(removed, 'fatou', 'AA:BB:CC:DD:EE:02')), 0)
        self.assertEqual(removed, [])

    def test_ordinary_voucher_still_locked(self):
        r = Router.objects.create(business=self.b, name='R1', ip_address='1.1.1.1', username='a', password='b')
        dl.claim(self.voucher, mac='AA:BB:CC:DD:EE:01')
        removed = []
        def rows(b):
            return [(self.voucher, {'id': '*7', 'mac-address': 'AA:BB:CC:DD:EE:02', 'bytes-in': b, 'bytes-out': 0}),
                    (self.voucher, {'id': '*8', 'mac-address': 'AA:BB:CC:DD:EE:01', 'bytes-in': b, 'bytes-out': 0})]
        dl.enforce_sessions(r, rows(10_000), svc=self.svc(removed, 'ONEDEV01', 'AA:BB:CC:DD:EE:02'))
        self.assertEqual(dl.enforce_sessions(r, rows(500_000), svc=self.svc(removed, 'ONEDEV01', 'AA:BB:CC:DD:EE:02')), 1)

    def test_idle_member_phone_keeps_its_slot_and_session(self):
        """The reported bug: a member's phone goes quiet (screen off) and another device shows up —
        the idle phone used to be treated as gone, its session and cookie removed, password asked again."""
        r = Router.objects.create(business=self.b, name='R1', ip_address='1.1.1.1', username='a', password='b')
        self.member.router = r
        self.member.save(update_fields=['router'])
        dl.claim(self.member, mac='AA:BB:CC:DD:EE:01')
        now = time.time()
        dl.note_activity(r.pk, 'AA:BB:CC:DD:EE:01', 1000, now - 600)         # last real traffic 10 min ago: idle
        dl.note_activity(r.pk, 'AA:BB:CC:DD:EE:01', 1000, now)
        with mock.patch.object(dl, '_forget_mac', wraps=dl._forget_mac) as forget, \
                mock.patch('core.linkops.send') as link_send:
            rows = [(self.member, {'id': '*8', 'mac-address': 'AA:BB:CC:DD:EE:01', 'bytes-in': 1000, 'bytes-out': 0}),
                    (self.member, {'id': '*9', 'mac-address': '7A:BB:CC:DD:EE:02', 'bytes-in': 50_000, 'bytes-out': 0})]
            for extra in (0, 600_000, 1_200_000):
                rows[1][1]['bytes-in'] = 50_000 + extra
                dl.enforce_sessions(r, rows, svc=self.svc([], 'fatou', '7A:BB:CC:DD:EE:02'))
        link_send.assert_not_called()
        self.assertEqual(self.member.device_bindings.get().current_mac, 'AA:BB:CC:DD:EE:01')   # slot stays with the phone
        for call in forget.call_args_list:                                                     # and nothing was removed
            self.assertTrue(dl.protected_member(call.args[0]))

    def test_release_stale_never_signs_out_member_devices(self):
        r = Router.objects.create(business=self.b, name='R1', ip_address='1.1.1.1', username='a', password='b')
        self.member.router = r
        self.member.save(update_fields=['router'])
        cache.set(f'tt:tr:users:{r.pk}', {'users': {'FATOU': [{'mac': 'AA:BB:CC:DD:EE:01'}]}}, 60)
        with mock.patch.object(dl, 'kick') as kick:
            self.assertEqual(dl.release_stale(self.member, '11:22:33:44:55:66'), 0)
        kick.assert_not_called()


class ProfileTests(Base):
    def test_member_profile_values(self):
        vals = sticky.member_profile_values(self.b)
        self.assertEqual((vals['idle-timeout'], vals['add-mac-cookie'], vals['session-timeout']), ('none', 'yes', '0s'))
        self.b.sticky_sessions = False
        self.assertEqual(sticky.member_profile_values(self.b)['keepalive-timeout'], '2h')   # members stay in anyway
        self.assertTrue(sticky.is_member_profile('taptap-member-plan-4'))
        self.assertTrue(sticky.is_member_profile('taptap-unlimited-member-plan-9'))
        self.assertFalse(sticky.is_member_profile('1-Day'))


class LinkTests(Base):
    def setUp(self):
        super().setUp()
        self.r = Router.objects.create(business=self.b, name='hAP', status='Online', connection_mode='agent')
        RouterAgent.objects.create(router=self.r, last_seen_at=timezone.now(), ros_version='7.15.3')

    def test_alignment_command_fixes_member_profile_too(self):
        from .agent import command_body, queue
        from .test_link_install import routeros_balanced
        cmd = queue(self.r, 'hotspot_users_limit', {'users': [{'n': 'fatou', 'lim': '30d', 'prof': 'taptap-member-plan-4'}],
                                                   'profiles': [{'name': 'taptap-member-plan-4', 'shared': 1}]}, label='x')
        body = command_body(cmd)
        self.assertTrue(routeros_balanced(body))
        self.assertIn('/ip hotspot user profile set [find name="taptap-member-plan-4"] add-mac-cookie=yes', body)
        self.assertIn('idle-timeout=none', body)
        self.assertIn('keepalive-timeout=2h', body)
        self.assertIn('mac-cookie', body.split('/ip hotspot profile find')[1])

    def test_voucher_profiles_are_not_touched(self):
        from .agent import command_body, queue
        cmd = queue(self.r, 'hotspot_users_limit', {'users': [{'n': 'ONEDEV01', 'lim': '1d', 'prof': '1-Day'}],
                                                   'profiles': [{'name': '1-Day', 'shared': 1}]}, label='x')
        self.assertNotIn('session-timeout=0s', command_body(cmd))

    def test_existing_bad_profiles_are_corrected_after_inventory(self):
        from .member_keepalive import ensure_member_profiles
        RouterHotspotProfile.objects.create(business=self.b, router=self.r, name='taptap-member-plan-4',
                                            idle_timeout='none', keepalive_timeout='2m', session_timeout='')
        RouterHotspotProfile.objects.create(business=self.b, router=self.r, name='taptap-member-plan-5',
                                            idle_timeout='none', keepalive_timeout='2h', session_timeout='0s')
        RouterHotspotProfile.objects.create(business=self.b, router=self.r, name='1-Day', keepalive_timeout='2m')
        self.assertEqual(ensure_member_profiles(self.r), 1)
        cmd = AgentCommand.objects.get(kind='member_profiles')
        self.assertEqual(cmd.params['names'], ['taptap-member-plan-4'])
        self.assertEqual(ensure_member_profiles(self.r), 0)               # not again within 30 minutes

    def test_member_profiles_command_is_validated(self):
        from .agent import queue
        with self.assertRaises(ValueError):
            queue(self.r, 'member_profiles', {'names': ['1-Day']})
        with self.assertRaises(ValueError):
            queue(self.r, 'member_profiles', {'names': ['taptap-member-plan-1"; /system reset']})


class ApiTests(Base):
    def test_ensure_hotspot_profile_gives_member_values(self):
        from .mikrotik import MikroTikService
        r = Router.objects.create(business=self.b, name='R1', ip_address='1.1.1.1', username='a', password='b')
        added = {}
        class Profiles:
            def get(s, **k): return []
            def add(s, **kw): added.update(kw); return '*1'
        svc = MikroTikService(r)
        with mock.patch.object(MikroTikService, 'resource', return_value=Profiles()):
            svc.ensure_hotspot_profile('taptap-member-plan-4', 2)
        self.assertEqual((added['idle_timeout'], added['session_timeout'], added['add_mac_cookie']), ('none', '0s', 'yes'))
