"""Customer calls reach the phones, a broken call never leaves people "busy", voice messages are only burnt when
they really played, staff see who is calling (MAC address, voucher) and can call back on the Wi-Fi page."""
import json
import time
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core import chat, chat_call, chat_media, mobile_auth, portal_call
from core.models import Business, PortalPage, Voucher, VoucherDeviceBinding
from core.models_chat import ChatImage, ChatMessage
from core.models_portal_call import PortalCaller
from core.models_team import TeamMember

OFFER = {'type': 'offer', 'sdp': 'v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\n'}
ANSWER = {'type': 'answer', 'sdp': 'v=0\r\no=- 2 1 IN IP4 0.0.0.0\r\n'}
WEBM = b'\x1a\x45\xdf\xa3' + b'\x00' * 400
DK = 'device-key-0123456789abcdef'


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'steady'}})
class Base(TestCase):
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
        portal_call.settings_for(self.biz).handlers.set([self.owner, self.staff])

    def post(self, body):
        return json.loads(self.client.post(self.api, json.dumps(body), content_type='text/plain').content)

    def act(self, body):
        return json.loads(self.client.post('/chat/call/act/', json.dumps(body), content_type='application/json').content)

    def phone(self, user):
        dev, token = mobile_auth.create_device(user, name='Tecno', platform='android')
        dev.push_token = f'tok-{user.pk}'
        dev.save(update_fields=['push_token'])
        return dev, token


class RingThePhones(Base):
    def test_phone_in_a_pocket_rings_although_a_colleague_has_taptap_open(self):
        chat.touch(self.owner)                                  # the owner has TapTap open on a computer
        self.phone(self.staff)                                  # Fatou's app is closed, her phone is signed in
        with mock.patch('core.mobile_push.configured', return_value=True), mock.patch('core.mobile_push.send') as send:
            d = self.post({'action': 'start', 'name': 'Awa'})
        call = chat_call.get(d['id'])
        self.assertEqual(set(call['targets']), {self.owner.pk, self.staff.pk})
        self.assertIn([self.staff.pk], [c.args[0] for c in send.call_args_list])

    def test_without_a_phone_only_the_people_with_taptap_open_ring(self):
        chat.touch(self.owner)
        d = self.post({'action': 'start', 'name': 'Awa'})
        self.assertEqual(chat_call.get(d['id'])['targets'], [self.owner.pk])

    def test_push_is_repeated_until_the_phone_says_it_rings(self):
        self.phone(self.staff)
        with mock.patch('core.mobile_push.configured', return_value=True), mock.patch('core.mobile_push.call_ringing') as ring:
            d = self.post({'action': 'start', 'name': 'Awa'})
            call = chat_call.get(d['id'])
            first = ring.call_count
            call['at'] -= 8
            cache.set(chat_call._k(call['id']), call, 600)
            self.post({'action': 'poll', 'id': d['id'], 'secret': d['secret']})      # the customer's page polls
            self.assertGreater(ring.call_count, first)
            self.assertEqual(ring.call_args.kwargs.get('attempt'), 2)
            again = ring.call_count
            self.post({'action': 'poll', 'id': d['id'], 'secret': d['secret']})      # same slot: not again
            self.assertEqual(ring.call_count, again)
            # the phone confirms: no more pushes for it
            for u in call['targets']:
                chat_call.ring_ack(call, u)
            call['at'] -= 10
            cache.set(chat_call._k(call['id']), call, 600)
            self.post({'action': 'poll', 'id': d['id'], 'secret': d['secret']})
            self.assertEqual(ring.call_count, again)

    def test_app_confirms_ringing(self):
        _, token = self.phone(self.staff)
        d = self.post({'action': 'start', 'name': 'Awa'})
        call = chat_call.get(d['id'])
        r = self.client.post('/chat/call/act/', json.dumps({'action': 'ringing', 'id': d['id']}), content_type='application/json',
                             HTTP_AUTHORIZATION=f'Bearer {token}', HTTP_X_REQUESTED_WITH='TapTapApp')
        self.assertTrue(json.loads(r.content)['ok'])
        self.assertTrue(chat_call.ring_acked(call, self.staff.pk))
        self.assertEqual(self.post({'action': 'poll', 'id': d['id'], 'secret': d['secret']})['call']['ringing_on'], 1)

    def test_answered_but_never_connected_does_not_stay_busy(self):
        chat.touch(self.staff)
        d = self.post({'action': 'start', 'name': 'Awa'})
        self.client.force_login(self.staff)
        self.assertTrue(self.act({'action': 'accept', 'id': d['id']})['ok'])
        self.assertTrue(chat_call.busy(self.staff.pk))
        t = cache.get(chat_call._k(d['id'], 'taken'))
        t['at'] -= chat_call.CONNECT_SECONDS + 5                 # her app died before it sent its answer
        cache.set(chat_call._k(d['id'], 'taken'), t, 600)
        self.assertFalse(chat_call.busy(self.staff.pk))
        self.client.logout()
        cache.delete(f'pcall:ip:{self.biz.pk}:127.0.0.1')
        self.assertTrue(self.post({'action': 'start', 'name': 'Musa'})['ok'])      # the next customer rings her again

    def test_live_call_both_sides_gone_ends(self):
        chat.touch(self.staff)
        d = self.post({'action': 'start', 'name': 'Awa'})
        self.post({'action': 'offer', 'id': d['id'], 'secret': d['secret'], 'desc': OFFER})
        self.client.force_login(self.staff)
        self.act({'action': 'accept', 'id': d['id']})
        self.act({'action': 'answer', 'id': d['id'], 'desc': ANSWER})
        call = chat_call.get(d['id'])
        self.assertEqual(chat_call.state(call)[0], 'active')
        old = time.time() - chat_call.HEARTBEAT_LOST - 5
        for u in (call['caller'], self.staff.pk):
            cache.set(chat_call._k(d['id'], f'hb:{u}'), old, 600)
        cache.set(chat_call._k(d['id'], 'live_at'), old, 600)
        self.assertFalse(chat_call.busy(self.staff.pk))
        self.assertEqual(cache.get(chat_call._k(d['id'], 'end')), 'lost')

    def test_one_phone_failing_does_not_end_the_call_for_everyone(self):
        chat.touch(self.owner); chat.touch(self.staff)
        d = self.post({'action': 'start', 'name': 'Awa'})
        self.client.force_login(self.staff)                     # her microphone failed: her app sends "end"
        self.assertTrue(self.act({'action': 'end', 'id': d['id'], 'reason': 'failed'})['ok'])
        call = chat_call.get(d['id'])
        self.assertEqual(chat_call.state(call)[0], 'ringing')
        self.assertEqual(len(chat_call.rings_for(self.owner)), 1)
        self.assertEqual(chat_call.rings_for(self.staff), [])


class VoiceOnlyBurntWhenPlayed(Base):
    def voice(self):
        t = chat.team_thread(self.biz)
        m = ChatMessage.objects.create(thread=t, sender=self.owner, body='', attachment='voice')
        chat_media.store_voice(m, self.biz.pk, WEBM, 'audio/webm', 5)
        return m

    def test_download_alone_does_not_start_the_minute(self):
        m = self.voice()
        self.client.force_login(self.staff)
        r = self.client.get(f'/chat/img/{m.pk}/?listen=2')
        self.assertEqual(r.status_code, 200); self.assertEqual(r.content, WEBM)
        self.assertIsNone(ChatImage.objects.get(message=m).listened_at)       # could not play it? still there
        d = json.loads(self.client.post(f'/chat/img/{m.pk}/played/').content)
        self.assertTrue(d['ok']); self.assertFalse(d['gone']); self.assertLessEqual(d['left'], 60)
        im = ChatImage.objects.get(message=m)
        self.assertIsNotNone(im.listened_at)
        self.assertLessEqual(im.expires_at, timezone.now() + timedelta(seconds=61))

    def test_sender_playing_never_starts_it_and_old_apps_still_work(self):
        m = self.voice()
        self.client.force_login(self.owner)
        self.client.get(f'/chat/img/{m.pk}/?listen=2')
        d = json.loads(self.client.post(f'/chat/img/{m.pk}/played/').content)
        self.assertIsNone(d['left']); self.assertFalse(d['gone'])
        self.client.force_login(self.staff)
        self.client.get(f'/chat/img/{m.pk}/?listen=1')                        # an older app: starts at once, as before
        self.assertIsNotNone(ChatImage.objects.get(message=m).listened_at)

    def test_played_twice_keeps_the_first_deadline(self):
        m = self.voice()
        self.client.force_login(self.staff)
        self.client.post(f'/chat/img/{m.pk}/played/')
        first = ChatImage.objects.get(message=m).expires_at
        self.assertIsNotNone(ChatImage.objects.get(message=m).listened_at)
        self.client.post(f'/chat/img/{m.pk}/played/')
        self.assertEqual(ChatImage.objects.get(message=m).expires_at, first)


class WhoIsCallingAndCallBack(Base):
    MAC = '3C:5A:B4:11:22:33'

    def setUp(self):
        super().setUp()
        self.v = Voucher.objects.create(business=self.biz, code='AVA8E', plan_name='1 Day', duration_minutes=1440,
                                        used_at=timezone.now() - timedelta(hours=2), expires_at=timezone.now() + timedelta(hours=22))

    def start(self, **extra):
        return self.post({'action': 'start', 'name': 'Awa', 'phone': '3001122', 'dk': DK, 'mac': '3c-5a-b4-11-22-33',
                          'ip': '10.5.50.23', **extra})

    def test_staff_see_mac_voucher_and_time_left(self):
        chat.touch(self.staff)
        d = self.start(user='ava8e')
        ring = chat_call.rings_for(self.staff)[0]
        c = ring['customer']
        self.assertEqual(c['mac'], self.MAC); self.assertEqual(c['ip'], '10.5.50.23'); self.assertEqual(c['phone'], '3001122')
        self.assertEqual(c['voucher']['code'], 'AVA8E'); self.assertIn('left', c['voucher']['left'])
        self.assertIn('AVA8E', c['line']); self.assertIn(self.MAC, c['line'])
        self.client.force_login(self.staff)
        self.act({'action': 'accept', 'id': d['id']})
        st = json.loads(self.client.get(f'/chat/call/poll/?id={d["id"]}').content)['call']
        self.assertEqual(st['customer']['mac'], self.MAC)
        self.client.logout()
        g = self.post({'action': 'poll', 'id': d['id'], 'secret': d['secret']})['call']
        self.assertNotIn('customer', g)                           # the customer's own page never gets the record

    def test_voucher_found_by_mac_and_why_they_call(self):
        VoucherDeviceBinding.objects.create(business=self.biz, voucher=self.v, slot_no=1, current_mac=self.MAC)
        Voucher.objects.filter(pk=self.v.pk).update(status='expired', expires_at=timezone.now() - timedelta(minutes=5))
        chat.touch(self.staff)
        self.start()
        c = chat_call.rings_for(self.staff)[0]['customer']
        self.assertEqual(c['voucher']['code'], 'AVA8E')
        self.assertIn('time is up', c['why'])

    def test_one_record_per_phone(self):
        chat.touch(self.staff)
        d = self.start()
        self.post({'action': 'end', 'id': d['id'], 'secret': d['secret']})
        self.post({'action': 'callback', 'name': 'Awa', 'phone': '3001122', 'dk': DK})
        self.assertEqual(PortalCaller.objects.count(), 1)
        c = PortalCaller.objects.get()
        self.assertEqual(c.calls, 2); self.assertEqual(c.mac, self.MAC); self.assertEqual(c.last_kind, 'callback')
        self.assertNotEqual(c.key_hash, DK)                       # only the hash of the page key is kept

    def test_call_back_rings_the_customers_page_and_connects(self):
        self.post({'action': 'callback', 'name': 'Awa', 'phone': '3001122', 'dk': DK, 'mac': self.MAC})
        cust = PortalCaller.objects.get()
        self.assertEqual(self.post({'action': 'inbox', 'dk': DK}), {'ok': True})          # nobody calling yet
        self.assertTrue(portal_call.caller_rows(self.biz)[0]['here'])                     # … but staff see them on the page
        self.client.force_login(self.staff)
        d = self.act({'action': 'start', 'customer': cust.pk})
        self.assertTrue(d['ok'], d); cid = d['call']['id']
        self.assertIn('Awa', d['title'])
        self.assertTrue(self.act({'action': 'offer', 'id': cid, 'desc': OFFER})['ok'])
        self.client.logout()
        # the router's login page only knows the MAC address
        box = self.post({'action': 'inbox', 'mac': self.MAC})
        self.assertEqual(box['ring']['id'], cid); self.assertEqual(box['ring']['from'], 'Kunda Net')
        self.assertEqual(self.post({'action': 'poll', 'id': cid, 'secret': 'wrong'})['call']['state'], 'ended')
        a = self.post({'action': 'accept', 'id': cid, 'secret': DK})
        self.assertEqual(a['offer'], OFFER)
        self.assertTrue(self.post({'action': 'answer', 'id': cid, 'secret': DK, 'desc': ANSWER})['ok'])
        self.assertEqual(self.post({'action': 'poll', 'id': cid, 'mac': self.MAC})['call']['peer']['name'], 'Fatou')
        self.client.force_login(self.staff)
        st = json.loads(self.client.get(f'/chat/call/poll/?id={cid}').content)['call']
        self.assertEqual(st['state'], 'active'); self.assertEqual(st['answer'], ANSWER)
        self.assertIn('Awa', st['peer']['name']); self.assertEqual(st['customer']['mac'], self.MAC)
        self.act({'action': 'end', 'id': cid})
        note = ChatMessage.objects.filter(thread=chat.team_thread(self.biz)).last()
        self.assertIn('Fatou called Awa back on the Wi-Fi page', note.body)
        self.assertEqual(PortalCaller.objects.get().last_outcome, 'answered')
        self.assertFalse(chat_call.busy(self.staff.pk))

    def test_missed_call_back_leaves_we_tried_to_call_you(self):
        self.post({'action': 'callback', 'name': 'Awa', 'phone': '3001122', 'dk': DK})
        cust = PortalCaller.objects.get()
        self.client.force_login(self.staff)
        cid = self.act({'action': 'start', 'customer': cust.pk})['call']['id']
        self.act({'action': 'end', 'id': cid})                    # nobody answered: staff give up
        self.client.logout()
        self.assertEqual(self.post({'action': 'inbox', 'dk': DK})['tried']['by'], 'Fatou')
        self.assertIn('tried to call Awa back', ChatMessage.objects.filter(thread=chat.team_thread(self.biz)).last().body)

    def test_only_the_team_can_call_back(self):
        self.post({'action': 'callback', 'name': 'Awa', 'phone': '3001122', 'dk': DK})
        other = User.objects.create_user('x', 'x@x.gm', 'pw')
        with self.assertRaises(chat.ChatError):
            portal_call.call_back(other, self.biz, PortalCaller.objects.get().pk)
