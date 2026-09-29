import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, Client
from django.utils import timezone

from . import chat
from .models import Business
from .models_chat import ChatMessage, ChatPrefs, ChatThread, SupportAgent
from .models_team import TeamMember


class ChatTests(TestCase):
    def setUp(self):
        cache.clear()
        def biz(n):
            u = User.objects.create_user(f'owner{n}', f'o{n}@x.com', 'pw12345678', first_name=f'Owner{n}')
            return u, Business.objects.create(user=u, business_name=f'Biz {n}', owner_name='A', phone='1',
                                              trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.owner, self.b = biz(1)
        self.other_owner, self.b2 = biz(2)
        self.staff = User.objects.create_user('awa', 'awa@x.com', 'pw12345678', first_name='Awa')
        TeamMember.objects.create(business=self.b, user=self.staff, role='viewer')
        self.agent = User.objects.create_user('sup', 'sup@taptap.com', 'pw12345678', first_name='Sara')
        SupportAgent.objects.create(user=self.agent)

    def client_for(self, u):
        c = Client(); c.force_login(u); return c

    def post(self, c, url, data):
        return c.post(url, json.dumps(data), content_type='application/json').json()

    def test_team_room_and_direct(self):
        c1, c2 = self.client_for(self.owner), self.client_for(self.staff)
        st = c1.get('/chat/state/').json()
        team = next(t for t in st['threads'] if t['kind'] == 'team')
        self.assertTrue(self.post(c1, '/chat/send/', {'thread': team['id'], 'body': 'Hello @Awa'})['ok'])
        st2 = c2.get('/chat/state/').json()
        t2 = next(t for t in st2['threads'] if t['kind'] == 'team')
        self.assertEqual(t2['unread'], 1)
        self.assertEqual(ChatMessage.objects.get().mentions, [self.staff.pk])
        d = self.post(c2, '/chat/direct/', {'user': self.owner.pk})
        self.assertTrue(d['ok'])
        # another business cannot open these threads
        c3 = self.client_for(self.other_owner)
        self.assertFalse(self.post(c3, '/chat/send/', {'thread': team['id'], 'body': 'hi'})['ok'])
        self.assertFalse(self.post(c3, '/chat/direct/', {'user': self.owner.pk})['ok'])
        self.assertEqual(c3.get(f'/chat/t/{d["thread"]}/').status_code, 403)

    def test_support_inbox(self):
        c1, ca = self.client_for(self.owner), self.client_for(self.agent)
        st = c1.get('/chat/state/').json()
        sup = next(t for t in st['threads'] if t['kind'] == 'support')
        self.assertEqual(sup['title'], 'TapTap Support')
        self.post(c1, '/chat/send/', {'thread': sup['id'], 'body': 'My router is offline', 'page_url': '/routers/', 'page_title': 'Routers'})
        # agent without a business lands in the inbox and sees the conversation
        self.assertRedirects(ca.get('/dashboard/'), '/chat/', fetch_redirect_response=False)
        st = ca.get('/chat/state/').json()
        inbox = [t for t in st['threads'] if t['inbox']]
        self.assertEqual([t['title'] for t in inbox], ['Biz 1'])
        self.assertTrue(self.post(ca, '/chat/send/', {'thread': sup['id'], 'body': 'Looking now'})['ok'])
        t = ChatThread.objects.get(pk=sup['id'])
        self.assertEqual(t.assigned_to, self.agent); self.assertEqual(t.support_status, 'open')
        self.assertTrue(self.post(ca, '/chat/support/', {'thread': t.pk, 'action': 'solve'})['ok'])
        msgs = c1.get(f'/chat/t/{t.pk}/').json()['messages']
        self.assertTrue(any(m['agent'] for m in msgs))
        # business owner cannot mark solved; another business cannot read it
        self.assertFalse(self.post(c1, '/chat/support/', {'thread': t.pk, 'action': 'solve'})['ok'])
        self.assertEqual(self.client_for(self.other_owner).get(f'/chat/t/{t.pk}/').status_code, 403)

    def test_new_messages_poll_and_email(self):
        c1, c2 = self.client_for(self.owner), self.client_for(self.staff)
        team = chat.team_thread(self.b)
        top = c2.get('/chat/state/').json()['top']
        self.post(c1, '/chat/send/', {'thread': team.pk, 'body': 'Stock is low'})
        new = c2.get(f'/chat/state/?since={top}').json()['new']
        self.assertEqual([m['body'] for m in new], ['Stock is low'])
        # missed-message email once they are away and the delay has passed
        cache.delete(f'chat:seen:{self.staff.pk}')
        ChatMessage.objects.update(created_at=timezone.now() - timedelta(minutes=20))
        self.assertEqual(chat.email_missed(), 1)
        self.assertEqual(mail.outbox[-1].to, ['awa@x.com'])
        self.assertEqual(chat.email_missed(), 0)          # not twice
        ChatPrefs.objects.filter(user=self.owner).update(email_missed=False)

    def test_pages_render(self):
        for u in (self.owner, self.staff, self.agent):
            self.assertEqual(self.client_for(u).get('/chat/').status_code, 200)
        su = User.objects.create_superuser('root', 'root@x.com', 'pw12345678')
        cs = self.client_for(su)
        self.assertEqual(cs.get('/platform/support-team/').status_code, 200)
        cs.post('/platform/support-team/', {'action': 'add', 'email': 'awa@x.com'})
        self.assertTrue(chat.is_agent(self.staff))
        self.assertEqual(self.client_for(self.owner).get('/platform/support-team/').status_code, 403)
