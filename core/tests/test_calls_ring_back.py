"""Customer calls that do not end up as just "missed": they ring 60 s, from a background tab too (the ring
watcher), widen to everyone picked when nobody picks up, and a missed call says why and offers a one-tap
call-back on the customer's Wi-Fi page — which, when it is missed in turn, routes their return call to the
person who tried."""
import json
import time
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core import chat, chat_call, portal_call
from core.models import Business, PortalPage, Voucher, VoucherDeviceBinding
from core.models_chat import ChatMessage
from core.models_portal_call import PortalCaller
from core.models_team import TeamMember

OFFER = {'type': 'offer', 'sdp': 'v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\n'}
ANSWER = {'type': 'answer', 'sdp': 'v=0\r\no=- 2 1 IN IP4 0.0.0.0\r\n'}
DK = 'device-key-0123456789abcdef'
MAC = 'AA:BB:CC:11:22:33'


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'ringback'}})
class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('owner', 'o@x.gm', 'pw', first_name='Baboucarr')
        self.biz = Business.objects.create(user=self.owner, business_name='Kunda Net', owner_name='O', phone='7771234',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.staff = User.objects.create_user('fatou', 'f@x.gm', 'pw', first_name='Fatou')
        TeamMember.objects.create(business=self.biz, user=self.staff, is_active=True)
        self.musa = User.objects.create_user('musa', 'm@x.gm', 'pw', first_name='Musa')
        TeamMember.objects.create(business=self.biz, user=self.musa, is_active=True)
        self.page = PortalPage.objects.create(business=self.biz, name='Login', slug='kunda-login', kind='login', is_published=True,
                                              config={'theme': {}, 'blocks': [{'id': 'c', 'type': 'call', 'hidden': False}]})
        self.api = f'/p/{self.page.slug}/call/api/'
        portal_call.settings_for(self.biz).handlers.set([self.owner, self.staff])

    def post(self, body):
        return json.loads(self.client.post(self.api, json.dumps(body), content_type='text/plain').content)

    def act(self, body):
        return json.loads(self.client.post('/chat/call/act/', json.dumps(body), content_type='application/json').content)

    def start(self, **kw):
        body = {'action': 'start', 'name': 'Awa', 'phone': '3001122', 'dk': DK, 'mac': MAC}
        body.update(kw)
        d = self.post(body)
        self.assertTrue(d['ok'], d)
        return d

    def age(self, cid, seconds):
        """Make the call `seconds` older (the engine reads its age from call['at'])."""
        call = chat_call.get(cid)
        call['at'] -= seconds
        cache.set(chat_call._k(cid), call, chat_call.CALL_TTL)
        return call

    def last_note(self):
        return ChatMessage.objects.filter(thread=chat.team_thread(self.biz)).last()


class RingsLonger(Base):
    def test_a_customer_call_rings_60_seconds(self):
        d = self.start()
        call = chat_call.get(d['id'])
        self.assertEqual(chat_call.ring_secs(call), 60)
        self.assertGreaterEqual(chat_call.rings_for(self.owner)[0]['left'], 58)
        self.age(d['id'], 45)
        self.assertEqual(chat_call.state(chat_call.get(d['id']))[0], 'ringing')          # a chat call would be over
        self.age(d['id'], 16)
        self.assertEqual(chat_call.state(chat_call.get(d['id']))[0], 'ended')

    def test_the_phone_push_lives_as_long_as_the_ring(self):
        from core import mobile_push
        with mock.patch.object(mobile_push, 'send') as send:
            mobile_push.call_ringing(self.owner.pk, {'id': 'x', 'kind': 'portal', 'ring': 60, 'label': 'Awa'})
        self.assertEqual(send.call_args.args[1]['android']['ttl'], '60s')

    def test_chat_calls_keep_40_seconds(self):
        self.assertEqual(chat_call.ring_secs({'id': 'x'}), 40)


class Widens(Base):
    def test_nobody_picks_up_then_everyone_picked_rings(self):
        chat.touch(self.owner)                     # "online first": only the owner (TapTap open somewhere) rings
        d = self.start()
        self.assertEqual(chat_call.get(d['id'])['targets'], [self.owner.pk])
        self.assertEqual(chat_call.rings_for(self.staff), [])
        self.post({'action': 'poll', 'id': d['id'], 'secret': d['secret']})
        self.assertEqual(chat_call.rings_for(self.staff), [])                            # not yet: 15 s first
        self.age(d['id'], 16)
        self.post({'action': 'poll', 'id': d['id'], 'secret': d['secret']})
        self.assertEqual(len(chat_call.rings_for(self.staff)), 1)
        self.assertEqual(set(chat_call.targets(chat_call.get(d['id']))), {self.owner.pk, self.staff.pk})
        self.assertEqual(chat_call.rings_for(self.musa), [])                             # not picked: never rings
        # the one added later answers like anyone else, and the others stop ringing
        self.client.force_login(self.staff)
        self.post({'action': 'offer', 'id': d['id'], 'secret': d['secret'], 'desc': OFFER})
        self.assertEqual(self.act({'action': 'accept', 'id': d['id']})['offer'], OFFER)
        self.assertEqual(chat_call.rings_for(self.owner), [])

    def test_widens_once(self):
        chat.touch(self.owner)
        d = self.start()
        call = self.age(d['id'], 20)
        self.assertEqual(portal_call.widen(call), 1)
        self.assertEqual(portal_call.widen(chat_call.get(d['id'])), 0)

    def test_a_widened_call_that_nobody_answers_clears_every_ring(self):
        chat.touch(self.owner)
        d = self.start()
        self.age(d['id'], 20)
        self.post({'action': 'poll', 'id': d['id'], 'secret': d['secret']})
        self.age(d['id'], 45)
        self.post({'action': 'poll', 'id': d['id'], 'secret': d['secret']})
        self.assertEqual(chat_call.rings_for(self.staff), [])
        self.assertFalse(chat_call.busy(self.staff.pk))


class MissedSaysWhy(Base):
    def test_missed_note_says_who_it_rang_and_how(self):
        d = self.start()
        chat_call.rings_for(self.owner)                     # the owner's TapTap showed the ring; Fatou's was not open
        self.age(d['id'], 61)
        self.post({'action': 'poll', 'id': d['id'], 'secret': d['secret']})
        n = self.last_note()
        self.assertIn('Missed Wi-Fi call from Awa · 3001122 — call them back', n.body)
        self.assertIn('Rang 60 s: ', n.body)
        self.assertIn('Baboucarr (TapTap rang)', n.body)
        self.assertIn('Fatou (TapTap not open)', n.body)

    def test_a_phone_that_rang_and_one_that_declined(self):
        d = self.start()
        call = chat_call.get(d['id'])
        chat_call.ring_ack(call, self.staff.pk)
        self.client.force_login(self.owner)
        self.act({'action': 'decline', 'id': d['id']})
        self.age(d['id'], 61)
        chat_call.state(chat_call.get(d['id']))
        body = self.last_note().body
        self.assertIn('Fatou (phone rang)', body)
        self.assertIn('Baboucarr (declined)', body)

    def test_hung_up_early(self):
        d = self.start()
        self.age(d['id'], 6)
        self.post({'action': 'end', 'id': d['id'], 'secret': d['secret']})
        self.assertIn('They hung up after 6 s', self.last_note().body)

    def test_note_carries_the_customer_for_one_tap_call_back(self):
        d = self.start()
        self.age(d['id'], 61)
        chat_call.state(chat_call.get(d['id']))
        n = self.last_note()
        cust = PortalCaller.objects.get()
        self.assertEqual(n.meta['pcall']['kind'], 'missed')
        self.assertEqual(n.meta['pcall']['cust'], cust.pk)
        self.assertEqual(n.meta['pcall']['phone'], '3001122')
        self.assertEqual(n.meta['pcall']['wa'], '2203001122')
        j = chat.message_json(n, self.owner.pk)
        self.assertEqual(j['call']['cust'], cust.pk)
        self.assertFalse(j['call']['here'])
        self.post({'action': 'inbox', 'dk': DK})                           # their page is open now
        self.assertTrue(chat.message_json(n, self.owner.pk)['call']['here'])

    def test_call_me_back_note_carries_the_customer(self):
        self.post({'action': 'callback', 'name': 'Awa', 'phone': '3001122', 'dk': DK})
        n = self.last_note()
        self.assertEqual(n.meta['pcall']['kind'], 'callback')
        self.assertEqual(n.meta['pcall']['cust'], PortalCaller.objects.get().pk)

    def test_ordinary_messages_have_no_call(self):
        m = chat.post(chat.team_thread(self.biz), self.owner, 'hello')
        self.assertIsNone(chat.message_json(m, self.owner.pk)['call'])


class CallBack(Base):
    def missed_call_back(self):
        d = self.start()
        self.post({'action': 'end', 'id': d['id'], 'secret': d['secret']})
        cust = PortalCaller.objects.get()
        self.client.force_login(self.musa)                 # Musa is not picked to answer, but he calls back
        r = self.act({'action': 'start', 'customer': cust.pk})
        self.assertTrue(r['ok'], r)
        self.assertFalse(r['here'])                        # the call window says their page is not open
        self.act({'action': 'end', 'id': r['call']['id']})
        self.client.logout()
        return cust

    def test_call_back_says_when_their_page_is_open(self):
        self.start()
        cust = PortalCaller.objects.get()
        self.post({'action': 'inbox', 'dk': DK})
        self.client.force_login(self.staff)
        self.assertTrue(self.act({'action': 'start', 'customer': cust.pk})['here'])

    def test_their_return_call_rings_the_person_who_tried_first(self):
        self.missed_call_back()
        self.assertEqual(self.post({'action': 'inbox', 'dk': DK})['tried']['by'], 'Musa')
        d = self.start()
        call = chat_call.get(d['id'])
        self.assertEqual(call['targets'][0], self.musa.pk)
        self.assertEqual(call['returning'], 'Musa')
        self.assertTrue(call['label'].startswith('↩ Awa'))
        ring = chat_call.rings_for(self.musa)[0]
        self.assertEqual(ring['returning'], 'Musa')
        self.assertIn('Returning Musa', ring['customer']['why'])

    def test_a_connected_return_call_clears_we_tried(self):
        self.missed_call_back()
        d = self.start()
        self.client.force_login(self.musa)
        self.post({'action': 'offer', 'id': d['id'], 'secret': d['secret'], 'desc': OFFER})
        self.act({'action': 'accept', 'id': d['id']})
        self.act({'action': 'answer', 'id': d['id'], 'desc': ANSWER})
        self.act({'action': 'end', 'id': d['id']})
        self.client.logout()
        self.assertIn('returning Musa’s call', self.last_note().body)
        self.assertNotIn('tried', self.post({'action': 'inbox', 'dk': DK}))

    def test_tried_lasts_two_hours(self):
        self.assertEqual(portal_call.TRIED_SECONDS, 2 * 3600)

    def test_old_tried_entries_still_read(self):
        cust = PortalCaller.objects.create(business=self.biz, key_hash='x', name='Awa')
        cache.set(f'pcall:tried:{self.biz.pk}:{cust.pk}', 'Fatou', 60)
        self.assertEqual(portal_call._tried(self.biz.pk, cust.pk), {'by': 'Fatou', 'uid': None})


class FromTheVoucherPage(Base):
    def setUp(self):
        super().setUp()
        self.v = Voucher.objects.create(business=self.biz, code='AVA8E', plan_name='Day', price=10)

    def test_found_by_the_code_it_signed_in_with(self):
        d = self.start(user='AVA8E')
        self.post({'action': 'end', 'id': d['id'], 'secret': d['secret']})
        rows = portal_call.callers_for_voucher(self.biz, self.v)
        self.assertEqual([r['name'] for r in rows], ['Awa'])
        self.assertEqual(rows[0]['last'], 'missed call')
        self.assertEqual(rows[0]['wa'], '2203001122')

    def test_found_by_a_device_slots_mac(self):
        self.start()
        VoucherDeviceBinding.objects.create(business=self.biz, voucher=self.v, slot_no=1, current_mac=MAC.lower())
        self.assertEqual(len(portal_call.callers_for_voucher(self.biz, self.v)), 1)

    def test_voucher_page_offers_the_call(self):
        self.start(user='AVA8E')
        self.client.force_login(self.owner)
        r = self.client.get(f'/vouchers/{self.v.pk}/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Call on Wi-Fi page')
        self.assertContains(r, f'data-wcall="{PortalCaller.objects.get().pk}"')

    def test_no_card_for_a_voucher_nobody_called_about(self):
        self.client.force_login(self.owner)
        self.assertNotContains(self.client.get(f'/vouchers/{self.v.pk}/'), 'data-wcall')

    def test_member_page_offers_the_call(self):
        m = Voucher.objects.create(business=self.biz, code='awa.k', password='x', login_type='member', plan_name='Gold',
                                   source='taptap')
        self.start(user='awa.k')
        self.client.force_login(self.owner)
        r = self.client.get(f'/members/{m.pk}/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Call on Wi-Fi page')
