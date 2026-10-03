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
