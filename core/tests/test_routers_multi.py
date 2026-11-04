"""Several MikroTiks on one account: each works on its own, tabs per router, moving a router to another business."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core import agent, live, router_move as rm
from core.models import (Activity, Agent, Business, Router, RouterAgent, RouterHotspotUser, RouterNeighbor,
                         SessionIncident, Voucher, VoucherBatch, VoucherPlan, VoucherSale)
from core.models_team import TeamMember
from core.netgraph import build_graph, focus_graph
from core.utils import log


def make_business(email, name):
    user = User.objects.create_user(email, email=email, password='pw-123456')
    biz = Business.objects.create(user=user, business_name=name, owner_name='Owner', phone='1',
                                  trial_ends_at=timezone.now() + timedelta(days=30))
    return user, biz


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.user, self.biz = make_business('owner@example.com', 'Two Sites')
        self.a = Router.objects.create(business=self.biz, name='Site A', ip_address='10.0.0.1', username='u', password='p', status='Online')
        self.b = Router.objects.create(business=self.biz, name='Site B', ip_address='10.0.0.2', username='u', password='p', status='Online')


# ─────────────────────────── each router on its own ───────────────────────────
class DirectApiIsolation(Base):
    def watch(self, router, users, active):
        svc = mock.MagicMock()
        svc.hotspot_users.return_value = users
        svc.active_users.return_value = active
        svc.bindings.return_value = []
        with mock.patch('core.live.MikroTikService') as cls:
            cls.return_value.connect.return_value = svc
            return live.watch_router(router, force=True)

    def test_site_b_session_does_not_start_site_a_voucher(self):
        v = Voucher.objects.create(business=self.biz, router=self.a, code='SITEA001', plan_name='Day', source='mikrotik')
        summary = self.watch(self.b, [{'name': 'SITEA001', 'profile': 'default', '.id': '*1'}],
                             [{'user': 'SITEA001', 'mac-address': 'AA:BB:CC:00:00:01', 'uptime': '5m'}])
        v.refresh_from_db()
        self.assertIsNone(v.used_at)
        self.assertEqual(v.router_id, self.a.pk)
        self.assertEqual(summary.get('foreign_codes'), 1)
        self.assertTrue(RouterHotspotUser.objects.filter(router=self.b, username='SITEA001').exists())

    def test_own_voucher_still_starts(self):
        v = Voucher.objects.create(business=self.biz, router=self.b, code='SITEB001', plan_name='Day', source='mikrotik')
        self.watch(self.b, [{'name': 'SITEB001', 'profile': 'default', '.id': '*1'}],
                   [{'user': 'SITEB001', 'mac-address': 'AA:BB:CC:00:00:02', 'uptime': '5m'}])
        v.refresh_from_db()
        self.assertIsNotNone(v.used_at)


class ActivityPerRouter(Base):
    def test_router_named_in_text_is_attributed(self):
        log(self.biz, 'Router Sync', 'Site B: full inventory synchronized')
        self.assertEqual(Activity.objects.latest('pk').router_id, self.b.pk)

    def test_longest_name_wins(self):
        c = Router.objects.create(business=self.biz, name='Site B2', ip_address='10.0.0.3')
        log(self.biz, 'Router Sync', 'Site B2: synced')
        self.assertEqual(Activity.objects.latest('pk').router_id, c.pk)

    def test_business_wide_entry_has_no_router(self):
        log(self.biz, 'Plan', 'Day plan price changed')
        self.assertIsNone(Activity.objects.latest('pk').router_id)

    def test_explicit_router(self):
        log(self.biz, 'Port Control', 'ether2 off', router=self.a)
        self.assertEqual(Activity.objects.latest('pk').router_id, self.a.pk)


class RouterTabs(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_dashboard_tab_shows_one_router(self):
        log(self.biz, 'Router Sync', 'Site A: synced')
        log(self.biz, 'Router Sync', 'Site B: synced')
        r = self.client.get(reverse('dashboard') + f'?router={self.b.pk}')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['selected_router'].pk, self.b.pk)
        self.assertEqual({a.router_id for a in r.context['activities']}, {self.b.pk})
        self.assertContains(r, 'All routers')

    def test_choice_is_remembered_and_all_clears_it(self):
        self.client.get(reverse('dashboard') + f'?router={self.a.pk}')
        r = self.client.get(reverse('dashboard'))
        self.assertEqual(r.context['selected_router'].pk, self.a.pk)
        r = self.client.get(reverse('dashboard') + '?router=all')
        self.assertIsNone(r.context['selected_router'])

    def test_another_business_router_is_ignored(self):
        _u, other = make_business('other@example.com', 'Other')
        foreign = Router.objects.create(business=other, name='Theirs', ip_address='10.9.9.9')
        r = self.client.get(reverse('dashboard') + f'?router={foreign.pk}')
        self.assertIsNone(r.context['selected_router'])

    def test_topology_graph_focuses_on_router(self):
        r = self.client.get(reverse('topology_graph') + f'?router={self.a.pk}')
        ids = {n['id'] for n in r.json()['nodes'] if n['type'] == 'router' and not n.get('outside')}
        self.assertEqual(ids, {f'router:{self.a.pk}'})

    def test_pages_render_with_a_router_tab(self):
        for name in ('topology', 'active_users', 'ip_bindings', 'traffic'):
            with mock.patch('core.views.MikroTikService'):
                r = self.client.get(reverse(name) + f'?router={self.b.pk}')
            self.assertEqual(r.status_code, 200, name)
            self.assertContains(r, 'data-router-tab', msg_prefix=name)
        self.assertEqual(self.client.get(reverse('ip_bindings_data')).status_code, 200)

    def test_routers_page_has_move_dialog(self):
        r = self.client.get(reverse('routers'))
        self.assertContains(r, 'data-move-router')
        self.assertContains(r, 'moveRouterModal')

    def test_single_router_business_shows_no_tabs(self):
        self.b.delete()
        r = self.client.get(reverse('dashboard'))
        self.assertFalse(r.context['router_tabs']['show'])


class DuplicateIdentity(Base):
    def test_two_routers_called_mikrotik_are_not_merged(self):
        RouterNeighbor.objects.create(router=self.a, neighbor_key='ap1', identity='MikroTik', mac_address='AA:00:00:00:00:09',
                                      interface_name='ether3', is_online=True)
        with mock.patch('core.netgraph._identity', return_value=('MikroTik', '')):
            g = build_graph(self.biz, include_clients=False)
        peers = [e for e in g['edges'] if e['kind'] == 'peer']
        self.assertEqual(peers, [])                         # the AP named MikroTik is not "Site B"
        labels = {n['label'] for n in g['nodes'] if n['type'] == 'router'}
        self.assertEqual(labels, {'Site A', 'Site B'})

    def test_focus_keeps_only_that_router(self):
        g = {'nodes': [{'id': f'router:{self.a.pk}', 'type': 'router', 'router_id': self.a.pk, 'status': 'online'},
                       {'id': f'router:{self.b.pk}', 'type': 'router', 'router_id': self.b.pk, 'status': 'online'},
                       {'id': 'nb:x', 'type': 'switch', 'router_id': self.b.pk}, {'id': 'internet', 'type': 'internet'}],
             'edges': [{'source': f'router:{self.b.pk}', 'target': 'nb:x', 'router_id': self.b.pk, 'kind': 'lan'}],
             'live_interfaces': {str(self.b.pk): ['ether2']}}
        f = focus_graph(g, self.a.pk)
        self.assertEqual({n['id'] for n in f['nodes']}, {f'router:{self.a.pk}', 'internet'})
        self.assertEqual(f['live_interfaces'], {})


class SharedLinkToken(Base):
    def setUp(self):
        super().setUp()
        self.agent = RouterAgent.objects.create(router=self.a, token_hash='c' * 64, enrolled_at=timezone.now())
        self.t = timezone.now()

    def beat(self, ident, up, ip='1.1.1.1', board='hAP ax2'):
        self.t += timedelta(seconds=10)
        return agent.note_device(self.agent, {'id': ident, 'board': board, 'ver': '7.15', 'up': up}, ip, self.t)

    def test_two_devices_on_one_token_are_found(self):
        found = None
        for i in range(4):
            found = self.beat('MikroTik', f'{10 + i}d') or found
            found = self.beat('MikroTik', f'{1 + i}h') or found       # same name and model, very different uptime
        self.assertIsNotNone(found)
        self.assertGreaterEqual(found['flips'], 3)

    def test_one_router_that_reboots_once_is_fine(self):
        found = None
        for i in range(5):
            found = self.beat('Shop', f'3d{i}m')
        found = self.beat('Shop', '20s')                                     # reboot
        for i in range(5):
            found = self.beat('Shop', f'{30 + i * 10}s')
        self.assertIsNone(found)

    def test_link_page_explains_it(self):
        for i in range(4):
            self.beat('Front', f'{10 + i}d'); self.beat('Back', f'{1 + i}h', board='RB750')
        self.client.force_login(self.user)
        r = self.client.get(reverse('router_link', args=[self.a.pk]))
        self.assertContains(r, 'Two routers share this token')


# ─────────────────────────── moving a router ───────────────────────────
class MoveRouter(Base):
    def setUp(self):
        super().setUp()
        self.owner2, self.target = make_business('owner2@example.com', 'New Owner Ltd')
        TeamMember.objects.create(business=self.target, user=self.user, role='admin', is_active=True)
        RouterAgent.objects.create(router=self.a, token_hash='d' * 64, enrolled_at=timezone.now())
        self.plan = VoucherPlan.objects.create(business=self.biz, name='Day Pass', price=25, duration_minutes=1440)
        self.seller = Agent.objects.create(business=self.biz, name='Fatou')
        self.batch_a = VoucherBatch.objects.create(business=self.biz, name='A only', plan=self.plan, agent=self.seller)
        self.mixed = VoucherBatch.objects.create(business=self.biz, name='Mixed', plan=self.plan)
        self.va = Voucher.objects.create(business=self.biz, router=self.a, code='MOVE0001', plan_name='Day Pass',
                                         batch=self.batch_a, agent=self.seller, source='taptap')
        self.va2 = Voucher.objects.create(business=self.biz, router=self.a, code='MOVE0002', plan_name='Day Pass',
                                          batch=self.mixed, used_at=timezone.now(), source='taptap')
        self.vb = Voucher.objects.create(business=self.biz, router=self.b, code='STAY0001', plan_name='Day Pass',
                                         batch=self.mixed, source='taptap')
        self.sale = VoucherSale.objects.create(business=self.biz, voucher=self.va2, router=self.a, agent=self.seller,
                                               plan_name='Day Pass', voucher_code='MOVE0002', amount=25)
        RouterHotspotUser.objects.create(business=self.biz, router=self.a, username='MOVE0001')
        self.b.uplink_router = self.a
        self.b.save(update_fields=['uplink_router'])

    def test_needs_admin_in_both_businesses(self):
        TeamMember.objects.filter(business=self.target).update(role='viewer')
        with self.assertRaises(rm.MoveRefused):
            rm.find_target(self.user, self.biz, self.target.pk)
        with self.assertRaises(rm.MoveRefused):
            rm.find_target(self.user, self.biz, self.biz.pk)
        with self.assertRaises(rm.MoveRefused):
            rm.find_target(self.user, self.biz, 999999)
        with self.assertRaises(rm.MoveRefused):
            rm.find_target(self.user, self.biz, 'abc')

    def test_targets_list_only_businesses_you_run(self):
        self.assertEqual([t['id'] for t in rm.targets(self.user, self.biz)], [self.target.pk])

    def test_move_with_vouchers(self):
        s = rm.move(self.a, rm.find_target(self.user, self.biz, self.target.pk), self.user)
        self.a.refresh_from_db(); self.va.refresh_from_db(); self.va2.refresh_from_db(); self.vb.refresh_from_db()
        self.sale.refresh_from_db(); self.b.refresh_from_db(); self.batch_a.refresh_from_db(); self.mixed.refresh_from_db()
        self.assertEqual(self.a.business_id, self.target.pk)
        self.assertEqual(s['vouchers'], 2)
        self.assertEqual((self.va.business_id, self.va2.business_id), (self.target.pk, self.target.pk))
        self.assertIsNone(self.va.agent_id)                                 # the agent belongs to the old business
        self.assertEqual(self.vb.business_id, self.biz.pk)                  # the other router's voucher stays
        self.assertEqual(self.sale.business_id, self.biz.pk)                # money stays where it was earned
        self.assertEqual(self.sale.agent_id, self.seller.pk)
        self.assertEqual(self.batch_a.business_id, self.target.pk)          # every voucher of it moved
        self.assertIsNone(self.batch_a.agent_id)
        self.assertEqual(self.batch_a.plan.business_id, self.target.pk)
        self.assertEqual(self.mixed.business_id, self.biz.pk)               # half moved: stays,
        self.assertIsNone(self.va2.batch_id)                                # the moved voucher leaves it
        self.assertTrue(VoucherPlan.objects.filter(business=self.target, name='Day Pass').exists())
        self.assertEqual(RouterHotspotUser.objects.get(username='MOVE0001').business_id, self.target.pk)
        self.assertIsNone(self.b.uplink_router_id)                          # no cable drawn across businesses
        self.assertEqual(self.a.agent.token_hash, 'd' * 64)                 # Link keeps working
        self.assertTrue(Activity.objects.filter(business=self.biz, type='Router moved').exists())
        self.assertTrue(Activity.objects.filter(business=self.target, type='Router moved', router=self.a).exists())

    def test_move_without_vouchers(self):
        inc = SessionIncident.objects.create(business=self.biz, router=self.a, voucher=self.va, username='MOVE0001',
                                             mac_address='AA:BB:CC:00:00:09', reason='expired', first_seen=timezone.now(),
                                             last_seen=timezone.now())
        rm.move(self.a, self.target, self.user, with_vouchers=False)
        self.va.refresh_from_db(); inc.refresh_from_db()
        self.assertEqual(self.va.business_id, self.biz.pk)
        self.assertEqual(inc.business_id, self.target.pk)
        self.assertIsNone(inc.voucher_id)                                   # it no longer reaches the old business

    def test_preview_counts(self):
        p = rm.preview(self.a)
        self.assertEqual(p['vouchers']['total'], 2)
        self.assertEqual(p['vouchers']['in_use'], 1)
        self.assertEqual(p['sales_stay'], 1)

    def test_page_flow(self):
        self.client.force_login(self.user)
        url = reverse('router_move_preview', args=[self.a.pk])
        d = self.client.get(url + f'?business_id={self.target.pk}').json()
        self.assertEqual(d['target']['id'], self.target.pk)
        r = self.client.post(reverse('router_move', args=[self.a.pk]), {'business_id': self.target.pk, 'confirm_name': 'wrong'})
        self.a.refresh_from_db()
        self.assertEqual(self.a.business_id, self.biz.pk)
        r = self.client.post(reverse('router_move', args=[self.a.pk]),
                             {'business_id': self.target.pk, 'confirm_name': 'site a', 'with_vouchers': 'on'})
        self.assertEqual(r.status_code, 302)
        self.a.refresh_from_db()
        self.assertEqual(self.a.business_id, self.target.pk)

    def test_viewer_cannot_move(self):
        viewer = User.objects.create_user('viewer@example.com', email='viewer@example.com', password='pw-123456')
        TeamMember.objects.create(business=self.biz, user=viewer, role='viewer', is_active=True)
        TeamMember.objects.create(business=self.target, user=viewer, role='admin', is_active=True)
        with self.assertRaises(rm.MoveRefused):
            rm.find_target(viewer, self.biz, self.target.pk)


# ─────────────────────────── a sync stuck in "running" (the Hap Ax3 case) ───────────────────────────
class StuckLinkSync(Base):
    def setUp(self):
        super().setUp()
        from core.models import RouterSyncJob
        self.a.connection_mode = 'agent'
        self.a.save(update_fields=['connection_mode'])
        RouterAgent.objects.create(router=self.a, token_hash='9' * 64, enrolled_at=timezone.now(), last_seen_at=timezone.now())
        self.job = RouterSyncJob.objects.create(business=self.biz, router=self.a, status='running', progress=5,
                                                phase='Waiting for TapTap Link — collecting RouterOS inventory')
        old = timezone.now() - timedelta(hours=8)
        RouterSyncJob.objects.filter(pk=self.job.pk).update(created_at=old, updated_at=timezone.now())   # touched recently

    def test_overlong_sync_is_stopped_even_if_recently_touched(self):
        from core.agent_inventory import settle_stalled_jobs
        settle_stalled_jobs()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'failed')

    def test_a_sync_that_cannot_be_settled_is_stopped_and_the_router_syncs_again(self):
        from core.agent_inventory import settle_router
        from core.tasks import enqueue_router_sync
        with mock.patch('core.agent_inventory.job_pieces', side_effect=RuntimeError('bad piece')):
            settle_router(self.a)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'failed')
        self.assertIn('could not be finished', self.job.phase)
        with mock.patch('core.tasks.sync_router_task.delay', return_value=mock.Mock(id='task-1')):
            new, created = enqueue_router_sync(self.a)
        self.assertTrue(created)
        self.assertNotEqual(new.pk, self.job.pk)

    def test_one_bad_job_does_not_block_other_routers(self):
        from core.agent_inventory import settle_stalled_jobs
        from core.models import RouterSyncJob
        self.b.connection_mode = 'agent'
        self.b.save(update_fields=['connection_mode'])
        other = RouterSyncJob.objects.create(business=self.biz, router=self.b, status='running')
        RouterSyncJob.objects.filter(pk=other.pk).update(created_at=timezone.now() - timedelta(hours=3))
        real = __import__('core.agent_inventory', fromlist=['job_pieces']).job_pieces
        with mock.patch('core.agent_inventory.job_pieces', side_effect=lambda r, j: (_ for _ in ()).throw(RuntimeError('x'))
                        if j == self.job.pk else real(r, j)):
            settle_stalled_jobs()
        other.refresh_from_db()
        self.assertEqual(other.status, 'failed')
