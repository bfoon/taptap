"""Internet lines applied through TapTap Link: safe order, automatic confirm, automatic undo."""
import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.utils import timezone

from core import agent, wan, wan_link
from core.models import AgentCommand, Business, Router, RouterAgent, RouterConfigSnapshot, WanSetup

LINES = [{'interface': 'ether1', 'type': 'dhcp', 'gateway': '192.168.1.1', 'check': '8.8.8.8', 'label': 'Starlink', 'speed': 50},
         {'interface': 'ether3', 'type': 'dhcp', 'gateway': '192.168.1.1', 'check': '1.1.1.1', 'label': 'QCell', 'speed': 50}]
FACTS = {'v7': True, 'hotspot': True, 'dns_servers': ['8.8.8.8'], 'lans': [],
         'links': [{'interface': 'ether1', 'type': 'dhcp', 'gateway': '192.168.1.1', 'add_default_route': 'yes'},
                   {'interface': 'ether3', 'type': 'dhcp', 'gateway': '192.168.1.1', 'add_default_route': 'yes'}]}


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('o', password='x')
        biz = Business.objects.create(user=self.user, business_name='B', owner_name='O', phone='1',
                                      trial_ends_at=timezone.now() + timedelta(days=7))
        self.router = Router.objects.create(business=biz, name='TapTap K', ip_address='10.0.0.1', username='a',
                                            password='p', connection_mode='agent')
        RouterAgent.objects.create(router=self.router, token_hash='h' * 64, last_seen_at=timezone.now(),
                                   enrolled_at=timezone.now(), ros_version='7.16.2')
        self.setup = WanSetup.objects.create(router=self.router)

    def start(self, strategy='ecmp'):
        cfg, errors, _ = wan.normalize({'strategy': strategy, 'links': LINES}, FACTS)
        self.assertEqual(errors, [])
        return wan_link.start(self.router, self.setup, cfg, dict(FACTS), user=self.user)

    def cmds(self):
        return list(AgentCommand.objects.filter(router=self.router, kind=wan_link.LINK_KIND).order_by('pk'))

    def mark(self, status, seconds_ago=0, cmds=None):
        for c in cmds or self.cmds():
            AgentCommand.objects.filter(pk=c.pk).update(status=status, done_at=timezone.now() - timedelta(seconds=seconds_ago))


class SafeOrder(Base):
    def test_undo_timer_first_then_parts(self):
        out = self.start()
        cmds = self.cmds()
        self.assertEqual(cmds[0].params['step'], 'arm')
        self.assertGreaterEqual(cmds[0].params['minutes'], wan_link.MIN_MINUTES)
        self.assertEqual([c.params['step'] for c in cmds[1:]], ['part'] * out['parts'])
        self.setup.refresh_from_db()
        self.assertEqual(self.setup.status, 'pending')
        self.assertEqual(self.setup.last_result['via'], 'link')

    def test_new_setup_goes_in_before_the_old_one_is_removed(self):
        self.start()
        text = '\n'.join(c.params['source'] for c in self.cmds() if c.params['step'] == 'part')
        first_route = text.index('/ip route add dst-address=0.0.0.0/0')
        cleanup = text.index('/ip route remove [find where comment~"^TapTap WAN" and !(comment~"#')
        handover = text.index('add-default-route=no')
        self.assertLess(first_route, cleanup)
        self.assertLess(cleanup, handover)
        self.assertNotIn('/ip route remove [find where comment~"^TapTap WAN"]', text)   # never the unconditional purge

    def test_existing_lists_are_retagged_and_old_taptap_nat_does_not_count(self):
        self.start()
        text = '\n'.join(c.params['source'] for c in self.cmds() if c.params['step'] == 'part')
        self.assertIn('else={/interface list set $x comment=', text)
        self.assertIn('action=masquerade and disabled=no and !(comment~"^TapTap WAN")', text)

    def test_each_line_is_tied_to_its_own_modem_and_old_hand_routes_are_switched_off(self):
        self.start()
        text = '\n'.join(c.params['source'] for c in self.cmds() if c.params['step'] == 'part')
        self.assertIn('dst-address=8.8.8.8/32 gateway="192.168.1.1%ether1"', text)
        self.assertIn('dst-address=1.1.1.1/32 gateway="192.168.1.1%ether3"', text)
        self.assertIn('dst-address=8.8.8.8/32 and static and disabled=no and !(comment~"^TapTap")', text)
        self.assertIn(wan.LINK_OFF, text)

    def test_every_command_fits_a_link_batch_and_parses_alone(self):
        self.start('balance')
        for c in self.cmds():
            self.assertLessEqual(len(agent.wrap(c, 'https://t.example', 'yes')), agent.MAX_SCRIPT, c.label)

    def test_a_part_is_never_sent_twice(self):
        self.assertIn(wan_link.LINK_KIND, agent.NEVER_RESEND)

    def test_one_apply_at_a_time(self):
        self.start()
        with self.assertRaises(wan_link.LinkApplyError):
            self.start()

    def test_undo_puts_hand_made_routes_back(self):
        self.start()
        undo = self.cmds()[0].params['undo']
        self.assertIn(f'comment~"{wan.LINK_OFF}"', undo)
        self.assertIn('/ip dns set servers=8.8.8.8', undo)
        body = wan_link.body(self.cmds()[0].params)
        self.assertIn('/system scheduler add name=TapTap-WAN-undo interval=8m', body)
        self.assertNotIn('\n', body.split('on-event=')[1].split(' comment=')[0])


class ConfirmAndUndo(Base):
    def test_confirmed_only_after_the_router_stays_online(self):
        self.start()
        self.mark('done', seconds_ago=10)
        self.assertEqual(wan_link.progress(self.router), 'settling')
        self.mark('done', seconds_ago=wan_link.SETTLE + 5)
        self.assertEqual(wan_link.progress(self.router), 'confirming')
        confirm = AgentCommand.objects.get(pk=WanSetup.objects.get(pk=self.setup.pk).last_result['confirm'])
        self.assertIn('already undone', wan_link.body(confirm.params))
        self.mark('done', cmds=[confirm])
        self.assertEqual(wan_link.progress(self.router), 'active')
        self.setup.refresh_from_db()
        self.assertEqual(self.setup.status, 'active')
        self.assertIsNone(self.setup.confirm_by)

    def test_router_that_undid_by_itself_is_reported(self):
        self.start()
        self.mark('done', seconds_ago=wan_link.SETTLE + 5)
        wan_link.progress(self.router)
        confirm = AgentCommand.objects.get(pk=WanSetup.objects.get(pk=self.setup.pk).last_result['confirm'])
        self.mark('failed', cmds=[confirm])
        self.assertEqual(wan_link.progress(self.router), 'undone')
        self.setup.refresh_from_db()
        self.assertEqual(self.setup.status, 'undone')

    def test_failed_part_undoes_at_once(self):
        self.start()
        parts = self.cmds()
        self.mark('done', cmds=parts[:2])
        self.mark('failed', cmds=parts[2:3])
        self.assertEqual(wan_link.progress(self.router), 'undoing')
        undo = self.cmds()[-1]
        self.assertEqual(undo.params['step'], 'undo')
        self.assertIn('/system scheduler remove [find where name="TapTap-WAN-undo"]', wan_link.body(undo.params))
        self.mark('done', cmds=[undo])
        self.assertEqual(wan_link.progress(self.router), 'undone')
        self.setup.refresh_from_db()
        self.assertEqual(self.setup.status, 'failed')

    def test_link_check_in_moves_it_on(self):
        """The real check-in handler (agent.handle_poll) confirms the apply — no page needs to be open."""
        self.start()
        self.mark('done', seconds_ago=wan_link.SETTLE + 5)
        try:
            agent.handle_poll(self.router.agent, {'sv': str(self.router.agent.script_version)}, '10.0.0.1', 'https://t.example')
        except Exception:
            pass        # the rest of the check-in may need more data; the Internet lines step runs before it
        self.setup.refresh_from_db()
        self.assertEqual(self.setup.last_result['stage'], 'confirming')


class DesignerOnALinkRouter(Base):
    def setUp(self):
        super().setUp()
        sec = lambda path, rows: {'path': path, 'rows': rows}
        RouterConfigSnapshot.objects.create(router=self.router, sections={
            'DHCP clients': sec('/ip/dhcp-client', [{'interface': 'ether1', 'status': 'bound', 'gateway': '192.168.1.1', 'add-default-route': 'yes'},
                                                    {'interface': 'ether3', 'status': 'bound', 'gateway': '192.168.1.1', 'add-default-route': 'yes'}]),
            'IP addresses': sec('/ip/address', [{'address': '192.168.1.50/24', 'interface': 'ether1'},
                                                {'address': '192.168.1.77/24', 'interface': 'ether3'}]),
            'Interfaces': sec('/interface', [{'name': 'ether1', 'running': 'true'}, {'name': 'ether3', 'running': 'true'}]),
            'Routes': sec('/ip/route', []),
            'DNS': sec('/ip/dns', [{'servers': '8.8.8.8'}]),
        })
        self.client = Client()
        self.client.force_login(self.user)

    def test_apply_now_works_through_link(self):
        r = self.client.post(self._url('wan_apply'),
                             data=json.dumps({'config': {'strategy': 'ecmp', 'links': LINES}, 'undo_minutes': 5}),
                             content_type='application/json')
        data = r.json()
        self.assertTrue(data['success'], data)
        self.assertTrue(data['via_link'])
        self.assertEqual(data['status'], 'pending')
        self.assertTrue(self.cmds())

    def test_status_reports_progress(self):
        self.client.post(self._url('wan_apply'), data=json.dumps({'config': {'strategy': 'ecmp', 'links': LINES}}),
                         content_type='application/json')
        data = self.client.get(self._url('wan_status')).json()
        self.assertTrue(data['via_link'])
        self.assertEqual(data['stage'], 'sending')

    def test_undo_through_link(self):
        data = self.client.post(self._url('wan_undo')).json()
        self.assertTrue(data['success'], data)
        self.assertEqual(self.cmds()[-1].params['step'], 'undo')

    def _url(self, name):
        from django.urls import reverse
        return reverse(name, args=[self.router.pk])
