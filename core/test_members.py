"""Members: username + password logins, paying or free/unlimited, on MikroTik and on the portal.

Run:  DB_ENGINE=sqlite python manage.py test core.test_members
"""
import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import members as mem
from . import voucher_codes as vc
from .agent import command_body
from .models import (Business, PortalPage, Router, Voucher, VoucherCodeAlias, VoucherEvent, VoucherPlan, VoucherSale)

from .mikrotik import MikroTikService as _RealSvc   # the real methods, even while tests patch the name

HTML = {'HTTP_ACCEPT': 'text/html'}


class FakeUsers:
    def __init__(self, rows):
        self.rows = rows

    def get(self, name=None, **kw):
        return [r for r in self.rows if name is None or r['name'] == name]

    def set(self, id, **fields):
        for r in self.rows:
            if r['id'] == id:
                r.update(fields)

    def add(self, **fields):
        fields['id'] = f'*{len(self.rows) + 1}'
        self.rows.append(fields)
        return fields['id']

    def remove(self, id):
        self.rows[:] = [r for r in self.rows if r['id'] != id]


class FakeSvc:
    """Enough of MikroTikService for pushes, password changes and a full sync."""
    def __init__(self, rows=None, profiles=None):
        self.users = FakeUsers(rows if rows is not None else [])
        self.active = FakeUsers([])
        self.profiles = profiles or []
        self.pushed = []

    def connect(self): return self
    def close(self): pass

    def resource(self, path):
        return self.active if path.endswith('/active') else self.users

    # used by push_one / sync
    def ensure_hotspot_profile(self, *a, **k): return '*p'

    def upsert_voucher(self, code, profile, limit_uptime=None, comment='', disabled=False, password=None):
        self.pushed.append({'name': code, 'password': password or code, 'comment': comment})
        return _RealSvc.upsert_voucher(self, code, profile, limit_uptime, comment, disabled, password)

    def set_user_password(self, name, password):
        return _RealSvc.set_user_password(self, name, password)

    def reset_active_by_name(self, code): pass

    # full sync
    def hotspot_profiles(self): return self.profiles
    def hotspot_users(self): return [dict(r) for r in self.users.rows]
    def bindings(self): return []
    def topology_data(self): return {}
    def configuration_snapshot(self): raise RuntimeError('not in tests')


@override_settings(AUTH_EMAIL_OTP=False)
class MemberTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('m@example.com', 'm@example.com', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='Kombo WiFi', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=7))
        self.router = Router.objects.create(business=self.biz, name='R1', ip_address='10.0.0.1', username='u', password='p')
        self.month = VoucherPlan.objects.create(business=self.biz, name='Monthly', price=Decimal('500'),
                                                duration_minutes=43200, duration_unit='months', max_devices=2)
        self.client.force_login(self.owner)

    def make(self, **kw):
        base = dict(username='fatou.j', password='Kombo2026', plan_value='free')
        base.update(kw)
        return mem.create_member(self.biz, **base)

    # ── creating ──
    def test_username_and_password_can_be_different(self):
        v = self.make(username='Fatou.J')
        self.assertEqual((v.code, v.password, v.login_password, v.login_type), ('fatou.j', 'Kombo2026', 'Kombo2026', 'member'))
        self.assertTrue(v.is_member)

    def test_username_and_password_can_be_the_same(self):
        v = self.make(username='lamin', same=True)
        self.assertEqual((v.password, v.login_password), ('', 'lamin'))
        w = self.make(username='awa', password='awa')          # typed the same: stored as "same"
        self.assertEqual((w.password, w.login_password), ('', 'awa'))

    def test_free_unlimited_member(self):
        v = self.make(devices='3', rate_limit='10M/10M')
        self.assertEqual((v.price, v.duration_minutes, v.max_devices, v.rate_limit, v.plan_name),
                         (0, 0, 3, '10M/10M', mem.FREE_PLAN_NAME))
        self.assertEqual(mem.kind_of(v), 'free')
        # used but never paid: not "missing a sale"
        Voucher.objects.filter(pk=v.pk).update(used_at=timezone.now())
        from .views_live import missing_sales
        self.assertEqual(missing_sales(self.biz), ([], 0))

    def test_paying_member_records_first_payment(self):
        v = self.make(plan_value=str(self.month.pk), paid=True, method='wave', reference='W123')
        self.assertEqual((v.price, v.duration_minutes, v.max_devices), (Decimal('500'), 43200, 2))
        self.assertEqual(mem.kind_of(v), 'paid')
        sale = VoucherSale.objects.get(voucher=v)
        self.assertEqual((sale.amount, sale.payment_method), (Decimal('500'), 'wave'))

    def test_username_rules(self):
        Voucher.objects.create(business=self.biz, code='ABCD1234', plan_name='Day')
        v = Voucher.objects.create(business=self.biz, code='NEW1', plan_name='Day')
        VoucherCodeAlias.objects.create(business=self.biz, voucher=v, code='OLDCODE')
        for bad in ('abcd1234', 'OldCode', 'ab', '-dash', 'x' * 40, 'semi;colon'):
            with self.assertRaises(mem.MemberError, msg=bad):
                self.make(username=bad)

    def test_password_rules(self):
        for bad in ('abc', 'has space', 'x' * 65):
            with self.assertRaises(mem.MemberError, msg=bad):
                self.make(username='ok.user', password=bad)

    def test_page_creates_member_and_pushes_password(self):
        svc = FakeSvc()
        with mock.patch('core.views_agents.MikroTikService', return_value=svc), \
                mock.patch('core.views_agents._on_link', return_value=False):
            r = self.client.post(reverse('members'), {'username': 'Musa', 'password': 'Secret99', 'plan': 'free',
                                                      'devices': '1', 'router': self.router.pk, 'customer_name': 'Musa J'}, **HTML)
        self.assertEqual(r.status_code, 302)
        v = Voucher.objects.get(code='musa')
        self.assertEqual(svc.users.rows[0]['name'], 'musa')
        self.assertEqual(svc.users.rows[0]['password'], 'Secret99')
        self.assertIn('TapTap member', svc.users.rows[0]['comment'])
        self.assertEqual(Voucher.objects.get(pk=v.pk).mikrotik_sync_status, 'Synced')
        page = self.client.get(r['Location'], **HTML)
        self.assertContains(page, 'Secret99')      # login card after creating
        self.assertContains(page, 'musa')

    def test_page_shows_errors(self):
        r = self.client.post(reverse('members'), {'username': 'x', 'password': 'Secret99', 'plan': 'free'}, **HTML)
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Voucher.objects.filter(login_type='member').exists())

    # ── router: direct API and TapTap Link ──
    def test_full_sync_pushes_member_password_not_the_code(self):
        from .sync import sync_router
        v = self.make(username='fatou.j', password='Kombo2026', router=self.router)
        svc = FakeSvc([{'id': '*1', 'name': 'fatou.j', 'password': 'fatou.j', 'profile': 'x'}])
        with mock.patch('core.sync.MikroTikService', return_value=svc):
            sync_router(self.router)
        self.assertEqual(svc.users.rows[0]['password'], 'Kombo2026')
        v.refresh_from_db()
        self.assertEqual((v.login_type, v.password), ('member', 'Kombo2026'))   # TapTap member: TapTap stays the truth

    def test_full_sync_imports_router_users_with_own_password_as_members(self):
        from .sync import sync_router
        svc = FakeSvc([{'id': '*1', 'name': 'staff1', 'password': 'Pa55word', 'profile': 'default'},
                       {'id': '*2', 'name': 'K7Q2M9XP', 'password': 'K7Q2M9XP', 'profile': 'default'}])
        with mock.patch('core.sync.MikroTikService', return_value=svc):
            sync_router(self.router)
        m = Voucher.objects.get(code='staff1'); v = Voucher.objects.get(code='K7Q2M9XP')
        self.assertEqual((m.login_type, m.password), ('member', 'Pa55word'))
        self.assertEqual((v.login_type, v.password), ('voucher', ''))

    def test_link_command_sets_member_password_on_add_and_update(self):
        body = command_body(SimpleNamespace(kind='hotspot_users', pk=1, params={'profiles': [], 'users': [
            {'n': 'fatou.j', 'pw': 'Kombo2026', 'prof': 'p', 'lim': '', 'dis': False},
            {'n': 'K7Q2', 'prof': 'p', 'lim': '1d', 'dis': False}]}))
        member, voucher = body.split('; ')
        self.assertIn('add name="fatou.j" password="Kombo2026"', member)
        self.assertIn('set [find name="fatou.j"] profile="p" password="Kombo2026"', member)
        self.assertIn('add name="K7Q2" password="K7Q2"', voucher)
        self.assertNotIn('password', voucher.split('else=')[1])     # vouchers never touch the password on update

    def test_link_push_includes_member_password(self):
        from .agent import push_pending_vouchers
        from .models import AgentCommand
        self.make(username='fatou.j', password='Kombo2026', router=self.router)
        Voucher.objects.create(business=self.biz, router=self.router, code='VCODE1', plan_name='Day', source='taptap')
        push_pending_vouchers(self.router)
        users = {u['n']: u for u in AgentCommand.objects.get(kind='hotspot_users').params['users']}
        self.assertEqual(users['fatou.j']['pw'], 'Kombo2026')
        self.assertNotIn('pw', users['VCODE1'])

    def test_change_password_on_router_and_history(self):
        v = self.make(router=self.router)
        svc = FakeSvc([{'id': '*1', 'name': 'fatou.j', 'password': 'Kombo2026'}])
        with mock.patch('core.mikrotik.MikroTikService', return_value=svc), \
                mock.patch('core.voucher_history.channel', return_value='Direct API'):
            ok, _ = mem.change_password(v, 'NewPass1', user=self.owner, reason='forgot')
            self.assertTrue(ok)
            self.assertEqual(svc.users.rows[0]['password'], 'NewPass1')
            mem.change_password(v, same=True, user=self.owner)
            self.assertEqual(svc.users.rows[0]['password'], 'fatou.j')
        self.assertEqual(VoucherEvent.objects.filter(voucher=v, event='password_changed').count(), 2)
        with self.assertRaises(mem.MemberError):
            mem.change_password(Voucher.objects.create(business=self.biz, code='VVV1', plan_name='Day'), 'abcd1234')

    def test_password_view(self):
        v = self.make()
        r = self.client.post(reverse('member_password', args=[v.pk]), {'password': 'Another9'}, **HTML)
        self.assertEqual(r.status_code, 302)
        v.refresh_from_db()
        self.assertEqual(v.password, 'Another9')

    def test_renaming_member_keeps_own_password(self):
        v = self.make(router=self.router)
        rows = [{'id': '*1', 'name': 'fatou.j', 'password': 'Kombo2026'}]
        svc = FakeSvc(rows)
        with mock.patch('core.mikrotik.MikroTikService', return_value=svc), \
                mock.patch('core.voucher_history.channel', return_value='Direct API'):
            vc.change_code(v, 'Fatou.Jallow', self.owner, 'married')
        self.assertEqual((rows[0]['name'], rows[0]['password']), ('fatou.jallow', 'Kombo2026'))
        # the "repair passwords" routine never resets a member's own password
        self.assertEqual(vc.stale_passwords(self.biz, self.router, rows), [])

    def test_renew_adds_time_and_books_payment(self):
        v = self.make(plan_value=str(self.month.pk), paid=True)
        Voucher.objects.filter(pk=v.pk).update(used_at=timezone.now() - timedelta(days=31))
        v.refresh_from_db()
        ok, _, sale, minutes = mem.renew(v, method='cash', user=self.owner)
        v.refresh_from_db()
        self.assertEqual(minutes, 43200)
        self.assertEqual(v.status, 'active')
        self.assertGreater(v.expires_at, timezone.now() + timedelta(days=29))
        self.assertEqual((sale.amount, sale.voucher_code, sale.voucher_id), (Decimal('500'), 'fatou.j', None))
        with self.assertRaises(mem.MemberError):
            mem.renew(self.make(username='free.one'))     # free unlimited: nothing to renew

    # ── customer portal ──
    def portal(self):
        return PortalPage.objects.create(business=self.biz, name='Login', slug='kombo', kind='login', is_published=True)

    def check(self, page, **data):
        return self.client.post(reverse('portal_check', args=[page.slug]), json.dumps(data), content_type='application/json')

    def test_portal_member_login(self):
        page = self.portal()
        self.make(username='fatou.j', password='Kombo2026')
        Voucher.objects.create(business=self.biz, code='K7Q2M9XP', plan_name='Day')
        ok = self.check(page, code='Fatou.J', password='Kombo2026', member=True)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual((ok.json()['code'], ok.json()['member']), ('fatou.j', True))
        self.assertEqual(self.check(page, code='fatou.j', password='wrong', member=True).status_code, 404)
        self.assertEqual(self.check(page, code='K7Q2M9XP', password='K7Q2M9XP', member=True).status_code, 404)   # a voucher is not a member
        self.assertEqual(self.check(page, code='fatou.j').status_code, 404)                                    # a member is not a voucher code
        self.assertEqual(self.check(page, code='K7Q2M9XP').status_code, 200)                                   # vouchers unchanged

    def test_portal_state_returns_real_username(self):
        page = self.portal()
        self.make(username='fatou.j', password='Kombo2026')
        r = self.client.post(reverse('portal_state', args=[page.slug]), json.dumps({'code': 'FATOU.J'}), content_type='text/plain')
        self.assertEqual(r.json(), {'ok': True, 'code': 'fatou.j'})

    def test_portal_renderer_has_member_tab(self):
        from django.conf import settings
        js = (settings.BASE_DIR / 'static' / 'studio' / 'portal-render.js').read_text()
        self.assertIn('data-tab="member"', js)
        self.assertIn("b.members === false", js)     # on unless switched off; Voucher stays the default tab
        self.assertIn('body.member = true', js)

    def test_members_page_lists_and_filters(self):
        self.make(username='free.one')
        self.make(username='payer', plan_value=str(self.month.pk))
        r = self.client.get(reverse('members'), **HTML)
        self.assertContains(r, 'free.one'); self.assertContains(r, 'payer')
        r = self.client.get(reverse('members') + '?kind=free', **HTML)
        self.assertContains(r, 'free.one'); self.assertNotContains(r, '>payer<')
