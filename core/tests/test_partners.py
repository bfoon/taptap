"""Three kinds of account: Wi-Fi owners, agents (buy before they resell) and resellers (buy on the Market)."""
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import partners as pt
from core.models import Business, CashCollection, Router, Subscription, Voucher, VoucherPlan, VoucherSale
from core.models_partners import AgentLink, AgentRetail, AgentTopUp


def make_owner(email='owner@x.gm', name='Kerr Wi-Fi'):
    u = User.objects.create_user(email, email, 'pw-123456', first_name='Omar')
    b = Business.objects.create(user=u, business_name=name, owner_name='Omar', phone='+220 7000001', country='GM',
                                trial_ends_at=timezone.now() + timedelta(days=30), wifi_ssid='KerrWiFi')
    Router.objects.create(business=b, name='Hap', ip_address='10.0.0.1', username='u', password='p', status='Online', connection_mode='agent')
    return u, b


def make_partner(kind, email='agent@x.gm', name='Awa Phone Corner', area='Brikama'):
    u = User.objects.create_user(email, email, 'pw-123456', first_name='Awa')
    b = Business.objects.create(user=u, business_name=name, owner_name='Awa', phone='+220 7000002', country='GM',
                                trial_ends_at=timezone.now())
    pt.setup_account(b, kind, area=area)
    return u, b


@override_settings(AUTH_EMAIL_OTP=False)
class Registration(TestCase):
    def setUp(self):
        cache.clear()

    def data(self, **kw):
        d = {'business_name': 'Awa Phone Corner', 'owner_name': 'Awa Jallow', 'country': 'GM', 'phone': '7000003',
             'email': 'awa@example.com', 'password': 'Very-strong-pw-42'}
        d.update(kw)
        return d

    @mock.patch('core.auth_security.validate_email_address', side_effect=lambda e: (e.lower(), ''))
    def test_three_kinds_land_in_their_own_portal(self, _v):
        r = self.client.get(reverse('register'))
        self.assertContains(r, 'value="agent"')
        self.assertContains(r, 'value="reseller"')
        r = self.client.post(reverse('register'), self.data(account_kind='agent'))
        self.assertEqual(r.status_code, 200)                         # an agent must say where they sell
        self.assertContains(r, 'Say the town or area')
        r = self.client.post(reverse('register'), self.data(account_kind='agent', area='Brikama'))
        self.assertRedirects(r, reverse('partner_home'), fetch_redirect_response=False)
        b = Business.objects.get(user__email='awa@example.com')
        self.assertEqual((b.account_kind, b.feature_tier, b.plans.count()), ('agent', 'free', 0))
        self.assertEqual(b.partner_profile.area, 'Brikama')
        self.assertTrue(pt.standing(b)['ok'])
        self.assertRedirects(self.client.get(reverse('dashboard')), reverse('partner_home'), fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse('vouchers')), reverse('partner_home'), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse('partner_home')).status_code, 200)
        self.assertEqual(self.client.get(reverse('market')).status_code, 302)          # the Market is for resellers
        self.client.logout()
        r = self.client.post(reverse('register'), self.data(account_kind='reseller', area='Serrekunda', email='lamin@example.com'))
        self.assertRedirects(r, reverse('partner_home'), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse('market')).status_code, 200)
        self.assertContains(self.client.get(reverse('market')), 'RESELLER PORTAL')
        self.client.logout()
        r = self.client.post(reverse('register'), self.data(email='owner2@example.com'))
        self.assertRedirects(r, reverse('dashboard'), fetch_redirect_response=False)
        owner = Business.objects.get(user__email='owner2@example.com')
        self.assertEqual(owner.account_kind, 'wifi')
        self.assertTrue(owner.plans.exists())
        self.assertRedirects(self.client.get(reverse('partner_home')), reverse('dashboard'), fetch_redirect_response=False)

    @mock.patch('core.auth_security.validate_email_address', side_effect=lambda e: (e.lower(), ''))
    def test_invitation_link_applies_by_itself(self, _v):
        _, owner = make_owner()
        prog = pt.program(owner)
        prog.open = True
        prog.save()
        r = self.client.get(reverse('agent_join', args=[prog.invite_code]))
        self.assertRedirects(r, reverse('register') + '?as=agent', fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse('register') + '?as=agent'), 'invited you')
        self.client.post(reverse('register'), self.data(account_kind='agent', area='Brikama'))
        link = AgentLink.objects.get()
        self.assertEqual((link.owner, link.status), (owner, 'pending'))


class AgentFlow(TestCase):
    def setUp(self):
        cache.clear()
        self.ou, self.owner = make_owner()
        self.day = VoucherPlan.objects.create(business=self.owner, name='Day', price=Decimal('25'), duration_minutes=1440)
        self.prog = pt.program(self.owner)
        self.prog.open, self.prog.areas, self.prog.discount_percent, self.prog.min_topup = True, 'Brikama, Nyambai', Decimal('20'), Decimal('500')
        self.prog.save()
        self.au, self.agent = make_partner('agent')

    def accepted(self, pct=20):
        link = pt.apply(self.agent, self.owner, 'Phone shop at the car park')
        return pt.accept(link.pk, self.owner, pct)

    def test_directory_finds_networks_near_the_agent(self):
        rows = pt.directory(self.agent)
        self.assertEqual([r['owner'] for r in rows], [self.owner])
        self.assertEqual(rows[0]['example']['pay'], Decimal('1000.00'))          # 50 × D20
        self.assertEqual(rows[0]['example']['worth'], Decimal('1250.00'))
        self.assertEqual(pt.directory(self.agent, 'Basse'), [])
        self.prog.open = False
        self.prog.save()
        self.assertEqual(pt.directory(self.agent), [])

    def test_pay_2000_get_2500_cash(self):
        link = self.accepted()
        self.assertEqual(link.agent.commission_percent, Decimal('20'))
        self.assertEqual(link.agent.business, self.owner)
        t = pt.request_topup(link, self.day.pk, '2000')
        self.assertEqual(t.status, 'requested')
        with self.assertRaises(pt.PartnerError):
            pt.approve(t.pk, self.owner, amount='3000')                        # never more than asked
        t = pt.approve(t.pk, self.owner, amount='1500')                         # the owner lowers it
        self.assertEqual((t.quantity, t.amount, t.status), (75, Decimal('1500.00'), 'approved'))
        t = pt.claim(t.reference, self.agent, 'cash')
        self.assertEqual(t.status, 'claimed')
        t = pt.confirm_paid(t.pk, self.owner, self.ou)
        self.assertEqual(t.status, 'issued')
        self.assertEqual(t.batch.vouchers.count(), 75)
        self.assertEqual(set(t.batch.vouchers.values_list('agent_id', flat=True)), {link.agent_id})
        self.assertEqual(t.batch.settlement, 'prepaid')
        sales = VoucherSale.objects.filter(business=self.owner, agent=link.agent)
        self.assertEqual(sales.count(), 75)
        self.assertEqual(sum(s.amount - s.commission for s in sales), Decimal('1500.00'))   # exactly what the agent paid
        self.assertEqual(CashCollection.objects.get(agent=link.agent).amount, Decimal('1500.00'))
        s = pt.agent_summary(self.agent)
        self.assertEqual((s['spent'], s['worth'], s['profit']), (Decimal('1500.00'), Decimal('1875.00'), Decimal('375.00')))

    def test_rules(self):
        with self.assertRaises(pt.PartnerError):
            pt.request_topup(AgentLink.objects.create(agent_account=self.agent, owner=self.owner), self.day.pk, '1000')   # not accepted yet
        AgentLink.objects.all().delete()
        link = self.accepted()
        with self.assertRaises(pt.PartnerError):
            pt.request_topup(link, self.day.pk, '100')                         # under the owner's minimum
        other = VoucherPlan.objects.create(business=self.owner, name='Week', price=100, duration_minutes=10080)
        self.prog.plans.set([self.day])
        with self.assertRaises(pt.PartnerError):
            pt.request_topup(link, other.pk, '1000')                           # not a plan for agents
        t = pt.request_topup(link, self.day.pk, '1000')
        pt.approve(t.pk, self.owner)
        with self.assertRaises(pt.PartnerError):
            pt.claim(t.reference, self.agent, 'wave', 'x')                     # no Wave wallet, and no transaction ID
        # trial over: no new stock until the fee is paid
        Business.objects.filter(pk=self.agent.pk).update(trial_ends_at=timezone.now() - timedelta(days=1))
        self.agent.refresh_from_db()
        with self.assertRaises(pt.PartnerError) as e:
            pt.request_topup(link, self.day.pk, '1000')
        self.assertIn('Pay', str(e.exception))
        sub = pt.request_month(self.agent)
        self.assertEqual((sub.amount, sub.plan, sub.payment_status), (Decimal('50'), '1 Month', 'Pending'))
        from core.subscriptions import activate_subscription
        activate_subscription(sub)
        self.agent.refresh_from_db()
        self.assertTrue(pt.standing(self.agent)['ok'])
        pt.request_topup(link, self.day.pk, '1000')

    def test_pause_cancels_open_requests_and_unpaid_expire(self):
        link = self.accepted()
        t = pt.request_topup(link, self.day.pk, '1000')
        pt.set_status(link.pk, self.owner, 'paused', 'Bring back last cash first')
        t.refresh_from_db()
        self.assertEqual(t.status, 'cancelled')
        link.agent.refresh_from_db()
        self.assertFalse(link.agent.active)
        pt.set_status(link.pk, self.owner, 'active')
        t2 = pt.approve(pt.request_topup(link, self.day.pk, '1000').pk, self.owner)
        AgentTopUp.objects.filter(pk=t2.pk).update(approved_at=timezone.now() - timedelta(hours=pt.PAY_HOURS + 1))
        self.assertEqual(pt.sweep(), 1)
        t2.refresh_from_db()
        self.assertEqual(t2.status, 'expired')

    def test_auto_approve_and_cash_at_the_counter(self):
        self.prog.auto_approve, self.prog.auto_approve_limit = True, Decimal('1000')
        self.prog.save()
        link = self.accepted()
        t = pt.request_topup(link, self.day.pk, '1000')
        self.assertEqual(t.status, 'approved')
        t = pt.confirm_paid(t.pk, self.owner, method='cash')                   # paid at the counter, never "claimed"
        self.assertEqual((t.status, t.quantity), ('issued', 50))

    @mock.patch('core.voucher_make.make_batch', side_effect=ValueError('plan locked'))
    def test_paid_but_batch_fails_is_kept_for_review(self, _m):
        link = self.accepted()
        t = pt.approve(pt.request_topup(link, self.day.pk, '1000').pk, self.owner)
        t = pt.confirm_paid(t.pk, self.owner, method='cash')
        self.assertEqual(t.status, 'review')
        self.assertIn('plan locked', t.error)

    def test_wave_webhook_makes_the_batch(self):
        from core import market
        from core.models_market import MarketWallet
        w = MarketWallet.objects.create(business=self.owner, provider='wave', number='+2207000001', holder='Kerr', mode='api', currency='GMD')
        link = self.accepted()
        t = pt.approve(pt.request_topup(link, self.day.pk, '1000').pk, self.owner)
        AgentTopUp.objects.filter(pk=t.pk).update(route='own', currency='GMD', method='wave', provider_session_id='cos_1')
        ev = {'reference': t.reference, 'paid': True, 'amount': '1000', 'currency': 'GMD', 'transaction_id': 'T1',
              'provider_session_id': 'cos_1', 'raw': {}}
        got = market.settle(ev, wallet=w, route='own')                       # the Market's webhook path, hooked
        self.assertEqual((got.reference, got.status), (t.reference, 'issued'))
        self.assertEqual(market.settle(ev, wallet=w, route='own').status, 'issued')   # Wave retries are harmless
        bad = pt.approve(pt.request_topup(link, self.day.pk, '1000').pk, self.owner)
        AgentTopUp.objects.filter(pk=bad.pk).update(route='own', currency='GMD')
        got = market.settle({**ev, 'reference': bad.reference, 'amount': '10', 'provider_session_id': ''}, wallet=w, route='own')
        self.assertEqual(got.status, 'review')


class Pages(TestCase):
    def setUp(self):
        cache.clear()
        self.ou, self.owner = make_owner()
        self.day = VoucherPlan.objects.create(business=self.owner, name='Day', price=Decimal('25'), duration_minutes=1440)
        self.prog = pt.program(self.owner)
        self.prog.open, self.prog.areas = True, 'Brikama'
        self.prog.save()
        self.au, self.agent = make_partner('agent')

    def test_end_to_end_in_the_browser(self):
        c = self.client
        c.force_login(self.au)
        self.assertContains(c.get(reverse('partner_home')), 'AGENT PORTAL')
        r = c.get(reverse('partner_find'))
        self.assertContains(r, 'Kerr Wi-Fi')
        self.assertContains(c.get(reverse('partner_owner', args=[self.owner.pk])), 'Send my application')
        c.post(reverse('partner_apply', args=[self.owner.pk]), {'message': 'Hi'})
        # the owner
        c.force_login(self.ou)
        r = c.get(reverse('agent_network'))
        self.assertContains(r, 'Awa Phone Corner')
        self.assertContains(c.get(reverse('dashboard')), 'Agent network')
        link = AgentLink.objects.get()
        c.post(reverse('agent_network'), {'action': 'accept', 'link': link.pk, 'discount_percent': '20'})
        link.refresh_from_db()
        self.assertEqual(link.status, 'active')
        # the agent tops up
        c.force_login(self.au)
        r = c.post(reverse('partner_topup_new', args=[self.owner.pk]), {'plan': self.day.pk, 'amount': '2000'})
        t = AgentTopUp.objects.get()
        self.assertRedirects(r, reverse('partner_topup', args=[t.reference]), fetch_redirect_response=False)
        self.assertContains(c.get(reverse('partner_topup', args=[t.reference])), 'Waiting for Kerr Wi-Fi')
        # the owner approves with cash on the spot
        c.force_login(self.ou)
        c.post(reverse('agent_network'), {'action': 'approve', 'topup': t.pk, 'amount': '2000', 'cash_now': '1'})
        t.refresh_from_db()
        self.assertEqual((t.status, t.quantity), ('issued', 100))
        # the agent sells
        c.force_login(self.au)
        r = c.get(reverse('partner_batch', args=[t.reference]))
        v = t.batch.vouchers.first()
        self.assertContains(r, v.code)
        c.post(reverse('partner_batch', args=[t.reference]), {'voucher': v.pk, 'price': '25'})
        self.assertTrue(AgentRetail.objects.filter(voucher=v, agent_account=self.agent).exists())
        self.assertContains(c.get(reverse('partner_vouchers')), '100× Day')
        self.assertContains(c.get(reverse('partner_batch', args=[t.reference]) + '?print=1&show=all'), v.code)
        self.assertEqual(c.get(reverse('partner_billing')).status_code, 200)
        self.assertEqual(c.get(reverse('partner_profile')).status_code, 200)
        self.assertEqual(c.get(reverse('partner_help')).status_code, 200)

    def test_staff_can_confirm_cash_but_not_approve(self):
        from core.models_team import TeamMember
        cashier = User.objects.create_user('cash@x.gm', 'cash@x.gm', 'pw-123456')
        TeamMember.objects.create(business=self.owner, user=cashier, role='finance', is_active=True)
        link = pt.accept(pt.apply(self.agent, self.owner).pk, self.owner, 20)
        t = pt.request_topup(link, self.day.pk, '1000')
        self.client.force_login(cashier)
        self.client.post(reverse('agent_network'), {'action': 'approve', 'topup': t.pk})
        t.refresh_from_db()
        self.assertEqual(t.status, 'requested')
        pt.approve(t.pk, self.owner)
        self.client.post(reverse('agent_network'), {'action': 'confirm', 'topup': t.pk, 'method': 'cash'})
        t.refresh_from_db()
        self.assertEqual(t.status, 'issued')

    def test_other_agents_cannot_see_my_topups(self):
        link = pt.accept(pt.apply(self.agent, self.owner).pk, self.owner, 20)
        t = pt.request_topup(link, self.day.pk, '1000')
        u2, _ = make_partner('agent', email='other@x.gm', name='Other')
        self.client.force_login(u2)
        self.assertEqual(self.client.get(reverse('partner_topup', args=[t.reference])).status_code, 404)
        u3, _ = make_partner('reseller', email='res@x.gm', name='Res')
        self.client.force_login(u3)
        self.assertEqual(self.client.get(reverse('partner_find')).status_code, 404)

    @override_settings(LANDING_STATS_MIN=1)
    def test_landing_counts_only_wifi_owners(self):
        from core.landing import public_stats
        make_partner('reseller', email='res2@x.gm', name='R2')
        stats = public_stats()
        self.assertEqual(stats[0]['value'], '1')
