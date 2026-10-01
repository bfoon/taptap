"""Slowed page: per-device data, exempt, reset speed, block/unblock and device links for shared vouchers.

Run:  DB_ENGINE=sqlite python manage.py test core.test_fup_devices
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import device_block, fair_usage as fu
from .models import (AgentCommand, Business, DeviceSignature, Router, SyncedIPBinding, UsageRecord, Voucher, VoucherDeviceBinding,
                     VoucherPlan)
from .models_fup import FairUsageExemption, FairUsagePolicy, FairUsageState

A, B = 'AA:BB:CC:00:00:0A', 'AA:BB:CC:00:00:0B'
MB = 1024 ** 2


class FakeRes:
    def __init__(self, rows): self.rows = rows
    def get(self, **f): return [r for r in self.rows if all(str(r.get(k)) == str(v) for k, v in f.items())]
    def remove(self, id): self.rows[:] = [r for r in self.rows if r['id'] != id]


class FakeSvc:
    def __init__(self, store): self.store = store; self.upserts = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def resource(self, path): return FakeRes(self.store.setdefault(path, []))
    def upsert_binding(self, b):
        self.upserts.append((b.mac_address, b.binding_type))
        rows = self.store.setdefault('/ip/hotspot/ip-binding', [])
        row = next((r for r in rows if r['mac-address'] == b.mac_address), None)
        if row is None:
            row = {'id': f'*{len(rows) + 1}', 'mac-address': b.mac_address}; rows.append(row)
        row['type'] = b.binding_type
        return 'updated', row['id']
    def delete_binding(self, item_id): FakeRes(self.store['/ip/hotspot/ip-binding']).remove(item_id)


@override_settings(AUTH_EMAIL_OTP=False)
class SharedDeviceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('f@x.com', 'f@x.com', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='B', owner_name='O', phone='1', trial_ends_at=timezone.now() + timedelta(days=7))
        self.r = Router.objects.create(business=self.biz, name='hAP', ip_address='10.0.0.1', username='u', password='p', status='Online')
        plan = VoucherPlan.objects.create(business=self.biz, name='Family', price=50, duration_minutes=1440 * 7, max_devices=2)
        self.pol = FairUsagePolicy.objects.create(business=self.biz, name='Daily fair use', period='day', counts='total',
                                                  tiers=[{'gb': 1, 'down': 2, 'up': 1}, {'gb': 3, 'down': 1, 'up': 0.5}])
        self.pol.plans.add(plan)
        self.v = Voucher.objects.create(business=self.biz, router=self.r, code='FAM00001', plan_name='Family', max_devices=2,
                                        duration_minutes=1440 * 7, used_at=timezone.now() - timedelta(hours=5))
        VoucherDeviceBinding.objects.create(business=self.biz, voucher=self.v, slot_no=1, current_mac=A, label="Fatou's phone")
        VoucherDeviceBinding.objects.create(business=self.biz, voucher=self.v, slot_no=2, current_mac=B, label='Laptop')
        hour = timezone.now().replace(minute=0, second=0, microsecond=0)
        UsageRecord.objects.create(business=self.biz, router=self.r, username='FAM00001', mac_address=A, hour=hour, download=1400 * MB, upload=136 * MB)
        UsageRecord.objects.create(business=self.biz, router=self.r, username='FAM00001', mac_address=B, hour=hour, download=180 * MB, upload=20 * MB)
        self.active = [{'user': 'FAM00001', 'address': '10.5.50.10', 'mac-address': A}, {'user': 'FAM00001', 'address': '10.5.50.11', 'mac-address': B}]
        self.client.force_login(self.owner)

    def caps(self):
        return {ip for ip, *_ in fu.desired_caps(self.r, self.active).values()}

    # ── data per device and who gets slowed ──
    def test_only_the_heavy_device_is_capped(self):
        self.assertEqual(self.caps(), {'10.5.50.10'})
        rows = fu.slowed(self.biz)
        self.assertEqual(len(rows), 1)                                      # one card for the voucher, not one per device
        devs = {d['key']: d for d in rows[0]['devices']}
        self.assertEqual((devs['slot1']['used_text'], devs['slot1']['tier'], devs['slot1']['cap']), ('1.50 GB', 1, '2 Mb/s'))
        self.assertEqual((devs['slot2']['used_text'], devs['slot2']['tier']), ('200 MB', 0))
        self.assertEqual(rows[0]['slowed_devices'], 1)

    def test_exempt_device_is_never_capped(self):
        fu.exempt_device(self.v, 'slot1', self.owner)
        self.assertEqual(self.caps(), set())
        self.assertTrue(FairUsageExemption.objects.filter(voucher=self.v, device='slot1', macs=[A]).exists())
        d = {x['key']: x for x in fu.card_devices(self.v, self.pol)}['slot1']
        self.assertEqual((d['exempt'], d['tier'], d['next_gb']), (True, 0, None))
        fu.unexempt_device(self.v, 'slot1', self.owner)
        self.assertEqual(self.caps(), {'10.5.50.10'})

    def test_exemption_follows_the_phone_when_its_mac_changes(self):
        fu.exempt_device(self.v, 'slot1', self.owner)
        VoucherDeviceBinding.objects.filter(voucher=self.v, slot_no=1).update(current_mac='DA:00:00:00:00:01', previous_mac=A)
        self.active[0]['mac-address'] = 'DA:00:00:00:00:01'
        self.assertEqual(self.caps(), set())

    def test_reset_speed_for_one_device_only(self):
        self.caps()
        until = fu.lift_device(self.v, 'slot1', self.owner)
        self.assertGreater(until, timezone.now())
        self.assertEqual(self.caps(), set())                                # slot1 at full speed now
        st = FairUsageState.objects.get(voucher=self.v, device='slot1')
        self.assertEqual(st.lifted_until, until)
        UsageRecord.objects.filter(mac_address=B).update(download=2000 * MB)
        self.assertEqual(self.caps(), {'10.5.50.11'})                       # the other device is still judged on its own

    # ── links ──
    def test_device_links(self):
        sig = DeviceSignature.objects.create(business=self.biz, fingerprint='fp1', last_mac=A)
        devs = {d['key']: d for d in fu.card_devices(self.v, self.pol)}
        self.assertEqual(devs['slot1']['url'], reverse('device_detail', args=[sig.pk]))
        self.assertEqual(devs['slot2']['url'], reverse('devices') + f'?q={B}')

    # ── block / unblock ──
    def test_block_and_unblock_over_the_api(self):
        store = {'/ip/hotspot/active': [{'id': '*s', 'user': 'FAM00001', 'mac-address': A}]}
        svc = FakeSvc(store)
        with mock.patch('core.mikrotik.MikroTikService', return_value=svc), mock.patch('core.voucher_history.channel', return_value='Direct API'):
            device_block.block(self.v, [A], "Fatou's phone", self.owner)
            self.assertEqual(svc.upserts, [(A, 'blocked')])
            self.assertEqual(store['/ip/hotspot/active'], [])                   # disconnected now
            self.assertTrue(SyncedIPBinding.objects.filter(mac_address=A, binding_type='blocked', sync_status='Synced').exists())
            self.assertIn(A, device_block.blocked_macs(self.biz))
            self.assertTrue({x['key']: x for x in fu.card_devices(self.v, self.pol)}['slot1']['blocked'])
            self.assertEqual(device_block.unblock(self.v, [A], '', self.owner), 1)
        self.assertEqual(store['/ip/hotspot/ip-binding'], [])
        self.assertFalse(SyncedIPBinding.objects.filter(mac_address=A).exists())

    def test_unblock_puts_back_a_paid_bypass(self):
        SyncedIPBinding.objects.create(business=self.biz, router=self.r, mac_address=A, binding_type='bypassed', comment='Paid: owner laptop', source='taptap')
        svc = FakeSvc({})
        with mock.patch('core.mikrotik.MikroTikService', return_value=svc), mock.patch('core.voucher_history.channel', return_value='Direct API'):
            device_block.block(self.v, [A], '', self.owner)
            device_block.unblock(self.v, [A], '', self.owner)
        b = SyncedIPBinding.objects.get(mac_address=A)
        self.assertEqual((b.binding_type, b.comment), ('bypassed', 'Paid: owner laptop'))
        self.assertEqual(svc.upserts[-1], (A, 'bypassed'))

    def test_block_over_taptap_link(self):
        with mock.patch('core.voucher_history.channel', return_value='TapTap Link'), mock.patch('core.linkops.ensure_online'):
            device_block.block(self.v, [B], 'Laptop', self.owner)
            kinds = list(AgentCommand.objects.order_by('id').values_list('kind', 'params'))
            self.assertEqual([k for k, _ in kinds], ['binding_upsert', 'hotspot_kick'])
            self.assertEqual(kinds[0][1]['type'], 'blocked')
            device_block.unblock(self.v, [B], 'Laptop', self.owner)
        self.assertEqual(AgentCommand.objects.order_by('-id').first().kind, 'binding_remove')

    def test_block_needs_a_mac(self):
        with self.assertRaises(ValueError):
            device_block.block(self.v, [], 'Unknown', self.owner)

    # ── the page and its buttons ──
    def test_page_and_actions(self):
        self.caps()
        r = self.client.get(reverse('fup_slowed'))
        self.assertContains(r, 'Devices on this voucher')
        self.assertContains(r, '1.50 GB'); self.assertContains(r, '200 MB')
        self.assertContains(r, 'value="lift"'); self.assertContains(r, 'value="exempt"'); self.assertContains(r, 'value="block"')
        url = reverse('fup_device', args=[self.v.pk])
        self.client.post(url, {'device': 'slot2', 'action': 'exempt'})
        self.assertTrue(FairUsageExemption.objects.filter(voucher=self.v, device='slot2').exists())
        self.client.post(url, {'device': 'slot1', 'action': 'lift'})
        self.assertIsNotNone(FairUsageState.objects.get(voucher=self.v, device='slot1').lifted_until)

    def test_cashier_cannot_use_device_actions(self):
        from .models_team import TeamMember
        u = User.objects.create_user('c@x.com', 'c@x.com', 'pw')
        TeamMember.objects.create(business=self.biz, user=u, role='voucher_creator')
        self.client.force_login(u)
        self.client.post(reverse('fup_device', args=[self.v.pk]), {'device': 'slot1', 'action': 'exempt'})
        self.assertFalse(FairUsageExemption.objects.exists())
