from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, Client
from django.utils import timezone

from .models import Agent, Business, Voucher
from .models_events import EventAlert


class AgentPortalTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o', 'o@x.com', 'pw12345678')
        self.b = Business.objects.create(user=self.owner, business_name='Kairaba Net', owner_name='A', phone='+220 700 0000',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.agent = Agent.objects.create(business=self.b, name='Awa Shop', phone='777')
        self.tok = self.agent.ensure_portal_token()
        self.unused = Voucher.objects.create(business=self.b, code='GOOD1234', plan_name='1 Day', duration_minutes=1440)
        self.inuse = Voucher.objects.create(business=self.b, code='BUSY1234', plan_name='1 Day', duration_minutes=1440, used_at=timezone.now() - timedelta(hours=2))
        self.done = Voucher.objects.create(business=self.b, code='DONE1234', plan_name='1 Hour', duration_minutes=60, used_at=timezone.now() - timedelta(days=2))
        self.c = Client()            # the agent is not logged in

    def check(self, code):
        return self.c.get(f'/ag/{self.tok}/', {'code': code})

    def test_three_answers(self):
        r = self.check('good1234')
        self.assertContains(r, 'class="card green"'); self.assertContains(r, 'Ask for help')
        r = self.check('BUSY1234')
        self.assertContains(r, 'class="card yellow"'); self.assertContains(r, 'Do not take it back'); self.assertContains(r, 'tel:+220 700 0000')
        r = self.check('DONE1234')
        self.assertContains(r, 'class="card red"'); self.assertContains(r, '>END<')
        self.assertContains(self.check('NOPE0000'), 'class="card grey"')

    def test_scanned_voucher_qr_with_a_link(self):
        self.assertContains(self.check('http://10.5.50.1/login?username=GOOD1234&password=GOOD1234'), 'class="card green"')

    def test_help_alerts_the_team_once(self):
        self.c.get(f'/ag/{self.tok}/')                                        # sets the CSRF cookie
        r = self.c.post(f'/ag/{self.tok}/help/', {'code': 'GOOD1234'})
        self.assertContains(r, 'Help is on the way')
        a = EventAlert.objects.get()
        self.assertEqual(a.link, f'/vouchers/{self.unused.pk}/'); self.assertIn('Awa Shop', a.title); self.assertTrue(a.sound and a.desktop)
        self.c.post(f'/ag/{self.tok}/help/', {'code': 'GOOD1234'})
        self.assertEqual(EventAlert.objects.count(), 1)                        # not spammed
        self.c.post(f'/ag/{self.tok}/help/', {'code': 'BUSY1234'})
        self.assertEqual(EventAlert.objects.count(), 1)                        # help only from the green card

    def test_bad_link_and_rate_limit_and_new_qr(self):
        self.assertEqual(self.c.get('/ag/not-a-real-token-at-all/').status_code, 404)
        cache.set(f'ap:{self.agent.pk}', 10_000, 3600)
        self.assertEqual(self.check('GOOD1234').status_code, 429)
        owner = Client(); owner.force_login(self.owner)
        page = owner.get(f'/finance/agents/{self.agent.pk}/')
        self.assertContains(page, f'/ag/{self.tok}/')
        owner.post(f'/finance/agents/{self.agent.pk}/qr/')
        self.agent.refresh_from_db()
        self.assertNotEqual(self.agent.portal_token, self.tok)
        self.assertEqual(self.c.get(f'/ag/{self.tok}/').status_code, 404)     # old QR no longer works


class HelpLineTests(AgentPortalTests):
    def test_help_line_then_agent_number(self):
        self.b.support_phone = '+220 300 1111'; self.b.save()
        self.assertContains(self.check('BUSY1234'), 'tel:+220 300 1111')           # customer help line wins over the business phone
        owner = Client(); owner.force_login(self.owner)
        owner.post(f'/finance/agents/{self.agent.pk}/checker-phone/', {'customer_phone': '+220 999 2222<script>'})
        self.agent.refresh_from_db(); self.assertEqual(self.agent.customer_phone, '+220 999 2222')
        self.assertContains(self.check('BUSY1234'), 'tel:+220 999 2222')            # the number you entered for this agent
        owner.post(f'/finance/agents/{self.agent.pk}/checker-phone/', {'customer_phone': ''})
        self.assertContains(self.check('BUSY1234'), 'tel:+220 300 1111')            # empty again: back to the help line


class AgentLogsAndOrdersTests(AgentPortalTests):
    def test_checks_and_help_are_logged_and_shown(self):
        from .models import AgentCheck, AgentHelp
        self.check('GOOD1234'); self.check('BUSY1234'); self.check('ZZZZ9999')
        self.assertEqual(sorted(AgentCheck.objects.values_list('result', flat=True)), ['in_use', 'unknown', 'unused'])
        self.c.get(f'/ag/{self.tok}/')
        self.c.post(f'/ag/{self.tok}/help/', {'code': 'GOOD1234'})
        h = AgentHelp.objects.get()
        owner = Client(); owner.force_login(self.owner)
        page = owner.get(f'/finance/agents/{self.agent.pk}/')
        self.assertContains(page, 'Voucher checks'); self.assertContains(page, 'In use — told not to take back'); self.assertContains(page, 'ZZZZ9999')
        owner.post(f'/finance/agents/{self.agent.pk}/log/', {'action': 'help_done', 'id': h.pk, 'note': 'called her'})
        h.refresh_from_db(); self.assertEqual((h.handled_by, h.note), (self.owner, 'called her'))

    def test_order_to_vouchers(self):
        from .models import AgentOrder, VoucherPlan
        from .models_events import EventAlert
        plan = VoucherPlan.objects.create(business=self.b, name='1 Day', price=25, duration_minutes=1440)
        self.assertContains(self.c.get(f'/ag/{self.tok}/order/'), 'Send order')
        r = self.c.post(f'/ag/{self.tok}/order/', {'plan': plan.pk, 'quantity': '20', 'note': 'for market day'})
        self.assertContains(r, 'Order sent')
        self.c.post(f'/ag/{self.tok}/order/', {'plan': plan.pk, 'quantity': '20'})        # double tap
        o = AgentOrder.objects.get()
        self.assertEqual((o.quantity, o.plan_name, o.note, o.status), (20, '1 Day', 'for market day', 'new'))
        a = EventAlert.objects.get(kind='agent_order')
        self.assertIn(f'order={o.pk}', a.link); self.assertIn('Awa Shop', a.title)
        owner = Client(); owner.force_login(self.owner)
        page = owner.get(a.link)
        self.assertContains(page, 'Making an agent'); self.assertContains(page, 'value="20"')
        owner.post('/vouchers/generate/', {'plan': plan.pk, 'quantity': 20, 'agent': self.agent.pk, 'settlement': 'credit', 'order': o.pk})
        o.refresh_from_db()
        self.assertEqual(o.status, 'done'); self.assertIsNotNone(o.batch)
        self.assertContains(self.c.get(f'/ag/{self.tok}/order/'), '✅ Ready')                # the agent sees it

    def test_decline(self):
        from .models import AgentOrder, VoucherPlan
        plan = VoucherPlan.objects.create(business=self.b, name='1 Day', price=25, duration_minutes=1440)
        self.c.post(f'/ag/{self.tok}/order/', {'plan': plan.pk, 'quantity': '5'})
        o = AgentOrder.objects.get()
        owner = Client(); owner.force_login(self.owner)
        owner.post(f'/finance/agents/{self.agent.pk}/log/', {'action': 'order_decline', 'id': o.pk})
        o.refresh_from_db(); self.assertEqual(o.status, 'rejected')
        self.assertContains(self.c.get(f'/ag/{self.tok}/order/'), '❌ Declined')


class OrderPlanLimitTests(AgentPortalTests):
    def test_agent_only_sees_and_orders_allowed_plans(self):
        from .models import AgentOrder, VoucherPlan
        day = VoucherPlan.objects.create(business=self.b, name='1 Day', price=25, duration_minutes=1440)
        month = VoucherPlan.objects.create(business=self.b, name='1 Month', price=400, duration_minutes=43200)
        page = self.c.get(f'/ag/{self.tok}/order/')
        self.assertContains(page, '1 Month'); self.assertContains(page, '1 Day')          # none ticked: every plan
        owner = Client(); owner.force_login(self.owner)
        owner.post(f'/finance/agents/{self.agent.pk}/order-plans/', {'plans': [day.pk]})
        page = self.c.get(f'/ag/{self.tok}/order/')
        self.assertContains(page, '1 Day'); self.assertNotContains(page, '1 Month')
        self.c.post(f'/ag/{self.tok}/order/', {'plan': month.pk, 'quantity': 5})          # not allowed: refused
        self.assertFalse(AgentOrder.objects.exists())
        self.c.post(f'/ag/{self.tok}/order/', {'plan': day.pk, 'quantity': 5})
        self.assertEqual(AgentOrder.objects.get().plan, day)
        self.assertContains(owner.get(f'/finance/agents/{self.agent.pk}/'), 'Plans Awa Shop can order')


class OrderAmountTests(AgentPortalTests):
    def test_default_and_fixed_amount(self):
        from .models import AgentOrder, VoucherPlan
        day = VoucherPlan.objects.create(business=self.b, name='1 Day', price=25, duration_minutes=1440)
        owner = Client(); owner.force_login(self.owner)
        owner.post(f'/finance/agents/{self.agent.pk}/order-plans/', {'order_quantity': '50'})
        self.assertContains(self.c.get(f'/ag/{self.tok}/order/'), 'value="50"')                 # starts at 50, can change
        self.c.post(f'/ag/{self.tok}/order/', {'plan': day.pk, 'quantity': 30})
        self.assertEqual(AgentOrder.objects.get().quantity, 30)
        owner.post(f'/finance/agents/{self.agent.pk}/order-plans/', {'order_quantity': '40', 'order_quantity_fixed': 'on'})
        page = self.c.get(f'/ag/{self.tok}/order/')
        self.assertContains(page, 'Your order is always 40'); self.assertNotContains(page, 'data-d="1"')
        self.c.post(f'/ag/{self.tok}/order/', {'plan': day.pk, 'quantity': 999})                 # tries another number
        self.assertEqual(AgentOrder.objects.order_by('-pk').first().quantity, 40)
