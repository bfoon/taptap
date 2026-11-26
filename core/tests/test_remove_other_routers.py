"""Removing "Other routers & APs": migration 0136 keeps routers clean, the farewell closes open paths, nothing else breaks."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from core import agent, features as ft, remote_farewell
from core.models import Business, Router
from core.models_farewell import RemoteFarewell


class Migration0136(TransactionTestCase):
    before = [('core', '0135_market_vetting')]
    after = [('core', '0136_remove_other_routers')]

    def test_open_paths_are_kept_for_closing_and_choices_cleaned(self):
        ex = MigrationExecutor(connection)
        ex.migrate(self.before)
        old = ex.loader.project_state(self.before).apps
        U = old.get_model('auth', 'User')
        B, R = old.get_model('core', 'Business'), old.get_model('core', 'Router')
        Dev, Ses, Cmd = old.get_model('core', 'NetDevice'), old.get_model('core', 'RemoteSession'), old.get_model('core', 'AgentCommand')
        u = U.objects.create(username='o@x.gm', email='o@x.gm')
        b = B.objects.create(user=u, business_name='B', owner_name='O', phone='1', trial_ends_at=timezone.now() + timedelta(days=5),
                             features_off=['other_devices', 'bonanza'], feature_overrides={'other_devices': True, 'fit': False})
        r = R.objects.create(business=b, name='Hap', ip_address='1.2.3.4', username='a', password='b')
        d = Dev.objects.create(business=b, router=r, name='TP-Link', ip='192.168.88.20')
        Ses.objects.create(device=d, user=u, token='abcdefghijKLMN', mode='proxy', port=41001, target_host='10.0.0.1',
                           expires_at=timezone.now() + timedelta(minutes=20))
        Ses.objects.create(device=d, user=u, token='closedXXXXyyyy', mode='proxy', port=41002, target_host='10.0.0.1',
                           expires_at=timezone.now(), closed_at=timezone.now())
        Cmd.objects.create(router=r, kind='remote_nat', params={'rules': []}, status='queued', expires_at=timezone.now() + timedelta(minutes=10))
        ex = MigrationExecutor(connection)
        ex.loader.build_graph()
        ex.migrate(self.after)
        new = ex.loader.project_state(self.after).apps
        F = new.get_model('core', 'RemoteFarewell')
        self.assertEqual(list(F.objects.values_list('comment', flat=True)), ['TT-REMOTE abcdefghij'])
        self.assertEqual(new.get_model('core', 'AgentCommand').objects.get().status, 'cancelled')
        b2 = new.get_model('core', 'Business').objects.get()
        self.assertEqual((b2.features_off, b2.feature_overrides), (['bonanza'], {'fit': False}))
        tables = connection.introspection.table_names()
        self.assertNotIn('core_netdevice', tables)
        self.assertNotIn('core_remotesession', tables)
        self.assertNotIn('core_omadacontroller', tables)


class Farewell(TestCase):
    def setUp(self):
        u = User.objects.create_user('f@x.gm', 'f@x.gm', 'pw-123456')
        self.b = Business.objects.create(user=u, business_name='B', owner_name='O', phone='1', trial_ends_at=timezone.now() + timedelta(days=5))
        self.r = Router.objects.create(business=self.b, name='Hap', ip_address='1.2.3.4', username='a', password='b')

    def test_closes_and_forgets(self):
        RemoteFarewell.objects.create(router=self.r, comment='TT-REMOTE abcdefghij')
        with mock.patch('core.remote_farewell._close') as close:
            self.assertEqual(remote_farewell.process(), 1)
        close.assert_called_once_with(self.r, 'TT-REMOTE abcdefghij')
        self.assertFalse(RemoteFarewell.objects.exists())

    def test_unreachable_router_is_tried_again_then_given_up(self):
        RemoteFarewell.objects.create(router=self.r, comment='TT-REMOTE abcdefghij')
        with mock.patch('core.remote_farewell._close', side_effect=OSError('down')):
            remote_farewell.process()
            self.assertEqual(RemoteFarewell.objects.get().tries, 1)
            RemoteFarewell.objects.update(tries=remote_farewell.MAX_TRIES - 1)
            remote_farewell.process()
        self.assertFalse(RemoteFarewell.objects.exists())

    def test_link_command_still_allowed_but_only_to_close(self):
        self.assertIn('remote_close', agent.SAFE_KINDS)
        self.assertNotIn('remote_nat', agent.SAFE_KINDS)
        agent._validate_remote({'comment': 'TT-REMOTE abcdefghij'})
        with self.assertRaises(ValueError):
            agent._validate_remote({'comment': 'anything else'})

    def test_module_is_gone_everywhere(self):
        for name in ('netdev_list', 'netdev_save', 'remote_proxy_root', 'omada_save'):
            with self.assertRaises(NoReverseMatch):
                reverse(name)
        self.assertNotIn('other_devices', ft.FEATURES)
        self.client.force_login(self.b.user)
        self.assertNotContains(self.client.get(reverse('dashboard')), 'Other routers')
        self.assertEqual(self.client.get('/network/devices/').status_code, 404)
