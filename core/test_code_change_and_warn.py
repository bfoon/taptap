"""Changing a voucher code keeps it working (password follows the code), and manual warnings.

Run:  DB_ENGINE=sqlite python manage.py test core.test_code_change_and_warn
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import voucher_codes as vc
from . import voucher_freeze as vf
from .agent import SAFE_KINDS
from .models import Business, Router, Voucher, VoucherCodeAlias
from .models_team import TeamMember

HTML = {'HTTP_ACCEPT': 'text/html'}


class FakeUsers:
    """Stands in for RouterOS /ip/hotspot/user over the API."""
    def __init__(self, rows):
        self.rows = rows

    def get(self, name=None):
        return [r for r in self.rows if r['name'] == name]

    def set(self, id, **fields):
        for r in self.rows:
            if r['id'] == id:
                r.update(fields)

    def remove(self, id):
        self.rows[:] = [r for r in self.rows if r['id'] != id]


class FakeSvc:
    def __init__(self, rows):
        self.users = FakeUsers(rows)

    def connect(self):
        return self

    def close(self):
        pass

    def resource(self, path):
        return self.users


@override_settings(AUTH_EMAIL_OTP=False)
class CodeChangeTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('cc@example.com', 'cc@example.com', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='B', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=7))
        self.router = Router.objects.create(business=self.biz, name='R', ip_address='10.0.0.1', username='u', password='p')

    def change(self, v, new, rows):
        svc = FakeSvc(rows)
        with mock.patch('core.mikrotik.MikroTikService', return_value=svc), \
                mock.patch('core.voucher_history.channel', return_value='Direct API'):
            vc.change_code(v, new, self.owner, 'customer asked')
        return svc.users.rows

    def test_taptap_voucher_password_follows_new_5_char_code(self):
        v = Voucher.objects.create(business=self.biz, router=self.router, code='ABCDEFGH', plan_name='Day', source='taptap')
        rows = self.change(v, 'XY7K2', [{'id': '*1', 'name': 'ABCDEFGH', 'password': 'ABCDEFGH'}])
        self.assertEqual(rows[0]['name'], 'XY7K2')
        self.assertEqual(rows[0]['password'], 'XY7K2')   # this was the bug: the password stayed ABCDEFGH

    def test_router_voucher_keeps_its_own_separate_password(self):
        v = Voucher.objects.create(business=self.biz, router=self.router, code='user01', plan_name='Day', source='mikrotik')
        rows = self.change(v, 'USR05', [{'id': '*2', 'name': 'user01', 'password': 'secret9'}])
        self.assertEqual((rows[0]['name'], rows[0]['password']), ('USR05', 'secret9'))

    def test_router_voucher_with_code_as_password_follows(self):
        v = Voucher.objects.create(business=self.biz, router=self.router, code='MK123456', plan_name='Day', source='mikrotik')
        rows = self.change(v, 'MK555', [{'id': '*3', 'name': 'MK123456', 'password': 'MK123456'}])
        self.assertEqual(rows[0]['password'], 'MK555')

    def test_link_rename_script_sets_password(self):
        from types import SimpleNamespace
        from .agent import command_body
        body = command_body(SimpleNamespace(kind='hotspot_user_rename', params={'name': 'ABCDEFGH', 'new_name': 'XY7K2', 'password': True}, pk=1))
        self.assertIn('name="XY7K2" password="XY7K2"', body)
        self.assertIn(':if (true or', body)
        body = command_body(SimpleNamespace(kind='hotspot_users_repass', params={'names': ['NEW55']}, pk=2))
        self.assertIn('password=$n', body)

    def test_repair_finds_vouchers_broken_before_the_fix(self):
        v = Voucher.objects.create(business=self.biz, router=self.router, code='NEW55', plan_name='Day', source='taptap')
        VoucherCodeAlias.objects.create(business=self.biz, voucher=v, code='OLDCODE88')
        rows = [{'name': 'NEW55', 'password': 'OLDCODE88'}, {'name': 'OTHER1', 'password': 'x'}]
        self.assertEqual(vc.stale_passwords(self.biz, self.router, rows), ['NEW55'])
        self.assertEqual(vc.stale_passwords(self.biz, self.router, [{'name': 'NEW55', 'password': 'NEW55'}]), [])
        svc = FakeSvc([{'id': '*9', 'name': 'NEW55', 'password': 'OLDCODE88'}])
        vc.repair_passwords(self.router, ['NEW55'], svc=svc)
        self.assertEqual(svc.users.rows[0]['password'], 'NEW55')

    def test_link_commands_are_allowed(self):
        self.assertIn('hotspot_users_repass', SAFE_KINDS)
        self.assertIn('hotspot_user_rename', SAFE_KINDS)


@override_settings(AUTH_EMAIL_OTP=False)
class ManualWarningTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('w@example.com', 'w@example.com', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='B', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=7))
        self.v = Voucher.objects.create(business=self.biz, code='WARN1', plan_name='Day', duration_minutes=1440,
                                        used_at=timezone.now() - timedelta(hours=1))

    def member(self, role, extra=()):
        u = User.objects.create_user(f'{role}@w.com', f'{role}@w.com', 'pw')
        TeamMember.objects.create(business=self.biz, user=u, role=role, extra_permissions=list(extra))
        return u

    def warn(self, **data):
        base = {'reason': 'too many downloads', 'message': 'Please stop heavy downloads between 8 and 10 pm.'}
        base.update(data)
        return self.client.post(reverse('voucher_warn', args=[self.v.pk]), base, **HTML)

    def test_owner_and_admin_can_warn_with_their_message(self):
        for who in (self.owner, self.member('admin')):
            Voucher.objects.filter(pk=self.v.pk).update(frozen_at=None, freeze_kind='', status='active', warning_message='')
            self.client.force_login(who)
            self.warn()
            v = Voucher.objects.get(pk=self.v.pk)
            self.assertEqual((v.freeze_kind, v.status), ('warning', 'disabled'))
            block = vf.portal_block(v)
            self.assertEqual((block['kind'], block['can_accept']), ('warning', True))
            self.assertIn('heavy downloads', block['message'])

    def test_other_roles_cannot_warn_even_as_extra(self):
        for u in (self.member('voucher_support'), self.member('voucher_creator', extra=['vouchers.warn'])):
            self.client.force_login(u)
            self.warn()
            self.assertIsNone(Voucher.objects.get(pk=self.v.pk).frozen_at)

    def test_reason_required_and_default_message(self):
        self.client.force_login(self.owner)
        self.warn(reason='')
        self.assertIsNone(Voucher.objects.get(pk=self.v.pk).frozen_at)
        self.warn(message='')
        self.assertEqual(Voucher.objects.get(pk=self.v.pk).warning_message, vf.MANUAL_WARNING)

    def test_customer_accepts_and_message_is_cleared(self):
        from .shared_use import customer_accepted
        self.client.force_login(self.owner)
        self.warn()
        v = Voucher.objects.get(pk=self.v.pk)
        customer_accepted(v)
        v.refresh_from_db()
        self.assertEqual((v.frozen_at, v.status, v.warning_message), (None, 'active', ''))

    def test_warn_from_active_users_by_code_or_old_code(self):
        self.client.force_login(self.owner)
        VoucherCodeAlias.objects.create(business=self.biz, voucher=self.v, code='OLDWARN9')
        self.client.post(reverse('session_warn'), {'user': 'oldwarn9', 'reason': 'test', 'message': 'hi'}, **HTML)
        self.assertEqual(Voucher.objects.get(pk=self.v.pk).freeze_kind, 'warning')
        r = self.client.post(reverse('session_warn'), {'user': 'not-a-voucher', 'reason': 'x'}, **HTML, follow=True)
        self.assertContains(r, 'not logged in with a TapTap voucher')

    def test_warn_button_shown_only_to_owner_admin(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('voucher_detail', args=[self.v.pk]), **HTML), 'id="warnModal"')
        self.client.force_login(self.member('voucher_support'))
        self.assertNotContains(self.client.get(reverse('voucher_detail', args=[self.v.pk]), **HTML), 'id="warnModal"')
