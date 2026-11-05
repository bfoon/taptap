"""The app's offline-first sync: ETag / 304 on its GETs, the /sync/ pulse, and chat ?peek=1 (core/mobile_sync.py)."""
import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.http import JsonResponse
from django.utils import timezone

from core import chat, mobile_auth, mobile_sync
from core.models import Business
from core.models_chat import ChatRead
from core.models_team import TeamMember
from core.models_voucher_reports import VoucherReport

API = '/api/mobile/v1/'
APP = {'HTTP_X_REQUESTED_WITH': 'TapTapApp'}


@override_settings(AUTH_EMAIL_OTP=False,
                   CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'mob-sync'}})
class MobileSyncTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456', first_name='Baboucarr')
        self.biz = Business.objects.create(user=self.owner, business_name='Kunda Net', owner_name='O', phone='7771234',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.musa = User.objects.create_user('m@x.gm', 'm@x.gm', 'pw-123456', first_name='Musa')
        TeamMember.objects.create(business=self.biz, user=self.musa, is_active=True, role='viewer')
        _dev, self.token = mobile_auth.create_device(self.owner, name='Pixel 7', platform='android')

    def get(self, path, app=True, **kw):
        extra = {'HTTP_AUTHORIZATION': f'Bearer {self.token}', **(APP if app else {}), **kw}
        return self.client.get(path, **extra)

    # ── ETag / 304 ──
    def test_app_get_has_etag_and_304_when_unchanged(self):
        r = self.get(API + 'me/')
        self.assertEqual(r.status_code, 200)
        tag = r['ETag']
        self.assertTrue(tag.startswith('"m1-'))
        self.assertIn('private', r['Cache-Control'])
        again = self.get(API + 'me/', HTTP_IF_NONE_MATCH=tag)
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.content, b'')
        self.assertEqual(again['ETag'], tag)

    def test_weak_tag_from_a_proxy_still_matches(self):
        tag = self.get(API + 'me/')['ETag']
        self.assertEqual(self.get(API + 'me/', HTTP_IF_NONE_MATCH='W/' + tag).status_code, 304)
        self.assertEqual(self.get(API + 'me/', HTTP_IF_NONE_MATCH=f'"nope", {tag}').status_code, 304)

    def test_changed_answer_is_sent_in_full(self):
        tag = self.get(API + 'me/')['ETag']
        self.owner.first_name = 'Foon'
        self.owner.save()
        r = self.get(API + 'me/', HTTP_IF_NONE_MATCH=tag)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.content)['user']['first'], 'Foon')
        self.assertNotEqual(r['ETag'], tag)

    def test_volatile_now_does_not_change_the_tag(self):
        a = self.get(API + 'home/')
        self.assertEqual(a.status_code, 200)
        self.assertEqual(self.get(API + 'home/', HTTP_IF_NONE_MATCH=a['ETag']).status_code, 304)

    def test_not_for_the_website_or_posts(self):
        r = self.get(API + 'me/', app=False)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.has_header('ETag'))
        p = self.client.post(API + 'business/', json.dumps({'id': self.biz.pk}), content_type='application/json',
                             HTTP_AUTHORIZATION=f'Bearer {self.token}', **APP)
        self.assertFalse(p.has_header('ETag'))

    def test_sign_in_is_never_tagged(self):
        rf = RequestFactory()
        req = rf.get(API + 'auth/login/', **APP)
        self.assertFalse(mobile_sync.conditional(req, JsonResponse({'ok': True})).has_header('ETag'))
        req = rf.get(API + 'me/', **APP)
        self.assertFalse(mobile_sync.conditional(req, JsonResponse({'ok': False}, status=400)).has_header('ETag'))

    def test_signed_out_is_still_401(self):
        r = self.client.get(API + 'me/', HTTP_AUTHORIZATION='Bearer ttm_nope', **APP)
        self.assertEqual(r.status_code, 401)

    # ── the pulse ──
    def test_pulse_stamps_move_when_a_customer_reports(self):
        r = self.get(API + 'sync/')
        self.assertEqual(r.status_code, 200)
        d = json.loads(r.content)
        self.assertTrue(d['ok'])
        self.assertEqual(d['v'], 1)
        for k in ('me', 'sales', 'complaints', 'calls', 'vouchers'):
            self.assertIn(k, d['stamps'])
        before = d['stamps']['complaints']
        VoucherReport.objects.create(business=self.biz, code='ABCD12', name='Awa', phone='3001122', message='Not working')
        cache.clear()                                            # past the few seconds the stamps are shared
        after = json.loads(self.get(API + 'sync/').content)['stamps']['complaints']
        self.assertNotEqual(before, after)

    def test_pulse_needs_sign_in(self):
        self.assertEqual(self.client.get(API + 'sync/', **APP).status_code, 401)

    def test_me_says_it_can_sync(self):
        self.assertEqual(json.loads(self.get(API + 'me/').content)['sync'], 1)

    # ── chat ?peek=1 ──
    def test_peek_keeps_the_conversation_unread(self):
        t = chat.team_thread(self.biz)
        m = chat.post(t, self.musa, 'Router 3 is down')
        r = self.get(f'/chat/t/{t.pk}/?peek=1')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.content)['messages'][-1]['id'], m.pk)
        read = ChatRead.objects.filter(thread=t, user=self.owner).first()
        self.assertTrue(read is None or read.last_read < m.pk)
        self.get(f'/chat/t/{t.pk}/')                             # opened for real: read
        self.assertEqual(ChatRead.objects.get(thread=t, user=self.owner).last_read, m.pk)
