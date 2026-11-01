"""TapTap's own router protection must never block TapTap — and the 10-second check must never be what triggers it."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from core import router_pulse, self_trust
from core.models import Business, Router


class FakeRes:
    def __init__(self, rows):
        self.rows = rows; self.added = []; self.removed = []
    def get(self, **kw):
        return [r for r in self.rows if all(str(r.get(k)) == str(v) for k, v in kw.items())]
    def add(self, **kw):
        self.added.append(kw)
    def remove(self, id):
        self.removed.append(id)


class FakeSvc:
    def __init__(self, filt=(), active=(), alist=()):
        self.res = {'/ip/firewall/filter': FakeRes(list(filt)), '/user/active': FakeRes(list(active)), '/ip/firewall/address-list': FakeRes(list(alist))}
    def resource(self, path):
        return self.res[path]


class Base(TestCase):
    def setUp(self):
        cache.clear()
        u = User.objects.create_user('o', password='x')
        self.biz = Business.objects.create(user=u, business_name='B', owner_name='O', phone='1', live_sync=True,
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.router = Router.objects.create(business=self.biz, name='Shop', ip_address='10.0.0.1', username='taptap', status='Online')


class Ensure(Base):
    def test_learns_taptaps_real_address_and_trusts_it(self):
        svc = FakeSvc(filt=[{'chain': 'taptap-ips'}],
                      active=[{'via': 'api', 'name': 'taptap', 'address': '203.0.113.9'}, {'via': 'winbox', 'name': 'admin', 'address': '10.0.0.5'}],
                      alist=[{'id': '*1', 'list': 'taptap-trusted', 'address': '198.51.100.1'},      # SITE_URL's address (a proxy)
                             {'id': '*2', 'list': 'taptap-bf-2', 'address': '203.0.113.9'}])          # half-way to being blocked
        self.assertEqual(self_trust.ensure(svc, self.router), 'trusted')
        alist = svc.res['/ip/firewall/address-list']
        self.assertEqual(alist.added[0]['address'], '203.0.113.9')
        self.assertEqual(alist.added[0]['list'], 'taptap-trusted')
        self.assertEqual(alist.removed, ['*2'], 'taken off the brute-force stage list at once')
        self.assertIn('203.0.113.9', self_trust.learned())

    def test_router_without_taptap_protection(self):
        svc = FakeSvc(active=[{'via': 'api', 'name': 'taptap', 'address': '203.0.113.9'}])
        self.assertEqual(self_trust.ensure(svc, self.router), 'none')
        self.assertEqual(svc.res['/ip/firewall/address-list'].added, [])

    def test_unknown_address_on_a_protected_router(self):
        svc = FakeSvc(filt=[{'chain': 'taptap-ddos'}], active=[])
        self.assertEqual(self_trust.ensure(svc, self.router), 'unknown')

    def test_checked_at_most_every_ten_minutes(self):
        svc = FakeSvc(active=[{'via': 'api', 'name': 'taptap', 'address': '203.0.113.9'}])
        self_trust.ensure(svc, self.router)
        with mock.patch.object(svc, 'resource', side_effect=AssertionError('read again')):
            self.assertEqual(self_trust.ensure(svc, self.router), 'none')

    def test_protection_trusts_the_learned_address_too(self):
        from core.protection import trusted_addresses
        self_trust._remember('203.0.113.9')
        self.assertIn('203.0.113.9', trusted_addresses())

    def test_live_check_confirms_trust_after_connecting(self):
        from core import live
        svc = mock.MagicMock(); svc.hotspot_users.return_value = []; svc.active_users.return_value = []
        with mock.patch('core.live.MikroTikService') as cls, mock.patch('core.self_trust.ensure') as ensure:
            cls.return_value.connect.return_value = svc
            live.watch_router(self.router, force=True)
        ensure.assert_called_once()


class KnockGating(Base):
    def test_no_knock_until_trust_is_known(self):
        with mock.patch('core.router_pulse.knock') as knock:
            router_pulse.pulse()
        knock.assert_not_called()
        cache.set(f'tt:selftrust:state:{self.router.pk}', 'unknown', 60)
        with mock.patch('core.router_pulse.knock') as knock:
            router_pulse.pulse()
        knock.assert_not_called()

    def test_knock_when_safe(self):
        for st in ('none', 'trusted'):
            cache.set(f'tt:selftrust:state:{self.router.pk}', st, 60)
            with mock.patch('core.router_pulse.knock', return_value=True) as knock:
                router_pulse.pulse()
            knock.assert_called_once()

    def test_no_extra_connection_when_the_live_check_just_confirmed_it(self):
        cache.set(f'tt:selftrust:state:{self.router.pk}', 'none', 60)
        Router.objects.filter(pk=self.router.pk).update(last_watch_at=timezone.now() - timedelta(seconds=5))
        with mock.patch('core.router_pulse.knock') as knock:
            router_pulse.pulse()
        knock.assert_not_called()
