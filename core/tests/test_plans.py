"""Plans: Free, Basic, Pro and Enterprise (core/features.py, core/plans.py, Subscription page)."""
import importlib
import json
from datetime import timedelta
from decimal import Decimal

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import features as ft, plans, sync_control as sc
from core.models import Business, Router, Subscription
from core.models_plans import EnterpriseFeature
from core.models_sync import RouterSyncPolicy
from core.subscriptions import activate_subscription, record_paid_subscription

CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'plans'}}


@override_settings(CACHES=CACHE, AUTH_EMAIL_OTP=False)
class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('owner@x.gm', 'owner@x.gm', 'pw-123456')
        self.biz = Business.objects.create(user=self.owner, business_name='Kerr Sering', owner_name='O', phone='7000000',
                                           trial_ends_at=timezone.now() + timedelta(days=14))
        self.router = Router.objects.create(business=self.biz, name='Hap', connection_mode='agent', status='Online')
        self.client.force_login(self.owner)

    def expire(self):
        Business.objects.filter(pk=self.biz.pk).update(trial_ends_at=timezone.now() - timedelta(days=1))
        self.biz.refresh_from_db()

    def tier(self, t):
        Business.objects.filter(pk=self.biz.pk).update(feature_tier=t)
        self.biz.refresh_from_db()


class Bundles(Base):
    def test_what_each_plan_holds(self):
        self.assertEqual(list(ft.TIERS), ['free', 'basic', 'pro', 'enterprise'])
        self.assertEqual(ft.TIERS['free'][2], frozenset())
        self.assertFalse({'team', 'payroll', 'fit'} & ft.TIERS['basic'][2])
        self.assertEqual(ft.TIERS['basic'][2] | {'team', 'payroll', 'fit'}, ft.ALL)
        self.assertEqual(ft.TIERS['pro'][2], ft.ALL)
        self.assertEqual(ft.TIERS['enterprise'][2], ft.ALL)
        self.assertEqual(ft.cheapest_tier_with('reports'), 'basic')
        self.assertEqual(ft.cheapest_tier_with('fit'), 'pro')

    def test_old_package_names_never_lose_a_module(self):
        old_starter = {'daily_sales', 'members', 'agents', 'customer_reports', 'missing_vouchers', 'tracking', 'reports', 'finance',
                       'ip_bindings', 'hotspot_profiles', 'alerts', 'security', 'portal_studio', 'voucher_designer'}
        self.tier('starter')
        self.assertEqual(ft.tier_of(self.biz), 'basic')
        self.assertTrue(old_starter <= ft.enabled_set(self.biz))
        self.tier('business')
        self.assertEqual(ft.tier_of(self.biz), 'pro')
        self.assertEqual(ft.enabled_set(self.biz), set(ft.ALL))
        self.tier('nonsense')
        self.assertEqual(ft.tier_of(self.biz), 'pro')

    def test_the_migration_renames_the_packages(self):
        mig = importlib.import_module('core.migrations.0131_plans_free_basic_pro_enterprise')
        self.tier('starter')
        other = Business.objects.create(user=User.objects.create_user('b@x.gm', 'b@x.gm', 'pw-123456'), business_name='B',
                                        owner_name='B', phone='1', trial_ends_at=timezone.now(), feature_tier='business')
        mig.rename_packages(django_apps, None)
        self.biz.refresh_from_db(); other.refresh_from_db()
        self.assertEqual((self.biz.feature_tier, other.feature_tier), ('basic', 'pro'))

    def test_new_accounts_start_on_pro_for_their_trial(self):
        self.assertEqual(self.biz.feature_tier, 'pro')
        self.assertEqual(self.biz.tier_key, 'pro')


class FallBackToFree(Base):
    def test_an_ended_trial_keeps_free_instead_of_locking_out(self):
        self.expire()
        self.assertFalse(self.biz.paid_access)
        self.assertTrue(self.biz.has_access)
        self.assertEqual(ft.effective_tier(self.biz), 'free')
        self.assertEqual(self.biz.tier_name, 'Free')
        self.assertEqual(self.client.get(reverse('vouchers')).status_code, 200)
        self.assertEqual(self.client.get(reverse('batches')).status_code, 200)

    @override_settings(FREE_PLAN_FALLBACK=False)
    def test_the_platform_can_keep_the_old_lockout(self):
        self.expire()
        self.assertFalse(self.biz.has_access)
        self.assertEqual(ft.effective_tier(self.biz), 'pro')
        self.assertRedirects(self.client.get(reverse('vouchers')), reverse('subscription'), fetch_redirect_response=False)

    def test_free_locks_the_rest_with_a_way_to_unlock(self):
        self.tier('free')
        r = self.client.get(reverse('reports'))
        self.assertEqual(r.status_code, 403)
        self.assertContains(r, 'Unlock with Basic', status_code=403)
        r = self.client.get(reverse('config_studio', args=[self.router.pk]))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.get(reverse('sync_control', args=[self.router.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse('vouchers')).status_code, 200)

    def test_basic_has_no_team_and_no_ai(self):
        self.tier('basic')
        r = self.client.get(reverse('team'))
        self.assertContains(r, 'Unlock with Pro', status_code=403)
        self.assertFalse(ft.uses(self.biz, 'fit'))
        self.assertTrue(ft.uses(self.biz, 'reports'))
        self.assertEqual(self.client.get(reverse('reports')).status_code, 200)

    def test_locked_menu_links_stay_visible_with_a_lock(self):
        self.tier('free')
        r = self.client.get(reverse('vouchers'))
        body = r.content.decode()
        self.assertIn('data-tt-locked="1"', body)
        self.assertIn('bi-lock-fill tt-lock', body)
        self.assertIn('Free plan · upgrade', body)

    def test_owner_hidden_modules_still_disappear(self):
        Business.objects.filter(pk=self.biz.pk).update(features_off=['reports'])
        body = self.client.get(reverse('vouchers')).content.decode()
        self.assertNotIn(f'href="{reverse("reports")}"', body.split('</aside>')[0])

    def test_free_prints_the_minimal_design(self):
        from core.studio_presets import voucher_template
        from core.models import VoucherDesign
        VoucherDesign.objects.create(business=self.biz, name='Mine', template_key='gold', config=voucher_template('gold'), is_default=True)
        self.tier('free')
        r = self.client.get(reverse('voucher_print') + '?sample=1')
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.context['design'])
        self.assertEqual(list(r.context['designs']), [])
        self.assertEqual(json.loads(r.context['config_json']), voucher_template('minimal'))
        self.tier('pro')
        r = self.client.get(reverse('voucher_print') + '?sample=1')
        self.assertEqual(r.context['design'].name, 'Mine')


class SubscriptionPage(Base):
    def test_four_plans_with_prices(self):
        r = self.client.get(reverse('subscription'))
        self.assertEqual(r.status_code, 200)
        for name in ('Free', 'Basic', 'Pro', 'Enterprise'):
            self.assertContains(r, f'<h3>{name}</h3>')
        self.assertContains(r, 'YOUR PLAN')
        pro = plans.price_for(self.biz, 'pro', '1m')
        basic = plans.price_for(self.biz, 'basic', '1m')
        self.assertEqual(pro['amount'], Decimal('700'))
        self.assertEqual(basic['amount'], Decimal('420'))

    def test_choosing_basic_starts_when_paid(self):
        r = self.client.post(reverse('subscription_select', args=['3m']), {'tier': 'basic'})
        self.assertEqual(r.status_code, 302)
        sub = Subscription.objects.get(business=self.biz)
        self.assertEqual((sub.tier, sub.plan, sub.payment_status), ('basic', '3 Months', 'Pending'))
        self.biz.refresh_from_db()
        self.assertEqual(self.biz.feature_tier, 'pro')                 # nothing changes before the payment
        activate_subscription(sub)
        self.biz.refresh_from_db()
        self.assertEqual((self.biz.feature_tier, self.biz.subscription_status), ('basic', 'active'))
        self.assertFalse(ft.enabled(self.biz, 'team'))

    def test_enterprise_cannot_be_bought_from_the_page(self):
        self.client.post(reverse('subscription_select', args=['1m']), {'tier': 'enterprise'})
        self.assertFalse(Subscription.objects.exists())

    def test_a_payment_without_a_plan_lifts_free_to_pro(self):
        self.expire()
        self.tier('free')
        sub = record_paid_subscription(self.biz, '1 Month', 700, 30)
        self.biz.refresh_from_db()
        self.assertEqual((self.biz.feature_tier, sub.tier), ('pro', 'pro'))
        self.assertEqual(ft.effective_tier(self.biz), 'pro')

    def test_free_waits_for_paid_time_to_end(self):
        r = self.client.post(reverse('subscription_free'), follow=True)
        self.assertContains(r, 'You move to Free by yourself')
        self.biz.refresh_from_db()
        self.assertEqual(self.biz.feature_tier, 'pro')
        self.expire()
        self.client.post(reverse('subscription_free'))
        self.biz.refresh_from_db()
        self.assertEqual(self.biz.feature_tier, 'free')

    def test_enterprise_requests_go_both_ways(self):
        r = self.client.post(reverse('subscription_enterprise'), {'title': 'Bank report every Friday', 'details': 'PDF', 'contact': '7000000'})
        self.assertEqual(r.status_code, 302)
        item = EnterpriseFeature.objects.get(business=self.biz)
        self.assertEqual(item.status, 'requested')
        admin = User.objects.create_superuser('root@x.gm', 'root@x.gm', 'pw-123456')
        self.client.force_login(admin)
        r = self.client.post(reverse('platform_business_action', args=[self.biz.pk]),
                             {'action': 'enterprise_item', 'item': item.pk, 'status': 'building', 'answer': 'Ready next week'})
        self.assertEqual(r.status_code, 302)
        self.client.force_login(self.owner)
        r = self.client.get(reverse('subscription'))
        self.assertContains(r, 'Bank report every Friday')
        self.assertContains(r, 'Being built')
        self.assertContains(r, 'Ready next week')
        self.assertFalse(self.client.post(reverse('subscription_enterprise'), {'title': ''}).status_code != 302)
        self.assertEqual(EnterpriseFeature.objects.count(), 1)

    def test_staff_without_subscription_rights_cannot_choose(self):
        from core.models_team import TeamMember
        staff = User.objects.create_user('s@x.gm', 's@x.gm', 'pw-123456')
        TeamMember.objects.create(business=self.biz, user=staff, role='admin')
        self.client.force_login(staff)
        self.assertNotEqual(self.client.post(reverse('subscription_select', args=['1m']), {'tier': 'pro'}).status_code, 200)
        self.assertFalse(Subscription.objects.exists())


class SyncControlFollowsThePlan(Base):
    def test_saved_choices_rest_on_free_and_waiting_vouchers_go(self):
        from core.models import Voucher
        RouterSyncPolicy.objects.create(router=self.router, full_mode='manual', voucher_mode='manual')
        Voucher.objects.create(business=self.biz, router=self.router, code='W1', plan_name='Day', duration_minutes=60,
                               source='taptap', mikrotik_sync_status=sc.HELD)
        self.assertEqual(sc.policy(self.router).full_mode, 'manual')
        self.expire()
        self.router.refresh_from_db()
        self.assertEqual(sc.policy(self.router).full_mode, 'auto')      # TapTap's defaults on Free
        sc.tick()
        self.assertEqual(sc.held_count(self.router), 0)


class Pages(Base):
    def test_platform_and_features_pages_render(self):
        EnterpriseFeature.objects.create(business=self.biz, title='Custom report')
        Subscription.objects.create(business=self.biz, plan='1 Month', amount=420, tier='basic')
        self.assertContains(self.client.get(reverse('business_features')), 'YOUR PLAN')
        admin = User.objects.create_superuser('root@x.gm', 'root@x.gm', 'pw-123456')
        self.client.force_login(admin)
        r = self.client.get(reverse('platform_business', args=[self.biz.pk]))
        self.assertContains(r, 'Tailor-made features')
        self.assertContains(r, 'Custom report')
        self.assertContains(r, 'Plan: Enterprise')
        self.assertContains(self.client.get(reverse('platform_subscriptions')), '<b>Basic</b>')
        r = self.client.post(reverse('platform_business_action', args=[self.biz.pk]),
                             {'action': 'record_payment', 'package': '1m', 'tier': 'enterprise', 'method': 'Cash'})
        self.biz.refresh_from_db()
        self.assertEqual(self.biz.feature_tier, 'enterprise')
