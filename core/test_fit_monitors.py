"""Fit monitors: slots-full refusals (tell / free an idle slot / email the member), state watches, Fit proposals, the page."""
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import device_lock, fit_actions, fit_monitors, fit_tools
from .models import Business, Router, Voucher, VoucherDeviceBinding
from .models_events import EventAlert
from .models_fit import FitMonitor, FitMonitorEvent, FitSettings
from .permissions import ALL_PERMISSIONS

OLD = 'AA:00:00:00:00:01'
NEW = 'BB:00:00:00:00:02'


def biz(email, name):
    u = User.objects.create_user(email, email, 'Pass-word-123', first_name=name.split()[0])
    b = Business.objects.create(user=u, business_name=name, owner_name=name, phone='220 123',
                                trial_ends_at=timezone.now() + timedelta(days=30), is_unlimited=True)
    return u, b


class MonitorBase(TestCase):
    def setUp(self):
        cache.clear()
        self.owner, self.b = biz('o@example.com', 'Alpha WiFi')
        self.gold = self.voucher('GOLD0001', 'Gold')
        self.bronze = self.voucher('BRNZ0001', 'Bronze')

    def voucher(self, code, plan, devices=1, router=None, **kw):
        v = Voucher.objects.create(business=self.b, code=code, plan_name=plan, price=Decimal('50'), duration_minutes=1440,
                                   max_devices=devices, router=router, **kw)
        return v

    def lock(self, v, mac=OLD, idle_minutes=120, slot=1):
        b = VoucherDeviceBinding.objects.create(business=self.b, voucher=v, slot_no=slot, current_mac=mac, label='Old phone')
        VoucherDeviceBinding.objects.filter(pk=b.pk).update(last_seen_at=timezone.now() - timedelta(minutes=idle_minutes))
        return b

    def monitor(self, kind='slot_refused', **kw):
        kw.setdefault('params', {})
        return FitMonitor.objects.create(business=self.b, created_by=self.owner, name=kind, kind=kind, **kw)

    def claim(self, v, mac=NEW):
        v.refresh_from_db()
        return device_lock.claim(v, mac=mac, source='portal')


class SlotRefusedTests(MonitorBase):
    def test_installed_on_claim(self):
        self.assertTrue(getattr(device_lock.claim, '_taptap_fit_monitors', False))

    def test_notify_only(self):
        self.lock(self.gold)
        self.monitor(params={'plans': ['Gold']})
        out = self.claim(self.gold)
        self.assertEqual(out.status, 'denied')
        ev = FitMonitorEvent.objects.get()
        self.assertEqual((ev.voucher_code, ev.action, ev.mac), ('GOLD0001', 'notified', NEW))
        alert = EventAlert.objects.get(kind='fit_monitor')
        self.assertEqual(alert.user, self.owner)                         # audience "me"
        self.assertIn('slot', alert.title)
        self.assertEqual(VoucherDeviceBinding.objects.filter(voucher=self.gold).count(), 1)   # nothing changed

    def test_other_plans_are_ignored(self):
        self.lock(self.bronze)
        self.monitor(params={'plans': ['Gold']})
        self.assertEqual(self.claim(self.bronze).status, 'denied')
        self.assertFalse(FitMonitorEvent.objects.exists())

    def test_free_idle_slot_lets_the_new_device_in(self):
        old = self.lock(self.gold, idle_minutes=120)
        self.monitor(params={'plans': ['Gold'], 'min_idle_minutes': 20}, response='free_slot')
        out = self.claim(self.gold)
        self.assertTrue(out.allowed, out)
        self.assertFalse(VoucherDeviceBinding.objects.filter(pk=old.pk).exists())
        self.assertEqual(list(self.gold.device_bindings.values_list('current_mac', flat=True)), [NEW])
        self.assertEqual(FitMonitorEvent.objects.get().action, 'admitted')

    def test_never_removes_a_device_that_is_online(self):
        router = Router.objects.create(business=self.b, name='Tower', ip_address='1.1.1.1', username='a', password='b')
        v = self.voucher('GOLD0002', 'Gold', router=router)
        self.lock(v, idle_minutes=600)
        cache.set(f'tt:tr:users:{router.pk}', {'users': {'GOLD0002': [{'mac': OLD, 'down_bps': 90000}]}}, 600)
        self.monitor(params={'plans': ['Gold']}, response='free_slot')
        out = self.claim(v)
        self.assertEqual(out.status, 'denied')
        self.assertEqual(v.device_bindings.count(), 1)
        self.assertEqual(FitMonitorEvent.objects.get().action, 'nothing_idle')

    def test_recently_used_device_is_kept(self):
        self.lock(self.gold, idle_minutes=5)
        self.monitor(params={'min_idle_minutes': 20}, response='free_slot')
        self.assertEqual(self.claim(self.gold).status, 'denied')
        self.assertEqual(FitMonitorEvent.objects.get().action, 'nothing_idle')

    def test_cooldown_tells_once_per_device(self):
        self.lock(self.gold)
        self.monitor()
        self.claim(self.gold); self.claim(self.gold)
        self.assertEqual(FitMonitorEvent.objects.count(), 1)
        self.claim(self.gold, mac='CC:00:00:00:00:03')              # a different phone is a new event
        self.assertEqual(FitMonitorEvent.objects.count(), 2)

    def test_member_is_emailed(self):
        from .models_member_plans import MemberNotificationSettings
        m = self.voucher('anna', 'Member Monthly', login_type='member')
        MemberNotificationSettings.objects.create(member=m, email='anna@example.com')
        self.lock(m)
        self.monitor(params={'members_only': True}, notify_member=True)
        with mock.patch.object(fit_monitors, '_send_member') as send:
            self.claim(m)
        to, subject, text = send.call_args.args
        self.assertEqual(to, 'anna@example.com')
        self.assertIn('anna', subject)
        self.assertIn('Alpha WiFi', text)
        self.assertTrue(FitMonitorEvent.objects.get().member_emailed)

    def test_email_to_team_goes_through_notifications(self):
        self.lock(self.gold)
        self.monitor(email=True)
        with mock.patch('core.notify.notify') as notify:
            self.claim(self.gold)
        self.assertEqual(notify.call_args.args[1], 'fit_monitor')

    def test_paused_or_ended_monitors_do_nothing(self):
        self.lock(self.gold)
        self.monitor(active=False)
        self.monitor(expires_at=timezone.now() - timedelta(minutes=1))
        self.claim(self.gold)
        self.assertFalse(FitMonitorEvent.objects.exists())

    def test_other_business_monitor_never_sees_it(self):
        _, other = biz('x@example.com', 'Bravo Net')
        FitMonitor.objects.create(business=other, name='x', kind='slot_refused', params={})
        self.lock(self.gold)
        self.claim(self.gold)
        self.assertFalse(FitMonitorEvent.objects.exists())

    def test_last_slot_taken(self):
        self.monitor('slot_filled', params={'plans': ['Gold']})
        self.assertEqual(self.claim(self.gold).status, 'new')
        self.assertIn('now full', FitMonitorEvent.objects.get().title)

    def test_refusals_are_counted_for_the_report(self):
        self.lock(self.gold)
        self.claim(self.gold)
        r = fit_monitors.slots_report(self.b, 'Gold, Nope')
        self.assertEqual(r['plans'][0]['full'], 1)
        self.assertEqual(r['plans'][0]['refused_today'], 1)
        self.assertEqual(r['plans'][0]['full_codes'], ['GOLD0001'])
        self.assertEqual(r['not_found_or_no_active'], ['Nope'])


class StateMonitorTests(MonitorBase):
    def setUp(self):
        super().setUp()
        self.router = Router.objects.create(business=self.b, name='Tower', ip_address='1.1.1.1', username='a', password='b', status='Online')

    def users(self, users):
        cache.set(f'tt:tr:users:{self.router.pk}', {'users': users}, 600)

    def test_voucher_online_once(self):
        m = self.monitor('voucher_online', params={'codes': ['GOLD0001'], 'once': True})
        self.users({})
        self.assertEqual(fit_monitors.evaluate(self.b, force=True), 0)
        self.users({'GOLD0001': [{'mac': OLD}]})
        self.assertEqual(fit_monitors.evaluate(self.b, force=True), 1)
        m.refresh_from_db()
        self.assertFalse(m.active)

    def test_state_fires_once_until_it_clears(self):
        self.monitor('voucher_online', params={'codes': ['GOLD0001']})
        self.users({'GOLD0001': [{'mac': OLD}]})
        fit_monitors.evaluate(self.b, force=True)
        fit_monitors.evaluate(self.b, force=True)
        self.assertEqual(FitMonitorEvent.objects.count(), 1)
        self.users({}); fit_monitors.evaluate(self.b, force=True)
        self.users({'GOLD0001': [{'mac': OLD}]}); fit_monitors.evaluate(self.b, force=True)
        self.assertEqual(FitMonitorEvent.objects.count(), 2)

    def test_online_drop(self):
        self.monitor('online_drop', params={'threshold': 50, 'min_users': 4})
        self.users({f'U{i}': [{'mac': f'AA:00:00:00:00:{i:02d}'}] for i in range(10)})
        fit_monitors.evaluate(self.b, force=True)
        self.users({'U1': [{'mac': OLD}], 'U2': [{'mac': NEW}]})
        self.assertEqual(fit_monitors.evaluate(self.b, force=True), 1)
        self.assertIn('fell from 10 to 2', FitMonitorEvent.objects.get().title)

    def test_heavy_user_needs_the_full_duration(self):
        m = self.monitor('heavy_user', params={'threshold': 10, 'minutes': 5})
        self.users({'GOLD0001': [{'mac': OLD, 'down_bps': 25_000_000}]})
        self.assertEqual(fit_monitors.evaluate(self.b, force=True), 0)       # just started
        m.refresh_from_db()
        m.state['heavy_since']['GOLD0001'] -= 6 * 60
        FitMonitor.objects.filter(pk=m.pk).update(state=m.state)
        self.assertEqual(fit_monitors.evaluate(self.b, force=True), 1)

    def test_sharing_disconnects_only_unlocked_devices(self):
        v = self.voucher('SHARE001', 'Gold', router=self.router)
        self.lock(v, mac=OLD)
        self.monitor('sharing', response='disconnect')
        self.users({'SHARE001': [{'mac': OLD}, {'mac': NEW}]})
        with mock.patch('core.device_lock.kick') as kick:
            fit_monitors.evaluate(self.b, force=True)
        self.assertEqual([c.args[2] for c in kick.call_args_list], [NEW])
        self.assertTrue(FitMonitorEvent.objects.filter(action='disconnected').exists())

    def test_idle_slot_sweep(self):
        v = self.voucher('IDLE0001', 'Gold')
        self.lock(v, idle_minutes=5 * 24 * 60)
        fresh = self.voucher('FRSH0001', 'Gold')
        self.lock(fresh, idle_minutes=10)
        self.monitor('idle_slots', params={'days': 3, 'only_when_full': True})
        self.assertEqual(fit_monitors.evaluate(self.b, force=True), 1)
        self.assertEqual(v.device_bindings.count(), 0)
        self.assertEqual(fresh.device_bindings.count(), 1)

    def test_router_offline(self):
        Router.objects.filter(pk=self.router.pk).update(status='Offline', last_watch_at=timezone.now() - timedelta(minutes=30))
        self.monitor('router_offline', params={'minutes': 5})
        self.assertEqual(fit_monitors.evaluate(self.b, force=True), 1)

    def test_dry_run_changes_nothing(self):
        self.lock(self.gold, idle_minutes=500)
        m = self.monitor(params={'plans': ['Gold']}, response='free_slot')
        r = fit_monitors.dry_run(m)
        self.assertEqual(r['full_now'], 1)
        self.assertEqual(r['examples'][0]['idle_slot_to_free'], 'Old phone')
        self.assertEqual(self.gold.device_bindings.count(), 1)
        self.assertFalse(FitMonitorEvent.objects.exists())


class FitProposalTests(MonitorBase):
    def ctx(self):
        s, _ = FitSettings.objects.get_or_create(business=self.b, defaults={'allowed': ['monitors', 'devices']})
        return fit_tools.FitContext(business=self.b, user=self.owner, perms=ALL_PERMISSIONS, settings=s, role_label='Owner')

    def test_fit_proposes_and_owner_approves(self):
        ctx = self.ctx()
        card = fit_actions.propose(ctx, 'create_monitor', {'monitor_kind': 'slot_refused', 'plans': 'gold, bronze',
                                                           'response': 'free_slot', 'notify_member': True, 'hours': 48})
        self.assertFalse(FitMonitor.objects.exists())                          # nothing until approved
        self.assertIn('Free one idle slot', card['detail'])
        from .models_fit import FitAction
        ok, msg = fit_actions.run(FitAction.objects.get(pk=card['id']), self.owner, ALL_PERMISSIONS, ctx.settings)
        self.assertTrue(ok, msg)
        m = FitMonitor.objects.get()
        self.assertEqual(m.params['plans'], ['Gold', 'Bronze'])                # real plan names
        self.assertTrue(m.notify_member and m.expires_at)

    def test_unknown_plan_is_refused(self):
        with self.assertRaises(fit_actions.FitActionError) as e:
            fit_actions.propose(self.ctx(), 'create_monitor', {'monitor_kind': 'slot_refused', 'plans': 'Platinum'})
        self.assertIn('Gold', str(e.exception))

    def test_response_must_fit_the_kind(self):
        with self.assertRaises(fit_actions.FitActionError):
            fit_actions.propose(self.ctx(), 'create_monitor', {'monitor_kind': 'heavy_user', 'response': 'free_slot'})

    def test_tools(self):
        ctx = self.ctx()
        self.lock(self.gold)
        m = self.monitor(params={'plans': ['Gold']})
        self.assertEqual(json.loads(fit_tools.call(ctx, 'slots_report', {'plans': 'Gold'}))['plans'][0]['full'], 1)
        self.assertEqual(json.loads(fit_tools.call(ctx, 'monitors', {}))['monitors'][0]['id'], m.pk)
        self.assertEqual(json.loads(fit_tools.call(ctx, 'monitor_test', {'monitor_id': m.pk}))['dry_run']['full_now'], 1)

    def test_free_idle_slot_action(self):
        ctx = self.ctx()
        self.lock(self.gold, idle_minutes=300)
        card = fit_actions.propose(ctx, 'free_idle_slot', {'code': 'GOLD0001'})
        from .models_fit import FitAction
        ok, msg = fit_actions.run(FitAction.objects.get(pk=card['id']), self.owner, ALL_PERMISSIONS, ctx.settings)
        self.assertTrue(ok, msg)
        self.assertEqual(self.gold.device_bindings.count(), 0)


class PageTests(MonitorBase):
    def test_owner_creates_tests_pauses_from_the_page(self):
        self.client.force_login(self.owner)
        r = self.client.post(reverse('fit_monitors_page'), {'monitor_kind': 'slot_refused', 'plans': 'Gold', 'response': 'notify'})
        self.assertEqual(r.status_code, 302)
        m = FitMonitor.objects.get()
        self.assertEqual(m.source, 'fit')
        page = self.client.get(reverse('fit_monitors_page'))
        self.assertContains(page, 'Device refused')
        self.client.post(reverse('fit_monitor_toggle', args=[m.pk]), {'op': 'test'})
        self.client.post(reverse('fit_monitor_toggle', args=[m.pk]), {'op': 'pause'})
        m.refresh_from_db()
        self.assertFalse(m.active)
