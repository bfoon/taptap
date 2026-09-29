"""Fair usage must not miss: usage is counted after late syncs, caps are checked against the
router every time, kept above hotspot queues and out of FastTrack."""
from datetime import datetime, time, timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from . import fair_usage as fu
from .models import Business, Router, UsageRecord, Voucher, VoucherPlan
from .models_fup import FairUsagePolicy
from .traffic import collect_sessions

GB = 1024 ** 3


class FakeRes:
    def __init__(self, rows=None): self.rows = rows or []; self.calls = []
    def get(self, **k): return [r for r in self.rows if all(str(r.get(x.replace('_', '-'))) == str(v) for x, v in k.items())]
    def add(self, **k):
        r = {kk.replace('_', '-'): v for kk, v in k.items()}; r['id'] = f'*{len(self.rows) + 10}'; self.rows.insert(0, r); self.calls.append(('add', r)); return r['id']
    def set(self, id, **k):
        for r in self.rows:
            if r['id'] == id: r.update({kk.replace('_', '-'): v for kk, v in k.items()})
        self.calls.append(('set', id))
    def remove(self, id): self.rows = [r for r in self.rows if r['id'] != id]; self.calls.append(('remove', id))
    def call(self, cmd, args): self.calls.append((cmd, args))


class FakeSvc:
    def __init__(self, queues=None, filters=None):
        self.res = {'/queue/simple': FakeRes(queues), '/ip/firewall/address-list': FakeRes(), '/ip/firewall/filter': FakeRes(filters)}
    def resource(self, p): return self.res[p]


class StrictTests(TestCase):
    def setUp(self):
        cache.clear()
        self.now = timezone.make_aware(datetime.combine(timezone.localdate(), time(13, 30)))
        owner = User.objects.create_user('st@example.com', 'st@example.com', 'pw')
        self.biz = Business.objects.create(user=owner, business_name='B', owner_name='O', phone='1', trial_ends_at=self.now + timedelta(days=7))
        self.router = Router.objects.create(business=self.biz, name='R', ip_address='10.0.0.1', username='u', password='p')
        VoucherPlan.objects.create(business=self.biz, name='Day', price=40, duration_minutes=1440)
        self.v = Voucher.objects.create(business=self.biz, router=self.router, code='ONE1', plan_name='Day', duration_minutes=1440,
                                        max_devices=1, used_at=self.now - timedelta(hours=2))
        FairUsagePolicy.objects.create(business=self.biz, name='Daily', period='day', counts='total', tiers=[{'gb': 1, 'down': 2, 'up': 1}])
        UsageRecord.objects.create(business=self.biz, router=self.router, username='ONE1', mac_address='AA:00:00:00:00:01',
                                   hour=self.now.replace(minute=0, second=0, microsecond=0), download=int(1.5 * GB))
        self.active = [{'user': 'ONE1', 'address': '10.5.50.7', 'mac-address': 'AA:00:00:00:00:01'}]

    def test_single_device_voucher_is_capped_as_one(self):
        caps = fu.desired_caps(self.router, self.active, self.now)
        self.assertEqual([c[0] for c in caps.values()], ['10.5.50.7'])

    def test_cap_deleted_on_router_is_put_back(self):
        svc = FakeSvc(queues=[{'id': '*1', 'name': '<hotspot-ONE1>', 'target': '10.5.50.7/32'}],
                      filters=[{'id': '*F', 'action': 'fasttrack-connection', 'disabled': 'false'}])
        fu.enforce(self.router, self.active, self.now, svc=svc)
        q = svc.res['/queue/simple']
        self.assertTrue(any(r['name'].startswith('TTFUP-') for r in q.rows))
        # someone deletes it on the router: the next run adds it again (no trust in TapTap's memory)
        q.rows = [r for r in q.rows if not r['name'].startswith('TTFUP-')]
        fu.enforce(self.router, self.active, self.now, svc=svc)
        self.assertTrue(any(r['name'].startswith('TTFUP-') for r in q.rows))
        # capped device is kept out of FastTrack
        self.assertEqual([r['address'] for r in svc.res['/ip/firewall/address-list'].rows], ['10.5.50.7'])
        self.assertEqual(len([r for r in svc.res['/ip/firewall/filter'].rows if 'no fasttrack' in str(r.get('comment'))]), 2)

    def test_usage_counted_after_a_late_sync(self):
        s = {'id': '*A', 'user': 'ONE1', 'mac-address': 'AA:00:00:00:00:01', 'address': '10.5.50.7', 'bytes-in': 0, 'bytes-out': 100, 'uptime': '1m'}
        collect_sessions(self.router, [s], self.now)
        s2 = {**s, 'bytes-out': 100 + 500 * 1024 * 1024, 'uptime': '2h'}
        collect_sessions(self.router, [s2], self.now + timedelta(hours=2))        # 2 h later — long gap
        total = sum(UsageRecord.objects.filter(username='ONE1', mac_address='AA:00:00:00:00:01').values_list('download', flat=True))
        self.assertGreaterEqual(total - int(1.5 * GB), 500 * 1024 * 1024)

    def test_link_router_gets_full_set_again(self):
        from unittest import mock
        with mock.patch('core.fair_usage._apply_link') as m:
            fu.enforce(self.router, self.active, self.now)
            fu.enforce(self.router, self.active, self.now)           # unchanged, inside 5 min: nothing sent
            self.assertEqual(m.call_count, 1)
            cache.delete(f'tt:fup:full:{self.router.pk}')           # 5 minutes later
            fu.enforce(self.router, self.active, self.now)
            self.assertEqual(m.call_count, 2)


class BypassFairUsageTests(TestCase):
    """IP-binding bypass devices are measured from the hotspot host counters and capped per device."""
    def setUp(self):
        cache.clear()
        self.now = timezone.make_aware(datetime.combine(timezone.localdate(), time(13, 30)))
        owner = User.objects.create_user('bp@example.com', 'bp@example.com', 'pw')
        self.biz = Business.objects.create(user=owner, business_name='B', owner_name='O', phone='1', trial_ends_at=self.now + timedelta(days=7))
        self.router = Router.objects.create(business=self.biz, name='R', ip_address='10.0.0.1', username='u', password='p')
        from .models import SyncedIPBinding
        SyncedIPBinding.objects.create(business=self.biz, router=self.router, mikrotik_id='*5', mac_address='BB:00:00:00:00:01',
                                       binding_type='bypassed', disabled=False, comment='Tenant 19', is_present=True)
        self.pol = FairUsagePolicy.objects.create(business=self.biz, name='Daily', period='day', counts='total', bypass=True,
                                                  tiers=[{'gb': 1, 'down': 2, 'up': 1}])
        self.hosts = [{'id': '*H1', 'mac-address': 'BB:00:00:00:00:01', 'address': '10.5.50.99', 'bypassed': 'true', 'bytes-in': 0, 'bytes-out': 0},
                      {'id': '*H2', 'mac-address': 'CC:00:00:00:00:02', 'address': '10.5.50.3', 'bypassed': 'false', 'bytes-in': 0, 'bytes-out': 0}]

    def test_bypass_device_counted_and_capped(self):
        rows = fu.bypass_rows(self.hosts)
        self.assertEqual([r['user'] for r in rows], ['BYPASS:BB:00:00:00:00:01'])      # only bypassed hosts
        collect_sessions(self.router, rows, self.now)
        grown = [{**rows[0], 'bytes-out': int(1.2 * GB)}]
        collect_sessions(self.router, grown, self.now + timedelta(minutes=2))
        caps = fu.desired_caps(self.router, grown, self.now + timedelta(minutes=2))
        self.assertEqual(len(caps), 1)
        name, cap = next(iter(caps.items()))
        self.assertTrue(name.startswith('TTFUP-BPBB0000000001-'))
        self.assertEqual((cap[0], cap[1]), ('10.5.50.99', '1000k/2000k'))
        self.assertEqual(fu.slowed_bypass(self.biz)[0]['name'], 'Tenant 19')
        st = fu.bypass_status(self.biz, ['BB:00:00:00:00:01'], self.now + timedelta(minutes=2))
        self.assertEqual(st['BB:00:00:00:00:01']['tier'], 1)

    def test_not_capped_when_policy_does_not_cover_bypass(self):
        self.pol.bypass = False; self.pol.save()
        rows = fu.bypass_rows(self.hosts)
        from .models import UsageRecord
        UsageRecord.objects.create(business=self.biz, router=self.router, username='BYPASS:BB:00:00:00:00:01', mac_address='BB:00:00:00:00:01',
                                   hour=self.now.replace(minute=0, second=0, microsecond=0), download=5 * GB)
        self.assertEqual(fu.desired_caps(self.router, rows, self.now), {})

    def test_link_heartbeat_reports_bypass_devices(self):
        from .agent import agent_script, parse_bypass
        self.assertIn('/ip hotspot host find where bypassed=yes', agent_script('https://t.example', 'TOK', 'yes'))
        rows = parse_bypass('BB:00:00:00:00:01,10.5.50.99,100,2000,*H1;')
        self.assertEqual((rows[0]['user'], rows[0]['address'], rows[0]['bytes-out']), ('BYPASS:BB:00:00:00:00:01', '10.5.50.99', '2000'))
