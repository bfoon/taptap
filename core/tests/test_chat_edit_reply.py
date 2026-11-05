"""Chat: reply to a message, edit your own message, delete it within an hour — and the others see it change."""
import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core import chat
from core.models import Business
from core.models_chat import ChatImage, ChatMessage
from core.models_team import TeamMember


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'chat-er'}})
class ChatEditReplyDeleteTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456', first_name='Baboucarr')
        self.biz = Business.objects.create(user=self.owner, business_name='Kunda Net', owner_name='O', phone='7771234',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.musa = User.objects.create_user('m@x.gm', 'm@x.gm', 'pw-123456', first_name='Musa')
        TeamMember.objects.create(business=self.biz, user=self.musa, is_active=True, role='viewer')
        self.t = chat.team_thread(self.biz)

    def act(self, user, body):
        self.client.force_login(user)
        return json.loads(self.client.post('/chat/message/', json.dumps(body), content_type='application/json').content)

    # ── reply ──
    def test_reply_is_quoted(self):
        a = chat.post(self.t, self.musa, 'Router 3 is down')
        b = chat.post(self.t, self.owner, 'On my way', reply_to=a.pk)
        d = chat.message_json(b, self.owner.pk)
        self.assertEqual(d['reply']['id'], a.pk)
        self.assertEqual(d['reply']['text'], 'Router 3 is down')
        self.assertEqual(d['reply']['sender'], chat.display_name(self.musa))

    def test_reply_through_send(self):
        a = chat.post(self.t, self.musa, 'Hello')
        self.client.force_login(self.owner)
        d = json.loads(self.client.post('/chat/send/', json.dumps({'thread': self.t.pk, 'body': 'Hi', 'reply_to': a.pk}),
                                        content_type='application/json').content)
        self.assertTrue(d['ok'], d)
        self.assertEqual(d['message']['reply']['id'], a.pk)

    def test_reply_to_another_conversation_is_refused(self):
        other = chat.direct_thread(self.biz, self.owner, self.musa)
        a = chat.post(other, self.musa, 'private')
        with self.assertRaises(chat.ChatError):
            chat.post(self.t, self.owner, 'x', reply_to=a.pk)

    # ── edit ──
    def test_edit_own_message(self):
        m = chat.post(self.t, self.owner, 'Price is D25')
        d = self.act(self.owner, {'id': m.pk, 'action': 'edit', 'body': 'Price is D30'})
        self.assertTrue(d['ok'], d)
        self.assertEqual(d['message']['body'], 'Price is D30')
        self.assertTrue(d['message']['edited'])
        m.refresh_from_db()
        self.assertIsNotNone(m.changed_at)

    def test_cannot_edit_someone_elses(self):
        m = chat.post(self.t, self.musa, 'mine')
        d = self.act(self.owner, {'id': m.pk, 'action': 'edit', 'body': 'hacked'})
        self.assertFalse(d['ok'])
        m.refresh_from_db()
        self.assertEqual(m.body, 'mine')

    def test_edit_later_is_allowed_by_default(self):
        m = chat.post(self.t, self.owner, 'old')
        ChatMessage.objects.filter(pk=m.pk).update(created_at=timezone.now() - timedelta(days=2))
        self.assertTrue(self.act(self.owner, {'id': m.pk, 'action': 'edit', 'body': 'new'})['ok'])

    # ── delete ──
    def test_delete_within_an_hour(self):
        m = chat.post(self.t, self.owner, 'oops wrong chat')
        self.assertIsNotNone(chat.message_json(m, self.owner.pk)['delete_until'])
        d = self.act(self.owner, {'id': m.pk, 'action': 'delete'})
        self.assertTrue(d['ok'], d)
        self.assertTrue(d['message']['deleted'])
        self.assertEqual(d['message']['body'], '')
        m.refresh_from_db()
        self.assertEqual(chat.preview_text(m), '🚫 Message deleted')

    def test_delete_after_an_hour_is_refused(self):
        m = chat.post(self.t, self.owner, 'too late')
        ChatMessage.objects.filter(pk=m.pk).update(created_at=timezone.now() - timedelta(minutes=61))
        m.refresh_from_db()
        self.assertIsNone(chat.message_json(m, self.owner.pk)['delete_until'])
        d = self.act(self.owner, {'id': m.pk, 'action': 'delete'})
        self.assertFalse(d['ok'])
        self.assertIn('1 hour', d['message'])
        m.refresh_from_db()
        self.assertEqual(m.body, 'too late')

    def test_cannot_delete_someone_elses(self):
        m = chat.post(self.t, self.musa, 'keep me')
        self.assertIsNone(chat.message_json(m, self.owner.pk)['delete_until'])
        self.assertFalse(self.act(self.owner, {'id': m.pk, 'action': 'delete'})['ok'])

    def test_delete_removes_the_photo(self):
        m = chat.post(self.t, self.owner, '', attachment='image')
        ChatImage.objects.create(message=m, business=self.biz, data=b'x', mime='image/png', width=1, height=1, size=1,
                                 expires_at=timezone.now() + timedelta(days=7))
        self.assertTrue(self.act(self.owner, {'id': m.pk, 'action': 'delete'})['ok'])
        self.assertFalse(ChatImage.objects.filter(message_id=m.pk).exists())

    def test_reply_to_a_deleted_message_shows_it_deleted(self):
        a = chat.post(self.t, self.musa, 'secret')
        b = chat.post(self.t, self.owner, 'ok', reply_to=a.pk)
        chat.delete(a, self.musa)
        b.refresh_from_db()
        r = chat.message_json(b, self.owner.pk)['reply']
        self.assertTrue(r['deleted'])
        self.assertNotIn('secret', r['text'])

    # ── the others see the change ──
    def test_poll_sends_changes(self):
        self.client.force_login(self.musa)
        first = json.loads(self.client.get('/chat/state/', {'since': -1}).content)
        m = chat.post(self.t, self.owner, 'typo')
        chat.edit(m, self.owner, 'fixed')
        self.client.force_login(self.musa)
        d = json.loads(self.client.get('/chat/state/', {'since': first['top'], 'chg': first['now']}).content)
        self.assertIn(m.pk, [x['id'] for x in d['changed']])
        self.assertEqual(next(x for x in d['changed'] if x['id'] == m.pk)['body'], 'fixed')
        self.assertFalse(next(x for x in d['changed'] if x['id'] == m.pk)['can_edit'])   # not Musa's message
