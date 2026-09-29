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
