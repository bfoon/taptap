"""Voice messages: up to 30 seconds, and deleted for good 1 minute after someone first listens — chat (web and
phone app) and the customer Wi-Fi page."""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from core import chat, chat_media, portal_call
from core.models import Business, PortalPage
from core.models_chat import ChatImage, ChatMessage, ChatThread

WEBM = b'\x1a\x45\xdf\xa3' + b'\x9f\x42\x86\x81\x01' + b'webm' + b'\x00' * 900
M4A = b'\x00\x00\x00\x1cftypM4A ' + b'\x00' * 900
OGG = b'OggS' + b'\x00' * 900

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'voice-tests'}}


def up(data=WEBM, name='v.webm', ctype='audio/webm'):
    return SimpleUploadedFile(name, data, ctype)


@override_settings(CACHES=LOCMEM)
class VoiceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.a = User.objects.create_user('a', 'a@x.gm', 'pw', first_name='Awa')
        self.b = User.objects.create_user('b', 'b@x.gm', 'pw', first_name='Baboucarr')
        self.biz = Business.objects.create(user=self.a, business_name='Kunda Net', owner_name='Awa', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.t = ChatThread.objects.create(kind='direct', business=self.biz, user_a=self.a, user_b=self.b)

    def _get(self, m, user, listen=True, **headers):
        from core.views_chat import chat_image
        req = RequestFactory().get(f'/chat/img/{m.pk}/', {'listen': '1'} if listen else {}, **headers)
        req.user = user
        req.tt_business = self.biz
        return chat_image(req, m.pk)

    # ── recording in ──
    def test_sniff(self):
        self.assertEqual(chat_media.sniff_audio(WEBM), 'audio/webm')
        self.assertEqual(chat_media.sniff_audio(M4A), 'audio/mp4')
        self.assertEqual(chat_media.sniff_audio(OGG), 'audio/ogg')
        self.assertEqual(chat_media.sniff_audio(b'<svg onload=alert(1)>' + b'x' * 100), '')

    def test_save_and_json(self):
        m = chat_media.save_voice(self.t, self.a, up(M4A, 'v.m4a', 'audio/mp4'), '12.4')
        im = ChatImage.objects.get(message=m)
        self.assertEqual((m.attachment, im.mime, im.width, im.listened_at), ('voice', 'audio/mp4', 12, None))
        j = chat_media.image_json(m)
        self.assertEqual((j['kind'], j['gone'], j['secs'], j['listened'], j['left']), ('voice', False, 12, None, None))
        self.assertEqual(chat.preview_text(m), '🎙️ Voice message')
        self.assertFalse(chat.can_edit(m, self.a.pk))
        with self.assertRaises(chat.ChatError):
            chat.edit(m, self.a, 'text')

    def test_max_30_seconds(self):
        with self.assertRaises(chat.ChatError):
            chat_media.save_voice(self.t, self.a, up(), '45')
        m = chat_media.save_voice(self.t, self.a, up(), '30.9')       # a recorder that stops a hair late is fine
        self.assertEqual(ChatImage.objects.get(message=m).width, 30)
        with self.assertRaises(chat.ChatError):
            chat_media.save_voice(self.t, self.a, up(), '')             # no length
        with self.assertRaises(chat.ChatError):
            chat_media.save_voice(self.t, self.a, up(), 'nan')

    def test_rejects_non_audio_and_too_big(self):
        with self.assertRaises(chat.ChatError):
            chat_media.save_voice(self.t, self.a, up(b'GIF89a' + b'\0' * 200), '5')
        with mock.patch.object(chat_media, 'VOICE_MAX_BYTES', 500):
            with self.assertRaises(chat.ChatError):
                chat_media.save_voice(self.t, self.a, up(), '5')

    def test_upload_view(self):
        self.client.force_login(self.a)
        with mock.patch('core.views_chat._biz', return_value=self.biz):
            r = self.client.post('/chat/upload/', {'thread': self.t.pk, 'kind': 'voice', 'secs': '7', 'file': up()})
        d = json.loads(r.content)
        self.assertTrue(d['ok'], d)
        self.assertEqual((d['message']['image']['kind'], d['message']['image']['secs']), ('voice', 7))

    # ── listen once, gone a minute later ──
    def test_only_a_real_play_gets_the_bytes(self):
        m = chat_media.save_voice(self.t, self.a, up(), '5')
        r = self._get(m, self.b, listen=False)           # an old app drawing it as a picture
        self.assertEqual(r.status_code, 409)
        self.assertIsNone(ChatImage.objects.get(message=m).listened_at)

    def test_sender_replay_does_not_start_the_clock(self):
        m = chat_media.save_voice(self.t, self.a, up(), '5')
        self.assertEqual(self._get(m, self.a).status_code, 200)
        self.assertIsNone(ChatImage.objects.get(message=m).listened_at)

    def test_first_listen_starts_one_minute_then_gone_everywhere(self):
        m = chat_media.save_voice(self.t, self.a, up(), '5')
        before = ChatMessage.objects.get(pk=m.pk).changed_at
        r = self._get(m, self.b)
        self.assertEqual((r.status_code, r.content, r['Cache-Control']), (200, WEBM, 'no-store'))
        self.assertEqual(r['Accept-Ranges'], 'bytes')
        im = ChatImage.objects.get(message=m)
        self.assertIsNotNone(im.listened_at)
        self.assertAlmostEqual((im.expires_at - im.listened_at).total_seconds(), 60, delta=1)
        m.refresh_from_db()
        self.assertNotEqual(m.changed_at, before)          # every open chat gets the countdown in its next poll
        j = chat_media.image_json(m)
        self.assertTrue(j['listened'])
        self.assertTrue(55 <= j['left'] <= 60)
        # a second listen inside the minute does not move the deadline
        first_deadline = im.expires_at
        self.assertEqual(self._get(m, self.b, HTTP_RANGE='bytes=0-9').status_code, 206)
        self.assertEqual(ChatImage.objects.get(message=m).expires_at, first_deadline)
        # a minute later: gone for good
        later = timezone.now() + timedelta(seconds=61)
        self.assertEqual(chat_media.purge_voice(later), 1)
        self.assertFalse(ChatImage.objects.filter(message=m).exists())
        m.refresh_from_db()
        self.assertEqual(m.attachment, 'voice_gone')
        self.assertEqual(chat_media.image_json(m), {'gone': True, 'opened': False, 'kind': 'voice', 'listened': True})
        self.assertEqual(chat.preview_text(m), '🎙️ Voice message · listened')
        self.assertEqual(self._get(m, self.b).status_code, 410)

    def test_not_purged_inside_the_minute(self):
        m = chat_media.save_voice(self.t, self.a, up(), '5')
        self._get(m, self.b)
        self.assertEqual(chat_media.purge_voice(), 0)
        self.assertTrue(ChatImage.objects.filter(message=m).exists())

    def test_expired_on_open_is_deleted_at_once(self):
        m = chat_media.save_voice(self.t, self.a, up(), '5')
        self._get(m, self.b)
        ChatImage.objects.filter(message=m).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self._get(m, self.b).status_code, 410)
        self.assertFalse(ChatImage.objects.filter(message=m).exists())

    def test_unheard_voice_expires_with_the_other_files(self):
        m = chat_media.save_voice(self.t, self.a, up(), '5')
        ChatImage.objects.filter(message=m).update(expires_at=timezone.now() - timedelta(seconds=1))
        chat_media.purge_expired()
        m.refresh_from_db()
        self.assertEqual(m.attachment, 'voice')
        self.assertEqual(chat_media.image_json(m), {'gone': True, 'opened': False, 'kind': 'voice', 'listened': False})

    def test_poll_sweeps_without_celery(self):
        m = chat_media.save_voice(self.t, self.a, up(), '5')
        self._get(m, self.b)
        ChatImage.objects.filter(message=m).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.client.force_login(self.a)
        first = json.loads(self.client.get('/chat/state/', {'since': -1}).content)
        self.assertFalse(ChatImage.objects.filter(message=m).exists())
        self.assertEqual(ChatMessage.objects.get(pk=m.pk).attachment, 'voice_gone')
        d = json.loads(self.client.get('/chat/state/', {'since': first['top'], 'chg': first['now'] - 5}).content)
        changed = [x for x in d['changed'] if x['id'] == m.pk]
        self.assertEqual(changed[0]['image'], {'gone': True, 'opened': False, 'kind': 'voice', 'listened': True})

    def test_delete_removes_the_recording(self):
        m = chat_media.save_voice(self.t, self.a, up(), '5')
        chat.delete(m, self.a)
        self.assertFalse(ChatImage.objects.filter(message=m).exists())

    def test_beat_task(self):
        from django.conf import settings
        from core import tasks
        self.assertEqual(settings.CELERY_BEAT_SCHEDULE['taptap-chat-purge-voice']['task'], 'core.tasks.chat_purge_voice')
        self.assertEqual(tasks.chat_purge_voice(), 0)


@override_settings(CACHES=LOCMEM)
class PortalVoiceTests(TestCase):
    """A customer on the Wi-Fi page leaves a voice message instead of calling."""
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('pv@x.gm', 'pv@x.gm', 'pw-123456', first_name='Awa')
        self.biz = Business.objects.create(user=self.owner, business_name='Kunda Net', owner_name='O', phone='7771234',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.page = PortalPage.objects.create(business=self.biz, name='Login', slug='pv-login', kind='login', is_published=True,
                                              config={'theme': {}, 'blocks': [{'id': 'c', 'type': 'call', 'hidden': False}]})
        self.url = f'/p/{self.page.slug}/call/voice/'

    def post(self, data=M4A, **q):
        q.setdefault('secs', '9')
        from urllib.parse import urlencode
        r = self.client.post(self.url + '?' + urlencode(q), data, content_type='text/plain')
        return r, json.loads(r.content)

    def test_lands_in_the_team_room_and_goes_after_listening(self):
        r, d = self.post(name='Fatou', phone='+220 777 1234')
        self.assertTrue(d['ok'], d)
        self.assertEqual(r['Access-Control-Allow-Origin'], '*')
        m = ChatMessage.objects.get(thread=chat.team_thread(self.biz), attachment='voice')
        self.assertIsNone(m.sender_id)
        self.assertFalse(m.system)
        self.assertIn('Fatou · +220 777 1234', m.body)
        self.assertEqual(ChatImage.objects.get(message=m).width, 9)
        # the owner listens → a minute later it is gone for good
        got = chat_media.open_image(m, self.owner, listen=True)
        self.assertEqual(got[0], M4A)
        chat_media.purge_voice(timezone.now() + timedelta(seconds=61))
        self.assertFalse(ChatImage.objects.filter(message=m).exists())

    def test_limits(self):
        self.assertFalse(self.post(secs='31.9')[1]['ok'])
        self.assertFalse(self.post(data=b'hello' * 50)[1]['ok'])
        self.assertFalse(self.post(phone='12')[1]['ok'])
        for _ in range(portal_call.VOICE_PER_IP):
            self.assertTrue(self.post()[1]['ok'])
        self.assertFalse(self.post()[1]['ok'])
        with mock.patch.object(chat_media, 'VOICE_MAX_BYTES', 100):
            r, d = self.post()
            self.assertEqual(r.status_code, 400)

    def test_preflight_and_get(self):
        self.assertEqual(self.client.options(self.url).status_code, 200)
        self.assertEqual(self.client.get(self.url).status_code, 400)

    def test_switched_off_when_the_page_has_no_call_button(self):
        self.page.config = {'theme': {}, 'blocks': []}
        self.page.save(update_fields=['config'])
        r, d = self.post()
        self.assertFalse(d['ok'])
        self.assertFalse(ChatMessage.objects.filter(attachment='voice').exists())

    def test_call_page_offers_voice(self):
        r = self.client.get(f'/p/{self.page.slug}/call/')
        self.assertContains(r, f'/p/{self.page.slug}/call/voice/')
