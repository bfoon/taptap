"""Role-based access, team management and the platform console.

Run:  DB_ENGINE=sqlite python manage.py test core.test_team_access
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Agent, Business, CashCollection, Subscription, VoucherPlan, VoucherSale
from .models_team import PlatformAudit, TeamMember, UsageDaily
from .permissions import ROLES, URL_PERMS

HTML = {'HTTP_ACCEPT': 'text/html,application/xhtml+xml'}


@override_settings(AUTH_EMAIL_OTP=False)
class TeamAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user('owner@example.com', 'owner@example.com', 'pass-Owner-123', first_name='Omar')
        cls.biz = Business.objects.create(user=cls.owner, business_name='Kairaba WiFi', owner_name='Omar', phone='7000000',
                                          trial_ends_at=timezone.now() + timedelta(days=7))
        VoucherPlan.objects.create(business=cls.biz, name='Daily', price=40, duration_minutes=1440)
        cls.agent = Agent.objects.create(business=cls.biz, name='Awa Shop', commission_percent=10)
        VoucherSale.objects.create(business=cls.biz, plan_name='Daily', amount=Decimal('40'), agent=cls.agent)
        cls.members = {}
        for role in ROLES:
            if role == 'owner':
                continue
            u = User.objects.create_user(f'{role}@example.com', f'{role}@example.com', 'pass-Staff-123', first_name=role.title())
            cls.members[role] = TeamMember.objects.create(business=cls.biz, user=u, role=role)
        cls.root = User.objects.create_superuser('root@taptap.gm', 'root@taptap.gm', 'pass-Root-123')

    def as_(self, role):
        self.client.force_login(self.owner if role == 'owner' else self.members[role].user)

    def get(self, name, *args, **query):
        return self.client.get(reverse(name, args=args), query, **HTML)

    # ── page access per role ──
    def test_role_page_matrix(self):
        expect = {
            'owner': {'dashboard': 200, 'finance': 200, 'team': 200, 'reports': 200, 'sales_daily': 200, 'settings': 200},
            'admin': {'dashboard': 200, 'finance': 200, 'team': 200, 'settings': 200, 'routers': 200},
            'finance': {'reports': 200, 'finance': 200, 'agent_detail': 200, 'sales_daily': 200,
                        'vouchers': 403, 'generate_vouchers': 403, 'settings': 403, 'team': 403, 'routers': 403, 'plans': 403},
            'voucher_creator': {'vouchers': 200, 'generate_vouchers': 200, 'batches': 200,
                                'finance': 403, 'reports': 403, 'sales_daily': 403, 'routers': 403},
            'voucher_support': {'vouchers': 200, 'missing_vouchers': 200,
                                'generate_vouchers': 403, 'finance': 403, 'reports': 403, 'routers': 403},
            'viewer': {'sales_daily': 200, 'vouchers': 403, 'finance': 403, 'reports': 403, 'routers': 403, 'team': 403},
        }
        for role, pages in expect.items():
            self.as_(role)
            for name, code in pages.items():
                args = (self.agent.pk,) if name == 'agent_detail' else ()
                r = self.get(name, *args)
                self.assertEqual(r.status_code, code, f'{role} → {name}')

    def test_dashboard_sends_staff_to_their_start_page(self):
        for role, landing in [('viewer', 'sales_daily'), ('finance', 'reports'), ('voucher_support', 'vouchers'),
                              ('voucher_creator', 'generate_vouchers')]:
            self.as_(role)
            r = self.get('dashboard')
            self.assertRedirects(r, reverse(landing), fetch_redirect_response=False, msg_prefix=role)

    def test_forbidden_actions_do_nothing(self):
        self.as_('viewer')
        r = self.client.post(reverse('finance_sale_add'), {'mode': 'manual', 'amount': '100'}, **HTML)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.biz.sales.count(), 1)
        r = self.client.post(reverse('finance_collection_add'), {'agent': self.agent.pk, 'amount': '10'}, HTTP_ACCEPT='application/json')
        self.assertEqual(r.status_code, 403)
        self.assertFalse(CashCollection.objects.exists())

    def test_finance_role_is_limited_to_agents_and_cash(self):
        self.as_('finance')
        r = self.get('finance', tab='expenses')
        self.assertEqual(r.context['tab'], 'agents')
        self.assertNotContains(r, 'Record sale')
        self.assertContains(r, 'Collect')
        r = self.client.get(reverse('finance_export'), {'type': 'pnl'})
        self.assertIn('Outstanding', r.content.decode())  # forced to the agent report
        r = self.client.post(reverse('finance_collection_add'), {'agent': self.agent.pk, 'amount': '25', 'method': 'cash'}, **HTML)
        self.assertEqual(r.status_code, 302)
        c = CashCollection.objects.get()
        self.assertEqual(c.amount, Decimal('25'))
        self.assertEqual(c.recorded_by, self.members['finance'].user)
        act = self.biz.activities.filter(type='Cash Collected').first()
        self.assertIn('Finance', act.actor)

    def test_every_route_is_covered(self):
        from .permissions import MEMBER_ALLOWED
        from .team import SKIP_PREFIXES
        from .urls import urlpatterns
        for p in urlpatterns:
            route = '/' + str(p.pattern)
            if any(route.startswith(x) for x in SKIP_PREFIXES) or p.name.startswith('platform_'):
                continue
            self.assertTrue(p.name in URL_PERMS or p.name in MEMBER_ALLOWED, p.name)

    # ── team management ──
    def test_owner_adds_member_with_generated_password(self):
        self.as_('owner')
        r = self.client.post(reverse('team_member_save'), {'first_name': 'Fatou', 'email': 'Fatou@Example.com', 'role': 'finance'}, **HTML)
        self.assertRedirects(r, reverse('team'), fetch_redirect_response=False)
        m = TeamMember.objects.get(user__email='fatou@example.com')
        self.assertEqual((m.role, m.business, m.must_change_password), ('finance', self.biz, True))
        self.client.force_login(m.user)
        r = self.get('reports')
        self.assertRedirects(r, reverse('account_password'), fetch_redirect_response=False)

    def test_duplicate_email_rejected(self):
        self.as_('owner')
        self.client.post(reverse('team_member_save'), {'first_name': 'X', 'email': 'viewer@example.com', 'role': 'viewer'}, **HTML)
        self.assertEqual(TeamMember.objects.filter(user__email='viewer@example.com').count(), 1)

    def test_admin_cannot_create_or_edit_admins(self):
        self.as_('admin')
        self.client.post(reverse('team_member_save'), {'first_name': 'Y', 'email': 'y@example.com', 'role': 'admin'}, **HTML)
        self.assertFalse(User.objects.filter(email='y@example.com').exists())
        other = TeamMember.objects.create(business=self.biz, role='admin',
                                          user=User.objects.create_user('a2@example.com', 'a2@example.com', 'x'))
        self.client.post(reverse('team_member_action', args=[other.pk]), {'action': 'deactivate'}, **HTML)
        other.refresh_from_db()
        self.assertTrue(other.is_active)
        self.client.post(reverse('team_member_save'), {'first_name': 'Z', 'email': 'z@example.com', 'role': 'viewer',
                                                       'extra': ['subscription.manage', 'team.manage', 'reports.view']}, **HTML)
        self.assertEqual(TeamMember.objects.get(user__email='z@example.com').extra_permissions, ['reports.view'])

    def test_extra_permission_grants_page(self):
        m = self.members['viewer']
        m.extra_permissions = ['reports.view']
        m.save()
        self.as_('viewer')
        self.assertEqual(self.get('reports').status_code, 200)

    def test_switched_off_member_is_signed_out(self):
        self.as_('owner')
        m = self.members['voucher_support']
        self.client.post(reverse('team_member_action', args=[m.pk]), {'action': 'deactivate'}, **HTML)
        self.client.force_login(m.user)
        r = self.get('vouchers')
        self.assertRedirects(r, reverse('login'), fetch_redirect_response=False)

    def test_staff_cannot_buy_subscription(self):
        self.as_('admin')
        self.get('subscription_select', '1m')
        self.assertFalse(Subscription.objects.exists())
        r = self.get('subscription')
        self.assertContains(r, 'Owner renews')

    def test_expired_business_blocks_staff_too(self):
        Business.objects.filter(pk=self.biz.pk).update(trial_ends_at=timezone.now() - timedelta(days=1))
        self.as_('voucher_creator')
        r = self.get('vouchers')
        self.assertRedirects(r, reverse('subscription'), fetch_redirect_response=False)

    def test_staff_cannot_open_platform(self):
        for role in ('owner', 'admin'):
            self.as_(role)
            self.assertEqual(self.get('platform_overview').status_code, 403)

    def test_usage_is_recorded(self):
        self.as_('viewer')
        self.get('sales_daily')
        self.get('sales_daily')
        row = UsageDaily.objects.get(user=self.members['viewer'].user)
        self.assertEqual(row.page_views, 2)
        self.assertEqual(row.sections, {'sales': 2})
        self.client.logout()
        self.client.post(reverse('login'), {'email': 'owner@example.com', 'password': 'pass-Owner-123'})
        self.assertEqual(UsageDaily.objects.get(user=self.owner).logins, 1)

    # ── platform console ──
    def test_superuser_lands_on_platform(self):
        self.client.force_login(self.root)
        self.assertRedirects(self.get('dashboard'), reverse('platform_overview'), fetch_redirect_response=False)
        for name, args in [('platform_overview', ()), ('platform_businesses', ()), ('platform_business', (self.biz.pk,)),
                           ('platform_subscriptions', ()), ('platform_audit', ())]:
            self.assertEqual(self.get(name, *args).status_code, 200, name)
        self.assertEqual(self.client.get(reverse('platform_businesses'), {'export': 'csv'}).status_code, 200)

    def test_confirm_pending_payment_and_revenue(self):
        sub = Subscription.objects.create(business=self.biz, plan='3 Months', amount=Decimal('2000'))
        self.client.force_login(self.root)
        r = self.client.post(reverse('platform_sub_action', args=[sub.pk]), {'action': 'confirm_sub', 'reference': 'WAVE-9'}, **HTML)
        self.assertEqual(r.status_code, 302)
        sub.refresh_from_db()
        self.biz.refresh_from_db()
        self.assertEqual((sub.payment_status, sub.transaction_id, self.biz.subscription_status), ('Paid', 'WAVE-9', 'active'))
        self.assertGreater(self.biz.subscription_expires_at, timezone.now() + timedelta(days=89))
        r = self.get('platform_overview')
        self.assertEqual(r.context['rev']['all'], Decimal('2000'))
        self.assertEqual(r.context['counts']['paid'], 1)
        self.assertTrue(PlatformAudit.objects.filter(action='Confirmed payment').exists())

    def test_support_actions(self):
        self.client.force_login(self.root)
        act = reverse('platform_business_action', args=[self.biz.pk])
        self.client.post(act, {'action': 'record_payment', 'package': '1m', 'method': 'Cash'}, **HTML)
        self.assertEqual(self.biz.subscriptions.get().payment_status, 'Paid')
        self.client.post(act, {'action': 'extend_paid', 'days': '10'}, **HTML)
        self.client.post(act, {'action': 'suspend', 'note': 'unpaid'}, **HTML)
        self.assertFalse(User.objects.get(pk=self.owner.pk).is_active)
        self.assertFalse(User.objects.get(pk=self.members['admin'].user.pk).is_active)
        self.client.post(act, {'action': 'reactivate'}, **HTML)
        self.assertTrue(User.objects.get(pk=self.owner.pk).is_active)
        self.client.post(act, {'action': 'reset_owner_password'}, **HTML)
        self.assertFalse(User.objects.get(pk=self.owner.pk).check_password('pass-Owner-123'))
        self.assertEqual(PlatformAudit.objects.filter(business=self.biz).count(), 5)

    def test_view_as_owner_is_audited(self):
        self.client.force_login(self.root)
        self.client.post(reverse('platform_business_action', args=[self.biz.pk]), {'action': 'view_as'}, **HTML)
        r = self.get('dashboard')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Support view')
        self.client.post(reverse('finance_collection_add'), {'agent': self.agent.pk, 'amount': '5'}, **HTML)
        self.assertTrue(PlatformAudit.objects.filter(action='Change while viewing as owner').exists())
        self.assertIn('TapTap support', self.biz.activities.filter(type='Cash Collected').first().actor)
        self.assertFalse(UsageDaily.objects.filter(user=self.root).exists())  # support visits don't count as customer usage
        self.client.post(reverse('platform_view_as_stop'))
        self.assertRedirects(self.get('dashboard'), reverse('platform_overview'), fetch_redirect_response=False)


@override_settings(AUTH_EMAIL_OTP=False)
class AgentCommissionPrecisionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('o2@example.com', 'o2@example.com', 'pw')
        self.biz = Business.objects.create(user=self.owner, business_name='B', owner_name='O', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=7))
        self.client.force_login(self.owner)

    def save_agent(self, pct):
        self.client.post(reverse('finance_agent_save'), {'name': 'Awa', 'commission_percent': pct, 'active': '1'})
        return Agent.objects.get(business=self.biz)

    def test_fractional_percent_is_kept(self):
        from .finance import commission_for
        from .templatetags.taptap_extras import pct
        a = self.save_agent('9.09')
        self.assertEqual(a.commission_percent, Decimal('9.09'))
        self.assertEqual(commission_for(a, Decimal('1100')), Decimal('99.99'))
        self.assertEqual(pct(a.commission_percent), '9.09')
        a.delete()
        a = self.save_agent('9.0909')
        self.assertEqual(commission_for(a, Decimal('1100')), Decimal('100.00'))
        self.assertEqual(pct(a.commission_percent), '9.0909')
        self.assertEqual(pct(Decimal('10.0000')), '10')

    def test_pages_show_the_exact_percent(self):
        a = self.save_agent('9.09')
        r = self.client.get(reverse('agent_detail', args=[a.pk]), HTTP_ACCEPT='text/html')
        self.assertContains(r, '9.09% commission')
        r = self.client.get(reverse('finance'), {'tab': 'agents'}, HTTP_ACCEPT='text/html')
        self.assertContains(r, '9.09% commission')
        self.assertContains(r, 'step="any"')
