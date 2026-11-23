"""A customer whose voucher ran out buys a new one and signs in again: the clean-up of the OLD voucher must never
throw the phone off its NEW session.

What people saw: "when their voucher time ends and they buy another, the portal doesn't show to enter the voucher —
it shows them they are logged in without internet". The expiry clean-up frees the old voucher's phones (removes
their hotspot host and drops them from MikroTik Wi-Fi so the login page comes up). It runs again a minute later
("free its phones once more"), every few days while the voucher waits for archiving, and over TapTap Link whenever
the router next checks in — by then the same phone was signed in with the new voucher, so the clean-up ended that
new session: the page still said "connected", but there was no internet and no login page.
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from core import agent, expiry
from core.mikrotik import MikroTikService
from core.models import AgentCommand, Business, Router, Voucher, VoucherDeviceBinding

PHONE = 'A4:83:E7:1C:0F:2B'
OTHER = '11:22:33:44:55:66'
CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'expiry-new-voucher'}}


class FakeRes:
    def __init__(self, rows):
        self.rows = rows
        self.removed = []

    def get(self, **where):
        out = []
        for r in self.rows:
            if all(str(r.get(k.replace('_', '-'), '')) == str(v) for k, v in where.items()):
                out.append(r)
        return out

    def remove(self, id):
        self.removed.append(id)
        self.rows = [r for r in self.rows if r['id'] != id]


class FakeRouter:
    """Just enough of /ip/hotspot and the Wi-Fi tables for MikroTikService.release_devices."""

    def __init__(self, active=(), cookies=(), hosts=(), wifi=()):
        self.res = {
            '/ip/hotspot/active': FakeRes(list(active)),
            '/ip/hotspot/cookie': FakeRes(list(cookies)),
            '/ip/hotspot/host': FakeRes(list(hosts)),
            '/interface/wireless/registration-table': FakeRes(list(wifi)),
        }

    def resource(self, path):
        if path not in self.res:
            raise RuntimeError('no such menu')
        return self.res[path]


def service(fake):
    svc = MikroTikService.__new__(MikroTikService)
    svc.resource = fake.resource
    return svc


class ReleaseDevicesTests(SimpleTestCase):
    def test_phone_signed_in_with_a_new_voucher_is_left_alone(self):
        fake = FakeRouter(
            active=[{'id': '*A1', 'user': 'NEW123', 'mac-address': PHONE}],
            hosts=[{'id': '*H1', 'mac-address': PHONE}, {'id': '*H2', 'mac-address': OTHER}],
            wifi=[{'id': '*W1', 'mac-address': PHONE}, {'id': '*W2', 'mac-address': OTHER}],
        )
        dropped = service(fake).release_devices('OLD999', [PHONE, OTHER])
        self.assertEqual(fake.res['/ip/hotspot/active'].removed, [])            # the new session stays
        self.assertEqual(fake.res['/ip/hotspot/host'].removed, ['*H2'])           # only the idle phone is freed
        self.assertEqual(fake.res['/interface/wireless/registration-table'].removed, ['*W2'])
        self.assertEqual(dropped, 1)

    def test_the_expired_vouchers_own_phone_is_still_freed(self):
        fake = FakeRouter(
            active=[{'id': '*A1', 'user': 'OLD999', 'mac-address': PHONE}],
            cookies=[{'id': '*C1', 'user': 'OLD999', 'mac-address': PHONE}],
            hosts=[{'id': '*H1', 'mac-address': PHONE}],
            wifi=[{'id': '*W1', 'mac-address': PHONE}],
        )
        dropped = service(fake).release_devices('OLD999', [])
        self.assertEqual(fake.res['/ip/hotspot/active'].removed, ['*A1'])
        self.assertEqual(fake.res['/ip/hotspot/cookie'].removed, ['*C1'])
        self.assertEqual(fake.res['/ip/hotspot/host'].removed, ['*H1'])
        self.assertEqual(dropped, 1)


class LinkScriptTests(SimpleTestCase):
    def test_drop_checks_for_a_live_session_before_freeing_a_phone(self):
        d = agent.DROP_DEVICE
        self.assertIn('/ip hotspot active find mac-address=$m', d)
        self.assertLess(d.index('active find mac-address=$m'), d.index('host remove'))
        self.assertLess(d.index('$busy = 0'), d.index('registration-table'))
        self.assertEqual(d.count('{'), d.count('}'))                             # still one well-formed script

    def test_expired_batch_uses_the_guarded_drop(self):
        cmd = AgentCommand(kind='hotspot_users_disable',
                           params={'names': ['OLD999'], 'disabled': True, 'reason': 'expired', 'macs': [PHONE]})
        body = agent.command_body(cmd)
        self.assertTrue(body.startswith(agent.DROP_DEVICE))
        # the voucher's own sessions are removed first, so only someone else's session can still hold the phone
        self.assertLess(body.index('/ip hotspot active remove $a'), body.index(f'$drop m="{PHONE}"'))


@override_settings(CACHES=CACHE)
class ExpirySweepTests(TestCase):
    def setUp(self):
        cache.clear()
        owner = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456')
        self.biz = Business.objects.create(user=owner, business_name='Shop', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.router = Router.objects.create(business=self.biz, name='R1', ip_address='10.0.0.1', username='a', password='p')

    def test_second_clean_up_pass_does_not_cut_the_new_voucher(self):
        now = timezone.now()
        old = Voucher.objects.create(business=self.biz, router=self.router, code='OLD999', status='expired',
                                     expires_at=now - timedelta(minutes=3), duration_minutes=60)
        Voucher.objects.create(business=self.biz, router=self.router, code='NEW123', status='active',
                               expires_at=now + timedelta(hours=1), duration_minutes=60)
        VoucherDeviceBinding.objects.create(business=self.biz, voucher=old, current_mac=PHONE)
        fake = FakeRouter(active=[{'id': '*A1', 'user': 'NEW123', 'mac-address': PHONE}],
                          hosts=[{'id': '*H1', 'mac-address': PHONE}],
                          wifi=[{'id': '*W1', 'mac-address': PHONE}])
        svc = service(fake)
        svc.disable_voucher = mock.Mock()
        users = [{'name': 'OLD999', 'disabled': 'true'}, {'name': 'NEW123', 'disabled': 'false'}]
        active = [{'user': 'NEW123', 'mac-address': PHONE}]
        with mock.patch('core.router_time.repair_api'):
            expiry.enforce_on_router(self.router, svc, users, active, now, fresh=False)
        self.assertEqual(fake.res['/ip/hotspot/active'].removed, [])
        self.assertEqual(fake.res['/ip/hotspot/host'].removed, [])
        self.assertEqual(fake.res['/interface/wireless/registration-table'].removed, [])
