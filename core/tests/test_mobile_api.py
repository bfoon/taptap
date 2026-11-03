"""The mobile app: token sign-in on a normal session, so chat, calls and Fit behave exactly like the website."""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core import chat, mobile_auth
from core.models import Business, VoucherSale
from core.models_mobile import MobileDevice
from core.models_team import TeamMember
from core.models_voucher_reports import VoucherReport

API = '/api/mobile/v1/'


@override_settings(AUTH_EMAIL_OTP=False,
                   CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'mob'}})
class MobileApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456', first_name='Baboucarr')
        self.biz = Business.objects.create(user=self.owner, business_name='Kunda Net', owner_name='O', phone='7771234',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.viewer = User.objects.create_user('v@x.gm', 'v@x.gm', 'pw-123456', first_name='Musa')
        TeamMember.objects.create(business=self.biz, user=self.viewer, is_active=True, role='viewer')

    def login(self, email='o@x.gm', pw='pw-123456'):
        r = self.client.post(API + 'auth/login/', json.dumps({'email': email, 'password': pw, 'device': 'Pixel 7', 'platform': 'android'}),
                             content_type='application/json')
        return r, json.loads(r.content)

    def get(self, path, token, **kw):
        return self.client.get(path, HTTP_AUTHORIZATION=f'Bearer {token}', **kw)

    def post(self, path, token, body):
        return self.client.post(path, json.dumps(body), content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {token}')

    def test_login_gives_token_and_me(self):
        r, d = self.login()
        self.assertTrue(d['ok']); self.assertTrue(d['token'].startswith('ttm_'))
        self.client.cookies.clear()
        me = json.loads(self.get(API + 'me/', d['token']).content)
        self.assertEqual(me['business']['name'], 'Kunda Net')
        self.assertTrue(me['can']['sales']); self.assertTrue(me['can']['customer_calls'])   # nobody picked = owner answers
        self.assertEqual(MobileDevice.objects.get().name, 'Pixel 7')

    def test_session_key_never_leaks(self):
        _, d = self.login()
        self.client.cookies.clear()
        r = self.get(API + 'me/', d['token'])
        self.assertNotIn('sessionid', r.cookies)

    def test_wrong_password_and_rate_limit(self):
        for _ in range(8):
            self.assertEqual(self.login(pw='nope')[0].status_code, 400)
        self.assertEqual(self.login()[0].status_code, 429)

    @override_settings(AUTH_EMAIL_OTP=True)
    def test_otp_flow(self):
        with mock.patch('core.auth_security.send_code', return_value=(True, 'Code sent.')), \
             mock.patch('core.auth_security.check_code', side_effect=[(False, 'Wrong code.'), (True, '')]):
            _, d = self.login()
            self.assertTrue(d['otp'])
            bad = json.loads(self.client.post(API + 'auth/verify/', json.dumps({'ticket': d['ticket'], 'code': '1'}), content_type='application/json').content)
            self.assertFalse(bad['ok'])
            ok = json.loads(self.client.post(API + 'auth/verify/', json.dumps({'ticket': d['ticket'], 'code': '2'}), content_type='application/json').content)
            self.assertTrue(ok['token'].startswith('ttm_'))

    def test_bad_token_is_401(self):
        r = self.client.get(API + 'me/', HTTP_AUTHORIZATION='Bearer ttm_nope')
        self.assertEqual(r.status_code, 401)

    def test_logout_and_password_change_end_it(self):
        _, d = self.login()
        self.client.cookies.clear()
        self.assertEqual(self.post(API + 'auth/logout/', d['token'], {}).status_code, 200)
        self.assertEqual(self.get(API + 'me/', d['token']).status_code, 401)
        _, d2 = self.login()
        self.client.cookies.clear()
        self.owner.set_password('new-pass-999'); self.owner.save()
        self.assertEqual(self.get(API + 'me/', d2['token']).status_code, 401)

    def test_website_chat_and_fit_endpoints_work_with_token(self):
        _, d = self.login()
        self.client.cookies.clear()
        st = json.loads(self.get('/chat/state/', d['token']).content)
        self.assertTrue(st['ok'])
        team = chat.team_thread(self.biz)
        r = json.loads(self.post('/chat/send/', d['token'], {'thread': team.pk, 'body': 'Hello from the phone'}).content)
        self.assertTrue(r['ok'])                                  # POST without CSRF works for bearer requests
        self.assertEqual(self.get('/chat/call/poll/', d['token']).status_code, 200)
        self.assertEqual(self.get('/fit/state/', d['token']).status_code, 200)

    def test_csrf_still_enforced_for_cookie_sessions(self):
        from django.test import Client
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.owner)
        r = c.post('/chat/send/', json.dumps({'thread': chat.team_thread(self.biz).pk, 'body': 'x'}), content_type='application/json')
        self.assertEqual(r.status_code, 403)

    def test_home_and_sales(self):
        VoucherSale.objects.create(business=self.biz, plan_name='5 Hours', amount=10, sold_at=timezone.now())
        VoucherSale.objects.create(business=self.biz, plan_name='24 Hours', amount=25, sold_at=timezone.now())
        _, d = self.login()
        self.client.cookies.clear()
        h = json.loads(self.get(API + 'home/', d['token']).content)
        self.assertEqual(h['sales']['total'], 35.0); self.assertEqual(h['sales']['count'], 2)
        s = json.loads(self.get(API + 'sales/', d['token']).content)
        self.assertEqual(len(s['sales']), 2); self.assertEqual(s['by_plan'][0]['plan_name'], '24 Hours')
        same = json.loads(self.get(API + 'sales/', d['token'], data={'fp': s['fp']}).content)
        self.assertFalse(same['changed'])
        plan = json.loads(self.get(API + 'sales/', d['token'], data={'plan': '5 Hours'}).content)
        self.assertEqual(plan['plan_detail']['amount'], 10.0)

    def test_viewer_cannot_see_complaints(self):
        _, d = self.login('v@x.gm')
        self.client.cookies.clear()
        me = json.loads(self.get(API + 'me/', d['token']).content)
        self.assertFalse(me['can']['complaints']); self.assertTrue(me['can']['sales'])
        self.assertEqual(self.get(API + 'complaints/', d['token']).status_code, 403)

    def test_complaints_list_and_close(self):
        r = VoucherReport.objects.create(business=self.biz, code='ABCD12', name='Awa', phone='3001122', message='Not working')
        _, d = self.login()
        self.client.cookies.clear()
        c = json.loads(self.get(API + 'complaints/', d['token']).content)
        self.assertEqual(c['open'], 1); self.assertEqual(c['cases'][0]['code'], 'ABCD12')
        self.assertFalse(c['cases'][0]['now']['state'] == 'ok')       # the code is not in TapTap
        out = json.loads(self.post(API + f'complaints/{r.pk}/', d['token'], {'action': 'close', 'note': 'Called her'}).content)
        self.assertTrue(out['ok'])
        r.refresh_from_db(); self.assertEqual(r.status, 'resolved'); self.assertEqual(r.staff_note, 'Called her')

    def test_devices_list_and_revoke(self):
        _, a = self.login()
        _, b = self.login()
        self.client.cookies.clear()
        rows = json.loads(self.get(API + 'devices/', a['token']).content)['devices']
        self.assertEqual(len(rows), 2)
        other = next(x for x in rows if not x['this'])
        self.post(API + 'devices/', a['token'], {'id': other['id']})
        self.assertEqual(self.get(API + 'me/', b['token']).status_code, 401)

    def test_push_token_moves_to_newest_phone(self):
        _, a = self.login()
        _, b = self.login()
        self.client.cookies.clear()
        self.post(API + 'push/', a['token'], {'token': 'fcm-1'})
        self.post(API + 'push/', b['token'], {'token': 'fcm-1'})
        self.assertEqual(MobileDevice.objects.filter(push_token='fcm-1').count(), 1)

    def test_push_noop_without_firebase(self):
        from core import mobile_push
        self.assertFalse(mobile_push.configured())
        self.assertEqual(mobile_push.send([self.owner.pk], {'data': {}}), 0)
