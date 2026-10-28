"""Chat video clips (call screen recordings) and the remote-control audit note."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase
from django.utils import timezone

from core import chat, chat_call, chat_media
from core.models import Business
from core.models_chat import ChatImage, ChatMessage, ChatThread

WEBM = b'\x1a\x45\xdf\xa3' + b'\x9f\x42\x86\x81\x01' + b'webm' + b'\x00' * 2000
MP4 = b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 2000


class VideoClipTests(TestCase):
    def setUp(self):
        cache.clear()
        self.a = User.objects.create_user('a', 'a@x.gm', 'pw', first_name='Awa')
        self.b = User.objects.create_user('b', 'b@x.gm', 'pw', first_name='Baboucarr')
        self.biz = Business.objects.create(user=self.a, business_name='Kunda Net', owner_name='Awa', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.t = ChatThread.objects.create(kind='direct', business=self.biz, user_a=self.a, user_b=self.b)

    def test_sniff(self):
        self.assertEqual(chat_media.sniff_video(WEBM), 'video/webm')
        self.assertEqual(chat_media.sniff_video(MP4), 'video/mp4')
        self.assertEqual(chat_media.sniff_video(b'<svg onload=alert(1)>' + b'x' * 100), '')
        self.assertEqual(chat_media.sniff(WEBM), '')             # never accepted as a photo

    def test_save_and_json(self):
        m = chat_media.save_video(self.t, self.b, SimpleUploadedFile('r.webm', WEBM, 'video/webm'), '🎬 Recording', 1920, 99999)
        im = ChatImage.objects.get(message=m)
        self.assertEqual((m.attachment, im.mime, im.width, im.height, im.view_once), ('video', 'video/webm', 1920, 720, False))
        self.assertAlmostEqual((im.expires_at - timezone.now()).days, chat_media.DAYS - 1, delta=1)
        j = chat_media.image_json(m)
        self.assertEqual(j['kind'], 'video'); self.assertFalse(j['gone'])
        self.assertEqual(chat.preview_text(m), '🎬 Recording')

    def test_rejects_non_video(self):
        with self.assertRaises(chat.ChatError):
            chat_media.save_video(self.t, self.b, SimpleUploadedFile('x.webm', b'GIF89a' + b'\0' * 200, 'video/webm'))

    def test_too_big(self):
        with mock.patch.object(chat_media, 'VIDEO_MAX_BYTES', 1000):
            with self.assertRaises(chat.ChatError):
                chat_media.save_video(self.t, self.b, SimpleUploadedFile('r.webm', WEBM, 'video/webm'))

    def test_purged_after_expiry(self):
        m = chat_media.save_video(self.t, self.b, SimpleUploadedFile('r.mp4', MP4, 'video/mp4'))
        ChatImage.objects.filter(message=m).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(chat_media.purge_expired(), 1)
        m.refresh_from_db()
        self.assertEqual(chat_media.image_json(m), {'gone': True, 'opened': False, 'kind': 'video'})

    def _get(self, pk, **headers):
        from core.views_chat import chat_image
        req = RequestFactory().get(f'/chat/img/{pk}/', **headers)
        req.user = self.b; req.tt_business = self.biz
        return chat_image(req, pk)

    def test_range_serving(self):
        m = chat_media.save_video(self.t, self.b, SimpleUploadedFile('r.webm', WEBM, 'video/webm'))
        full = self._get(m.pk)
        self.assertEqual((full.status_code, full['Accept-Ranges'], len(full.content)), (200, 'bytes', len(WEBM)))
        part = self._get(m.pk, HTTP_RANGE='bytes=0-99')
        self.assertEqual((part.status_code, part['Content-Range'], part.content), (206, f'bytes 0-99/{len(WEBM)}', WEBM[:100]))
        tail = self._get(m.pk, HTTP_RANGE='bytes=-10')
        self.assertEqual(tail.content, WEBM[-10:])
        self.assertEqual(self._get(m.pk, HTTP_RANGE=f'bytes={len(WEBM) + 5}-').status_code, 416)

    def test_control_audit_note(self):
        call = {'id': 'c1', 'thread': self.t.pk, 'kind': 'direct', 'caller': self.a.pk, 'caller_name': 'Awa', 'caller_initials': 'A',
                'video': True, 'at': 0, 'targets': [self.b.pk], 'label': 'Awa'}
        cache.set('call:c1', call); cache.add('call:c1:taken', {'uid': self.b.pk, 'at': 0})
        chat_call.log_control(call, self.a, 125)
        chat_call.log_control(call, self.a, 125)                   # a repeated beacon leaves one note
        notes = list(ChatMessage.objects.filter(thread=self.t, system=True).values_list('body', flat=True))
        self.assertEqual(notes, ['🖱 Baboucarr had remote control of Awa’s TapTap tab · 2:05'])
