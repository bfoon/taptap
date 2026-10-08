"""Who else is on this page: avatars on agent, voucher, plan, members and member pages.

Run:  DB_ENGINE=sqlite python manage.py test core.test_page_presence
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import page_presence as pp
from .models import Agent, Business, Voucher, VoucherPlan
from .models_team import TeamMember


@override_settings(AUTH_EMAIL_OTP=False)
class PresenceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('owner', 'baboucarr@x.gm', 'pw12345678', first_name='Baboucarr', last_name='Foon')
        self.b = Business.objects.create(user=self.owner, business_name='TapTap KerrSering', owner_name='B', phone='1',
                                         trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.awa = User.objects.create_user('awa', 'awa.jallow@x.gm', 'pw12345678')
        TeamMember.objects.create(business=self.b, user=self.awa, role='admin')
        self.agent = Agent.objects.create(business=self.b, name='Abdou Salam')
        self.plan = VoucherPlan.objects.create(business=self.b, name='24 HOURS', price=25)
        self.voucher = Voucher.objects.create(business=self.b, code='ABC123', plan_name='24 HOURS')
        self.member = Voucher.objects.create(business=self.b, code='jimmy', login_type='member', plan_name='Monthly')
        self.c1, self.c2 = Client(), Client()
        self.c1.force_login(self.owner); self.c2.force_login(self.awa)

    def ping(self, client, kind='agent', pk=None, **extra):
        pk = self.agent.pk if pk is None else pk
        return client.post(reverse('page_presence_ping'), {'kind': kind, 'id': pk, 'tab': extra.pop('tab', 't1'), **extra})

    def test_two_people_see_each_other(self):
        alone = self.ping(self.c1).json()['viewers']
        self.assertEqual([(v['name'], v['you']) for v in alone], [('Baboucarr Foon', True)])
        both = self.ping(self.c2).json()['viewers']
        self.assertEqual([(v['name'], v['initials'], v['role'], v['you']) for v in both],
                         [('Baboucarr Foon', 'BF', 'Owner', False), ('Awa Jallow', 'AJ', 'Admin', True)])
        self.assertEqual(len({v['color'] for v in both}), 2) if both[0]['color'] != both[1]['color'] else None
        mine = self.ping(self.c1).json()['viewers']
        self.assertEqual([v['name'] for v in mine], ['Awa Jallow', 'Baboucarr Foon'])          # others first

    def test_records_and_businesses_do_not_mix(self):
        self.ping(self.c1, 'agent')
        self.assertEqual(len(self.ping(self.c2, 'voucher', self.voucher.pk).json()['viewers']), 1)
        self.assertEqual(len(self.ping(self.c2, 'agent', self.agent.pk + 1).json()['viewers']), 1)
        other = User.objects.create_user('x', 'x@x.gm', 'pw12345678')
        Business.objects.create(user=other, business_name='X', owner_name='X', phone='1', trial_ends_at=timezone.now() + timedelta(days=9))
        c3 = Client(); c3.force_login(other)
        self.assertEqual([v['you'] for v in self.ping(c3, 'agent').json()['viewers']], [True])     # same agent id, other business

    def test_leave_tabs_and_expiry(self):
        self.ping(self.c1, tab='a'); self.ping(self.c1, tab='b'); self.ping(self.c2)
        self.c1.post(reverse('page_presence_leave'), {'kind': 'agent', 'id': self.agent.pk, 'tab': 'a'})
        self.assertEqual(len(self.ping(self.c2).json()['viewers']), 2)                     # second tab still open
        self.c1.post(reverse('page_presence_leave'), {'kind': 'agent', 'id': self.agent.pk, 'tab': 'b'})
        self.assertEqual([v['you'] for v in self.ping(self.c2).json()['viewers']], [True])
        self.ping(self.c1)
        later = pp.time.time() + pp.ACTIVE_SECONDS + 5
        with mock.patch('core.page_presence.time.time', return_value=later):
            self.assertEqual([v['you'] for v in self.ping(self.c2).json()['viewers']], [True])  # gone quiet = gone

    def test_states(self):
        self.ping(self.c1, 'member', self.member.pk, state='editing', tab='a')
        self.ping(self.c1, 'member', self.member.pk, state='away', tab='b')
        v = self.ping(self.c2, 'member', self.member.pk).json()['viewers'][0]
        self.assertEqual((v['state'], v['tabs']), ('editing', 2))
        self.ping(self.c1, 'member', self.member.pk, state='hacking', tab='a')
        self.assertEqual(self.ping(self.c2, 'member', self.member.pk).json()['viewers'][0]['state'], 'viewing')

    def test_bad_requests_and_permissions(self):
        for data in ({'kind': 'router', 'id': 1}, {'kind': 'agent', 'id': 'x'}, {'kind': 'agent', 'id': '9' * 20}):
            self.assertEqual(self.c1.post(reverse('page_presence_ping'), data).status_code, 400)
        support = User.objects.create_user('sup', 's@x.gm', 'pw12345678')
        TeamMember.objects.create(business=self.b, user=support, role='voucher_support')
        c = Client(); c.force_login(support)
        self.assertEqual(self.ping(c, 'voucher', self.voucher.pk).status_code, 200)             # can open vouchers
        self.assertEqual(self.ping(c, 'agent').status_code, 400)                                # cannot open agents
        self.assertEqual(Client().post(reverse('page_presence_ping'), {'kind': 'agent', 'id': 1}).status_code, 302)

    def test_pages_carry_the_presence_strip(self):
        for url, kind, pk in ((reverse('agent_detail', args=[self.agent.pk]), 'agent', self.agent.pk),
                              (reverse('voucher_detail', args=[self.voucher.pk]), 'voucher', self.voucher.pk),
                              (reverse('plan_detail', args=[self.plan.pk]), 'plan', self.plan.pk),
                              (reverse('members'), 'members', 0),
                              (reverse('member_detail', args=[self.member.pk]), 'member', self.member.pk)):
            html = self.c1.get(url).content.decode()
            self.assertIn(f'id="ttPresence" data-kind="{kind}" data-id="{pk}"', html, url)
            self.assertIn('js/presence.js', html)
        self.assertNotIn('ttPresence', self.c1.get(reverse('vouchers')).content.decode())       # only on those pages
