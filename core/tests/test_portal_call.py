"""In-app calls from the Wi-Fi pages ring the people the owner picked, through the chat call system."""
import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core import chat, chat_call, portal_call
from core.models import Business, PortalPage
from core.models_chat import ChatMessage
from core.models_team import TeamMember

OFFER = {'type': 'offer', 'sdp': 'v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\n'}
ANSWER = {'type': 'answer', 'sdp': 'v=0\r\no=- 2 1 IN IP4 0.0.0.0\r\n'}


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'pcall'}})
class PortalCallTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('owner', 'o@x.gm', 'pw', first_name='Baboucarr')
        self.biz = Business.objects.create(user=self.owner, business_name='Kunda Net', owner_name='O', phone='7771234',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.staff = User.objects.create_user('fatou', 'f@x.gm', 'pw', first_name='Fatou')
        TeamMember.objects.create(business=self.biz, user=self.staff, is_active=True)
        self.page = PortalPage.objects.create(business=self.biz, name='Login', slug='kunda-login', kind='login', is_published=True,
                                              config={'theme': {}, 'blocks': [{'id': 'c', 'type': 'call', 'hidden': False}]})
        self.api = f'/p/{self.page.slug}/call/api/'

    def post(self, body):
        return json.loads(self.client.post(self.api, json.dumps(body), content_type='text/plain').content)

    def test_owner_answers_when_nobody_picked(self):
        self.assertEqual(portal_call.handlers(self.biz), [self.owner])

    def test_switched_off_without_visible_block(self):
        self.page.config = {'blocks': [{'type': 'call', 'hidden': True}]}; self.page.save()
        self.assertFalse(self.post({'action': 'status'})['open'])
        self.assertFalse(self.post({'action': 'start', 'name': 'Awa'})['ok'])

    def test_rings_picked_people_and_staff_can_answer(self):
        portal_call.settings_for(self.biz).handlers.set([self.staff])
        d = self.post({'action': 'start', 'name': 'Awa', 'phone': '3001122'})
        self.assertTrue(d['ok']); cid, secret = d['id'], d['secret']
        rings = chat_call.rings_for(self.staff)
        self.assertEqual(len(rings), 1); self.assertEqual(rings[0]['kind'], 'portal'); self.assertIn('Awa · 3001122', rings[0]['from'])
        self.assertEqual(chat_call.rings_for(self.owner), [])                     # not picked: does not ring
        self.assertTrue(self.post({'action': 'offer', 'id': cid, 'secret': secret, 'desc': OFFER})['ok'])
        # staff answers through the normal chat call endpoints
        self.client.force_login(self.staff)
        r = json.loads(self.client.post('/chat/call/act/', json.dumps({'action': 'accept', 'id': cid}), content_type='application/json').content)
        self.assertEqual(r['offer'], OFFER)
        self.assertTrue(json.loads(self.client.post('/chat/call/act/', json.dumps({'action': 'answer', 'id': cid, 'desc': ANSWER}), content_type='application/json').content)['ok'])
        st = json.loads(self.client.get(f'/chat/call/poll/?id={cid}').content)['call']
        self.assertEqual(st['state'], 'active'); self.assertIn('Awa', st['peer']['name'])
        self.client.logout()
        g = self.post({'action': 'poll', 'id': cid, 'secret': secret})['call']
        self.assertEqual(g['answer'], ANSWER); self.assertEqual(g['peer']['name'], 'Fatou')
        self.post({'action': 'end', 'id': cid, 'secret': secret})
        note = ChatMessage.objects.filter(thread=chat.team_thread(self.biz)).last()
        self.assertIn('Wi-Fi call from Awa · 3001122', note.body); self.assertIn('answered by Fatou', note.body)

    def test_wrong_secret_sees_nothing(self):
        d = self.post({'action': 'start', 'name': 'Awa'})
        g = self.post({'action': 'poll', 'id': d['id'], 'secret': 'nope'})['call']
        self.assertEqual(g['state'], 'ended')
        self.post({'action': 'offer', 'id': d['id'], 'secret': 'nope', 'desc': OFFER})
        self.assertIsNone(cache.get(chat_call._k(d['id'], 'offer')))

    def test_missed_call_leaves_callback_note(self):
        d = self.post({'action': 'start', 'name': 'Musa', 'phone': '2223344'})
        call = chat_call.get(d['id'])
        chat_call.finish(call, 'missed')
        note = ChatMessage.objects.filter(thread=chat.team_thread(self.biz)).last()
        self.assertIn('Missed Wi-Fi call from Musa · 2223344 — call them back', note.body)
        self.assertTrue(note.system); self.assertIsNone(note.sender)

    def test_rate_limit_per_ip(self):
        for _ in range(portal_call.STARTS_PER_IP):
            d = self.post({'action': 'start', 'name': 'x'})
            self.assertTrue(d['ok']); chat_call.finish(chat_call.get(d['id']), 'cancelled')
        self.assertIn('several times', self.post({'action': 'start', 'name': 'x'})['message'])

    def test_settings_only_team_people(self):
        stranger = User.objects.create_user('stranger', 's@x.gm', 'pw')
        self.client.force_login(self.owner)
        self.client.post('/studio/portal/calls/', {'handlers': [self.staff.pk, stranger.pk], 'online_first': 'on'})
        self.assertEqual(list(portal_call.settings_for(self.biz).handlers.all()), [self.staff])
        html = self.client.get('/studio/portal/').content.decode()
        self.assertIn('id="portal-calls"', html); self.assertIn('Rings now: <b>Fatou</b>', html)

    def test_left_team_stops_ringing(self):
        portal_call.settings_for(self.biz).handlers.set([self.staff])
        TeamMember.objects.filter(user=self.staff).update(is_active=False)
        self.assertEqual(portal_call.handlers(self.biz), [self.owner])

    def test_call_page_renders_with_mic_policy(self):
        r = self.client.get(f'/p/{self.page.slug}/call/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('microphone=(self)', r['Permissions-Policy'])
        self.assertIn('portal-call.js', r.content.decode())

    def test_portal_ctx_has_call_urls(self):
        from core.views_studio import _export_html
        html = _export_html(self.page, base='https://taptapnetwork.com')
        self.assertIn('https://taptapnetwork.com/p/kunda-login/call/', html)
