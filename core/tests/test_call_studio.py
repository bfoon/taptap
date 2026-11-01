"""Calls: the remote-control audit note, the call page's Studio data, and the vendored effect models.

The browser side (pointer receipts, laser, drawing, guardian, control log, effects) is covered by real-browser
tests in tools/call_tests/ — they need Playwright and Chromium, so they are not part of `manage.py test`.
"""
import json
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from core import chat, chat_call
from core.models import Business
from core.models_chat import ChatMessage
from core.models_team import TeamMember

STATIC = Path(settings.BASE_DIR) / 'static'


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('fatou', password='x', first_name='Fatou', last_name='Sowe')
        self.helper = User.objects.create_user('lamin', password='x', first_name='Lamin', last_name='Jallow')
        self.biz = Business.objects.create(user=self.owner, business_name='Banjul Wi-Fi', owner_name='Fatou', phone='1',
                                           trial_ends_at=timezone.now() + timedelta(days=7))
        TeamMember.objects.create(business=self.biz, user=self.helper, role='manager')
        self.thread = chat.direct_thread(self.biz, self.owner, self.helper)

    def live_call(self):
        call = chat_call.start(self.thread, self.helper, video=False)
        chat_call.accept(call, self.owner)
        return call


class ControlSummary(Base):
    def test_counts_pages_and_last_actions(self):
        out = chat_call.control_summary({'actions': 3, 'blocked': 1, 'pages': ['/vouchers/', '/plans/'], 'last': ['Save plan', '🚫 Delete voucher']})
        self.assertIn('3 actions, 1 blocked', out)
        self.assertIn('pages: /vouchers/, /plans/', out)
        self.assertIn('last: Save plan · 🚫 Delete voucher', out)

    def test_only_clean_values_reach_the_chat(self):
        out = chat_call.control_summary({'actions': 'lots', 'blocked': -4, 'pages': ['https://evil.example/x', '/ok/', 7, '/' + 'a' * 300],
                                         'last': ['  spaced\n\nout  ', {'x': 1}, 'b' * 300] + ['n'] * 20})
        self.assertNotIn('evil', out)
        self.assertIn('/ok/', out)
        self.assertTrue(all(len(line) < 450 for line in out.split('\n')))
        self.assertIn('spaced out', out)
        self.assertLessEqual(out.count('·'), 5, 'at most six recent actions')
        self.assertNotIn('{', out, 'non-text entries are dropped')

    def test_nothing_to_say(self):
        self.assertEqual(chat_call.control_summary(None), '')
        self.assertEqual(chat_call.control_summary({'actions': 0, 'pages': [], 'last': []}), '')
        self.assertEqual(chat_call.control_summary('not a dict'), '')

    def test_one_action_is_singular(self):
        self.assertIn('1 action', chat_call.control_summary({'actions': 1}))
        self.assertNotIn('1 actions', chat_call.control_summary({'actions': 1}))


class ControlAuditNote(Base):
    def test_note_includes_the_summary(self):
        call = self.live_call()
        chat_call.log_control(call, self.owner, 75, {'actions': 2, 'pages': ['/'], 'last': ['Save plan', 'Delete voucher']})
        note = ChatMessage.objects.filter(thread=self.thread, system=True, body__contains='remote control').latest('pk').body
        self.assertIn('Lamin had remote control of Fatou’s TapTap tab · 1:15', note)
        self.assertIn('2 actions', note)
        self.assertIn('last: Save plan · Delete voucher', note)

    def test_note_without_summary_still_works(self):
        call = self.live_call()
        chat_call.log_control(call, self.owner, 10)
        body = ChatMessage.objects.filter(thread=self.thread, body__contains='remote control').latest('pk').body
        self.assertTrue(body.endswith('0:10'))

    def test_call_window_sends_it_through_the_log_action(self):
        call = self.live_call()
        self.client.force_login(self.owner)
        r = self.client.post('/chat/call/act/', json.dumps({'action': 'log', 'id': call['id'], 'event': 'control', 'secs': 30,
                                                            'summary': {'actions': 4, 'blocked': 1, 'pages': ['/vouchers/'], 'last': ['Save plan']}}),
                             content_type='application/json', HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertTrue(r.json()['ok'])
        note = ChatMessage.objects.filter(thread=self.thread, body__contains='remote control').latest('pk').body
        self.assertIn('4 actions, 1 blocked', note)
        self.assertIn('pages: /vouchers/', note)

    def test_logged_once_even_if_sent_twice(self):
        call = self.live_call()
        chat_call.log_control(call, self.owner, 30, {'actions': 1})
        chat_call.log_control(call, self.owner, 30, {'actions': 1})
        self.assertEqual(ChatMessage.objects.filter(thread=self.thread, body__contains='remote control').count(), 1)


class CallPage(Base):
    def page(self, user):
        self.client.force_login(user)
        return self.client.get(f'/chat/call/?thread={self.thread.pk}&video=1')

    def test_studio_is_loaded_with_its_models(self):
        r = self.page(self.owner)
        html = r.content.decode()
        self.assertIn('js/call-fx.js', html)
        self.assertLess(html.index('js/call-fx.js'), html.index('js/chat-call.js'), 'the effects engine must load first')
        self.assertIn('data-fx-base="/static/vendor/mediapipe/"', html)
        self.assertIn('data-brand="#1769e0"', html)
        self.assertIn('camera=(self)', r['Permissions-Policy'])

    def test_name_tag_title_business_or_support(self):
        self.assertIn('data-me-title="Banjul Wi-Fi"', self.page(self.owner).content.decode())
        agent = User.objects.create_user('agent', password='x', is_superuser=True)
        self.assertIn('data-me-title="TapTap Support"', self.page(agent).content.decode())


class VendoredModels(TestCase):
    """call-fx.js loads these from TapTap itself; a missing file would break effects without any server error."""
    FILES = {
        'selfie_segmentation': ['selfie_segmentation.js', 'selfie_segmentation.binarypb', 'selfie_segmentation.tflite',
                                'selfie_segmentation_landscape.tflite', 'selfie_segmentation_solution_simd_wasm_bin.js',
                                'selfie_segmentation_solution_simd_wasm_bin.wasm'],
        'face_mesh': ['face_mesh.js', 'face_mesh.binarypb', 'face_mesh_solution_packed_assets.data', 'face_mesh_solution_packed_assets_loader.js',
                      'face_mesh_solution_simd_wasm_bin.js', 'face_mesh_solution_simd_wasm_bin.wasm'],
    }

    def test_model_files_are_present(self):
        for folder, files in self.FILES.items():
            for f in files:
                p = STATIC / 'vendor' / 'mediapipe' / folder / f
                self.assertTrue(p.exists() and p.stat().st_size > 0, f'missing {p}')
        self.assertIn('Apache License', (STATIC / 'vendor' / 'mediapipe' / 'NOTICE.md').read_text())

    def test_scripts_wire_up(self):
        fx = (STATIC / 'js' / 'call-fx.js').read_text()
        self.assertIn('window.TTCallFX', fx)
        self.assertIn('selfie_segmentation/selfie_segmentation.js', fx)
        self.assertIn('face_mesh/face_mesh.js', fx)
        tab = (STATIC / 'js' / 'chat.js').read_text()
        self.assertIn('.tt-guard', tab.split('var PRIVATE = ')[1].split(';')[0], 'the guardian bubble must be out of remote reach')
        self.assertIn("e.isTrusted", tab.split('function guard')[1].split('GUARD = {')[0])
